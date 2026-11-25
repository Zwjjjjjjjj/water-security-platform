#!/usr/bin/env python3
"""Augment the simulated DFKI common-frame data with extra distractor tracks
on BOTH sides so the associator must reject false positives.

This turns each (A-track, B-track) test into a meaningful stress test:
- A-side: real DFKI track + 2 synthetic far-away distractors
- B-side: simulated track + 2 synthetic far-away distractors

The associator should accept exactly one pair (the true match) per set.
"""

from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd


ASSOC_DIR = Path("/root/Tritech_GLF_Track_Project/outputs/06_association")


def add_distractors(df_a: pd.DataFrame, df_b: pd.DataFrame, seed: int, distractor_count: int = 2):
    """Append `distractor_count` synthetic far-away tracks to BOTH sides.

    Distractor tracks are linear paths far from the real track so they
    should NEVER be accepted by the association gate.
    """
    rng = np.random.default_rng(seed)

    # Time grid from A
    t0  = pd.to_datetime(df_a["timestamp_utc"].iloc[0], utc=True)
    ta  = (pd.to_datetime(df_a["timestamp_utc"], utc=True) - t0).dt.total_seconds().to_numpy()
    tb  = (pd.to_datetime(df_b["timestamp_utc"], utc=True) - t0).dt.total_seconds().to_numpy()
    t_min, t_max = min(ta.min(), tb.min()), max(ta.max(), tb.max())

    # Two reference ranges — choose well outside the real track area
    real_x_min = float(min(df_a["common_x_m"].min(), df_b["common_x_m"].min()))
    real_x_max = float(max(df_a["common_x_m"].max(), df_b["common_x_m"].max()))
    real_y_min = float(min(df_a["common_y_m"].min(), df_b["common_y_m"].min()))
    real_y_max = float(max(df_a["common_y_m"].max(), df_b["common_y_m"].max()))
    real_cx    = (real_x_min + real_x_max) / 2
    real_cy    = (real_y_min + real_y_max) / 2
    real_range = max(real_x_max - real_x_min, real_y_max - real_y_min, 5.0)

    def _make_track(label_prefix: str, side: str, t_offset: float, base_xy: np.ndarray, vel: np.ndarray, tids: list):
        """Make one distractor track."""
        n_pts = int((t_max - t_min) / 0.2)
        if n_pts < 5:
            n_pts = 5
        ts_local = np.linspace(t_min, t_max, n_pts) + t_offset
        ts_utc   = [t0 + pd.to_timedelta(float(tt), unit="s") for tt in ts_local]
        # Add jitter
        ts_utc   = [t + pd.to_timedelta(float(rng.normal(0, 0.05)), unit="s") for t in ts_utc]
        # Linear path with small wiggle
        xy = base_xy + np.outer(ts_local - ts_local[0], vel) + rng.normal(0, 0.5, (n_pts, 2))
        track_id = tids[0]
        df_new = pd.DataFrame({
            "timestamp_utc": [t.isoformat().replace("+00:00", "Z") for t in ts_utc],
            "frame_id":       np.arange(n_pts),
            "sensor_id":      f"{side}",
            "track_id":       track_id,
            "coordinate_frame": "ASSOCIATION_COMMON_LOCAL",
            "common_x_m":     xy[:, 0],
            "common_y_m":     xy[:, 1],
            "common_vx_mps":  vel[0] * np.ones(n_pts),
            "common_vy_mps":  vel[1] * np.ones(n_pts),
            "Pxx_m2":         1.0, "Pxy_m2": 0.0, "Pyy_m2": 1.0,
            "Pvxvx_m2ps2":    0.25, "Pvyvy_m2ps2": 0.25,
            "is_simulated":   False,
        })
        return df_new

    # Distractor offsets: well outside the real path
    far_offsets = [
        np.array([ 3 * real_range,  3 * real_range]),
        np.array([-3 * real_range,  2 * real_range]),
    ][:distractor_count]

    aug_a = [df_a.copy()]
    aug_b = [df_b.copy()]
    for k, off in enumerate(far_offsets):
        base_xy = np.array([real_cx, real_cy]) + off
        vel_a = rng.normal(0, 0.3, 2)
        vel_b = rng.normal(0, 0.3, 2)
        tids_a = [f"A_DIST_{k+1:02d}"]
        tids_b = [f"B_DIST_{k+1:02d}"]
        aug_a.append(_make_track("A", "DFKI_REAL_SONAR_A", 0.0, base_xy, vel_a, tids_a))
        aug_b.append(_make_track("B", "SIM_SONAR_B",        0.0, base_xy + rng.normal(0, 5, 2), vel_b, tids_b))

    df_a_aug = pd.concat(aug_a, ignore_index=True)
    df_b_aug = pd.concat(aug_b, ignore_index=True)
    return df_a_aug, df_b_aug


def augment_one_set(set_num: int):
    a_csv = ASSOC_DIR / f"sonar_A_dfki_set_{set_num}_common.csv"
    b_csv = ASSOC_DIR / f"sonar_B_dfki_set_{set_num}_common.csv"
    if not a_csv.exists() or not b_csv.exists():
        print(f"  set_{set_num}: missing inputs")
        return

    df_a = pd.read_csv(a_csv)
    df_b = pd.read_csv(b_csv)
    print(f"\n  set_{set_num}: augmenting with 2 distractor tracks each side")
    print(f"    before: A={len(df_a)} rows ({df_a['track_id'].nunique()} tracks), "
          f"B={len(df_b)} rows ({df_b['track_id'].nunique()} tracks)")

    a_aug, b_aug = add_distractors(df_a, df_b, seed=set_num * 100, distractor_count=2)
    print(f"    after:  A={len(a_aug)} rows ({a_aug['track_id'].nunique()} tracks), "
          f"B={len(b_aug)} rows ({b_aug['track_id'].nunique()} tracks)")

    out_a = ASSOC_DIR / f"sonar_A_dfki_set_{set_num}_common_aug.csv"
    out_b = ASSOC_DIR / f"sonar_B_dfki_set_{set_num}_common_aug.csv"
    a_aug.to_csv(out_a, index=False)
    b_aug.to_csv(out_b, index=False)


def main():
    for s in [1, 2, 3, 4]:
        augment_one_set(s)
    print("\nDone. Use the *_aug.csv files in the next step.")


if __name__ == "__main__":
    main()
