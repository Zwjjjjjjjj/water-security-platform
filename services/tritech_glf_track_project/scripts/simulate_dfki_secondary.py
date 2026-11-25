#!/usr/bin/env python3
"""
Generate realistic simulated second-sonar common-frame observations from
DFKI v14 pixel-coordinate tracks.

This version (v2) models a realistic dual-sonar configuration by reducing
the previously exaggerated parameters:

| Parameter         | v1 (exaggerated) | v2 (realistic)        |
|-------------------|------------------|-----------------------|
| Translation       | 40-70 m          | 5-15 m                |
| Yaw               | +/-15-35 deg     | +/-10-25 deg          |
| Position noise    | 1.0 m RMS        | 0.25 m RMS            |
| Velocity noise    | 0.5 m/s RMS      | 0.10 m/s RMS          |
| Sampling rate     | 5 Hz             | 10 Hz                 |
| Time jitter       | 50 ms            | 10 ms                 |
| Drop model        | independent 10%  | burst 2-5 frames      |
| Occlusion         | none             | random A-side gap     |
| Covariance        | hardcoded        | matches noise sigma   |

Inputs
------
  /root/dfki_output_v14/dfki_set_N/tracks_raw.csv   (for each set 1..4)

Output
------
  /root/Tritech_GLF_Track_Project/outputs/06_association/
      sonar_A_dfki_set_N_common.csv        - Sonar A tracks in common frame
      sonar_B_dfki_set_N_common.csv        - simulated second-sonar observations
      simulation_parameters_set_N.json     - transform + noise config used
"""

from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, OUTPUT_ROOT

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# DFKI sonar pixels -> metres scale.
# Used ONLY for synthetic association evaluation; this is NOT a calibrated
# physical conversion (DFKI dataset has no metric calibration).
PIXEL_TO_METRE = 0.10   # 10 cm per pixel - typical for short-range sonar fan

# Per-set random seeds for reproducibility
SEEDS = {1: 11, 2: 22, 3: 33, 4: 44}

# Realistic per-set sonar-pair configurations:
#   translation_m: physical separation between the two sonar apertures (5-15 m).
#   yaw_deg:       relative orientation of B's boresight w.r.t. A's frame.
#                  0 means both sonars point the same way; +/-10-25 deg covers
#                  the typical mounting offsets seen on small AUVs/ROVs.
SIM_PARAMS = {
    1: {"translation_m": [ 8.0,  5.0], "yaw_deg":  15.0},
    2: {"translation_m": [12.0,  3.0], "yaw_deg": -18.0},
    3: {"translation_m": [ 6.0,  9.0], "yaw_deg":  22.0},
    4: {"translation_m": [14.0,  6.0], "yaw_deg": -12.0},
}

# Realistic per-sensor noise model (short-range mechanical scanning sonar)
# Slightly different noise scales for A (more precise) vs B (slightly noisier).
NOISE_A = {
    "position_std_m":   0.15,    # 15 cm RMS position error (A is the "reference")
    "velocity_std_mps": 0.08,    # 8 cm/s velocity noise
    "detection_rate":   0.98,    # A detects 98% of frames
}
NOISE_B = {
    "position_std_m":   0.30,    # 30 cm RMS position error (B is the simulated second)
    "velocity_std_mps": 0.12,    # 12 cm/s velocity noise
    "detection_rate":   0.90,    # B detects 90% of frames (more missing due to
                                  #                            different viewing angle)
}

# Sampling rates - both close to the original DFKI frame rate (~30 Hz max,
# but most detections are sparse). Use 10 Hz which is a realistic operating
# rate for a dual-sonar system that has been pre-filtered.
SAMPLING_RATE_HZ = 10.0
TIMESTAMP_JITTER_S = 0.010       # 10 ms - typical clock-sync residual

# Burst-drop model: instead of independent Bernoulli drops, drop consecutive
# frames in bursts of 2-5 to mimic real-world packet loss / occlusions.
# `burst_rate` is the probability that a burst-loss event starts at any given
# frame; `burst_lengths` is the typical burst length distribution.
BURST_RATE_PER_FRAME = 0.015     # ~1.5% chance per frame to start a burst
BURST_LENGTH_RANGE   = (2, 5)    # bursts drop 2..5 consecutive frames

# Occlusion: an additional contiguous gap of 1-3 seconds on the A side
# to simulate target momentarily being out of A's field-of-view but in B's.
OCCLUSION_GAP_RANGE_S = (1.0, 3.0)


DFKI_TRACKS_ROOT = DFKI_OUTPUT_V14
OUT_DIR          = OUTPUT_ROOT / "06_association"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pick_primary(df: pd.DataFrame) -> pd.DataFrame:
    """Pick the highest-quality track (PRIMARY_DIVER if present, else longest)."""
    primaries = df[df["track_id"] == "PRIMARY_DIVER"]
    if len(primaries) > 0:
        return primaries.copy()
    longest = (
        df.groupby("track_id")
          .size()
          .sort_values(ascending=False)
          .index[0]
    )
    return df[df["track_id"] == longest].copy()


def pixel_to_common(df: pd.DataFrame) -> pd.DataFrame:
    """Convert DFKI pixel coords (fan_u_pred, fan_v_pred) to common-frame metres.

    Coordinate convention:
        origin = sonar aperture, in metres
        x (cross-fan, right) = (xpix - cx_pix) * PIXEL_TO_METRE
        y (along boresight)  = (cy_pix - ypix) * PIXEL_TO_METRE

    cx_pix and cy_pix are computed from the data so the diver path is centred.
    """
    out = df.copy()
    out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp_utc"]).sort_values("timestamp_utc").reset_index(drop=True)

    cx_pix = (out["fan_u_pred_m"].min() + out["fan_u_pred_m"].max()) / 2.0
    cy_pix = (out["fan_v_pred_m"].min() + out["fan_v_pred_m"].max()) / 2.0

    out["common_x_m"] = (out["fan_u_pred_m"] - cx_pix) * PIXEL_TO_METRE
    out["common_y_m"] = (cy_pix - out["fan_v_pred_m"]) * PIXEL_TO_METRE

    # Velocity by finite differences
    t = (out["timestamp_utc"] - out["timestamp_utc"].iloc[0]).dt.total_seconds().to_numpy()
    out["common_vx_mps"] = np.gradient(out["common_x_m"].to_numpy(), t)
    out["common_vy_mps"] = np.gradient(out["common_y_m"].to_numpy(), t)

    return out


def generate_burst_drop_mask(n: int, rng: np.random.Generator) -> np.ndarray:
    """Generate a boolean mask using a burst-drop model.

    Bursts of `burst_length_range` consecutive frames are dropped with a
    per-frame start probability of `burst_rate_per_frame`. This produces
    realistic packet-loss patterns rather than independent Bernoulli noise.
    """
    keep = np.ones(n, dtype=bool)
    i = 0
    while i < n:
        if rng.random() < BURST_RATE_PER_FRAME:
            burst_len = int(rng.integers(BURST_LENGTH_RANGE[0],
                                         BURST_LENGTH_RANGE[1] + 1))
            keep[i:i + burst_len] = False
            i += burst_len
        else:
            i += 1
    return keep


def add_occlusion_gap(df: pd.DataFrame, t_min: float, t_max: float,
                      rng: np.random.Generator) -> pd.DataFrame:
    """Drop a contiguous time window from the dataframe to simulate an
    occlusion event where the target leaves the sonar's field-of-view.

    The gap is placed somewhere in the middle 80% of the timeline so that
    we still have observations both before and after.
    """
    if len(df) == 0:
        return df
    elapsed = (pd.to_datetime(df["timestamp_utc"], utc=True)
               - pd.to_datetime(df["timestamp_utc"].iloc[0], utc=True)
               ).dt.total_seconds().to_numpy()
    t_actual_min = float(elapsed.min())
    t_actual_max = float(elapsed.max())
    if t_actual_max <= t_actual_min + OCCLUSION_GAP_RANGE_S[1]:
        return df

    gap_duration = float(rng.uniform(*OCCLUSION_GAP_RANGE_S))
    gap_start = float(rng.uniform(t_actual_min + 0.1 * (t_actual_max - t_actual_min),
                                  t_actual_max - 0.1 * (t_actual_max - t_actual_min)
                                  - gap_duration))
    gap_end = gap_start + gap_duration

    mask = (elapsed < gap_start) | (elapsed > gap_end)
    return df.loc[mask].reset_index(drop=True)


def simulate_secondary(sonar_a: pd.DataFrame, translation_m, yaw_deg, seed):
    """Generate realistic simulated secondary-sonar observations.

    Pipeline:
      1. Resample A onto a uniform 10 Hz grid (interpolation).
      2. Apply burst-loss mask (independent on B, captures ~10% missing).
      3. Apply occlusion gap on A (handled by caller via add_occlusion_gap).
      4. Transform A-common -> B-local via R(yaw)^T @ (p - T).
      5. Inject Gaussian noise in B-local with per-sensor sigma.
      6. Transform B-local -> A-common for the associator.
      7. Apply small clock-jitter (~10 ms) to timestamps.
    """
    rng = np.random.default_rng(seed)
    yaw = np.deg2rad(yaw_deg)
    R   = np.array([[np.cos(yaw), -np.sin(yaw)],
                    [np.sin(yaw),  np.cos(yaw)]])
    translation = np.asarray(translation_m, dtype=float)

    t0 = sonar_a["timestamp_utc"].iloc[0]
    t  = (sonar_a["timestamp_utc"] - t0).dt.total_seconds().to_numpy()
    dt = 1.0 / SAMPLING_RATE_HZ
    tb = np.arange(t.min(), t.max(), dt)

    pc = np.column_stack([
        np.interp(tb, t, sonar_a["common_x_m"].to_numpy()),
        np.interp(tb, t, sonar_a["common_y_m"].to_numpy()),
    ])
    vc = np.column_stack([
        np.interp(tb, t, sonar_a["common_vx_mps"].to_numpy()),
        np.interp(tb, t, sonar_a["common_vy_mps"].to_numpy()),
    ])

    # Burst-loss mask for B
    keep = generate_burst_drop_mask(len(tb), rng)
    tb = tb[keep]
    pc = pc[keep]
    vc = vc[keep]

    # Transform A-common -> B-local
    p_local = (R.T @ (pc - translation).T).T
    v_local = (R.T @ vc.T).T

    # Inject Gaussian noise in B-local (realistic anisotropic noise via
    # different per-axis scaling would be a future refinement).
    p_local_noisy = p_local + rng.normal(0, NOISE_B["position_std_m"],   p_local.shape)
    v_local_noisy = v_local + rng.normal(0, NOISE_B["velocity_std_mps"], v_local.shape)

    # Transform B-local -> A-common for the associator
    p_common_obs = (R @ p_local_noisy.T).T + translation
    v_common_obs = (R @ v_local_noisy.T).T

    jitter = rng.normal(0, TIMESTAMP_JITTER_S, len(tb))
    ts_obs = [t0 + pd.to_timedelta(float(tt + jj), unit="s")
              for tt, jj in zip(tb, jitter)]

    return pd.DataFrame({
        "timestamp_utc":   [t.isoformat().replace("+00:00", "Z") for t in ts_obs],
        "elapsed_s":       tb + jitter,
        "sensor_id":       "SIM_SONAR_B",
        "track_id":        "B_SIM_DIVER",
        "coordinate_frame": "ASSOCIATION_COMMON_LOCAL",
        "common_x_m":      p_common_obs[:, 0],
        "common_y_m":      p_common_obs[:, 1],
        "common_vx_mps":   v_common_obs[:, 0],
        "common_vy_mps":   v_common_obs[:, 1],
        "Pxx_m2":          NOISE_B["position_std_m"] ** 2,
        "Pxy_m2":          0.0,
        "Pyy_m2":          NOISE_B["position_std_m"] ** 2,
        "Pvxvx_m2ps2":     NOISE_B["velocity_std_mps"] ** 2,
        "Pvyvy_m2ps2":     NOISE_B["velocity_std_mps"] ** 2,
        "is_simulated":    True,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_one_set(set_num: int) -> dict:
    src = DFKI_TRACKS_ROOT / f"dfki_set_{set_num}/tracks_raw.csv"
    if not src.exists():
        print(f"  set_{set_num}: tracks_raw.csv missing - skipping")
        return {}

    print(f"\n  set_{set_num}: reading {src}")
    df = pd.read_csv(src)
    primary = pick_primary(df)
    print(f"    selected track: {primary['track_id'].iloc[0]}  ({len(primary)} rows)")

    # Pixel -> common frame (A side)
    sonar_a = pixel_to_common(primary)

    # Use the original Puu/Pvv covariance from tracks_raw when available;
    # otherwise fall back to NOISE_A.
    default_pos_var = NOISE_A["position_std_m"] ** 2
    default_vel_var = NOISE_A["velocity_std_mps"] ** 2
    if "Puu_m2" in sonar_a.columns:
        pos_var = sonar_a["Puu_m2"].fillna(default_pos_var).to_numpy()
    else:
        pos_var = np.full(len(sonar_a), default_pos_var)
    if "Puv_m2" in sonar_a.columns:
        cross_var = sonar_a["Puv_m2"].fillna(0.0).to_numpy()
    else:
        cross_var = np.zeros(len(sonar_a))
    if "Pdu_m2ps2" in sonar_a.columns:
        vel_var = sonar_a["Pdu_m2ps2"].fillna(default_vel_var).to_numpy()
    else:
        vel_var = np.full(len(sonar_a), default_vel_var)

    # Build A output
    sonar_a_out = pd.DataFrame({
        "timestamp_utc":   sonar_a["timestamp_utc"],
        "frame_id":        sonar_a.get("frame_id", pd.Series(np.arange(len(sonar_a)))),
        "sensor_id":       "DFKI_REAL_SONAR_A",
        "track_id":        "A_" + primary["track_id"].iloc[0],
        "coordinate_frame": "ASSOCIATION_COMMON_LOCAL",
        "common_x_m":      sonar_a["common_x_m"],
        "common_y_m":      sonar_a["common_y_m"],
        "common_vx_mps":   sonar_a["common_vx_mps"],
        "common_vy_mps":   sonar_a["common_vy_mps"],
        "Pxx_m2":          pos_var,
        "Pxy_m2":          cross_var,
        "Pyy_m2":          pos_var,
        "Pvxvx_m2ps2":     vel_var,
        "Pvyvy_m2ps2":     vel_var,
        "is_simulated":    False,
    })

    # Add an occlusion gap to A (target briefly out of A's FOV)
    rng = np.random.default_rng(SEEDS[set_num] + 7)
    sonar_a_out = add_occlusion_gap(sonar_a_out, 0.0, 1e9, rng)

    # Simulate B
    sonar_b = simulate_secondary(
        sonar_a,    # use pre-occlusion A as the truth reference
        translation_m=SIM_PARAMS[set_num]["translation_m"],
        yaw_deg=SIM_PARAMS[set_num]["yaw_deg"],
        seed=SEEDS[set_num],
    )

    out_a = OUT_DIR / f"sonar_A_dfki_set_{set_num}_common.csv"
    out_b = OUT_DIR / f"sonar_B_dfki_set_{set_num}_common.csv"
    out_p = OUT_DIR / f"simulation_parameters_set_{set_num}.json"
    sonar_a_out.to_csv(out_a, index=False)
    sonar_b.to_csv(out_b, index=False)

    params = {
        "set": set_num,
        "source_track": primary["track_id"].iloc[0],
        "pixel_to_metre": PIXEL_TO_METRE,
        "translation_m": SIM_PARAMS[set_num]["translation_m"],
        "yaw_deg": SIM_PARAMS[set_num]["yaw_deg"],
        "noise_A": NOISE_A,
        "noise_B": NOISE_B,
        "sampling_rate_hz": SAMPLING_RATE_HZ,
        "timestamp_jitter_s": TIMESTAMP_JITTER_S,
        "burst_drop": {
            "rate_per_frame": BURST_RATE_PER_FRAME,
            "length_range": list(BURST_LENGTH_RANGE),
        },
        "occlusion_gap_range_s": list(OCCLUSION_GAP_RANGE_S),
        "random_seed": SEEDS[set_num],
        "n_sonar_a_rows": len(sonar_a_out),
        "n_sonar_b_rows": len(sonar_b),
    }
    with open(out_p, "w") as f:
        json.dump(params, f, indent=2)
    print(f"    wrote {out_a.name}  ({len(sonar_a_out)} rows)")
    print(f"    wrote {out_b.name}  ({len(sonar_b)} rows)")
    return params


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_params = []
    for s in [1, 2, 3, 4]:
        p = process_one_set(s)
        if p:
            all_params.append(p)

    with open(OUT_DIR / "all_simulation_parameters.json", "w") as f:
        json.dump(all_params, f, indent=2)
    print(f"\n  Generated {len(all_params)} DFKI simulated-sonar pairs")
    print(f"  Output: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
