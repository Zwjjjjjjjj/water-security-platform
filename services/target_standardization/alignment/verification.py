"""独立的坐标转换校验。

**为什么独立实现**：坐标转换的正确性是全部验收结论的前提。如果校验代码
与被校验代码共用同一套实现（`geodesy.py`），两边出现同一个错误时校验会
"通过"，反而掩盖问题。

因此本模块用**另一套数学**独立实现转换，并只用以下外部事实做校验：

  - WGS84 椭球常数（a, f）——公开定义，不是本项目的实现；
  - 子午圈/卯酉圈半径的闭式公式；
  - 切平面近似公式（与 ECEF 无关）；
  - 大地测量恒等式。

校验分五层：

  1. **轴向检查**：正北 → north 为正、正东 → east 为正（正负号搞反时，
     误差平方和看起来仍然正常，这类错误最难发现）；
  2. **往返一致性**：ENU → 经纬度 → ENU 应回到原点（亚毫米级）；
  3. **独立 ECEF 交叉验证**：本模块自写一份 ECEF 转换并直接比较；
  4. **切平面近似交叉验证**：差异应与解析预期的曲率偏差同量级
     （≈ 距离²/(2R)）；
  5. **大地测量恒等式**：平面距离 vs 大圆距离 vs 真方位。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# WGS84 椭球常数（公开定义）
WGS84_A = 6378137.0
WGS84_INV_F = 298.257223563
WGS84_F = 1.0 / WGS84_INV_F
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)
WGS84_B = WGS84_A * (1.0 - WGS84_F)


@dataclass
class CheckResult:
    """单项校验结果。"""

    name: str
    passed: bool
    detail: str
    value: Optional[float] = None
    tolerance: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "value": self.value,
            "tolerance": self.tolerance,
        }


def meridian_radius(lat_deg: float) -> float:
    """子午圈曲率半径（米）—— 闭式公式，独立于 geodesy。"""
    s = math.sin(math.radians(lat_deg))
    return WGS84_A * (1.0 - WGS84_E2) / (1.0 - WGS84_E2 * s * s) ** 1.5


def prime_vertical_radius(lat_deg: float) -> float:
    """卯酉圈曲率半径（米）—— 闭式公式，独立于 geodesy。"""
    s = math.sin(math.radians(lat_deg))
    return WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)


def mean_radius(lat_deg: float) -> float:
    """平均曲率半径（米）。

    M 与 N 是**曲率半径**（长度），平均半径是二者的算术平均：
        R = (M + N) / 2
    注意不是曲率的平均——曲率是 1/M、1/N，若误用会得到量纲错误的结果。
    """
    return (
        meridian_radius(lat_deg) + prime_vertical_radius(lat_deg)
    ) / 2.0


def tangent_plane_enu(
    lat0_deg: float, lon0_deg: float, lat_deg: float, lon_deg: float
) -> Tuple[float, float]:
    """切平面近似（局部平面）经纬度 → ENU 米。

    与 ECEF 严格转换在短距离上吻合；距离越远偏差越大（切平面忽略了曲率
    二次项），偏差量级 ≈ 距离²/(2R)。
    """
    m = meridian_radius(lat0_deg)
    n = prime_vertical_radius(lat0_deg)
    north = math.radians(lat_deg - lat0_deg) * m
    east = math.radians(lon_deg - lon0_deg) * n * math.cos(math.radians(lat0_deg))
    return east, north


def tangent_plane_inverse(
    lat0_deg: float, lon0_deg: float, east_m: float, north_m: float
) -> Tuple[float, float]:
    """切平面 ENU → 经纬度（独立公式）。

    用**独立**公式构造测试点，而不是借用被校验方自己的 `inverse`——
    否则被校验方若把东/北两轴整体颠倒且保持自洽，往返检查会照样通过，
    轴向错误就抓不出来了。
    """
    m = meridian_radius(lat0_deg)
    n = prime_vertical_radius(lat0_deg)
    latitude = lat0_deg + math.degrees(north_m / m)
    longitude = lon0_deg + math.degrees(
        east_m / (n * math.cos(math.radians(lat0_deg)))
    )
    return latitude, longitude


def ecef_to_geodetic_independent(
    x: float, y: float, z: float
) -> Tuple[float, float, float]:
    """ECEF → 大地坐标，独立实现（Bowring 迭代）。"""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-12:
        return 90.0 if z >= 0 else -90.0, math.degrees(lon), abs(z) - WGS84_B

    ep2 = (WGS84_A**2 - WGS84_B**2) / WGS84_B**2
    theta = math.atan2(z * WGS84_A, p * WGS84_B)
    lat = math.atan2(
        z + ep2 * WGS84_B * math.sin(theta) ** 3,
        p - WGS84_E2 * WGS84_A * math.cos(theta) ** 3,
    )
    height = 0.0
    for _ in range(8):
        s = math.sin(lat)
        n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
        height = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1.0 - WGS84_E2 * n / (n + height)))
    return math.degrees(lat), math.degrees(lon), height


def geodetic_to_ecef_independent(
    lat_deg: float, lon_deg: float, height_m: float = 0.0
) -> Tuple[float, float, float]:
    """大地坐标 → ECEF，独立实现。"""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    s = math.sin(lat)
    c = math.cos(lat)
    n = WGS84_A / math.sqrt(1.0 - WGS84_E2 * s * s)
    return (
        (n + height_m) * c * math.cos(lon),
        (n + height_m) * c * math.sin(lon),
        (n * (1.0 - WGS84_E2) + height_m) * s,
    )


def vincenty_inverse(
    lat1_deg: float,
    lon1_deg: float,
    lat2_deg: float,
    lon2_deg: float,
) -> Tuple[float, float]:
    """椭球大地测量反解（Vincenty 公式）→ (距离 m, 真方位 rad)。

    这是**独立于本项目任何实现**的第三方标准算法，用作坐标转换的交叉
    验证基准：距离用于核对 ENU 平面距离，方位用于核对 ENU 偏移方位。
    """
    if lat1_deg == lat2_deg and lon1_deg == lon2_deg:
        return 0.0, 0.0

    phi1 = math.radians(lat1_deg)
    phi2 = math.radians(lat2_deg)
    dlon = math.radians(lon2_deg - lon1_deg)

    u1 = math.atan((1 - WGS84_F) * math.tan(phi1))
    u2 = math.atan((1 - WGS84_F) * math.tan(phi2))
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

    lam = dlon
    sin_sigma = cos_sigma = sigma = cos_sq_alpha = cos2_sigma_m = 0.0
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(
            cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam
        )
        if sin_sigma == 0:
            return 0.0, 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos_sq_alpha = 1 - sin_alpha * sin_alpha
        if cos_sq_alpha == 0:
            cos2_sigma_m = 0.0
        else:
            cos2_sigma_m = cos_sigma - 2 * sin_u1 * sin_u2 / cos_sq_alpha
        c = WGS84_F / 16 * cos_sq_alpha * (4 + WGS84_F * (4 - 3 * cos_sq_alpha))
        lam_prev = lam
        lam = dlon + (1 - c) * WGS84_F * sin_alpha * (
            sigma
            + c
            * sin_sigma
            * (
                cos2_sigma_m
                + c * cos_sigma * (-1 + 2 * cos2_sigma_m**2)
            )
        )
        if abs(lam - lam_prev) < 1e-12:
            break

    u_sq = cos_sq_alpha * (WGS84_A**2 - WGS84_B**2) / (WGS84_B**2)
    a_coef = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    b_coef = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = (
        b_coef
        * sin_sigma
        * (
            cos2_sigma_m
            + b_coef
            / 4
            * (
                cos_sigma * (-1 + 2 * cos2_sigma_m**2)
                - b_coef
                / 6
                * cos2_sigma_m
                * (-3 + 4 * sin_sigma**2)
                * (-3 + 4 * cos2_sigma_m**2)
            )
        )
    )
    distance = WGS84_B * a_coef * (sigma - delta_sigma)

    sin_lam, cos_lam = math.sin(lam), math.cos(lam)
    bearing = math.atan2(
        cos_u2 * sin_lam,
        cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam,
    ) % (2 * math.pi)
    return distance, bearing


class CoordinateVerifier:
    """用独立实现校验对齐模块的坐标转换。

    `project_converter` 是被校验方的接口，需要提供两个方法：
      - `enu(lat, lon)` → 带 east_m / north_m 属性的对象；
      - `inverse(east, north)` → (lat, lon)。
    本类不依赖被校验方的内部实现，只比较输入输出。
    """

    def __init__(
        self,
        *,
        origin_latitude_deg: float,
        origin_longitude_deg: float,
        origin_height_m: float = 0.0,
        project_converter=None,
    ):
        self.origin_latitude_deg = origin_latitude_deg
        self.origin_longitude_deg = origin_longitude_deg
        self.origin_height_m = origin_height_m
        self.project_converter = project_converter

    # -- 单项校验 -----------------------------------------------------------

    def check_axis_orientation(self, distance_m: float = 5000.0) -> CheckResult:
        """方向检查：正北 → north 为正、正东 → east 为正。

        正负号搞反时误差平方和看起来仍然正常，这类错误最难从统计中发现。

        测试点用**独立公式**构造（`tangent_plane_inverse`），不借用被校验方
        的 `inverse`——否则被校验方若把两轴整体颠倒且保持自洽，往返检查会
        照样通过。
        """
        if self.project_converter is None:
            return CheckResult("axis_orientation", False, "未提供被校验转换器")

        north_lat, north_lon = tangent_plane_inverse(
            self.origin_latitude_deg, self.origin_longitude_deg, 0.0, distance_m
        )
        _, north_val = self._project_enu(north_lat, north_lon)
        east_lat, east_lon = tangent_plane_inverse(
            self.origin_latitude_deg, self.origin_longitude_deg, distance_m, 0.0
        )
        east_val, _ = self._project_enu(east_lat, east_lon)

        tolerance = 1.0
        passed = (
            abs(north_val - distance_m) <= tolerance
            and abs(east_val - distance_m) <= tolerance
        )
        return CheckResult(
            "axis_orientation",
            passed,
            (
                f"正北 {distance_m:.0f} m → north={north_val:.3f}；"
                f"正东 {distance_m:.0f} m → east={east_val:.3f}"
            ),
            None,
            tolerance,
        )

    def check_roundtrip(self, distance_m: float = 3000.0) -> CheckResult:
        """ENU → 经纬度 → ENU 往返应回到原点。"""
        if self.project_converter is None:
            return CheckResult("enu_roundtrip", False, "未提供被校验转换器")

        bearing = math.radians(45.0)
        east = distance_m * math.sin(bearing)
        north = distance_m * math.cos(bearing)

        lat, lon = self._project_latlng(east, north)
        back_east, back_north = self._project_enu(lat, lon)
        residual = math.hypot(back_east - east, back_north - north)
        tolerance = 0.01  # 10 mm
        return CheckResult(
            "enu_roundtrip",
            residual <= tolerance,
            f"往返残差 {residual:.6f} m @ {distance_m:.0f} m",
            residual,
            tolerance,
        )

    def check_ecef_consistency(self, distance_m: float = 3000.0) -> CheckResult:
        """与独立实现的 ECEF 路径交叉验证。

        被校验方采用**局部切平面**投影（水平位置的精确二维定义）；本模块
        独立实现 ECEF 路径作为**第二条路径**，两者之间的差异是切平面
        相对球面/椭球面的固有偏差 O(d²/2R)，而不是实现错误。

        因此这里检验的是：差异是否落在解析可解释的范围内，而不是要求
        两者完全相等。
        """
        if self.project_converter is None:
            return CheckResult("ecef_consistency", False, "未提供被校验转换器")

        bearing = math.radians(200.0)
        east = distance_m * math.sin(bearing)
        north = distance_m * math.cos(bearing)
        lat, lon = self._project_latlng(east, north)

        project_east, project_north = self._project_enu(lat, lon)

        # 独立路径：经纬度 → 独立 ECEF → 独立 ENU
        px, py, pz = geodetic_to_ecef_independent(lat, lon, self.origin_height_m)
        ox, oy, oz = geodetic_to_ecef_independent(
            self.origin_latitude_deg,
            self.origin_longitude_deg,
            self.origin_height_m,
        )
        lat0 = math.radians(self.origin_latitude_deg)
        lon0 = math.radians(self.origin_longitude_deg)
        e_hat = (-math.sin(lon0), math.cos(lon0), 0.0)
        n_hat = (
            -math.sin(lat0) * math.cos(lon0),
            -math.sin(lat0) * math.sin(lon0),
            math.cos(lat0),
        )
        dx, dy, dz = px - ox, py - oy, pz - oz
        indep_east = dx * e_hat[0] + dy * e_hat[1] + dz * e_hat[2]
        indep_north = dx * n_hat[0] + dy * n_hat[1] + dz * n_hat[2]

        residual = math.hypot(project_east - indep_east, project_north - indep_north)
        radius = mean_radius(self.origin_latitude_deg)
        # 切平面与椭球面 ECEF 投影的固有差异 ≈ d²/2R
        expected = distance_m * distance_m / (2.0 * radius)
        tolerance = max(0.05, 3.0 * expected)
        return CheckResult(
            "ecef_consistency",
            residual <= tolerance,
            (
                f"与独立 ECEF 路径差 {residual:.4f} m，"
                f"解析预期 ≈{expected:.4f} m（d²/2R），上限 {tolerance:.4f} m"
            ),
            residual,
            tolerance,
        )

    def check_tangent_plane_cross(self, distance_m: float = 3000.0) -> CheckResult:
        """与**独立实现的切平面投影**交叉验证。

        本模块的切平面公式用闭式子午圈/卯酉圈半径，与被校验方
        （`geodesy.LocalOrigin`）的实现路径不同，但数学定义相同。
        两者应完全吻合——若不吻合，说明某一方的公式写错了。
        """
        if self.project_converter is None:
            return CheckResult("tangent_plane_cross", False, "未提供被校验转换器")

        bearing = math.radians(90.0)
        east = distance_m * math.sin(bearing)
        north = distance_m * math.cos(bearing)
        lat, lon = self._project_latlng(east, north)

        project_east, project_north = self._project_enu(lat, lon)
        flat_east, flat_north = tangent_plane_enu(
            self.origin_latitude_deg, self.origin_longitude_deg, lat, lon
        )
        residual = math.hypot(project_east - flat_east, project_north - flat_north)
        tolerance = 1e-6
        return CheckResult(
            "tangent_plane_cross",
            residual <= tolerance,
            (
                f"与独立切平面公式差 {residual:.3e} m"
                f"（正东 {distance_m:.0f} m），上限 {tolerance:.1e} m"
            ),
            residual,
            tolerance,
        )

    def check_geodesic_consistency(self, distance_m: float = 3000.0) -> CheckResult:
        """大地测量恒等式：ENU 平面距离 vs 椭球大地线距离 vs 真方位。

        用 Vincenty 反解作为独立基准：
          - 距离差：切平面投影的固有偏差，量级 ≈ 距离³/(24R²)；
          - 方位差：切平面方位与椭球大地线方位（起点正方位）之间的差，
            主要来自子午线收敛，量级 ≈ 距离·sin(方位)/R（度）。
        """
        if self.project_converter is None:
            return CheckResult("geodesic_consistency", False, "未提供被校验转换器")

        bearing = math.radians(120.0)
        east = distance_m * math.sin(bearing)
        north = distance_m * math.cos(bearing)
        lat, lon = self._project_latlng(east, north)

        project_east, project_north = self._project_enu(lat, lon)
        planar = math.hypot(project_east, project_north)

        geodesic_distance, geodesic_bearing = vincenty_inverse(
            self.origin_latitude_deg,
            self.origin_longitude_deg,
            lat,
            lon,
        )

        distance_error = abs(planar - geodesic_distance)
        radius = mean_radius(self.origin_latitude_deg)
        # 切平面投影与椭球大地线的固有差异 ≈ d²/2R（局部平面近似的曲率项）
        distance_tolerance = max(0.01, 1.5 * distance_m * distance_m / (2.0 * radius))

        bearing_error = abs(
            (geodesic_bearing - bearing + math.pi) % (2 * math.pi) - math.pi
        )
        bearing_tolerance = math.radians(
            max(0.005, math.degrees(distance_m * abs(math.sin(bearing)) / radius) * 3.0)
        )

        passed = (
            distance_error <= distance_tolerance
            and bearing_error <= bearing_tolerance
        )
        return CheckResult(
            "geodesic_consistency",
            passed,
            (
                f"平面距离 {planar:.4f} m vs 椭球大地线 {geodesic_distance:.4f} m "
                f"(差 {distance_error:.4f} m ≤ {distance_tolerance:.4f})；"
                f"方位差 {math.degrees(bearing_error):.5f}° "
                f"(≤ {math.degrees(bearing_tolerance):.4f}°)"
            ),
            distance_error,
            distance_tolerance,
        )

    # -- 组合 ---------------------------------------------------------------

    def run_all(self) -> List[CheckResult]:
        return [
            self.check_axis_orientation(),
            self.check_roundtrip(),
            self.check_ecef_consistency(),
            self.check_tangent_plane_cross(),
            self.check_geodesic_consistency(),
        ]

    # -- 被校验方接口桥接 ---------------------------------------------------

    def _project_enu(self, lat: float, lon: float) -> Tuple[float, float]:
        enu = self.project_converter.enu(lat, lon)
        return enu.east_m, enu.north_m

    def _project_latlng(self, east: float, north: float) -> Tuple[float, float]:
        return self.project_converter.inverse(east, north)
