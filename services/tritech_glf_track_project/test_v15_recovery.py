"""Quick test: re-run evaluate_gt with the v15 Lost-Track Oracle Recovery on set_2."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_ROOT, SRC_DIR

sys.path.insert(0, str(SRC_DIR))
from run_dfki import evaluate_gt

OUT = Path(str(DFKI_OUTPUT_V14 / "dfki_set_2"))
GT  = Path(str(DFKI_ROOT / "set_2" / "tracking_sonar_coordinates_2.csv"))

result = evaluate_gt(
    tracks_csv=OUT / "tracks_raw.csv",
    gt_csv=GT,
    output_dir=OUT,
    set_name="set_2",
)
print(result)

# 检查恢复情况
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_ROOT, SRC_DIR
ev = pd.read_csv(OUT / "gt_evaluation.csv")
print("\n=== status 分布 ===")
print(ev["status"].value_counts())
print("\n=== LOST_RECOVERED 帧误差 ===")
rec = ev[ev["status"] == "LOST_RECOVERED"]
if len(rec) > 0:
    print(rec["error_px"].describe())
