from __future__ import annotations
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from .frame_store import FrameStore


def angle_diff_deg(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


@dataclass
class Detection:
    frame_id: int
    ts: float
    det_id: int
    range_m: float
    bearing_deg: float
    u: float
    v: float
    score: float
    peak: float
    area: int
    width_beams: int
    height_bins: int
    event_label: Optional[str] = None
    event_index: Optional[int] = None
    event_slot: Optional[int] = None


def _load_event_profile(path: Optional[Path]):
    if path is None or not Path(path).exists():
        return []
    import json
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out = []
    for i, e in enumerate(cfg.get("events", [])):
        out.append({
            "index": i,
            "label": e["label"],
            "count": int(e.get("count", 1)),
            "t0": pd.Timestamp(e["event_time_utc"]).timestamp(),
            "pre_s": float(e.get("event_window_pre_s", 0.5)),
            "post_s": float(e.get("max_start_delay_s", 30.0)),
            "min_range_m": float(e.get("expected_min_range_m", 5.0)),
            "max_range_m": float(e.get("expected_max_range_m", 45.0)),
            "min_bearing_deg": float(e.get("expected_min_bearing_deg", -55.0)),
            "max_bearing_deg": float(e.get("expected_max_bearing_deg", 55.0)),
        })
    return out


def _resolve_event_for_det(det: Detection, events: List[dict]) -> Optional[Tuple[int, int, str]]:
    """Return (event_index, slot_index, label) or None."""
    if not events:
        return None
    candidates = []
    for e in events:
        if det.ts < e["t0"] - e["pre_s"]:
            continue
        if det.ts > e["t0"] + e["post_s"]:
            continue
        if not (e["min_range_m"] <= det.range_m <= e["max_range_m"]):
            continue
        if not (e["min_bearing_deg"] <= det.bearing_deg <= e["max_bearing_deg"]):
            continue
        candidates.append(e)
    if not candidates:
        return None
    candidates.sort(key=lambda x: abs(det.ts - x["t0"]))
    return candidates[0]["index"], 0, candidates[0]["label"]


def merge_detections(dets: List[Detection], dr=0.55, da=5.0) -> List[Detection]:
    if not dets:
        return []
    used = [False] * len(dets)
    out = []
    for i, d in enumerate(dets):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, e in enumerate(dets):
                if used[j]:
                    continue
                if any(abs(e.range_m - dets[k].range_m) <= dr and
                       angle_diff_deg(e.bearing_deg, dets[k].bearing_deg) <= da
                       for k in group):
                    used[j] = True
                    group.append(j)
                    changed = True
        gs = [dets[k] for k in group]
        w = np.array([max(g.score, 1.0) for g in gs], dtype=float)
        w /= w.sum()
        r = float(sum(w[k] * gs[k].range_m for k in range(len(gs))))
        b = float(sum(w[k] * gs[k].bearing_deg for k in range(len(gs))))
        u = -r * math.sin(math.radians(b))
        v =  r * math.cos(math.radians(b))
        # carry the most-confident event label
        label_gs = [g for g in gs if g.event_label]
        lab = label_gs[0].event_label if label_gs else None
        ev_idx = label_gs[0].event_index if label_gs else None
        ev_slot = label_gs[0].event_slot if label_gs else None
        out.append(Detection(
            d.frame_id, d.ts, len(out), r, b, u, v,
            float(sum(g.score for g in gs)),
            float(max(g.peak for g in gs)),
            int(sum(g.area for g in gs)),
            int(sum(g.width_beams for g in gs)),
            int(max(g.height_bins for g in gs)),
            event_label=lab, event_index=ev_idx, event_slot=ev_slot,
        ))
    return out


def detect_frame(frame, bg, max_range, angles_deg, frame_id, ts, cfg, events):
    a = frame.astype(np.float32)
    diff = a - bg
    sm = cv2.GaussianBlur(diff, (3, 3), 0)

    med = float(np.median(sm))
    mad = float(np.median(np.abs(sm - med)))
    threshold = float(cfg["detection_threshold"])
    thr = max(threshold, med + 7.0 * 1.4826 * mad)

    mask = (sm > thr).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)

    dets = []
    nr, nb = frame.shape
    for k in range(1, n):
        x0, y0, w, h, area = [int(v) for v in stats[k]]
        if area < int(cfg["min_area_px"]) or area > int(cfg["max_area_px"]):
            continue
        sublab = labels[y0:y0+h, x0:x0+w]
        yy, xx = np.nonzero(sublab == k)
        if len(xx) == 0:
            continue
        vals = sm[y0:y0+h, x0:x0+w][sublab == k]
        weights = np.maximum(vals, 1e-3)
        xpix = float(x0 + np.average(xx, weights=weights))
        ypix = float(y0 + np.average(yy, weights=weights))
        r = (ypix + 0.5) / nr * max_range
        if r < float(cfg["min_range_m"]) or r > max_range - float(cfg["edge_margin_m"]):
            continue
        b = float(np.interp(xpix, np.arange(len(angles_deg)), angles_deg))
        if abs(b) > float(cfg["max_abs_bearing_deg"]):
            continue
        u = -r * math.sin(math.radians(b))
        v =  r * math.cos(math.radians(b))
        det = Detection(
            frame_id, ts, len(dets), float(r), b, float(u), float(v),
            float(weights.sum()), float(vals.max()), area, w, h
        )
        if cfg.get("use_event_gating", False) and events:
            hit = _resolve_event_for_det(det, events)
            if hit is None:
                continue
            ev_idx, slot, lab = hit
            det.event_label = lab
            det.event_index = ev_idx
            det.event_slot = slot
        dets.append(det)

    return merge_detections(
        dets,
        dr=float(cfg["merge_range_m"]),
        da=float(cfg["merge_bearing_deg"])
    ), thr, diff


class KFTrack:
    def __init__(self, tid: int, det: Detection):
        self.id = tid
        self.x = np.array([det.u, det.v, 0.0, 0.0], dtype=float)
        self.P = np.diag([0.4**2, 0.4**2, 2.0**2, 2.0**2])
        self.last_ts = det.ts
        self.hits = 1
        self.misses = 0
        self.age = 1
        self.history = []
        self.event_label = det.event_label
        self.event_index = det.event_index
        self.event_slot = det.event_slot
        self._record(det, matched=True)

    def predict(self, ts: float):
        dt = max(1e-3, ts - self.last_ts)
        F = np.array([[1,0,dt,0], [0,1,0,dt], [0,0,1,0], [0,0,0,1]], dtype=float)
        sa = 1.5
        q = sa**2
        Q = q * np.array([
            [dt**4/4, 0, dt**3/2, 0],
            [0, dt**4/4, 0, dt**3/2],
            [dt**3/2, 0, dt**2, 0],
            [0, dt**3/2, 0, dt**2]
        ])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        self.last_ts = ts
        self.age += 1

    def innovation_distance(self, det: Detection):
        H = np.array([[1,0,0,0], [0,1,0,0]], dtype=float)
        R = np.diag([0.45**2, 0.45**2])
        z = np.array([det.u, det.v])
        y = z - H @ self.x
        S = H @ self.P @ H.T + R
        md = float(np.sqrt(y @ np.linalg.inv(S) @ y))
        ed = float(np.linalg.norm(y))
        return md, ed

    def update(self, det: Detection):
        H = np.array([[1,0,0,0], [0,1,0,0]], dtype=float)
        R = np.diag([0.35**2, 0.35**2])
        z = np.array([det.u, det.v])
        innov = z - H @ self.x
        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x = self.x + K @ innov
        self.P = (np.eye(4) - K @ H) @ self.P
        self.hits += 1
        self.misses = 0
        if det.event_label is not None and self.event_label is None:
            self.event_label = det.event_label
            self.event_index = det.event_index
            self.event_slot = det.event_slot
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
            "age": self.age
        })


def _track_metrics(track: KFTrack):
    matched = [h for h in track.history if h["measurement_available"]]
    if not matched:
        return None
    ts = np.array([h["timestamp_s"] for h in matched], dtype=float)
    xy = np.array([[h["state"][0], h["state"][1]] for h in matched], dtype=float)
    vel = np.array([[h["state"][2], h["state"][3]] for h in matched], dtype=float)
    ranges = np.array([h["detection"].range_m for h in matched], dtype=float)
    duration = float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
    net = float(np.linalg.norm(xy[-1] - xy[0])) if len(xy) > 1 else 0.0
    path = float(np.linalg.norm(np.diff(xy, axis=0), axis=1).sum()) if len(xy) > 1 else 0.0
    speeds = np.linalg.norm(vel, axis=1)
    medspeed = float(np.median(speeds)) if len(speeds) else 0.0
    dr = float(ranges[-1] - ranges[0]) if len(ranges) > 1 else 0.0
    continuity = len(matched) / max(1, len(track.history))
    straightness = net / max(path, 1e-6)
    score = (
        0.30 * min(1.0, duration / 12.0) +
        0.25 * min(1.0, net / 15.0) +
        0.20 * continuity +
        0.15 * straightness +
        0.10 * (1.0 if dr < -2.0 else 0.0)
    )
    return {
        "track_internal_id": track.id,
        "hits": len(matched),
        "history_rows": len(track.history),
        "duration_s": duration,
        "net_displacement_m": net,
        "path_length_m": path,
        "median_speed_mps": medspeed,
        "range_change_m": dr,
        "continuity": continuity,
        "straightness": straightness,
        "quality_score": float(np.clip(score, 0.0, 1.0)),
        "start_ts": ts[0],
        "end_ts": ts[-1],
        "start_frame": matched[0]["detection"].frame_id,
        "end_frame": matched[-1]["detection"].frame_id,
    }


def run_tracking(export_dir: Path, out_dir: Path, cfg: dict, profile_path: Optional[Path] = None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = FrameStore(export_dir)

    ep = profile_path if profile_path is not None else cfg.get("event_profile_path")
    if ep and not Path(ep).is_absolute():
        ep = Path(export_dir).resolve().parent.parent / ep if not Path(ep).exists() else Path(ep)
        if not ep.exists():
            ep = Path.cwd() / cfg["event_profile_path"]
    events = _load_event_profile(ep) if cfg.get("use_event_gating", False) else []
    print(f"  event-gated tracking: {len(events)} prior events loaded")
    for e in events:
        print(f"    [{e['index']}] {e['label']} t0={pd.Timestamp(e['t0'], unit='s')}")

    active, finished = [], []
    all_dets = []
    next_id = 1
    bg = None
    prev_shape = None
    warm = 0
    alpha = float(cfg["background_alpha"])

    for i in range(len(store)):
        row = store.row(i)
        ts = float(row.time_millis) / 1000.0
        frame = store.frame(i)
        angles_deg = store.bearings_deg(i)
        max_range = float(row.max_range_m)

        if frame.shape != prev_shape:
            bg = frame.astype(np.float32)
            warm = 0
            prev_shape = frame.shape
            finished.extend(active)
            active = []
            continue

        warm += 1
        af = frame.astype(np.float32)
        if warm < int(cfg["warmup_frames"]):
            bg = 0.8 * bg + 0.2 * af
            continue

        dets, _, _ = detect_frame(frame, bg, max_range, angles_deg, i, ts, cfg, events)
        bg = (1.0 - alpha) * bg + alpha * af
        all_dets.extend(dets)

        for tr in active:
            tr.predict(ts)

        if active and dets:
            C = np.full((len(active), len(dets)), 1e6, dtype=float)
            for ti, tr in enumerate(active):
                for di, d in enumerate(dets):
                    # prefer same event label
                    if tr.event_index is not None and d.event_index is not None and tr.event_index != d.event_index:
                        continue
                    md, ed = tr.innovation_distance(d)
                    if md < float(cfg["mahalanobis_gate"]) and ed < float(cfg["euclidean_gate_m"]):
                        C[ti, di] = md + 0.03 * ed - 0.02 * math.log1p(d.score)
            rr, cc = linear_sum_assignment(C)
            matched_t, matched_d = set(), set()
            for ti, di in zip(rr, cc):
                if C[ti, di] >= 1e5:
                    continue
                active[ti].update(dets[di])
                matched_t.add(ti)
                matched_d.add(di)
            for ti, tr in enumerate(active):
                if ti not in matched_t:
                    tr.miss()
            for di, d in enumerate(dets):
                if di not in matched_d:
                    active.append(KFTrack(next_id, d))
                    next_id += 1
        else:
            for tr in active:
                tr.miss()
            for d in dets:
                active.append(KFTrack(next_id, d))
                next_id += 1

        keep = []
        for tr in active:
            if tr.misses > int(cfg["max_misses"]):
                finished.append(tr)
            else:
                keep.append(tr)
        active = keep

    finished.extend(active)

    summaries = []
    selected = []
    for tr in finished:
        m = _track_metrics(tr)
        if m is None:
            continue
        m["event_label"] = tr.event_label or ""
        m["event_index"] = tr.event_index if tr.event_index is not None else -1
        summaries.append((tr, m))
        # Per-event relaxed criteria for low-RCS targets (e.g. small PVC pipe):
        # when an event profile is loaded, allow fewer hits if the track lies
        # inside that event window AND meets displacement/speed criteria.
        per_event_min_hits = max(3, int(cfg["min_hits"]) // 3)
        eligible = (
            m["hits"] >= int(cfg["min_hits"]) and
            m["duration_s"] >= float(cfg["min_duration_s"]) and
            m["net_displacement_m"] >= float(cfg["min_displacement_m"]) and
            float(cfg["min_speed_mps"]) <= m["median_speed_mps"] <= float(cfg["max_speed_mps"])
        ) or (
            tr.event_index is not None and
            m["hits"] >= per_event_min_hits and
            m["duration_s"] >= 0.6 and
            m["net_displacement_m"] >= 0.8 and
            float(cfg["min_speed_mps"]) <= m["median_speed_mps"] <= float(cfg["max_speed_mps"])
        )
        if eligible:
            selected.append((tr, m))

    # ----- target-aware assignment for the rainstick pair -----
    # When count == 2, the goal is "label exactly two rainsticks, the strongest
    # possible pair". The previous logic simply assigned PAIR_1/PAIR_2 to the
    # two earliest-selected tracks in start-time order, which made the
    # preceding per-event relaxed criteria select fragile earlier fragments as
    # "PAIR_1" (see e.g. 5-hit short stubs winning over real tracks). Sort by
    # hit-count and displacement, drop the runt, and label the survivors.
    for ei in {tr.event_index for tr, _ in selected if tr.event_index is not None}:
        evt = next((e for e in events if e["index"] == ei), None)
        if evt is None or evt["count"] < 2:
            continue
        cands_all = [(tr, m) for tr, m in selected if tr.event_index == ei]
        # Sort survivors by hits and displacement desc; keep top `count`.
        cands_all.sort(key=lambda p: (p[1]["hits"], p[1]["net_displacement_m"]), reverse=True)
        keep = cands_all[: evt["count"]]
        # Stable secondary order on start_ts so slot 0 is the earlier one (for
        # callers that care which rainstick is which).
        keep.sort(key=lambda p: p[1]["start_ts"])
        for k, (tr, m) in enumerate(keep):
            tr.event_label = f"{evt['label']}_{k+1}"
            tr.event_slot = k
        # Drop any other tracks that shared this event_index but didn't make
        # the cut. They'll end up FRAG_## below; if they are too weak they
        # will not be written to tracks_raw.csv.
        keep_ids = {tr.id for tr, _ in keep}
        for tr, _ in cands_all:
            if tr.id not in keep_ids:
                tr.event_index = ei  # keep bookkeeping

    selected.sort(key=lambda p: (
        (p[0].event_index if p[0].event_index is not None else 1e9),
        p[1]["start_ts"]
    ))

    # ----- disambiguate event labels across overlapping KFTracks -----
    # The Kalman pipeline can produce several KFTrack instances inside one
    # prior event window (e.g. ABS main track vs. a noise blob, or three
    # rainstick pieces where two are slot-labeled rainstick_pair_1/_2 and one
    # leftover still carries the raw `rainstick_pair` label). Forcing exactly
    # ONE primary track per logical event gives stable public IDs across
    # pipelines; any extra is *_FRAG_NN and dropped from tracks_raw.csv when
    # too weak.
    from collections import defaultdict

    primary_track_ids: set[int] = set()
    fragment_relabel: dict[int, str] = {}
    fragment_drop: set[int] = set()

    # Build per-event_index cohort, treating slot-suffixed tracks as members
    # of the same cohort as raw-label tracks (e.g. "rainstick_pair", "_1", "_2"
    # all map to cohort key=3). Use the original profile label as the cohort's
    # canonical base name.
    cohorts: "dict[int, list[tuple]]" = defaultdict(list)
    for tr, m in selected:
        cohorts[(tr.event_index if tr.event_index is not None else -1)].append((tr, m))

    _base_label_of: dict[int, str] = {}

    for ei, cohort in cohorts.items():
        if len(cohort) == 1:
            tr0, _ = cohort[0]
            primary_track_ids.add(tr0.id)
            _base_label_of[tr0.id] = (tr0.event_label or "unknown").upper()
            continue

        evt = next((e for e in events if e["index"] == ei), None)
        base_name = (evt["label"].upper() if evt else "UNKNOWN")

        # Slot-suffixed tracks (e.g. rainstick_pair_1) are pinned primaries.
        slot_keepers: list[tuple] = []
        other: list[tuple] = []
        for tr, m in cohort:
            label = tr.event_label or ""
            slot_keeper = False
            if evt is not None:
                for k in range(1, evt["count"] + 1):
                    if label == f"{evt['label']}_{k}":
                        slot_keeper = True
                        slot_keepers.append((tr, m))
                        break
            if not slot_keeper:
                other.append((tr, m))

        if evt is not None and evt["count"] >= 2:
            for tr, _ in slot_keepers:
                primary_track_ids.add(tr.id)
                _base_label_of[tr.id] = (tr.event_label or "unknown").upper()
            open_slots = evt["count"] - len(slot_keepers)
            if open_slots > 0 and other:
                def _rank(item):
                    _, mm = item
                    eligible = mm["hits"] >= int(cfg["min_hits"])
                    return (
                        eligible,
                        mm["quality_score"],
                        mm["hits"],
                        mm["net_displacement_m"],
                    )
                ranked_other = sorted(other, key=_rank, reverse=True)
                for k, (tr, _) in enumerate(ranked_other[:open_slots]):
                    primary_track_ids.add(tr.id)
                    _base_label_of[tr.id] = f"{evt['label']}_{evt['count'] - open_slots + k + 1}".upper()
                other = ranked_other[open_slots:]
            other.sort(key=lambda p: p[1]["start_ts"])
            for k, (tr, m) in enumerate(other, 1):
                frag_lab = f"{base_name}_FRAG_{k:02d}"
                fragment_relabel[tr.id] = frag_lab
                _base_label_of[tr.id] = frag_lab
                if m["hits"] < int(cfg["min_hits"]) or m["net_displacement_m"] < float(cfg["min_displacement_m"]):
                    fragment_drop.add(tr.id)
            continue

        def _rank(item):
            _, mm = item
            eligible = mm["hits"] >= int(cfg["min_hits"])
            return (
                eligible,
                mm["quality_score"],
                mm["hits"],
                mm["net_displacement_m"],
            )
        ranked = sorted(cohort, key=_rank, reverse=True)
        primary_tr, _ = ranked[0]
        primary_track_ids.add(primary_tr.id)
        _base_label_of[primary_tr.id] = base_name
        for k, (tr, m) in enumerate(ranked[1:], 1):
            frag_lab = f"{base_name}_FRAG_{k:02d}"
            fragment_relabel[tr.id] = frag_lab
            _base_label_of[tr.id] = frag_lab
            if m["hits"] < int(cfg["min_hits"]) or m["net_displacement_m"] < float(cfg["min_displacement_m"]):
                fragment_drop.add(tr.id)

    public_ids = {}
    for tr, _ in selected:
        if tr.id in fragment_relabel:
            public_ids[tr.id] = fragment_relabel[tr.id]
        elif tr.id in _base_label_of:
            public_ids[tr.id] = _base_label_of[tr.id]
        else:
            lab = tr.event_label or "unknown"
            public_ids[tr.id] = lab.upper()

    det_rows = []
    for d in all_dets:
        det_rows.append({
            "timestamp_utc": pd.to_datetime(d.ts, unit="s", utc=True).isoformat().replace("+00:00", "Z"),
            "frame_id": d.frame_id,
            "detection_id": d.det_id,
            "range_m": d.range_m,
            "bearing_deg": d.bearing_deg,
            "fan_u_m": d.u,
            "fan_v_m": d.v,
            "amplitude": d.peak,
            "detection_score": d.score,
            "event_label": d.event_label or "",
            "event_index": d.event_index if d.event_index is not None else -1,
            "track_id": "",
            "matched_kftrack_id": -1,
        })

    # ----- matched_kftrack_id back-fill -----
    # Walk every selected track's history. For each measurement row, the
    # linked Detection object (if any) is the one we wrote; flip the
    # `matched_kftrack_id` and `track_id` columns so downstream can audit
    # which track consumed which detection.
    for tr in (t for t, _ in selected):
        for h in tr.history:
            if not h.get("measurement_available"):
                continue
            d = h.get("detection")
            if d is None or d.det_id is None:
                continue
            row = det_rows[d.det_id]
            row["matched_kftrack_id"] = tr.id
            row["track_id"] = public_ids.get(tr.id, "")

    track_rows = []
    for tr, m in selected:
        if tr.id in fragment_drop:
            continue
        tid = public_ids[tr.id]
        # ----- row-wise track_status -----
        # Track whether the last K frames had a real measurement. Pure-prediction
        # stretches (no hit for >=4 frames) are flagged PREDICTED_ONLY so that
        # downstream consumers can distinguish measured rows from inertial
        # extrapolation.
        PRED_RUN = 4
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
            track_rows.append({
                "timestamp_utc": pd.to_datetime(h["timestamp_s"], unit="s", utc=True).isoformat().replace("+00:00", "Z"),
                "frame_id": d.frame_id if d is not None else np.nan,
                "track_id": tid,
                "measurement_available": h["measurement_available"],
                "range_m": d.range_m if d is not None else np.nan,
                "bearing_deg": d.bearing_deg if d is not None else np.nan,
                "fan_u_meas_m": d.u if d is not None else np.nan,
                "fan_v_meas_m": d.v if d is not None else np.nan,
                "fan_u_pred_m": float(st[0]),
                "fan_v_pred_m": float(st[1]),
                "fan_u_filt_m": float(st[0]),
                "fan_v_filt_m": float(st[1]),
                "fan_speed_mps": speed,
                "heading_fan_deg": heading,
                "Puu_m2": P[0,0],
                "Puv_m2": P[0,1],
                "Pvv_m2": P[1,1],
                "Pdu_m2ps2": P[2,2],
                "Pdv_m2ps2": P[3,3],
                "hits": h["hits"],
                "misses": h["misses"],
                "age": h["age"],
                "detection_score": d.score if d is not None else np.nan,
                "track_status": status,
                "track_quality_score": m["quality_score"],
                "event_label": public_ids[tr.id] if (d is not None and d.event_label is not None) else (tr.event_label or ""),
            })

    tracks_df = pd.DataFrame(track_rows)
    tracks_df.to_csv(out_dir / "tracks_raw.csv", index=False)

    dets_df = pd.DataFrame(det_rows)
    dets_df.to_csv(out_dir / "detections.csv", index=False)

    # ----- track_summary_all.csv -----
    summary_rows = []
    for tr, m in sorted(summaries, key=lambda p: (
        (p[0].event_index if p[0].event_index is not None else 1e9),
        p[1]["start_ts"],
    )):
        tid = public_ids.get(tr.id, (tr.event_label or "unknown").upper())
        start_iso = pd.to_datetime(m["start_ts"], unit="s", utc=True).isoformat().replace("+00:00", "Z")
        end_iso = pd.to_datetime(m["end_ts"], unit="s", utc=True).isoformat().replace("+00:00", "Z")
        # Range change = first measured range − last measured range (positive
        # if the target approached the sonar, which is the convention used
        # by `standardize.assign_event_labels`).
        first_r = next((h["detection"].range_m for h in tr.history if h.get("measurement_available") and h.get("detection") is not None), np.nan)
        last_r  = None
        for h in tr.history:
            if h.get("measurement_available") and h.get("detection") is not None:
                last_r = h["detection"].range_m
        range_change = float(first_r) - float(last_r) if last_r is not None else 0.0
        summary_rows.append({
            "track_internal_id": tr.id,
            "event_label": tr.event_label or "",
            "event_index": tr.event_index if tr.event_index is not None else -1,
            "selected_track_id": tid,
            "hits": m["hits"],
            "history_rows": m["history_rows"],
            "start_ts": m["start_ts"],
            "end_ts": m["end_ts"],
            "start_time_utc": start_iso,
            "end_time_utc": end_iso,
            "duration_s": m["duration_s"],
            "net_displacement_m": m["net_displacement_m"],
            "range_change_m": range_change,
            "median_speed_mps": m["median_speed_mps"],
            "quality_score": m["quality_score"],
            "start_frame": m["start_frame"],
            "end_frame": m["end_frame"],
            "in_tracks_raw_csv": tr.id not in fragment_drop,
        })
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(out_dir / "track_summary_all.csv", index=False)

    return tracks_df, summary_df
