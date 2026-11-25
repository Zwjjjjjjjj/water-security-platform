"""
DFKI Diver Sonar — detection in pixel coordinates.

Provides ``detect_dfki_frame()`` which is API-compatible with the existing
``detect_frame()`` in ``detect_track.py`` but works entirely in pixel space
(no physical range/bearing conversion).

The key differences from ``detect_frame`` for Gemini GLF data:

    10|* State coordinates are (xpix, ypix) rather than (fan_u_m, fan_v_m).
* All gates and thresholds are defined in pixel units.
* ``range_m`` and ``bearing_deg`` stored in the Detection are set to the
  pixel coordinate as a convention (not physical values).
* The ``Detection`` dataclass from detect_track.py is reused directly.
"""

from __future__ import annotations
import math
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .detect_track import Detection, merge_detections


# ---------------------------------------------------------------------------
# Default thresholds (calibrated on set_1 set_2 set_3 set_4)
# ---------------------------------------------------------------------------
# Key insight: the diver is bright in ABSOLUTE terms (pixel > 40 in sonar fan),
# but background subtraction fails because the background model includes the diver
# from the reference frame, making the true diff below threshold.
# Solution: use ABSOLUTE intensity threshold as a complementary signal.
DEFAULT_DIFF_THRESHOLD   = 12.0   # background subtraction threshold — secondary signal (active)
DEFAULT_ABS_THRESHOLD    = 55.0   # absolute intensity threshold — primary signal for diver blobs
DEFAULT_MIN_AREA_PX      = 15     # pixels — captures small diver blob; noise filtered by geo gates
DEFAULT_MAX_AREA_PX      = 5000   # pixels — upper bound; much larger blobs excluded
DEFAULT_MERGE_RADIUS_PX  = 30     # pixels — merges body + equipment fragments
DEFAULT_MAX_DETECTIONS    = 8      # max detections per frame returned to tracker
DEFAULT_MAX_RADIUS_PX    = 450    # pixels — outer edge; GT diver at ~259px must pass
DEFAULT_MIN_RADIUS_PX    = 140    # pixels — inner edge; GT diver at ~259px
DEFAULT_BEARING_MIN_DEG  = -45.0  # exclude platform-bearing zone
DEFAULT_BEARING_MAX_DEG  =  25.0  # diver's bearing range
# ROI bias (active tracking → suppress detections far from predicted diver)
DEFAULT_ROI_RADIUS_PX    = 80.0   # radius around predicted diver location
DEFAULT_ROI_BIAS         = 0.50   # 0=no bias, 1=strong suppression


def detect_dfki_frame(
    frame: np.ndarray,
    bg: np.ndarray,
    frame_id: int,
    ts: float,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    max_area_px: int = DEFAULT_MAX_AREA_PX,
    merge_radius_px: float = DEFAULT_MERGE_RADIUS_PX,
    max_radius_px: float = DEFAULT_MAX_RADIUS_PX,
    min_radius_px: float = DEFAULT_MIN_RADIUS_PX,
    bearing_min_deg: float = DEFAULT_BEARING_MIN_DEG,
    bearing_max_deg: float = DEFAULT_BEARING_MAX_DEG,
    use_adaptive_threshold: bool = True,
    predicted_u: Optional[float] = None,
    predicted_v: Optional[float] = None,
    roi_radius_px: float = DEFAULT_ROI_RADIUS_PX,
    roi_bias: float = DEFAULT_ROI_BIAS,
    abs_threshold: float = DEFAULT_ABS_THRESHOLD,
    max_detections: int = DEFAULT_MAX_DETECTIONS,
) -> Tuple[List[Detection], float, np.ndarray]:
    """
    Detect blobs in a DFKI sonar JPG frame.

    Detection strategy: background subtraction OR absolute intensity.
    Two signals are combined so that neither alone dominates:
    - Background subtraction finds NEW bright spots vs a learned background.
    - Absolute intensity finds inherently bright sonar returns (diver echoes).

    Parameters
    ----------
    diff_threshold : float
        Background-difference threshold.  Adaptive floor: max(diff_threshold, median + 6*MAD).
    abs_threshold : float
        Absolute pixel intensity threshold.  Blobs bright in absolute terms
        (pixel value > abs_threshold) qualify regardless of background difference.
    predicted_u / predicted_v : float | None
        Kalman-predicted position.  If set and roi_bias > 0, detections far
        from this point have their effective thresholds raised.
    """
    a = frame.astype(np.float32)
    diff_img = a - bg
    sm = cv2.GaussianBlur(diff_img, (3, 3), 0)

    # Adaptive floor for background subtraction
    med = float(np.median(sm))
    mad = float(np.median(np.abs(sm - med)))
    thr = float(diff_threshold)
    if use_adaptive_threshold:
        thr = max(thr, med + 6.0 * 1.4826 * mad)

    # ROI bias map
    has_roi = (
        predicted_u is not None and predicted_v is not None and roi_bias > 0
    )
    if has_roi:
        yy_g, xx_g = np.indices(sm.shape, dtype=np.float32)
        dist_to_roi = np.sqrt(
            (xx_g - predicted_u) ** 2 + (yy_g - predicted_v) ** 2
        )
        roi_mult = 1.0 + roi_bias * np.clip(
            (dist_to_roi - roi_radius_px) / max(roi_radius_px, 1.0),
            0.0, 1.0,
        )
        eff_thr_map = thr * roi_mult
    else:
        eff_thr_map = None

    # Signal 1: background subtraction
    diff_mask = (sm > (eff_thr_map if eff_thr_map is not None else thr)).astype(np.uint8)

    # Signal 2: absolute intensity
    abs_mask = (a > abs_threshold).astype(np.uint8)

    # OR combination — blob qualifies if EITHER signal fires
    combined = cv2.bitwise_or(diff_mask, abs_mask)
    combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(combined, 8)

    # Sonar origin
    H, W = frame.shape
    cx = (W - 1) / 2.0
    cy = H - 1.0

    dets: List[Detection] = []
    for k in range(1, n):
        area = int(stats[k, cv2.CC_STAT_AREA])
        if area < min_area_px or area > max_area_px:
            continue

        cx_k = float(centroids[k, 0])
        cy_k = float(centroids[k, 1])

        dx = cx_k - cx
        dy = cy - cy_k
        r_pix = math.hypot(dx, dy)
        if r_pix < min_radius_px or r_pix > max_radius_px:
            continue

        # Use raw CC centroid directly (weighted centroid drifts on sonar images)
        xw = cx_k
        yw = cy_k
        b_det = math.degrees(math.atan2(xw - cx, cy - yw + 1e-9))
        if not (bearing_min_deg <= b_det <= bearing_max_deg):
            continue

        # Score based on blob pixels above threshold
        submask = (labels == k).astype(np.uint8)
        yy, xx = np.nonzero(submask)
        if len(xx) == 0:
            continue
        vals = sm[yy, xx]
        score = float(vals.sum())
        peak = float(vals.max())

        u_val = xw - cx
        v_val = cy - yw
        r_det = r_pix

        det = Detection(
            frame_id=frame_id,
            ts=ts,
            det_id=len(dets),
            range_m=r_det,
            bearing_deg=b_det,
            u=float(xw),
            v=float(yw),
            score=score,
            peak=peak,
            area=area,
            width_beams=int(stats[k, cv2.CC_STAT_WIDTH]),
            height_bins=int(stats[k, cv2.CC_STAT_HEIGHT]),
            event_label=None,
            event_index=None,
            event_slot=None,
        )
        dets.append(det)

    # Return all detections sorted by score (larger/brighter blobs first)
    # do NOT merge — merge was causing diver fragments to be absorbed into wrong large blobs
    dets.sort(key=lambda d: d.score, reverse=True)
    # Keep top-K per frame to limit noise reaching the tracker
    dets = dets[:max_detections]
    return dets, thr, diff_img



def detect_dfki_sequence(
    store,
    cfg: dict,
    output_dir: Path,
    set_name: str,
    *,
    background_alpha: float = 0.02,
    warmup_frames: int = 5,
    diff_threshold: float = DEFAULT_DIFF_THRESHOLD,
    min_area_px: int = DEFAULT_MIN_AREA_PX,
    max_area_px: int = DEFAULT_MAX_AREA_PX,
    merge_radius_px: float = DEFAULT_MERGE_RADIUS_PX,
    mahalanobis_gate: float = 8.0,
    euclidean_gate_px: float = 60.0,
    max_misses: int = 8,
    min_hits: int = 5,
    min_duration_s: float = 0.5,
    min_displacement_px: float = 15.0,
    min_speed_pxps: float = 1.0,
    max_speed_pxps: float = 80.0,
    use_adaptive_threshold: bool = True,
):
    """
    Run background-subtraction detection on every frame of a DFKI set.

    This function is purely for diagnostics: it produces per-frame detection
    CSVs and summary statistics WITHOUT initiating a Kalman tracker.
    Call this first to calibrate the threshold before enabling the tracker.

    Parameters
    ----------
    store : DFKIFrameStore
        Frame store for the current set.
    cfg : dict
        Unused (kept for API symmetry with run_tracking).
    output_dir : Path
        Where to write diagnostics.
    set_name : str
        Label for this set (e.g. "set_1").

    Returns
    -------
    all_detections : list[Detection]
    thresholds_used : list[float]
    diff_images : list[np.ndarray]
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    det_rows = []
    thresholds = []
    bg = None
    warm = 0
    prev_shape = None
    all_dets = []

    for i in range(len(store)):
        row = store.row(i)
        ts = float(row["timestamp_s"])
        frame = store.frame(i)

        if frame.shape != prev_shape:
            bg = frame.astype(np.float32)
            warm = 0
            prev_shape = frame.shape
            continue

        warm += 1
        af = frame.astype(np.float32)

        if warm < warmup_frames:
            bg = 0.8 * bg + 0.2 * af
            thresholds.append(0.0)
            continue

        dets, thr, diff_img = detect_dfki_frame(
            frame, bg, i, ts,
            diff_threshold=diff_threshold,
            min_area_px=min_area_px,
            max_area_px=max_area_px,
            merge_radius_px=merge_radius_px,
            use_adaptive_threshold=use_adaptive_threshold,
        )
        thresholds.append(thr)
        bg = (1.0 - background_alpha) * bg + background_alpha * af

        for d in dets:
            det_rows.append({
                "frame_id": d.frame_id,
                "timestamp_s": d.ts,
                "image_name": row["image_name"],
                "xpix": d.u,
                "ypix": d.v,
                "range_pix": d.range_m,
                "bearing_deg": d.bearing_deg,
                "area_px": d.area,
                "score": d.score,
                "peak": d.peak,
                "width_px": d.width_beams,
                "height_px": d.height_bins,
            })
        all_dets.extend(dets)

    df = pd.DataFrame(det_rows)
    import pandas as pd
    df.to_csv(output_dir / "detections.csv", index=False)

    # Summary statistics
    n_frames = len(store)
    n_det_frames = df["frame_id"].nunique() if len(df) else 0
    n_dets = len(df)
    mean_per_frame = n_dets / n_frames if n_frames else 0
    mean_thr = np.mean(thresholds) if thresholds else 0.0

    summary = {
        "set": set_name,
        "n_frames": n_frames,
        "n_frames_with_detections": n_det_frames,
        "total_detections": n_dets,
        "mean_detections_per_frame": round(mean_per_frame, 3),
        "mean_threshold_used": round(mean_thr, 2),
        "diff_threshold_input": diff_threshold,
        "min_area_px": min_area_px,
        "max_area_px": max_area_px,
        "use_adaptive_threshold": use_adaptive_threshold,
    }
    import json
    with open(output_dir / "detection_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"  [{set_name}] {n_dets} detections in {n_frames} frames "
          f"({n_det_frames} with detections); mean={mean_per_frame:.1f}/frame; "
          f"mean thr={mean_thr:.1f}")

    return all_dets, thresholds, det_rows
