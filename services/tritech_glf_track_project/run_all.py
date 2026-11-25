#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path

from src.extract_glf import export_glf
from src.detect_track import run_tracking
from src.standardize import standardize_tracks
from src.visualize import visualize_tracks
from src.simulate_second_sonar import simulate_second_sonar
from src.associate_tracks import associate_two_single_tracks


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Gemini GLF -> target tracks -> second-sonar simulation")
    ap.add_argument("--glf", required=True, type=Path, help="Input Gemini .glf file")
    ap.add_argument("--out", type=Path, default=root/"outputs", help="Output directory")
    ap.add_argument("--config", type=Path, default=root/"config"/"default_config.json")
    ap.add_argument("--profile", type=Path, default=None, help="Optional experiment-event profile for labeling")
    ap.add_argument("--skip-extract", action="store_true", help="Reuse existing 01_extracted files")
    ap.add_argument("--skip-visualize", action="store_true")
    ap.add_argument("--skip-simulation", action="store_true")
    ap.add_argument("--with-association", action="store_true", help="Also validate A/B association")
    args = ap.parse_args()

    cfg = load_json(args.config)
    out = args.out.resolve(); out.mkdir(parents=True, exist_ok=True)
    extracted = out/"01_extracted"
    tracking = out/"02_tracking"
    standardized_dir = out/"03_standardized"
    viz = out/"04_visualization"
    sim = out/"05_second_sonar_simulation"
    assoc = out/"06_association"

    print("[1/6] GLF extraction with TritechFiles")
    if not args.skip_extract:
        export_glf(args.glf, extracted, root)
    elif not (extracted/"frame_metadata.csv").exists():
        raise FileNotFoundError("--skip-extract specified but extracted files are missing")

    print("[2/6] Background suppression, detection and Kalman tracking")
    raw_tracks, summary = run_tracking(
        extracted, tracking, cfg["tracking"],
        profile_path=args.profile
    )
    if raw_tracks.empty:
        raise RuntimeError("No moving tracks selected; tune tracking.detection_threshold/gates in config.")

    print("[3/6] SONAR_FAN standardization")
    standardized_dir.mkdir(parents=True, exist_ok=True)
    std_csv = standardized_dir/"standardized_sonar_tracks.csv"
    std, mapping = standardize_tracks(
        tracking/"tracks_raw.csv",
        tracking/"track_summary_all.csv",
        std_csv,
        cfg["sensor"],
        args.profile
    )
    print("    label mapping:", mapping if mapping else "none")

    if not args.skip_visualize:
        print("[4/6] Track visualization")
        visualize_tracks(std_csv, viz)
    else:
        print("[4/6] Visualization skipped")

    if not args.skip_simulation:
        print("[5/6] Simulated second sonar")
        a_csv, b_csv, source_tid = simulate_second_sonar(std_csv, sim, cfg["simulation"])
        print("    source track:", source_tid)
        if args.with_association:
            print("[6/6] Track-to-track association validation")
            _, s = associate_two_single_tracks(a_csv, b_csv, assoc, cfg["association"])
            print(s.to_string(index=False))
        else:
            print("[6/6] Association skipped (enable with --with-association)")
    else:
        print("[5/6] Simulation skipped")
        print("[6/6] Association skipped")

    print("\nDone. Output:", out)


if __name__ == "__main__":
    main()
