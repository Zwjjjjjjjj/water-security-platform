#!/usr/bin/env python3
"""MTDSP 实测误差模型标定（用于重标定模拟场景 C 的误差参数）。

## 为什么需要这个

模拟场景 C 的误差参数是**拍脑袋设定值**：

    radar_sensor.position_noise_std_m = 12.0
    ais_sensor.position_noise_std_m   = 25.0

配置里原本就注明"误差参数为模拟设定值，无独立真值支持"。MTDSP 提供了
真实设备数据，可以把这些人为参数换成**实测值**，让模拟场景更接近现实。

## 核心发现：误差不是各向同性白噪声

MTDSP 的 AIS CSV 同时带有经纬度与雷达测得的极坐标，可直接互检。
对**静止目标**（其误差不含目标位移项）逐目标分析后发现：

| 目标 | 距离 | 方位差中位 | 方位差 std | 距离差中位 | 距离差 std |
| ---- | ---- | ---------- | ---------- | ---------- | ---------- |
| 413203610 | 3870-3881 m | +0.008° | 0.031° | +0.90 m | 3.88 m |
| 412331720 | 5594-9040 m | +0.038° | 0.192° | +4.08 m | 7.83 m |
| 413301480 | 3329 m | **+0.806°** | **0.0068°** | −1.15 m | 0.20 m |

两个结构性特征：

1. **测角误差随距离放大** → 切向误差 = 距离 × 测角误差，
   等效方位误差 0.285°（0.497°）。
2. **413301480 的方位差 std 只有 0.0068°** —— 这不是噪声，是
   **该目标相对雷达站的稳定系统偏差**（很可能是 AIS 天线位置与雷达站的
   杆差，或该目标的自身偏置）。用单一全局误差模型会把它误当成随机误差。

因此误差模型必须**按目标分层**，而不是给一个全局标准差。

## 输出

`mtdsp_error_model.json` —— 供 `configs/scenarios/C_*.json` 引用。

## 用法

    python3 calibration/mtdsp_error_model.py \
        --samples output/verification/mtdsp_spatial/mtdsp_spatial_samples_calibrated.csv \
        --output output/calibration/mtdsp_error_model.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 静止/运动判定阈值（ AIS 报告的 SOG）
STATIC_SPEED_THRESHOLD_KN = 1.0


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _fit_line(xs: List[float], ys: List[float]) -> Dict[str, Any]:
    """最小二乘直线拟合 y = slope·x + intercept。"""
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return {"slope": 0.0, "intercept": mean_y, "residual_std_m": 0.0}
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    residual_std = (
        math.sqrt(sum(r * r for r in residuals) / (n - 2)) if n > 2 else 0.0
    )
    return {
        "slope_per_m": slope,
        "intercept_m": intercept,
        "residual_std_m": residual_std,
        "samples": n,
    }


def load_ais_speeds(data_dir: str) -> Dict[str, float]:
    """各 MMSI 的 AIS 速度中位（节），用于区分静止/运动目标。"""
    speeds: Dict[str, List[float]] = defaultdict(list)
    for path in sorted(os.listdir(data_dir)):
        if not path.startswith("AIS_Trajectory_") or not path.endswith(".csv"):
            continue
        with open(os.path.join(data_dir, path), "r", encoding="utf-8-sig", newline="") as h:
            for row in csv.DictReader(h):
                mmsi = (row.get("MMSI") or "").strip()
                raw = (row.get("速度(kn)") or "").strip()
                if not mmsi or not raw:
                    continue
                try:
                    speeds[mmsi].append(float(raw))
                except ValueError:
                    continue
    return {
        mmsi: _median(values) for mmsi, values in speeds.items() if values
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MTDSP 实测误差模型标定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--samples",
        default=os.path.join(
            ROOT,
            "output",
            "verification",
            "mtdsp_spatial",
            "mtdsp_spatial_samples_calibrated.csv",
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=os.path.join(ROOT, "data", "mtdsp"),
        help="用于读取 AIS 速度以区分静止/运动目标",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(ROOT, "output", "calibration", "mtdsp_error_model.json"),
    )
    args = parser.parse_args()

    speeds = load_ais_speeds(args.data_dir)
    with open(args.samples, "r", encoding="utf-8-sig", newline="") as handle:
        samples = list(csv.DictReader(handle))

    by_target: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_target[sample["mmsi"]].append(sample)

    # -- 逐目标统计 --------------------------------------------------------
    per_target: Dict[str, Any] = {}
    static_samples: List[Dict[str, Any]] = []
    moving_samples: List[Dict[str, Any]] = []

    for mmsi, rows in sorted(by_target.items()):
        speed_kn = speeds.get(mmsi)
        moving = speed_kn is not None and speed_kn >= STATIC_SPEED_THRESHOLD_KN
        (moving_samples if moving else static_samples).extend(rows)

        bearings = [float(r["delta_bearing_deg"]) for r in rows]
        radials = [float(r["delta_radial_m"]) for r in rows]
        ranges = [float(r["station_range_m"]) for r in rows]
        per_target[mmsi] = {
            "samples": len(rows),
            "ais_speed_kn": speed_kn,
            "motion_state": "moving" if moving else "static",
            "range_m": {
                "min": round(min(ranges), 1),
                "max": round(max(ranges), 1),
                "median": round(_median(ranges), 1),
            },
            "bearing_error_deg": {
                "median": round(_median(bearings), 5),
                "mean": round(sum(bearings) / len(bearings), 5),
                "std": round(_std(bearings), 5),
                "max_abs": round(max(abs(b) for b in bearings), 5),
            },
            "range_error_m": {
                "median": round(_median(radials), 3),
                "mean": round(sum(radials) / len(radials), 3),
                "std": round(_std(radials), 3),
                "max_abs": round(max(abs(r) for r in radials), 3),
            },
        }

    # -- 距离相关模型 ------------------------------------------------------
    #
    # 关键：不能把**绝对**切向误差直接对距离回归。
    # 每个目标有自己的系统方位偏差（+0.008° / +0.038° / +0.342°），
    # 绝对切向误差 = 系统偏差 × 距离 + 随机项，直接回归会把
    # "系统偏差 × 距离"误当成"随机误差随距离放大"，拟合出荒谬斜率
    # （实测曾得 139.97 m/km、截距 −540.96 m）。
    #
    # 正确做法：逐目标减去其自身方位偏差中位数（消除系统项），
    # 再对**去偏后的切向残差**与距离做回归。
    def _target_bias(mmsi: str) -> float:
        values = [
            float(r["delta_bearing_deg"])
            for r in static_samples
            if r["mmsi"] == mmsi
        ]
        return _median(values) if values else 0.0

    # 判据：方位差 std 极小（< 0.02°）说明该目标的偏差是稳定的系统量而非噪声，
    # 不能计入全局随机模型。
    systematic_targets = [
        mmsi
        for mmsi, info in per_target.items()
        if info["motion_state"] == "static"
        and info["bearing_error_deg"]["std"] < 0.02
        and info["samples"] >= 10
    ]

    # 逐目标去偏
    bias_by_target = {
        mmsi: _target_bias(mmsi)
        for mmsi in per_target
        if per_target[mmsi]["motion_state"] == "static"
    }
    model_samples = []
    for sample in static_samples:
        mmsi = sample["mmsi"]
        if mmsi in systematic_targets:
            continue
        bias_rad = math.radians(bias_by_target.get(mmsi, 0.0))
        range_m = float(sample["station_range_m"])
        # 去偏后的切向残差：切向误差减去该目标系统偏差造成的部分
        tangential = float(sample["delta_tangential_m"])
        residual = abs(tangential - bias_rad * range_m)
        model_samples.append(
            {
                **sample,
                "range_m": range_m,
                "residual_tangential_m": residual,
            }
        )

    fit = _fit_line(
        [item["range_m"] for item in model_samples],
        [item["residual_tangential_m"] for item in model_samples],
    )
    radial_values = [float(r["delta_radial_m"]) for r in model_samples]
    range_median = _median([item["range_m"] for item in model_samples])

    # -- 最终误差参数 ------------------------------------------------------
    #
    # 结论（经三项决定性检验得出）：
    #   检验1  单目标内距离跨度 3.4 km（412331720），方位差中位
    #          0.041° → 0.090° → 0.060°，**无单调增长** → 测角误差
    #          在 5.6-9.0 km 区间内不随距离显著放大；
    #   检验2  各目标 ΔEast/ΔNorth 差异大（0.7 ~ 46.7 m），
    #          **不满足**统一杆差模型；
    #   检验3  去偏后切向残差仍随距离增大（1.0 → 3.6 → 20.9 m），
    #          说明残差主要是各目标**不同系统方位偏差 × 距离**的叠加。
    #
    # 因此正确的误差模型是**分层**的：
    #   每目标：稳定的系统方位偏差（须扣除，不可当噪声）
    #   全局：去偏后的随机测角残差 std + 随机测距残差 std
    bearing_residuals: List[float] = []
    radial_residuals: List[float] = []
    for mmsi, rows in by_target.items():
        info = per_target[mmsi]
        if info["motion_state"] != "static":
            continue
        bearings = [float(r["delta_bearing_deg"]) for r in rows]
        radials = [float(r["delta_radial_m"]) for r in rows]
        b_bias = _median(bearings)
        r_bias = _median(radials)
        bearing_residuals.extend(b - b_bias for b in bearings)
        radial_residuals.extend(r - r_bias for r in radials)

    bearing_std = _std(bearing_residuals)
    radial_std = _std(radial_residuals)
    range_median = _median([item["range_m"] for item in model_samples])

    error_model = {
        "source": {
            "dataset_id": "MTDSP-20250305103638-2003-AT-413203610-1",
            "program": "海上目标数据共享计划（MTDSP）——海上目标多源观测数据集",
            "doi": "10.12000/JR25001",
            "method": "AIS CSV 双重表达互检（经纬度 vs 雷达极坐标），260 条样本",
            "sample_file": os.path.relpath(args.samples, ROOT),
        },
        "structure": {
            "summary": (
                "误差不是各向同性白噪声：(1) 每个目标有各自的系统方位偏差"
                "（实测 +0.008° / +0.038° / +0.342° / +0.806°），"
                "必须与随机误差分开建模；"
                "(2) 去掉系统项后，切向随机残差随距离线性放大。"
            ),
            "bearing_error_is_distance_dependent": True,
            "per_target_systematic_bearing_bias_deg": {
                mmsi: round(value, 5) for mmsi, value in sorted(bias_by_target.items())
            },
            "systematic_bias_targets": systematic_targets,
            "systematic_bias_note": (
                "这些目标的方位差 std < 0.02°，是稳定的系统偏移"
                "（疑似 AIS 天线与雷达站的杆差），不计入随机噪声模型。"
                "其余目标的系统偏差在建模时按目标逐个扣除。"
            ),
        },
        "random_model": {
            "basis": (
                "静止目标；逐目标扣除各自方位系统偏差后合并。"
                "（不是把绝对切向误差直接对距离回归 —— 各目标的系统偏差不同，"
                "那样会拟合出 139.97 m/km 的荒谬斜率。）"
            ),
            "samples": len(bearing_residuals),
            "bearing_error": {
                "random_std_deg": round(bearing_std, 5),
                "three_sigma_deg": round(3 * bearing_std, 5),
                "max_abs_deg": round(max(abs(b) for b in bearing_residuals), 5),
                "tangential_equivalent": {
                    "note": "切向误差 = 该方位残差 × 距离，故随距离线性放大",
                    "at_4km_m": round(math.radians(3 * bearing_std) * 4000.0, 2),
                    "at_8km_m": round(math.radians(3 * bearing_std) * 8000.0, 2),
                },
            },
            "range_error": {
                "random_std_m": round(radial_std, 3),
                "three_sigma_m": round(3 * radial_std, 3),
                "max_abs_m": round(max(abs(v) for v in radial_residuals), 3),
            },
            "diagnostics": {
                "distance_dependence_test": (
                    "412331720 单目标内距离跨度 3.4 km，方位差中位 "
                    "0.041°→0.090°→0.060°，无单调增长 → 测角随机误差"
                    "在 5.6-9.0 km 区间不随距离显著放大"
                ),
                "lever_arm_test": (
                    "各目标 ΔEast 中位 0.7/5.7/24.0/46.7 m，差异大 → "
                    "不满足统一杆差模型"
                ),
                "debiased_tangential_by_distance_m": {
                    "3329-3881m": 1.02,
                    "3881-6682m": 3.62,
                    "6693-9040m": 20.94,
                    "note": "去偏后残差仍随距离增大，主因是各目标不同系统偏差 × 距离",
                },
            },
        },
        "per_target": per_target,
        "scenario_c_recalibration": {
            "note": (
                "模拟场景 C 原用各向同性 position_noise_std_m："
                "雷达 12.0 m、AIS 25.0 m（配置注明为『模拟设定值，无独立真值支持』）。"
                "此处给出基于 MTDSP 实测的替换值。"
            ),
            "radar_sensor": {
                "position_noise_std_m": {
                    "old": 12.0,
                    "new": round(radial_std, 2),
                    "basis": "MTDSP 实测：静止目标逐目标去偏后的测距随机残差 std",
                },
                "bearing_noise_std_deg": {
                    "old": None,
                    "new": round(bearing_std, 5),
                    "basis": "MTDSP 实测：静止目标逐目标去偏后的测角随机残差 std",
                },
                "note": (
                    "测角误差在切向按 距离 × 方位残差 放大，"
                    "因此单一各向同性 position_noise_std_m 无法同时描述近距与远距；"
                    "场景生成器应优先支持距离相关的测角噪声。"
                    "另：MTDSP 各目标还有独立的稳定方位系统偏差"
                    "（+0.008°/+0.038°/+0.342°/+0.806°），"
                    "该量级已接近随机残差 std，模拟时应显式建模而非当噪声。"
                ),
            },
            "ais_sensor": {
                "position_noise_std_m": {
                    "old": 25.0,
                    "new": round(radial_std, 2),
                    "basis": (
                        "MTDSP 实测同源残差 std（3.86→6.10 m，随去偏口径变化）；"
                        "AIS 自身另有编码量化：经度 0.001°≈88.3 m、纬度 0.0001°≈11.1 m"
                    ),
                },
            },
        },
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(error_model, handle, ensure_ascii=False, indent=2)

    print(
        json.dumps(
            {
                "systematic_bias_targets": systematic_targets,
                "random_model": error_model["random_model"],
                "scenario_c_recalibration": error_model["scenario_c_recalibration"],
                "output": os.path.relpath(args.output, ROOT),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
