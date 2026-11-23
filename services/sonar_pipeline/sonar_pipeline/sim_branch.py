"""Sim branch: reference track → N virtual sonars → local Kalman → assoc → fuse.

Wraps two_sonar_association/src/n_sonar_simulator + run_n_sonar_association.run_n_sonar_pipeline
so the sim path and the real path share ONE association/fusion implementation
(sonar_pipeline.association_fusion). No duplicated Hungarian/fusion code.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

import pandas as pd

from .common import TWO_SONAR_SRC

sys.path.insert(0, str(TWO_SONAR_SRC))

from n_sonar_simulator import run_simulation  # noqa: E402
from simulate_two_sonars import load_reference_track_from_dfki  # noqa: E402
from run_n_sonar_association import (  # noqa: E402
    run_n_sonar_pipeline, build_local_tracks,
)


def run_sim_branch(cfg: Dict, out_dir: Path, reference_csv: Path,
                   n_sonars: int = 2, experiment: str = "easy",
                   seed: int | None = None) -> Dict:
    """Full sim pipeline. Returns metrics dict (fused RMSE, single RMSE, clusters)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = seed if seed is not None else int(cfg.get("random_seed", 42))
    reference = load_reference_track_from_dfki(str(reference_csv), "PRIMARY_DIVER")

    sim_dir = out_dir / "sim"
    sim_res = run_simulation(
        config=cfg, output_dir=sim_dir,
        reference_tracks={"T1": reference}, seed=seed,
        experiment_type=experiment,
    )

    sonars_full = cfg["sonars"][:n_sonars]
    measurements_by_sensor: Dict[str, pd.DataFrame] = {}
    for s in sonars_full:
        s_id, s_short = s["id"], s["short"]
        meas = pd.read_csv(sim_dir / f"sonar_{s_short}_measurements.csv")
        dist_path = sim_dir / f"{s_short}_distractors.csv"
        if dist_path.exists():
            dist = pd.read_csv(dist_path)
            if "x_common" not in dist.columns and "x" in dist.columns:
                dist = dist.rename(columns={"x": "x_common", "y": "y_common"})
            dist["ref_label"] = "T1"
            measurements_by_sensor[s_id] = pd.concat([meas, dist], ignore_index=True, sort=False)
        else:
            measurements_by_sensor[s_id] = meas

    ref_df = pd.DataFrame({
        "timestamp": reference.timestamps,
        "x_ref": reference.x, "y_ref": reference.y,
    })
    metrics = run_n_sonar_pipeline(
        config=cfg, output_dir=out_dir / f"n{n_sonars}",
        measurements_by_sensor=measurements_by_sensor,
        reference=ref_df, n_sonars=n_sonars,
    )
    metrics["sim"] = {k: v for k, v in sim_res.items()}
    return metrics


def run_sim_from_standardized(cfg: Dict, out_dir: Path,
                              standardized_csv: Path, n_sonars: int = 2) -> Dict:
    """Alternative sim entry: derive reference from a standardized real track.

    Uses the longest track in standardized_csv as ReferenceTrack, so sim can run
    even without a DFKI tracks_raw.csv.
    """
    import numpy as np
    sys.path.insert(0, str(TWO_SONAR_SRC))
    from simulate_two_sonars import ReferenceTrack
    df = pd.read_csv(standardized_csv)
    # pick longest track
    tid = df["track_id"].value_counts().idxmax()
    sub = df[df["track_id"] == tid].sort_values("timestamp").reset_index(drop=True)
    xcol = next(c for c in ("x", "x_pix", "x_common", "fan_u_filt_m") if c in sub.columns)
    ycol = next(c for c in ("y", "y_pix", "y_common", "fan_v_filt_m") if c in sub.columns)
    tcol = "timestamp" if "timestamp" in sub.columns else None
    if tcol is None:
        ts = pd.to_datetime(sub["timestamp_utc"], utc=True, errors="coerce")
        t = (ts - ts.iloc[0]).dt.total_seconds().values
    else:
        t = sub[tcol].values.astype(float) - float(sub[tcol].values[0])
    ref = ReferenceTrack(
        timestamps=np.asarray(t, dtype=float),
        x=np.asarray(sub[xcol], dtype=float),
        y=np.asarray(sub[ycol], dtype=float),
        track_id="T1",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    sim_dir = out_dir / "sim"
    sim_res = run_simulation(config=cfg, output_dir=sim_dir,
                             reference_tracks={"T1": ref}, seed=int(cfg.get("random_seed", 42)),
                             experiment_type="default")
    measurements_by_sensor = {}
    for s in cfg["sonars"][:n_sonars]:
        measurements_by_sensor[s["id"]] = pd.read_csv(sim_dir / f"sonar_{s['short']}_measurements.csv")
    ref_df = pd.DataFrame({"timestamp": ref.timestamps, "x_ref": ref.x, "y_ref": ref.y})
    return run_n_sonar_pipeline(config=cfg, output_dir=out_dir / f"n{n_sonars}",
                                measurements_by_sensor=measurements_by_sensor,
                                reference=ref_df, n_sonars=n_sonars)
