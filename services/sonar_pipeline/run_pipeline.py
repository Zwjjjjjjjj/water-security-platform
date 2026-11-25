#!/usr/bin/env python3
"""Unified entry: raw data → single-sonar tracking → association → fusion.

Real path (GLF / DFKI set_1..4):
    Stage 1 manifest → Stage 2 tracking → Stage 3 standardize
    → Stage 4 local tracks (A real + B/C/D synthetic, via e2e_bridge B-synthesis only)
    → Stage 5 associate (OFFICIAL TrackAssociation / NPartiteAssociator)
    → Stage 6 fuse      (OFFICIAL CovarianceWeightedFusion / InformationFusionN)
    → Stage 7/8 evaluate (e2e_evaluate + extended: RMSE_A/B/Fused, NEES, honesty audit)

Sim path (N virtual sonars):
    reference → n_sonar_simulator → local Kalman → N-way align/assoc/fuse
    (shares the SAME official engines as the real path)

Usage:
    python run_pipeline.py --mode real --datasets dfki_set_1 --stages 4,5,6,7
    python run_pipeline.py --mode real --datasets all --stages all
    python run_pipeline.py --mode sim --n-sonars 2 --experiment easy
    python run_pipeline.py --mode all
"""
from __future__ import annotations
import argparse, json, os, sys, time, traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sonar_pipeline.common import (
    load_pipeline_config, resolve_paths, DATASETS, dataset_kind, dfki_set_num,
    TRITECH_ROOT,
)
from sonar_pipeline import stages as ST

# Legacy cache roots used as read-only fallbacks for stage 2/3.
# Overridable so the pipeline can run against relocated datasets.
LEGACY_E2E_ROOT = Path(os.environ.get("LEGACY_E2E_ROOT", "/root/outputs_e2e"))


def parse_list(s: str):
    s = s.strip().lower()
    if s == "all":
        return None  # caller expands
    return [x.strip() for x in s.split(",") if x.strip()]


def _resolve_stage2_cache(dataset: str, ds_out: Path) -> tuple[Path | None, Path | None]:
    """Locate a cached stage-2 (single-sonar tracking) result.

    Search order — first hit wins:
      1. unified layout inside the current output root
      2. legacy ``outputs_e2e`` cache
      3. the Tritech project's own ``outputs`` / ``outputs_dfki*`` directories
      4. the DFKI v15 reference output (``dfki_output_v15``)

    The Tritech project ships only ``outputs/06_association`` in the monorepo,
    so path 3 resolves to nothing for DFKI.  The v15 directory is the one that
    actually holds a DFKI stage-2 result and is also the sim branch's reference
    track source, so it is searched last.
    """
    n = dfki_set_num(dataset)
    rel = Path("02_tracking") if dataset_kind(dataset) == "glf" else Path(f"dfki_set_{n}")
    cands: list[Path] = [
        ds_out / "stage_2_tracking" / rel / "tracks_raw.csv",
        LEGACY_E2E_ROOT / dataset / "stage_2_tracking" / rel / "tracks_raw.csv",
        TRITECH_ROOT / "outputs" / rel / "tracks_raw.csv",
        TRITECH_ROOT / "outputs_dfki" / rel / "tracks_raw.csv",
        TRITECH_ROOT / "outputs_dfki_v2" / rel / "tracks_raw.csv",
        TRITECH_ROOT / "outputs_dfki_v3" / rel / "tracks_raw.csv",
        Path(os.environ.get("DFKI_OUTPUT_V15", "/root/dfki_output_v15")) / rel / "tracks_raw.csv",
    ]
    for c in cands:
        if c.exists():
            summ = c.parent / c.name.replace("tracks_raw.csv", "track_summary_all.csv")
            return c, (summ if summ.exists() else None)
    return None, None


def run_real_dataset(dataset: str, stage_list: list[int] | None, cfg: dict,
                     out_root: Path, sensor_ids: list[str]) -> dict:
    ds_out = out_root / "real" / dataset
    ds_out.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*70}\n  REAL dataset={dataset} stages={stage_list}\n{'='*70}")
    summary: dict = {"dataset": dataset, "kind": dataset_kind(dataset),
                     "stages_run": [], "sensors": sensor_ids}
    t_stage: dict = {}
    try:
        # resolve cached / fresh stage-2/3 paths
        raw = summ = std_csv = None
        if stage_list is None or 1 in stage_list:
            t0 = time.time()
            ST.stage1_manifest(dataset, ds_out, cfg)
            t_stage[1] = time.time() - t0; summary["stages_run"].append(1)
        if stage_list is None or 2 in stage_list:
            t0 = time.time()
            raw, summ = ST.stage2_tracking(dataset, ds_out, cfg)
            t_stage[2] = time.time() - t0; summary["stages_run"].append(2)
        else:
            # reuse cache: new unified layout first, then legacy outputs_e2e
            raw, summ = _resolve_stage2_cache(dataset, ds_out)
            if raw is None:
                # No cache anywhere — recompute stage 2 from the raw dataset so
                # the run still produces a complete downstream result.
                print("  [stage 2] no cache found → recomputing from raw data")
                t0 = time.time()
                raw, summ = ST.stage2_tracking(dataset, ds_out, cfg)
                t_stage[2] = time.time() - t0
                summary["stages_run"].append(2)
            else:
                print(f"  [stage 2] cached → {raw}")
        if (stage_list is None or 3 in stage_list) and raw is not None:
            t0 = time.time()
            std_csv = ST.stage3_standardize(dataset, raw, summ, ds_out / "stage_3_standardized")
            t_stage[3] = time.time() - t0; summary["stages_run"].append(3)
        else:
            cands = ([ds_out / "stage_3_standardized" / "glf_standardized.csv",
                      LEGACY_E2E_ROOT / "glf" / "stage_3_standardized" / "glf_standardized.csv"]
                     if dataset_kind(dataset) == "glf" else
                     [ds_out / "stage_3_standardized" / f"{dataset}_standardized.csv",
                      LEGACY_E2E_ROOT / dataset / "stage_3_standardized" / f"{dataset}_standardized.csv"])
            for c in cands:
                if c.exists():
                    std_csv = c; print(f"  [stage 3] cached → {c}"); break
            if std_csv is None:
                if raw is None:
                    raise FileNotFoundError(
                        f"stage 3 standardized output not found for {dataset}; "
                        f"looked in {[str(c) for c in cands]}"
                    )
                # A cached stage-2 result was found but its standardized output
                # is missing (e.g. the cache lives outside the unified layout).
                # Standardize from the cached raw tracks instead of failing.
                print("  [stage 3] no cached standardized output → standardizing from stage 2")
                t0 = time.time()
                std_csv = ST.stage3_standardize(dataset, raw, summ, ds_out / "stage_3_standardized")
                t_stage[3] = time.time() - t0
        need4567 = (stage_list is None) or any(s in stage_list for s in (4, 5, 6, 7, 8))
        if raw is None and need4567:
            raise FileNotFoundError(
                f"stage 2 tracking output not found for {dataset}; "
                f"cannot run stages 4-8 without tracks_raw.csv"
            )
        if need4567 and std_csv is not None:
            t0 = time.time()
            stage4_dir = ds_out / "stage_4_local_tracks"
            tracks_map = ST.stage4_build_local_tracks(dataset, std_csv, ds_out, cfg, sensor_ids)
            if stage_list is None or 4 in stage_list:
                summary["stages_run"].append(4)
            paths5 = ST.stage5_associate(stage4_dir, tracks_map, cfg, sensor_ids)
            if stage_list is None or 5 in stage_list:
                summary["stages_run"].append(5)
            fused_csv = ST.stage6_fuse(stage4_dir, tracks_map, paths5["local_to_global"], cfg, sensor_ids)
            if stage_list is None or 6 in stage_list:
                summary["stages_run"].append(6)
            t_stage["4-6"] = time.time() - t0
            # stage 7/8 evaluation
            if stage_list is None or 7 in stage_list or 8 in stage_list:
                ev = ST.stage7_8_evaluate(dataset, ds_out, raw, summ, std_csv,
                                          stage4_dir, fused_csv, paths5, cfg)
                summary["evaluation"] = ev
                summary["stages_run"] += [s for s in (7, 8) if (stage_list is None or s in stage_list)]
            import pandas as pd
            fused = pd.read_csv(fused_csv) if fused_csv.exists() else pd.DataFrame()
            summary["n_fused"] = int(len(fused))
            summary["fusion_modes"] = fused["fusion_mode"].value_counts().to_dict() if len(fused) else {}
    except Exception as e:
        traceback.print_exc()
        summary["error"] = f"{type(e).__name__}: {e}"
    summary["t_stage"] = t_stage
    (ds_out / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def run_sim(cfg: dict, out_root: Path, n_sonars: int, experiment: str) -> dict:
    from sonar_pipeline.sim_branch import run_sim_branch
    # default reference: dfki_set_1 PRIMARY track (real single-sonar output)
    ref_cands = [out_root / "real" / "dfki_set_1" / "stage_2_tracking" / "dfki_set_1" / "tracks_raw.csv",
                 LEGACY_E2E_ROOT / "dfki_set_1" / "stage_2_tracking" / "dfki_set_1" / "tracks_raw.csv",
                 TRITECH_ROOT / "outputs_dfki" / "dfki_set_1" / "tracks_raw.csv",
                 Path(os.environ.get("DFKI_OUTPUT_V15", "/root/dfki_output_v15")) / "dfki_set_1" / "tracks_raw.csv"]
    ref = next((c for c in ref_cands if c.exists()), None)
    if ref is None:
        return {"error": "no reference tracks_raw.csv found for sim branch"}
    out_dir = out_root / "sim" / experiment / f"n{n_sonars}"
    print(f"\n{'='*70}\n  SIM N={n_sonars} experiment={experiment} ref={ref}\n{'='*70}")
    try:
        metrics = run_sim_branch(cfg, out_dir, ref, n_sonars=n_sonars, experiment=experiment)
        return {"n_sonars": n_sonars, "experiment": experiment, "metrics": metrics}
    except Exception as e:
        traceback.print_exc()
        return {"n_sonars": n_sonars, "experiment": experiment, "error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="real", choices=["real", "sim", "all"])
    ap.add_argument("--datasets", default="all")
    ap.add_argument("--stages", default="all")
    ap.add_argument("--n-sonars", type=int, default=2)
    ap.add_argument("--experiment", default="easy", choices=["easy", "difficult", "default"])
    ap.add_argument("--config", default=str(ROOT / "config" / "pipeline.json"))
    ap.add_argument("--out", default=os.environ.get("PIPELINE_OUT", str(ROOT.parent.parent / "outputs")))
    ap.add_argument("--sensors", default="SONAR_A,SONAR_B")
    args = ap.parse_args()

    cfg = load_pipeline_config(args.config)
    out_root = Path(args.out); out_root.mkdir(parents=True, exist_ok=True)
    results = []
    if args.mode in ("real", "all"):
        dsels = list(DATASETS) if args.datasets.strip().lower() == "all" else parse_list(args.datasets)
        stgs = None if args.stages.strip().lower() == "all" else sorted({int(x) for x in args.stages.split(",") if x.strip()})
        sensors = [s.strip() for s in args.sensors.split(",") if s.strip()]
        for d in dsels:
            results.append(run_real_dataset(d, stgs, cfg, out_root, sensors))
    if args.mode in ("sim", "all"):
        results.append(run_sim(cfg, out_root, args.n_sonars, args.experiment))
    md = ["# sonar_pipeline run report", "", f"mode={args.mode} out={out_root}", "",
          "| entry | detail |", "|---|---|"]
    for r in results:
        md.append(f"| `{r.get('dataset', 'sim')}` | `{json.dumps({k: v for k, v in r.items() if k in ('stages_run','n_fused','fusion_modes','n_sonars','experiment','error')}, default=str)}` |")
    (out_root / "PIPELINE_REPORT.md").write_text("\n".join(md))
    (out_root / "pipeline_summary.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {(out_root/'PIPELINE_REPORT.md')} + pipeline_summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
