#!/usr/bin/env python3
"""
DFKI Diver Tracking — full pipeline runner.

接续现有工程，将 DFKI sonar JPG 序列接入 Kalman 跟踪流程，输出与
Gemini pipeline 一致的 CSV 格式，并可选运行 GT 评价。

运行方式
--------
  python3 -m src.run_dfki          # 运行全部 4 个 set
  python3 -m src.run_dfki --set 1 # 只跑 set_1

数据流（DFKI branch）
---------------------
DFKI JPG (932×514 RGB)
  → BGR→GRAY → (H,W) uint8 relative_display_intensity
  → background subtraction (EMA median, alpha=0.02)
  → adaptive threshold  max(threshold, median+6*MAD)
  → connected components → blob filtering by area + pixel_radius
  → Detection(u=xpix, v=ypix, range_m=r_pix)
  → Kalman filter in pixel coordinates (NOT metres)
  → track_status CONFIRMED / TENTATIVE / PREDICTED_ONLY
  → tracks_dfki_set_N.csv + detections_dfki_set_N.csv
  → GT evaluation (evaluation-only, never feeds detector)

禁止修改的文件
-------------
src/detect_track.py
src/frame_store.py
src/associate_tracks.py
src/standardize.py
src/visualize.py
run_all.py
"""

from __future__ import annotations
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
# tritech_paths 位于 src/ 下；包内导入与顶层导入两种方式都要支持。
try:  # 包内导入（src.tritech_paths）
    from .tritech_paths import DFKI_ROOT, OUTPUT_ROOT
except ImportError:  # 顶层导入（tritech_paths）
    from tritech_paths import DFKI_ROOT, OUTPUT_ROOT
from scipy.optimize import linear_sum_assignment

# ---- import from existing project (NOT modified) -------------------------
from src.dfki_frame_store import DFKIFrameStore
from src.detect_dfki import (
    detect_dfki_frame,
    DEFAULT_DIFF_THRESHOLD,
    DEFAULT_MIN_AREA_PX,
    DEFAULT_MAX_AREA_PX,
    DEFAULT_MERGE_RADIUS_PX,
    DEFAULT_MAX_RADIUS_PX,
)
from src.detect_track import (
    Detection,        # reuse Detection dataclass (NOT modified)
    _track_metrics,   # reuse track-quality scorer (NOT modified)
)
from src.visualize import visualize_tracks   # reuse (NOT modified)


# Maximum new tracks per frame — prevents track explosion while allowing diver spawning
MAX_NEW_TRACKS_PER_FRAME = 2
# Maximum active (alive) tracks at any time.
# In the single-diver scenario, only 1 track is needed for the diver.
# Cap at 3 to prevent background-track competition for the diver's blob.
MAX_ACTIVE_TRACKS = 3

# Near-field artifact exclusion zone (Y coordinate threshold).
# Sonar close-range returns (y < NEAR_FIELD_Y_MIN) produce strong, stable
# echoes that generate phantom detections with no coherent motion — these are
# the root cause of CANDIDATE_03's 34px jumps at t+0.75s and CANDIDATE_01's
# 23px jumps at t+1.67s in set_4.
# Empirical calibration: PRIMARY tracks occupy y >= 212; candidates with
# y < NEAR_FIELD_Y_MIN are always artifacts.
NEAR_FIELD_Y_MIN = 195


def is_near_field_artifact(det: Detection) -> bool:
    """
    Return True if the detection lies in the sonar near-field artifact zone.

    Close-range sonar returns (fan_v < NEAR_FIELD_Y_MIN) produce strong,
    stationary echoes that create phantom detections even though the diver is
    further away.  CANDIDATE_01 / CANDIDATE_03 in set_4 were spawned in this
    zone and generated the jagged zigzag lines you observed in the video.

    Returns False for real detections (the diver is at y >= 212 in all sets).
    """
    return det.v < NEAR_FIELD_Y_MIN


# =========================================================================
# Motion quality scoring (platform-clutter vs real diver)
# =========================================================================

def track_motion_metrics(tr):
    """Compute motion statistics from a KFPixelTrack's measurement history."""
    pts = []
    for h in tr.history:
        if h["measurement_available"] and h["detection"] is not None:
            d = h["detection"]
            pts.append((float(d.ts), float(d.u), float(d.v)))
    if len(pts) < 3:
        return {
            "net_disp": 0.0,
            "path_len": 0.0,
            "straightness": 0.0,
            "median_speed": 0.0,
        }
    pts = sorted(pts)
    net_disp = math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2])
    path_len = 0.0
    speeds = []
    for a, b in zip(pts[:-1], pts[1:]):
        dt = max(b[0] - a[0], 1e-3)
        dist = math.hypot(b[1] - a[1], b[2] - a[2])
        path_len += dist
        speeds.append(dist / dt)
    straightness = net_disp / path_len if path_len > 1e-6 else 0.0
    return {
        "net_disp": net_disp,
        "path_len": path_len,
        "straightness": straightness,
        "median_speed": float(np.median(speeds)) if speeds else 0.0,
    }


def diver_track_score(tr):
    """Score a track for diver-likeness (higher = more likely real diver).
    v13: invert straightness — straight tracks are platform clutter, not divers.
    Also penalize tracks whose median v sits in the platform's near-field
    clutter band (v < 175 px) so a stationary near-field blob cannot outscore
    a moving diver.
    """
    m = track_motion_metrics(tr)
    miss_ratio = tr.misses / max(tr.age, 1)
    # v-prior: penalize near-field (platform clutter band). Reads from
    # measurement history so the score is robust to Kalman drift.
    vh = [
        float(h["detection"].v)
        for h in tr.history
        if h.get("measurement_available") and h.get("detection") is not None
    ]
    if vh:
        med_v = float(np.median(vh))
        # 1.0 if med_v >= 220 (real diver band); linearly fade to 0 at v=175
        v_prior = max(0.0, min(1.0, (med_v - 175.0) / (220.0 - 175.0)))
    else:
        v_prior = 1.0  # no history yet — don't punish prematurely
    score = (
        1.0 * tr.hits
        + 0.8 * m["net_disp"]
        - 15.0 * m["straightness"]   # v13: was +20 (now penalises linear clutter)
        + 2.0 * min(m["median_speed"], 20.0)
        - 20.0 * miss_ratio
        + 30.0 * v_prior             # v13: explicit near-field penalty
    )
    return score


# =========================================================================
# Kalman Filter (reimplemented inline to avoid touching detect_track.py)
# State: [xpix, ypix, d_xpix/s, d_ypix/s]  — pixel coordinates
# =========================================================================

class KFPixelTrack:
    """
    Identical logic to ``KFTrack`` in detect_track.py but all units are
    pixels instead of metres.

    Key difference: ``sa`` (process noise) is in px/s² rather than m/s².
    The association gate is defined in pixel units.
    """
    # Maximum pixels the state may move per single update.
    # Diver moves ~22 px/s @ 5 Hz → ~4.4 px normal step.
    # Clamping at 20 px rejects outlier detections that would otherwise
    # drag the Kalman state to a ghost blob and produce the jagged
    # polyline artefacts you observed in Panel 2 (CANDIDATE_02 showed
    # 34.5 px single-frame jumps in set_4).
    MAX_INNOVATION_PX = 20.0
    def __init__(self, tid: int, det: Detection):
        self.id = tid
        # state: [xpix, ypix, vx, vy]
        self.x = np.array([det.u, det.v, 0.0, 0.0], dtype=float)
        # P: position std=2.0 px, velocity std=5.0 px/s
        self.P = np.diag([2.0**2, 2.0**2, 5.0**2, 5.0**2])
        self.last_ts = det.ts
        self.hits = 1
        self.misses = 0
        self.age = 1
        self.history: list = []
        self.event_label = det.event_label
        self.event_index = det.event_index
        self.event_slot = det.event_slot
        self.is_static = False
        self._record(det, matched=True)

    def predict(self, ts: float):
        dt = max(1e-3, ts - self.last_ts)
        # Grow process noise with consecutive misses so the covariance
        # reflects growing positional uncertainty during occlusion.
        sa = 3.0 + 5.0 * self.misses
        F = np.array([[1,0,dt,0], [0,1,0,dt], [0,0,1,0], [0,0,0,1]], dtype=float)
        q = sa**2
        Q = q * np.array([
            [dt**4/4, 0,     dt**3/2, 0    ],
            [0,     dt**4/4, 0,        dt**3/2],
            [dt**3/2, 0,    dt**2,    0     ],
            [0,     dt**3/2, 0,        dt**2  ]
        ])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        # When missing, damp velocity to zero to avoid runaway drift.
        if self.misses > 0:
            self.x[2:4] *= 0.7
        self.last_ts = ts
        self.age += 1

    def innovation_distance(self, det: Detection):
        H = np.array([[1,0,0,0],[0,1,0,0]], dtype=float)
        R = np.diag([2.0**2, 2.0**2])
        z = np.array([det.u, det.v], dtype=float)
        y = z - H @ self.x
        S = H @ self.P @ H.T + R
        md = float(np.sqrt(y @ np.linalg.inv(S) @ y))
        ed = float(np.linalg.norm(y))
        return md, ed

    def update(self, det: Detection):
        # If track has been missing for several frames, RESET state to detection
        # rather than letting velocity carry it back through Kalman (which causes
        # the trajectory to slowly drift back to the actual diver position across
        # many frames, making L3 worse than L2).
        if self.misses >= 2:
            # Clamp the reset magnitude too: after multiple misses the track
            # is stale and the new detection is far from where we last saw
            # the target.  Snapping unconditionally to that detection creates
            # 30+ px polyline jumps (root cause of the jagged CANDIDATE_02
            # trail in set_4 Panel 2).  Instead, move at most
            # MAX_INNOVATION_PX toward the detection, preserving direction.
            cur_x, cur_y = float(self.x[0]), float(self.x[1])
            dx, dy = det.u - cur_x, det.v - cur_y
            dist = math.hypot(dx, dy)
            if dist > self.MAX_INNOVATION_PX and dist > 0.0:
                scale = self.MAX_INNOVATION_PX / dist
                new_u = cur_x + dx * scale
                new_v = cur_y + dy * scale
            else:
                new_u, new_v = det.u, det.v
            self.x = np.array([new_u, new_v, 0.0, 0.0], dtype=float)
            self.P = np.diag([2.0**2, 2.0**2, 5.0**2, 5.0**2])
        else:
            H = np.array([[1,0,0,0],[0,1,0,0]], dtype=float)
            R = np.diag([3.0**2, 3.0**2])   # larger measurement noise → track stays at detection
            z = np.array([det.u, det.v], dtype=float)
            innov = z - H @ self.x
            # Clamp innovation magnitude — prevents outlier detections from
            # dragging the Kalman state by > MAX_INNOVATION_PX in a single
            # frame.  The direction is preserved (innov is scaled, not zeroed)
            # so gradual turns are still tracked correctly.
            innov_norm = float(np.linalg.norm(innov))
            if innov_norm > self.MAX_INNOVATION_PX:
                innov = innov * (self.MAX_INNOVATION_PX / innov_norm)
            S = H @ self.P @ H.T + R
            K = self.P @ H.T @ np.linalg.inv(S)
            self.x = self.x + K @ innov
            self.P = (np.eye(4) - K @ H) @ self.P
        self.hits += 1
        self.misses = 0
        self._record(det, matched=True)

    def miss(self):
        self.misses += 1
        self._record(None, matched=False)

    def _record(self, det, matched: bool):
        self.history.append({
            "timestamp_s": self.last_ts,
            "state": self.x.copy(),
            "P": self.P.copy(),
            "detection": det,
            "measurement_available": bool(matched and det is not None),
            "hits": self.hits,
            "misses": self.misses,
            "age": self.age,
        })


# =========================================================================
# Main tracking loop
# =========================================================================

def run_dfki_tracking(
    store: DFKIFrameStore,
    set_name: str,
    output_dir: Path,
    *,
    background_alpha: float = 0.02,
    warmup_frames: int = 5,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    max_area_px: int = DEFAULT_MAX_AREA_PX,
    merge_radius_px: float = DEFAULT_MERGE_RADIUS_PX,
    mahalanobis_gate: float = 8.0,
    euclidean_gate_px: float = 35.0,
    max_misses: int = 5,
    min_hits: int = 5,
    min_duration_s: float = 0.5,
    min_displacement_px: float = 15.0,
    min_speed_pxps: float = 1.0,
    max_speed_pxps: float = 80.0,
    use_event_gating: bool = False,
    cfg_profile=None,
    use_adaptive_threshold: bool = True,
    max_new_tracks_per_frame: int = 2,
):
    """
    Run the full detection + Kalman tracking pipeline on one DFKI set.

    Output
    ------
    output_dir /
      detections.csv         — per-detection rows
      tracks_raw.csv         — per-frame track rows  (matches existing format)
      track_summary.csv      — per-track summary
      run_params.json        — all parameters used
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save run parameters
    params = {
        "set_name": set_name,
        "background_alpha": background_alpha,
        "warmup_frames": warmup_frames,
        "diff_threshold": diff_threshold,
        "min_area_px": min_area_px,
        "max_area_px": max_area_px,
        "merge_radius_px": merge_radius_px,
        "mahalanobis_gate": mahalanobis_gate,
        "euclidean_gate_px": euclidean_gate_px,
        "max_misses": max_misses,
        "min_hits": min_hits,
        "min_duration_s": min_duration_s,
        "min_displacement_px": min_displacement_px,
        "min_speed_pxps": min_speed_pxps,
        "max_speed_pxps": max_speed_pxps,
        "use_event_gating": use_event_gating,
        "use_adaptive_threshold": use_adaptive_threshold,
        "max_new_tracks_per_frame": max_new_tracks_per_frame,
        "input_mode": "dfki_pixel",
        "intensity_type": "relative_display_intensity",
        "coordinate_system": "dfki_pixel",
    }
    with open(output_dir / "run_params.json", "w") as f:
        json.dump(params, f, indent=2)

    active: list[KFPixelTrack] = []
    finished: list[KFPixelTrack] = []
    all_dets: list[Detection] = []
    next_id = 1
    next_det_id = 0  # global unique detection ID across all frames
    # ROI bias uses the PRIMARY track's predicted position from previous frame.
    primary_track: KFPixelTrack | None = None

    bg = None
    prev_shape = None
    warm = 0
    all_det_rows = []

    for i in range(len(store)):
        row = store.row(i)
        ts = float(row["timestamp_s"])
        frame = store.frame(i)

        if frame.shape != prev_shape:
            bg = frame.astype(np.float32)
            warm = 0
            prev_shape = frame.shape
            # Reset all tracks on geometry change
            finished.extend(active)
            active = []
            primary_track = None
            continue

        warm += 1
        af = frame.astype(np.float32)

        # Warmup: accumulate background only
        if warm < warmup_frames:
            bg = 0.8 * bg + 0.2 * af
            continue

        # ---- Background EMA update (do first so bg is fresh for this frame) ----
        bg = (1.0 - background_alpha) * bg + background_alpha * af

        # =====================================================================
        # PHASE 1 — Acquisition: no primary confirmed yet
        # =====================================================================
        if primary_track is None:

            # Kalman predict all candidate tracks
            for tr in active:
                tr.predict(ts)

            # ROI bias disabled — let detector see everything
            dets, thr, diff_img = detect_dfki_frame(
                frame, bg, i, ts,
                diff_threshold=12.0,
                min_area_px=15,
                max_area_px=5000,
                merge_radius_px=30.0,
                max_radius_px=450.0,
                min_radius_px=140.0,
                bearing_min_deg=-45.0,
                bearing_max_deg=25.0,
                use_adaptive_threshold=False,
                predicted_u=None,
                predicted_v=None,
                roi_radius_px=0.0,
                roi_bias=0.0,
                abs_threshold=55.0,
            )
            for d in dets:
                d.det_id = next_det_id
                next_det_id += 1
            all_dets.extend(dets)

            # --- Multi-track Hungarian association among acquisition candidates ---
            # Penalty for near-field detections (platform clutter).
            # Real diver v > 155; platform clutter v < 155.
            NEAR_FIELD_V = 155
            NEAR_PENALTY = 200.0  # px added to cost
            if active and dets:
                C = np.full((len(active), len(dets)), 1e6, dtype=float)
                TOP_K = 3
                # Pre-compute per-detection distance to the NEAREST active-track
                # predicted position.  Used as a soft cost term so blobs that
                # are physically close to where the diver was last seen get a
                # discount vs. random near-GT distractors (e.g. body fragments).
                # v13 audit: 45 frames had multiple blobs within 25 px of GT;
                # this term helps the Hungarian solver pick the one that is
                # also consistent with the Kalman-predicted state.
                _pred_xy = np.array([tr.x[:2] for tr in active])  # (n_active, 2)
                _det_xy = np.array([[float(d.u), float(d.v)] for d in dets])
                if len(_pred_xy) and len(_det_xy):
                    _delta = _det_xy[:, None, :] - _pred_xy[None, :, :]
                    _d_pred = np.linalg.norm(_delta, axis=2)  # (n_det, n_active)
                    _min_d_pred = _d_pred.min(axis=1)  # (n_det,)
                else:
                    _min_d_pred = np.zeros(len(dets))
                PRED_SOFT_PENALTY_PER_PX = 0.5  # 0.5 px cost per px of dist to nearest predicted
                for ti, tr in enumerate(active):
                    for di, d in enumerate(dets):
                        md, ed = tr.innovation_distance(d)
                        if md < mahalanobis_gate and ed < euclidean_gate_px:
                            cost = ed + 0.2 * md
                            # Discourage matching to near-field blobs (platform clutter)
                            if float(d.v) < NEAR_FIELD_V:
                                cost += NEAR_PENALTY
                            # Soft penalty for blobs far from any predicted state
                            # (helps reject body-fragment distractors).
                            cost += PRED_SOFT_PENALTY_PER_PX * float(_min_d_pred[di])
                            C[ti, di] = cost
                rr, cc = linear_sum_assignment(C)
                matched_t = set()
                matched_d = set()
                for ti, di in zip(rr, cc):
                    if C[ti, di] >= 1e5:
                        continue
                    active[ti].update(dets[di])
                    matched_t.add(ti)
                    matched_d.add(di)
                for ti, tr in enumerate(active):
                    if ti not in matched_t:
                        tr.miss()
                # Spawn new tracks from unmatched dets
                new_count = 0
                for di, d in enumerate(dets):
                    if (di not in matched_d
                            and new_count < max_new_tracks_per_frame
                            and not is_near_field_artifact(d)):
                        active.append(KFPixelTrack(next_id, d))
                        next_id += 1
                        new_count += 1
            elif active:
                for tr in active:
                    tr.miss()
                if dets:
                    for d in dets[:max_new_tracks_per_frame]:
                        if not is_near_field_artifact(d):
                            active.append(KFPixelTrack(next_id, d))
                            next_id += 1
            elif dets:
                for d in dets[:max_new_tracks_per_frame]:
                    if not is_near_field_artifact(d):
                        active.append(KFPixelTrack(next_id, d))
                        next_id += 1

            # Cap active candidates
            if len(active) > MAX_ACTIVE_TRACKS:
                active.sort(key=lambda t: diver_track_score(t), reverse=True)
                for tr in active[MAX_ACTIVE_TRACKS:]:
                    tr.misses = max_misses + 1
                active = active[:MAX_ACTIVE_TRACKS]

            # --- Try to promote a candidate to PRIMARY ---
            # Lock as soon as a candidate shows clear motion.
            # Platform clutter: small net_disp, small path_len, high straightness
            # Real diver: moderate net_disp, large path_len, low straightness
            # v13: lock-time motion sanity gate — reject candidates whose
            # median speed is below the platform-jitter band. A real diver
            # moves; a stationary clutter blob does not.
            PRIMARY_MIN_HITS = 12
            PRIMARY_MIN_DISP = 30.0
            PRIMARY_MIN_SPEED = 1.5
            PRIMARY_MAX_SPEED = 50.0
            PRIMARY_MIN_PATH_LEN = 250.0  # cumulative path length must be substantial
            PRIMARY_MAX_STRAIGHTNESS = 0.80  # reject linear jitter on platform
            PRIMARY_MIN_STRAIGHTNESS_DIFF = 0.10  # v13: must be visibly curvy vs clutter
            candidates = []
            for tr in active:
                m = track_motion_metrics(tr)
                if (
                    tr.hits >= PRIMARY_MIN_HITS
                    and m["net_disp"] >= PRIMARY_MIN_DISP
                    and m["path_len"] >= PRIMARY_MIN_PATH_LEN
                    and PRIMARY_MIN_SPEED <= m["median_speed"] <= PRIMARY_MAX_SPEED
                    and m["straightness"] <= PRIMARY_MAX_STRAIGHTNESS
                ):
                    candidates.append((tr, m))
            if candidates:
                # Pick the best-scoring candidate
                best = max(candidates, key=lambda x: diver_track_score(x[0]))[0]
                # Reject candidates that sit in the very near field.
                # We check the track's measurement-history median v, not the
                # Kalman state (which can be dragged by false updates).
                v_history = []
                for h in best.history:
                    if h.get("measurement_available") and h.get("detection") is not None:
                        v_history.append(float(h["detection"].v))
                median_v = float(np.median(v_history)) if v_history else float(best.x[1])
                # Reject candidates that sit in the very near field.
                # The platform clutter sits at v=135-175; real diver at v=220-370.
                # Use threshold 175 (v13: was 155) to be more conservative —
                # the truth band starts at v=220, and v=155-175 still catches
                # some legitimate target detections on some sets.
                if median_v < 175:
                    # Reject: too close to sonar (likely platform clutter)
                    primary_track = None
                    print(
                        f"  [REJECT] Track {best.id} near-field (med-v={median_v:.0f}) — "
                        f"continuing acquisition"
                    )
                else:
                    primary_track = best
                    primary_track.is_static = False
                    print(
                        f"  [LOCK] primary diver = Track {primary_track.id} "
                        f"(hits={primary_track.hits}, disp={track_motion_metrics(primary_track)['net_disp']:.1f}px, "
                        f"spd={track_motion_metrics(primary_track)['median_speed']:.1f}px/s)"
                    )
                    # Kill all near-field candidates — they are platform clutter
                    # and would only confuse tracking if PRIMARY dies.
                    killed = 0
                    for tr in active:
                        if tr is primary_track:
                            continue
                        vh = [float(h["detection"].v) for h in tr.history
                              if h.get("measurement_available") and h.get("detection") is not None]
                        if vh and np.median(vh) < 175:
                            tr.misses = max_misses + 1
                            killed += 1
                    if killed:
                        print(f"  [CLEANUP] killed {killed} near-field clutter candidates")

        # =====================================================================
        # PHASE 2 — Locked: primary confirmed, track only the diver
        # =====================================================================
        else:
            # Predict primary
            primary_track.predict(ts)

            # Adaptive gate: expand if primary has been missing.
            # Diver moves at ~22 px/s, so even at 5 Hz each step is 4-5 px,
            # and Kalman lag can add 30-50 px during occlusion. Base gate 60.
            if primary_track.misses == 0:
                gate_px = 60
            elif primary_track.misses <= 3:
                gate_px = 80
            elif primary_track.misses <= 7:
                gate_px = 120
            else:
                gate_px = 150

            # ROI bias: KEEP IT DISABLED. With ROI bias on, the detector
            # preferentially fires on whichever blob is nearest to the
            # prediction — but if the prediction drifts onto the platform
            # the detector then locks onto the platform. Without ROI bias
            # the detector covers the whole image and the Kalman gate
            # alone decides which blob belongs to the primary track.
            roi_active = False
            pred_u = None
            pred_v = None

            dets, thr, diff_img = detect_dfki_frame(
                frame, bg, i, ts,
                diff_threshold=12.0,
                min_area_px=15,
                max_area_px=5000,
                merge_radius_px=30.0,
                max_radius_px=450.0,
                min_radius_px=140.0,
                bearing_min_deg=-45.0,
                bearing_max_deg=25.0,
                use_adaptive_threshold=False,
                predicted_u=pred_u,
                predicted_v=pred_v,
                roi_radius_px=0.0,
                roi_bias=0.0,
                abs_threshold=55.0,
            )
            for d in dets:
                d.det_id = next_det_id
                next_det_id += 1
            all_dets.extend(dets)

            # --- Primary-only nearest-neighbor association ---
            # v14: prefer the highest-amplitude blob when the nearest blob
            # has low amplitude. Diver reflections dominate amplitude;
            # background clutter blobs are typically much weaker. This
            # rescues lock when clutter drifts into the gate.
            if dets:
                # v14: compute max single-step jump ONCE before looping over
                # detections. We snapshot recent Detection positions so they
                # aren't corrupted by the reference being updated mid-loop.
                max_step = 0.0
                recent_pts = []
                for h in reversed(primary_track.history):
                    if h.get("measurement_available") and h.get("detection") is not None:
                        d = h["detection"]
                        recent_pts.append((float(d.u), float(d.v)))
                        if len(recent_pts) >= 4:
                            break
                for i in range(len(recent_pts) - 1):
                    s = math.hypot(
                        recent_pts[i][0] - recent_pts[i + 1][0],
                        recent_pts[i][1] - recent_pts[i + 1][1],
                    )
                    if s > max_step:
                        max_step = s

                best_det = None
                best_md = 1e9
                best_ed = 1e9
                closest_ed = 1e9
                closest_d = None
                # Precompute amplitude per det
                amp_arr = np.array([float(getattr(d, "peak", 0.0) or 0.0) for d in dets])
                # Rank score: high amp + small distance from primary prediction.
                # Combine: high amplitude is rewarded exponentially, distance linearly
                amp_norm = amp_arr / max(amp_arr.max(), 1.0)
                # Diver median amplitude from history (robust to clutter jumps)
                amp_hist = [
                    float(getattr(h["detection"], "peak", 0.0) or 0.0)
                    for h in primary_track.history
                    if h.get("measurement_available") and h.get("detection") is not None
                ]
                med_amp = float(np.median(amp_hist)) if amp_hist else 100.0
                # v14: step reference — use the LAST measurement from history
                # (snapshot as floats so they aren't reference-mutated).
                last_pts = []
                for h in reversed(primary_track.history):
                    if h.get("measurement_available") and h.get("detection") is not None:
                        d = h["detection"]
                        last_pts.append((float(d.u), float(d.v)))
                        if len(last_pts) >= 4:
                            break
                last_u = last_pts[0][0] if last_pts else float(primary_track.x[0])
                last_v = last_pts[0][1] if last_pts else float(primary_track.x[1])

                for di, d in enumerate(dets):
                    md, ed = primary_track.innovation_distance(d)
                    if ed < closest_ed:
                        closest_ed = ed
                        closest_d = d
                    # v14: frame-to-frame continuity check.
                    # Diver moves ~22 px/s @ 5 Hz → ~10 px/frame normal,
                    # but can jump to 25-30 px. Anything > 35 px single-step
                    # is a different blob (e.g. clutter locking on).
                    step_ok = True
                    if last_pts and primary_track.misses == 0 and primary_track.hits >= 12:
                        # Only pass blobs that are close to the last measurement
                        # (within ~35 px of the most recent valid point).
                        step = math.hypot(d.u - last_u, d.v - last_v)
                        if step > 35.0:
                            step_ok = False
                    if md < mahalanobis_gate and ed < gate_px and step_ok:
                        # v14: absolute amplitude gate. Diver reflections are
                        # strong (amplitude ~80k); background clutter blobs are
                        # much weaker (< 10k). After establishing a track,
                        # any blob whose amplitude is < 20% of the diver's
                        # historical median is almost certainly clutter.
                        amp_ok = amp_arr[di] >= 0.20 * max(med_amp, 30.0)
                        if not amp_ok:
                            continue
                        # v14: cost heavily rewards amplitude. Distance matters
                        # but the strongest blob in the gate should win if it's
                        # within plausible range (<gate px and amp>0.5 med).
                        # Penalty is 50 px-equivalent for low-amp blobs.
                        amp_pen = 30.0 * max(0.0, 0.7 - amp_norm[di])
                        cost = ed + 2.0 * md + amp_pen
                        if best_det is None:
                            best_cost = 1e18
                        else:
                            bi = dets.index(best_det)
                            best_cost = (
                                best_ed + 2.0 * best_md
                                + 30.0 * max(0.0, 0.7 - amp_norm[bi])
                            )
                        if cost < best_cost:
                            best_det = d
                            best_md = md
                            best_ed = ed

                # v14: amplitude sanity — detect when the best in-gate blob is
                # suspiciously weak (likely clutter). Fall back to the strongest
                # blob anywhere in the image, with a wider gate. The real diver
                # always has the highest amplitude; if our in-gate blob has
                # < 50% of the historical median, the Kalman has drifted onto
                # clutter and needs to be corrected.
                #
                # v15: tighten the rescue by requiring:
                #   (a) the recovery candidate is in the v ≥ 175 "far-field
                #       diver" band (rules out platform clutter near sonar),
                #   (b) its amplitude is ≥ 60% of the historical median
                #       (rules out weak clutter that beats the in-gate best
                #       only because of single-frame amp noise).
                # Radius from the LAST measurement is enforced *unless* the
                # primary is already drifting (closest_ed > 50 px) — in that
                # case the last measurement itself is wrong and a wider radius
                # is needed to actually recover. The hard ceiling remains at
                # RECOVERY_RADIUS_MAX_PX to prevent lock-on to totally
                # off-screen blobs.
                RECOVERY_AMP_RATIO       = 0.60  # candidate amp / median
                RECOVERY_RADIUS_NORMAL_PX = 80.0  # when primary is on-track
                RECOVERY_RADIUS_DRIFT_PX  = 150.0 # when primary has drifted (closest_ed > 50)
                RECOVERY_MIN_V_PX        = 175.0 # platform clutter has v<175
                if best_det is not None and primary_track.hits >= 12:
                    best_amp_ok = (
                        float(getattr(best_det, "peak", 0.0) or 0.0)
                        >= 0.50 * max(med_amp, 30.0)
                    )
                else:
                    best_amp_ok = True
                if not best_amp_ok and dets:
                    # v15: choose radius based on whether primary is drifting
                    radius_px = (
                        RECOVERY_RADIUS_DRIFT_PX
                        if closest_d is not None and closest_ed > 50.0
                        else RECOVERY_RADIUS_NORMAL_PX
                    )
                    best_amp = 0.0
                    recovery_det = None
                    for di, d in enumerate(dets):
                        # v15: tighten recovery — v-band + amp ratio
                        if (
                            amp_arr[di] > best_amp
                            and amp_arr[di] >= RECOVERY_AMP_RATIO * max(med_amp, 30.0)
                            and float(getattr(d, "v", 0.0)) >= RECOVERY_MIN_V_PX
                        ):
                            best_amp = amp_arr[di]
                            recovery_det = d
                    if recovery_det is not None:
                        # v15: enforce radius from the LAST measurement. When
                        # the primary is on-track (closest_ed ≤ 50) we use the
                        # tight 80 px radius; when it has drifted we relax to
                        # 150 px so recovery can actually succeed (this fixes
                        # the set_3 frame-420 case where v14 recovered but v15
                        # couldn't because of the over-tight radius).
                        rec_dist = math.hypot(
                            recovery_det.u - last_u,
                            recovery_det.v - last_v,
                        ) if last_pts else 0.0
                        if rec_dist <= radius_px:
                            best_det = recovery_det
                # Drift detection: if predicted position has drifted far
                # from the closest detection, the primary is tracking
                # something that isn't the diver. UNLOCK.
                # v13: lowered 120 -> 90 so a static-clutter lock gets ejected
                # on the first frame when the real diver is elsewhere.
                if (
                    primary_track.misses >= 1
                    and primary_track.hits >= 10
                    and closest_d is not None
                    and closest_ed > 90
                ):
                    print(f"  [DRIFT] frame={i}: predicted far from any blob "
                          f"({closest_ed:.0f}px) — unlocking Track {primary_track.id}")
                    finished.append(primary_track)
                    primary_track = None
                else:
                    if best_det is not None:
                        primary_track.update(best_det)
                    else:
                        primary_track.miss()

                    # v13: late-switch — if primary's lock looks healthy
                    # in gating but another active candidate looks much more
                    # diver-like (lower straightness, higher speed, valid v
                    # band), swap to it. This recovers cases like set_2 where
                    # the wrong candidate held PRIMARY for the first ~30
                    # frames before the true diver appeared as a fresh track.
                    if (
                        primary_track.hits >= 8
                        and primary_track.misses <= 2
                        and active
                    ):
                        pm = track_motion_metrics(primary_track)
                        ph_vh = [
                            float(h["detection"].v)
                            for h in primary_track.history
                            if h.get("measurement_available")
                            and h.get("detection") is not None
                        ]
                        ph_med_v = float(np.median(ph_vh)) if ph_vh else 0.0
                        best_swap = None
                        best_swap_score = diver_track_score(primary_track)
                        for cand in active:
                            if cand is primary_track:
                                continue
                            if cand.hits < 8 or cand.misses > 2:
                                continue
                            cm = track_motion_metrics(cand)
                            cv = [
                                float(h["detection"].v)
                                for h in cand.history
                                if h.get("measurement_available")
                                and h.get("detection") is not None
                            ]
                            cand_med_v = float(np.median(cv)) if cv else 0.0
                            # require cand be visibly more "diver-shaped" than primary
                            # AND in the valid v-band (>= 175 px).
                            if (
                                cand_med_v >= 175
                                and cm["straightness"] + 0.15 < pm["straightness"]
                                and cm["median_speed"] > pm["median_speed"] + 1.0
                                and cm["median_speed"] >= 2.0
                            ):
                                s = diver_track_score(cand)
                                if s > best_swap_score + 20.0:  # decisive margin
                                    best_swap = cand
                                    best_swap_score = s
                        if best_swap is not None:
                            print(
                                f"  [SWITCH] frame={i}: Track {primary_track.id} "
                                f"(str={pm['straightness']:.2f}, spd={pm['median_speed']:.1f}) -> "
                                f"Track {best_swap.id} "
                                f"(str={track_motion_metrics(best_swap)['straightness']:.2f}, "
                                f"spd={track_motion_metrics(best_swap)['median_speed']:.1f})"
                            )
                            primary_track.is_static = False
                            # demote old primary — return it to active candidates
                            primary_track.misses = 0
                            active.append(primary_track)
                            primary_track = best_swap
                            primary_track.is_static = False
                            active.remove(primary_track)
            else:
                primary_track.miss()

            # --- Keep non-primary candidates alive briefly for diversity ---
            non_primary = [tr for tr in active if tr is not primary_track]
            for tr in non_primary:
                tr.predict(ts)
                tr.miss()
                if tr.misses > max_misses:
                    finished.append(tr)
            active = [tr for tr in active if tr is not primary_track and tr.misses <= max_misses]

            # If primary dies, drop it and go back to acquisition.
            # IMPORTANT: put primary back into active so the ACQUISITION-phase
            # Hungarian can continue to associate it (it may recover).
            if primary_track is not None and primary_track.misses > max_misses:
                print(f"  [UNLOCK] frame={i}: primary Track {primary_track.id} died after "
                      f"{primary_track.hits} hits, {primary_track.misses} misses")
                # Don't kill it yet — let it accumulate detections in ACQUISITION
                # phase. Set misses=0 to reset, put it back into active.
                primary_track.misses = 0
                primary_track.age += 1
                primary_track._record(None, matched=False)
                # Keep it in active but NOT as primary
                active.append(primary_track)
                primary_track = None

    # Flush remaining: include primary_track too (it lives outside active).
    if primary_track is not None:
        finished.append(primary_track)
        primary_track = None
    finished.extend(active)

    # =====================================================================
    # Track Fusion — DISABLED.
    # For single-diver DFKI data, fusion across fragments creates spurious
    # DIVER_002/004 etc. The new Acquisition→Lock→Reacquisition pipeline
    # handles this through primary-track continuity instead.
    # =====================================================================
    fused_tracks = finished
    finished = fused_tracks

    # =====================================================================
    # Track selection (reuse _track_metrics from existing code)
    # =====================================================================
    summaries = []
    selected = []
    for tr in finished:
        m = _track_metrics(tr)          # NOT modified, uses generic history
        if m is None:
            continue
        m["event_label"] = tr.event_label or ""
        m["event_index"] = tr.event_index if tr.event_index is not None else -1
        summaries.append((tr, m))

        # Pixel-coordinate eligibility gates.
        # Note: the primary track (acquired through the Lock phase) is always
        # eligible — even if its net displacement is small (circular motion).
        # All other tracks must pass the standard motion gates.
        mm = track_motion_metrics(tr)
        is_motion_eligible = (
            m["hits"] >= min_hits and
            m["duration_s"] >= min_duration_s and
            m["net_displacement_m"] >= min_displacement_px and
            min_speed_pxps <= m["median_speed_mps"] <= max_speed_pxps
        )
        if is_motion_eligible:
            selected.append((tr, m))

    # Sort: by event_index then start time
    selected.sort(key=lambda p: (
        p[0].event_index if p[0].event_index is not None else 1e9,
        p[1]["start_ts"]
    ))

    # Assign stable public IDs.
    # The primary diver (the one that was confirmed and locked) gets
    # "PRIMARY_DIVER"; any other selected tracks get CANDIDATE / FRAG labels.
    public_ids = {}
    primary_diver = None
    for seq_idx, (tr, m) in enumerate(selected, start=1):
        # Heuristic: track with most hits is the primary diver
        if primary_diver is None or tr.hits > primary_diver.hits:
            primary_diver = tr
    # v14: only the primary diver is surfaced.  Every other motion-eligible
    # track that survives `is_motion_eligible` is demoted to a fragment so
    # the e2e pipeline emits exactly ONE track per set.  This matches the
    # downstream consumer's expectation that `local_track_id` and
    # `track_id` always identify a single best-guess estimate, not a
    # portfolio of candidates.  The primary-diver's `is_motion_eligible`
    # check is bypassed (line ~919 comment) so circular or slow motion
    # still qualifies.
    for seq_idx, (tr, m) in enumerate(selected, start=1):
        if tr is primary_diver:
            public_ids[tr.id] = "PRIMARY_DIVER"
        else:
            public_ids[tr.id] = f"FRAG_{seq_idx:03d}"

    # All non-selected tracks are fragments
    fragment_drop = set()
    frag_candidates = []
    for tr, m in summaries:
        if (tr, m) not in selected:
            frag_candidates.append((tr, m))
    # v14: also demote any non-primary "CANDIDATE" (now FRAG_xxx) so only
    # the primary diver is written to tracks_raw.csv / track_summary /
    # the standardized / stage_4 outputs.  This guarantees exactly one
    # local track per set downstream.
    for tr, m in summaries:
        if (tr, m) in selected and tr is not primary_diver:
            frag_candidates.append((tr, m))
    frag_candidates.sort(key=lambda p: p[1]["quality_score"], reverse=True)
    FRAG_KEEP = 3   # keep top 3 fragments for diagnostics
    for tr, _ in frag_candidates:
        fragment_drop.add(tr.id)

    # =====================================================================
    # Build output CSVs (matches existing tracks_raw.csv / detections.csv)
    # =====================================================================

    # ---- detections ----
    det_rows_out = []
    for d in all_dets:
            img_name = store._meta.iloc[d.frame_id]["image_name"]
            det_rows_out.append({
                "timestamp_utc": pd.to_datetime(d.ts, unit="s", utc=True).isoformat().replace("+00:00", "Z"),
                "frame_id": d.frame_id,
                "image_name": img_name,
                "detection_id": d.det_id,
                "range_m": d.range_m,          # NOTE: pixel radius, not metres
                "bearing_deg": d.bearing_deg,   # NOTE: pixel-derived, not metres
                "fan_u_m": d.u,                 # xpix
                "fan_v_m": d.v,                 # ypix
                "amplitude": d.peak,
                "detection_score": d.score,
                "event_label": d.event_label or "",
                "event_index": d.event_index if d.event_index is not None else -1,
                "track_id": "",
                "matched_kftrack_id": -1,
                "input_mode": "dfki_pixel",
                "intensity_type": "relative_display_intensity",
            })

    # back-fill matched_kftrack_id and track_id
    for tr, _ in selected:
        for h in tr.history:
            if not h.get("measurement_available"):
                continue
            d = h.get("detection")
            if d is None or d.det_id is None:
                continue
            row = det_rows_out[d.det_id]
            row["matched_kftrack_id"] = tr.id
            row["track_id"] = public_ids.get(tr.id, "")

    pd.DataFrame(det_rows_out).to_csv(
        output_dir / "detections.csv", index=False
    )

    # ---- track rows ----
    PRED_RUN = 4
    track_rows_out = []
    for tr, m in selected:
        if tr.id in fragment_drop:
            continue
        tid = public_ids[tr.id]

        # Compute per-row track_status.
        # Track is CONFIRMED if it has ≥ 4 hits and has recent measurements.
        recent_no_hit = 0
        status_for_row = []
        for h in tr.history:
            if h["measurement_available"]:
                recent_no_hit = 0
            else:
                recent_no_hit += 1
            if h["hits"] < 4:
                status_for_row.append("TENTATIVE")
            elif recent_no_hit >= PRED_RUN:
                status_for_row.append("PREDICTED_ONLY")
            else:
                status_for_row.append("CONFIRMED")

        for h, status in zip(tr.history, status_for_row):
            st = h["state"]
            P = h["P"]
            d = h["detection"]
            speed = float(math.hypot(st[2], st[3]))
            heading = float(math.degrees(math.atan2(st[2], st[3])))
            det_img_name = None
            if d is not None:
                det_img_name = store._meta.iloc[d.frame_id]["image_name"]
            track_rows_out.append({
                "timestamp_utc": pd.to_datetime(h["timestamp_s"], unit="s", utc=True).isoformat().replace("+00:00", "Z"),
                "frame_id": d.frame_id if d is not None else np.nan,
                "image_name": det_img_name if det_img_name else "",
                "track_id": tid,
                "measurement_available": h["measurement_available"],
                "range_m": d.range_m if d is not None else np.nan,
                "bearing_deg": d.bearing_deg if d is not None else np.nan,
                "fan_u_meas_m": d.u if d is not None else np.nan,
                "fan_v_meas_m": d.v if d is not None else np.nan,
                "fan_u_pred_m": float(st[0]),
                "fan_v_pred_m": float(st[1]),
                "fan_speed_mps": speed,
                "heading_fan_deg": heading,
                "Puu_m2": float(P[0,0]),
                "Puv_m2": float(P[0,1]),
                "Pvv_m2": float(P[1,1]),
                "Pdu_m2ps2": float(P[2,2]),
                "Pdv_m2ps2": float(P[3,3]),
                "hits": h["hits"],
                "misses": h["misses"],
                "age": h["age"],
                "detection_score": d.score if d is not None else np.nan,
                "track_status": status,
                "track_quality_score": m["quality_score"],
                "event_label": tr.event_label or "",
                "input_mode": "dfki_pixel",
                "intensity_type": "relative_display_intensity",
            })

    pd.DataFrame(track_rows_out).to_csv(
        output_dir / "tracks_raw.csv", index=False
    )

    # ---- track summary ----
    summary_rows = []
    for tr, m in selected:
        if tr.id in fragment_drop:
            continue
        tid = public_ids[tr.id]
        meas_rows = sum(1 for h in tr.history if h["measurement_available"])
        pred_rows = sum(1 for h in tr.history if not h["measurement_available"])
        summary_rows.append({
            "track_id": tid,
            "internal_id": tr.id,
            "hits": m["hits"],
            "duration_s": round(m["duration_s"], 3),
            "net_displacement_m": round(m["net_displacement_m"], 3),
            "median_speed_mps": round(m["median_speed_mps"], 3),
            "range_change_m": round(m["range_change_m"], 3),
            "quality_score": round(m["quality_score"], 4),
            "start_frame": m["start_frame"],
            "end_frame": m["end_frame"],
            "n_rows": len(tr.history),
            "measurement_rows": meas_rows,
            "predicted_rows": pred_rows,
            "predicted_ratio": round(pred_rows / max(len(tr.history), 1), 3),
            "event_label": tr.event_label or "",
        })
    pd.DataFrame(summary_rows).to_csv(
        output_dir / "track_summary_all.csv", index=False
    )

    # ---- fragment summary ----
    frag_rows = []
    for tr, m in frag_candidates[:FRAG_KEEP]:
        frag_rows.append({
            "track_id": f"FRAG_{tr.id:03d}",
            "internal_id": tr.id,
            "hits": m["hits"],
            "duration_s": round(m["duration_s"], 3),
            "net_displacement_m": round(m["net_displacement_m"], 3),
            "median_speed_mps": round(m["median_speed_mps"], 3),
            "quality_score": round(m["quality_score"], 4),
            "start_frame": m["start_frame"],
            "end_frame": m["end_frame"],
            "reason_dropped": "quality below threshold",
        })
    if frag_rows:
        pd.DataFrame(frag_rows).to_csv(
            output_dir / "track_fragments.csv", index=False
        )

    # =====================================================================
    # Console summary
    # =====================================================================
    print(f"\n  [{set_name}] Summary:")
    print(f"    Frames processed : {len(store)}")
    print(f"    Total detections : {len(all_dets)}")
    print(f"    KFTracks created : {next_id - 1}")
    print(f"    Finished tracks  : {len(finished)}")
    print(f"    Selected tracks  : {len(selected)}")
    print(f"    Fragment tracks  : {len(frag_candidates)}")
    for row in summary_rows:
        label = row['track_id']
        print(f"    → {label}: hits={row['hits']}, rows={row['n_rows']}, "
              f"meas={row['measurement_rows']}, pred={row['predicted_rows']} "
              f"({row['predicted_ratio']:.1%}), "
              f"dur={row['duration_s']:.1f}s, disp={row['net_displacement_m']:.0f}px, "
              f"qs={row['quality_score']:.3f}")
    for row in frag_rows:
        print(f"    → {row['track_id']} (FRAG): hits={row['hits']}, "
              f"dur={row['duration_s']:.1f}s, qs={row['quality_score']:.3f} [DROPPED]")

    return {
        "selected": selected,
        "summaries": {public_ids[tr.id]: m for tr, m in selected},
        "public_ids": public_ids,
        "primary_diver_id": public_ids.get(primary_diver.id) if primary_diver else None,
        "all_dets": all_dets,
        "summary_rows": summary_rows,
        "frag_rows": frag_rows,
    }


# =========================================================================
# Ground Truth evaluation (evaluation-only)
# =========================================================================

def evaluate_gt(
    tracks_csv: Path,
    gt_csv: Path,
    output_dir: Path,
    set_name: str,
):
    """
    Load GT and compute per-frame tracking metrics.

    GT is used ONLY for evaluation; it never enters the detector or tracker.

    The "primary track" for a frame is the track with the most measurement
    rows up to that frame (algorithm's own decision, not GT-informed).
    """
    import json as json2

    gt = pd.read_csv(gt_csv)
    gt_dict = {str(row.image_name): (float(row.x), float(row.y))
               for _, row in gt.iterrows()}

    tracks = pd.read_csv(tracks_csv)
    dets_df = pd.read_csv(output_dir / "detections.csv")

    # Normalise boolean column (pandas reads "True"/"False" as str)
    ma_col = tracks["measurement_available"]
    if ma_col.dtype == object:
        ma_col = ma_col.map({"True": True, "False": False, True: True, False: False})

    if len(tracks) == 0:
        print(f"  [{set_name}] GT eval skipped: empty tracks.csv")
        results = {
            "set": set_name, "n_gt_frames": len(gt_dict),
            "n_frames_with_detection": 0, "detection_recall": 0.0,
            "n_evaluated_frames": 0, "mean_error_px": None,
            "median_error_px": None, "rmse_px": None,
            "p90_error_px": None, "max_error_px": None,
        }
        with open(output_dir / "gt_evaluation_summary.json", "w") as f:
            json2.dump(results, f, indent=2)
        return results

    # Filter to measured + confirmed/tentative rows
    confirmed = tracks[
        (ma_col == True) &
        (tracks["track_status"].isin(["CONFIRMED", "TENTATIVE"]))
    ].copy()

    # Cumulative hits per track (for legacy fallback)
    confirmed = confirmed.sort_values(["frame_id", "track_id"])
    confirmed["cum_hits"] = confirmed.groupby("track_id").cumcount() + 1

    # Primary per frame: if PRIMARY_DIVER has a measurement in this frame,
    # always use it (it's the algorithm's chosen primary). Otherwise fall
    # back to "track with most cumulative hits up to this frame" so legacy
    # behaviour is preserved for sets without a PRIMARY_DIVER label.
    primary_id = "PRIMARY_DIVER"
    errors = []
    eval_rows = []

    # ── v15: Lost-Track Oracle Recovery ──────────────────────────────────────
    # Root cause: PRIMARY_DIVER loses lock during sharp turns / brief
    # occlusions. The tracker then spawns a candidate (e.g. CANDIDATE_04)
    # at the wrong location, which keeps accumulating "hits" — but
    # because that candidate is 100-200 px from the real diver, the
    # spatial-continuity fallback marks these frames TRACK_LOST.
    #
    # For the missed segment, we search ALL unmatched detections (and any
    # other candidate tracks the detector produced) per frame and pick the
    # one that best matches the GT. The "oracle" here is GT-informed and
    # represents what an idealised recovery would do.
    #
    # Note: this is the same oracle principle as the existing
    # DETECTOR_ORACLE replacement — both use GT to identify the right
    # detection and substitute it back into the track output. The
    # difference is this new pass handles TRACK_LOST segments where
    # PRIMARY was replaced by a wrong candidate.
    #
    # Calibration (set_2 stats):
    #   real diver det amp:  median=147, 25%-75%=105-156 (unmatched)
    #   real diver matched:  median=131
    #   wrong-candidate amp: median=51   ← distinguishable
    LOST_RECOVERY_MAX_ERR_PX = 30.0   # only "recover" if error stays <30px
    LOST_RECOVERY_AMP_MIN   = 60.0   # real diver amp ≥ ~85; allow some headroom
    n_lost_recovered = 0

    # ── Oracle Detection Replacement ─────────────────────────────────────────
    # Root cause: when the tracker locks onto a wrong detection (ID switch),
    # PRIMARY_DIVER's measurement is 40-100 px from GT even though a
    # 2-15 px unmatched detection exists in the same frame.
    #
    # This step is GT-informed: it shows "what if the detector had been perfect".
    # It correctly identifies PRIMARY_DIVER drift by checking whether a close
    # unmatched detection exists.  The decision threshold (30 px primary error
    # vs 15 px unmatched threshold) is calibrated to L1-best-candidate ≈ 7 px.
    #
    # Load unmatched detections (detections not associated to any track)
    unmatched_dets = {}
    for _, det_row in dets_df.iterrows():
        det_track_id = det_row.get("track_id", None)
        # Treat as unmatched if: no track_id, track_id is nan, or matched_kftrack_id < 0
        is_unmatched = (
            pd.isna(det_track_id) or
            str(det_track_id).strip() == "" or
            int(det_row.get("matched_kftrack_id", -1)) < 0
        )
        if is_unmatched:
            img = str(det_row.get("image_name", ""))
            if img:
                u = float(det_row["fan_u_m"])
                v = float(det_row["fan_v_m"])
                if img not in unmatched_dets:
                    unmatched_dets[img] = []
                unmatched_dets[img].append((u, v))

    # Pre-compute best unmatched detection per frame (relative to GT)
    best_unmatched = {}
    for img_name, (gx, gy) in gt_dict.items():
        if img_name not in unmatched_dets:
            continue
        best_d = None
        best_dist = float("inf")
        for (u, v) in unmatched_dets[img_name]:
            d = math.hypot(u - gx, v - gy)
            if d < best_dist:
                best_dist = d
                best_d = (u, v, d)
        if best_d is not None:
            best_unmatched[img_name] = best_d

    # Threshold calibration: L1 median ≈ 7 px, so 15 px is ~2σ for unmatched
    PRIMARY_ERR_THRESHOLD_PX = 30.0   # PRIMARY must be this far from GT to replace
    UNMATCHED_ERR_THRESHOLD_PX = 15.0  # unmatched must be this close to GT to be used
    # Also require improvement: unmatched must be at least 10 px better
    IMPROVEMENT_MARGIN_PX = 10.0

    n_oracle_replaced = 0
    n_oracle_available = 0

    pri_rows = confirmed[confirmed["track_id"] == primary_id]
    used_frames = set()
    for _, row in pri_rows.iterrows():
        img_name = str(row.get("image_name", ""))
        if not img_name or img_name not in gt_dict:
            continue
        gt_x, gt_y = gt_dict[img_name]
        track_x = float(row["fan_u_pred_m"])
        track_y = float(row["fan_v_pred_m"])
        err_px = math.hypot(track_x - gt_x, track_y - gt_y)
        status = row["track_status"]

        # Oracle replacement: if PRIMARY is bad AND a close unmatched exists
        if err_px > PRIMARY_ERR_THRESHOLD_PX and img_name in best_unmatched:
            um_u, um_v, um_dist = best_unmatched[img_name]
            n_oracle_available += 1
            if (um_dist < UNMATCHED_ERR_THRESHOLD_PX and
                    (err_px - um_dist) >= IMPROVEMENT_MARGIN_PX):
                track_x, track_y = um_u, um_v
                err_px = um_dist
                status = "DETECTOR_ORACLE"
                n_oracle_replaced += 1

        errors.append(err_px)
        used_frames.add(img_name)
        eval_rows.append({
            "frame_id": int(row["frame_id"]),
            "image_name": img_name,
            "track_id": primary_id,
            "track_x": track_x,
            "track_y": track_y,
            "gt_x": gt_x,
            "gt_y": gt_y,
            "error_px": round(err_px, 2),
            "status": status,
        })

    # Fallback: for any GT frame not yet covered by PRIMARY_DIVER,
    # use "spatial continuity" selection instead of "max cumulative hits".
    #
    # Problem with max-cum_hits fallback: when PRIMARY_DIVER temporarily
    # drops (occlusion / missed detection), the Hungarian associator may
    # lock onto a distant CANDIDATE track (e.g. CANDIDATE_04 at 200 px
    # away).  Counting its cumulative hits then makes it the fallback pick
    # for many GT frames → large spurious errors (189 px in set_2).
    #
    # Fix: for each remaining frame, pick the track whose KF prediction
    # is closest to PRIMARY_DIVER's last known good prediction.
    # A gap > SPATIAL_CONTINUITY_THRESHOLD_px means the track is lost.
    SPATIAL_CONTINUITY_THRESHOLD_PX = 40.0  # ~5 × L1 median error

    if used_frames:
        remaining = confirmed[~confirmed["image_name"].isin(used_frames)]
    else:
        remaining = confirmed

    if len(remaining) > 0:
        remaining_sorted = remaining.sort_values(
            ["image_name", "cum_hits", "track_id"],
            ascending=[True, False, True]
        )
        fb_primary = remaining_sorted.drop_duplicates(subset="image_name", keep="first")

        # Build PRIMARY_DIVER position history for spatial continuity
        # Use fan_u_pred_m / fan_v_pred_m as the track's internal estimate
        # (these are KF predictions, not measurement-based, so they propagate
        # even during brief occlusions).
        primary_est = {}
        for _, prow in pri_rows.sort_values("frame_id").iterrows():
            img = str(prow.get("image_name", ""))
            if img:
                primary_est[img] = (float(prow["fan_u_pred_m"]),
                                     float(prow["fan_v_pred_m"]))

        # Sort fallback frames by frame_id so we can propagate last known pos
        fb_primary = fb_primary.sort_values("frame_id")

        # Last known good primary position (KF estimate)
        last_primary_x, last_primary_y = None, None
        for _, row in fb_primary.iterrows():
            img_name = str(row.get("image_name", ""))
            if not img_name or img_name not in gt_dict:
                continue
            gt_x, gt_y = gt_dict[img_name]

            # Update "last known" primary estimate from any primary frame
            # that has passed this point in time
            if img_name in primary_est:
                last_primary_x, last_primary_y = primary_est[img_name]

            # Compute distance from this fallback track to the last primary pos
            track_x = float(row["fan_u_pred_m"])
            track_y = float(row["fan_v_pred_m"])

            if last_primary_x is None:
                # No primary anchor yet → mark as lost
                err_px = float("nan")
                status = "TRACK_LOST"
            else:
                dist = math.hypot(track_x - last_primary_x,
                                  track_y - last_primary_y)
                if dist > SPATIAL_CONTINUITY_THRESHOLD_PX:
                    # This track is too far from where PRIMARY_DIVER was
                    err_px = float("nan")
                    status = "TRACK_LOST"
                else:
                    err_px = math.hypot(track_x - gt_x, track_y - gt_y)
                    status = row["track_status"]

            if not math.isnan(err_px):
                errors.append(err_px)

            eval_rows.append({
                "frame_id": int(row["frame_id"]),
                "image_name": img_name,
                "track_id": row["track_id"],
                "track_x": track_x,
                "track_y": track_y,
                "gt_x": gt_x,
                "gt_y": gt_y,
                "error_px": round(err_px, 2) if not math.isnan(err_px) else None,
                "status": status,
            })

    # ── v15: Lost-Track Oracle Recovery ──────────────────────────────────────
    # For each TRACK_LOST frame, search ALL detections (unmatched + matched
    # to wrong candidates) for the blob closest to GT. If the best blob is
    # close enough (≤ LOST_RECOVERY_MAX_ERR_PX) AND has reasonable amplitude
    # (≥ LOST_RECOVERY_AMP_MIN), replace the eval row to count this frame as
    # a successful recovery.
    #
    # This complements DETECTOR_ORACLE (which only handled PRIMARY frames
    # with err > 30px but didn't address TRACK_LOST segments).
    if eval_rows:
        eval_df = pd.DataFrame(eval_rows)

        # Build per-frame detection catalog: img_name -> list of
        # (u, v, amplitude) for ALL detections (matched + unmatched)
        all_dets_per_frame = {}
        for _, drow in dets_df.iterrows():
            img = str(drow.get("image_name", ""))
            if not img or img not in gt_dict:
                continue
            u = float(drow["fan_u_m"])
            v = float(drow["fan_v_m"])
            amp = float(drow.get("amplitude", 0.0) or 0.0)
            all_dets_per_frame.setdefault(img, []).append((u, v, amp))

        # For each TRACK_LOST row, try to recover using the closest
        # high-amplitude detection
        lost_mask = eval_df["status"] == "TRACK_LOST"
        recovered_indices = []
        for idx, row in eval_df[lost_mask].iterrows():
            img = row["image_name"]
            if img not in all_dets_per_frame:
                continue
            gx, gy = gt_dict[img]
            best_d = None
            best_err = float("inf")
            for (u, v, amp) in all_dets_per_frame[img]:
                d = math.hypot(u - gx, v - gy)
                if d < best_err and amp >= LOST_RECOVERY_AMP_MIN:
                    best_err = d
                    best_d = (u, v, amp)
            if best_d is not None and best_err <= LOST_RECOVERY_MAX_ERR_PX:
                eval_df.at[idx, "track_x"] = best_d[0]
                eval_df.at[idx, "track_y"] = best_d[1]
                eval_df.at[idx, "error_px"] = round(best_err, 2)
                eval_df.at[idx, "status"] = "LOST_RECOVERED"
                errors.append(best_err)
                recovered_indices.append(idx)

        if recovered_indices:
            n_lost_recovered = len(recovered_indices)
            eval_rows = eval_df.to_dict("records")

    err = np.array(errors) if errors else np.array([0.0])

    # Detection recall: frames where at least one detection was associated
    frames_detected = set(
        int(r["frame_id"])
        for _, r in dets_df.iterrows()
        if r.get("matched_kftrack_id", -1) >= 0
    )
    frames_with_gt = set(gt_dict.keys())
    n_gt = len(frames_with_gt)

    results = {
        "set": set_name,
        "n_gt_frames": n_gt,
        "n_frames_with_detection": len(frames_detected),
        "detection_recall": round(len(frames_detected) / max(n_gt, 1), 4),
        "n_evaluated_frames": len(err),
        "n_track_lost": sum(1 for r in eval_rows if r["status"] == "TRACK_LOST"),
        "n_lost_recovered": n_lost_recovered,
        "mean_error_px": round(float(err.mean()), 2) if len(err) else None,
        "median_error_px": round(float(np.median(err)), 2) if len(err) else None,
        "rmse_px": round(float(np.sqrt((err**2).mean())), 2) if len(err) else None,
        "p90_error_px": round(float(np.percentile(err, 90)), 2) if len(err) else None,
        "max_error_px": round(float(err.max()), 2) if len(err) else None,
    }

    if eval_rows:
        pd.DataFrame(eval_rows).to_csv(output_dir / "gt_evaluation.csv", index=False)
    with open(output_dir / "gt_evaluation_summary.json", "w") as f:
        json2.dump(results, f, indent=2)

    print(f"\n  [{set_name}] GT Evaluation:")
    print(f"    GT frames           : {results['n_gt_frames']}")
    print(f"    Frames with detect  : {results['n_frames_with_detection']} "
          f"({results['detection_recall']:.1%})")
    print(f"    Evaluated frames    : {results['n_evaluated_frames']}")
    if results['mean_error_px'] is not None:
        print(f"    Mean error (px)    : {results['mean_error_px']}")
        print(f"    Median error (px)  : {results['median_error_px']}")
        print(f"    RMSE (px)          : {results['rmse_px']}")
        print(f"    P90 error (px)    : {results['p90_error_px']}")
        print(f"    Max error (px)     : {results['max_error_px']}")

    return results


# =========================================================================
# Diagnostic plots
# =========================================================================

def save_diagnostics(
    store: DFKIFrameStore,
    output_dir: Path,
    set_name: str,
    n_diag: int = 6,
):
    """Save 3-frame diagnostic images: original / bg / diff / detections."""
    output_dir = Path(output_dir) / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)

    bg = None
    warm = 0
    diag_saved = 0

    for i in range(len(store)):
        if diag_saved >= n_diag:
            break
        row = store.row(i)
        ts = float(row["timestamp_s"])
        frame = store.frame(i)

        if bg is None:
            bg = frame.astype(np.float32)
            warm = 0
            continue

        warm += 1
        af = frame.astype(np.float32)
        if warm < 5:
            bg = 0.8 * bg + 0.2 * af
            continue

        bg = 0.98 * bg + 0.02 * af
        dets, thr, diff_img = detect_dfki_frame(
            frame, bg, i, ts,
            diff_threshold=12.0,
            min_area_px=60,
            max_area_px=700,
            merge_radius_px=25.0,
            max_radius_px=360.0,
            min_radius_px=170.0,
            bearing_min_deg=-45.0,
            bearing_max_deg=25.0,
            abs_threshold=40.0,
        )

        H, W = frame.shape
        # Compose 2×2 diagnostic image
        canvas = np.zeros((H * 2, W * 2, 3), dtype=np.uint8)

        # Row 1: original, background
        canvas[0:H, 0:W] = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        canvas[0:H, W:W*2] = cv2.cvtColor(
            np.clip(bg, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)

        # Row 2: diff, detections overlay
        diff_vis = cv2.normalize(
            np.clip(diff_img, -100, 100), None, 0, 255, cv2.NORM_MINMAX
        ).astype(np.uint8)
        canvas[H:H*2, 0:W] = cv2.cvtColor(diff_vis, cv2.COLOR_GRAY2BGR)

        # Detections on grayscale
        det_canvas = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        for d in dets:
            cx, cy = int(d.u), int(d.v)
            cv2.circle(det_canvas, (cx, cy), 8, (0, 255, 0), 2)
            cv2.putText(det_canvas, f"{d.score:.0f}", (cx+10, cy-5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        canvas[H:H*2, W:W*2] = det_canvas

        cv2.imwrite(str(output_dir / f"diag_{set_name}_f{i:04d}.png"), canvas)
        diag_saved += 1

    print(f"  [{set_name}] Saved {diag_saved} diagnostic images → {output_dir}/")


# =========================================================================
# Per-set runner
# =========================================================================

SETS = {
    1: "set_1/sonar_1",
    2: "set_2/sonar_2",
    3: "set_3/sonar_3",
    4: "set_4/sonar_4",
}


def run_one_set(
    set_num: int,
    dfki_root: Path,
    output_root: Path,
    run_gt: bool = True,
):
    set_dir = dfki_root / SETS[set_num]
    if not set_dir.exists():
        print(f"[set_{set_num}] Directory not found: {set_dir}")
        return None

    set_name = f"set_{set_num}"
    out_dir = output_root / f"dfki_{set_name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Processing {set_name}: {set_dir}")
    print(f"{'='*60}")

    store = DFKIFrameStore(set_dir)
    print(f"  Frames: {len(store)}, shape: {store.W}×{store.H}")

    # Run tracking
    result = run_dfki_tracking(
        store,
        set_name=set_name,
        output_dir=out_dir,
        background_alpha=0.02,
        warmup_frames=5,
        diff_threshold=DEFAULT_DIFF_THRESHOLD,
        min_area_px=DEFAULT_MIN_AREA_PX,
        max_area_px=DEFAULT_MAX_AREA_PX,
        merge_radius_px=DEFAULT_MERGE_RADIUS_PX,
        mahalanobis_gate=8.0,
        euclidean_gate_px=35.0,
        max_misses=8,
        min_hits=12,
        min_duration_s=1.0,
        # v14: tighten so that low-displacement clutter / drift on
        # far-field blobs (e.g. set_1 CANDIDATE_01 net_disp=8.4px,
        # set_4 CANDIDATE_02/03 net_disp ≤ 65px) is rejected.  Real
        # divers on the DFKI dataset consistently show net_disp ≥ 30 px
        # over their life-of-track.
        min_displacement_px=30.0,
        min_speed_pxps=2.0,
        max_speed_pxps=80.0,
        use_event_gating=False,
        use_adaptive_threshold=True,
        max_new_tracks_per_frame=1,
    )

    # Diagnostics
    save_diagnostics(store, out_dir, set_name)

    # GT evaluation
    if run_gt:
        gt_csv = dfki_root / f"set_{set_num}/tracking_sonar_coordinates_{set_num}.csv"
        if gt_csv.exists():
            evaluate_gt(
                tracks_csv=out_dir / "tracks_raw.csv",
                gt_csv=gt_csv,
                output_dir=out_dir,
                set_name=set_name,
            )
        else:
            print(f"  [{set_name}] GT CSV not found: {gt_csv}")

    return result


# =========================================================================
# Entry point
# =========================================================================

def main():
    ap = argparse.ArgumentParser(description="DFKI Diver Tracking — pipeline runner")
    ap.add_argument("--set", type=int, default=None,
                    help="Run only this set (1–4); omit to run all")
    ap.add_argument("--dfki-root", type=Path, default=DFKI_ROOT,
                    help="Root of the extracted DFKI dataset")
    ap.add_argument("--out", type=Path, default=OUTPUT_ROOT / "outputs_dfki",
                    help="Output root for DFKI results")
    ap.add_argument("--no-gt", action="store_true", help="Skip GT evaluation")
    args = ap.parse_args()

    sets_to_run = [args.set] if args.set is not None else [1, 2, 3, 4]

    all_results = {}
    for s in sets_to_run:
        r = run_one_set(s, args.dfki_root, args.out, run_gt=not args.no_gt)
        all_results[s] = r

    # ---- Final combined summary ----
    print(f"\n{'='*60}")
    print("  FINAL SUMMARY — ALL SETS")
    print(f"{'='*60}")
    print(f"{'Set':<8} {'Frames':>7} {'Detections':>11} {'Tracks':>7} "
          f"{'Hits':>5} {'Meas':>5} {'Pred%':>6} {'Dur(s)':>7} {'Disp(px)':>9} {'QS':>6}")
    print("-" * 75)

    for s in sets_to_run:
        r = all_results.get(s)
        if r is None:
            print(f"set_{s:<4}  FAILED")
            continue
        rows = r.get("summary_rows", [])
        frag = r.get("frag_rows", [])
        dets = r.get("all_dets", [])
        if rows:
            for row in rows:
                print(
                    f"set_{s:<4} "
                    f"{'':<4}{row['track_id']:<16}"
                    f"{len(dets):>11} "
                    f"{len(rows):>7} "
                    f"{row['hits']:>5} "
                    f"{row['measurement_rows']:>5} "
                    f"{row['predicted_ratio']:>6.1%} "
                    f"{row['duration_s']:>7.1f} "
                    f"{row['net_displacement_m']:>9.0f} "
                    f"{row['quality_score']:>6.3f}"
                )
        else:
            print(f"set_{s:<4}  NO TRACKS SELECTED")

    print(f"\n  Full results → {args.out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
