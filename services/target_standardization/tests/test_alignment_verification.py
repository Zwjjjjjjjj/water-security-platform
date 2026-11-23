"""本轮补验专项测试。

覆盖在补验过程中发现并修复的缺陷，防止回归：

  1. 水平位置定义：切平面投影必须精确可逆，且与 ECEF 路径的差异落在解析范围内；
  2. 雷达极坐标转换不得引入随距离增长的系统偏差；
  3. 方位参考字段在 position 内时也必须被识别；
  4. 误差上界的噪声项必须逐轴计入；
  5. 独立坐标校验器自身的公式不得写错（mean_radius 等）；
  6. 评测必须对现场原点的选择免疫。
"""

from __future__ import annotations

import json
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment.config import AlignmentConfig  # noqa: E402
from alignment.transform import (  # noqa: E402
    FrameConverter,
    SiteFrame,
    latlon_to_enu,
    radar_polar_to_enu,
)
from alignment.verification import (  # noqa: E402
    CoordinateVerifier,
    mean_radius,
    meridian_radius,
    prime_vertical_radius,
    tangent_plane_enu,
    vincenty_inverse,
)
from geodesy import LocalOrigin  # noqa: E402

ORIGIN_LAT = 54.108200
ORIGIN_LON = 12.056800


class HorizontalDefinitionTests(unittest.TestCase):
    """水平位置的切平面定义。"""

    def test_roundtrip_is_exact(self):
        """ENU → 经纬度 → ENU 往返必须无损（纳米级）。"""
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        for east, north in [(0.0, 0.0), (400.0, 3000.0), (-2000.0, 15000.0)]:
            lat, lon = origin.to_latlng(east, north)
            back_east, back_north = origin.to_enu(lat, lon)
            self.assertAlmostEqual(back_east, east, places=6)
            self.assertAlmostEqual(back_north, north, places=6)

    def test_no_distance_growing_bias(self):
        """不得出现随距离增长的系统偏差（曾经的 0.023 m @ 12.3 km）。

        这类偏差源于"把目标压回海平面再转回"，会让径向误差略微超出
        报文量化界限。
        """
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        for distance in (1000.0, 6000.0, 12300.0, 20000.0):
            for bearing_deg in (0.0, 30.0, 90.0, 180.0, 351.0):
                bearing = math.radians(bearing_deg)
                east = distance * math.sin(bearing)
                north = distance * math.cos(bearing)
                lat, lon = origin.to_latlng(east, north)
                back_east, back_north = origin.to_enu(lat, lon)
                residual = math.hypot(back_east - east, back_north - north)
                self.assertLess(residual, 1e-6, f"{distance} m / {bearing_deg}°")

    def test_matches_independent_formula(self):
        """与独立实现的闭式子午圈/卯酉圈公式一致。"""
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        for east, north in [(400.0, 3000.0), (-2000.0, 15000.0)]:
            lat, lon = origin.to_latlng(east, north)
            got_east, got_north = origin.to_enu(lat, lon)
            expected_east, expected_north = tangent_plane_enu(
                ORIGIN_LAT, ORIGIN_LON, lat, lon
            )
            self.assertAlmostEqual(got_east, expected_east, places=6)
            self.assertAlmostEqual(got_north, expected_north, places=6)

    def test_curvature_radii_are_radii_not_curvatures(self):
        """曲率半径是长度量；平均半径是 (M+N)/2。

        曾因写成"曲率的平均"而返回 1/m 量级的数，导致所有解析预期值失真。
        """
        m = meridian_radius(ORIGIN_LAT)
        n = prime_vertical_radius(ORIGIN_LAT)
        self.assertGreater(m, 6_000_000.0)
        self.assertLess(m, 6_500_000.0)
        self.assertGreater(n, 6_000_000.0)
        self.assertLess(n, 6_500_000.0)
        self.assertAlmostEqual(mean_radius(ORIGIN_LAT), (m + n) / 2.0, places=6)


class RadarPolarTests(unittest.TestCase):
    """雷达极坐标 → ENU。"""

    def test_no_systematic_bias_vs_truth_enu(self):
        """雷达路径不得相对真值 ENU 引入系统偏差。"""
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        frame = SiteFrame("f", "WGS84", ORIGIN_LAT, ORIGIN_LON, 0.0)
        for distance in (3000.0, 12300.0, 20000.0):
            for bearing_deg in (0.0, 90.0, 351.0):
                bearing = math.radians(bearing_deg)
                east = distance * math.sin(bearing)
                north = distance * math.cos(bearing)
                lat, lon = origin.to_latlng(east, north)
                true_east, true_north = origin.to_enu(lat, lon)
                range_m = math.hypot(true_east, true_north)
                true_bearing = math.atan2(true_east, true_north)
                enu = radar_polar_to_enu(
                    frame,
                    station_latitude_deg=ORIGIN_LAT,
                    station_longitude_deg=ORIGIN_LON,
                    station_height_m=0.0,
                    range_m=range_m,
                    bearing_true_rad=true_bearing,
                )
                residual = math.hypot(
                    enu.east_m - east, enu.north_m - north
                )
                self.assertLess(residual, 1e-6, f"{distance} m / {bearing_deg}°")


class VerifierSelfTests(unittest.TestCase):
    """独立校验器自身的正确性。"""

    def test_all_checks_pass(self):
        frame = SiteFrame("f", "WGS84", ORIGIN_LAT, ORIGIN_LON, 0.0)
        verifier = CoordinateVerifier(
            origin_latitude_deg=ORIGIN_LAT,
            origin_longitude_deg=ORIGIN_LON,
            project_converter=FrameConverter(frame),
        )
        for check in verifier.run_all():
            self.assertTrue(check.passed, f"{check.name}: {check.detail}")

    def test_verifier_detects_a_broken_converter(self):
        """校验器必须能抓出被校验方的错误（否则校验毫无意义）。"""
        class BrokenConverter:
            def enu(self, latitude_deg, longitude_deg):
                from geodesy import Enu

                origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
                east, north = origin.to_enu(latitude_deg, longitude_deg)
                # 故意把北向与东向颠倒
                return Enu(east_m=north, north_m=east, up_m=0.0)

            def inverse(self, east_m, north_m):
                origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
                return origin.to_latlng(north_m, east_m)

        verifier = CoordinateVerifier(
            origin_latitude_deg=ORIGIN_LAT,
            origin_longitude_deg=ORIGIN_LON,
            project_converter=BrokenConverter(),
        )
        results = {check.name: check for check in verifier.run_all()}
        # 轴向搞反必须被抓出来
        self.assertFalse(results["axis_orientation"].passed)
        # 大地测量一致性也应当失败
        self.assertFalse(results["geodesic_consistency"].passed)

    def test_vincenty_matches_known_distance(self):
        """Vincenty 反解与解析特例一致（同纬度、已知经差）。"""
        distance, bearing = vincenty_inverse(0.0, 0.0, 0.0, 1.0)
        # 沿赤道 1° 经差
        expected = 111319.4907932736
        self.assertAlmostEqual(distance, expected, delta=0.5)
        self.assertAlmostEqual(math.degrees(bearing), 90.0, delta=0.01)


class BoundModelTests(unittest.TestCase):
    """误差上界模型。"""

    def _config(self) -> AlignmentConfig:
        return AlignmentConfig.from_dict(
            {
                "site_frame": {
                    "origin_latitude_deg": ORIGIN_LAT,
                    "origin_longitude_deg": ORIGIN_LON,
                },
                "radar_station": {
                    "station_latitude_deg": ORIGIN_LAT,
                    "station_longitude_deg": ORIGIN_LON,
                },
                "sources": [
                    {
                        "source_id": "sim_radar",
                        "source_type": "radar",
                        "encoding": {
                            "distance_lsb": 0.001,
                            "distance_unit": "nautical_mile",
                            "bearing_lsb_deg": 0.01,
                        },
                        "error_model": {"position_noise_std_m": 12.0},
                    }
                ],
            }
        )

    def test_noise_is_added_per_axis(self):
        """噪声逐轴计入 k·σ，不按 √2 分摊。

        若分摊，逐轴上限会被低估（σ/√2），把未超限的记录误判为超限。
        """
        from alignment.bounds import BoundCalculator

        calculator = BoundCalculator(self._config())
        record = {
            "source_id": "sim_radar",
            "source_type": "radar",
            "position": {"range_m": 10000.0, "bearing_rad": 0.5},
        }
        bound = calculator.bound_for_record(record)
        # 距离量化 0.0005 nm = 0.926 m；噪声 3×12 = 36 m
        self.assertAlmostEqual(bound.radial_m, 0.926 + 36.0, places=6)
        self.assertAlmostEqual(bound.tangential_m, 36.0 + 0.87, delta=0.01)

    def test_no_noise_means_no_noise_term(self):
        from alignment.bounds import BoundCalculator

        config = self._config()
        config.sources[0].error_model.position_noise_std_m = 0.0
        calculator = BoundCalculator(config)
        bound = calculator.bound_for_record(
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "position": {"range_m": 10000.0, "bearing_rad": 0.5},
            }
        )
        self.assertAlmostEqual(bound.radial_m, 0.926, places=6)


class OriginIndependenceTests(unittest.TestCase):
    """评测必须对现场原点的选择免疫。"""

    def test_truth_recomputed_under_current_origin(self):
        """真值按经纬度在当前原点下重算。"""
        from alignment.evaluation import AlignmentEvaluator

        truth_path = os.path.join(
            ROOT, "output", "scenarios", "A_geometry_baseline",
            "reference_truth.csv",
        )
        if not os.path.exists(truth_path):
            self.skipTest("场景 A 真值未生成")

        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        shifted_lat, shifted_lon = origin.to_latlng(500.0, 1000.0)

        evaluator = AlignmentEvaluator(
            truth_path,
            None,
            frame_origin=(shifted_lat, shifted_lon),
        )
        row = evaluator.truth[0]
        east, north = evaluator.truth_enu(row)
        # 新原点在旧系中位于 (500, 1000)，因此真值点在新系中的坐标应减去该偏移
        self.assertAlmostEqual(east, float(row["truth_east_m"]) - 500.0, delta=0.5)
        self.assertAlmostEqual(north, float(row["truth_north_m"]) - 1000.0, delta=0.5)

    def test_same_origin_matches_truth_file(self):
        """原点与真值一致时，重算结果应与真值文件的米制坐标一致。"""
        from alignment.evaluation import AlignmentEvaluator

        truth_path = os.path.join(
            ROOT, "output", "scenarios", "A_geometry_baseline",
            "reference_truth.csv",
        )
        if not os.path.exists(truth_path):
            self.skipTest("场景 A 真值未生成")

        evaluator = AlignmentEvaluator(
            truth_path,
            None,
            frame_origin=(ORIGIN_LAT, ORIGIN_LON),
        )
        row = evaluator.truth[0]
        east, north = evaluator.truth_enu(row)
        self.assertAlmostEqual(east, float(row["truth_east_m"]), delta=0.5)
        self.assertAlmostEqual(north, float(row["truth_north_m"]), delta=0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
