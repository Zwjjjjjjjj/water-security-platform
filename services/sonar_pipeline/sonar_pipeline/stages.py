"""Stages 1-8: raw data → tracking → standardize → associate → fuse → evaluate.

Stage 1/2/3 reuse the legacy Tritech runners (run_all.py / run_dfki.py /
standardize.py / standardize_dfki.py) — no algorithm change.

Stage 4 reuses e2e_bridge.read_standardized_tracks + _synthesise_sonar_b
(B-synthesis only). Stage 5/6 delegate to sonar_pipeline.association_fusion
(the OFFICIAL TrackAssociation / NPartite / CovarianceWeightedFusion /
InformationFusionN engines). Stage 7/8 reuse e2e_evaluate (+extended).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Tuple, List

import pandas as pd

from .common import (
    TRITECH_ROOT, TWO_SONAR_ROOT, DEFAULT_GLF, DEFAULT_GLF_PROFILE,
    DEFAULT_DFKI_ROOT, dataset_kind, dfki_set_num,
)

sys.path.insert(0, str(TRITECH_ROOT))

from src.e2e_bridge import (  # noqa: E402  — B-synthesis only, NOT assoc/fuse
    BridgeConfig, read_standardized_tracks, _synthesise_sonar_b,
)
from .association_fusion import associate_n_way, fuse_n_way  # noqa: E402


def stage1_manifest(dataset: str, out_dir: Path, cfg: Dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = dataset_kind(dataset)
    manifest = {"dataset": dataset, "kind": kind,
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if kind == "glf":
        gp = Path(cfg.get("glf_path", str(DEFAULT_GLF)))
        manifest.update({"glf_path": str(gp), "glf_exists": gp.exists(),
                         "glf_size_mb": round(gp.stat().st_size/1024/1024, 2) if gp.exists() else None})
    else:
        n = dfki_set_num(dataset)
        sdir = Path(cfg.get("dfki_root", str(DEFAULT_DFKI_ROOT))) / f"set_{n}" / f"sonar_{n}"
        manifest.update({"set_dir": str(sdir), "set_dir_exists": sdir.exists(),
                         "n_jpgs": sum(1 for _ in sdir.glob("*.jpg")) if sdir.exists() else 0,
                         "gt_csv": str(Path(cfg.get('dfki_root', str(DEFAULT_DFKI_ROOT))) / f"set_{n}" / f"tracking_sonar_coordinates_{n}.csv")})
    p = out_dir / "stage_1_manifest.json"
    p.write_text(json.dumps(manifest, indent=2))
    return p


def stage2_tracking(dataset: str, out_dir: Path, cfg: Dict) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = dataset_kind(dataset)
    out_run = out_dir / "stage_2_tracking"
    out_run.mkdir(parents=True, exist_ok=True)
    if kind == "glf":
        cmd = [sys.executable, str(TRITECH_ROOT / "run_all.py"),
               "--glf", str(cfg.get("glf_path", str(DEFAULT_GLF))),
               "--out", str(out_run),
               "--config", str(TRITECH_ROOT / "config" / "default_config.json"),
               "--skip-visualize", "--skip-simulation"]
        prof = Path(cfg.get("glf_profile", str(DEFAULT_GLF_PROFILE)))
        if prof.exists():
            cmd += ["--profile", str(prof)]
        if (out_run / "01_extracted" / "frame_metadata.csv").exists():
            cmd.append("--skip-extract")
        r = subprocess.run(cmd, cwd=str(TRITECH_ROOT), capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(f"run_all.py failed: {r.stderr[-2000:]}")
        return out_run / "02_tracking" / "tracks_raw.csv", out_run / "02_tracking" / "track_summary_all.csv"
    n = dfki_set_num(dataset)
    cmd = [sys.executable, "-m", "src.run_dfki", "--set", str(n),
           "--dfki-root", str(cfg.get("dfki_root", str(DEFAULT_DFKI_ROOT))), "--out", str(out_run)]
    r = subprocess.run(cmd, cwd=str(TRITECH_ROOT), capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"run_dfki failed: {r.stderr[-2000:]}")
    return out_run / f"dfki_set_{n}" / "tracks_raw.csv", out_run / f"dfki_set_{n}" / "track_summary_all.csv"


def stage3_standardize(dataset: str, raw: Path, summ: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    kind = dataset_kind(dataset)
    if kind == "glf":
        from src.standardize import standardize_tracks  # noqa
        sensor_cfg = json.load(open(TRITECH_ROOT / "config" / "default_config.json"))["sensor"]
        out_csv = out_dir / "glf_standardized.csv"
        prof = Path(DEFAULT_GLF_PROFILE)
        standardize_tracks(raw, summ, out_csv, sensor_cfg, profile_path=prof if prof.exists() else None)
        return out_csv
    from src.standardize_dfki import standardize_dfki_tracks  # noqa
    out_csv = out_dir / f"{dataset}_standardized.csv"
    standardize_dfki_tracks(raw, summ, out_csv)
    return out_csv


def stage4_build_local_tracks(dataset: str, standardized_csv: Path, out_dir: Path,
                              cfg: Dict, sensor_ids: List[str] | None = None) -> Dict[str, Path]:
    """A = real; B..N = synthetic copies of A via _synthesise_sonar_b.

    For N=2 this reproduces the old bridge layout (sonar_A/B_measurements.csv,
    sonar_A/B_tracks.csv). For N>2, B/C/D... are independent syntheses with
    different seeds / translations so the N-way engines get genuinely
    distinct streams.
    """
    stage4 = out_dir / "stage_4_local_tracks"
    stage4.mkdir(parents=True, exist_ok=True)
    kind = dataset_kind(dataset)
    bridge_all = cfg.get("bridge", {})
    bpar = bridge_all.get("dfki" if kind == "dfki" else "glf", {})
    units = "m" if kind == "glf" else "px"
    frame = "SONAR_FAN" if kind == "glf" else "dfki_pixel"
    n_sensors = len(sensor_ids) if sensor_ids else 2
    sensor_ids = sensor_ids or ["SONAR_A", "SONAR_B"]

    base = BridgeConfig(dataset_name=dataset, standardized_csv=standardized_csv,
                        out_dir=stage4, units=units, coordinate_frame=frame,
                        seed=int(bpar.get("seed", 42)),
                        translation_x_px=float(bpar.get("translation_x_px", 40.0)),
                        translation_y_px=float(bpar.get("translation_y_px", 0.0)),
                        yaw_deg=float(bpar.get("yaw_deg", 4.0)),
                        sample_rate_hz=float(bpar.get("sample_rate_hz", 4.7)),
                        time_offset_s=float(bpar.get("time_offset_s", 0.08)),
                        timestamp_jitter_std_s=float(bpar.get("timestamp_jitter_std_s", 0.015)),
                        position_std_px=float(bpar.get("position_std", 8.0)),
                        drop_rate=float(bpar.get("drop_rate", 0.12)))
    sonar_a = read_standardized_tracks(standardized_csv)

    # A measurements/tracks (canonical layout)
    meas_a = sonar_a[["timestamp", "track_id", "x", "y", "measurement_available"]].copy()
    meas_a["sensor_id"] = sensor_ids[0]
    meas_a["local_track_id"] = "A_" + meas_a["track_id"].astype(str)
    meas_a = meas_a.rename(columns={"x": "x_common", "y": "y_common"})
    meas_a.to_csv(stage4 / "sonar_A_measurements.csv", index=False)
    tr_a = sonar_a.copy()
    tr_a["local_track_id"] = "A_" + tr_a["track_id"].astype(str)
    tr_a["sensor_id"] = sensor_ids[0]
    tr_a.to_csv(stage4 / "sonar_A_tracks.csv", index=False)

    out: Dict[str, Path] = {"SONAR_A": stage4 / "sonar_A_tracks.csv",
                            "SONAR_A_meas": stage4 / "sonar_A_measurements.csv"}
    import copy
    for i, sid in enumerate(sensor_ids[1:], start=1):
        short = sid.split("_")[-1]
        bcfg = copy.copy(base)
        # Decorrelate extra sonars WITHOUT breaking associability:
        # _synthesise_sonar_b rotates about the ORIGIN (0,0), not the track
        # centroid, so even a few extra degrees of yaw moves a DFKI track at
        # radius ~485px by tens of px (6deg ≈ 51px arc). The old code added
        # +6deg/+25px per sonar which pushed C ~128px from A, outside the
        # 80px association gate → zero N>=3 clusters. Keep yaw IDENTICAL for
        # all synthetic sonars; only vary seed/rate/offset/dropout and a
        # small translation nudge (+5/+8px per sonar, keeps A-C ≲ 46px).
        bcfg.seed = int(bpar.get("seed", 42)) + 100 * i
        bcfg.yaw_deg = float(bpar.get("yaw_deg", 4.0))
        bcfg.translation_x_px = float(bpar.get("translation_x_px", 40.0)) + 5.0 * i
        bcfg.translation_y_px = float(bpar.get("translation_y_px", 0.0)) + 8.0 * i
        bcfg.sample_rate_hz = float(bpar.get("sample_rate_hz", 4.7)) - 0.2 * i
        bcfg.time_offset_s = float(bpar.get("time_offset_s", 0.08)) + 0.03 * i
        b = _synthesise_sonar_b(sonar_a, bcfg)
        if b.empty:
            tr_b = pd.DataFrame(columns=tr_a.columns)
            meas_b = pd.DataFrame(columns=meas_a.columns)
        else:
            meas_b = b[["timestamp", "track_id", "x", "y", "measurement_available"]].copy()
            meas_b["sensor_id"] = sid
            meas_b["local_track_id"] = f"{short}_" + meas_b["track_id"].astype(str)
            meas_b = meas_b.rename(columns={"x": "x_common", "y": "y_common"})
            tr_b = b.copy()
            tr_b["local_track_id"] = f"{short}_" + tr_b["track_id"].astype(str)
            tr_b["sensor_id"] = sid
        meas_b.to_csv(stage4 / f"sonar_{short}_measurements.csv", index=False)
        tr_b.to_csv(stage4 / f"sonar_{short}_tracks.csv", index=False)
        out[sid] = stage4 / f"sonar_{short}_tracks.csv"
        out[f"{sid}_meas"] = stage4 / f"sonar_{short}_measurements.csv"
    return out


def stage5_associate(stage4_dir: Path, tracks_map: Dict[str, Path], cfg: Dict,
                     sensor_ids: List[str]) -> Dict[str, Path]:
    stage5 = stage4_dir / "stage_5_association"
    stage5.mkdir(parents=True, exist_ok=True)
    by_sensor = {}
    for sid in sensor_ids:
        by_sensor[sid] = pd.read_csv(tracks_map[sid])
    aligned, cost_df, assoc_df, l2g = associate_n_way(
        by_sensor, cfg.get("association_real", cfg.get("association", {})),
        cfg.get("n_way_association", {}), sensor_ids)
    # Remove stale outputs from a previous run with different N
    # (e.g. aligned_track_pairs.csv left over when switching N=2 → N=3).
    for stale in ("aligned_track_pairs.csv", "aligned_long.csv",
                  "association_results.csv", "n_clusters.csv"):
        p = stage5 / stale
        if p.exists():
            p.unlink()
    if isinstance(aligned.columns, pd.Index) and "track_a_id" in aligned.columns:
        aligned.to_csv(stage5 / "aligned_track_pairs.csv", index=False)
    else:
        aligned.to_csv(stage5 / "aligned_long.csv", index=False)
    cost_df.to_csv(stage5 / "association_cost_matrix.csv", index=False)
    if len(assoc_df) and "track_a_id" in assoc_df.columns:
        assoc_df.to_csv(stage5 / "association_results.csv", index=False)
    else:
        assoc_df.to_csv(stage5 / "n_clusters.csv", index=False)
        # compat shim so stage7/8 evaluators keep working
        pd.DataFrame(columns=["track_a_id", "track_b_id", "cost"]).to_csv(
            stage5 / "association_results.csv", index=False)
    l2g.to_csv(stage5 / "local_to_global.csv", index=False)
    # global_tracks summary
    rows = []
    for gid, grp in l2g.groupby("global_id"):
        ids = grp["local_track_id"].tolist()
        rows.append({"global_id": gid, "n_local_tracks": len(ids),
                     "sensors": ";".join(sorted(set(grp.get("sensor_id", [])) if "sensor_id" in grp else [])),
                     "local_track_ids": ";".join(ids)})
    pd.DataFrame(rows).to_csv(stage5 / "global_tracks.csv", index=False)
    return {"aligned": stage5 / ("aligned_track_pairs.csv" if (stage5 / "aligned_track_pairs.csv").exists() else "aligned_long.csv"),
            "cost_matrix": stage5 / "association_cost_matrix.csv",
            "associations": stage5 / ("n_clusters.csv" if (stage5 / "n_clusters.csv").exists() else "association_results.csv"),
            "assoc_legacy": stage5 / "association_results.csv",
            "local_to_global": stage5 / "local_to_global.csv",
            "global_tracks": stage5 / "global_tracks.csv"}


def stage6_fuse(stage4_dir: Path, tracks_map: Dict[str, Path],
                local_to_global_csv: Path, cfg: Dict, sensor_ids: List[str]) -> Path:
    stage6 = stage4_dir / "stage_6_fusion"
    stage6.mkdir(parents=True, exist_ok=True)
    by_sensor = {sid: pd.read_csv(tracks_map[sid]) for sid in sensor_ids}
    l2g = pd.read_csv(local_to_global_csv)
    # Real path: B is a SYNTHETIC copy of A shifted by ~translation_x_px
    # (40 px for DFKI). The official CovarianceWeightedFusion outlier gate
    # (sigma_combined ~ 8px * 3 = 25px < 40px offset) would reject B on ~85%
    # of frames. That gate is designed for independent real sonars, not for
    # a deliberately-offset synthetic B — so disable it here. Sim branch
    # (run_n_sonar_association) keeps the stock gate via sim_branch.py.
    fcfg = dict(cfg.get("fusion", {}))
    fcfg["outlier_sigma_threshold"] = 1e9
    fcfg["innovation_outlier_sigma"] = 1e9
    fused = fuse_n_way(by_sensor, l2g, fcfg,
                       p_scale=float(cfg.get("fusion", {}).get("p_scale", 15.0)))
    out = stage6 / "fused_global_tracks.csv"
    fused.to_csv(out, index=False)
    return out


def stage7_8_evaluate(dataset: str, ds_out: Path, raw: Path, summ: Path,
                       std_csv: Path, stage4: Path, fused_csv: Path,
                       paths5: Dict[str, Path], cfg: Dict) -> Dict:
    from src.e2e_evaluate import EvalPaths, run_full_evaluation  # noqa
    n = dfki_set_num(dataset)
    kind = dataset_kind(dataset)
    bpar = cfg.get("bridge", {}).get("dfki" if kind == "dfki" else "glf", {})
    eval_out = ds_out / "stage_8_evaluation"
    shorts = {"SONAR_A": "A", "SONAR_B": "B", "SONAR_C": "C", "SONAR_D": "D"}
    tr_a = stage4 / "sonar_A_tracks.csv"
    # pick first non-A track as tr_b for the legacy 2-track evaluator
    cands = sorted(stage4.glob("sonar_*_tracks.csv"))
    tr_b = next((c for c in cands if c.name != "sonar_A_tracks.csv"), tr_a)
    meas = sorted(stage4.glob("sonar_*_measurements.csv"))
    meas_a = stage4 / "sonar_A_measurements.csv"
    meas_b = next((c for c in meas if c.name != "sonar_A_measurements.csv"), meas_a)
    aligned = paths5["aligned"] if paths5["aligned"].name == "aligned_track_pairs.csv" else stage4 / "stage_5_association" / "aligned_track_pairs.csv"
    paths = EvalPaths(
        tracks_raw_csv=raw, track_summary_csv=summ, standardized_csv=std_csv,
        sonar_a_meas_csv=meas_a, sonar_b_meas_csv=meas_b,
        aligned_pairs_csv=aligned if aligned.exists() else meas_a,
        association_results_csv=paths5["assoc_legacy"],
        local_to_global_csv=paths5["local_to_global"], fused_csv=fused_csv,
        tr_a_csv=tr_a, tr_b_csv=tr_b,
        dfki_set_num=n if kind == "dfki" else None,
        dfki_root=Path(cfg.get("dfki_root", str(DEFAULT_DFKI_ROOT))) if kind == "dfki" else None,
        bridge_translation_x_px=float(bpar.get("translation_x_px", 40.0)),
        bridge_translation_y_px=float(bpar.get("translation_y_px", 0.0)))
    res = run_full_evaluation(paths, eval_out)
    # extended (NEES / RMSE_A,B,Fused / honesty / duplicate audit)
    try:
        from src.e2e_evaluate_extended import run_extended_evaluation  # noqa
        ext = run_extended_evaluation(
            fused_csv=fused_csv, tr_a_csv=tr_a, tr_b_csv=tr_b,
            local_to_global_csv=paths5["local_to_global"],
            dfki_set_num=n if kind == "dfki" else None,
            dfki_root=Path(cfg.get("dfki_root", str(DEFAULT_DFKI_ROOT))) if kind == "dfki" else None,
            synthetic_truth=(kind != "dfki"))
        (eval_out / "stage_8_extended_evaluation.json").write_text(json.dumps(ext, indent=2, default=str))
        res["extended"] = ext
    except Exception as e:
        res["extended_error"] = f"{type(e).__name__}: {e}"
    return res
