"""对 v14 已有的 track_fragments.csv 应用修复逻辑 (写一份 v14_fixed 版).
修复点: track_id 之前是 `FRAG_{tr.id:03d}` (内部 KF id), 现在按 (start_frame,end_frame)
        回填为 selected 中的 public_id (PRIMARY_DIVER / CANDIDATE_XX)."""
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14
from pathlib import Path

SRC = Path(str(DFKI_OUTPUT_V14))
SETS = ["set_1", "set_2", "set_3", "set_4"]

for s in SETS:
    base = SRC / f"dfki_{s}"
    fr = pd.read_csv(base / "track_fragments.csv") if (base/"track_fragments.csv").exists() else None
    summ = pd.read_csv(base / "track_summary_all.csv") if (base/"track_summary_all.csv").exists() else None
    if fr is None or len(fr) == 0:
        print(f"[{s}] no fragments — skip")
        continue
    # primary-diver tie-breaker: 取出每条 summary 的 public_id + 帧范围
    pub_ranges = []  # (public_id, sf, ef)
    if summ is not None:
        for _, r in summ.iterrows():
            pub_ranges.append((r["track_id"], int(r["start_frame"]), int(r["end_frame"])))
    def map_pid(sf, ef):
        best_pid, best_ov = None, -1
        for pid, psf, pef in pub_ranges:
            ov = min(ef, pef) - max(sf, psf) + 1
            if ov > best_ov:
                best_ov = ov; best_pid = pid
        return best_pid if best_ov > 0 else None
    # rewrite
    out_rows = []
    for _, row in fr.iterrows():
        sf, ef = int(row["start_frame"]), int(row["end_frame"])
        mapped = map_pid(sf, ef)
        public_tid = mapped if mapped is not None else f"FRAG_{int(row['internal_id']):03d}"
        new = row.to_dict()
        new["track_id"]    = public_tid
        new["fragment_id"] = f"FRAG_{int(row['internal_id']):03d}"
        out_rows.append(new)
    out_df = pd.DataFrame(out_rows)
    out_path = base / "track_fragments_FIXED.csv"
    out_df.to_csv(out_path, index=False)
    print(f"\n[{s}] -> {out_path}")
    print(out_df[["track_id","fragment_id","internal_id","start_frame","end_frame","hits","quality_score"]].to_string(index=False))
