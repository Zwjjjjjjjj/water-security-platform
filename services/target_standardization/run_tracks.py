#!/usr/bin/env python3
"""航迹导出命令行入口。

把**已对齐**的逐点记录整理成按传感器分组的完整航迹，为航迹关联做准备。

用法：

    # 从已有的对齐输出目录导出（推荐）
    python3 run_tracks.py --alignment-dir output/alignment/mtdsp2_yantai

    # 直接从标准化记录 + 对齐配置导出（不重跑对齐）
    python3 run_tracks.py \
        --radar-records output/mtdsp2_radar/standard_records.jsonl \
        --ais-records   output/mtdsp2_ais/standard_records.jsonl \
        --extra-records output/mtdsp2_optical/standard_records.jsonl \
        --config configs/alignment/alignment_site_yantai_mtdsp2.json \
        --output-dir output/alignment/mtdsp2_yantai

## 产出

    tracks/radar/<航迹号>.csv     雷达完整航迹
    tracks/ais/<航迹号>.csv       AIS 完整航迹
    tracks/optical/<航迹号>.csv   光电完整航迹（Inf/TV 已分离）
    tracks/track_index.csv        航迹清单（关联候选集）
    tracks/track_summary.json     汇总统计
    tracks/radar_detections.csv   雷达逐帧检测点（900xxx 内部号未聚类）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from alignment import AlignmentConfig, SpatialAligner, export_tracks  # noqa: E402


def _read_csv(path: str):
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            yield row


def _from_alignment_dir(alignment_dir: str, output_dir: str) -> dict:
    """从已有对齐输出目录导出航迹。

    直接读 `aligned_records.csv` —— 该文件已含全部对齐结果（含 ENU、
    误差上界、平台状态），无需重跑对齐。
    """
    aligned = os.path.join(alignment_dir, "aligned_records.csv")
    if not os.path.exists(aligned):
        raise FileNotFoundError(f"找不到 {aligned}")

    quality_path = os.path.join(alignment_dir, "alignment_quality.json")
    origin_lat = origin_lon = 0.0
    frame_id = "site_enu"
    if os.path.exists(quality_path):
        with open(quality_path, encoding="utf-8") as handle:
            quality = json.load(handle)
        frame = quality.get("site_frame") or {}
        origin_lat = float(frame.get("origin_latitude_deg") or 0.0)
        origin_lon = float(frame.get("origin_longitude_deg") or 0.0)
        frame_id = str(frame.get("frame_id") or frame_id)

    records = list(_read_csv(aligned))
    return export_tracks(
        records,
        output_dir,
        origin_latitude_deg=origin_lat,
        origin_longitude_deg=origin_lon,
        frame_id=frame_id,
    )


def _from_records(args) -> dict:
    """从标准化记录 + 对齐配置导出（不重跑对齐流水线）。"""
    from alignment.pipeline import read_jsonl

    with open(args.config, encoding="utf-8") as handle:
        config = AlignmentConfig.from_dict(json.load(handle))

    records = read_jsonl(args.radar_records) + read_jsonl(args.ais_records)
    for extra in args.extra_records or []:
        records.extend(read_jsonl(extra))

    # 对齐到现场 ENU（与流水线同一条路径，保证坐标一致）
    aligner = SpatialAligner(config)
    aligner.align_records(records)
    for record in records:
        source_config = config.source_config(str(record.get("source_id")))
        from alignment.temporal import TemporalAligner

        corrected = TemporalAligner(config, spatial_aligner=aligner).corrected_state_epoch(
            record, source_config
        )
        if corrected is not None:
            record["aligned_time_utc"] = TemporalAligner(
                config, spatial_aligner=aligner
            )._format_grid_time(corrected)

    return export_tracks(
        records,
        args.output_dir,
        origin_latitude_deg=config.site_frame.origin_latitude_deg,
        origin_longitude_deg=config.site_frame.origin_longitude_deg,
        frame_id=config.site_frame.frame_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="导出按传感器分组的完整航迹（供航迹关联使用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--alignment-dir", default=None, help="已有的对齐输出目录")
    parser.add_argument("--radar-records", default=None, help="雷达标准记录 JSONL")
    parser.add_argument("--ais-records", default=None, help="AIS 标准记录 JSONL")
    parser.add_argument(
        "--extra-records",
        action="append",
        default=None,
        help="第三类传感器标准记录 JSONL（可重复）",
    )
    parser.add_argument("--config", default=None, help="对齐配置 JSON")
    parser.add_argument("--output-dir", default=None, help="航迹输出目录")
    args = parser.parse_args()

    if args.alignment_dir:
        output_dir = args.output_dir or args.alignment_dir
        result = _from_alignment_dir(args.alignment_dir, output_dir)
    elif args.radar_records and args.ais_records:
        if not args.config or not args.output_dir:
            print("错误：直接模式必须提供 --config 与 --output-dir", file=sys.stderr)
            return 2
        result = _from_records(args)
    else:
        print(
            "错误：需提供 --alignment-dir，或同时提供 --radar-records/--ais-records",
            file=sys.stderr,
        )
        return 2

    summary = result["summary_data"]
    print(json.dumps(
        {
            "output_dir": os.path.relpath(result["tracks_dir"], ROOT),
            "track_count": summary["track_count"],
            "point_count": summary["point_count"],
            "radar_detection_count": summary["radar_detection_count"],
            "by_sensor": {
                sensor: {
                    "track_count": item["track_count"],
                    "point_count": item["point_count"],
                    "stable_track_count": item["stable_track_count"],
                    "unstable_track_count": item["unstable_track_count"],
                }
                for sensor, item in summary["by_sensor"].items()
            },
            "index": os.path.relpath(result["index"], ROOT),
            "summary": os.path.relpath(result["summary"], ROOT),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
