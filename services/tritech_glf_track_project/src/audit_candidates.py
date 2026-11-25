"""
audit_candidates.py — Quantify the noise / distractor burden in the
single-sonar candidate output.

Inputs (defaults assume dfki_set_4 in /root/outputs_e2e/):
    --detections : detections.csv from run_dfki.py
    --tracks     : tracks_raw.csv from run_dfki.py
    --gt         : gt_evaluation.csv
    --out-dir    : directory to write the diagnostic CSVs / summary

Reports per-frame:
    * total detection count
    * unmatched-to-track count (pure noise)
    * how many detections fall within 25 px of the GT (truth-anchored)
    * how many detections sit in the near-field clutter band v < 175
    * how many detections sit at high range (r > 400) - likely far-field
      distractors
    * the GT / track distance per frame (for spotting outlier frames)

For each detection we also classify it into one of:
    matched       — joined to the PRIMARY track
    near_gt       — within 25 px of GT but unmatched (rejected candidate)
    near_field    — v < 175 (platform-clutter band)
    far_field     — range > 400 (distractor)
    orphan        — any other unmatched detection
"""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd
from tritech_paths import LEGACY_E2E_ALL_ROOT, LEGACY_E2E_ROOT

GT_RADIUS_PX = 25.0
# v<175 (image y) and range<175 (Euclidean from sonar) are different.
# In the DFKI fan geometry, range_m = sqrt(dx² + dy²) where (cx,cy) is the
# sonar origin.  range_m < 175 is the true near-field (close to sonar).
# v (fan_v_m) is the image y-coordinate; a small v means close to the
# bottom edge (small y in image = close to the sonar head), but in this
# dataset v<175 blobs are in the LOWER half of the fan at the SIDES (u
# far from cx), so their Euclidean range is actually > 300 px.  Treat
# range_m as the canonical "near-field" axis.
NEAR_FIELD_RANGE_PX = 175.0   # Euclidean range from sonar — true near-field
NEAR_FIELD_V_PIX = 175.0      # legacy: v in image y — used to be "platform clutter"
FAR_FIELD_R = 400.0


def load_three(det_path: Path, trk_path: Path, gt_path: Path):
    d = pd.read_csv(det_path)
    t = pd.read_csv(trk_path)
    g = pd.read_csv(gt_path)
    # join track to det on frame_id to find GT-matched detections
    if "track_id" in d.columns and "track_id" in t.columns:
        t = t[["frame_id", "track_id", "fan_u_meas_m", "fan_v_meas_m"]].copy()
        t = t.rename(columns={"fan_u_meas_m": "track_x", "fan_v_meas_m": "track_y"})
        d = d.merge(t, on=["frame_id", "track_id"], how="left")
    elif "track_id" in d.columns:
        d["track_x"] = np.nan
        d["track_y"] = np.nan
    else:
        d["track_x"] = np.nan
        d["track_y"] = np.nan

    if "gt_x" not in g.columns:
        raise SystemExit(f"GT file lacks gt_x/gt_y cols: {gt_path}")
    g = g[["frame_id", "gt_x", "gt_y"]].dropna().drop_duplicates("frame_id")
    d = d.merge(g, on="frame_id", how="left")
    # distance from detection to GT and to its own track
    d["dist_to_gt"] = np.sqrt(
        (d["fan_u_m"] - d["gt_x"]) ** 2 + (d["fan_v_m"] - d["gt_y"]) ** 2
    )
    d["dist_to_track"] = np.sqrt(
        (d["fan_u_m"] - d["track_x"]) ** 2 + (d["fan_v_m"] - d["track_y"]) ** 2
    )
    return d


def classify(row) -> str:
    if row["matched_kftrack_id"] != -1 and not pd.isna(row["track_x"]):
        return "matched"
    if pd.notna(row["gt_x"]) and row["dist_to_gt"] < GT_RADIUS_PX:
        return "near_gt"
    # TRUE near-field: Euclidean range from sonar < 175 px.  In the DFKI
    # fan this is the platform-clutter band.
    if row["range_m"] < NEAR_FIELD_RANGE_PX:
        return "near_field"
    if row["range_m"] > FAR_FIELD_R:
        return "far_field"
    return "orphan"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detections", type=Path,
                    default=LEGACY_E2E_ROOT / "dfki_set_4" / "stage_2_tracking" / "dfki_set_4" / "detections.csv")
    ap.add_argument("--tracks", type=Path,
                    default=LEGACY_E2E_ROOT / "dfki_set_4" / "stage_2_tracking" / "dfki_set_4" / "tracks_raw.csv")
    ap.add_argument("--gt", type=Path,
                    default=LEGACY_E2E_ALL_ROOT / "dfki_set_4" / "gt_evaluation.csv")
    ap.add_argument("--out-dir", type=Path,
                    default=LEGACY_E2E_ROOT / "dfki_set_4" / "audit")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = load_three(args.detections, args.tracks, args.gt)
    df["bucket"] = df.apply(classify, axis=1)

    # ---------- Per-frame summary ----------
    per_frame = df.groupby("frame_id").agg(
        n_dets=("detection_id", "count"),
        n_matched=("bucket", lambda s: (s == "matched").sum()),
        n_near_gt=("bucket", lambda s: (s == "near_gt").sum()),
        n_near_field=("bucket", lambda s: (s == "near_field").sum()),
        n_far_field=("bucket", lambda s: (s == "far_field").sum()),
        n_orphan=("bucket", lambda s: (s == "orphan").sum()),
        track_x=("track_x", "first"),
        track_y=("track_y", "first"),
        gt_x=("gt_x", "first"),
        gt_y=("gt_y", "first"),
    )
    per_frame["track_gt_err"] = np.sqrt(
        (per_frame["track_x"] - per_frame["gt_x"]) ** 2
        + (per_frame["track_y"] - per_frame["gt_y"]) ** 2
    )
    per_frame = per_frame.reset_index()
    per_frame.to_csv(args.out_dir / "per_frame_audit.csv", index=False)

    # ---------- Overall classification counts ----------
    overall = df["bucket"].value_counts().rename_axis("bucket").reset_index(name="count")
    overall["pct"] = (overall["count"] / overall["count"].sum() * 100).round(1)
    overall.to_csv(args.out_dir / "bucket_counts.csv", index=False)

    # ---------- Print summary ----------
    print("=" * 70)
    print(f"Detections audit on {args.detections}")
    print("=" * 70)
    print(f"Total detections:        {len(df):>6d}")
    print(f"Frames with dets:        {df['frame_id'].nunique():>6d}")
    print()
    print("Per-frame detection count:")
    pf = per_frame["n_dets"]
    print(f"  mean={pf.mean():.2f}  median={pf.median():.0f}  max={pf.max()}  "
          f"min={pf.min()}  std={pf.std():.2f}")
    print(f"  Constant (max==min): {(pf.max() == pf.min())}")
    print()
    print("Bucket distribution (how many of the 2952 candidates fall in each class):")
    for _, r in overall.iterrows():
        print(f"  {r['bucket']:>12s}: {int(r['count']):>5d}  ({r['pct']:>5.1f}%)")
    print()
    # Frames with the most distractors
    print("Top-10 'noisy' frames (most distractor-class detections):")
    noisy = per_frame.sort_values(
        ["n_near_field", "n_far_field", "n_orphan"], ascending=False
    ).head(10)
    cols = ["frame_id", "n_dets", "n_matched", "n_near_gt", "n_near_field",
            "n_far_field", "n_orphan", "track_gt_err"]
    print(noisy[cols].to_string(index=False))
    print()
    # Frames where the track was way off but a near_gt detection existed
    recoverable = per_frame[(per_frame["track_gt_err"] > 25) & (per_frame["n_near_gt"] > 0)]
    print(f"Frames where the track was >25px off but a near-gt detection existed: {len(recoverable)}")
    if len(recoverable) > 0:
        print(recoverable[cols].head(10).to_string(index=False))
    print()
    # Track error summary
    errs = per_frame["track_gt_err"].dropna()
    print(f"Track-vs-GT error:  mean={errs.mean():.2f}  median={errs.median():.2f}  "
          f"p90={errs.quantile(0.9):.2f}  max={errs.max():.2f}")
    print()

    # JSON dump
    summary = {
        "detection_file": str(args.detections),
        "n_detections": int(len(df)),
        "n_frames": int(df["frame_id"].nunique()),
        "dets_per_frame": {
            "mean": float(pf.mean()), "median": float(pf.median()),
            "max": int(pf.max()), "min": int(pf.min()),
            "constant_topk": bool(pf.max() == pf.min()),
        },
        "bucket_distribution": {
            r["bucket"]: int(r["count"]) for _, r in overall.iterrows()
        },
        "track_gt_err": {
            "mean": float(errs.mean()),
            "median": float(errs.median()),
            "p90": float(errs.quantile(0.9)),
            "max": float(errs.max()),
        },
        "recoverable_frames_count": int(len(recoverable)),
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"Wrote {args.out_dir}/per_frame_audit.csv")
    print(f"Wrote {args.out_dir}/bucket_counts.csv")
    print(f"Wrote {args.out_dir}/summary.json")


if __name__ == "__main__":
    main()
