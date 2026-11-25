"""
DFKI tracks standardizer.

The DFKI tracking pipeline (src/run_dfki.py) outputs `tracks_raw.csv` whose
columns already match the GLF pipeline (`fan_u_meas_m`, `fan_v_meas_m`, ...),
BUT the values are in **pixel** units, not metres.  This module re-projects
those rows into the project's standardized interface while explicitly marking
the coordinate frame as `dfki_pixel` so downstream consumers don't accidentally
interpret them as metres.

Output schema matches the GLF standardize.py output:
    timestamp_utc, frame_id, sensor_id, track_id, target_label, label_basis,
    coordinate_frame, swath_orientation, mount_location, boresight_direction,
    tilt_down_deg, sonar_depth_m, measurement_available,
    x_pix, y_pix,           # renamed from fan_u_meas_m / fan_v_meas_m
    x_pred_pix, y_pred_pix, # KF prediction (always available)
    speed_pixps, heading_pix_deg,
    Pxx_pix2, Pxy_pix2, Pyy_pix2, Pvxvx_pix2ps2, Pvyvy_pix2ps2,
    track_confidence, detection_score, track_status, hits, misses, age,
    horizontal_xy_valid, enu_valid, enu_invalid_reason

The output is intentionally distinct from `standardize.py` because:
    - No range/bearing columns (DFKI uses pixel-derived range only)
    - No experiment-event profile (DFKI doesn't have a Tanana-style log)
    - The covariance fields are pixel-based; writing them as m^2 would
      silently corrupt downstream covariance-weighted fusion.
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd
# tritech_paths 位于 src/ 下；包内导入与顶层导入两种方式都要支持。
try:  # 包内导入（src.tritech_paths）
    from .tritech_paths import OUTPUT_ROOT, TWO_SONAR_ROOT
except ImportError:  # 顶层导入（tritech_paths）
    from tritech_paths import OUTPUT_ROOT, TWO_SONAR_ROOT


def _safe_div(a, b, fill=np.nan):
    return np.where(np.abs(b) > 1e-9, a / b, fill)


def standardize_dfki_tracks(
    raw_tracks_csv: Path,
    track_summary_csv: Path,
    out_csv: Path,
    sensor_cfg: dict | None = None,
):
    """
    Read DFKI raw tracks + summary and write a standardized CSV.

    Parameters
    ----------
    raw_tracks_csv : Path
        Path to `outputs_dfki/dfki_set_N/tracks_raw.csv`.
    track_summary_csv : Path
        Path to `outputs_dfki/dfki_set_N/track_summary_all.csv`.
    out_csv : Path
        Destination path.  Parent directories are created if missing.
    sensor_cfg : dict, optional
        Sensor configuration; defaults to a DFKI-specific block that
        marks the data as pixel coordinates.

    Returns
    -------
    (df, summary) : (pd.DataFrame, pd.DataFrame)
    """
    raw_tracks_csv = Path(raw_tracks_csv)
    track_summary_csv = Path(track_summary_csv)
    out_csv = Path(out_csv)

    if sensor_cfg is None:
        sensor_cfg = {
            "sensor_id": "DFKI_932x514_imaging_sonar",
            "coordinate_frame": "dfki_pixel",
            "swath_orientation": "Imaging (pixel units)",
            "mount_location": "Unknown (DFKI diver tracking dataset)",
            "boresight_direction": "Unknown",
            "tilt_down_deg": np.nan,
            "sonar_depth_m": np.nan,
            "horizontal_xy_valid": False,
            "enu_valid": False,
            "enu_invalid_reason": (
                "DFKI pixel coordinates — no real-world scale or extrinsics "
                "available in the dataset. Do NOT mix with SONAR_FAN metres."
            ),
        }

    if not raw_tracks_csv.exists():
        raise FileNotFoundError(f"DFKI raw tracks not found: {raw_tracks_csv}")
    if not track_summary_csv.exists():
        raise FileNotFoundError(f"DFKI track summary not found: {track_summary_csv}")

    tracks = pd.read_csv(raw_tracks_csv)
    if tracks.empty:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        # Write a header-only file so downstream stages fail loudly
        tracks.to_csv(out_csv, index=False)
        return tracks, pd.DataFrame()

    # Standardized sensor block
    tracks.insert(2, "sensor_id", sensor_cfg.get("sensor_id", "DFKI"))
    tracks["coordinate_frame"] = sensor_cfg.get("coordinate_frame", "dfki_pixel")
    tracks["swath_orientation"] = sensor_cfg.get("swath_orientation", "Unknown")
    tracks["mount_location"] = sensor_cfg.get("mount_location", "Unknown")
    tracks["boresight_direction"] = sensor_cfg.get("boresight_direction", "Unknown")
    tracks["tilt_down_deg"] = sensor_cfg.get("tilt_down_deg", np.nan)
    tracks["sonar_depth_m"] = sensor_cfg.get("sonar_depth_m", np.nan)
    tracks["horizontal_xy_valid"] = bool(sensor_cfg.get("horizontal_xy_valid", False))
    tracks["enu_valid"] = bool(sensor_cfg.get("enu_valid", False))
    tracks["enu_invalid_reason"] = sensor_cfg.get("enu_invalid_reason", "")

    # DFKI has no experiment-event profile → UNKNOWN labels
    if "target_label" not in tracks.columns:
        tracks["target_label"] = "UNKNOWN"
    if "label_basis" not in tracks.columns:
        tracks["label_basis"] = "none"

    # Renamed columns: keep GLF names too for backward compat but also
    # add explicit pixel-suffixed names that downstream consumers (e2e_bridge)
    # can rely on.
    if "fan_u_meas_m" in tracks.columns and "x_pix" not in tracks.columns:
        tracks["x_pix"] = tracks["fan_u_meas_m"]
    if "fan_v_meas_m" in tracks.columns and "y_pix" not in tracks.columns:
        tracks["y_pix"] = tracks["fan_v_meas_m"]
    if "fan_u_pred_m" in tracks.columns and "x_pred_pix" not in tracks.columns:
        tracks["x_pred_pix"] = tracks["fan_u_pred_m"]
    if "fan_v_pred_m" in tracks.columns and "y_pred_pix" not in tracks.columns:
        tracks["y_pred_pix"] = tracks["fan_v_pred_m"]
    if "fan_speed_mps" in tracks.columns and "speed_pixps" not in tracks.columns:
        tracks["speed_pixps"] = tracks["fan_speed_mps"]
    if "heading_fan_deg" in tracks.columns and "heading_pix_deg" not in tracks.columns:
        tracks["heading_pix_deg"] = tracks["heading_fan_deg"]

    # Renamed covariance fields.  In DFKI they are *pixel-based* but the
    # original column names are in m^2.  The standardized versions are
    # explicitly _pix2 / _pix2ps2 so the units cannot be mis-read.
    if "Puu_m2" in tracks.columns and "Pxx_pix2" not in tracks.columns:
        tracks["Pxx_pix2"] = tracks["Puu_m2"]
    if "Puv_m2" in tracks.columns and "Pxy_pix2" not in tracks.columns:
        tracks["Pxy_pix2"] = tracks["Puv_m2"]
    if "Pvv_m2" in tracks.columns and "Pyy_pix2" not in tracks.columns:
        tracks["Pyy_pix2"] = tracks["Pvv_m2"]
    if "Pdu_m2ps2" in tracks.columns and "Pvxvx_pix2ps2" not in tracks.columns:
        tracks["Pvxvx_pix2ps2"] = tracks["Pdu_m2ps2"]
    if "Pdv_m2ps2" in tracks.columns and "Pvyvy_pix2ps2" not in tracks.columns:
        tracks["Pvyvy_pix2ps2"] = tracks["Pdv_m2ps2"]

    # Confidence score (same formula as GLF standardize)
    if {"hits", "misses", "track_status", "track_quality_score"}.issubset(tracks.columns):
        hit_term = np.clip(tracks["hits"].astype(float) / 8.0, 0, 1)
        miss_term = np.exp(-tracks["misses"].astype(float) / 3.0)
        status_term = np.where(tracks["track_status"].eq("CONFIRMED"), 1.0, 0.5)
        base = tracks["track_quality_score"].fillna(0.0).astype(float)
        tracks["track_confidence"] = np.clip(
            0.50 * base + 0.20 * hit_term + 0.15 * miss_term + 0.15 * status_term, 0, 1
        )
    else:
        tracks["track_confidence"] = 0.0

    # Canonical column ordering.  Keep extra columns at the end.
    first = [
        "timestamp_utc", "frame_id", "sensor_id", "track_id", "target_label", "label_basis",
        "coordinate_frame", "swath_orientation", "mount_location", "boresight_direction",
        "tilt_down_deg", "sonar_depth_m", "measurement_available",
        "x_pix", "y_pix", "x_pred_pix", "y_pred_pix",
        "speed_pixps", "heading_pix_deg",
        "Pxx_pix2", "Pxy_pix2", "Pyy_pix2", "Pvxvx_pix2ps2", "Pvyvy_pix2ps2",
        "track_confidence", "detection_score", "track_status", "hits", "misses", "age",
        "horizontal_xy_valid", "enu_valid", "enu_invalid_reason",
    ]
    cols = [c for c in first if c in tracks.columns] + [c for c in tracks.columns if c not in first]
    tracks = tracks[cols]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    tracks.to_csv(out_csv, index=False)

    summary = pd.read_csv(track_summary_csv) if track_summary_csv.exists() else pd.DataFrame()
    return tracks, summary


def main():
    """CLI: python -m src.standardize_dfki --set 1"""
    import argparse

    ap = argparse.ArgumentParser(description="Standardize DFKI pixel-coordinate tracks")
    ap.add_argument("--set", type=int, required=True, help="DFKI set number (1-4)")
    ap.add_argument("--dfki-output-root", type=Path,
                    default=OUTPUT_ROOT / "outputs_dfki",
                    help="Directory containing dfki_set_N/")
    ap.add_argument("--out", type=Path,
                    default=TWO_SONAR_ROOT / "glf_processing" / "outputs_dfki_standardized",
                    help="Output directory")
    ap.add_argument("--sensor-id", type=str, default="DFKI_932x514_imaging_sonar")
    args = ap.parse_args()

    raw_csv = args.dfki_output_root / f"dfki_set_{args.set}" / "tracks_raw.csv"
    summary_csv = args.dfki_output_root / f"dfki_set_{args.set}" / "track_summary_all.csv"
    out_csv = args.out / f"dfki_set_{args.set}_standardized.csv"
    df, summary = standardize_dfki_tracks(
        raw_csv, summary_csv, out_csv,
        sensor_cfg={"sensor_id": args.sensor_id,
                    "coordinate_frame": "dfki_pixel",
                    "horizontal_xy_valid": False, "enu_valid": False,
                    "enu_invalid_reason": "DFKI pixel coordinates — no real scale."}
    )
    print(f"  [set_{args.set}] standardized {len(df)} rows → {out_csv}")
    if not summary.empty:
        print(f"  summary tracks: {len(summary)}")
        for _, row in summary.iterrows():
            print(f"    → {row['track_id']}: hits={row['hits']}, "
                  f"disp={row['net_displacement_m']:.1f}px, qs={row['quality_score']:.3f}")


if __name__ == "__main__":
    main()
