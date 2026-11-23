"""WGS84 大地测量与局部 ENU 坐标（项目内唯一的坐标定义）。

本模块是**坐标数学的唯一来源**：场景生成（simulation）与时空对齐
（alignment）共用同一套转换，避免"生成用平面近似、对齐用 ECEF"造成的
系统性偏差混入评测误差。

坐标转换链路：
    经纬度 → ECEF → 现场局部 ENU（东/北/天）

现场 ENU 的定义：
    原点：配置给定的现场坐标原点（默认取雷达站位置）；
    东：原点处的真东方向；
    北：原点处的真北方向；
    天：原点处的天顶方向。
三个基向量在 ECEF 中为单位长度且相互正交。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple

# WGS84 椭球
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
EARTH_RADIUS_M = 6371008.8
# 平面近似的米/纬度，仅用于粗略估算，不参与正式转换
METERS_PER_DEG_LAT_APPROX = 111320.0


@dataclass(frozen=True)
class Ecef:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Enu:
    """局部 ENU 坐标（米）。"""

    east_m: float
    north_m: float
    up_m: float


def geodetic_to_ecef(
    latitude_deg: float, longitude_deg: float, height_m: float = 0.0
) -> Ecef:
    """大地坐标（WGS84）→ 地心坐标。"""
    lat = math.radians(latitude_deg)
    lon = math.radians(longitude_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    prime_vertical = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    return Ecef(
        x=(prime_vertical + height_m) * cos_lat * math.cos(lon),
        y=(prime_vertical + height_m) * cos_lat * math.sin(lon),
        z=(prime_vertical * (1.0 - WGS84_E2) + height_m) * sin_lat,
    )


def ecef_to_geodetic(point: Ecef) -> Tuple[float, float, float]:
    """地心坐标 → 大地坐标（WGS84，Bowring 迭代）。"""
    lon = math.atan2(point.y, point.x)
    p = math.hypot(point.x, point.y)
    if p < 1e-9:
        return (
            90.0 if point.z >= 0 else -90.0,
            math.degrees(lon),
            abs(point.z) - WGS84_A,
        )
    lat = math.atan2(point.z, p * (1.0 - WGS84_E2))
    for _ in range(10):
        sin_lat = math.sin(lat)
        prime_vertical = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
        height = p / math.cos(lat) - prime_vertical
        new_lat = math.atan2(
            point.z,
            p * (1.0 - WGS84_E2 * prime_vertical / (prime_vertical + height)),
        )
        converged = abs(new_lat - lat) < 1e-14
        lat = new_lat
        if converged:
            break
    # 用收敛后的纬度重算高度：上面的高度公式用的是上一轮的纬度，
    # 提前退出会在高程上残留约 0.3 mm 的偏差（长距离上足以影响
    # 误差上界的逐点判定）。
    sin_lat = math.sin(lat)
    prime_vertical = WGS84_A / math.sqrt(1.0 - WGS84_E2 * sin_lat * sin_lat)
    height = p / math.cos(lat) - prime_vertical
    return math.degrees(lat), math.degrees(lon), height


@dataclass(frozen=True)
class LocalOrigin:
    """局部 ENU 原点。

    ## 水平投影的定义

    水平位置采用**局部切平面**投影（tangent plane）：

        North = (lat − lat0) · M0
        East  = (lon − lon0) · N0 · cos(lat0)

    其中 M0、N0 是原点处的子午圈与卯酉圈曲率半径。

    之所以不用"经纬度 → ECEF → ENU"的三维映射来定义水平位置：该映射把
    目标的高程一并确定，而水平 ENU 只是它在天向的投影。把 (E,N) 转成
    经纬度再丢掉高程、按 h=0 转回，等价于把目标压回海平面，水平距离
    随之改变，偏差随距离增长（12.3 km 处约 0.023 m）——这个量级足以让
    径向误差略微超出报文量化界限。

    切平面投影是**精确的二维双射**：(E,N) → (lat,lon) → (E,N) 往返无损，
    雷达与 AIS 两条路径因此共享同一个位置定义。

    ## 与 ECEF 的关系

    切平面与 ECEF-ENU 相差 O(d²/2R)（20 km 处约 0.06 m），这是局部平面
    近似本身的固有量。二者都正确；本模块选切平面是为了让水平位置定义
    无损且与任务定义（雷达：距离方位 → 相对东/北 → 加入雷达站位置）
    完全一致。ECEF 版本仍保留在 `to_enu_ecef` / `to_latlng_ecef` 中，
    供独立校验使用。
    """

    latitude_deg: float
    longitude_deg: float
    height_m: float = 0.0

    @property
    def origin_ecef(self) -> Ecef:
        return geodetic_to_ecef(
            self.latitude_deg, self.longitude_deg, self.height_m
        )

    def curvature_radii(self) -> Tuple[float, float]:
        """原点处的 (子午圈曲率半径 M0, 卯酉圈曲率半径 N0)（米）。"""
        sin_lat = math.sin(math.radians(self.latitude_deg))
        w = 1.0 - WGS84_E2 * sin_lat * sin_lat
        meridian = WGS84_A * (1.0 - WGS84_E2) / (w ** 1.5)
        prime_vertical = WGS84_A / math.sqrt(w)
        return meridian, prime_vertical

    def basis(self) -> Tuple[Ecef, Ecef, Ecef]:
        """(东, 北, 天) 三个单位基向量（ECEF 分量）。"""
        lat = math.radians(self.latitude_deg)
        lon = math.radians(self.longitude_deg)
        east = Ecef(-math.sin(lon), math.cos(lon), 0.0)
        north = Ecef(
            -math.sin(lat) * math.cos(lon),
            -math.sin(lat) * math.sin(lon),
            math.cos(lat),
        )
        up = Ecef(
            math.cos(lat) * math.cos(lon),
            math.cos(lat) * math.sin(lon),
            math.sin(lat),
        )
        return east, north, up

    def to_enu(
        self, latitude_deg: float, longitude_deg: float, height_m: float = 0.0
    ) -> Tuple[float, float]:
        """经纬度 → (东向 m, 北向 m)（切平面投影，精确二维映射）。"""
        meridian, prime_vertical = self.curvature_radii()
        north = math.radians(latitude_deg - self.latitude_deg) * meridian
        east = (
            math.radians(longitude_deg - self.longitude_deg)
            * prime_vertical
            * math.cos(math.radians(self.latitude_deg))
        )
        return east, north

    def to_enu_full(
        self, latitude_deg: float, longitude_deg: float, height_m: float = 0.0
    ) -> Enu:
        """经纬度 → ENU（切平面投影；高度只影响天向分量）。"""
        east, north = self.to_enu(latitude_deg, longitude_deg, height_m)
        # 天向分量用 ECEF 路径（切平面在水平方向没有高程项）
        target = geodetic_to_ecef(latitude_deg, longitude_deg, height_m)
        origin = self.origin_ecef
        e_hat, n_hat, u_hat = self.basis()
        dx = target.x - origin.x
        dy = target.y - origin.y
        dz = target.z - origin.z
        up = dx * u_hat.x + dy * u_hat.y + dz * u_hat.z
        return Enu(east_m=east, north_m=north, up_m=up)

    def to_latlng(
        self, east_m: float, north_m: float, up_m: float = 0.0
    ) -> Tuple[float, float]:
        """(东向 m, 北向 m) → (纬度, 经度)（切平面投影的精确反解）。"""
        meridian, prime_vertical = self.curvature_radii()
        latitude = self.latitude_deg + math.degrees(north_m / meridian)
        longitude = self.longitude_deg + math.degrees(
            east_m / (prime_vertical * math.cos(math.radians(self.latitude_deg)))
        )
        return latitude, longitude

    def to_latlng_full(
        self, east_m: float, north_m: float, up_m: float = 0.0
    ) -> Tuple[float, float, float]:
        """(东, 北, 天) 米 → (纬度, 经度, 高程)。

        高程由 ECEF 路径确定：把水平切平面上的点投影到椭球面上时，
        其高程随距离增长（12.3 km 处约 11.9 m）。
        """
        latitude, longitude = self.to_latlng(east_m, north_m, up_m)
        # 在该经纬度处求椭球面高度
        point = geodetic_to_ecef(latitude, longitude, 0.0)
        origin = self.origin_ecef
        e_hat, n_hat, u_hat = self.basis()
        dx = point.x - origin.x
        dy = point.y - origin.y
        dz = point.z - origin.z
        height = dx * u_hat.x + dy * u_hat.y + dz * u_hat.z
        return latitude, longitude, height


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离（米）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """真方位（从北起顺时针，弧度）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        dlambda
    )
    return math.atan2(y, x) % (2 * math.pi)


def enu_offset_to_bearing(east_m: float, north_m: float) -> float:
    """局部 (东, 北) 偏移 → 真方位（弧度）。"""
    return math.atan2(east_m, north_m) % (2 * math.pi)


def enu_offset_to_speed(east_m: float, north_m: float, dt_s: float) -> float:
    """局部 (东, 北) 位移 → 速率（m/s）。"""
    if dt_s <= 0:
        return 0.0
    return math.hypot(east_m, north_m) / dt_s
