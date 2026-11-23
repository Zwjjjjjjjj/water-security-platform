#!/usr/bin/env python3
"""时空对齐命令行入口（统一入口）。

三种用法：

1. **直接对齐已有的标准化数据**（推荐用于真实数据）：

       python3 run_alignment.py \
           --radar-records  <radar_standard_records.jsonl> \
           --ais-records    <ais_standard_records.jsonl> \
           --config         configs/alignment/alignment_site_rostock.json \
           --output-dir     output/alignment/run1 \
           [--truth <reference_truth.csv>] [--mapping <target_mapping.json>]

   仅读取标准化数据与运行配置即可输出可追溯的公共时空记录；真值文件
   仅供评测（不给 `--truth` 就只产出对齐结果，不产出评测报告）。

2. **按场景一键跑通**（含标准化步骤，用模拟数据）：

       python3 run_alignment.py --scenario A_geometry_baseline
       python3 run_alignment.py --all

3. **生成验收报告**：

       python3 run_acceptance.py
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from alignment import AlignmentConfig, run_alignment  # noqa: E402
from standardization import standardize  # noqa: E402

SCENARIO_DIR = os.path.join(ROOT, "output", "scenarios")
CONFIG_DIR = os.path.join(ROOT, "configs", "alignment")
DEFAULT_CONFIG = os.path.join(CONFIG_DIR, "alignment_site_rostock.json")

RADAR_SOURCE_CONFIG = {
    "source_id": "sim_radar",
    "source_type": "radar",
    "session_id": "sim-session",
    "frame_id": "radar_local_polar",
    "simulation_date_binding": "zda_date",
    "field_mapping": {
        "target_number": 0,
        "distance": 1,
        "bearing": 2,
        "bearing_reference": 3,
        "speed": 4,
        "course": 5,
        "course_reference": 6,
        "cpa_distance": 7,
        "cpa_time": 8,
        "speed_distance_units": 9,
        "distance_unit": 9,
        "target_name": 10,
        "tracking_status": 11,
        "reference_target": 12,
        "time_of_day": 13,
        "acquisition_type": 14,
    },
}

AIS_SOURCE_CONFIG = {
    "source_id": "sim_ais",
    "source_type": "ais_vdm",
    "session_id": "sim-session",
    "frame_id": "wgs84",
    "simulation_date_binding": "zda_date",
    "placeholder_positions": [[0.0, 0.0]],
}


def standardize_scenario(scenario_dir: str, work_dir: str):
    """用现有标准化模块处理场景数据，返回两路标准记录路径。"""
    context = os.path.join(scenario_dir, "all_incoming_nmea_with_capture_time.csv")

    radar_config = dict(
        RADAR_SOURCE_CONFIG,
        data_root=scenario_dir,
        all_incoming_csv=context,
    )
    radar_out = os.path.join(work_dir, "radar")
    standardize(
        os.path.join(scenario_dir, "radar_ttm_with_capture_time.csv"),
        radar_config,
        radar_out,
    )

    ais_config = dict(
        AIS_SOURCE_CONFIG, data_root=scenario_dir, all_incoming_csv=context
    )
    ais_out = os.path.join(work_dir, "ais")
    standardize(
        os.path.join(scenario_dir, "ais_with_capture_time.csv"),
        ais_config,
        ais_out,
    )

    return (
        os.path.join(radar_out, "standard_records.jsonl"),
        os.path.join(ais_out, "standard_records.jsonl"),
    )


def _config_path_for(scenario_id: str) -> str:
    """场景专用的对齐配置（含该场景的已知时钟偏移）；没有则用默认配置。"""
    path = os.path.join(CONFIG_DIR, f"{scenario_id}.json")
    if os.path.exists(path):
        return path
    return DEFAULT_CONFIG


def run_scenario(scenario_id: str, output_root: str) -> dict:
    scenario_dir = os.path.join(SCENARIO_DIR, scenario_id)
    if not os.path.isdir(scenario_dir):
        raise FileNotFoundError(f"找不到场景目录 {scenario_dir}")

    out_dir = os.path.join(output_root, scenario_id)
    work_dir = os.path.join(out_dir, "standardized")
    os.makedirs(work_dir, exist_ok=True)

    radar_records, ais_records = standardize_scenario(scenario_dir, work_dir)

    with open(
        _config_path_for(scenario_id), "r", encoding="utf-8"
    ) as handle:
        alignment_config = AlignmentConfig.from_dict(json.load(handle))

    result = run_alignment(
        radar_records_path=radar_records,
        ais_records_path=ais_records,
        alignment_config=alignment_config,
        output_dir=out_dir,
        truth_path=os.path.join(scenario_dir, "reference_truth.csv"),
        mapping_path=os.path.join(scenario_dir, "target_mapping.json"),
    )

    evaluation = result["evaluation"]
    summary = {
        "scenario_id": scenario_id,
        "output_dir": os.path.relpath(out_dir, ROOT),
        "coordinate_verification_passed": result["quality"]["coordinate_verification"][
            "all_passed"
        ],
        "spatial": result["quality"]["spatial"]["by_source"],
        "radar_planar_error_m": evaluation.get("position_error_by_sensor", {})
        .get("radar", {})
        .get("planar_m", {}),
        "ais_planar_error_m": evaluation.get("position_error_by_sensor", {})
        .get("ais", {})
        .get("planar_m", {}),
        "time_error_by_sensor": evaluation.get("time_error_by_sensor", {}),
        "cross_source": evaluation.get("cross_source_consistency", {}),
    }
    return summary


def run_direct(args) -> dict:
    """直接对齐已有的标准化数据。"""
    with open(args.config, "r", encoding="utf-8") as handle:
        alignment_config = AlignmentConfig.from_dict(json.load(handle))

    result = run_alignment(
        radar_records_path=args.radar_records,
        ais_records_path=args.ais_records,
        alignment_config=alignment_config,
        output_dir=args.output_dir,
        truth_path=args.truth,
        mapping_path=args.mapping,
        extra_records_paths=args.extra_records,
    )
    return {
        "output_dir": os.path.relpath(args.output_dir, ROOT),
        "coordinate_verification_passed": result["quality"]["coordinate_verification"][
            "all_passed"
        ],
        "spatial": result["quality"]["spatial"]["by_source"],
        "missing_requirements": result["quality"]["missing_requirements"][
            "by_source"
        ],
        "aligned_records": result["aligned_records"],
        "grid_records": result["grid_records"],
        "evaluation_written": bool(result["evaluation"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="雷达—AIS 时空对齐与评测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--scenario", default=None, help="场景 ID（模拟数据）")
    parser.add_argument("--all", action="store_true", help="处理全部场景")
    parser.add_argument(
        "--radar-records", default=None, help="雷达标准记录 JSONL 路径"
    )
    parser.add_argument("--ais-records", default=None, help="AIS 标准记录 JSONL 路径")
    parser.add_argument(
        "--extra-records",
        action="append",
        default=None,
        help="第三类传感器标准记录 JSONL 路径（可重复，如光电）",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="对齐配置 JSON")
    parser.add_argument("--output-dir", default=None, help="输出目录")
    parser.add_argument("--truth", default=None, help="参考真值 CSV（仅供评测）")
    parser.add_argument("--mapping", default=None, help="目标映射 JSON（仅供评测）")
    parser.add_argument(
        "--output-root",
        default=os.path.join(ROOT, "output", "alignment"),
        help="输出根目录（--scenario/--all 模式）",
    )
    args = parser.parse_args()

    # -- 直接模式 ----------------------------------------------------------
    if args.radar_records or args.ais_records:
        if not (args.radar_records and args.ais_records):
            print(
                "错误：--radar-records 与 --ais-records 必须同时提供", file=sys.stderr
            )
            return 2
        if not args.output_dir:
            print("错误：直接模式必须提供 --output-dir", file=sys.stderr)
            return 2
        summary = run_direct(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    # -- 场景模式 ----------------------------------------------------------
    if args.all or not args.scenario:
        scenario_ids = sorted(
            os.path.basename(path)
            for path in glob.glob(os.path.join(SCENARIO_DIR, "*"))
            if os.path.isdir(path)
        )
    else:
        scenario_ids = [args.scenario]

    if not scenario_ids:
        print("找不到任何场景目录", file=sys.stderr)
        return 2

    summaries = [run_scenario(sid, args.output_root) for sid in scenario_ids]
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
