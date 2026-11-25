"""
diagnose_nees.py — Find the P_scale that makes NEES well-calibrated.

Reads the existing fused_global_tracks.csv and GT, sweeps P_scale from 1 to 40,
reports mean/median NEES for each, and prints the recommended scale.

Usage:
    python diagnose_nees.py --dataset dfki_set_4
    python diagnose_nees.py --dataset dfki_set_4 --dfki-root /root/outputs_e2e_all
"""
import argparse, json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root")
DEFAULT_DFKI_ROOT = ROOT / "outputs_e2e_all"


def load_gt(dfki_set_num: int, dfki_root: Path) -> tuple:
    # Try multiple possible locations
    candidates = [
        dfki_root / f"dfki_set_{dfki_set_num}" / "gt_evaluation.csv",
        ROOT / "outputs_e2e_all" / f"dfki_set_{dfki_set_num}" / "gt_evaluation.csv",
        dfki_root / f"dfki_set_{dfki_set_num}" / f"tracking_sonar_coordinates_{dfki_set_num}.csv",
    ]
    for gt_path in candidates:
        if gt_path.exists():
            break
    else:
        raise FileNotFoundError(f"GT not found in any of: {candidates}")
    gt = pd.read_csv(gt_path)
    if "image_name" in gt.columns:
        # Extract timestamp from filename like "1650985704.015656233.jpg"
        gt["ts"] = pd.to_numeric(
            gt["image_name"].str.replace(".jpg", "", regex=False), errors="coerce"
        )
    elif "timestamp" in gt.columns:
        gt["ts"] = gt["timestamp"]
    else:
        raise ValueError(f"Cannot find timestamp column in {gt_path}")
    col_x = "gt_x" if "gt_x" in gt.columns else ("x" if "x" in gt.columns else None)
    col_y = "gt_y" if "gt_y" in gt.columns else ("y" if "y" in gt.columns else None)
    if col_x is None or col_y is None:
        raise ValueError(f"Cannot find x/y columns in {gt_path}: {gt.columns.tolist()}")
    gt = gt.dropna(subset=["ts", col_x, col_y]).sort_values("ts").reset_index(drop=True)
    return gt["ts"].to_numpy(), gt[col_x].to_numpy(), gt[col_y].to_numpy()


def compute_nees(fused_csv: Path, gt_t, gt_x, gt_y, p_scale: float, tol_s: float = 0.25) -> dict:
    fused = pd.read_csv(fused_csv)
    fused = fused[fused["is_valid"] == True].sort_values("timestamp")
    f_t = fused["timestamp"].to_numpy()
    f_x = fused["x_fused"].to_numpy()
    f_y = fused["y_fused"].to_numpy()
    f_Pxx = fused["Pxx_fused"].to_numpy() * p_scale
    f_Pyy = fused["Pyy_fused"].to_numpy() * p_scale

    n_re = []
    n = 0
    for i, t in enumerate(gt_t):
        d = np.abs(f_t - t)
        j = int(np.argmin(d))
        if d[j] > tol_s:
            continue
        Pxx = max(float(f_Pxx[j]), 1e-6)
        Pyy = max(float(f_Pyy[j]), 1e-6)
        ex = float(f_x[j] - gt_x[i])
        ey = float(f_y[j] - gt_y[i])
        n_re.append((ex * ex) / Pxx + (ey * ey) / Pyy)
        n += 1
    if not n_re:
        return {"available": False, "n": 0}
    n_re = np.array(n_re)
    mean_nees = float(n_re.mean())
    return {
        "available": True,
        "n_matched": int(n),
        "mean_NEES": round(mean_nees, 3),
        "median_NEES": round(float(np.median(n_re)), 3),
        "p95_NEES": round(float(np.percentile(n_re, 95)), 3),
        "verdict": (
            "over-confident" if mean_nees > 4.0
            else ("well-calibrated" if 0.5 <= mean_nees <= 4.0
                  else "under-confident")
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="e.g. dfki_set_4, glf, dfki_set_1")
    ap.add_argument("--dfki-root", type=Path, default=DEFAULT_DFKI_ROOT)
    ap.add_argument("--fused-csv", type=Path, default=None,
                    help="Override auto-path; otherwise derived from dataset")
    args = ap.parse_args()

    # Resolve fused CSV
    if args.fused_csv:
        fused_csv = args.fused_csv
    elif args.dataset.startswith("dfki_set_"):
        num = int(args.dataset.split("_")[-1])
        fused_csv = (
            args.dfki_root / args.dataset
            / "stage_4_local_tracks" / "stage_6_fusion"
            / "fused_global_tracks.csv"
        )
    elif args.dataset == "glf":
        fused_csv = (
            args.dfki_root / "glf"
            / "stage_4_local_tracks" / "stage_6_fusion"
            / "fused_global_tracks.csv"
        )
    else:
        raise ValueError(f"Unknown dataset: {args.dataset}")

    if not fused_csv.exists():
        print(f"ERROR: fused CSV not found: {fused_csv}", file=sys.stderr)
        sys.exit(1)

    # Load GT for dfki datasets
    if args.dataset.startswith("dfki_set_"):
        num = int(args.dataset.split("_")[-1])
        gt_t, gt_x, gt_y = load_gt(num, args.dfki_root)
        print(f"GT: {len(gt_t)} frames from {fused_csv}")
    else:
        # For glf: use A track as pseudo-GT
        a_csv = fused_csv.parents[1] / "sonar_A_tracks.csv"
        if not a_csv.exists():
            print(f"ERROR: A track not found for glf: {a_csv}", file=sys.stderr)
            sys.exit(1)
        a = pd.read_csv(a_csv)
        primary = a[a["local_track_id"].str.upper().str.contains("PRIMARY", na=False)]
        if primary.empty:
            primary = a
        ref = primary.sort_values("timestamp")
        gt_t = ref["timestamp"].to_numpy()
        gt_x = ref["x"].to_numpy()
        gt_y = ref["y"].to_numpy()
        print(f"Pseudo-GT (A track): {len(gt_t)} frames")

    # Sweep P_scale
    print(f"\n{'P_scale':>8} {'mean_NEES':>10} {'median_NEES':>12} {'verdict':>16}  {'n_matched':>10}")
    print("-" * 60)
    best = None
    for scale in [0.25, 0.5, 1.0, 2.0, 4.0, 7.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0]:
        r = compute_nees(fused_csv, gt_t, gt_x, gt_y, p_scale=scale)
        if not r["available"]:
            continue
        mark = "  <<<" if 0.5 <= r["mean_NEES"] <= 4.0 else ""
        print(f"{scale:>8.2f} {r['mean_NEES']:>10.3f} {r['median_NEES']:>12.3f} "
              f"{r['verdict']:>16}  {r['n_matched']:>10}{mark}")
        if 0.5 <= r["mean_NEES"] <= 4.0 and best is None:
            best = scale

    if best:
        print(f"\nRecommended P_scale: {best}  (brings NEES into [0.5, 4.0] range)")
    else:
        print("\nNo P_scale in [0.25, 50] brings NEES into well-calibrated range.")

    # Also print current (scale=1) stats
    r1 = compute_nees(fused_csv, gt_t, gt_x, gt_y, p_scale=1.0)
    print(f"\nCurrent (scale=1.0): mean_NEES={r1['mean_NEES']:.3f}  "
          f"median_NEES={r1['median_NEES']:.3f}")
    print(f"RMSE vs GT (not affected by scale): run tracking_accuracy.py for fused CSV")


if __name__ == "__main__":
    main()
