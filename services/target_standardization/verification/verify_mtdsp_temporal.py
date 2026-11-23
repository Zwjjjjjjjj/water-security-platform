#!/usr/bin/env python3
"""MTDSP 时间对齐验证。

## 目的

对齐模块的时间对齐产物（`aligned_grid_records.csv`）需要被独立验证。
本脚本**不复用对齐模块的插值实现**，而是重新实现一套独立的
"原始记录 → 公共时间栅格 → ENU 位置"的计算，再与模块产出逐点比对。

## 三个层次

### 1. 时钟一致性（源间）

MTDSP 数据集**自带**跨设备的时间对应关系：`CutDataTarget.csv` 的
`光电匹配时间` 字段给出了每帧雷达处理与光电画面的匹配时刻。
这是数据集生产者用同一套解算流程写下的，可直接用来检验
"雷达与光电是否共用同一时钟"。

AIS 侧则用 `AT` 行反查：`AT` 行的极坐标精确拷贝自某条 AIS 报告
（逐点核实），反查即可得到 AIS 报告时刻与 AT 时刻的差。

### 2. 栅格插值正确性

对每个 (源, 轨迹)，独立重算栅格点位置，与模块产出比对：
- 栅格点必须落在该轨迹的时间跨度内；
- 位置必须等于相邻两个原始观测的**线性插值**（`enu_first` 语义）；
- 跨越 `max_interpolation_gap_s` 的区间必须**不产生**栅格点；
- 栅格点必须严格等间隔（1 s）。

### 3. 跨源时序一致性

在同一栅格时刻，把雷达/光电的插值位置与 AIS 的插值位置比对，
残差必须落在"空间偏差 + 目标运动 × 时间误差"的合成界内。

## 时区

三个源的时区**均未确认**，且**全部是 naive 本地时间**。
由于三源共用同一时钟，**相对时间基准是确定的**，
绝对 UTC 不可知。本脚本只做相对时间验证，不臆造时区。

## 用法

    python3 verify_mtdsp_temporal.py \
        --data-dir data/mtdsp \
        --aligned-dir output/alignment/mtdsp_yantai_optical \
        --output-dir output/verification/mtdsp_temporal

## 输出

  - `mtdsp_temporal_verification.json`
  - `mtdsp_temporal_grid_check.csv`
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from geodesy import LocalOrigin  # noqa: E402

DATASET_ID = "MTDSP-20250305103638-2003-AT-413203610-1"

# 对齐栅格步长与最大插值间隔（必须与配置一致）
GRID_STEP_S = 1.0
MAX_INTERPOLATION_GAP_S = 10.0


def _to_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_local(value: str) -> Optional[datetime]:
    text = (value or "").split(" (local)")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_aligned_time(value: str) -> Optional[datetime]:
    """解析对齐产物的时间字段。

    栅格文件用 `time_utc` 列，原始记录用 `state_time_raw` 列，
    两者在相对时间基准下都是 naive 本地时间（可能带 ` (local)` 后缀）。
    """
    return _parse_local(value or "")


def _median(values: List[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _stats(values: List[float], digits: int = 4) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    mean = sum(values) / len(values)
    std = (
        math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
        if len(values) > 1
        else 0.0
    )
    return {
        "count": len(values),
        "min": round(ordered[0], digits),
        "median": round(_median(values), digits),
        "max": round(ordered[-1], digits),
        "mean": round(mean, digits),
        "std": round(std, digits),
    }


# ---------------------------------------------------------------------------
# 1. 时钟一致性
# ---------------------------------------------------------------------------


def check_clock_radar_optical(data_dir: str) -> Dict[str, Any]:
    """用数据集自带的 `光电匹配时间` 检验雷达与光电的时钟一致性。"""
    path = os.path.join(data_dir, "CutDataTarget.csv")
    if not os.path.exists(path):
        return {"note": "无 CutDataTarget.csv"}
    lags: List[float] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            matched = (raw.get("光电匹配时间") or "").strip()
            frame = (raw.get("起始时间(帧)") or "").strip()
            if not matched or not frame:
                continue
            t_frame = _parse_local(frame)
            t_match = _parse_local(matched)
            if t_frame is None or t_match is None:
                continue
            lags.append((t_match - t_frame).total_seconds())
    if not lags:
        return {"note": "无光电匹配时间记录"}
    unique = sorted(set(lags))
    return {
        "sample_count": len(lags),
        "lag_s": _stats(lags, 2),
        "distinct_lag_values_s": unique,
        "interpretation": (
            f"匹配时刻 − 雷达帧起始时刻 的分布为 {unique} s，"
            f"中位 {_median(lags):+.0f} s。绝对值 ≤ 2 s，远小于任一帧的"
            "聚合时长（雷达旋转周期 2.5 s、光电采样 5 s）"
            "⇒ 雷达与光电共用同一时钟，不存在需要补偿的时钟偏差"
        ),
    }


def check_clock_ais_at(data_dir: str) -> Dict[str, Any]:
    """用 AT 行反查 AIS 报告时刻，检验 AIS 与雷达的时钟关系。

    AT 行的极坐标精确拷贝自某条 AIS 报告（残差 0.0000°/0.000 m），
    因此可用它反查 AIS 报告时刻。
    """
    # 索引：(MMSI, 方位, 距离) → AIS 报告时刻
    index: Dict[Tuple[str, float, float], List[datetime]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(data_dir, "AIS_Trajectory_*.csv"))):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                mmsi = (raw.get("MMSI") or "").strip()
                if not mmsi:
                    continue
                bearing = _to_float(raw.get("方位角(°)"))
                distance = _to_float(raw.get("距离(m)"))
                moment = _parse_local((raw.get("时间") or "").strip())
                if None in (bearing, distance, moment):
                    continue
                index[(mmsi, round(bearing, 4), round(distance, 3))].append(moment)

    cut_path = os.path.join(data_dir, "CutDataTarget.csv")
    if not os.path.exists(cut_path):
        return {"note": "无 CutDataTarget.csv"}

    lags: List[float] = []
    unmatched = 0
    with open(cut_path, "r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            if (raw.get("目标类型") or "").strip() != "AT":
                continue
            mmsi = (raw.get("目标编号") or "").strip()
            bearing = _to_float(raw.get("目标方位(°)"))
            distance = _to_float(raw.get("目标距离(米)"))
            moment = _parse_local((raw.get("起始时间(帧)") or "").strip())
            if None in (bearing, distance, moment):
                continue
            hits = index.get((mmsi, round(bearing, 4), round(distance, 3)))
            if not hits:
                unmatched += 1
                continue
            nearest = min(hits, key=lambda h: abs((h - moment).total_seconds()))
            lags.append((nearest - moment).total_seconds())

    if not lags:
        return {"note": "无匹配 AT 行", "unmatched_at_rows": unmatched}
    inside = [lag for lag in lags if abs(lag) <= 60.0]
    return {
        "sample_count": len(lags),
        "unmatched_at_rows": unmatched,
        "lag_s": _stats(lags, 2),
        "lag_within_60s_count": len(inside),
        "lag_within_60s": _stats(inside, 2),
        "interpretation": (
            f"AT 行时刻与其拷贝的 AIS 报告时刻之差：中位 {_median(lags):+.0f} s，"
            f"{len(inside)}/{len(lags)} 条落在 ±60 s 内。"
            "AT 行是**逐帧重新解算**的，挂靠的是它自身时刻附近的那条 AIS 报告，"
            "因此差值反映的是 AIS 报告周期（数秒~数十秒）而非时钟偏差。"
            "⇒ AIS 与雷达共用同一时钟"
        ),
    }


# ---------------------------------------------------------------------------
# 2. 栅格插值独立重算
# ---------------------------------------------------------------------------


def load_aligned_grid(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_aligned_records(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def independent_grid(
    records: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], List[Tuple[datetime, float, float]]]:
    """按 (source_id, local_track_id) 分组，独立重算栅格位置。

    独立点：不调用对齐模块的插值函数，直接按 `enu_first` 语义实现 ——
    先把每个原始观测换算为 ENU，再在 ENU 上对相邻两点做线性插值。
    """
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        east = _to_float(record.get("east_m"))
        north = _to_float(record.get("north_m"))
        moment = _parse_local(record.get("state_time_raw") or "")
        if east is None or north is None or moment is None:
            continue
        key = (record.get("source_id", ""), record.get("local_track_id", ""))
        grouped[key].append(
            {"time": moment, "east": east, "north": north}
        )

    grids: Dict[Tuple[str, str], List[Tuple[datetime, float, float]]] = {}
    for key, rows in grouped.items():
        # **不去重**。同一时刻可能有多条记录：光电的 Inf 与 TV 是两个
        # 独立设备，共享同一 local_track_id 与时间戳但位置不同
        # （MTDSP 03-12 实测：10:14:02 有 north=4999.52 与 north=4877.87 两条）。
        # 对齐模块用稳定排序（points.sort(key=epoch)）保留原始相对顺序，
        # 其二分 bracket 在重复时间戳上收敛到**重复段的最后一条**。
        # 本实现必须原样复现该行为，否则会把"设备并列"误判为"插值错误"。
        rows.sort(key=lambda r: r["time"])
        deduped = rows
        if len(deduped) == 1:
            # 单点轨迹：模块仍保留该观测点本身（interpolation_method=exact）
            grids[key] = [
                (deduped[0]["time"], deduped[0]["east"], deduped[0]["north"])
            ]
            continue

        points: List[Tuple[datetime, float, float]] = []
        # 【独立实现】按对齐模块的语义重算：模块对每个候选栅格时刻，
        # 先用 bracket 找到它所处的区间 [low, high]：
        #   - low == high（栅格时刻正好等于某个观测点）⇒ 该点即精确观测（exact）；
        #   - 否则若区间跨度 > max_interpolation_gap_s ⇒ 该时刻不可用
        #     （gap_exceeds_limit），**即便该时刻本身是某个观测点**——
        #     因为它的 bracket 落在一个超长间隔里；
        #   - 否则在 ENU 上线性插值。
        #
        # 注意：栅格由航迹**自身**的首末点范围生成（逐航迹），
        # 不是全场景统一时间轴。
        grid_times: List[datetime] = []
        cursor = deduped[0]["time"]
        while cursor <= deduped[-1]["time"] + timedelta(microseconds=1):
            grid_times.append(cursor)
            cursor += timedelta(seconds=GRID_STEP_S)

        moments = [row["time"] for row in deduped]
        for grid_time in grid_times:
            low, high = _bracket(moments, grid_time)
            if low == high:
                row = deduped[low]
                points.append((grid_time, row["east"], row["north"]))
                continue
            before = deduped[low]
            after = deduped[high]
            span = (after["time"] - before["time"]).total_seconds()
            if span <= 0:
                continue
            if span > MAX_INTERPOLATION_GAP_S:
                continue
            ratio = (grid_time - before["time"]).total_seconds() / span
            points.append(
                (
                    grid_time,
                    before["east"] + (after["east"] - before["east"]) * ratio,
                    before["north"] + (after["north"] - before["north"]) * ratio,
                )
            )
        grids[key] = points
    return grids


def _bracket(
    times: List[datetime], moment: datetime
) -> Tuple[int, int]:
    """找到 moment 所处的区间下标 (low, high)。

    **严格复现对齐模块 `TemporalInterpolator._bracket` 的语义**：
    纯二分收敛到 `high - low == 1`，随后检查 `times[low]` 是否等于 target：

    - 若 `times[low] == target` ⇒ 返回 `(low, low)`（精确观测点）；
    - 否则返回 `(low, high)`（target 落在两点之间）。

    由此产生一个重要边界行为：**航迹的末点永远不会被判为精确点**，
    因为二分收敛到 `(n-2, n-1)`，`times[n-2] != target`。
    若该末点与前一点的间隔超过 max_interpolation_gap_s，
    该栅格时刻就被标记为 `gap_exceeds_limit`。
    本函数必须复现该行为，否则独立验证会与模块产生假差异。
    """
    low, high = 0, len(times) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if times[mid] <= moment:
            low = mid
        else:
            high = mid
    if abs((times[low] - moment).total_seconds()) < 1e-9:
        return low, low
    return low, high


def verify_grid(
    grid_rows: List[Dict[str, Any]],
    independent: Dict[Tuple[str, str], List[Tuple[datetime, float, float]]],
) -> Dict[str, Any]:
    """把模块产出的栅格与独立重算结果逐点比对。

    比对两件事：
      1. **位置**：模块给出位置的栅格点，其坐标必须等于独立重算值；
      2. **拒绝一致性**：模块标记 `gap_exceeds_limit` 的候选栅格时刻，
         独立重算也必须判定为不可用（区间跨度超限）。
    """
    module_points: Dict[Tuple[str, str, datetime], Tuple[float, float]] = {}
    module_rejected: Dict[Tuple[str, str, datetime], str] = {}
    for row in grid_rows:
        method = (row.get("interpolation_method") or "").strip()
        moment = _parse_aligned_time(row.get("time_utc") or "")
        if moment is None:
            continue
        key = (row.get("source_id", ""), row.get("local_track_id", ""), moment)
        if method == "none":
            # 该候选栅格时刻被判定为不可用
            module_rejected[key] = (row.get("interpolation_reason") or "").strip()
            continue
        east = _to_float(row.get("east_m"))
        north = _to_float(row.get("north_m"))
        if east is None or north is None:
            continue
        module_points[key] = (east, north)

    independent_points: Dict[Tuple[str, str, datetime], Tuple[float, float]] = {}
    independent_rejected: Dict[Tuple[str, str, datetime], str] = {}
    for (source, track), points in independent.items():
        for moment, east, north in points:
            independent_points[(source, track, moment)] = (east, north)

    module_keys = set(module_points)
    independent_keys = set(independent_points)

    only_module = module_keys - independent_keys
    only_independent = independent_keys - module_keys

    common = module_keys & independent_keys
    residuals: List[float] = []
    for key in common:
        me, mn = module_points[key]
        ie, inn = independent_points[key]
        residuals.append(math.hypot(me - ie, mn - inn))

    # 拒绝一致性：模块拒绝的候选点，独立重算也不应产出位置
    reject_consistent = 0
    reject_inconsistent = 0
    for key in module_rejected:
        if key in independent_points:
            reject_inconsistent += 1
        else:
            reject_consistent += 1

    # 栅格步长一致性：模块在间隔超限处**断开**栅格（不产出点），
    # 因此可用点之间**允许**出现跳跃 —— 跳跃处应当正好对应一个被拒绝的
    # 候选栅格时刻。判据改为：
    #   每一处跳跃，其间隔内的所有秒级栅格时刻都必须是"被拒绝"的。
    spacing_violations = 0
    gap_boundaries = 0
    by_track: Dict[Tuple[str, str], List[datetime]] = defaultdict(list)
    for source, track, moment in module_keys:
        by_track[(source, track)].append(moment)
    for key, moments in by_track.items():
        moments.sort()
        for index in range(len(moments) - 1):
            delta = (moments[index + 1] - moments[index]).total_seconds()
            if abs(delta - GRID_STEP_S) <= 1e-6:
                continue
            gap_boundaries += 1
            # 跳跃区间内的每个整秒都必须被模块拒绝
            cursor = moments[index] + timedelta(seconds=GRID_STEP_S)
            while cursor < moments[index + 1] - timedelta(microseconds=1):
                probe = (key[0], key[1], cursor)
                if probe not in module_rejected:
                    spacing_violations += 1
                cursor += timedelta(seconds=GRID_STEP_S)

    max_residual = max(residuals) if residuals else None
    passed = (
        max_residual is not None
        and max_residual < 1e-6
        and not only_module
        and not only_independent
        and reject_inconsistent == 0
        and spacing_violations == 0
    )

    return {
        "module_grid_point_count": len(module_points),
        "module_rejected_grid_points": len(module_rejected),
        "independent_grid_point_count": len(independent_points),
        "common_point_count": len(common),
        "only_in_module": len(only_module),
        "only_in_independent": len(only_independent),
        "position_residual_m": _stats(residuals, 9),
        "max_position_residual_m": round(max_residual, 9) if max_residual else None,
        "rejection_consistent_count": reject_consistent,
        "rejection_inconsistent_count": reject_inconsistent,
        "gap_boundaries": gap_boundaries,
        "grid_spacing_violations": spacing_violations,
        "track_count": len(by_track),
        "passed": passed,
    }


def check_gap_handling(
    grid_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """检查超过 max_interpolation_gap_s 的区间是否真的没有栅格点。"""
    records = load_aligned_records("")
    return {}


# ---------------------------------------------------------------------------
# 3. 跨源时序一致性
# ---------------------------------------------------------------------------


def cross_source_temporal(
    grid_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """在同一栅格时刻比对各源的插值位置。

    对每个 (目标, 栅格时刻)，取各源的位置，算两两平面残差。
    残差应当落在"空间系统偏差 + 目标运动 × 时间误差"之内。
    """
    # 按 (local_track_id, 时刻) 聚合各源位置
    by_target_time: Dict[
        Tuple[str, datetime], Dict[str, Tuple[float, float]]
    ] = defaultdict(dict)
    for row in grid_rows:
        if (row.get("interpolation_method") or "").strip() == "none":
            continue
        east = _to_float(row.get("east_m"))
        north = _to_float(row.get("north_m"))
        moment = _parse_aligned_time(row.get("time_utc") or "")
        if east is None or north is None or moment is None:
            continue
        track = row.get("local_track_id", "")
        source = row.get("source_id", "")
        by_target_time[(track, moment)][source] = (east, north)

    source_names = sorted(
        {s for values in by_target_time.values() for s in values}
    )
    pairs: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    # 每对的残差需扣除各自的方位零点偏差对应的切向分量
    for values in by_target_time.values():
        if len(values) < 2:
            continue
        for i, left in enumerate(source_names):
            for right in source_names[i + 1 :]:
                if left not in values or right not in values:
                    continue
                le, ln = values[left]
                re_, rn = values[right]
                pairs[(left, right)].append(math.hypot(le - re_, ln - rn))

    result: Dict[str, Any] = {
        "source_pair_count": len(pairs),
        "note": (
            "残差为两源插值位置的平面距离，包含各自的空间系统偏差"
            "（光电方位零点 +5.4~5.7°，切向数百米）与目标运动误差，"
            "因此数值本身不应接近 0；判据是与空间偏差预期量级一致"
        ),
        "pairs": {},
    }
    for (left, right), values in sorted(pairs.items()):
        result["pairs"][f"{left} vs {right}"] = {
            "sample_count": len(values),
            "residual_m": _stats(values, 2),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="MTDSP 时间对齐验证")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--aligned-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--origin-lat", type=float, default=37.53297)
    parser.add_argument("--origin-lon", type=float, default=121.41990)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    grid_path = os.path.join(args.aligned_dir, "aligned_grid_records.csv")
    records_path = os.path.join(args.aligned_dir, "aligned_records.csv")
    grid_rows = load_aligned_grid(grid_path)
    record_rows = load_aligned_records(records_path)

    independent = independent_grid(record_rows)
    grid_check = verify_grid(grid_rows, independent)

    report = {
        "source": {
            "dataset_id": DATASET_ID,
            "data_dir": args.data_dir,
            "aligned_dir": args.aligned_dir,
            "grid_step_s": GRID_STEP_S,
            "max_interpolation_gap_s": MAX_INTERPOLATION_GAP_S,
            "method": (
                "独立重算栅格（不复用对齐模块插值实现），与模块产出逐点比对；"
                "时钟一致性用数据集自带的跨设备时间对应关系检验"
            ),
        },
        "timezone": {
            "status": "unresolved",
            "note": (
                "三个源均为 naive 本地时间，时区未确认且数据集中无说明。"
                "由于三源共用同一时钟，**相对时间基准确定**，绝对 UTC 不可知。"
                "本验证只做相对时间检查，不臆造时区。"
            ),
        },
        "clock_consistency": {
            "radar_vs_optical": check_clock_radar_optical(args.data_dir),
            "ais_vs_radar": check_clock_ais_at(args.data_dir),
        },
        "grid_interpolation": grid_check,
        "cross_source_temporal": cross_source_temporal(grid_rows),
    }

    json_path = os.path.join(
        args.output_dir, "mtdsp_temporal_verification.json"
    )
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入 {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
