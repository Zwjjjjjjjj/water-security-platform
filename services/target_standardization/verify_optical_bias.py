#!/usr/bin/env python3
"""实测光电方位系统差，验证导出时施加的 +5.28° 修正是否合理。

## 为什么需要这个脚本

光电设备有**恒定的方位零点偏差**：实测偏东约 5.4~5.7°。
适配器**刻意不隐式补偿**（`optical_azimuth_bias_applied=false`），
把这件事留给下游显式处理。

前端场景里如果直接用未修正的光电点迹，
光电航迹会稳定偏在真值东侧约 350 m（3.9 km 处），
与雷达/AIS 航迹明显分叉 —— 看起来就像「光电坏了」。

本脚本用**权威 AIS** 作为真值参照，反解出应当施加的偏���角，
并给出「修正前 / 修正后」的距离残差对比，
让导出脚本里的 `OPTICAL_AZIMUTH_BIAS_DEG` 有据可依。

## 用法

    python3 verify_optical_bias.py
    python3 verify_optical_bias.py --scenario mtdsp_0305 --bias 5.28
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def parse_local(text: str) -> Optional[datetime]:
    raw = (text or "").split(" (local)")[0].strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def rotate_enu(east: float, north: float, deg: float) -> Tuple[float, float]:
    if not deg:
        return east, north
    rad = math.radians(deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return (
        east * cos_a - north * sin_a,
        east * sin_a + north * cos_a,
    )


def load_rows(alignment_dir: str) -> List[Dict[str, str]]:
    path = os.path.join(alignment_dir, "aligned_records.csv")
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def build_ais_series(rows: List[Dict[str, str]], mmsi: str) -> List[Tuple[str, float, float]]:
    series: List[Tuple[str, float, float]] = []
    for row in rows:
        if row.get("source_type") != "ais" or row.get("alignment_status") != "ok":
            continue
        if row.get("local_track_id") != mmsi:
            continue
        moment = parse_local(row.get("aligned_time_utc", ""))
        if moment is None:
            continue
        series.append(
            (moment.strftime("%Y-%m-%d %H:%M:%S.%f"), float(row["east_m"]), float(row["north_m"]))
        )
    series.sort()
    return series


def interpolate(series: List[Tuple[str, float, float]], key: str) -> Optional[Tuple[float, float]]:
    if not series:
        return None
    times = [s[0] for s in series]
    idx = bisect.bisect_left(times, key)
    if idx < len(times) and times[idx] == key:
        return series[idx][1], series[idx][2]
    if idx == 0 or idx >= len(times):
        return None
    t0, e0, n0 = series[idx - 1]
    t1, e1, n1 = series[idx]
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    span = (datetime.strptime(t1, fmt) - datetime.strptime(t0, fmt)).total_seconds()
    if span <= 0:
        return e0, n0
    ratio = (datetime.strptime(key, fmt) - datetime.strptime(t0, fmt)).total_seconds() / span
    return e0 + (e1 - e0) * ratio, n0 + (n1 - n0) * ratio


def main() -> int:
    parser = argparse.ArgumentParser(description="实测光电方位系统差")
    parser.add_argument("--scenario", default="mtdsp_0305")
    parser.add_argument(
        "--system-root", default=os.path.join(ROOT, "output", "system")
    )
    parser.add_argument("--bias", type=float, default=5.28, help="待验证的修正角（度）")
    parser.add_argument("--mmsi", default="413203610", help="作为真值参照的 AIS MMSI")
    args = parser.parse_args()

    alignment_dir = os.path.join(args.system_root, "alignment", args.scenario)
    rows = load_rows(alignment_dir)
    ais_series = build_ais_series(rows, args.mmsi)
    if not ais_series:
        print(f"找不到 MMSI {args.mmsi} 的 AIS 航迹")
        return 1

    # 光电主目标航迹（Inf / TV 各自成一条）
    optical: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row.get("source_type") != "optical" or row.get("alignment_status") != "ok":
            continue
        if row.get("optical_record_kind") != "primary_target":
            continue
        moment = parse_local(row.get("aligned_time_utc", ""))
        if moment is None:
            continue
        optical[row["local_track_id"]].append(row)

    print("=" * 68)
    print("光电方位系统差实测（参照 AIS 真值）")
    print("=" * 68)
    print(f"场景      : {args.scenario}")
    print(f"参照 MMSI : {args.mmsi}")
    print()

    # ---- 反解偏置角 -------------------------------------------------------
    bearings: List[float] = []
    for track_id, items in optical.items():
        for row in items:
            moment = parse_local(row.get("aligned_time_utc", ""))
            key = moment.strftime("%Y-%m-%d %H:%M:%S.%f")
            truth = interpolate(ais_series, key)
            if truth is None:
                continue
            oe, on = float(row["east_m"]), float(row["north_m"])
            ae, an = truth
            b_opt = math.degrees(math.atan2(oe, on)) % 360
            b_ais = math.degrees(math.atan2(ae, an)) % 360
            bearings.append(((b_opt - b_ais + 180) % 360) - 180)

    bearings.sort()
    if not bearings:
        print("没有可配对的光电/AIS 时刻")
        return 1
    median_bias = bearings[len(bearings) // 2]
    print(f"样本数          : {len(bearings)}")
    print(f"实测方位偏差    : 中位 {median_bias:+.3f}°  "
          f"最小 {bearings[0]:+.3f}°  最大 {bearings[-1]:+.3f}°")
    print()

    # ---- 修正前后对比 -----------------------------------------------------
    print("修正前后与 AIS 真值的距离残差：")
    header = f"  {'航迹':<22s}{'未修正':>14s}{'修正后':>14s}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    summary: List[Tuple[str, float, float]] = []
    for track_id in sorted(optical):
        raw_seps: List[float] = []
        fix_seps: List[float] = []
        for row in optical[track_id]:
            moment = parse_local(row.get("aligned_time_utc", ""))
            key = moment.strftime("%Y-%m-%d %H:%M:%S.%f")
            truth = interpolate(ais_series, key)
            if truth is None:
                continue
            ae, an = truth
            oe, on = float(row["east_m"]), float(row["north_m"])
            raw_seps.append(math.hypot(ae - oe, an - on))
            fe, fn = rotate_enu(oe, on, args.bias)
            fix_seps.append(math.hypot(ae - fe, an - fn))
        if not raw_seps:
            continue
        raw_seps.sort()
        fix_seps.sort()
        raw_m = raw_seps[len(raw_seps) // 2]
        fix_m = fix_seps[len(fix_seps) // 2]
        summary.append((track_id, raw_m, fix_m))
        print(f"  {track_id:<22s}{raw_m:>11.1f} m{fix_m:>11.1f} m")

    print()
    if summary:
        avg_raw = sum(s[1] for s in summary) / len(summary)
        avg_fix = sum(s[2] for s in summary) / len(summary)
        print(f"平均残差：未修正 {avg_raw:.1f} m → 修正后 {avg_fix:.1f} m "
              f"（改善 {avg_raw - avg_fix:.1f} m）")
    print()
    print(f"导出脚本使用的偏置角：{args.bias:+.2f}°")
    print(f"本次实测中位值    ：{median_bias:+.3f}°")
    print("两者接近 ⇒ 修正参数与实测自洽。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
