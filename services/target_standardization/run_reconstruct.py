#!/usr/bin/env python3
"""CDT 跨帧重建命令行入口。

把 `tracks/radar_detections.csv`（逐帧检测点）聚成稳定航迹，为航迹关联做准备。

用法：

    # 从已有的对齐输出目录重建（推荐）
    python3 run_reconstruct.py --alignment-dir output/alignment/mtdsp2_yantai

    # 指定检测表与输出目录
    python3 run_reconstruct.py \\
        --detections output/alignment/mtdsp2_yantai/tracks/radar_detections.csv \\
        --output-dir output/alignment/mtdsp2_yantai

## 产出

    tracks/radar_reconstructed/R<序号>.csv   重建航迹点序列
    tracks/radar_reconstructed_index.csv     重建航迹清单
    tracks/reconstruction_summary.json       重建统计 + 参数

## 何时需要 / 何时不需要

  - 03-12 包无 RT 行：必须走本模块重建；
  - 03-05 包有可靠 RT-1（107 行连续）：可直接使用 RT，本模块输出作为补充。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from association.cdt_tracker import (  # noqa: E402
    BASE_GATE_M,
    DRANGE_M,
    DTHETA_DEG,
    MAX_GAP_S,
    VMAX_MPS,
    V_SMOOTH,
    CdtTracker,
    load_detections,
)

RECON_FIELDS = [
    "track_id",
    "sensor_type",
    "point_index",
    "time_utc",
    "time_epoch_s",
    "latitude_deg",
    "longitude_deg",
    "east_m",
    "north_m",
    "up_m",
    "range_m",
    "bearing_deg",
    "radar_frame_index",
    "measurement_kind",
    "member_count",
    "source_row",
    "member_source_rows",
    "error_bound_planar_m",
]

INDEX_FIELDS = [
    "track_id",
    "sensor_type",
    "track_kind",
    "track_stability",
    "point_count",
    "start_time_utc",
    "end_time_utc",
    "duration_s",
    "median_dt_s",
    "path_length_m",
    "straight_line_distance_m",
    "mean_speed_mps",
    "max_speed_mps",
]


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return float(values[mid])
    return (values[mid - 1] + values[mid]) / 2.0


def _index_row(track_id, points):
    epochs = [p["time_epoch_s"] for p in points]
    dts = [epochs[i + 1] - epochs[i] for i in range(len(epochs) - 1)]
    path = 0.0
    for i in range(len(points) - 1):
        path += math.hypot(
            points[i + 1]["east_m"] - points[i]["east_m"],
            points[i + 1]["north_m"] - points[i]["north_m"],
        )
    straight = 0.0
    if len(points) >= 2:
        straight = math.hypot(
            points[-1]["east_m"] - points[0]["east_m"],
            points[-1]["north_m"] - points[0]["north_m"],
        )
    duration = epochs[-1] - epochs[0] if epochs else 0.0
    speed = straight / duration if duration > 0 else 0.0
    return {
        "track_id": track_id,
        "sensor_type": "radar",
        "track_kind": "radar_reconstructed",
        "track_stability": "reconstructed",
        "point_count": len(points),
        "start_time_utc": points[0]["time_utc"] if points else None,
        "end_time_utc": points[-1]["time_utc"] if points else None,
        "duration_s": round(duration, 3),
        "median_dt_s": round(_median(dts), 3) if dts else None,
        "path_length_m": round(path, 3),
        "straight_line_distance_m": round(straight, 3),
        "mean_speed_mps": round(speed, 4),
        "max_speed_mps": round(speed, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CDT 跨帧重建（逐帧检测 → 稳定航迹）")
    parser.add_argument("--alignment-dir", default=None)
    parser.add_argument("--detections", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    if args.alignment_dir:
        det_path = os.path.join(args.alignment_dir, "tracks", "radar_detections.csv")
        out_dir = args.output_dir or args.alignment_dir
    elif args.detections:
        det_path = args.detections
        out_dir = args.output_dir or os.path.dirname(os.path.dirname(det_path))
    else:
        print("错误：需提供 --alignment-dir 或 --detections", file=sys.stderr)
        return 2
    if not os.path.exists(det_path):
        print(f"错误：找不到 {det_path}", file=sys.stderr)
        return 2

    frames = load_detections(det_path)
    tracker = CdtTracker()
    tracks = tracker.reconstruct(frames)

    tracks_dir = os.path.join(out_dir, "tracks")
    recon_dir = os.path.join(tracks_dir, "radar_reconstructed")
    os.makedirs(recon_dir, exist_ok=True)

    index_rows = []
    for pid in sorted(tracks):
        points = tracks[pid]
        track_id = f"radar_reconstructed:R{pid}"
        rows = []
        for idx, p in enumerate(points):
            rows.append(
                {
                    "track_id": track_id,
                    "sensor_type": "radar",
                    "point_index": idx,
                    "time_utc": p["time_utc"],
                    "time_epoch_s": p["time_epoch_s"],
                    "latitude_deg": p.get("latitude_deg"),
                    "longitude_deg": p.get("longitude_deg"),
                    "east_m": p.get("east_m"),
                    "north_m": p.get("north_m"),
                    "up_m": p.get("up_m"),
                    "range_m": p.get("range_m"),
                    "bearing_deg": p.get("bearing_deg"),
                    "radar_frame_index": p.get("radar_frame_index"),
                    "measurement_kind": p.get("measurement_kind"),
                    "member_count": p.get("member_count"),
                    "source_row": p.get("source_row"),
                    "member_source_rows": ";".join(
                        str(x) for x in (p.get("member_source_rows") or [])
                    ),
                    "error_bound_planar_m": p.get("error_bound_planar_m"),
                }
            )
        safe = f"R{pid}.csv"
        with open(os.path.join(recon_dir, safe), "w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=RECON_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        index_rows.append(_index_row(track_id, rows))

    index_path = os.path.join(tracks_dir, "radar_reconstructed_index.csv")
    with open(index_path, "w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(index_rows)

    summary = {
        "detection_count": sum(len(m) for _, _, m in frames),
        "frame_count": len(frames),
        "reconstructed_track_count": len(tracks),
        "params": {
            "dtheta_deg": DTHETA_DEG,
            "drange_m": DRANGE_M,
            "base_gate_m": BASE_GATE_M,
            "vmax_mps": VMAX_MPS,
            "max_gap_s": MAX_GAP_S,
            "v_smooth": V_SMOOTH,
        },
        "usage_note": (
            "03-12 包无 RT 行，必须使用本重建输出；"
            "03-05 包有可靠 RT-1，可直接使用 RT，本输出为补充。"
            "偏差修正不默认应用，见 association/bias.py。"
        ),
    }
    summary_path = os.path.join(tracks_dir, "reconstruction_summary.json")
    with open(summary_path, "w", encoding="utf-8") as h:
        json.dump(summary, h, ensure_ascii=False, indent=2)
        h.write("\n")

    print(json.dumps({"output_dir": os.path.relpath(recon_dir, ROOT),
                      "track_count": len(tracks),
                      "index": os.path.relpath(index_path, ROOT),
                      "summary": os.path.relpath(summary_path, ROOT)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
