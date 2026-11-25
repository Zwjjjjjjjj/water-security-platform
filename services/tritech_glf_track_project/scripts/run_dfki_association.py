#!/usr/bin/env python3
"""Run many-to-many Hungarian association on all 4 DFKI simulated pairs.

Inputs (per set N):
  /root/Tritech_GLF_Track_Project/outputs/06_association/
      sonar_A_dfki_set_N_common.csv
      sonar_B_dfki_set_N_common.csv

Outputs (per set N):
      association_set_N_results.csv
      association_set_N_summary.csv
      global_summary_set_N.json

Final:
      all_sets_summary.csv   — combined metrics across all sets
"""

from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

from src.associate_tracks import associate_track_sets, DEFAULT_ASSOC_CFG


# Per-set gate configuration.
# The new (v2) simulation has realistic position differences (mean≈0.38m, p95≈0.75m).
# Mahalanobis gate: sigma^2 ≈ (NOISE_A.pos^2 + NOISE_B.pos^2) = (0.15^2+0.30^2) = 0.1125 → sqrt=0.335
#   With S = 2*var = 0.225, Mahalanobis threshold of 3.0 corresponds to 3*sigma ≈ 1.0m positional tolerance.
# Velocity gate: now computed as |pos_A - pos_B| / |dt| (not gradient-based), so the
#   gate should be set to a fraction of max_pos_diff / min_dt_tolerance ≈ 0.75 / 0.1 = 7.5 m/s.
CFG_PER_SET = {
    s: {**DEFAULT_ASSOC_CFG,
        "position_gate_m":    2.0,
        "velocity_gate_mps":  8.0,   # generous; vel = pos/dt, |dt|>=10ms always
        "mahalanobis_gate":   3.0,
        "position_variance":  0.1125,  # NOISE_A^2 + NOISE_B^2 ≈ (0.15)^2 + (0.30)^2
        }
    for s in [1, 2, 3, 4]
}


ASSOC_DIR = Path("/root/Tritech_GLF_Track_Project/outputs/06_association")


def run_one_set(set_num: int) -> dict:
    a_csv = ASSOC_DIR / f"sonar_A_dfki_set_{set_num}_common_aug.csv"
    b_csv = ASSOC_DIR / f"sonar_B_dfki_set_{set_num}_common_aug.csv"
    if not a_csv.exists() or not b_csv.exists():
        # fall back to non-augmented
        a_csv = ASSOC_DIR / f"sonar_A_dfki_set_{set_num}_common.csv"
        b_csv = ASSOC_DIR / f"sonar_B_dfki_set_{set_num}_common.csv"
    if not a_csv.exists() or not b_csv.exists():
        print(f"  set_{set_num}: missing inputs — skipping")
        return {}

    cfg  = CFG_PER_SET[set_num]
    set_dir = ASSOC_DIR / f"set_{set_num}"
    set_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  set_{set_num}: running many-to-many Hungarian association")
    print(f"    input: {a_csv.name}, {b_csv.name}")
    print(f"    pos_gate={cfg['position_gate_m']:.1f} m, "
          f"vel_gate={cfg['velocity_gate_mps']:.1f} m/s, "
          f"md_gate={cfg['mahalanobis_gate']:.2f}")

    pairs_df, summary_df, global_df = associate_track_sets(a_csv, b_csv, set_dir, cfg=cfg)

    # Save outputs
    pairs_df.to_csv(set_dir / f"association_set_{set_num}_results.csv", index=False)
    summary_df.to_csv(set_dir / f"association_set_{set_num}_summary.csv", index=False)
    global_df.to_csv(set_dir / f"global_summary_set_{set_num}.csv", index=False)

    n_pairs_tested  = int(global_df["n_pairs_tested"].iloc[0])
    n_pairs_accepted = int(global_df["n_pairs_accepted"].iloc[0])
    mean_pos        = float(global_df["mean_position_difference_m"].iloc[0])
    print(f"    pairs: {n_pairs_accepted}/{n_pairs_tested} accepted, "
          f"mean_pos_diff={mean_pos:.2f} m")

    return {
        "set": set_num,
        "n_A_tracks": int(global_df["n_A_tracks"].iloc[0]),
        "n_B_tracks": int(global_df["n_B_tracks"].iloc[0]),
        "n_pairs_tested": n_pairs_tested,
        "n_pairs_accepted": n_pairs_accepted,
        "pair_acceptance_rate": float(global_df["pair_acceptance_rate"].iloc[0]),
        "mean_position_difference_m": mean_pos,
        "mean_cost": float(global_df["mean_cost"].iloc[0]),
    }


def main():
    rows = []
    for s in [1, 2, 3, 4]:
        r = run_one_set(s)
        if r:
            rows.append(r)

    all_df = pd.DataFrame(rows)
    out = ASSOC_DIR / "all_sets_summary.csv"
    all_df.to_csv(out, index=False)

    print("\n" + "=" * 70)
    print("ALL SETS — ASSOCIATION SUMMARY")
    print("=" * 70)
    print(all_df.to_string(index=False))
    print(f"\n  Output: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
