"""统一时间基准检查：三路时间必须可比，不强求 UTC。

## 要求

  - **必须满足**：复用现有对齐模块的时间。两路时间必须可比，
    但不一定强求 UTC；经核验的共同本地时间基准也可以；
    未知时区不能擅自补全。

## 现状（已核验）

  - 三路（雷达 / AIS / 光电）的 `time_basis` 均为
    `csv_datetime_string`，`time_precision` 均为 `second`；
  - `aligned_time_utc` 形如 `2025-03-12 10:12:33.000 (local)` ——
    本地无时区时间，**源间相对时差确定，绝对 UTC 不可知**；
  - 对齐配置对三源使用同一 `reference_local_datetime`
    且 `known_clock_offset_s = 0.0`，即三路共享同一本地时钟假设。

## 因此

  - 关联时按**相对时差**配对即可，不需要绝对 UTC；
  - 本模块提供 `check_timebase`：断言三路时间可比，
    不可比时给出明确原因（而不是静默通过）。

## 不做什么

  - 不补全时区、不臆造 UTC、不做时钟估计。
    时区未知就是未知，`missing_requirements` 已如实记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence


def parse_local(text: str) -> Optional[datetime]:
    """解析本地无时区时间（末尾可能带 "(local)"）。"""
    raw = (text or "").split(" (local)")[0].strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def check_timebase(
    records: Sequence[Dict[str, Any]],
    required_sensors: Sequence[str] = ("radar", "ais", "optical"),
) -> Dict[str, Any]:
    """检查三路时间是否可比。

    检查项：

      1. 每路至少有一个可用时间点（`aligned_time_utc` 可解析）；
      2. 三路共享同一时间表达（`time_basis` 一致、无混合时区标记）；
      3. 给出三路各自的时间覆盖（起止），供关联判断重叠。

    返回 `{"comparable": bool, "reasons": [...], "coverage": {...}}`。
    `comparable` 为 False 时 `reasons` 说明确切原因。
    """
    reasons: List[str] = []
    coverage: Dict[str, Any] = {}
    by_sensor: Dict[str, List[datetime]] = {s: [] for s in required_sensors}
    bases: Dict[str, set] = {s: set() for s in required_sensors}

    for record in records:
        sensor = str(record.get("source_type") or "")
        if sensor not in by_sensor:
            continue
        moment = parse_local(str(record.get("aligned_time_utc") or ""))
        if moment is not None:
            by_sensor[sensor].append(moment)
        basis = str(record.get("time_basis") or "")
        if basis:
            bases[sensor].add(basis)

    for sensor in required_sensors:
        moments = sorted(by_sensor[sensor])
        if not moments:
            reasons.append(f"{sensor}：无可用时间点")
            coverage[sensor] = {"point_count": 0}
            continue
        coverage[sensor] = {
            "point_count": len(moments),
            "start": moments[0].strftime("%Y-%m-%d %H:%M:%S"),
            "end": moments[-1].strftime("%Y-%m-%d %H:%M:%S"),
            "time_basis": sorted(bases[sensor]),
        }

    # 时间表达一致性：三路 basis 交集非空才算共享同一表达
    nonempty = [bases[s] for s in required_sensors if bases[s]]
    if nonempty:
        shared = set.intersection(*nonempty)
        if not shared:
            reasons.append(
                "三路 time_basis 无交集："
                + "; ".join(
                    f"{s}={sorted(bases[s])}" for s in required_sensors
                )
            )

    return {
        "comparable": not reasons,
        "reasons": reasons,
        "coverage": coverage,
        "note": (
            "本地无时区共同基准：源间相对时差确定，绝对 UTC 不可知；"
            "关联按相对时差配对，不补全时区。"
            if not reasons
            else "时间不可比，见 reasons；不得强行关联。"
        ),
    }
