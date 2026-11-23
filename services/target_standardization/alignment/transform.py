"""坐标转换：ECEF ↔ 现场局部 ENU。

**坐标数学统一由根目录 `geodesy` 模块提供**，本模块只做场景相关的语义处理：

  - 雷达距离/真方位 → 站址切平面相对东/北 → 叠加到站址 ECEF → 现场 ENU；
  - 雷达方位参考（T/M/R）的换算语义；
  - 位置参考点偏移的处理。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from geodesy import (
    Ecef,
    Enu,
    LocalOrigin,
    bearing_rad,
    ecef_to_geodetic,
    geodetic_to_ecef,
    haversine_m,
)

__all__ = [
    "Ecef",
    "Enu",
    "LocalOrigin",
    "SiteFrame",
    "geodetic_to_ecef",
    "ecef_to_geodetic",
    "haversine_m",
    "bearing_rad",
    "latlon_to_enu",
    "radar_polar_to_enu",
]


@dataclass(frozen=True)
class SiteFrame:
    """现场坐标基准（现场局部 ENU 的原点与基准）。"""

    frame_id: str
    datum: str
    origin_latitude_deg: float
    origin_longitude_deg: float
    origin_height_m: float = 0.0

    @property
    def origin(self) -> LocalOrigin:
        return LocalOrigin(
            latitude_deg=self.origin_latitude_deg,
            longitude_deg=self.origin_longitude_deg,
            height_m=self.origin_height_m,
        )


def latlon_to_enu(
    frame: SiteFrame,
    latitude_deg: float,
    longitude_deg: float,
    height_m: float = 0.0,
) -> Enu:
    """经纬度 → 现场 ENU。"""
    return frame.origin.to_enu_full(latitude_deg, longitude_deg, height_m)


class FrameConverter:
    """供独立校验使用的转换适配器。

    把 `SiteFrame` 包装成校验器需要的接口（`enu` / `inverse`），便于
    `alignment.verification` 在不依赖被校验代码内部实现的前提下调用它。
    """

    def __init__(self, frame: SiteFrame):
        self.frame = frame

    def enu(self, latitude_deg: float, longitude_deg: float) -> Enu:
        return latlon_to_enu(self.frame, latitude_deg, longitude_deg)

    def inverse(self, east_m: float, north_m: float) -> Tuple[float, float]:
        return self.frame.origin.to_latlng(east_m, north_m)


def radar_polar_to_enu(
    frame: SiteFrame,
    *,
    station_latitude_deg: float,
    station_longitude_deg: float,
    station_height_m: float,
    range_m: float,
    bearing_true_rad: float,
) -> Enu:
    """雷达距离/真方位 → 现场 ENU。

    **语义**：TTM 的距离与方位定义在雷达站址处的**局部水平面**内
    （距离是地面距离，方位是站址处的真方位）。因此转换分两步：

      1. 站址局部平面内的相对东/北 → 站址局部 ENU
         （东 = R·sinθ，北 = R·cosθ）→ 站址局部 ENU 反算大地坐标；
      2. 该大地坐标 → 现场 ENU（与 AIS 走同一条路径）。

    为什么不直接把相对量加到站址的 ECEF 上再展开：那样做等价于在球面上
    走"弦"，而雷达测的是切平面上的弧长，展开后会产生随距离增长的系统
    偏差（12 km 处约 0.023 m），足以让径向误差略微超出报文量化界限。
    经由大地坐标中转后，雷达与 AIS 的位置定义完全一致。
    """
    station = LocalOrigin(
        latitude_deg=station_latitude_deg,
        longitude_deg=station_longitude_deg,
        height_m=station_height_m,
    )

    # 站址局部平面内的相对分量
    d_east = range_m * math.sin(bearing_true_rad)
    d_north = range_m * math.cos(bearing_true_rad)

    # 站址局部 ENU → 大地坐标
    lat, lon, height = station.to_latlng_full(d_east, d_north)

    # 大地坐标 → 现场 ENU
    return frame.origin.to_enu_full(lat, lon, height)
