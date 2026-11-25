"""
End-to-end evaluation: tracking quality + association quality + fusion quality.

This module is called by `run_e2e_pipeline.py` (as stage 8) AFTER the bridge
has produced its stage 4..6 outputs.  It computes three families of metrics:

  A) TRACKING QUALITY (single-sonar)
     - Per-track stats: duration, hits, predicted_ratio, quality_score,
       mean speed, net displacement.
     - For DFKI: position RMSE of the PRIMARY track against the GT CSV
       (after the A-pipeline's standardized coordinate frame is mapped back
       to image pixels).  Coverage = n_track_frames / n_gt_frames.

  B) DATA SUFFICIENCY + SYNTHESIS QUALITY (the "B-synthesis" report)
     Because we have only ONE real sonar, the e2e_bridge synthesizes a
     plausible second sonar by perturbing the primary A track with
     translation, yaw, time offset, jitter, dropouts.  This stage
     quantifies how much data the bridge had to work with and how clean
     the synthesized B is:
       n_A_frames, n_B_frames, n_aligned_pairs,
       alignment_rate = n_aligned_pairs / n_A_frames,
       B-synthesis sanity = median A↔B position distance
                            (should be ≈ config translation_x_px).

  C) ASSOCIATION QUALITY
     Uses the SYNTHETIC GROUND TRUTH (we know B was generated from the
     PRIMARY A track) to compute:
       - true / false / missed associations
       - association precision / recall / F1
       - ID-switches
     gt_mapping = {A_PRIMARY_DIVER: B_PRIMARY_DIVER,
                   A_OTHER: None,    # no true B partner
                   ...}

  D) FUSION QUALITY
     For the BOTH_MEASURED frames, the fused state should be a covariance-
     weighted average of A and B, hence CLOSER to the true A-position than
     the noisy B is.  We report:
       - RMSE( x_fused - x_A ) vs RMSE( x_B - x_A )   (information gain)
       - mean trace of fused P vs trace of A-only P    (uncertainty reduction)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
# tritech_paths 位于 src/ 下；包内导入与顶层导入两种方式都要支持。
try:  # 包内导入（src.tritech_paths）
    from .tritech_paths import DFKI_ROOT
except ImportError:  # 顶层导入（tritech_paths）
    from tritech_paths import DFKI_ROOT


# ---------------------------------------------------------------------------
# A) Tracking quality
# ---------------------------------------------------------------------------
def evaluate_tracking_quality(
    tracks_raw_csv: Path,
    track_summary_csv: Path,
    dfki_set_num: Optional[int] = None,
    dfki_root: Optional[Path] = None,
    standardized_csv: Optional[Path] = None,
) -> Dict:
    """Per-track stats + (for DFKI) GT-based RMSE for the PRIMARY track.

    Parameters
    ----------
    tracks_raw_csv : stage 2 output (per-frame rows)
    track_summary_csv : stage 2 output (per-track summary)
    dfki_set_num : 1..4 if the dataset is DFKI, else None
    dfki_root : root of the "Diver Tracking - Sonar" directory

    Returns
    -------
    dict with keys:
        per_track : list of per-track metric dicts
        primary_vs_gt : dict with RMSE / coverage / median_err (or None for GLF)
    """
    tracks = pd.read_csv(tracks_raw_csv)
    summary = pd.read_csv(track_summary_csv)
    if tracks.empty or summary.empty:
        return {"per_track": [], "primary_vs_gt": None}

    # Detect the track-id column in the summary file (could be
    # `track_id` for DFKI or `selected_track_id` for GLF).
    summary_tid_col = None
    for cand in ("track_id", "selected_track_id", "track_internal_id"):
        if cand in summary.columns:
            summary_tid_col = cand
            break
    if summary_tid_col is None:
        return {"per_track": [], "primary_vs_gt": None,
                "error": f"no track-id column in {track_summary_csv} (cols: {list(summary.columns)})"}

    # Per-track: extract first frame timestamp / last frame timestamp / n rows
    per_track: List[Dict] = []
    # The summary rows are not deduplicated by track; one track can appear
    # in multiple summary rows.  Group by summary_tid_col.
    primary_tid: Optional[str] = None
    primary_quality = -1.0
    for _, row in summary.iterrows():
        tid = row[summary_tid_col]
        if pd.isna(tid):
            continue
        # In the GLF summary, the column says `selected_track_id` (e.g.
        # 4IN_ABS_PIPE) which is a per-row track name.  We use that as the
        # track_id in the output.
        sub = tracks[tracks["track_id"] == tid]
        if sub.empty:
            # Try internal numeric id (DFKI-style)
            try:
                internal = int(row.get("track_internal_id", -1))
            except (TypeError, ValueError):
                internal = -1
            if internal >= 0:
                sub = tracks[tracks["track_id"] == f"T{internal:03d}"]
        if sub.empty:
            continue
        # Per-track RMSE of filtered vs measured (Kalman consistency)
        if "fan_u_meas_m" in sub.columns and "fan_u_pred_m" in sub.columns:
            meas = sub[sub["measurement_available"] == True]  # noqa: E712
            if not meas.empty:
                rmse_x = float(np.sqrt(((meas["fan_u_meas_m"] - meas["fan_u_pred_m"]) ** 2).mean()))
                rmse_y = float(np.sqrt(((meas["fan_v_meas_m"] - meas["fan_v_pred_m"]) ** 2).mean()))
            else:
                rmse_x = rmse_y = float("nan")
        else:
            rmse_x = rmse_y = float("nan")
        # Mean speed (using the standardized speed column)
        mean_speed = float(sub["fan_speed_mps"].mean()) if "fan_speed_mps" in sub.columns else float("nan")
        net_disp = float(row.get("net_displacement_m", float("nan")))
        quality = float(row.get("quality_score", 0.0))
        if quality > primary_quality:
            primary_quality = quality
            primary_tid = tid
        per_track.append({
            "track_id": str(tid),
            "internal_id": int(row.get("track_internal_id", -1)) if not pd.isna(row.get("track_internal_id", np.nan)) else -1,
            "n_rows": int(len(sub)),
            "hits": int(row.get("hits", len(sub))),
            "duration_s": float(row.get("duration_s", 0.0)),
            "predicted_ratio": 0.0,  # not present in GLF summary
            "quality_score": quality,
            "mean_speed_mps": mean_speed,
            "net_displacement_m": net_disp,
            "kalman_rmse_u_m": rmse_x,
            "kalman_rmse_v_m": rmse_y,
            "is_primary": False,  # filled in below
        })
    # Mark the highest-quality track as primary (for GLF; for DFKI the
    # PRIMARY_DIVER is already named)
    if primary_tid and not any(p.get("is_primary") for p in per_track):
        for p in per_track:
            if p["track_id"] == primary_tid:
                p["is_primary"] = True
                break

    # DFKI: compute PRIMARY-vs-GT RMSE
    primary_vs_gt: Optional[Dict] = None
    if dfki_set_num is not None and dfki_root is not None:
        gt_path = dfki_root / f"set_{dfki_set_num}" / f"tracking_sonar_coordinates_{dfki_set_num}.csv"
        if gt_path.exists():
            gt = pd.read_csv(gt_path)
            # PRIMARY track
            primary_rows = tracks[tracks["track_id"].str.upper().str.startswith("PRIMARY")].copy()
            if not primary_rows.empty:
                # The stage 2 raw tracks have image coordinates as
                # (range_m, bearing_deg) and pixel coordinates as
                # (fan_u_meas_m, fan_v_meas_m) — for DFKI the "m" naming is
                # misleading; the values are pixels.  The standardized CSV
                # (stage 3) has explicit x_pix / y_pix columns.  Prefer those.
                cols_needed = ["ts", "x_pix", "y_pix"]
                # Try the stage 2 raw columns first (they are present here)
                # and fall back to standardized columns if available.
                if not all(c in primary_rows.columns for c in cols_needed):
                    if standardized_csv and standardized_csv.exists():
                        std_df = pd.read_csv(standardized_csv)
                        std_primary = std_df[std_df["track_id"].str.upper().str.startswith("PRIMARY")].copy()
                        if not std_primary.empty:
                            std_primary["ts"] = pd.to_datetime(
                                std_primary["timestamp_utc"], utc=True, errors="coerce"
                            )
                            primary_rows = std_primary
                # Build GT timestamps from image filename (= unix seconds)
                gt["ts"] = pd.to_datetime(
                    pd.to_numeric(gt["image_name"].str.replace(".jpg", "", regex=False), errors="coerce"),
                    unit="s", utc=True, errors="coerce",
                )
                merged = pd.merge(
                    primary_rows[[c for c in ("ts", "x_pix", "y_pix") if c in primary_rows.columns]],
                    gt[["ts", "x", "y"]],
                    on="ts", how="inner",
                )
                if not merged.empty:
                    # Image pixel coordinates (drop NaN positions)
                    valid = merged.dropna(subset=["x_pix", "y_pix", "x", "y"])
                    if valid.empty:
                        primary_vs_gt = {
                            "n_matched_frames": int(len(merged)),
                            "n_gt_frames": int(len(gt)),
                            "n_track_frames": int(len(primary_rows)),
                            "note": "matched frames have NaN positions",
                        }
                    else:
                        dx = valid["x_pix"] - valid["x"]
                        dy = valid["y_pix"] - valid["y"]
                        errs = np.hypot(dx, dy)
                        primary_vs_gt = {
                            "n_matched_frames": int(len(valid)),
                            "n_gt_frames": int(len(gt)),
                            "n_track_frames": int(len(primary_rows)),
                            "coverage": round(len(valid) / max(1, len(gt)), 4),
                            "rmse_px": round(float(np.sqrt((dx ** 2 + dy ** 2).mean())), 2),
                            "median_err_px": round(float(np.median(errs)), 2),
                            "p90_err_px": round(float(np.percentile(errs, 90)), 2),
                            "max_err_px": round(float(np.max(errs)), 2),
                            "mean_dx_px": round(float(dx.mean()), 2),
                            "mean_dy_px": round(float(dy.mean()), 2),
                        }
                else:
                    primary_vs_gt = {
                        "n_matched_frames": 0, "n_gt_frames": int(len(gt)),
                        "n_track_frames": int(len(primary_rows)),
                        "note": "no timestamps matched between PRIMARY and GT",
                    }
            else:
                primary_vs_gt = {"note": "no PRIMARY track found"}

    return {"per_track": per_track, "primary_vs_gt": primary_vs_gt}


# ---------------------------------------------------------------------------
# B) Data sufficiency + synthesis quality
# ---------------------------------------------------------------------------
def evaluate_data_sufficiency(
    sonar_a_meas_csv: Path,
    sonar_b_meas_csv: Path,
    aligned_pairs_csv: Path,
    bridge_translation_x_px: float = 25.0,
    bridge_translation_y_px: float = 0.0,
) -> Dict:
    """Quantify how much data the bridge had and how clean the synthesized B is.

    The e2e pipeline is fundamentally a one-real-sonar pipeline, so
    the bridge has to *manufacture* a second stream.  This function
    makes that visible: how many A frames, how many synthesized B
    frames, how many aligned pairs, and the median A↔B position
    distance (which should be ≈ config translation_x_px if the
    synthesis parameters are honored).
    """
    a = pd.read_csv(sonar_a_meas_csv) if Path(sonar_a_meas_csv).exists() else pd.DataFrame()
    b = pd.read_csv(sonar_b_meas_csv) if Path(sonar_b_meas_csv).exists() else pd.DataFrame()
    pairs = pd.read_csv(aligned_pairs_csv) if Path(aligned_pairs_csv).exists() else pd.DataFrame()

    n_a = int(len(a))
    n_b = int(len(b))
    n_aligned = int(len(pairs))

    # Expected A↔B distance is the magnitude of the configured translation
    # (the yaw rotation is small for both GLF and DFKI in this project, so
    # the Euclidean magnitude of (tx, ty) is a good proxy).
    expected_d = float(math.hypot(bridge_translation_x_px, bridge_translation_y_px))

    if pairs.empty:
        return {
            "n_A_frames": n_a,
            "n_B_frames": n_b,
            "n_aligned_pairs": 0,
            "alignment_rate_vs_A": 0.0,
            "alignment_rate_vs_B": 0.0,
            "median_A_to_B_distance_px": None,
            "expected_translation_distance_px": expected_d,
            "synthesis_ok": False,
        }

    med = float(pairs["position_distance"].median())
    mean_d = float(pairs["position_distance"].mean())
    p90_d = float(pairs["position_distance"].quantile(0.9))
    # Sanity: the synthesized B is offset from A by `expected_d` (with noise σ).
    delta = abs(med - expected_d)
    # Allow a generous tolerance: 3 × noise sigma + a small fudge factor.
    # For DFKI: noise σ=8px, so tolerance ~25px.  For GLF: noise σ=0.18m, so ~1m.
    synthesis_ok = delta <= max(25.0, expected_d * 0.5)

    return {
        "n_A_frames": n_a,
        "n_B_frames": n_b,
        "n_aligned_pairs": n_aligned,
        "alignment_rate_vs_A": round(n_aligned / max(1, n_a), 4),
        "alignment_rate_vs_B": round(min(1.0, n_aligned / max(1, n_b)), 4),
        "median_A_to_B_distance_px": round(med, 2),
        "mean_A_to_B_distance_px": round(mean_d, 2),
        "p90_A_to_B_distance_px": round(p90_d, 2),
        "expected_translation_distance_px": expected_d,
        "synthesis_ok": bool(synthesis_ok),
        "synthesis_deviation_px": round(delta, 2),
    }


# ---------------------------------------------------------------------------
# C) Association quality (against synthetic GT)
# ---------------------------------------------------------------------------
def evaluate_association_quality(
    association_results_csv: Path,
    local_to_global_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
) -> Dict:
    """
    The e2e bridge synthesizes Sonar B from the longest (primary) A track.
    Therefore the SYNTHETIC GROUND TRUTH is:

        gt_mapping[ A_PRIMARY_DIVER ] = B_PRIMARY_DIVER
        gt_mapping[ A_OTHER ]         = None    (no true B partner)

    We use this to compute association precision / recall / F1 / ID-switches.
    """
    assoc = pd.read_csv(association_results_csv) if Path(association_results_csv).exists() else pd.DataFrame()
    l2g = pd.read_csv(local_to_global_csv) if Path(local_to_global_csv).exists() else pd.DataFrame()
    tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
    tr_b = pd.read_csv(tr_b_csv) if Path(tr_b_csv).exists() else pd.DataFrame()

    # Build synthetic GT
    a_tracks = sorted(tr_a["local_track_id"].unique().tolist()) if "local_track_id" in tr_a.columns else []
    b_tracks = sorted(tr_b["local_track_id"].unique().tolist()) if "local_track_id" in tr_b.columns else []
    # The bridge writes the B track_id as the same underlying track_id as the
    # source A track (see e2e_bridge._synthesise_sonar_b).  So the synthetic GT
    # pair is (A_<source_track_id>, B_<source_track_id>).
    # Prefer the track whose ID starts with "PRIMARY_" (DFKI naming).  If none
    # match (e.g. GLF, where tracks are named by event like 5/8IN_PLYWOOD),
    # fall back to the longest A track — which is exactly what the bridge
    # synthesizer uses to pick the source.
    a_primary = next((t for t in a_tracks if "PRIMARY" in t.upper()), None)
    b_primary = next((t for t in b_tracks if "PRIMARY" in t.upper()), None)
    if a_primary is None and a_tracks:
        a_primary = max(
            a_tracks,
            key=lambda t: int(len(tr_a[tr_a["local_track_id"] == t]))
        )
        # Find the matching B track (same underlying name)
        underlying = a_primary.split("_", 1)[1] if "_" in a_primary else a_primary
        b_primary = next(
            (b for b in b_tracks if b.endswith("_" + underlying) or b == "B_" + underlying),
            None,
        )

    gt_mapping: Dict[str, Optional[str]] = {t: None for t in a_tracks}
    if a_primary and b_primary:
        gt_mapping[a_primary] = b_primary

    # ---- Association precision / recall ----
    gt_pairs = {a_primary: b_primary} if (a_primary and b_primary) else {}
    actual_pairs = {row["track_a_id"]: row["track_b_id"] for _, row in assoc.iterrows()}

    if gt_pairs:
        tp = sum(1 for a, b in actual_pairs.items() if gt_pairs.get(a) == b)
        fp = sum(1 for a, b in actual_pairs.items() if gt_pairs.get(a) != b)
        fn = sum(1 for a, b in gt_pairs.items() if actual_pairs.get(a) != b)
    else:
        tp = fp = fn = 0
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)

    # ---- ID switches (at the global track level) ----
    id_switches = 0
    if not l2g.empty and gt_mapping:
        merged = l2g.copy()
        merged["gt_target"] = merged["local_track_id"].map(gt_mapping)
        for gid, grp in merged.groupby("global_id"):
            targets = set(grp["gt_target"].dropna())
            if len(targets) > 1:
                id_switches += 1

    # ---- Mean association cost ----
    if not assoc.empty and "cost" in assoc.columns:
        mean_cost = float(assoc["cost"].mean())
        min_cost = float(assoc["cost"].min())
        max_cost = float(assoc["cost"].max())
    else:
        mean_cost = min_cost = max_cost = None

    return {
        "n_A_local_tracks": len(a_tracks),
        "n_B_local_tracks": len(b_tracks),
        "synthetic_gt_a_primary": a_primary,
        "synthetic_gt_b_primary": b_primary,
        "n_gt_pairs": len(gt_pairs),
        "n_predicted_associations": len(actual_pairs),
        "n_true_positive": tp,
        "n_false_positive": fp,
        "n_false_negative": fn,
        "association_precision": round(precision, 4),
        "association_recall": round(recall, 4),
        "association_f1": round(f1, 4),
        "id_switches": id_switches,
        "mean_association_cost": round(mean_cost, 4) if mean_cost is not None else None,
        "min_association_cost": round(min_cost, 4) if min_cost is not None else None,
        "max_association_cost": round(max_cost, 4) if max_cost is not None else None,
    }


# ---------------------------------------------------------------------------
# D) Fusion quality
# ---------------------------------------------------------------------------
def evaluate_fusion_quality(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
) -> Dict:
    """
    For BOTH_MEASURED frames, the fused (x, y) should sit between the noisy A
    and B measurements, and the fused covariance should be smaller than either
    individual covariance (information gain).
    """
    fused = pd.read_csv(fused_csv) if Path(fused_csv).exists() else pd.DataFrame()
    if fused.empty:
        return {"n_both_measured": 0}

    both = fused[fused["fusion_mode"] == "BOTH_MEASURED"].copy()
    a_only = fused[fused["fusion_mode"] == "A_ONLY"].copy()
    b_only = fused[fused["fusion_mode"] == "B_ONLY"].copy()

    # The fusion engine can emit more modes than the three "measured"
    # ones.  Notably BOTH_PREDICTED is produced whenever both local tracks
    # exist at a timestamp but neither had an actual measurement there
    # (both are pure Kalman prediction steps), and NONE is produced when a
    # global track has no local track within tolerance.  Count EVERY mode
    # from the data instead of hard-coding a list, then verify that the
    # mode counts are conserved against the row count.
    mode_counts = (
        fused["fusion_mode"].value_counts().sort_index().to_dict()
        if not fused.empty else {}
    )
    _counted = int(sum(int(v) for v in mode_counts.values()))
    _total = int(len(fused))
    _conserved = (_counted == _total)

    # Build per-global-id lookups
    a_idx: Dict[str, pd.DataFrame] = {}
    b_idx: Dict[str, pd.DataFrame] = {}
    tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
    tr_b = pd.read_csv(tr_b_csv) if Path(tr_b_csv).exists() else pd.DataFrame()

    def _index_by_lid(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        if df.empty or "local_track_id" not in df.columns:
            return out
        for tid, grp in df.groupby("local_track_id"):
            out[tid] = grp.set_index("timestamp").sort_index()
        return out

    a_by_tid = _index_by_lid(tr_a)
    b_by_tid = _index_by_lid(tr_b)

    # Map global_id -> sensor local_track_id
    out: Dict = {
        "n_fused_total": _total,
        "n_both_measured": int(len(both)),
        "n_A_only": int(len(a_only)),
        "n_B_only": int(len(b_only)),
        # Every mode actually emitted by the fusion engine, not just the
        # three measured ones.  Keys are mode names, values are counts.
        "fusion_mode_counts": {str(k): int(v) for k, v in mode_counts.items()},
        "n_both_predicted": int(mode_counts.get("BOTH_PREDICTED", 0)),
        "n_none": int(mode_counts.get("NONE", 0)),
        "n_valid": int(fused["is_valid"].sum()) if "is_valid" in fused.columns else _total,
        # Conservation check: the sum of all fusion modes must equal the
        # total number of fused rows, otherwise some rows are unaccounted
        # for and the per-mode table is misleading.
        "mode_counts_conserved": bool(_conserved),
        "mode_count_sum": _counted,
    }
    if not _conserved:
        print(f"    [WARN] fusion_mode counts do not sum to row count "
              f"({_counted} != {_total}); modes={mode_counts}")

    if both.empty:
        return out

    # We compare fused (x, y) to A measurement (x, y) on BOTH_MEASURED frames
    # (A is the reference; B is the noisy partner).
    # We need to know which local A track each GID corresponds to.
    # This comes from local_to_global.
    # But we have only fused_csv here; instead, we iterate the "largest" A track
    # in each GID.
    rmse_x_list = []
    rmse_y_list = []
    info_gain_x_list = []
    info_gain_y_list = []
    pos_err_b_vs_a = []  # how far is B from A in BOTH_MEASURED frames
    pos_err_fused_vs_a = []  # how far is fused from A
    pos_err_fused_vs_truth = []  # if we treat A as truth

    a_by_gid: Dict[str, str] = {}
    b_by_gid: Dict[str, str] = {}
    if not both.empty:
        for gid in both["global_id"].unique():
            # Find which A track has the most rows in this GID
            cand_a = [tid for tid, df in a_by_tid.items() if not df.empty]
            # The bridge's local_to_global is the authoritative source. We try
            # to read it lazily; if unavailable, fall back to the most common
            # local_track_id by timestamp intersection.
            def _overlap_a(tid):
                return _overlap_with_fused(a_by_tid[tid], both, gid)
            def _overlap_b(tid):
                return _overlap_with_fused(b_by_tid[tid], both, gid)
            a_tid = max(cand_a, key=_overlap_a) if cand_a else None
            b_tid = max(list(b_by_tid.keys()), key=_overlap_b) if b_by_tid else None
            if a_tid:
                a_by_gid[gid] = a_tid
            if b_tid:
                b_by_gid[gid] = b_tid

    for _, row in both.iterrows():
        gid = row["global_id"]
        t = float(row["timestamp"])
        a_tid = a_by_gid.get(gid)
        b_tid = b_by_gid.get(gid)
        if a_tid is None or b_tid is None:
            continue
        a_df = a_by_tid[a_tid]
        b_df = b_by_tid[b_tid]
        if a_df.empty or b_df.empty:
            continue
        # Nearest neighbour
        a_t = a_df.index.values
        b_t = b_df.index.values
        ia = int(np.argmin(np.abs(a_t - t)))
        ib = int(np.argmin(np.abs(b_t - t)))
        if abs(a_t[ia] - t) > 0.25 or abs(b_t[ib] - t) > 0.25:
            continue
        xa = float(a_df.iloc[ia]["x"]); ya = float(a_df.iloc[ia]["y"])
        xb = float(b_df.iloc[ib]["x"]); yb = float(b_df.iloc[ib]["y"])
        # Reference = A (the real sonar)
        pos_err_b_vs_a.append(math.hypot(xb - xa, yb - ya))
        pos_err_fused_vs_a.append(math.hypot(row["x_fused"] - xa, row["y_fused"] - ya))

    if pos_err_b_vs_a:
        out["B_vs_A_median_err_px"] = round(float(np.median(pos_err_b_vs_a)), 2)
        out["fused_vs_A_median_err_px"] = round(float(np.median(pos_err_fused_vs_a)), 2)
        # Information gain: how much closer is the fused estimate to A than B is?
        # (Lower fused error = more information gained.)
        out["information_gain_px"] = round(
            float(np.median(pos_err_b_vs_a) - np.median(pos_err_fused_vs_a)), 2
        )

    # Covariance shrinkage
    if "Pxx_fused" in both.columns:
        a_P = []
        f_P = []
        for _, row in both.iterrows():
            gid = row["global_id"]; t = float(row["timestamp"])
            a_tid = a_by_gid.get(gid)
            if a_tid is None:
                continue
            a_df = a_by_tid[a_tid]
            if a_df.empty:
                continue
            a_t = a_df.index.values
            ia = int(np.argmin(np.abs(a_t - t)))
            if abs(a_t[ia] - t) > 0.25:
                continue
            a_P.append(float(a_df.iloc[ia]["Pxx"]))
            f_P.append(float(row["Pxx_fused"]))
        if a_P:
            out["mean_Pxx_A"] = round(float(np.mean(a_P)), 3)
            out["mean_Pxx_fused"] = round(float(np.mean(f_P)), 3)
            out["Pxx_shrinkage_ratio"] = round(float(np.mean(f_P) / np.mean(a_P)), 3)

    return out


def _overlap_with_fused(local_df: pd.DataFrame, both: pd.DataFrame, gid: str) -> int:
    """Count how many BOTH_MEASURED timestamps in `gid` are within 0.25s of any
    timestamp in `local_df`.  Used to pick the right local_track_id per GID."""
    if local_df.empty:
        return 0
    sub = both[both["global_id"] == gid]
    if sub.empty:
        return 0
    lt = local_df.index.values
    count = 0
    for t in sub["timestamp"].values:
        if np.min(np.abs(lt - t)) < 0.25:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Top-level convenience: run all four evaluations
# ---------------------------------------------------------------------------
@dataclass
class EvalPaths:
    """All paths that the evaluator needs for one dataset."""
    tracks_raw_csv: Path
    track_summary_csv: Path
    standardized_csv: Optional[Path] = None
    sonar_a_meas_csv: Path = field(default_factory=lambda: Path("."))
    sonar_b_meas_csv: Path = field(default_factory=lambda: Path("."))
    aligned_pairs_csv: Path = field(default_factory=lambda: Path("."))
    association_results_csv: Path = field(default_factory=lambda: Path("."))
    local_to_global_csv: Path = field(default_factory=lambda: Path("."))
    fused_csv: Path = field(default_factory=lambda: Path("."))
    tr_a_csv: Path = field(default_factory=lambda: Path("."))
    tr_b_csv: Path = field(default_factory=lambda: Path("."))
    dfki_set_num: Optional[int] = None
    dfki_root: Optional[Path] = None
    bridge_translation_x_px: float = 25.0
    bridge_translation_y_px: float = 0.0


def run_full_evaluation(paths: EvalPaths, out_dir: Path) -> Dict:
    """Run all four evaluations and write a JSON + Markdown report."""
    out_dir.mkdir(parents=True, exist_ok=True)

    tracking = evaluate_tracking_quality(
        paths.tracks_raw_csv, paths.track_summary_csv,
        dfki_set_num=paths.dfki_set_num, dfki_root=paths.dfki_root,
        standardized_csv=paths.standardized_csv,
    )
    sufficiency = evaluate_data_sufficiency(
        paths.sonar_a_meas_csv, paths.sonar_b_meas_csv, paths.aligned_pairs_csv,
        bridge_translation_x_px=paths.bridge_translation_x_px,
        bridge_translation_y_px=paths.bridge_translation_y_px,
    )
    assoc = evaluate_association_quality(
        paths.association_results_csv, paths.local_to_global_csv,
        paths.tr_a_csv, paths.tr_b_csv,
    )
    fusion = evaluate_fusion_quality(
        paths.fused_csv, paths.tr_a_csv, paths.tr_b_csv,
    )

    summary = {
        "tracking": tracking,
        "data_sufficiency": sufficiency,
        "association": assoc,
        "fusion": fusion,
    }

    def _json_default(o):
        if isinstance(o, float) and math.isnan(o):
            return None
        if isinstance(o, (np.floating,)):
            v = float(o)
            return None if math.isnan(v) else v
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.ndarray,)):
            return o.tolist()
        return str(o)

    (out_dir / "evaluation.json").write_text(
        json.dumps(summary, indent=2, default=_json_default, allow_nan=False)
    )
    _write_markdown_report(summary, out_dir / "evaluation.md")
    return summary


def _write_markdown_report(summary: Dict, out_path: Path) -> None:
    lines = ["# E2E Evaluation Report", ""]
    t = summary["tracking"]
    s = summary["data_sufficiency"]
    a = summary["association"]
    f = summary["fusion"]

    # ---- Tracking ----
    lines += ["## 1. Tracking Quality (single-sonar)", ""]
    if t.get("per_track"):
        lines += ["| track_id | hits | duration_s | quality | predicted_ratio | mean_speed_mps | net_disp_m | Kalman RMSE (u/v) | primary? |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in t["per_track"]:
            lines.append(
                f"| `{r['track_id']}` | {r['hits']} | {r['duration_s']:.1f} | "
                f"{r['quality_score']:.3f} | {r['predicted_ratio']:.2f} | "
                f"{r['mean_speed_mps']:.2f} | {r['net_displacement_m']:.1f} | "
                f"{r['kalman_rmse_u_m']:.2f}/{r['kalman_rmse_v_m']:.2f} | "
                f"{'✓' if r['is_primary'] else ''} |"
            )
    if t.get("primary_vs_gt"):
        g = t["primary_vs_gt"]
        if "rmse_px" in g:
            lines += ["", "### PRIMARY track vs DFKI ground truth", "",
                      f"- matched frames: **{g['n_matched_frames']}** / {g['n_gt_frames']} GT, "
                      f"{g['n_track_frames']} track rows",
                      f"- coverage: **{g['coverage']:.1%}**",
                      f"- position RMSE: **{g['rmse_px']:.2f} px** "
                      f"(median {g['median_err_px']:.2f}, p90 {g['p90_err_px']:.2f}, "
                      f"max {g['max_err_px']:.2f})",
                      f"- mean bias (Δx, Δy) = ({g['mean_dx_px']:+.2f}, {g['mean_dy_px']:+.2f}) px"]
        elif "note" in g:
            lines += ["", f"### PRIMARY vs GT: {g['note']}"]

    # ---- Data sufficiency ----
    lines += ["", "## 2. Data Sufficiency & B-Synthesis Quality", ""]
    lines += [f"- Sonar A frames: **{s['n_A_frames']}**",
              f"- Sonar B frames (synthesized): **{s['n_B_frames']}**",
              f"- Aligned pairs: **{s['n_aligned_pairs']}** "
              f"({s['alignment_rate_vs_A']:.1%} of A, {s['alignment_rate_vs_B']:.1%} of B)",
              f"- Median A↔B position distance: **{s['median_A_to_B_distance_px']:.2f}** "
              f"(expected ≈ {s['expected_translation_distance_px']:.2f})"]
    if s.get("synthesis_ok"):
        lines += [f"- ✅ B-synthesis parameters honored (Δ={s['synthesis_deviation_px']:.2f})"]
    else:
        lines += [f"- ⚠️ B-synthesis deviates from expected translation by {s['synthesis_deviation_px']:.2f}"]

    # ---- Association ----
    lines += ["", "## 3. Association Quality (vs synthetic GT)", ""]
    lines += [f"- A local tracks: {a['n_A_local_tracks']}, B local tracks: {a['n_B_local_tracks']}",
              f"- Synthetic GT: `{a['synthetic_gt_a_primary']}` ↔ `{a['synthetic_gt_b_primary']}`",
              f"- Predicted associations: {a['n_predicted_associations']}",
              f"- True positives: **{a['n_true_positive']}**, "
              f"false positives: {a['n_false_positive']}, "
              f"false negatives: {a['n_false_negative']}",
              f"- **Precision: {a['association_precision']:.3f}** &nbsp; "
              f"**Recall: {a['association_recall']:.3f}** &nbsp; "
              f"**F1: {a['association_f1']:.3f}**",
              f"- ID switches: {a['id_switches']}",
              f"- Mean association cost: {a['mean_association_cost']}"]
    if a['mean_association_cost'] is not None:
        verdict = "✅" if a['association_f1'] >= 0.8 else "⚠️"
        lines += [f"- {verdict} Association verdict: "
                  f"{'PASS' if a['association_f1'] >= 0.8 else 'NEEDS REVIEW'}"]

    # ---- Fusion ----
    lines += ["", "## 4. Fusion Quality (BOTH_MEASURED frames)", ""]
    _mc = f.get("fusion_mode_counts") or {}
    if _mc:
        # Render EVERY fusion mode found in the data, so a mode that is
        # normally rare (e.g. BOTH_PREDICTED, NONE) can never be silently
        # dropped from the report.
        lines += ["| fusion mode | frames |", "|---|---|"]
        for _mode in sorted(_mc):
            lines.append(f"| `{_mode}` | {int(_mc[_mode])} |")
        _sum = int(sum(int(v) for v in _mc.values()))
        _tot = int(f.get("n_fused_total", _sum))
        _ok = f.get("mode_counts_conserved", _sum == _tot)
        lines += [f"| **total** | **{_tot}** |", ""]
        lines += [f"- conservation check: modes sum to **{_sum}**, fused rows "
                  f"**{_tot}** → {'✅ consistent' if _ok else '❌ MISMATCH'}"]
        if "n_valid" in f and _tot != int(f["n_valid"]):
            lines.append(f"- note: {_tot - int(f['n_valid'])} row(s) are "
                         f"`is_valid = False` and carry NaN positions.")
        lines += [""]
    lines += [f"- BOTH_MEASURED frames: {f.get('n_both_measured', 0)}",
              f"- BOTH_PREDICTED frames: {f.get('n_both_predicted', 0)}",
              f"- A_ONLY frames: {f.get('n_A_only', 0)}",
              f"- B_ONLY frames: {f.get('n_B_only', 0)}"]
    if "B_vs_A_median_err_px" in f:
        lines += [f"- B vs A median error: **{f['B_vs_A_median_err_px']:.2f} px**",
                  f"- Fused vs A median error: **{f['fused_vs_A_median_err_px']:.2f} px**",
                  f"- Information gain (B - fused): **{f['information_gain_px']:.2f} px**"]
    if "mean_Pxx_A" in f:
        lines += [f"- A covariance (Pxx): {f['mean_Pxx_A']:.2f} → "
                  f"fused (Pxx): {f['mean_Pxx_fused']:.2f} "
                  f"(shrinkage ratio {f['Pxx_shrinkage_ratio']:.2f})"]
    lines.append("")
    out_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="E2E evaluation: tracking + association + fusion")
    ap.add_argument("--out-dir", type=Path, required=True,
                    help="Dataset directory produced by run_e2e_pipeline "
                         "(must contain stage_2_tracking/, stage_4_local_tracks/, ...)")
    ap.add_argument("--dfki-set-num", type=int, default=None)
    ap.add_argument("--dfki-root", type=Path, default=DFKI_ROOT)
    ap.add_argument("--bridge-translation-x", type=float, default=25.0)
    args = ap.parse_args()

    out = args.out_dir
    p = EvalPaths(
        tracks_raw_csv=_pick(out, "stage_2_tracking/dfki_set_*/tracks_raw.csv",
                             "stage_2_tracking/02_tracking/tracks_raw.csv"),
        track_summary_csv=_pick(out, "stage_2_tracking/dfki_set_*/track_summary_all.csv",
                                "stage_2_tracking/02_tracking/track_summary_all.csv"),
        sonar_a_meas_csv=out / "stage_4_local_tracks" / "sonar_A_measurements.csv",
        sonar_b_meas_csv=out / "stage_4_local_tracks" / "sonar_B_measurements.csv",
        aligned_pairs_csv=out / "stage_4_local_tracks" / "stage_5_association" / "aligned_track_pairs.csv",
        association_results_csv=out / "stage_4_local_tracks" / "stage_5_association" / "association_results.csv",
        local_to_global_csv=out / "stage_4_local_tracks" / "stage_5_association" / "local_to_global.csv",
        fused_csv=out / "stage_4_local_tracks" / "stage_6_fusion" / "fused_global_tracks.csv",
        tr_a_csv=out / "stage_4_local_tracks" / "sonar_A_tracks.csv",
        tr_b_csv=out / "stage_4_local_tracks" / "sonar_B_tracks.csv",
        dfki_set_num=args.dfki_set_num,
        dfki_root=args.dfki_root,
        bridge_translation_x_px=args.bridge_translation_x,
    )
    res = run_full_evaluation(p, out / "stage_8_evaluation")
    print(json.dumps(res, indent=2, default=str))


def _pick(out: Path, *patterns: str) -> Path:
    """Find the first existing file matching any of the glob patterns."""
    for pat in patterns:
        for p in out.glob(pat):
            if p.exists():
                return p
    # Return the first pattern's first glob (may not exist) so the caller
    # gets a sensible error message
    return out / patterns[0]


if __name__ == "__main__":
    main()
