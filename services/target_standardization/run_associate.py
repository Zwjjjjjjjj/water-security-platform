#!/usr/bin/env python3
"""航迹关联命令行入口。

对一个对齐输出目录做三组配对：radar-ais / optical-ais / radar-optical，
输出关联对 + 落选原因 + 真值评估（MMSI 一致性）。

用法：

    python3 run_associate.py --alignment-dir output/alignment/mtdsp2_yantai_optical
    python3 run_associate.py --alignment-dir output/alignment/mtdsp_yantai_optical --output-dir output/association/mtdsp_yantai_optical
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from association.associate import (  # noqa: E402
    GATE_OPTICAL_AIS_M,
    GATE_RADAR_AIS_M,
    GATE_RADAR_OPTICAL_M,
    OPTICAL_BIAS_DEG,
    RADAR_BIAS_DEG,
    associate_pair,
    load_candidates,
)
from association.timebase import check_timebase  # noqa: E402


def _mmsi_of(track_id: str) -> str:
    """从航迹号提取 MMSI（能提取才返回，否则返回空）。

    - ais:<mmsi> / radar:<mmsi> / optical:<mmsi>@Dev → <mmsi>
    - radar:1（RT）/ radar_reconstructed:R<n> / optical:optical_det_* → ''
    """
    name = track_id.split(":", 1)[-1] if ":" in track_id else track_id
    name = name.split("@")[0]
    if name.startswith("R"):
        return ""
    if name.startswith("optical_det"):
        return ""
    if name.isdigit() and len(name) == 9 and name.startswith("41"):
        return name
    if name == "1":
        return ""
    return ""


def _evaluate(pairs, kind):
    tp = fp = 0
    for p in pairs:
        a = _mmsi_of(p["track_a"])
        b = _mmsi_of(p["track_b"])
        if a and b:
            if a == b:
                tp += 1
            else:
                fp += 1
        # 至少一端无 MMSI（如重建航迹/RT/检测轨）：不计入 MMSI 精度，
        # 由人工/检测级真值另评
    return {"kind": kind, "n_pairs": len(pairs), "mmsi_tp": tp, "mmsi_fp": fp}


def main() -> int:
    parser = argparse.ArgumentParser(description="跨传感器航迹关联")
    parser.add_argument("--alignment-dir", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--min-points", type=int, default=3)
    parser.add_argument("--no-reconstructed", action="store_true")
    parser.add_argument("--optical-bias-deg", type=float, default=OPTICAL_BIAS_DEG)
    parser.add_argument("--radar-bias-deg", type=float, default=RADAR_BIAS_DEG)
    parser.add_argument("--no-bias-correction", action="store_true")
    args = parser.parse_args()

    alignment_dir = args.alignment_dir
    out_dir = args.output_dir or os.path.join(
        "output", "association", os.path.basename(alignment_dir.rstrip("/"))
    )
    os.makedirs(out_dir, exist_ok=True)

    # 1. 时间基准断言（只检查实际存在的传感器；无光电场景不要求光电）
    aligned_path = os.path.join(alignment_dir, "aligned_records.csv")
    timebase = {"comparable": None}
    if os.path.exists(aligned_path):
        with open(aligned_path, encoding="utf-8", newline="") as handle:
            records = list(csv.DictReader(handle))
        present = sorted({r.get("source_type") for r in records if r.get("source_type")})
        required = [s for s in ("radar", "ais", "optical") if s in present]
        timebase = check_timebase(records, required_sensors=required or ["radar", "ais"])
        if not timebase["comparable"]:
            print(
                json.dumps(
                    {"error": "时间不可比", "reasons": timebase["reasons"]},
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 3

    # 2. 加载候选
    cands = load_candidates(
        alignment_dir,
        min_points=args.min_points,
        use_reconstructed=not args.no_reconstructed,
    )

    results = {}
    assoc_kwargs = {
        "optical_bias_deg": args.optical_bias_deg,
        "radar_bias_deg": args.radar_bias_deg,
        "apply_bias_correction": not args.no_bias_correction,
    }
    for label, ta, tb, gate, m2o in [
        ("radar_ais", cands["radar"], cands["ais"], GATE_RADAR_AIS_M, True),
        ("optical_ais", cands["optical"], cands["ais"], GATE_OPTICAL_AIS_M, True),
        ("radar_optical", cands["radar"], cands["optical"], GATE_RADAR_OPTICAL_M, True),
    ]:
        accepted, rejected = associate_pair(
            ta, tb, gate_m=gate, allow_many_to_one=m2o, **assoc_kwargs
        )
        results[label] = {"accepted": accepted, "rejected": rejected}
        for name, rows, fields in [
            (f"{label}_accepted.csv", accepted, None),
            (f"{label}_rejected.csv", rejected, None),
        ]:
            path = os.path.join(out_dir, name)
            if rows:
                fields = sorted(rows[0].keys())
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write("")

    evals = {
        "radar_ais": _evaluate(results["radar_ais"]["accepted"], "radar_ais"),
        "optical_ais": _evaluate(results["optical_ais"]["accepted"], "optical_ais"),
        "radar_optical": _evaluate(
            results["radar_optical"]["accepted"], "radar_optical"
        ),
    }

    summary = {
        "alignment_dir": alignment_dir,
        "timebase": timebase.get("coverage"),
        "candidates": {
            s: [{"track_id": t.track_id, "points": t.point_count,
                 "raw": t.raw_point_count} for t in v]
            for s, v in cands.items()
        },
        "associations": {
            k: {"accepted": v["accepted"], "rejected_count": len(v["rejected"])}
            for k, v in results.items()
        },
        "mmsi_evaluation": evals,
        "gates": {
            "radar_ais_m": GATE_RADAR_AIS_M,
            "optical_ais_m": GATE_OPTICAL_AIS_M,
            "radar_optical_m": GATE_RADAR_OPTICAL_M,
            "optical_bias_deg": args.optical_bias_deg,
            "radar_bias_deg": args.radar_bias_deg,
            "bias_correction_applied": not args.no_bias_correction,
        },
        "note": "雷达侧只用纯测量点；评分前做显式系统差修正（光电/雷达ENU绕站心旋转，不改原航迹）；optical_ais与radar_optical允许B端多对一（Inf/TV双设备与重建碎片）；重建航迹无MMSI，MMSI精度只统计双端有MMSI的对。",
    }
    with open(os.path.join(out_dir, "association_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(json.dumps(
        {
            "output_dir": os.path.relpath(out_dir, ROOT),
            "radar_ais": [(a["track_a"], a["track_b"], a["median_m"], a["overlap"]) for a in results["radar_ais"]["accepted"]],
            "optical_ais": [(a["track_a"], a["track_b"], a["median_m"], a["overlap"]) for a in results["optical_ais"]["accepted"]],
            "radar_optical": [(a["track_a"], a["track_b"], a["median_m"], a["overlap"]) for a in results["radar_optical"]["accepted"]][:8],
            "mmsi_evaluation": evals,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
