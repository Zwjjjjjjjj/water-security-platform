"""插值方式专项测试。

覆盖 `enu_first`（首轮基线）与 `observation_domain` 两种方式：
  - 匀速直线
  - 转弯段
  - 方位跨零（359° → 1°）
  - 长间隔（超过 max_interpolation_gap_s）
  - 两种方式的差异量化
"""

from __future__ import annotations

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment.config import (  # noqa: E402
    INTERP_ENU_FIRST,
    INTERP_OBSERVATION_DOMAIN,
    AlignmentConfig,
)
from alignment.spatial import SpatialAligner  # noqa: E402
from alignment.temporal import TemporalAligner, parse_iso  # noqa: E402
from geodesy import LocalOrigin  # noqa: E402

ORIGIN_LAT = 54.108200
ORIGIN_LON = 12.056800


def make_config(mode: str, max_gap: float = 10.0) -> AlignmentConfig:
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
                    "source_id": "sim_ais",
                    "source_type": "ais",
                    "time": {"time_resolution_s": 1.0},
                }
            ],
            "interpolation_mode": mode,
            "max_interpolation_gap_s": max_gap,
            "output_time_step_s": 1.0,
        }
    )


def ais_record(east: float, north: float, iso: str, row: int) -> dict:
    origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
    lat, lon = origin.to_latlng(east, north)
    return {
        "source_id": "sim_ais",
        "source_type": "ais",
        "local_track_id": "228050803",
        "source_row": row,
        "state_time_utc": iso,
        "position": {"latitude_deg": lat, "longitude_deg": lon},
        "speed_mps": 5.0,
    }


class InterpolationModeTests(unittest.TestCase):
    """插值方式的行为。"""

    def _grid_at(self, records, seconds: float):
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + seconds]
        results = temporal.interpolate_track(points, grid)
        return results[0]

    # -- 匀速直线 -----------------------------------------------------------

    def test_straight_line_interpolation_is_exact(self):
        """匀速直线：enu_first 插值应落在真线上（误差 < 1 mm）。"""
        records = [
            ais_record(0.0, 0.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(0.0, 100.0, "2019-09-30T15:00:10+00:00", 2),
        ]
        point = self._grid_at(records, 5.0)
        self.assertTrue(point.interpolation["available"])
        self.assertAlmostEqual(point.east_m, 0.0, places=3)
        self.assertAlmostEqual(point.north_m, 50.0, places=3)

    def test_straight_line_oblique(self):
        """斜向匀速直线。

        切平面投影在 (lat, lon) 上是线性的，因此 ENU 线性插值与
        经纬度线性插值在直线上完全一致——两者的差异只出现在转弯段。
        """
        records = [
            ais_record(100.0, 200.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(300.0, 600.0, "2019-09-30T15:00:10+00:00", 2),
        ]
        point = self._grid_at(records, 2.5)
        # 栅格在 10 s 跨度的 25% 处
        self.assertAlmostEqual(point.east_m, 150.0, delta=0.01)
        self.assertAlmostEqual(point.north_m, 300.0, delta=0.01)

    def test_straight_line_modes_agree(self):
        """直线上两种插值方式结果一致。"""
        records = [
            ais_record(100.0, 200.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(300.0, 600.0, "2019-09-30T15:00:10+00:00", 2),
        ]
        enu_point = self._grid_at(records, 2.5)

        config = make_config(INTERP_OBSERVATION_DOMAIN)
        aligner = SpatialAligner(config)
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 2.5]
        domain_point = temporal.interpolate_track(points, grid)[0]
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        domain_east, domain_north = origin.to_enu(
            domain_point.position["latitude_deg"],
            domain_point.position["longitude_deg"],
        )
        self.assertAlmostEqual(enu_point.east_m, domain_east, delta=0.01)
        self.assertAlmostEqual(enu_point.north_m, domain_north, delta=0.01)

    # -- 转弯 ---------------------------------------------------------------

    def test_turn_chord_error_is_bounded(self):
        """转弯段：enu_first 把弧拉成弦，误差应小于弧长且随转弯半径变化。

        这不是缺陷而是该插值方式的固有性质，测试把它的量级固定下来，
        防止无意中改变语义。
        """
        # 半径 500 m 的 90° 转弯，30° 弧段
        radius = 500.0
        center_east, center_north = 500.0, 0.0
        angle = math.radians(30.0)

        def arc_point(theta: float) -> tuple:
            return (
                center_east + radius * math.cos(theta),
                center_north + radius * math.sin(theta),
            )

        e0, n0 = arc_point(0.0)
        e1, n1 = arc_point(angle)
        records = [
            ais_record(e0, n0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(e1, n1, "2019-09-30T15:00:10+00:00", 2),
        ]
        point = self._grid_at(records, 5.0)

        mid_theta = angle / 2.0
        true_east, true_north = arc_point(mid_theta)
        chord_error = math.hypot(
            point.east_m - true_east, point.north_m - true_north
        )
        arc_length = radius * angle
        # 弦与弧的差：1 − cos(θ/2) 乘以半径 ≈ 3.4 m
        expected = radius * (1.0 - math.cos(mid_theta))
        self.assertAlmostEqual(chord_error, expected, delta=0.05)
        self.assertLess(chord_error, arc_length)

    def test_turn_error_decreases_with_shorter_interval(self):
        """采样越密，插值误差越小（这是选择插值间隔的依据）。"""
        radius = 500.0
        center_east = 500.0

        def arc_point(theta: float) -> tuple:
            return (
                center_east + radius * math.cos(theta),
                radius * math.sin(theta),
            )

        angle = math.radians(30.0)
        errors = []
        for step, count in ((10.0, 2), (2.0, 10)):
            records = []
            for index in range(count):
                theta = angle * index / (count - 1)
                east, north = arc_point(theta)
                seconds = index * step
                records.append(
                    ais_record(
                        east,
                        north,
                        f"2019-09-30T15:00:{seconds:06.3f}+00:00",
                        index + 1,
                    )
                )
            point = self._grid_at(records, step * (count - 1) / 2.0)
            true_east, true_north = arc_point(angle / 2.0)
            errors.append(
                math.hypot(
                    point.east_m - true_east, point.north_m - true_north
                )
            )
        self.assertLess(errors[1], errors[0] / 5.0)

    # -- 方位跨零 -----------------------------------------------------------

    def test_bearing_crossing_zero(self):
        """方位跨零（359° → 1°）：角度插值必须走最短路径，不能绕 358°。"""
        config = make_config(INTERP_OBSERVATION_DOMAIN)
        temporal = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {
                    "range_m": 3000.0,
                    "bearing_rad": math.radians(359.0),
                    "bearing_reference": "T",
                },
            },
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {
                    "range_m": 3000.0,
                    "bearing_rad": math.radians(1.0),
                    "bearing_reference": "T",
                },
            },
        ]
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        bearing = result.position["bearing_rad"]
        self.assertAlmostEqual(math.degrees(bearing), 0.0, delta=1e-6)
        # 绝不能绕远路
        self.assertLess(abs(math.degrees(bearing)), 1.0)

    def test_course_crossing_zero(self):
        """航向跨零同样按最短路径。"""
        config = make_config(INTERP_OBSERVATION_DOMAIN)
        temporal = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "m1",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"latitude_deg": 54.1, "longitude_deg": 12.0},
                "course_rad": math.radians(359.0),
            },
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "m1",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {"latitude_deg": 54.2, "longitude_deg": 12.0},
                "course_rad": math.radians(1.0),
            },
        ]
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertAlmostEqual(math.degrees(result.course_rad), 0.0, delta=1e-6)

    # -- 长间隔 -------------------------------------------------------------

    def test_long_gap_marked_unavailable(self):
        """间隔超过上限：该栅格点不可用，且原因明确。"""
        config = make_config(INTERP_ENU_FIRST, max_gap=5.0)
        aligner = SpatialAligner(config)
        records = [
            ais_record(0.0, 0.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(0.0, 300.0, "2019-09-30T15:00:30+00:00", 2),
        ]
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 15.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertFalse(result.interpolation["available"])
        self.assertEqual(result.interpolation["reason"], "gap_exceeds_limit")
        self.assertEqual(result.interpolation["gap_s"], 30.0)
        # 不可用点不得带有位置
        self.assertIsNone(result.east_m)
        self.assertIsNone(result.north_m)

    def test_gap_at_limit_is_allowed(self):
        """间隔正好等于上限：允许插值。"""
        config = make_config(INTERP_ENU_FIRST, max_gap=10.0)
        aligner = SpatialAligner(config)
        records = [
            ais_record(0.0, 0.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(0.0, 100.0, "2019-09-30T15:00:10+00:00", 2),
        ]
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertTrue(result.interpolation["available"])

    def test_no_extrapolation(self):
        """超出航迹范围不外推。"""
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        records = [
            ais_record(0.0, 0.0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(0.0, 100.0, "2019-09-30T15:00:10+00:00", 2),
        ]
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch - 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertFalse(result.interpolation["available"])
        self.assertEqual(result.interpolation["reason"], "outside_track_range")

    # -- 两种方式的差异 -----------------------------------------------------

    def test_two_modes_differ_on_turn(self):
        """转弯段：两种插值方式给出不同结果（说明方式确实生效）。"""
        radius = 500.0

        def arc_point(theta: float) -> tuple:
            return (
                500.0 + radius * math.cos(theta),
                radius * math.sin(theta),
            )

        angle = math.radians(30.0)
        e0, n0 = arc_point(0.0)
        e1, n1 = arc_point(angle)
        records = [
            ais_record(e0, n0, "2019-09-30T15:00:00+00:00", 1),
            ais_record(e1, n1, "2019-09-30T15:00:10+00:00", 2),
        ]

        results = {}
        for mode in (INTERP_ENU_FIRST, INTERP_OBSERVATION_DOMAIN):
            config = make_config(mode)
            aligner = SpatialAligner(config)
            for record in records:
                aligner.align_record(record)
            temporal = TemporalAligner(config, spatial_aligner=aligner)
            tracks = temporal.build_tracks(records)
            points = list(tracks.values())[0]
            grid = [points[0].state_epoch + 5.0]
            results[mode] = temporal.interpolate_track(points, grid)[0]

        # enu_first 的插值结果直接是 ENU
        self.assertIsNotNone(results[INTERP_ENU_FIRST].east_m)
        # observation_domain 保留观测域位置
        self.assertIn("latitude_deg", results[INTERP_OBSERVATION_DOMAIN].position)
        self.assertIsNone(results[INTERP_OBSERVATION_DOMAIN].east_m)

    def test_observation_domain_interpolates_latlon(self):
        """observation_domain：经纬度线性插值。"""
        config = make_config(INTERP_OBSERVATION_DOMAIN)
        temporal = TemporalAligner(config)
        origin = LocalOrigin(ORIGIN_LAT, ORIGIN_LON)
        lat0, lon0 = origin.to_latlng(0.0, 0.0)
        lat1, lon1 = origin.to_latlng(0.0, 100.0)
        records = [
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "m1",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"latitude_deg": lat0, "longitude_deg": lon0},
            },
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "m1",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {"latitude_deg": lat1, "longitude_deg": lon1},
            },
        ]
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertAlmostEqual(
            result.position["latitude_deg"], (lat0 + lat1) / 2.0, places=9
        )

    def test_source_enu_unavailable_blocks_interpolation(self):
        """源记录空间转换失败时，插值点必须标记不可用而不是静默通过。"""
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        records = [
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                # 方位参考 M 但无磁差 → 空间转换失败
                "position": {
                    "range_m": 3000.0,
                    "bearing_rad": math.radians(30.0),
                    "bearing_reference": "M",
                },
            },
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {
                    "range_m": 3000.0,
                    "bearing_rad": math.radians(31.0),
                    "bearing_reference": "M",
                },
            },
        ]
        for record in records:
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]
        self.assertFalse(result.interpolation["available"])
        self.assertEqual(
            result.interpolation["reason"], "source_enu_unavailable"
        )

    # -- 标准化层标记为不可用的记录（误检）--------------------------------

    def test_unusable_position_records_excluded_from_tracks(self):
        """标准化层标记 valid_for_spatial_alignment=False 的记录不建轨迹。

        这类记录（如光电 DLDete 的超量程误检）位置数值本身是误检产物，
        若参与插值会把极端坐标带进栅格，污染下游跨源比对与航迹关联。
        """
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        records = [
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "228050803",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON},
                "valid_for_spatial_alignment": False,
            },
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "228050803",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON},
                "valid_for_spatial_alignment": False,
            },
        ]
        for record in records:
            aligner.align_record(record)
            self.assertEqual(record["alignment_status"], "unusable_position")

        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks(records)
        self.assertEqual(tracks, {}, "误检记录不应建出轨迹")

    def test_unusable_position_single_record_drops_whole_track_point(self):
        """同航迹中仅部分记录被标记不可用时，被标记的那些点不参与插值。"""
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        good = {
            "source_id": "sim_ais",
            "source_type": "ais",
            "local_track_id": "228050803",
            "source_row": 1,
            "state_time_utc": "2019-09-30T15:00:00+00:00",
            "position": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON},
            "valid_for_spatial_alignment": True,
        }
        bad = {
            "source_id": "sim_ais",
            "source_type": "ais",
            "local_track_id": "228050803",
            "source_row": 2,
            "state_time_utc": "2019-09-30T15:00:10+00:00",
            "position": {"latitude_deg": ORIGIN_LAT, "longitude_deg": ORIGIN_LON},
            "valid_for_spatial_alignment": False,
        }
        for record in (good, bad):
            aligner.align_record(record)
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks([good, bad])
        points = list(tracks.values())[0]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].source_row, 1)

    def test_failed_conversion_still_builds_track(self):
        """空间转换【失败】（缺前提）仍建轨迹，与"数据本身不可用"区分。

        失败意味着记录是真实观测但当前缺前提（如磁差未配置），
        保留轨迹让下游能看见"该航迹存在但暂不可用"。
        """
        config = make_config(INTERP_ENU_FIRST)
        aligner = SpatialAligner(config)
        record = {
            "source_id": "sim_radar",
            "source_type": "radar",
            "local_track_id": "00",
            "source_row": 1,
            "state_time_utc": "2019-09-30T15:00:00+00:00",
            "position": {
                "range_m": 3000.0,
                "bearing_rad": math.radians(30.0),
                "bearing_reference": "M",
            },
        }
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "failed")
        temporal = TemporalAligner(config, spatial_aligner=aligner)
        tracks = temporal.build_tracks([record])
        self.assertEqual(len(tracks), 1)

    # -- 光电与雷达同路 -----------------------------------------------------

    def test_optical_uses_polar_interpolation_domain(self):
        """光电与雷达同址，observation_domain 下必须插距离/方位而非经纬度。"""
        config = make_config(INTERP_OBSERVATION_DOMAIN)
        temporal = TemporalAligner(config, spatial_aligner=SpatialAligner(config))
        records = [
            {
                "source_id": "mtdsp_optical",
                "source_type": "optical",
                "local_track_id": "413203610",
                "source_row": 1,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {
                    "range_m": 3000.0,
                    "bearing_rad": math.radians(10.0),
                    "bearing_reference": "T",
                },
            },
            {
                "source_id": "mtdsp_optical",
                "source_type": "optical",
                "local_track_id": "413203610",
                "source_row": 2,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {
                    "range_m": 2900.0,
                    "bearing_rad": math.radians(20.0),
                    "bearing_reference": "T",
                },
            },
        ]
        tracks = temporal.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        result = temporal.interpolate_track(points, grid)[0]

        self.assertIn("range_m", result.position)
        self.assertIn("bearing_rad", result.position)
        self.assertNotIn("latitude_deg", result.position)
        self.assertAlmostEqual(result.position["range_m"], 2950.0, places=6)
        self.assertAlmostEqual(
            result.position["bearing_rad"], math.radians(15.0), places=9
        )

    def test_optical_grid_row_uses_polar_to_enu(self):
        """栅格输出中光电按极坐标换算 ENU，不走经纬度分支。"""
        from alignment.config import AlignmentConfig as _AC
        from alignment.pipeline import _grid_to_enu
        from alignment.transform import SiteFrame

        config = _AC.from_dict(
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
                        "source_id": "mtdsp_optical",
                        "source_type": "optical",
                        "time": {"time_resolution_s": 1.0},
                    }
                ],
                "interpolation_mode": INTERP_OBSERVATION_DOMAIN,
                "max_interpolation_gap_s": 10.0,
                "output_time_step_s": 1.0,
            }
        )
        aligner = SpatialAligner(config)

        class _GP:
            source_id = "mtdsp_optical"
            source_type = "optical"
            local_track_id = "413203610"
            time_utc = "2019-09-30T15:00:00+00:00"
            speed_mps = None
            course_rad = None
            east_m = None
            north_m = None
            up_m = None
            position = {
                "range_m": 3000.0,
                "bearing_rad": math.radians(0.0),
                "bearing_reference": "T",
            }
            interpolation = {
                "available": True,
                "method": "linear",
                "mode": INTERP_OBSERVATION_DOMAIN,
                "gap_s": 0.0,
                "ratio": 0.0,
            }

        row = _grid_to_enu(_GP(), aligner, config)
        self.assertEqual(row["alignment_status"], "ok")
        # 正北 3000 m → 东向约 0，北向约 3000
        self.assertAlmostEqual(row["east_m"], 0.0, places=3)
        self.assertAlmostEqual(row["north_m"], 3000.0, places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
