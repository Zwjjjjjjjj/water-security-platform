"""Re-run evaluate_gt on all 4 sets and compare to baseline."""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_ROOT, SRC_DIR

sys.path.insert(0, str(SRC_DIR))
from run_dfki import evaluate_gt

OUT = Path(str(DFKI_OUTPUT_V14))
GT_DIR = Path(str(DFKI_ROOT))

sets = [
    ("set_1", "dfki_set_1", "set_1/tracking_sonar_coordinates_1.csv"),
    ("set_2", "dfki_set_2", "set_2/tracking_sonar_coordinates_2.csv"),
    ("set_3", "dfki_set_3", "set_3/tracking_sonar_coordinates_3.csv"),
    ("set_4", "dfki_set_4", "set_4/tracking_sonar_coordinates_4.csv"),
]

for set_name, out_dir, gt_rel in sets:
    out_path = OUT / out_dir
    gt_csv = GT_DIR / gt_rel
    if not (out_path / "tracks_raw.csv").exists():
        print(f"  [{set_name}] skip — no tracks_raw.csv")
        continue
    print(f"\n=== {set_name} ===")
    r = evaluate_gt(
        tracks_csv=out_path / "tracks_raw.csv",
        gt_csv=gt_csv,
        output_dir=out_path,
        set_name=set_name,
    )
    print(json.dumps(r, indent=2))
