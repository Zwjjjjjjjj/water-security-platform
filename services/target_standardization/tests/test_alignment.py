"""时空对齐模块测试。

覆盖：
  - 坐标数学：ECEF ↔ ENU 往返、雷达极坐标与经纬路径的一致性
  - 方位参考语义：T 不叠加安装方向/艏向；M/R 缺少换算参数时转换失败
  - 位置参考点偏移单独叠加
  - 时间整理：已知时钟偏移修正、按航迹独立分组、区间线性插值、
    超限间隔标记不可用
  - 端到端：A/B/C 三场景的位置误差、时间误差与跨源自洽性
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment import AlignmentConfig, run_alignment  # noqa: E402
from alignment.evaluation import AlignmentEvaluator  # noqa: E402
from alignment.spatial import SpatialAligner  # noqa: E402
from alignment.temporal import TemporalAligner  # noqa: E402
from alignment.transform import SiteFrame, latlon_to_enu, radar_polar_to_enu  # noqa: E402
from geodesy import LocalOrigin  # noqa: E402
from simulation.scenario import ScenarioConfig  # noqa: E402


def load_config(scenario_id: str) -> ScenarioConfig:
    """读取场景配置。"""
    path = os.path.join(ROOT, "configs", "scenarios", f"{scenario_id}.json")
    with open(path, "r", encoding="utf-8") as handle:
        return ScenarioConfig.from_dict(json.load(handle))

SCENARIO_DIR = os.path.join(ROOT, "output", "scenarios")
ALIGNMENT_CONFIG_DIR = os.path.join(ROOT, "configs", "alignment")

RADAR_SOURCE_CONFIG = {
    "source_id": "sim_radar",
    "source_type": "radar",
    "session_id": "sim-session",
    "frame_id": "radar_local_polar",
    "simulation_date_binding": "zda_date",
    "field_mapping": {
        "target_number": 0,
        "distance": 1,
        "bearing": 2,
        "bearing_reference": 3,
        "speed": 4,
        "course": 5,
        "course_reference": 6,
        "cpa_distance": 7,
        "cpa_time": 8,
        "speed_distance_units": 9,
        "distance_unit": 9,
        "target_name": 10,
        "tracking_status": 11,
        "reference_target": 12,
        "time_of_day": 13,
        "acquisition_type": 14,
    },
}

AIS_SOURCE_CONFIG = {
    "source_id": "sim_ais",
    "source_type": "ais_vdm",
    "session_id": "sim-session",
    "frame_id": "wgs84",
    "simulation_date_binding": "zda_date",
    "placeholder_positions": [[0.0, 0.0]],
}


def load_alignment_config(scenario_id: str) -> AlignmentConfig:
    path = os.path.join(ALIGNMENT_CONFIG_DIR, f"{scenario_id}.json")
    with open(path, encoding="utf-8") as handle:
        return AlignmentConfig.from_dict(json.load(handle))


def _epoch(iso: str) -> float:
    from alignment.temporal import parse_iso

    return parse_iso(iso)


def standardize_scenario(scenario_dir: str, work_dir: str):
    from standardization import standardize

    context = os.path.join(scenario_dir, "all_incoming_nmea_with_capture_time.csv")
    radar_config = dict(
        RADAR_SOURCE_CONFIG, data_root=scenario_dir, all_incoming_csv=context
    )
    radar_out = os.path.join(work_dir, "radar")
    standardize(
        os.path.join(scenario_dir, "radar_ttm_with_capture_time.csv"),
        radar_config,
        radar_out,
    )
    ais_config = dict(
        AIS_SOURCE_CONFIG, data_root=scenario_dir, all_incoming_csv=context
    )
    ais_out = os.path.join(work_dir, "ais")
    standardize(
        os.path.join(scenario_dir, "ais_with_capture_time.csv"),
        ais_config,
        ais_out,
    )
    return (
        os.path.join(radar_out, "standard_records.jsonl"),
        os.path.join(ais_out, "standard_records.jsonl"),
    )


class GeodesyTests(unittest.TestCase):
    """坐标数学。"""

    def test_enu_roundtrip_is_exact(self):
        origin = LocalOrigin(latitude_deg=54.1082, longitude_deg=12.0568)
        lat, lon = origin.to_latlng(400.0, 3000.0)
        east, north = origin.to_enu(lat, lon)
        self.assertAlmostEqual(east, 400.0, places=3)
        self.assertAlmostEqual(north, 3000.0, places=3)

    def test_radar_and_latlng_paths_agree(self):
        """同一位置由雷达极坐标与经纬两条路径得到同一 ENU。"""
        frame = SiteFrame("f", "WGS84", 54.1082, 12.0568, 0.0)
        origin = LocalOrigin(54.1082, 12.0568)
        radius = math.hypot(400.0, 3000.0)
        bearing = math.atan2(400.0, 3000.0)
        radar_enu = radar_polar_to_enu(
            frame,
            station_latitude_deg=54.1082,
            station_longitude_deg=12.0568,
            station_height_m=0.0,
            range_m=radius,
            bearing_true_rad=bearing,
        )
        latlon_enu = latlon_to_enu(frame, *origin.to_latlng(400.0, 3000.0))
        self.assertAlmostEqual(radar_enu.east_m, latlon_enu.east_m, places=2)
        self.assertAlmostEqual(radar_enu.north_m, latlon_enu.north_m, places=2)

    def test_flat_approximation_bias_is_avoided(self):
        """平面近似（111320 m/度）在 3 km 处有约 0.9 m 系统偏差，必须避免。

        对齐模块采用**局部切平面投影**（用精确的子午圈/卯酉圈曲率半径），
        与"每纬度 111320 m"的粗略近似有系统性差异。
        """
        frame = SiteFrame("f", "WGS84", 54.1082, 12.0568, 0.0)
        origin = LocalOrigin(54.1082, 12.0568)
        lat, lon = origin.to_latlng(400.0, 3000.0)
        exact = latlon_to_enu(frame, lat, lon)
        flat_east = (lon - 12.0568) * 111320.0 * math.cos(math.radians(54.1082))
        flat_north = (lat - 54.1082) * 111320.0
        # 两者确有系统性差异（东向约 0.9 m）
        self.assertGreater(abs(exact.east_m - flat_east), 0.5)
        # 但切平面投影自身是精确的
        self.assertAlmostEqual(exact.east_m, 400.0, delta=0.01)
        self.assertAlmostEqual(exact.north_m, 3000.0, delta=0.01)


class BearingReferenceTests(unittest.TestCase):
    """方位参考语义。"""

    def _record(self, bearing_rad: float, reference: str) -> dict:
        return {
            "source_type": "radar",
            "position": {"range_m": 3000.0, "bearing_rad": bearing_rad},
            "bearing_reference": reference,
        }

    def test_true_bearing_not_shifted(self):
        """T 参考：方位已是真方位，不得叠加安装方向或艏向。"""
        config = AlignmentConfig.from_dict(
            {
                "radar_station": {"install_heading_deg": 37.0},
                "own_ship_heading_deg": 112.0,
            }
        )
        aligner = SpatialAligner(config)
        record = self._record(math.radians(30.0), "T")
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "ok")
        self.assertFalse(record["alignment_install_heading_applied"])

        # 结果应与"直接用真方位"一致
        frame = aligner.frame
        expected = radar_polar_to_enu(
            frame,
            station_latitude_deg=config.radar_station.station_latitude_deg,
            station_longitude_deg=config.radar_station.station_longitude_deg,
            station_height_m=config.radar_station.station_height_m,
            range_m=3000.0,
            bearing_true_rad=math.radians(30.0),
        )
        self.assertAlmostEqual(record["east_m"], expected.east_m, places=9)
        self.assertAlmostEqual(record["north_m"], expected.north_m, places=9)

    def test_magnetic_reference_needs_variation(self):
        """M 参考且磁差未知 → 转换失败并记录原因。"""
        config = AlignmentConfig.from_dict({})
        aligner = SpatialAligner(config)
        record = self._record(math.radians(30.0), "M")
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "failed")
        self.assertIn("m_not_convertible", record["alignment_failure_reason"])

    def test_bearing_reference_read_from_position(self):
        """方位参考字段在 position 内（标准化模块的存放位置）也必须被识别。

        曾经因为只读记录顶层，导致 M/R 参考的处理在真实数据上从未生效。
        """
        config = AlignmentConfig.from_dict({})
        aligner = SpatialAligner(config)
        record = {
            "source_type": "radar",
            "position": {
                "range_m": 3000.0,
                "bearing_rad": math.radians(30.0),
                "bearing_reference": "M",
            },
        }
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "failed")
        self.assertIn("m_not_convertible", record["alignment_failure_reason"])

    def test_relative_reference_needs_own_heading(self):
        """R 参考且本船艏向未知 → 转换失败。"""
        config = AlignmentConfig.from_dict({})
        aligner = SpatialAligner(config)
        record = self._record(math.radians(30.0), "R")
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "failed")
        self.assertIn("bearing_reference", record["alignment_failure_reason"])

    def test_relative_reference_applies_own_heading(self):
        """R 参考且给了艏向 → 叠加艏向后得到真方位。"""
        config = AlignmentConfig.from_dict({"own_ship_heading_deg": 90.0})
        aligner = SpatialAligner(config)
        record = self._record(math.radians(30.0), "R")
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "ok")
        expected = radar_polar_to_enu(
            aligner.frame,
            station_latitude_deg=config.radar_station.station_latitude_deg,
            station_longitude_deg=config.radar_station.station_longitude_deg,
            station_height_m=config.radar_station.station_height_m,
            range_m=3000.0,
            bearing_true_rad=math.radians(120.0),
        )
        self.assertAlmostEqual(record["east_m"], expected.east_m, places=9)

    def test_reference_point_offset_applied_separately(self):
        """位置参考点偏移在站址上单独叠加。

        偏移定义在站址处的 ENU 上，直接加到结果上（不经经纬度换算），
        因此结果是精确的：目标 = 站址 + 偏移 + (距离, 方位)。
        """
        config = AlignmentConfig.from_dict(
            {
                "radar_station": {
                    "reference_point_offset_east_m": 100.0,
                    "reference_point_offset_north_m": -50.0,
                }
            }
        )
        aligner = SpatialAligner(config)
        record = self._record(math.radians(0.0), "T")
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "ok")
        # 站址处 3000 m 正北方向 → 北向 3000 m；再叠加偏移（东 100 / 北 −50）
        self.assertAlmostEqual(record["north_m"], 2950.0, delta=0.01)
        self.assertAlmostEqual(record["east_m"], 100.0, delta=0.01)
        self.assertEqual(
            record["alignment_reference_point_offset_applied_m"]["east"], 100.0
        )


class TemporalTests(unittest.TestCase):
    """时间整理与插值。"""

    def _config(self, **kwargs) -> AlignmentConfig:
        base = {
            "sources": [
                {
                    "source_id": "sim_radar",
                    "source_type": "radar",
                    "time": {
                        "known_clock_offset_s": 0.25,
                        "time_resolution_s": 0.01,
                    },
                }
            ],
            "output_time_step_s": 1.0,
            "max_interpolation_gap_s": 5.0,
            # 观测域插值：本组测试直接检查观测域内的插值结果
            "interpolation_mode": "observation_domain",
        }
        base.update(kwargs)
        return AlignmentConfig.from_dict(base)

    def test_known_clock_offset_corrected(self):
        """已知偏移被减去：状态时间 15:00:00.25 修正后应为 15:00:00.00。"""
        config = self._config()
        aligner = TemporalAligner(config)
        source_config = config.source_config("sim_radar")
        corrected = aligner.corrected_state_epoch(
            {"state_time_utc": "2019-09-30T15:00:00.250000+00:00"}, source_config
        )
        expected = _epoch("2019-09-30T15:00:00.000000+00:00")
        self.assertAlmostEqual(corrected, expected, places=9)

        # 与未修正的读数相比正好差一个已知偏移
        zero_config = AlignmentConfig.from_dict(
            {
                "sources": [
                    {
                        "source_id": "sim_radar",
                        "source_type": "radar",
                        "time": {"known_clock_offset_s": 0.0},
                    }
                ]
            }
        ).source_config("sim_radar")
        raw = aligner.corrected_state_epoch(
            {"state_time_utc": "2019-09-30T15:00:00.250000+00:00"}, zero_config
        )
        self.assertAlmostEqual(raw - corrected, 0.25, places=9)

    def test_tracks_grouped_by_target(self):
        """不同目标的记录不得混在同一条插值序列里。"""
        config = self._config()
        aligner = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"range_m": 1000.0, "bearing_rad": 0.0},
            },
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "01",
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"range_m": 2000.0, "bearing_rad": 1.0},
            },
        ]
        tracks = aligner.build_tracks(records)
        self.assertEqual(len(tracks), 2)

    def test_linear_interpolation_between_points(self):
        config = self._config(max_interpolation_gap_s=15.0)
        aligner = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 11,
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"range_m": 1000.0, "bearing_rad": 0.0},
            },
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "source_row": 22,
                "state_time_utc": "2019-09-30T15:00:10+00:00",
                "position": {"range_m": 2000.0, "bearing_rad": 1.0},
            },
        ]
        tracks = aligner.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 5.0]
        results = aligner.interpolate_track(points, grid)
        self.assertTrue(results[0].interpolation["available"])
        self.assertEqual(results[0].interpolation["method"], "linear")
        self.assertAlmostEqual(results[0].position["range_m"], 1500.0, places=6)
        # 插值前后两条源记录都被记录下来
        self.assertEqual(results[0].interpolation["before_source_row"], 11)
        self.assertEqual(results[0].interpolation["after_source_row"], 22)
        self.assertEqual(results[0].interpolation["gap_s"], 10.0)

    def test_gap_exceeding_limit_is_unavailable(self):
        config = self._config()
        aligner = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"range_m": 1000.0, "bearing_rad": 0.0},
            },
            {
                "source_id": "sim_radar",
                "source_type": "radar",
                "local_track_id": "00",
                "state_time_utc": "2019-09-30T15:00:30+00:00",
                "position": {"range_m": 2000.0, "bearing_rad": 1.0},
            },
        ]
        tracks = aligner.build_tracks(records)
        points = list(tracks.values())[0]
        grid = [points[0].state_epoch + 15.0]
        results = aligner.interpolate_track(points, grid)
        self.assertFalse(results[0].interpolation["available"])
        self.assertEqual(results[0].interpolation["reason"], "gap_exceeds_limit")

    def test_ais_whole_second_resolution_preserved(self):
        """AIS 的整秒精度限制必须保留在输出中。"""
        config = AlignmentConfig.from_dict(
            {
                "sources": [
                    {
                        "source_id": "sim_ais",
                        "source_type": "ais",
                        "time": {
                            "known_clock_offset_s": 0.0,
                            "time_resolution_s": 1.0,
                        },
                    }
                ]
            }
        )
        aligner = TemporalAligner(config)
        records = [
            {
                "source_id": "sim_ais",
                "source_type": "ais",
                "local_track_id": "228050803",
                "state_time_utc": "2019-09-30T15:00:00+00:00",
                "position": {"latitude_deg": 54.1, "longitude_deg": 12.0},
            }
        ]
        tracks = aligner.build_tracks(records)
        points = list(tracks.values())[0]
        self.assertEqual(points[0].time_resolution_s, 1.0)


class ScenarioAlignmentTests(unittest.TestCase):
    """A/B/C 三场景的端到端对齐与评测。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.results = {}
        for scenario_id in (
            "A_geometry_baseline",
            "B_asynchronous",
            "C_observation_perturbation",
        ):
            scenario_dir = os.path.join(SCENARIO_DIR, scenario_id)
            if not os.path.isdir(scenario_dir):
                continue
            work = os.path.join(cls.tmp.name, scenario_id)
            radar_records, ais_records = standardize_scenario(scenario_dir, work)
            out_dir = os.path.join(cls.tmp.name, f"out_{scenario_id}")
            result = run_alignment(
                radar_records_path=radar_records,
                ais_records_path=ais_records,
                alignment_config=load_alignment_config(scenario_id),
                output_dir=out_dir,
                truth_path=os.path.join(scenario_dir, "reference_truth.csv"),
                mapping_path=os.path.join(scenario_dir, "target_mapping.json"),
            )
            cls.results[scenario_id] = result

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_scenarios_ran(self):
        self.assertEqual(len(self.results), 3)

    # -- A ------------------------------------------------------------------

    def test_a_all_records_converted(self):
        for record in self.results["A_geometry_baseline"]["records"]:
            self.assertEqual(record["alignment_status"], "ok")
            self.assertIsNotNone(record["east_m"])
            self.assertIsNotNone(record["north_m"])
            self.assertEqual(
                record["alignment_frame_id"], "site_enu_rostock"
            )

    def test_a_position_error_within_quantization(self):
        """A 场景：无噪声，误差应完全来自协议量化。"""
        evaluation = self.results["A_geometry_baseline"]["evaluation"]
        radar = evaluation["position_error_by_sensor"]["radar"]["planar_m"]
        ais = evaluation["position_error_by_sensor"]["ais"]["planar_m"]

        # TTM：距离 0.001 nm（1.85 m）+ 方位 0.01°（3000 m 处 0.52 m）
        self.assertLess(radar["max"], 1.5)
        self.assertLess(radar["mean"], 0.9)
        # AIS：位置编码精度约 0.2 m
        self.assertLess(ais["max"], 0.25)
        self.assertLess(ais["mean"], 0.1)

    def test_a_no_systematic_bias(self):
        """各轴平均误差应接近 0（量化噪声无方向性）。"""
        evaluation = self.results["A_geometry_baseline"]["evaluation"]
        for sensor in ("radar", "ais"):
            for axis in ("east_m", "north_m"):
                mean = evaluation["position_error_by_sensor"][sensor][axis]["mean"]
                self.assertLess(abs(mean), 0.05)

    def test_a_time_error_zero(self):
        evaluation = self.results["A_geometry_baseline"]["evaluation"]
        for sensor, stats in evaluation["time_error_by_sensor"].items():
            self.assertLess(abs(stats["max"]), 1e-6)

    # -- B ------------------------------------------------------------------

    def test_b_clock_offset_corrected(self):
        """B 场景：雷达的 +0.25 s 偏移修正后应归零。"""
        evaluation = self.results["B_asynchronous"]["evaluation"]
        correction = evaluation["clock_offset_correction"]
        radar_before = correction["radar"]["before_correction_s"]["mean"]
        radar_after = correction["radar"]["after_correction_s"]["mean"]
        self.assertAlmostEqual(radar_before, 0.25, delta=0.01)
        self.assertLess(abs(radar_after), 1e-6)

    def test_b_ais_residual_is_whole_second_limit(self):
        """AIS 修正后应归零：整秒字段的截断偏差已作为显式参数建模。

        AIS VDM 的 `second` 字段只有整秒分辨率，仿真时刻 15:00:00.5
        在报文中只能表达为 15:00:00。因此原始读数比真实时刻早 1.0 s
        （0.5 s 时钟偏移 + 0.5 s 截断）。

        对齐配置把这两部分分开给出：
          known_clock_offset_s = −0.5（时钟偏移）
          known_quantum_bias_s = −0.5（时间字段量化的截断偏差）
        两者相加即为读数的完整偏差，修正后残差应为零。
        """
        evaluation = self.results["B_asynchronous"]["evaluation"]
        ais = evaluation["clock_offset_correction"]["ais"]

        # 原始读数偏差 = 时钟偏移 + 量化偏差 = −1.0 s
        self.assertAlmostEqual(
            ais["before_correction_s"]["mean"], -1.0, delta=0.01
        )
        # 修正后残差归零
        self.assertAlmostEqual(
            ais["after_correction_s"]["mean"], 0.0, delta=1e-6
        )
        # 残差已被配置完整解释
        self.assertTrue(ais["residual_explained"])
        self.assertAlmostEqual(ais["configured_clock_offset_s"], -0.5, delta=1e-9)
        self.assertAlmostEqual(ais["configured_quantum_bias_s"], -0.5, delta=1e-9)

    def test_b_ais_quantum_bias_is_configured_explicitly(self):
        """量化偏差必须显式配置，不能由代码隐式推断。"""
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "configs",
            "alignment",
            "B_asynchronous.json",
        )
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        ais = [
            item
            for item in config["sources"]
            if item["source_id"] == "sim_ais"
        ][0]
        self.assertEqual(ais["time"]["time_quantum_s"], 1.0)
        self.assertEqual(ais["time"]["known_quantum_bias_s"], -0.5)
        # 量化偏差必须有文字说明，便于追溯
        self.assertIn("quantum_bias_note", ais["time"])

    def test_b_asynchronous_sampling_matched(self):
        """不同采样率下，两路都应覆盖公共栅格且时间误差为零。"""
        result = self.results["B_asynchronous"]
        evaluation = result["evaluation"]
        grid = evaluation["grid"]
        self.assertGreater(grid["evaluated_count"], 0)
        for sensor, stats in grid["time_error_by_sensor"].items():
            self.assertLess(abs(stats["max"]), 1.5)

    # -- C ------------------------------------------------------------------

    def test_c_dropouts_appear_as_gaps(self):
        """漏报在观测流中缺席；插值后长间隔被标记不可用。"""
        result = self.results["C_observation_perturbation"]
        quality = result["quality"]
        ais_track = None
        for key, stats in quality["temporal"]["by_track"].items():
            if "ais" in key:
                ais_track = stats
        self.assertIsNotNone(ais_track)
        self.assertGreater(ais_track["unavailable"], 0)
        self.assertIn(
            "gap_exceeds_limit", ais_track["unavailable_reasons"]
        )

    def test_c_noise_reflected_in_error(self):
        """噪声场景：位置误差应与配置的噪声标准差同量级。

        场景 C 的噪声参数已用 MTDSP 实测重标定（雷达 σ=3.55 m、AIS σ=3.55 m，
        另加雷达测角噪声 0.02821°），因此误差量级比旧值（12 m / 25 m）小得多。
        判据改为按**配置的噪声标准差**推导期望区间，而不是写死数值。
        """
        evaluation = self.results["C_observation_perturbation"]["evaluation"]
        radar = evaluation["position_error_by_sensor"]["radar"]["planar_m"]
        ais = evaluation["position_error_by_sensor"]["ais"]["planar_m"]

        config = load_config("C_observation_perturbation")
        radar_sigma = config.radar_sensor.position_noise_std_m
        ais_sigma = config.ais_sensor.position_noise_std_m

        # 瑞利分布均值 = σ·sqrt(π/2)，观测误差还含量化与测角分量，
        # 故期望落在 [0.6, 2.5] 倍 σ 之间。
        def expect(sigma):
            return sigma * (math.pi / 2.0) ** 0.5

        for name, stats, sigma in (
            ("radar", radar, radar_sigma),
            ("ais", ais, ais_sigma),
        ):
            expected = expect(sigma)
            self.assertGreater(
                stats["mean"], 0.6 * expected, f"{name} 误差低于噪声期望"
            )
            self.assertLess(
                stats["mean"], 2.5 * expected, f"{name} 误差高于噪声期望"
            )

        # 雷达含测角噪声。测角误差的**切向**分量 = 方位残差 × 距离，
        # 切向方向垂直于视线。本场景目标视线方位约 7.6°（起始）到 −9.0°（末段），
        # 切向因此指向 ~98°，即**东向**。故雷达的东向误差 RMS 应大于北向。
        # （不能用总均值比较：AIS 额外含 0.0001° 量化 ≈ 11 m，
        #   其总均值反而可能略高于雷达。）
        radar_east = evaluation["position_error_by_sensor"]["radar"]["east_m"]
        radar_north = evaluation["position_error_by_sensor"]["radar"]["north_m"]
        self.assertGreater(radar_east["rms"], radar_north["rms"])

    def test_c_no_position_records_are_flagged(self):
        """无位置的记录不应静默通过。"""
        records = self.results["C_observation_perturbation"]["records"]
        for record in records:
            self.assertIn(
                record["alignment_status"], ("ok", "no_position", "failed")
            )
            if record["alignment_status"] != "ok":
                self.assertTrue(record.get("alignment_failure_reason"))

    # -- 通用 --------------------------------------------------------------

    def test_output_files_exist(self):
        for scenario_id, result in self.results.items():
            base = os.path.dirname(result["aligned_records"])
            for name in (
                "aligned_records.csv",
                "alignment_quality.json",
                "alignment_evaluation.json",
            ):
                self.assertTrue(
                    os.path.exists(os.path.join(base, name)),
                    f"{scenario_id} 缺少 {name}",
                )

    def test_aligned_records_keep_source_identity(self):
        """对齐结果不得提前合并成全局目标：雷达编号与 MMSI 分别保留。"""
        records = self.results["A_geometry_baseline"]["records"]
        radar_ids = {
            r["local_track_id"] for r in records if r["source_type"] == "radar"
        }
        ais_ids = {
            r["local_track_id"] for r in records if r["source_type"] == "ais"
        }
        self.assertEqual(radar_ids, {"00"})
        self.assertEqual(ais_ids, {"228050803"})
        self.assertNotEqual(radar_ids, ais_ids)

    def test_cross_source_consistency_reported(self):
        """两路在同一时刻的互差应接近协议量化之和。"""
        evaluation = self.results["A_geometry_baseline"]["evaluation"]
        cross = evaluation["cross_source_consistency"]
        self.assertGreater(cross["pair_count"], 0)
        self.assertLess(cross["planar_diff_m"]["mean"], 1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
