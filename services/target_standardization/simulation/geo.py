"""局部地理坐标工具。

**坐标数学的唯一来源是项目根目录的 `geodesy` 模块**（WGS84 ECEF ↔ 局部 ENU）。
本模块仅作兼容导出，避免场景生成与时空对齐使用两套不同的坐标定义
——那会把平面近似的系统偏差混入评测误差。

保留 `LocalOrigin` 的导入路径以兼容既有调用。
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from geodesy import (  # noqa: F401
    EARTH_RADIUS_M,
    METERS_PER_DEG_LAT_APPROX as METERS_PER_DEG_LAT,
    Enu,
    LocalOrigin,
    bearing_rad,
    ecef_to_geodetic,
    enu_offset_to_bearing,
    enu_offset_to_speed,
    geodetic_to_ecef,
    haversine_m,
)


def fit_polyline(
    points: list,
) -> Optional[list]:
    """占位：保留接口，实际拟合在 trajectory 模块中实现。"""
    return points
