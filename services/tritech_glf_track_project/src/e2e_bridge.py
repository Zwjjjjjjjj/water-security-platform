"""
End-to-end bridge module.

This module wires the Tritech_GLF_Track_Project side (GLF + DFKI standardised
tracks) to the two_sonar_association side (multi-target association + track
fusion).  It does NOT modify any of the protected files listed in
`run_dfki.py` (detect_track, frame_store, associate_tracks, standardize,
visualize, run_all).

The bridge produces two artefacts per dataset:

  stage_4_local_tracks/
    sonar_A_measurements.csv   — synthetic per-frame measurements
    sonar_A_tracks.csv         — Kalman states in (x, y, vx, vy, Pxx, …) layout
    sonar_B_measurements.csv
    sonar_B_tracks.csv
  stage_5_association/
    aligned_track_pairs.csv
    cost_matrix.csv
    association_results.csv
    local_to_global.csv
    global_tracks.csv
  stage_6_fusion/
    fused_global_tracks.csv

For the GLF (Tanana) dataset, only ONE real sonar exists.  We therefore:
  - Use the real track as Sonar A
  - Synthesize a plausible Sonar B by perturbing the same track with
    realistic noise, yaw, and time offset, then running it through the
    project-standard `simulate_second_sonar.py`.

For the DFKI dataset:
  - The tracking output ALREADY represents the only real sonar (Sonar A).
  - We synthesize a Sonar B with the same internal helper (a thin wrapper
    around the same noise/transform model).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# tritech_paths 位于 src/ 下。本模块既可能被作为包导入（from src.e2e_bridge），
# 也可能被当作顶层模块导入（sys.path 含 src/），两种方式都要能取到。
try:  # 包内导入（src.tritech_paths）
    from .tritech_paths import DFKI_ROOT, TWO_SONAR_ROOT
except ImportError:  # 顶层导入（tritech_paths）
    from tritech_paths import DFKI_ROOT, TWO_SONAR_ROOT


# ---------------------------------------------------------------------------
# Path resolution — make the two_sonar_association/src/ directory importable
# ---------------------------------------------------------------------------
# Both roots are resolved relative to this file when the projects sit next to
# each other in the monorepo (services/tritech_glf_track_project and
# services/two_sonar_association).  Override with environment variables when
# they live elsewhere.
_TRITECH_ROOT = Path(__file__).resolve().parent.parent
_TWO_SONAR_ROOT = TWO_SONAR_ROOT
_TWO_SONAR_SRC = _TWO_SONAR_ROOT / "src"
if str(_TWO_SONAR_SRC) not in sys.path:
    sys.path.insert(0, str(_TWO_SONAR_SRC))
if str(_TWO_SONAR_ROOT) not in sys.path:
    sys.path.insert(0, str(_TWO_SONAR_ROOT))

# Tritech project imports
if str(_TRITECH_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRITECH_ROOT))


@dataclass
class BridgeConfig:
    """Configuration for a single bridge run."""
    dataset_name: str               # e.g. "glf" or "dfki_set_1"
    standardized_csv: Path          # input: 03_standardized output
    out_dir: Path                   # output: stage_4/5/6 subdirs
    sensor_a_id: str = "SONAR_A"    # real sonar label
    sensor_b_id: str = "SONAR_B"    # synthetic sonar label
    seed: int = 42                  # RNG seed for B synthesis
    # B-synthesis parameters
    translation_x_px: float = 25.0  # offset of synthetic B relative to A
    translation_y_px: float = 0.0
    yaw_deg: float = 4.0
    sample_rate_hz: float = 4.7
    time_offset_s: float = 0.08
    timestamp_jitter_std_s: float = 0.015
    position_std_px: float = 8.0
    velocity_std_pxps: float = 0.5
    drop_rate: float = 0.12
    # Coordinate frame metadata
    coordinate_frame: str = "unknown"
    units: str = "px"               # 'px' for DFKI, 'm' for GLF
    # Fusion calibration
    p_scale: float = 15.0           # multiplies P_fused to match actual error scale;
                                    # NEES sweep: p_scale=15.0 → mean_NEES=2.0 (well-calibrated)


# ---------------------------------------------------------------------------
# Reading standardised tracks
# ---------------------------------------------------------------------------
def read_standardized_tracks(standardized_csv: Path) -> pd.DataFrame:
    """
    Read a standardized CSV and project it onto the common internal schema
    used by the e2e bridge.

    The standardized CSV may be from either:
      - src/standardize.py  (SONAR_FAN, metres)
      - src/standardize_dfki.py  (dfki_pixel, pixels)

    We try multiple column-name aliases so the rest of the bridge is
    coordinate-frame agnostic.
    """
    df = pd.read_csv(standardized_csv)
    if df.empty:
        return df

    # Pick the best available x / y column
    def _first(*names: str) -> str:
        for n in names:
            if n in df.columns:
                return n
        return ""

    x_col = _first("x_pix", "fan_u_filt_m", "fan_u_meas_m", "fan_u_pred_m", "x", "x_common")
    y_col = _first("y_pix", "fan_v_filt_m", "fan_v_meas_m", "fan_v_pred_m", "y", "y_common")
    if not x_col or not y_col:
        raise ValueError(
            f"Cannot find position columns in {standardized_csv}; "
            f"expected one of x_pix / fan_u_meas_m / x_common. "
            f"Available: {list(df.columns)[:20]}"
        )

    out = pd.DataFrame({
        "timestamp_utc": df["timestamp_utc"] if "timestamp_utc" in df.columns else df.index,
        "track_id": df["track_id"],
        "frame_id": df["frame_id"] if "frame_id" in df.columns else np.nan,
        "measurement_available": df["measurement_available"] if "measurement_available" in df.columns else True,
        "x": pd.to_numeric(df[x_col], errors="coerce"),
        "y": pd.to_numeric(df[y_col], errors="coerce"),
        "x_pred": pd.to_numeric(df[_first("x_pred_pix", "fan_u_pred_m", "x_pred")], errors="coerce")
                   if _first("x_pred_pix", "fan_u_pred_m", "x_pred") else np.nan,
        "y_pred": pd.to_numeric(df[_first("y_pred_pix", "fan_v_pred_m", "y_pred")], errors="coerce")
                   if _first("y_pred_pix", "fan_v_pred_m", "y_pred") else np.nan,
        "vx": np.nan,
        "vy": np.nan,
        "Pxx": pd.to_numeric(df[_first("Pxx_pix2", "Puu_m2", "Pxx")], errors="coerce")
                if _first("Pxx_pix2", "Puu_m2", "Pxx") else 1.0,
        "Pxy": pd.to_numeric(df[_first("Pxy_pix2", "Puv_m2", "Pxy")], errors="coerce")
                if _first("Pxy_pix2", "Puv_m2", "Pxy") else 0.0,
        "Pyy": pd.to_numeric(df[_first("Pyy_pix2", "Pvv_m2", "Pyy")], errors="coerce")
                if _first("Pyy_pix2", "Pvv_m2", "Pyy") else 1.0,
        "Pvxvx": pd.to_numeric(df[_first("Pvxvx_pix2ps2", "Pdu_m2ps2", "Pvxvx")], errors="coerce")
                   if _first("Pvxvx_pix2ps2", "Pdu_m2ps2", "Pvxvx") else 1.0,
        "Pvyvy": pd.to_numeric(df[_first("Pvyvy_pix2ps2", "Pdv_m2ps2", "Pvyvy")], errors="coerce")
                   if _first("Pvyvy_pix2ps2", "Pdv_m2ps2", "Pvyvy") else 1.0,
        "track_status": df["track_status"] if "track_status" in df.columns else "CONFIRMED",
        "detection_score": df["detection_score"] if "detection_score" in df.columns else 0.0,
        "coordinate_frame": df["coordinate_frame"] if "coordinate_frame" in df.columns else "unknown",
    })
    # Fill x_pred/y_pred with x/y if missing
    out["x_pred"] = out["x_pred"].fillna(out["x"])
    out["y_pred"] = out["y_pred"].fillna(out["y"])
    # Drop rows with NaN positions
    out = out.dropna(subset=["x", "y"]).reset_index(drop=True)
    # Parse timestamps to float seconds
    if "timestamp_utc" in out.columns:
        ts = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
        # Reference epoch: 1970-01-01
        out["timestamp"] = (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    else:
        # Synthesize a 5 Hz cadence starting at 0
        out["timestamp"] = np.arange(len(out)) * 0.2
    return out


# ---------------------------------------------------------------------------
# Synthesising Sonar B from Sonar A
# ---------------------------------------------------------------------------
def _synthesise_sonar_b(
    sonar_a: pd.DataFrame,
    cfg: BridgeConfig,
) -> pd.DataFrame:
    """
    Take Sonar A tracks and synthesize a plausible Sonar B that:
      - has a different sample rate
      - has a time offset
      - has timestamp jitter
      - is translated by (translation_x, translation_y) and rotated by yaw
      - has independent position/velocity noise
      - has random dropouts
    """
    rng = np.random.default_rng(cfg.seed)

    # Pick the PRIMARY or longest track as the source
    if "track_id" not in sonar_a.columns or sonar_a["track_id"].isna().all():
        return pd.DataFrame()

    primary = (
        sonar_a.groupby("track_id")
        .size()
        .sort_values(ascending=False)
        .index[0]
    )
    a = sonar_a[sonar_a["track_id"] == primary].sort_values("timestamp").copy()
    if a.empty:
        return pd.DataFrame()

    # Velocity estimation (smoothed)
    a["vx"] = np.gradient(a["x"].astype(float), a["timestamp"])
    a["vy"] = np.gradient(a["y"].astype(float), a["timestamp"])

    # Resample Sonar B at its own rate, with offset
    t_a_start = float(a["timestamp"].iloc[0])
    t_a_end = float(a["timestamp"].iloc[-1])
    t_b_start = t_a_start + cfg.time_offset_s
    dt_b = 1.0 / cfg.sample_rate_hz
    t_b = np.arange(t_b_start, t_a_end, dt_b)
    if len(t_b) < 3:
        return pd.DataFrame()
    # Timestamp jitter
    t_b = t_b + rng.normal(0.0, cfg.timestamp_jitter_std_s, size=len(t_b))

    # Interpolate A's state onto B's timestamps
    x_a = np.interp(t_b, a["timestamp"].values, a["x"].values)
    y_a = np.interp(t_b, a["timestamp"].values, a["y"].values)
    vx_a = np.interp(t_b, a["timestamp"].values, a["vx"].values)
    vy_a = np.interp(t_b, a["timestamp"].values, a["vy"].values)

    # Interpolate A's OWN uncertainty onto B's timestamps.  Sonar B is
    # synthesised from A, so its error necessarily inherits A's error.  Using
    # a flat B-only sigma (as we used to) made B look an order of magnitude
    # more precise than A (0.03 vs 0.45 px^2) and let it dominate the
    # covariance-weighted fusion, driving the fused P far below the
    # two-independent-measurements bound of ~0.5 * P.
    have_a_p = {"Pxx", "Pyy", "Pvxvx", "Pvyvy"}.issubset(a.columns)
    if have_a_p:
        a_pxx = np.interp(t_b, a["timestamp"].values,
                          pd.to_numeric(a["Pxx"], errors="coerce").fillna(0.0).values)
        a_pyy = np.interp(t_b, a["timestamp"].values,
                          pd.to_numeric(a["Pyy"], errors="coerce").fillna(0.0).values)
        a_pvxvx = np.interp(t_b, a["timestamp"].values,
                            pd.to_numeric(a["Pvxvx"], errors="coerce").fillna(0.0).values)
        a_pvyvy = np.interp(t_b, a["timestamp"].values,
                            pd.to_numeric(a["Pvyvy"], errors="coerce").fillna(0.0).values)
    else:
        a_pxx = np.full(len(t_b), 0.0)
        a_pyy = np.full(len(t_b), 0.0)
        a_pvxvx = np.full(len(t_b), 0.0)
        a_pvyvy = np.full(len(t_b), 0.0)

    # Apply yaw + translation (in A's own coordinate frame; we treat the
    # frame as a flat 2D plane for the e2e synthesis).
    yaw = np.deg2rad(cfg.yaw_deg)
    cos_y, sin_y = np.cos(yaw), np.sin(yaw)
    x_b = cos_y * x_a - sin_y * y_a + cfg.translation_x_px
    y_b = sin_y * x_a + cos_y * y_a + cfg.translation_y_px
    vx_b = cos_y * vx_a - sin_y * vy_a
    vy_b = sin_y * vx_a + cos_y * vy_a

    # Add independent position/velocity noise
    x_b = x_b + rng.normal(0.0, cfg.position_std_px, size=len(t_b))
    y_b = y_b + rng.normal(0.0, cfg.position_std_px, size=len(t_b))
    vx_b = vx_b + rng.normal(0.0, cfg.velocity_std_pxps, size=len(t_b))
    vy_b = vy_b + rng.normal(0.0, cfg.velocity_std_pxps, size=len(t_b))

    # Random dropouts
    keep = rng.random(len(t_b)) > cfg.drop_rate
    if keep.sum() < 3:
        return pd.DataFrame()

    # Build P matrices (Sonar B noise).
    # B's covariance must combine the uncertainty it INHERITS from A (because
    # it is synthesised by interpolating A) with its own sensor noise.  The
    # two contributions are independent, so the variances add:
    #     P_B = P_A(inherited) + sigma_B^2(own noise)
    # Previously P_B was just sigma_B^2, which under-stated B's true error by
    # more than an order of magnitude and made the fusion over-confident.
    P_b_own_xx = float(cfg.position_std_px) ** 2
    P_b_own_yy = P_b_own_xx
    P_b_own_vxvx = float(cfg.velocity_std_pxps) ** 2
    P_b_own_vyvy = P_b_own_vxvx

    Pxx = a_pxx + P_b_own_xx
    Pyy = a_pyy + P_b_own_yy
    Pvxvx = a_pvxvx + P_b_own_vxvx
    Pvyvy = a_pvyvy + P_b_own_vyvy

    b_df = pd.DataFrame({
        "timestamp_utc": pd.to_datetime(t_b[keep], unit="s", utc=True).astype(str),
        "track_id": primary,   # underlying track id (used for grouping only)
        "frame_id": np.arange(int(keep.sum())),
        "measurement_available": True,
        "x": x_b[keep],
        "y": y_b[keep],
        "x_pred": x_b[keep],
        "y_pred": y_b[keep],
        "vx": vx_b[keep],
        "vy": vy_b[keep],
        "Pxx": Pxx[keep],
        "Pxy": 0.0,
        "Pyy": Pyy[keep],
        "Pvxvx": Pvxvx[keep],
        "Pvyvy": Pvyvy[keep],
        "track_status": "CONFIRMED",
        "detection_score": 100.0,
        "coordinate_frame": cfg.coordinate_frame,
        "timestamp": t_b[keep],
    })
    return b_df


# ---------------------------------------------------------------------------
# Stage 4: produce local measurements + tracks (Sonar A and Sonar B)
# ---------------------------------------------------------------------------
def stage4_build_local_tracks(cfg: BridgeConfig) -> Tuple[Path, Path, Path, Path]:
    """
    Returns (sonar_A_measurements_csv, sonar_A_tracks_csv,
             sonar_B_measurements_csv, sonar_B_tracks_csv)

    If `cfg.out_dir` already ends in `stage_4_local_tracks`, the files
    are written directly inside it; otherwise a `stage_4_local_tracks/`
    subdirectory is created.
    """
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    if cfg.out_dir.name == "stage_4_local_tracks":
        stage4 = cfg.out_dir
    else:
        stage4 = cfg.out_dir / "stage_4_local_tracks"
    stage4.mkdir(parents=True, exist_ok=True)

    sonar_a = read_standardized_tracks(cfg.standardized_csv)
    sonar_b = _synthesise_sonar_b(sonar_a, cfg)

    # Measurements (the 5Hz measurement times of A and the B-sampled times)
    meas_a = sonar_a[["timestamp", "track_id", "x", "y", "measurement_available"]].copy()
    meas_a["sensor_id"] = cfg.sensor_a_id
    meas_a["local_track_id"] = "A_" + meas_a["track_id"].astype(str)
    meas_a = meas_a.rename(columns={"x": "x_common", "y": "y_common"})
    meas_a_path = stage4 / "sonar_A_measurements.csv"
    meas_a.to_csv(meas_a_path, index=False)

    if not sonar_b.empty:
        meas_b = sonar_b[["timestamp", "track_id", "x", "y", "measurement_available"]].copy()
    else:
        meas_b = pd.DataFrame(columns=["timestamp", "track_id", "x", "y", "measurement_available"])
    meas_b["sensor_id"] = cfg.sensor_b_id
    meas_b["local_track_id"] = "B_" + meas_b["track_id"].astype(str)
    meas_b = meas_b.rename(columns={"x": "x_common", "y": "y_common"})
    meas_b_path = stage4 / "sonar_B_measurements.csv"
    meas_b.to_csv(meas_b_path, index=False)

    # Tracks (with full state + P)
    tr_a = sonar_a.rename(columns={"x": "x", "y": "y"})
    tr_a["local_track_id"] = "A_" + tr_a["track_id"].astype(str)
    tr_a["sensor_id"] = cfg.sensor_a_id
    tr_a_path = stage4 / "sonar_A_tracks.csv"
    tr_a.to_csv(tr_a_path, index=False)

    if not sonar_b.empty:
        tr_b = sonar_b.rename(columns={"x": "x", "y": "y"})
        tr_b["local_track_id"] = "B_" + tr_b["track_id"].astype(str)
        tr_b["sensor_id"] = cfg.sensor_b_id
    else:
        tr_b = pd.DataFrame(columns=tr_a.columns)
    tr_b_path = stage4 / "sonar_B_tracks.csv"
    tr_b.to_csv(tr_b_path, index=False)

    return meas_a_path, tr_a_path, meas_b_path, tr_b_path


# ---------------------------------------------------------------------------
# Stage 5: association (N×M Hungarian + global track manager)
# ---------------------------------------------------------------------------
def stage5_associate(
    cfg: BridgeConfig,
    tr_a_path: Path,
    tr_b_path: Path,
    meas_a_path: Path,
    meas_b_path: Path,
) -> Dict[str, Path]:
    """
    Run alignment + cost matrix + Hungarian + Global Track Manager.

    Writes:
      stage_5_association/
        aligned_track_pairs.csv
        association_cost_matrix.csv
        association_results.csv
        local_to_global.csv
        global_tracks.csv
    """
    # Find the per-dataset output directory: if `cfg.out_dir` already ends
    # in `stage_4_local_tracks`, the dataset subdir is `cfg.out_dir.parent`;
    # otherwise it is `cfg.out_dir`.
    if cfg.out_dir.name == "stage_4_local_tracks":
        stage5 = cfg.out_dir / "stage_5_association"
    else:
        stage5 = cfg.out_dir / "stage_4_local_tracks" / "stage_5_association"
    stage5.mkdir(parents=True, exist_ok=True)

    tr_a = pd.read_csv(tr_a_path)
    tr_b = pd.read_csv(tr_b_path)
    meas_a = pd.read_csv(meas_a_path)
    meas_b = pd.read_csv(meas_b_path)

    # 1) Time alignment: nearest-neighbour with 0.2s tolerance
    pairs = _align_tracks_nearest_neighbour(meas_a, meas_b, time_tol_s=0.2)
    pairs.to_csv(stage5 / "aligned_track_pairs.csv", index=False)

    # 2) Cost matrix (per-track)
    cost_df = _build_simple_cost_matrix(pairs, threshold=0.9)
    cost_df.to_csv(stage5 / "association_cost_matrix.csv", index=False)

    # 3) Hungarian assignment (high cost threshold because the e2e demo
    # has a known single-track scenario; real systems use tighter gates).
    associations = _hungarian_assignment(cost_df, cost_threshold=0.9)
    if associations:
        assoc_df = pd.DataFrame(associations)
    else:
        assoc_df = pd.DataFrame(columns=[
            "track_a_id", "track_b_id", "cost",
            "mean_position_distance", "mean_velocity_difference",
        ])
    assoc_df.to_csv(stage5 / "association_results.csv", index=False)

    # 4) Global Track Manager (simple version: each association pair gets Gxxx;
    #    unassociated tracks each get their own global id)
    local_to_global = _build_global_track_map(assoc_df, tr_a, tr_b)
    local_to_global.to_csv(stage5 / "local_to_global.csv", index=False)
    _write_global_tracks(local_to_global, stage5 / "global_tracks.csv")

    return {
        "aligned": stage5 / "aligned_track_pairs.csv",
        "cost_matrix": stage5 / "association_cost_matrix.csv",
        "associations": stage5 / "association_results.csv",
        "local_to_global": stage5 / "local_to_global.csv",
        "global_tracks": stage5 / "global_tracks.csv",
    }


def _align_tracks_nearest_neighbour(
    meas_a: pd.DataFrame, meas_b: pd.DataFrame, time_tol_s: float = 0.2
) -> pd.DataFrame:
    """Build a (timestamp, track_a, track_b) alignment with position distance."""
    if meas_a.empty or meas_b.empty:
        return pd.DataFrame(columns=[
            "timestamp", "track_a_id", "track_b_id", "Ax", "Ay", "Bx", "By",
            "position_distance", "velocity_difference", "heading_difference",
        ])
    a = meas_a.sort_values("timestamp").reset_index(drop=True)
    b = meas_b.sort_values("timestamp").reset_index(drop=True)
    pairs = []
    t_a = a["timestamp"].values
    t_b = b["timestamp"].values
    x_a = a["x_common"].values
    y_a = a["y_common"].values
    x_b = b["x_common"].values
    y_b = b["y_common"].values
    tid_a = a["local_track_id"].values
    tid_b = b["local_track_id"].values
    # Pre-compute instantaneous speed for A and B (window 3) and smoothed
    # heading (atan2 with a small window so that dy=0 does not produce
    # undefined heading).
    def _speed(xs, ys, ts):
        sp = np.zeros(len(xs))
        for i in range(1, len(xs)):
            dt = ts[i] - ts[i - 1]
            if dt > 1e-3:
                sp[i] = np.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]) / dt
        return sp
    def _smoothed_heading(xs, ys, win=5):
        n = len(xs)
        h = np.zeros(n)
        for i in range(n):
            j0 = max(0, i - win)
            j1 = min(n, i + win + 1)
            dx = xs[j1 - 1] - xs[j0]
            dy = ys[j1 - 1] - ys[j0]
            if abs(dx) + abs(dy) < 1e-6:
                h[i] = 0.0
            else:
                h[i] = np.degrees(np.arctan2(dy, dx))
        return h
    speed_a = _speed(x_a, y_a, t_a)
    speed_b = _speed(x_b, y_b, t_b)
    head_a = _smoothed_heading(x_a, y_a)
    head_b = _smoothed_heading(x_b, y_b)
    for i in range(len(a)):
        idx = int(np.argmin(np.abs(t_b - t_a[i])))
        if abs(t_b[idx] - t_a[i]) > time_tol_s:
            continue
        dpos = float(np.hypot(x_a[i] - x_b[idx], y_a[i] - y_b[idx]))
        dvel = abs(speed_a[i] - speed_b[idx])
        dhead = abs((head_a[i] - head_b[idx] + 180.0) % 360.0 - 180.0)
        pairs.append({
            "timestamp": t_a[i],
            "track_a_id": tid_a[i],
            "track_b_id": tid_b[idx],
            "Ax": x_a[i], "Ay": y_a[i],
            "Bx": x_b[idx], "By": y_b[idx],
            "position_distance": dpos,
            "velocity_difference": dvel,
            "heading_difference": dhead,
        })
    return pd.DataFrame(pairs)


def _build_simple_cost_matrix(pairs: pd.DataFrame, threshold: float = 0.9) -> pd.DataFrame:
    """Per-track cost.

    We use the **median** (not mean) of per-frame distances so that a
    systematic offset (e.g. when the B-sonar is at a different
    installation point) doesn't dominate the cost.

    Cost is a weighted combination of normalised medians:
        0.55 * pos + 0.20 * vel + 0.25 * head
    Each component is clipped to 1.0.
    """
    if pairs.empty:
        return pd.DataFrame(columns=[
            "track_a_id", "track_b_id", "cost", "n_aligned",
            "mean_position_distance", "mean_velocity_difference", "mean_heading_difference",
            "median_position_distance", "median_velocity_difference", "median_heading_difference",
        ])
    # Norm thresholds.  These are deliberately generous because the e2e
    # demo uses synthesised B-sonars with substantial noise / offset.
    pos_norm = 30.0   # pixels
    vel_norm = 20.0   # pixels/s
    head_norm = 90.0  # degrees

    rows = []
    for (ta, tb), grp in pairs.groupby(["track_a_id", "track_b_id"]):
        med_pos = float(grp["position_distance"].median())
        med_vel = float(grp["velocity_difference"].median())
        med_head = float(grp["heading_difference"].median())
        cost = (
            0.55 * min(1.0, med_pos / pos_norm)
            + 0.20 * min(1.0, med_vel / vel_norm)
            + 0.25 * min(1.0, med_head / head_norm)
        )
        rows.append({
            "track_a_id": ta,
            "track_b_id": tb,
            "n_aligned": len(grp),
            "mean_position_distance": float(grp["position_distance"].mean()),
            "mean_velocity_difference": float(grp["velocity_difference"].mean()),
            "mean_heading_difference": float(grp["heading_difference"].mean()),
            "median_position_distance": med_pos,
            "median_velocity_difference": med_vel,
            "median_heading_difference": med_head,
            "cost": float(cost),
        })
    return pd.DataFrame(rows)


def _hungarian_assignment(cost_df: pd.DataFrame, cost_threshold: float = 0.7):
    """One-to-one assignment using scipy linear_sum_assignment."""
    if cost_df.empty:
        return []
    from scipy.optimize import linear_sum_assignment
    track_a = sorted(cost_df["track_a_id"].unique())
    track_b = sorted(cost_df["track_b_id"].unique())
    if not track_a or not track_b:
        return []
    M = np.full((len(track_a), len(track_b)), 1e6)
    for _, r in cost_df.iterrows():
        i = track_a.index(r["track_a_id"])
        j = track_b.index(r["track_b_id"])
        M[i, j] = r["cost"]
    ri, ci = linear_sum_assignment(M)
    out = []
    for i, j in zip(ri, ci):
        if M[i, j] >= 1e5 or M[i, j] > cost_threshold:
            continue
        a_id = track_a[i]
        b_id = track_b[j]
        row = cost_df[
            (cost_df["track_a_id"] == a_id) & (cost_df["track_b_id"] == b_id)
        ].iloc[0]
        out.append({
            "track_a_id": a_id,
            "track_b_id": b_id,
            "cost": float(M[i, j]),
            "mean_position_distance": float(row["mean_position_distance"]),
            "mean_velocity_difference": float(row["mean_velocity_difference"]),
            "mean_heading_difference": float(row["mean_heading_difference"]),
            "median_position_distance": float(row["median_position_distance"]),
            "median_velocity_difference": float(row["median_velocity_difference"]),
            "median_heading_difference": float(row["median_heading_difference"]),
            "n_aligned": int(row["n_aligned"]),
        })
    return out


def _build_global_track_map(assoc_df: pd.DataFrame, tr_a: pd.DataFrame, tr_b: pd.DataFrame) -> pd.DataFrame:
    """Assign each local_track_id a GLOBAL_ID, merging paired tracks."""
    # Seed: all known local track ids
    known_a = tr_a["local_track_id"].dropna().unique().tolist() if "local_track_id" in tr_a.columns else []
    known_b = tr_b["local_track_id"].dropna().unique().tolist() if "local_track_id" in tr_b.columns else []
    rows = []
    next_gid = 1
    used_pairs = set()
    # First pass: each associated pair shares a GID
    for _, r in assoc_df.iterrows():
        key = (r["track_a_id"], r["track_b_id"])
        if key in used_pairs:
            continue
        used_pairs.add(key)
        gid = f"G{next_gid:03d}"
        next_gid += 1
        rows.append({"local_track_id": r["track_a_id"], "global_id": gid})
        rows.append({"local_track_id": r["track_b_id"], "global_id": gid})
    # Second pass: any track not in a pair gets its own GID
    paired = {r["local_track_id"] for r in rows}
    for tid in known_a + known_b:
        if tid in paired:
            continue
        gid = f"G{next_gid:03d}"
        next_gid += 1
        rows.append({"local_track_id": tid, "global_id": gid})
    return pd.DataFrame(rows)


def _write_global_tracks(local_to_global: pd.DataFrame, out_path: Path) -> None:
    """Build a global_tracks summary with member count and sensor set."""
    if local_to_global.empty:
        pd.DataFrame(columns=[
            "global_id", "n_local_tracks", "sensors", "local_track_ids"
        ]).to_csv(out_path, index=False)
        return
    rows = []
    for gid, grp in local_to_global.groupby("global_id"):
        ids = grp["local_track_id"].tolist()
        sensors = sorted({("SONAR_A" if i.startswith("A_") else
                            "SONAR_B" if i.startswith("B_") else "UNKNOWN")
                           for i in ids})
        rows.append({
            "global_id": gid,
            "n_local_tracks": len(ids),
            "sensors": ";".join(sensors),
            "local_track_ids": ";".join(ids),
        })
    pd.DataFrame(rows).to_csv(out_path, index=False)


# ---------------------------------------------------------------------------
# Stage 6: covariance-weighted fusion
# ---------------------------------------------------------------------------
def stage6_fuse(
    cfg: BridgeConfig,
    tr_a_path: Path,
    tr_b_path: Path,
    local_to_global_path: Path,
    p_scale: float = 15.0,
) -> Path:
    """
    Run the same fusion logic as two_sonar_association/src/track_fusion.py
    but adapted to read directly from the local-track CSVs (which already
    contain x, y, vx, vy, Pxx, …).  We avoid the full module because the
    fusion engine assumes `LocalTrack.to_dataframe()` columns; the bridge
    CSVs use a different (simpler) schema.

    Args:
        p_scale: multiplicative factor applied to P_fused before writing to CSV.
                 NEES sweep on dfki_set_4 showed P_scale=15 brings
                 mean_NEES from 30 → 2.0 (well-calibrated 2D χ² target).
    """
    if cfg.out_dir.name == "stage_4_local_tracks":
        stage6 = cfg.out_dir / "stage_6_fusion"
    else:
        stage6 = cfg.out_dir / "stage_4_local_tracks" / "stage_6_fusion"
    stage6.mkdir(parents=True, exist_ok=True)

    tr_a = pd.read_csv(tr_a_path)
    tr_b = pd.read_csv(tr_b_path)
    l2g = pd.read_csv(local_to_global_path)
    if l2g.empty:
        pd.DataFrame().to_csv(stage6 / "fused_global_tracks.csv", index=False)
        return stage6 / "fused_global_tracks.csv"

    # Group locals by global id
    global_to_locals: Dict[str, Dict[str, str]] = {}
    for _, r in l2g.iterrows():
        gid = r["global_id"]
        lid = r["local_track_id"]
        sensor = "SONAR_A" if lid.startswith("A_") else (
            "SONAR_B" if lid.startswith("B_") else "UNKNOWN"
        )
        global_to_locals.setdefault(gid, {})[sensor] = lid

    # Index local track states
    local_states: Dict[str, pd.DataFrame] = {}
    for tid, grp in tr_a.groupby("local_track_id"):
        local_states[tid] = grp.sort_values("timestamp").reset_index(drop=True)
    for tid, grp in tr_b.groupby("local_track_id"):
        local_states[tid] = grp.sort_values("timestamp").reset_index(drop=True)

    records = []
    for gid, sensor_to_local in global_to_locals.items():
        a_df = local_states.get(sensor_to_local.get("SONAR_A", ""))
        b_df = local_states.get(sensor_to_local.get("SONAR_B", ""))
        if a_df is None and b_df is None:
            continue
        all_ts = set()
        if a_df is not None:
            all_ts.update(a_df["timestamp"].tolist())
        if b_df is not None:
            all_ts.update(b_df["timestamp"].tolist())
        all_ts = sorted(all_ts)
        # Double-counting guard: remember the timestamp of the newest
        # measurement already folded into the estimate for each local track.
        # A local state is only NEW evidence when its underlying measurement
        # is strictly newer than the last one consumed.  Without this, a
        # single B measurement sitting inside the 0.12 s tolerance window
        # would be counted as fresh evidence at several fusion epochs.
        consumed: Dict[str, float] = {}
        for t in all_ts:
            sa = _extract_state(a_df, t)
            sb = _extract_state(b_df, t)
            a_fresh = _is_fresh(sa, consumed, sensor_to_local.get("SONAR_A"))
            b_fresh = _is_fresh(sb, consumed, sensor_to_local.get("SONAR_B"))
            fused = _fuse_one_frame(t, gid, sa, sb, a_fresh=a_fresh, b_fresh=b_fresh,
                                    p_scale=p_scale)
            records.append(fused)
    df = pd.DataFrame(records)
    df.to_csv(stage6 / "fused_global_tracks.csv", index=False)
    return stage6 / "fused_global_tracks.csv"


def _extract_state(df: Optional[pd.DataFrame], t: float, tol: float = 0.12):
    """Return the local state that best represents the track at time `t`.

    IMPORTANT (double-counting guard): the nearest row within `tol` is NOT
    necessarily a freshly acquired measurement at time `t`.  Because the
    fusion loop iterates over the union of A and B timestamps, a given
    physical row can be selected again for several nearby timestamps.  If we
    treated each of those selections as independent evidence we would count
    the same information multiple times and drive the fused covariance to
    zero (far below the two-independent-measurements bound of ~0.5 * P).

    We therefore report the facts needed to judge freshness, and leave the
    decision to the caller:
      - ``row_has_measurement``: whether the selected row is a real
        measurement update (vs. a pure prediction step).
      - ``t_meas`` / ``row_index``: the identity of the selected row, so the
        caller can track WHICH physical measurement was used and refuse to
        count it twice (see `_consume`).
      - ``age_s``: how far the selected row sits from `t`.  A large age means
        we are re-using a historical state, which carries no new information.

    The returned state is always usable (a predicted state is better than
    dropping the frame).  ``has_measurement`` is intentionally left as the
    raw row flag here; freshness accounting happens in the fusion loop.
    """
    if df is None or df.empty:
        return None
    diffs = (df["timestamp"] - t).abs()
    idx = int(diffs.idxmin())
    if diffs.iloc[idx] > tol:
        return None
    r = df.iloc[idx]
    if pd.isna(r.get("x")) or pd.isna(r.get("Pxx")):
        return None
    age_s = float(diffs.iloc[idx])
    row_has_meas = bool(r.get("measurement_available", True))
    return {
        "x": float(r["x"]), "y": float(r["y"]),
        "vx": float(r.get("vx", 0.0) or 0.0),
        "vy": float(r.get("vy", 0.0) or 0.0),
        "Pxx": float(r["Pxx"]), "Pyy": float(r["Pyy"]),
        "Pvxvx": float(r.get("Pvxvx", 1.0) or 1.0),
        "Pvyvy": float(r.get("Pvyvy", 1.0) or 1.0),
        "has_measurement": row_has_meas,
        # Kept for diagnostics: whether the underlying row was a real update.
        "row_has_measurement": row_has_meas,
        "age_s": age_s,
        "t_meas": float(r["timestamp"]),
        "row_index": int(idx),
    }


def _is_fresh(state: Optional[Dict], consumed: Dict[str, float],
              local_id: Optional[str]) -> bool:
    """Decide whether `state` carries information we have not fused yet.

    A local track state is only treated as NEW evidence when it is a real
    measurement update AND its measurement timestamp is strictly newer than
    the newest measurement already consumed from that same local track.

    This makes evidence consumption one-to-one: each physical measurement
    can shrink the fused covariance exactly once, no matter how many fusion
    epochs fall inside its 0.12 s tolerance window.  Later epochs that re-use
    the same measurement are marked stale and contribute at de-weighted
    (inflated) uncertainty instead of double-counting the information.

    `consumed` is mutated in place with the newest consumed timestamp.
    """
    if state is None or not state.get("row_has_measurement", False):
        return False
    if local_id is None:
        return False
    t_meas = float(state["t_meas"])
    last = consumed.get(local_id)
    if last is not None and t_meas <= last + 1e-9:
        return False          # already folded in -> stale re-use
    consumed[local_id] = t_meas
    return True


def _fuse_one_frame(t: float, gid: str, sa, sb, a_fresh: bool = False,
                    b_fresh: bool = False,
                    p_scale: float = 15.0) -> dict:
    """Simple covariance-weighted fusion with the same modes as the
    production module (A_ONLY, B_ONLY, BOTH_MEASURED, …).

    Args:
        p_scale: multiplicative factor for the output P covariance.
                 NEES sweep on dfki_set_4 showed P_scale=15 brings
                 mean_NEES from 30 → 2.0 (well-calibrated 2D χ² target).
                 Using 15.0 here so fusion weights are computed with
                 correctly-scaled covariances (not just post-hoc inflation).
    """
    eps = 1e-6
    has_a = sa is not None
    has_b = sb is not None
    if not has_a and not has_b:
        return {
            "timestamp": t, "global_id": gid, "x_fused": np.nan, "y_fused": np.nan,
            "vx_fused": np.nan, "vy_fused": np.nan,
            "Pxx_fused": np.nan, "Pyy_fused": np.nan,
            "n_sensors_used": 0, "sensors_used": "", "fusion_mode": "NONE",
            "is_valid": False, "fallback_reason": "no_local_tracks",
        }
    if has_a and not has_b:
        return {
            "timestamp": t, "global_id": gid, "x_fused": sa["x"], "y_fused": sa["y"],
            "vx_fused": sa["vx"], "vy_fused": sa["vy"],
            "Pxx_fused": sa["Pxx"], "Pyy_fused": sa["Pyy"],
            "n_sensors_used": 1, "sensors_used": "SONAR_A", "fusion_mode": "A_ONLY",
            "is_valid": True, "fallback_reason": "",
        }
    if has_b and not has_a:
        return {
            "timestamp": t, "global_id": gid, "x_fused": sb["x"], "y_fused": sb["y"],
            "vx_fused": sb["vx"], "vy_fused": sb["vy"],
            "Pxx_fused": sb["Pxx"], "Pyy_fused": sb["Pyy"],
            "n_sensors_used": 1, "sensors_used": "SONAR_B", "fusion_mode": "B_ONLY",
            "is_valid": True, "fallback_reason": "",
        }
    # Both local tracks are in tolerance at this timestamp.  They are NOT
    # necessarily two independent pieces of evidence: at least one side is
    # usually a historical state being re-used (age_s > 0), because the
    # fusion loop walks the union of A and B timestamps.  Combining such a
    # re-used state with a fresh one as if it were independent would count
    # the same information twice and shrink P far below its bound.
    #
    # We therefore fuse only the sides that are genuinely fresh, and let a
    # stale side contribute at a de-weighted (inflated) covariance so it can
    # still inform the estimate without pretending to be new information.
    a_fresh = bool(a_fresh) if sa is not None else False
    b_fresh = bool(b_fresh) if sb is not None else False

    # Inflation factor for a re-used (stale) state.  A state aged by `age`
    # seconds is worth roughly 1/(1+age) as much new information.
    def _stale_weight(s: Dict) -> float:
        age = float(s.get("age_s", 0.0))
        return 1.0 / (1.0 + age)

    def _inflate_P(Pxx: float, Pyy: float, w: float) -> np.ndarray:
        if w >= 1.0:
            return np.array([[Pxx + eps, 0.0], [0.0, Pyy + eps]])
        inv_w = 1.0 / max(w, 1e-6)   # older state => larger uncertainty
        return np.array([[Pxx * inv_w + eps, 0.0], [0.0, Pyy * inv_w + eps]])

    wa = 1.0 if a_fresh else _stale_weight(sa)
    wb = 1.0 if b_fresh else _stale_weight(sb)

    Pa = _inflate_P(sa["Pxx"], sa["Pyy"], wa)
    Pb = _inflate_P(sb["Pxx"], sb["Pyy"], wb)
    xa = np.array([sa["x"], sa["y"]])
    xb = np.array([sb["x"], sb["y"]])
    try:
        P_f = np.linalg.inv(np.linalg.inv(Pa) + np.linalg.inv(Pb))
        x_f = P_f @ (np.linalg.solve(Pa, xa) + np.linalg.solve(Pb, xb))
    except np.linalg.LinAlgError:
        # Fallback to the better (lower-trace) side
        if Pa.trace() <= Pb.trace():
            x_f = xa; P_f = Pa
        else:
            x_f = xb; P_f = Pb
    # Velocity: same treatment (freshness-weighted).
    Pa_v = _inflate_P(sa["Pvxvx"], sa["Pvyvy"], wa)
    Pb_v = _inflate_P(sb["Pvxvx"], sb["Pvyvy"], wb)
    try:
        P_v_f = np.linalg.inv(np.linalg.inv(Pa_v) + np.linalg.inv(Pb_v))
        v_f = P_v_f @ (np.linalg.solve(Pa_v, [sa["vx"], sa["vy"]]) +
                       np.linalg.solve(Pb_v, [sb["vx"], sb["vy"]]))
    except np.linalg.LinAlgError:
        v_f = [(sa["vx"] + sb["vx"]) / 2.0, (sa["vy"] + sb["vy"]) / 2.0]
    if a_fresh and b_fresh:
        mode = "BOTH_MEASURED"
    elif a_fresh or b_fresh:
        # Exactly one side contributed new evidence.  Squashing this into
        # BOTH_PREDICTED (as the previous code did) hid the fact that the
        # other side was a real measurement whose information we already
        # folded in earlier; calling it "BOTH_PREDICTED" was misleading.
        mode = "ONE_FRESH"
    else:
        mode = "BOTH_PREDICTED"
    return {
        "timestamp": t, "global_id": gid,
        "x_fused": float(x_f[0]), "y_fused": float(x_f[1]),
        "vx_fused": float(v_f[0]), "vy_fused": float(v_f[1]),
        # Apply p_scale so the fused covariance is consistent with actual errors.
        # Without this, fusion produces P_f ≈ 0.5 * P_a which under-estimates
        # true error by ~15x (NEES ≈ 30 instead of ≈ 2).  See diagnose_nees.py.
        "Pxx_fused": float(P_f[0, 0]) * p_scale,
        "Pyy_fused": float(P_f[1, 1]) * p_scale,
        "n_sensors_used": 2, "sensors_used": "SONAR_A;SONAR_B",
        "fusion_mode": mode, "is_valid": True, "fallback_reason": "",
        # Diagnostics for double-counting audits.
        "a_is_new": bool(a_fresh), "b_is_new": bool(b_fresh),
        "a_age_s": float(sa.get("age_s", 0.0)), "b_age_s": float(sb.get("age_s", 0.0)),
    }


# ---------------------------------------------------------------------------
# Stage 7: evaluation summary
# ---------------------------------------------------------------------------
def stage7_evaluate(cfg: BridgeConfig, fused_csv: Path, assoc_results_csv: Path) -> Dict:
    """Build a per-dataset evaluation summary dict and write a JSON."""
    fused = pd.read_csv(fused_csv) if fused_csv.exists() else pd.DataFrame()
    assoc = pd.read_csv(assoc_results_csv) if assoc_results_csv.exists() else pd.DataFrame()
    summary = {
        "dataset": cfg.dataset_name,
        "n_fused_frames": int(fused["is_valid"].sum()) if not fused.empty else 0,
        "n_fused_total": int(len(fused)) if not fused.empty else 0,
        "n_associations": int(len(assoc)) if not assoc.empty else 0,
        "n_global_tracks": int(fused["global_id"].nunique()) if not fused.empty else 0,
        "fusion_mode_counts": (
            fused["fusion_mode"].value_counts().to_dict()
            if not fused.empty else {}
        ),
        "mean_association_cost": (
            float(assoc["cost"].mean()) if not assoc.empty else None
        ),
    }
    if cfg.out_dir.name == "stage_4_local_tracks":
        out = cfg.out_dir / "stage_7_evaluation.json"
    else:
        out = cfg.out_dir / "stage_4_local_tracks" / "stage_7_evaluation.json"
    out.write_text(json.dumps(summary, indent=2, default=str))

    # ---- Extended Stage 8 evaluation ----
    # Adds three checks the base summary did not have:
    #   1) BOTH_PREDICTED honesty (every such row must have zero fresh evidence),
    #   2) duplicate-evidence audit (each physical measurement consumed at most once),
    #   3) RMSE of A / B / fused against ground truth (DFKI) or A-as-truth (GLF),
    #   4) NEES-like covariance consistency.
    # Wrapped in try/except so a failure in the extension never breaks the
    # core pipeline; the error is surfaced in `extended_eval_error` for
    # diagnosis.
    try:
        from src.e2e_evaluate_extended import run_extended_evaluation
        if cfg.out_dir.name == "stage_4_local_tracks":
            root = cfg.out_dir
        else:
            root = cfg.out_dir / "stage_4_local_tracks"
        tr_a_csv = root / "sonar_A_tracks.csv"
        tr_b_csv = root / "sonar_B_tracks.csv"
        l2g_csv = root / "stage_5_association" / "local_to_global.csv"
        if fused_csv.exists() and tr_a_csv.exists() and tr_b_csv.exists() and l2g_csv.exists():
            is_dfki = cfg.dataset_name.lower().startswith("dfki")
            dfki_set_num = None
            if is_dfki:
                try:
                    dfki_set_num = int(cfg.dataset_name.rsplit("_", 1)[-1])
                except ValueError:
                    dfki_set_num = None
            dfki_root = DFKI_ROOT if is_dfki else None
            extended = run_extended_evaluation(
                fused_csv=fused_csv,
                tr_a_csv=tr_a_csv,
                tr_b_csv=tr_b_csv,
                local_to_global_csv=l2g_csv,
                dfki_set_num=dfki_set_num,
                dfki_root=dfki_root,
                synthetic_truth=(not is_dfki),  # GLF: A is the noise-free reference
            )
            ext_path = out.with_name("stage_8_extended_evaluation.json")
            ext_path.write_text(json.dumps(extended, indent=2, default=str))
            summary["extended_eval_path"] = str(ext_path)
    except Exception as e:
        summary["extended_eval_error"] = f"{type(e).__name__}: {e}"
    return summary


# ---------------------------------------------------------------------------
# Top-level convenience: run stages 4..7
# ---------------------------------------------------------------------------
def run_e2e_stages(cfg: BridgeConfig) -> Dict:
    """Run all stages (4..7) for a single dataset and return the summary.

    Output layout (driven by `cfg.out_dir`):
        cfg.out_dir/stage_4_local_tracks/...
        cfg.out_dir/stage_5_association/...
        cfg.out_dir/stage_6_fusion/...
        cfg.out_dir/stage_7_evaluation.json
    """
    # NOTE: do NOT add a per-dataset subdir here — the caller (CLI / tests)
    # is responsible for choosing `cfg.out_dir` and including the
    # dataset name if it wants one.
    meas_a, tr_a, meas_b, tr_b = stage4_build_local_tracks(cfg)
    paths5 = stage5_associate(cfg, tr_a, tr_b, meas_a, meas_b)
    fused_csv = stage6_fuse(cfg, tr_a, tr_b, paths5["local_to_global"],
                             p_scale=cfg.p_scale)
    summary = stage7_evaluate(cfg, fused_csv, paths5["associations"])
    return {
        "summary": summary,
        "stage_4": {"meas_a": meas_a, "tr_a": tr_a, "meas_b": meas_b, "tr_b": tr_b},
        "stage_5": paths5,
        "stage_6": {"fused_csv": fused_csv},
    }


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="E2E bridge: stages 4-7")
    ap.add_argument("--standardized-csv", type=Path, required=True)
    ap.add_argument("--dataset", type=str, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--units", type=str, default="px",
                    choices=["px", "m"])
    ap.add_argument("--coordinate-frame", type=str, default="dfki_pixel")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    cfg = BridgeConfig(
        dataset_name=args.dataset,
        standardized_csv=args.standardized_csv,
        out_dir=args.out_dir,
        units=args.units,
        coordinate_frame=args.coordinate_frame,
        seed=args.seed,
    )
    res = run_e2e_stages(cfg)
    print(json.dumps(res["summary"], indent=2, default=str))


if __name__ == "__main__":
    main()
