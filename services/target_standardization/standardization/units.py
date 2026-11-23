"""距离、速度与角度单位换算。

换算常数取自航海常用定义：
- 1 海里(nm) = 1852 m（国际海里）
- 1 节(kn) = 1 nm/h = 1852/3600 m/s
- 1 英里(mile) = 1609.344 m

TTM 第 10 字段为 "Speed/distance units"，同时规定距离与速度单位：

    标记 | 距离单位 | 速度单位
    -----+---------+----------
    N   | 海里     | 节
    K   | 千米     | 千米/小时
    S   | 英里     | 英里/小时
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

NM_TO_M = 1852.0
KN_TO_MPS = NM_TO_M / 3600.0
KM_TO_M = 1000.0
KMH_TO_MPS = 1000.0 / 3600.0
MILE_TO_M = 1609.344
MPH_TO_MPS = MILE_TO_M / 3600.0

#: 通用距离单位表（用于非 TTM 来源按单位标记直接换算）
DISTANCE_UNITS: Dict[str, Tuple[str, float]] = {
    "N": ("nautical_mile", NM_TO_M),
    "K": ("kilometre", KM_TO_M),
    "M": ("metre", 1.0),
    "MI": ("statute_mile", MILE_TO_M),
}

#: 通用速度单位表
SPEED_UNITS: Dict[str, Tuple[str, float]] = {
    "K": ("knot", KN_TO_MPS),
    "M": ("metre_per_second", 1.0),
    "N": ("kilometre_per_hour", KMH_TO_MPS),
    "MI": ("statute_mile_per_hour", MPH_TO_MPS),
}

#: TTM 第 10 字段标记 → (距离单位名, 距离系数, 速度单位名, 速度系数)
TTM_UNIT_PAIRS: Dict[str, Tuple[str, float, str, float]] = {
    "N": ("nautical_mile", NM_TO_M, "knot", KN_TO_MPS),
    "K": ("kilometre", KM_TO_M, "kilometre_per_hour", KMH_TO_MPS),
    "S": ("statute_mile", MILE_TO_M, "statute_mile_per_hour", MPH_TO_MPS),
}


def deg_to_rad(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return math.radians(value)


def rad_to_deg(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return math.degrees(value)


def distance_to_meters(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """距离统一为米。unit 取 N/K/M（海里/公里/米），未知单位返回 None。"""
    if value is None or not unit:
        return None
    key = str(unit).strip().upper()[:1]
    if key not in DISTANCE_UNITS:
        return None
    return float(value) * DISTANCE_UNITS[key][1]


def speed_to_mps(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """速度统一为 m/s。unit 取 K/M/N（节/m·s⁻¹/km·h⁻¹）。"""
    if value is None or not unit:
        return None
    key = str(unit).strip().upper()[:1]
    if key not in SPEED_UNITS:
        return None
    return float(value) * SPEED_UNITS[key][1]


def distance_unit_name(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    key = str(unit).strip().upper()[:1]
    return DISTANCE_UNITS.get(key, (None, None))[0]


def speed_unit_name(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    key = str(unit).strip().upper()[:1]
    return SPEED_UNITS.get(key, (None, None))[0]


def ttm_unit_pair(unit: Optional[str]) -> Optional[Tuple[str, float, str, float]]:
    """按 TTM 第 10 字段标记返回 (距离单位名, 距离系数, 速度单位名, 速度系数)。

    未识别的标记返回 None（不做任何默认假设）。
    """
    if not unit:
        return None
    key = str(unit).strip().upper()[:1]
    return TTM_UNIT_PAIRS.get(key)
