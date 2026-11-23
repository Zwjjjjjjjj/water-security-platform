#!/usr/bin/env python3
"""模拟场景生成命令行入口。

用法：
    python3 run_scenario_generation.py --all
    python3 run_scenario_generation.py --scenario A_geometry_baseline

生成物（每个场景一个目录）：
    radar_ttm_with_capture_time.csv   雷达 TTM 观测（可被现有适配器直接读取）
    ais_with_capture_time.csv         AIS !AIVDM 观测
    all_incoming_nmea_with_capture_time.csv  ZDA / RMC 时间与位置上下文
    reference_truth.csv               评测真值（参考位置/速度/目标编号）
    target_mapping.json               雷达编号 ↔ 模拟 MMSI 对应表（仅供评测）
    scenario_manifest.json            场景清单与全部来源记录
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from simulation import ScenarioConfig, ScenarioGenerator  # noqa: E402

SCENARIO_DIR = os.path.join(ROOT, "configs", "scenarios")
OUTPUT_ROOT = os.path.join(ROOT, "output", "scenarios")


def load_config(path: str) -> ScenarioConfig:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    # 模板 CSV 路径相对仓库根目录
    template = data.get("template_source_csv")
    if template and not os.path.isabs(template):
        data["template_source_csv"] = os.path.join(ROOT, template)
    return ScenarioConfig.from_dict(data)


def generate(config_path: str, output_root: str = OUTPUT_ROOT) -> dict:
    config = load_config(config_path)
    out_dir = os.path.join(output_root, config.scenario_id)
    generator = ScenarioGenerator(config)
    result = generator.generate(out_dir)
    manifest = result["manifest"]
    return {
        "scenario_id": manifest["scenario_id"],
        "output_dir": os.path.relpath(out_dir, ROOT),
        "counts": manifest["counts"],
        "reference": {
            "source_mmsi": manifest["reference_trajectory"]["source_mmsi"],
            "source_points": manifest["reference_trajectory"]["source_points"],
            "outliers_removed": len(
                manifest["reference_trajectory"]["outliers_removed"]
            ),
            "reference_point_count": manifest["reference_trajectory"][
                "reference_point_count"
            ],
            "reference_duration_s": manifest["reference_trajectory"][
                "reference_duration_s"
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成雷达—AIS 模拟场景")
    parser.add_argument("--scenario", default=None, help="场景 ID（不含 .json）")
    parser.add_argument("--all", action="store_true", help="生成全部场景")
    parser.add_argument("--output-root", default=OUTPUT_ROOT, help="输出根目录")
    args = parser.parse_args()

    if args.all or not args.scenario:
        paths = sorted(glob.glob(os.path.join(SCENARIO_DIR, "*.json")))
    else:
        path = os.path.join(SCENARIO_DIR, f"{args.scenario}.json")
        if not os.path.exists(path):
            parser.error(f"找不到场景配置 {path}")
        paths = [path]

    results = [generate(path, args.output_root) for path in paths]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
