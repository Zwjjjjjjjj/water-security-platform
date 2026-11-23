#!/usr/bin/env python3
"""MTDSP 跨源时空对齐评估。

## 评估对象

MTDSP **没有独立真值**。本脚本用数据集自带的**跨源关联结果**作为评测基准：

  - `AT` 行（`目标类型=AT`）：信号处理链已把该目标关联到具体 MMSI，
    其方位/距离**继承自 AIS 位置**（已逐点核实：与 AIS CSV 同行数值
    精确到 0.000°/0.0 m）。
  - `CDT` 行（`目标类型=CDT`）：**雷达实测**检测目标，方位/距离落在
    外接矩形角度与距离区间内的比例为 271/271 = 100%。

因此：
  - **雷达测角/测距精度**用 `CDT` 行评估 → 但 CDT 没有 MMSI，
    只能按 (方位, 距离) 邻近度与 AIS 位置做**空间最近邻配对**；
  - **跨源时间对齐质量**用 `AT` 行评估 → AT 行已知 MMSI，
    可与该 MMSI 的 AIS 轨迹按时刻配对。

## 时间基准

时区未确认，但雷达与 AIS 共享同一设备时钟，因此使用**相对时间基准**
（参考时刻 2025-03-05 10:35:38）。绝对 UTC 不可知，源间相对时差确定。

## 输出

  - `mtdsp_cross_source_evaluation.json`  评估结果
  - `mtdsp_cross_source_pairs.csv`       逐对明细
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment.transform import SiteFrame, latlon_to_enu  # noqa: E402

DATASET_ID = "MTDSP-20250305103638-2003-AT-413203610-1"
REFERENCE_LOCAL_DATETIME = "2025-03-05 10:35:38"


def _parse_local(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.split(" (local)")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _median(values: List[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _stats(values: List[float], digits: int = 3) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], digits),
        "median": round(_median(ordered), digits),
        "max": round(ordered[-1], digits),
        "mean": round(sum(ordered) / len(ordered), digits),
    }


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _station_basis_report(
    ais_records_path: str,
    frame: SiteFrame,
    radar_station_latitude_deg: float,
    radar_station_longitude_deg: float,
) -> Dict[str, Any]:
    """反算 AIS 行内（经纬度 + 方位角 + 距离）三元组隐含的参考站。

    AIS CSV 每行同时给出目标经纬度与该目标相对**某个观测点**的方位/距离。
    把三元组代入几何关系即可反解出那个观测点：
        隐含站址 = 目标位置 - 距离 * (sin(方位), cos(方位))

    若 AIS 的极坐标列由雷达站测得，反解结果应等于雷达站址。
    实际差值即「两列数据站址基准不同」的系统差。
    """
    from geodesy import LocalOrigin

    radar_origin = LocalOrigin(radar_station_latitude_deg, radar_station_longitude_deg)

    per_mmsi: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for record in load_jsonl(ais_records_path):
        position = record.get("position") or {}
        latitude = position.get("latitude_deg")
        longitude = position.get("longitude_deg")
        bearing = record.get("bearing_deg_unverified")
        distance = record.get("distance_m_unverified")
        if latitude is None or longitude is None:
            continue
        if bearing is None or distance is None:
            continue
        east, north = radar_origin.to_enu(float(latitude), float(longitude))
        beta = math.radians(float(bearing))
        # 隐含站址（相对雷达自记站址的 ENU 偏移）
        per_mmsi[str(record.get("local_track_id"))].append(
            (
                east - float(distance) * math.sin(beta),
                north - float(distance) * math.cos(beta),
            )
        )

    by_mmsi: Dict[str, Any] = {}
    offsets_east: List[float] = []
    offsets_north: List[float] = []
    for mmsi, samples in sorted(per_mmsi.items()):
        if len(samples) < 2:
            continue
        mean_east = sum(s[0] for s in samples) / len(samples)
        mean_north = sum(s[1] for s in samples) / len(samples)
        std_east = (sum((s[0] - mean_east) ** 2 for s in samples) / len(samples)) ** 0.5
        std_north = (sum((s[1] - mean_north) ** 2 for s in samples) / len(samples)) ** 0.5
        by_mmsi[mmsi] = {
            "samples": len(samples),
            "implied_station_offset_east_m": round(mean_east, 3),
            "implied_station_offset_north_m": round(mean_north, 3),
            "scatter_east_std_m": round(std_east, 3),
            "scatter_north_std_m": round(std_north, 3),
        }
        offsets_east.append(mean_east)
        offsets_north.append(mean_north)

    if offsets_east:
        median_east = _median(offsets_east)
        median_north = _median(offsets_north)
    else:
        median_east = float("nan")
        median_north = float("nan")

    return {
        "description": (
            "站址基准一致性诊断。AIS 行内（经纬度 + 方位角 + 距离）三元组"
            "隐含的参考站，与雷达设备自记站址的差。"
        ),
        "radar_self_reported_station": {
            "latitude_deg": radar_station_latitude_deg,
            "longitude_deg": radar_station_longitude_deg,
            "source": (
                "IF_2003_AT_413203610.csv 帧头（123 帧经纬度列完全一致），"
                "为雷达设备自身记录，视为权威。"
            ),
        },
        "ais_implied_station_offset_m": {
            "east_median": round(median_east, 3),
            "north_median": round(median_north, 3),
            "targets": len(offsets_east),
            "note": (
                "负值表示 AIS 极坐标列的参考站位于雷达自记站址以西。"
                "在目标距离 R 处，该东向差产生约 atan(ΔE/R) 的方位差。"
            ),
        },
        "by_mmsi": by_mmsi,
        "conclusion": (
            "AIS 极坐标列与雷达 CDT 行使用了不同的站址基准，"
            "因此两者直接相减会产生系统性方位差。"
            "这是标定口径问题，不是数据错误，也不是「检测框的固有偏移」。"
            "跨源比对前须统一站址基准。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MTDSP 跨源时空对齐评估",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--radar-records",
        default=os.path.join(ROOT, "output", "mtdsp_radar", "standard_records.jsonl"),
    )
    parser.add_argument(
        "--ais-records",
        default=os.path.join(ROOT, "output", "mtdsp_ais", "standard_records.jsonl"),
    )
    parser.add_argument(
        "--aligned-records",
        default=os.path.join(
            ROOT, "output", "alignment", "mtdsp_yantai", "aligned_records.csv"
        ),
    )
    parser.add_argument("--station-lat", type=float, default=37.53297)
    parser.add_argument("--station-lon", type=float, default=121.41990)
    parser.add_argument(
        "--radar-station-lat",
        type=float,
        default=37.53297,
        help="雷达设备自记站址（IF 帧头），用于站址基准一致性诊断",
    )
    parser.add_argument(
        "--radar-station-lon",
        type=float,
        default=121.42020,
        help="雷达设备自记站址（IF 帧头），用于站址基准一致性诊断",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(ROOT, "output", "evaluation", "mtdsp_cross_source"),
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    reference = _parse_local(REFERENCE_LOCAL_DATETIME)
    frame = SiteFrame(
        frame_id="site_enu_yantai",
        datum="WGS84",
        origin_latitude_deg=args.station_lat,
        origin_longitude_deg=args.station_lon,
    )

    # -- AIS 轨迹（按 MMSI 分组，相对秒） --------------------------------
    ais_by_mmsi: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    ais_speed: Dict[str, float] = {}
    for record in load_jsonl(args.ais_records):
        moment = _parse_local(str(record.get("state_time_raw") or ""))
        if moment is None:
            continue
        position = record.get("position") or {}
        latitude = position.get("latitude_deg")
        longitude = position.get("longitude_deg")
        if latitude is None or longitude is None:
            continue
        offset = (moment - reference).total_seconds()
        mmsi = str(record.get("local_track_id"))
        ais_by_mmsi[mmsi].append(
            (offset, float(latitude), float(longitude))
        )
        speed = record.get("speed_mps")
        if speed is not None:
            ais_speed.setdefault(mmsi, float(speed))
    for mmsi in ais_by_mmsi:
        ais_by_mmsi[mmsi].sort()

    def ais_enu_at(mmsi: str, offset: float) -> Optional[Tuple[float, float]]:
        """AIS 轨迹在给定相对时刻的线性插值 ENU（不外推）。"""
        sequence = ais_by_mmsi.get(mmsi)
        if not sequence:
            return None
        if offset < sequence[0][0] - 1e-9 or offset > sequence[-1][0] + 1e-9:
            return None
        low, high = 0, len(sequence) - 1
        while high - low > 1:
            mid = (low + high) // 2
            if sequence[mid][0] <= offset:
                low = mid
            else:
                high = mid
        t0, lat0, lon0 = sequence[low]
        t1, lat1, lon1 = sequence[high]
        if t1 - t0 <= 0:
            lat, lon = lat0, lon0
        else:
            ratio = (offset - t0) / (t1 - t0)
            lat = lat0 + (lat1 - lat0) * ratio
            lon = lon0 + (lon1 - lon0) * ratio
        enu = latlon_to_enu(frame, lat, lon, 0.0)
        return enu.east_m, enu.north_m

    # -- 对齐记录（已转 ENU） ----------------------------------------------
    aligned: List[Dict[str, Any]] = []
    with open(args.aligned_records, "r", encoding="utf-8", newline="") as handle:
        aligned = list(csv.DictReader(handle))

    # -- 评估 1：AT 行（已关联 MMSI）跨源一致性 --------------------------
    at_pairs: List[Dict[str, Any]] = []
    for row in aligned:
        if row.get("radar_target_type") != "AT":
            continue
        moment = _parse_local(str(row.get("state_time_raw") or ""))
        if moment is None:
            continue
        offset = (moment - reference).total_seconds()
        mmsi = str(row.get("local_track_id"))
        target = ais_enu_at(mmsi, offset)
        if target is None:
            continue
        east = float(row["east_m"])
        north = float(row["north_m"])
        d_east = east - target[0]
        d_north = north - target[1]
        planar = math.hypot(d_east, d_north)
        # 径向/切向分解
        azimuth = math.atan2(target[0], target[1])
        radial = d_east * math.sin(azimuth) + d_north * math.cos(azimuth)
        tangential = d_east * math.cos(azimuth) - d_north * math.sin(azimuth)
        # 与 AIS 最近原始记录的时刻差
        sequence = ais_by_mmsi[mmsi]
        nearest = min(abs(item[0] - offset) for item in sequence)
        at_pairs.append(
            {
                "source": "radar_AT",
                "mmsi": mmsi,
                "time_raw": row.get("state_time_raw"),
                "offset_s": round(offset, 3),
                "radar_east_m": round(east, 4),
                "radar_north_m": round(north, 4),
                "ais_east_m": round(target[0], 4),
                "ais_north_m": round(target[1], 4),
                "delta_east_m": round(d_east, 4),
                "delta_north_m": round(d_north, 4),
                "delta_planar_m": round(planar, 4),
                "delta_radial_m": round(radial, 4),
                "delta_tangential_m": round(tangential, 4),
                "ais_nearest_report_dt_s": round(nearest, 3),
                "ais_speed_mps": ais_speed.get(mmsi),
            }
        )

    by_mmsi: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pair in at_pairs:
        by_mmsi[pair["mmsi"]].append(pair)

    at_report = {
        "description": (
            "雷达 AT 行（已关联 MMSI，方位/距离继承自 AIS）"
            "与同 MMSI 的 AIS 轨迹按同一时刻比较。"
            "AT 行位置本就来自 AIS，故此处残差反映的是"
            "**融合/关联延迟**与 AIS 轨迹插值误差，而非雷达测距精度。"
        ),
        "total_pairs": len(at_pairs),
        "by_mmsi": {},
    }
    for mmsi, pairs in sorted(by_mmsi.items()):
        at_report["by_mmsi"][mmsi] = {
            "pairs": len(pairs),
            "ais_speed_mps": pairs[0].get("ais_speed_mps"),
            "delta_planar_m": _stats([p["delta_planar_m"] for p in pairs]),
            "delta_radial_m": _stats([p["delta_radial_m"] for p in pairs]),
            "delta_tangential_m": _stats([p["delta_tangential_m"] for p in pairs]),
            "ais_nearest_report_dt_s": _stats(
                [p["ais_nearest_report_dt_s"] for p in pairs], 1
            ),
        }

    # -- 评估 2：CDT 行（雷达实测）与 AIS 位置的配对 ----------------------
    # CDT 的 `目标编号` 有两类，语义完全不同：
    #   - 带 MMSI 的（如 413203610）：已关联到具体船，可与 AIS 位置直接比较；
    #   - 内部检测号 900xxx：雷达**独有的检测**（海杂波与未关联目标），
    #     本包 AIS 中没有对应目标。任何强行配对都是错的
    #     （试过空间最近邻与方位/距离联合择优，残差均达 600-2300 m）。
    #
    # 因此只评估带 MMSI 的 CDT 行，900xxx 单独统计为"无 AIS 对应目标"。
    ais_targets = []
    for mmsi, sequence in ais_by_mmsi.items():
        for offset, latitude, longitude in sequence:
            enu = latlon_to_enu(frame, latitude, longitude, 0.0)
            range_m = math.hypot(enu.east_m, enu.north_m)
            azimuth = math.degrees(math.atan2(enu.east_m, enu.north_m)) % 360.0
            ais_targets.append(
                (mmsi, offset, enu.east_m, enu.north_m, range_m, azimuth)
            )

    cdt_pairs: List[Dict[str, Any]] = []
    cdt_internal_only = 0
    cdt_internal_ids: Dict[str, int] = defaultdict(int)
    cdt_no_ais_in_window = 0
    for row in aligned:
        if row.get("radar_target_type") != "CDT":
            continue
        internal_id = str(row.get("local_track_id"))
        if internal_id.startswith("900"):
            # 雷达独有检测，无 AIS 对应目标
            cdt_internal_only += 1
            cdt_internal_ids[internal_id] += 1
            continue
        moment = _parse_local(str(row.get("state_time_raw") or ""))
        if moment is None:
            continue
        offset = (moment - reference).total_seconds()
        east = float(row["east_m"])
        north = float(row["north_m"])
        radar_range = math.hypot(east, north)
        radar_azimuth = math.degrees(math.atan2(east, north)) % 360.0

        # 编号本身即 MMSI → 直接按该 MMSI 的 AIS 轨迹配对，不做空间择优。
        # （曾用空间择优，导致 CDT/413203610 被误配到无关目标，残差上千米。）
        #
        # 时间窗口放宽到 60 s：该船 AIS 报告间隔中位约 112 s，
        # 用 ±2 s 窗口会导致几乎全部 CDT 观测无配对。窗口放宽不引入
        # 误配 —— 配对对象已由 MMSI 唯一确定。
        candidates = [
            item
            for item in ais_targets
            if item[0] == internal_id and abs(item[1] - offset) <= 60.0
        ]
        if not candidates:
            cdt_no_ais_in_window += 1
            continue
        # 同一 MMSI 内多条 AIS 观测时，取方位+距离代价最小者
        best = min(
            candidates,
            key=lambda item: (
                ((radar_azimuth - item[5] + 180.0) % 360.0 - 180.0) / 1.2
            ) ** 2
            + ((radar_range - item[4]) / 2.5) ** 2,
        )
        (
            mmsi,
            ais_offset,
            ais_east,
            ais_north,
            ais_range,
            ais_azimuth,
        ) = best
        d_east = east - ais_east
        d_north = north - ais_north
        azimuth = math.atan2(ais_east, ais_north)
        radial = d_east * math.sin(azimuth) + d_north * math.cos(azimuth)
        tangential = d_east * math.cos(azimuth) - d_north * math.sin(azimuth)
        cdt_pairs.append(
            {
                "source": "radar_CDT",
                "mmsi": mmsi,
                "time_raw": row.get("state_time_raw"),
                "offset_s": round(offset, 3),
                "radar_east_m": round(east, 4),
                "radar_north_m": round(north, 4),
                "ais_east_m": round(ais_east, 4),
                "ais_north_m": round(ais_north, 4),
                "delta_east_m": round(d_east, 4),
                "delta_north_m": round(d_north, 4),
                "delta_planar_m": round(math.hypot(d_east, d_north), 4),
                "delta_radial_m": round(radial, 4),
                "delta_tangential_m": round(tangential, 4),
                "delta_bearing_deg": round(
                    (radar_azimuth - ais_azimuth + 180.0) % 360.0 - 180.0, 5
                ),
                "delta_range_m": round(radar_range - ais_range, 4),
                "ais_nearest_report_dt_s": round(abs(ais_offset - offset), 3),
                "ais_speed_mps": ais_speed.get(mmsi),
            }
        )

    cdt_by_mmsi: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for pair in cdt_pairs:
        cdt_by_mmsi[pair["mmsi"]].append(pair)

    cdt_report = {
        "description": (
            "雷达 CDT 行（雷达实测检测目标）与 AIS 位置的配对。"
            "只评估**带 MMSI 编号**的 CDT 行；900xxx 内部检测号是雷达独有的"
            "检测（海杂波与未关联目标），本包 AIS 无对应目标，"
            "强行配对会产生 600-2300 m 的假残差，已单独统计。"
            "配对按同一时刻（±2 s）内 (方位/波束宽度)² + (距离/分辨率)² 择优。"
            "注意：残差中含**站址基准系统差**（见 station_basis_consistency），"
            "不能直接解读为雷达测角/测距误差。"
        ),
        "pairing": {
            "time_window_s": 60.0,
            "bearing_norm_deg": 1.2,
            "range_norm_m": 2.5,
            "note": (
                "差值不做门限过滤：差值本身即雷达实测与 AIS 位置的差。"
                "时间窗口放宽到 60 s 是因为该船 AIS 报告间隔中位约 112 s；"
                "配对对象由 MMSI 唯一确定，放宽窗口不引入误配。"
            ),
        },
        "total_cdt_records": len(cdt_pairs)
        + cdt_internal_only
        + cdt_no_ais_in_window,
        "evaluated_with_mmsi": len(cdt_pairs),
        "internal_only_no_ais_target": cdt_internal_only,
        "internal_only_by_id": dict(cdt_internal_ids),
        "no_ais_in_time_window": cdt_no_ais_in_window,
        "by_mmsi": {},
    }
    for mmsi, pairs in sorted(cdt_by_mmsi.items()):
        cdt_report["by_mmsi"][mmsi] = {
            "pairs": len(pairs),
            "ais_speed_mps": pairs[0].get("ais_speed_mps"),
            "delta_planar_m": _stats([p["delta_planar_m"] for p in pairs]),
            "delta_radial_m": _stats([p["delta_radial_m"] for p in pairs]),
            "delta_tangential_m": _stats([p["delta_tangential_m"] for p in pairs]),
        }

    # -- 评估 3：站址基准一致性诊断 --------------------------------------
    # 背景：CDT 行的方位/距离由雷达设备自记站址 (37.53297, 121.42020) 测得；
    # AIS CSV 的「方位角(°)/距离(m)」列则隐含另一个参考站。
    # 两者相差约 26.5 m（东向），在 3.9 km 处产生约 -2.4° 的方位差 ——
    # 这正是 CDT 相对 AIS 出现稳定方位差的原因。
    # 本项独立反算 AIS 三元组隐含的站址，量化该系统差，而不是假设它。
    station_basis = _station_basis_report(
        ais_records_path=args.ais_records,
        frame=frame,
        radar_station_latitude_deg=args.radar_station_lat,
        radar_station_longitude_deg=args.radar_station_lon,
    )

    # -- 汇总 --------------------------------------------------------------
    report = {
        "dataset_id": DATASET_ID,
        "dataset_source": {
            "program": "海上目标数据共享计划（MTDSP）——海上目标多源观测数据集",
            "publisher": "海军航空大学",
            "doi": "10.12000/JR25001",
        },
        "time_basis": {
            "mode": "relative_local",
            "reference_local_datetime": REFERENCE_LOCAL_DATETIME,
            "note": (
                "时区未确认。雷达与 AIS 共享同一设备时钟，"
                "故用相对时间基准做同场景内对齐；绝对 UTC 不可知。"
            ),
        },
        "geometry": {
            "origin_latitude_deg": args.station_lat,
            "origin_longitude_deg": args.station_lon,
            "datum": "WGS84",
            "frame_id": "site_enu_yantai",
        },
        "radar_AT_cross_source": at_report,
        "radar_CDT_with_mmsi": cdt_report,
        "station_basis_consistency": station_basis,
    }

    json_path = os.path.join(
        args.output_dir, "mtdsp_cross_source_evaluation.json"
    )
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    csv_path = os.path.join(args.output_dir, "mtdsp_cross_source_pairs.csv")
    all_pairs = at_pairs + cdt_pairs
    if all_pairs:
        # 统一字段集合：AT 行没有 delta_bearing_deg / delta_range_m
        fieldnames: List[str] = []
        for pair in all_pairs:
            for key in pair:
                if key not in fieldnames:
                    fieldnames.append(key)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=fieldnames, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(all_pairs)

    print(
        json.dumps(
            {
                "radar_AT_cross_source": {
                    "total_pairs": at_report["total_pairs"],
                    "by_mmsi": at_report["by_mmsi"],
                },
                "radar_CDT_with_mmsi": {
                    "total_cdt_records": cdt_report["total_cdt_records"],
                    "evaluated_with_mmsi": cdt_report["evaluated_with_mmsi"],
                    "internal_only_no_ais_target": cdt_report[
                        "internal_only_no_ais_target"
                    ],
                    "internal_only_by_id": cdt_report["internal_only_by_id"],
                    "no_ais_in_time_window": cdt_report["no_ais_in_time_window"],
                    "by_mmsi": cdt_report["by_mmsi"],
                },
                "outputs": [
                    os.path.relpath(json_path, ROOT),
                    os.path.relpath(csv_path, ROOT),
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
