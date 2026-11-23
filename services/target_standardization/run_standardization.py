#!/usr/bin/env python3
"""命令行入口：按配置运行标准化。

用法：

雷达 TTM（RadarPWN）：
    python3 run_standardization.py --config configs/radarpwn_radar.json \
        --input data/radarpwn/radar_ttm_with_capture_time.csv \
        --all-incoming data/radarpwn/all_incoming_nmea_with_capture_time.csv \
        --output output/radar_ttm

RadarPWN 包内 AIS（!AIVDM 报文流）：
    python3 run_standardization.py --config configs/radarpwn_ais_vdm.json \
        --input data/radarpwn/ais_with_capture_time.csv \
        --all-incoming data/radarpwn/all_incoming_nmea_with_capture_time.csv \
        --output output/radarpwn_ais

MTDSP AIS CSV：
    python3 run_standardization.py --config configs/mtdsp_ais.json \
        --input data/mtdsp \
        --static-dir data/mtdsp \
        --output output/mtdsp_ais

一次运行全部三个数据源：
    python3 run_standardization.py --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from standardization import standardize  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))

# 三个数据源的默认运行参数
JOBS = [
    {
        "name": "radar_ttm",
        "config": "configs/radarpwn_radar.json",
        "input": "data/radarpwn/radar_ttm_with_capture_time.csv",
        "all_incoming": "data/radarpwn/all_incoming_nmea_with_capture_time.csv",
        "static_dir": None,
        "output": "output/radar_ttm",
    },
    {
        "name": "radarpwn_ais",
        "config": "configs/radarpwn_ais_vdm.json",
        "input": "data/radarpwn/ais_with_capture_time.csv",
        "all_incoming": "data/radarpwn/all_incoming_nmea_with_capture_time.csv",
        "static_dir": None,
        "output": "output/radarpwn_ais",
    },
    {
        "name": "mtdsp_ais",
        "config": "configs/mtdsp_ais.json",
        "input": "data/mtdsp",
        "all_incoming": None,
        "static_dir": "data/mtdsp",
        "output": "output/mtdsp_ais",
    },
    {
        "name": "mtdsp_radar",
        "config": "configs/mtdsp_radar.json",
        "input": "data/mtdsp",
        "all_incoming": None,
        "static_dir": None,
        "output": "output/mtdsp_radar",
    },
    {
        "name": "mtdsp_optical",
        "config": "configs/mtdsp_optical.json",
        "input": "data/mtdsp",
        "all_incoming": None,
        "static_dir": None,
        "output": "output/mtdsp_optical",
    },
    {
        "name": "mtdsp2_radar",
        "config": "configs/mtdsp2_radar.json",
        "input": "data/mtdsp2",
        "all_incoming": None,
        "static_dir": None,
        "output": "output/mtdsp2_radar",
    },
    {
        "name": "mtdsp2_ais",
        "config": "configs/mtdsp2_ais.json",
        "input": "data/mtdsp2",
        "all_incoming": None,
        "static_dir": "data/mtdsp2",
        "output": "output/mtdsp2_ais",
    },
    {
        "name": "mtdsp2_optical",
        "config": "configs/mtdsp2_optical.json",
        "input": "data/mtdsp2",
        "all_incoming": None,
        "static_dir": None,
        "output": "output/mtdsp2_optical",
    },
]


def run_one(job: dict, output_root: str = None) -> dict:
    config_path = job["config"]
    with open(os.path.join(ROOT, config_path), "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if job.get("all_incoming"):
        config["all_incoming_csv"] = os.path.join(ROOT, job["all_incoming"])
    if job.get("static_dir"):
        config["static_info_dir"] = os.path.join(ROOT, job["static_dir"])
    input_path = os.path.join(ROOT, job["input"])
    out_dir = job["output"]
    if output_root:
        out_dir = os.path.join(output_root, os.path.basename(out_dir))
    return standardize(input_path, config, os.path.join(ROOT, out_dir))


def main() -> int:
    parser = argparse.ArgumentParser(description="标准化输入为公共目标状态记录")
    parser.add_argument("--config", help="来源配置 JSON")
    parser.add_argument("--input", help="输入文件或目录")
    parser.add_argument("--output", help="输出目录")
    parser.add_argument(
        "--all-incoming", default=None, help="all_incoming CSV（用于仿真日期上下文）"
    )
    parser.add_argument("--static-dir", default=None, help="AIS 静态属性目录")
    parser.add_argument("--all", action="store_true", help="一次运行全部三个数据源")
    args = parser.parse_args()

    if args.all:
        summaries = []
        for job in JOBS:
            summary = run_one(job)
            summary["job"] = job["name"]
            summaries.append(summary)
        print(
            json.dumps(
                [
                    {
                        k: v
                        for k, v in s.items()
                        if k
                        in (
                            "job",
                            "source_id",
                            "source_type",
                            "output_count",
                            "rejected_count",
                            "total_count",
                            "track_count",
                        )
                    }
                    for s in summaries
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not (args.config and args.input and args.output):
        parser.error("需要 --config/--input/--output，或使用 --all")

    with open(args.config, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if args.all_incoming:
        config["all_incoming_csv"] = args.all_incoming
    if args.static_dir:
        config["static_info_dir"] = args.static_dir

    summary = standardize(args.input, config, args.output)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k != "config"}, ensure_ascii=False, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
