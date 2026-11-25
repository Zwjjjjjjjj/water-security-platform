#!/usr/bin/env python3
"""
Error budget decomposition for DFKI diver tracking.

Decomposes total tracking error into 5 layers:
  1. best_candidate_error   — best detection available vs GT (detector quality)
  2. selected_measurement_error — what the tracker actually associated vs GT
  3. track_estimate_error    — Kalman smoothed estimate vs GT (filtering quality)
  4. longest_coverage        — frames with continuous track coverage
  5. id_switches / fragments — trajectory fragmentation

Usage:
  python3 -m src.error_budget --out /root/dfki_output_v5
"""

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tritech_paths import DFKI_OUTPUT_V5
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


SETS = {
    1: "dfki_set_1",
    2: "dfki_set_2",
    3: "dfki_set_3",
    4: "dfki_set_4",
}


def load_data(out_dir: Path, set_name: str):
    gt_csv  = out_dir / set_name / "gt_evaluation.csv"
    det_csv = out_dir / set_name / "detections.csv"
    trk_csv = out_dir / set_name / "tracks_raw.csv"

    gt  = pd.read_csv(gt_csv)
    det = pd.read_csv(det_csv)
    trk = pd.read_csv(trk_csv)
    return gt, det, trk


def compute_error_budget(gt: pd.DataFrame, det: pd.DataFrame, trk: pd.DataFrame,
                         out_dir: Path, set_name: str):
    """
    Returns a dict of per-frame metrics, keyed by (frame_id, image_name).
    """
    # Build GT dict
    gt_dict = {}
    for _, row in gt.drop_duplicates("image_name").iterrows():
        gt_dict[str(row.image_name)] = (float(row.gt_x), float(row.gt_y))

    # --- Layer 1: best-candidate error per frame ---
    # For each GT frame: find ALL detections and compute distance to GT
    best_candidate = {}
    for img_name, (gx, gy) in gt_dict.items():
        frame_dets = det[det["image_name"] == img_name]
        if len(frame_dets) == 0:
            best_candidate[img_name] = {"has_det": False, "best_err": None,
                                         "n_dets": 0, "gx": gx, "gy": gy}
            continue
        dists = np.array([math.hypot(r.fan_u_m - gx, r.fan_v_m - gy)
                          for _, r in frame_dets.iterrows()])
        best_candidate[img_name] = {
            "has_det": True,
            "best_err": float(dists.min()),
            "best_idx": int(dists.argmin()),
            "n_dets": len(dists),
            "gx": gx, "gy": gy,
        }

    # --- Layer 2: selected-measurement error per frame ---
    ma_col = trk["measurement_available"]
    if ma_col.dtype == object:
        ma_col = ma_col.map(
            {"True": True, "False": False, True: True, False: False})
    measured = trk[ma_col == True].copy()

    # Pick primary track per frame: highest cumulative hits up to this frame.
    measured = measured.sort_values(["frame_id", "track_id"])
    measured["cum_hits"] = measured.groupby("track_id").cumcount() + 1
    measured_sorted = measured.sort_values(
        ["image_name", "cum_hits", "track_id"],
        ascending=[True, False, True]
    )
    primary = measured_sorted.drop_duplicates(subset="image_name", keep="first")

    selected_meas = {}
    for _, row in primary.iterrows():
        img_name = str(row.image_name)
        if img_name not in gt_dict:
            continue
        gx, gy = gt_dict[img_name]
        tx = float(row.fan_u_meas_m)
        ty = float(row.fan_v_meas_m)
        err = math.hypot(tx - gx, ty - gy)
        selected_meas[(int(row.frame_id), img_name)] = {
            "track_id": str(row.track_id),
            "meas_err": err,
            "gx": gx, "gy": gy,
            "tx": tx, "ty": ty,
        }

    # --- Layer 3: track-estimate (prediction/smoothed) error per frame ---
    est_rows = []
    for _, row in primary.iterrows():
        img_name = str(row.image_name)
        if img_name not in gt_dict:
            continue
        gx, gy = gt_dict[img_name]
        ex = float(row.fan_u_pred_m)
        ey = float(row.fan_v_pred_m)
        err = math.hypot(ex - gx, ey - gy)
        est_rows.append({
            "frame_id": int(row.frame_id),
            "image_name": img_name,
            "track_id": str(row.track_id),
            "est_err": err,
            "meas_err": math.hypot(float(row.fan_u_meas_m) - gx,
                                   float(row.fan_v_meas_m) - gy),
        })

    est_df = pd.DataFrame(est_rows)

    # --- Longest continuous coverage ---
    # Group by track_id, find longest consecutive frame run
    if len(measured) > 0:
        measured = measured.sort_values(["track_id", "frame_id"])
        measured["frame_diff"] = measured.groupby("track_id")["frame_id"].diff()
        measured["segment_id"] = (measured["frame_diff"] > 1).cumsum()

        seg_lengths = (
            measured.groupby(["track_id", "segment_id"])["frame_id"]
            .count()
            .reset_index(name="length")
        )
        longest_run = int(seg_lengths["length"].max())
        total_tracked_frames = measured["image_name"].nunique()
    else:
        longest_run = 0
        total_tracked_frames = 0

    # --- ID switches: count track_id changes within 5 frames of each other ---
    id_switches = 0
    if len(measured) > 1:
        measured_sorted = measured.sort_values("frame_id")
        prev_tid = None
        prev_fid = -999
        for _, row in measured_sorted.iterrows():
            fid = int(row.frame_id)
            tid = str(row.track_id)
            if prev_tid is not None:
                if fid - prev_fid <= 5 and tid != prev_tid:
                    id_switches += 1
            prev_tid = tid
            prev_fid = fid

    # --- Fragment count ---
    frag_csv = (out_dir / set_name / "track_summary_all.csv")
    if frag_csv.exists():
        summary = pd.read_csv(frag_csv)
        total_tracks = len(summary)
        # A track is "fragment" if it has < 20 measurement rows
        fragments = int((summary["measurement_rows"] < 20).sum())
    else:
        total_tracks = 0
        fragments = 0

    # --- Per-frame summary ---
    frame_metrics = []
    for img_name, (gx, gy) in gt_dict.items():
        fid = None
        m_row = measured[measured["image_name"] == img_name]
        if len(m_row) > 0:
            fid = int(m_row.iloc[0]["frame_id"])

        bc = best_candidate.get(img_name, {})
        sm_key = (fid, img_name) if fid is not None else None
        sm = selected_meas.get(sm_key, {}) if sm_key else {}
        er = est_df[est_df["image_name"] == img_name]

        frame_metrics.append({
            "image_name": img_name,
            "frame_id": fid,
            "gx": gx, "gy": gy,
            # Layer 1
            "best_cand_has_det": bc.get("has_det", False),
            "best_cand_err": bc.get("best_err"),
            "n_dets": bc.get("n_dets", 0),
            # Layer 2
            "sel_track_id": sm.get("track_id"),
            "meas_err": sm.get("meas_err"),
            # Layer 3
            "est_err": er["est_err"].iloc[0] if len(er) > 0 else None,
        })

    fm_df = pd.DataFrame(frame_metrics)

    # Summaries for each layer
    has_best = fm_df["best_cand_err"].notna()
    has_meas = fm_df["meas_err"].notna()
    has_est  = fm_df["est_err"].notna()

    def _stats(col):
        v = col.dropna()
        if len(v) == 0:
            return None, None, None, None
        return float(v.mean()), float(v.median()), float(v.quantile(0.9)), float(v.max())

    bc_mean, bc_med, bc_p90, bc_max = _stats(fm_df["best_cand_err"])
    sm_mean, sm_med, sm_p90, sm_max = _stats(fm_df["meas_err"])
    es_mean, es_med, es_p90, es_max = _stats(fm_df["est_err"])

    # How much does Kalman add beyond measurement?
    meas_err_vals  = fm_df["meas_err"].dropna().values
    est_err_vals   = fm_df["est_err"].dropna().values
    if len(meas_err_vals) and len(est_err_vals):
        kalman_addition = float(np.median(est_err_vals) - np.median(meas_err_vals))
    else:
        kalman_addition = None

    # Association error: measurement error vs best-candidate error
    assoc_err = None
    if bc_med is not None and sm_med is not None:
        assoc_err = sm_med - bc_med

    return {
        "n_gt_frames": len(gt_dict),
        # Layer 1: best detection available
        "layer1_best_candidate": {
            "n_frames_with_det": int(has_best.sum()),
            "detection_rate": round(float(has_best.sum() / len(gt_dict)), 4),
            "mean_err": round(bc_mean, 2) if bc_mean else None,
            "median_err": round(bc_med, 2) if bc_med else None,
            "p90_err": round(bc_p90, 2) if bc_p90 else None,
            "max_err": round(bc_max, 2) if bc_max else None,
        },
        # Layer 2: selected measurement
        "layer2_selected_measurement": {
            "n_frames_with_meas": int(has_meas.sum()),
            "mean_err": round(sm_mean, 2) if sm_mean else None,
            "median_err": round(sm_med, 2) if sm_med else None,
            "p90_err": round(sm_p90, 2) if sm_p90 else None,
            "max_err": round(sm_max, 2) if sm_max else None,
            "association_error_vs_best": round(assoc_err, 2) if assoc_err else None,
        },
        # Layer 3: Kalman estimate
        "layer3_track_estimate": {
            "n_frames_with_est": int(has_est.sum()),
            "mean_err": round(es_mean, 2) if es_mean else None,
            "median_err": round(es_med, 2) if es_med else None,
            "p90_err": round(es_p90, 2) if es_p90 else None,
            "max_err": round(es_max, 2) if es_max else None,
            "kalman_addition_median": round(kalman_addition, 2) if kalman_addition else None,
        },
        # Coverage
        "layer4_coverage": {
            "longest_continuous_run": longest_run,
            "total_tracked_frames": total_tracked_frames,
            "coverage_rate": round(total_tracked_frames / len(gt_dict), 4)
                                 if len(gt_dict) else None,
        },
        # Fragmentation
        "layer5_fragmentation": {
            "total_tracks": total_tracks,
            "fragments": fragments,
            "id_switches": id_switches,
        },
    }, fm_df


def print_budget(budget: dict):
    print("\n" + "=" * 70)
    print("ERROR BUDGET DECOMPOSITION")
    print("=" * 70)

    print("\n[L1] Best-Candidate Error (detector ceiling)")
    l1 = budget["layer1_best_candidate"]
    print(f"  Detection rate    : {l1['detection_rate']:.1%}")
    print(f"  Median error      : {l1['median_err']} px" if l1['median_err'] else "  Median error      : N/A")
    print(f"  P90 error         : {l1['p90_err']} px" if l1['p90_err'] else "  P90 error         : N/A")
    print(f"  Max error         : {l1['max_err']} px" if l1['max_err'] else "  Max error         : N/A")

    print("\n[L2] Selected-Measurement Error (association quality)")
    l2 = budget["layer2_selected_measurement"]
    print(f"  Median error      : {l2['median_err']} px" if l2['median_err'] else "  Median error      : N/A")
    print(f"  P90 error         : {l2['p90_err']} px" if l2['p90_err'] else "  P90 error         : N/A")
    if l2['association_error_vs_best'] is not None:
        print(f"  Assoc. overhead  : {l2['association_error_vs_best']:+.1f} px vs best-candidate")
    else:
        print(f"  Assoc. overhead   : N/A")

    print("\n[L3] Track-Estimate Error (Kalman smoothing)")
    l3 = budget["layer3_track_estimate"]
    print(f"  Median error      : {l3['median_err']} px" if l3['median_err'] else "  Median error      : N/A")
    print(f"  P90 error         : {l3['p90_err']} px" if l3['p90_err'] else "  P90 error         : N/A")
    if l3['kalman_addition_median'] is not None:
        print(f"  Kalman Δ vs meas. : {l3['kalman_addition_median']:+.1f} px")
    else:
        print(f"  Kalman Δ vs meas. : N/A")

    print("\n[L4] Coverage")
    l4 = budget["layer4_coverage"]
    print(f"  Longest run       : {l4['longest_continuous_run']} frames")
    print(f"  Total tracked     : {l4['total_tracked_frames']} / {budget['n_gt_frames']}")
    print(f"  Coverage rate     : {l4['coverage_rate']:.1%}" if l4['coverage_rate'] else "  Coverage rate     : N/A")

    print("\n[L5] Fragmentation")
    l5 = budget["layer5_fragmentation"]
    print(f"  Total tracks      : {l5['total_tracks']}")
    print(f"  Fragments (<20m)  : {l5['fragments']}")
    print(f"  ID switches       : {l5['id_switches']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DFKI_OUTPUT_V5), type=str)
    args = parser.parse_args()

    out_dir = Path(args.out)
    all_budgets = {}

    for set_idx, set_name in SETS.items():
        print(f"\n{'='*60}")
        print(f"  Processing {set_name} ...")
        print(f"{'='*60}")
        gt, det, trk = load_data(out_dir, set_name)
        budget, fm_df = compute_error_budget(gt, det, trk, out_dir, set_name)
        all_budgets[set_name] = budget

        print_budget(budget)

        # Save per-frame metrics
        fm_df.to_csv(out_dir / set_name / "error_budget_frames.csv", index=False)

        # Save budget JSON
        with open(out_dir / set_name / "error_budget_summary.json", "w") as f:
            json.dump(budget, f, indent=2)

    # Global summary
    print("\n" + "=" * 70)
    print("GLOBAL SUMMARY (4 sets)")
    print("=" * 70)

    layer1_meds = [b["layer1_best_candidate"]["median_err"]
                   for b in all_budgets.values() if b["layer1_best_candidate"]["median_err"]]
    layer2_meds = [b["layer2_selected_measurement"]["median_err"]
                   for b in all_budgets.values() if b["layer2_selected_measurement"]["median_err"]]
    layer3_meds = [b["layer3_track_estimate"]["median_err"]
                   for b in all_budgets.values() if b["layer3_track_estimate"]["median_err"]]
    assoc_over  = [b["layer2_selected_measurement"]["association_error_vs_best"]
                   for b in all_budgets.values() if b["layer2_selected_measurement"]["association_error_vs_best"] is not None]

    print(f"\n{'Set':<8} {'L1-best':>8} {'L2-sel':>8} {'L3-est':>8} {'Assoc Ovhead':>12} {'Longest':>8} {'Frag':>6} {'Switch':>8}")
    print("-" * 70)
    for set_name, b in all_budgets.items():
        l1m = f"{b['layer1_best_candidate']['median_err']:.1f}" if b['layer1_best_candidate']['median_err'] else "N/A"
        l2m = f"{b['layer2_selected_measurement']['median_err']:.1f}" if b['layer2_selected_measurement']['median_err'] else "N/A"
        l3m = f"{b['layer3_track_estimate']['median_err']:.1f}" if b['layer3_track_estimate']['median_err'] else "N/A"
        ao  = f"{b['layer2_selected_measurement']['association_error_vs_best']:.1f}" if b['layer2_selected_measurement']['association_error_vs_best'] else "N/A"
        lr  = f"{b['layer4_coverage']['longest_continuous_run']}"
        fg  = f"{b['layer5_fragmentation']['fragments']}"
        sw  = f"{b['layer5_fragmentation']['id_switches']}"
        print(f"{set_name:<8} {l1m:>8} {l2m:>8} {l3m:>8} {ao:>12} {lr:>8} {fg:>6} {sw:>8}")

    if layer1_meds:
        print(f"\n{'Avg':<8} {np.mean(layer1_meds):>8.1f} {np.mean(layer2_meds):>8.1f} {np.mean(layer3_meds):>8.1f} {np.mean(assoc_over):>12.1f}")
    print()


if __name__ == "__main__":
    main()
