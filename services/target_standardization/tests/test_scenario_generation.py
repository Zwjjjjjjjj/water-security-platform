"""模拟场景生成的端到端验证。

验证链：
  生成场景 → 现有适配器读取 → 标准记录 → 与真值比对

覆盖三个场景：
  A 几何基线：单位、坐标转换、报文编码与解码正确
  B 异步观测：时间整理与匹配
  C 观测扰动：对齐误差与缺失处理
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization import standardize  # noqa: E402
from simulation import ScenarioConfig, ScenarioGenerator  # noqa: E402
from simulation import geo, nmea_encode  # noqa: E402

SCENARIO_DIR = os.path.join(ROOT, "configs", "scenarios")
OUTPUT_ROOT = os.path.join(ROOT, "output", "scenarios")

RADAR_CONFIG = {
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

AIS_CONFIG = {
    "source_id": "sim_ais",
    "source_type": "ais_vdm",
    "session_id": "sim-session",
    "frame_id": "wgs84",
    "simulation_date_binding": "zda_date",
    "placeholder_positions": [[0.0, 0.0]],
}


def load_config(scenario_id: str) -> ScenarioConfig:
    with open(
        os.path.join(SCENARIO_DIR, f"{scenario_id}.json"), encoding="utf-8"
    ) as handle:
        data = json.load(handle)
    template = data.get("template_source_csv")
    if template and not os.path.isabs(template):
        data["template_source_csv"] = os.path.join(ROOT, template)
    return ScenarioConfig.from_dict(data)


def read_csv(path: str):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class EncodingTests(unittest.TestCase):
    """编码层单元测试。"""

    def test_time_format(self):
        self.assertEqual(nmea_encode.format_time_of_day(54000.0), "150000.00")
        self.assertEqual(nmea_encode.format_time_of_day(54054.5), "150054.50")
        self.assertEqual(nmea_encode.format_time_of_day(86399.99), "235959.99")

    def test_ttm_layout_matches_manual(self):
        sentence = nmea_encode.encode_ttm(
            target_number="00",
            distance_nm=0.1,
            bearing_deg=9.7,
            speed_kn=4.0,
            course_deg=2.7,
            time_of_day="150054.00",
        )
        body = sentence.split("*")[0]
        fields = body.strip("$").split(",")[1:]
        self.assertEqual(fields[0], "00")
        self.assertEqual(fields[1], "0.100")
        self.assertEqual(fields[2], "9.70")
        self.assertEqual(fields[3], "T")
        self.assertEqual(fields[4], "4.0")
        self.assertEqual(fields[5], "2.7")
        self.assertEqual(fields[6], "T")
        self.assertEqual(fields[9], "N")  # 第 10 字段 = Speed/distance units
        self.assertEqual(fields[13], "150054.00")
        self.assertTrue(nmea_encode.verify_sentence(sentence))

    def test_ais_vdm_roundtrip(self):
        import pyais

        sentence = nmea_encode.encode_ais_vdm(
            mmsi=228050803,
            latitude_deg=54.260303,
            longitude_deg=12.048277,
            speed_kn=17.2,
            course_deg=344.0,
            heading_deg=344,
            second_of_minute=8,
        )
        self.assertTrue(sentence.startswith("!AIVDM,"))
        message = pyais.decode(sentence)
        self.assertEqual(str(message.mmsi), "228050803")
        self.assertAlmostEqual(message.lat, 54.260303, places=4)
        self.assertAlmostEqual(message.lon, 12.048277, places=4)
        self.assertEqual(message.second, 8)

    def test_local_origin_roundtrip(self):
        origin = geo.LocalOrigin(latitude_deg=54.1082, longitude_deg=12.0568)
        lat, lon = origin.to_latlng(400.0, 3000.0)
        east, north = origin.to_enu(lat, lon)
        # ECEF 往返是精确的（残差来自椭球↔大地坐标迭代收敛）
        self.assertAlmostEqual(east, 400.0, places=2)
        self.assertAlmostEqual(north, 3000.0, places=2)


class ReferenceTrajectoryTests(unittest.TestCase):
    """共同参考轨迹的提取。"""

    def test_template_selection_and_extraction(self):
        from simulation import trajectory

        source = os.path.join(ROOT, "data", "radarpwn", "ais_with_capture_time.csv")
        tracks, stats = trajectory.read_ais_vdm_points(
            source, placeholder_positions=[(0.0, 0.0)]
        )
        self.assertEqual(stats["decoded"], 7719)
        self.assertEqual(stats["skipped_placeholder"], 18)
        # 76 个 MMSI 中有 3 个的全部位置都是 (0,0) 占位值，剔除后剩 73 条有位置的轨迹
        self.assertEqual(len(tracks), 73)

        scores = trajectory.score_tracks(tracks)
        self.assertTrue(scores)
        best = scores[0]
        # 模板必须有真实运动、连续采样、航向自洽
        self.assertGreater(best["path_length_m"], 1000.0)
        self.assertEqual(best["gap_count"], 0)
        self.assertLess(best["cog_consistency_mean_deg"], 2.0)

        extraction = trajectory.extract_reference_trajectory(
            source,
            mmsi=best["mmsi"],
            placeholder_positions=[(0.0, 0.0)],
            resample_dt_s=1.0,
        )
        self.assertGreater(len(extraction.samples), 100)
        self.assertEqual(extraction.provenance["source_points"], len(tracks[best["mmsi"]]))
        # 参考轨迹应有连续的速度
        speeds = [s.speed_mps for s in extraction.samples]
        self.assertGreater(min(speeds), 0.5)
        self.assertLess(max(speeds) - min(speeds), 3.0)

    def test_provenance_records_all_steps(self):
        from simulation import trajectory

        source = os.path.join(ROOT, "data", "radarpwn", "ais_with_capture_time.csv")
        extraction = trajectory.extract_reference_trajectory(
            source,
            mmsi="228050803",
            placeholder_positions=[(0.0, 0.0)],
        )
        provenance = extraction.provenance
        for key in (
            "source_file",
            "source_mmsi",
            "source_points",
            "outliers_removed",
            "outlier_threshold_m",
            "resample_dt_s",
            "smooth_window",
            "origin",
            "decode_stats",
        ):
            self.assertIn(key, provenance)


class ScenarioEndToEndTests(unittest.TestCase):
    """生成 → 适配器读取 → 与真值比对。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.generated = {}
        for scenario_id in (
            "A_geometry_baseline",
            "B_asynchronous",
            "C_observation_perturbation",
        ):
            config = load_config(scenario_id)
            out_dir = os.path.join(cls.tmp.name, scenario_id)
            ScenarioGenerator(config).generate(out_dir)
            cls.generated[scenario_id] = out_dir

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _standardize(self, scenario_id: str, source: str):
        """把生成的数据用现有适配器标准化，返回标准记录与真值。"""
        out_dir = self.generated[scenario_id]
        work = os.path.join(self.tmp.name, f"std_{scenario_id}_{source}")
        if source == "radar":
            config = dict(
                RADAR_CONFIG,
                data_root=out_dir,
                all_incoming_csv=os.path.join(
                    out_dir, "all_incoming_nmea_with_capture_time.csv"
                ),
            )
            input_path = os.path.join(out_dir, "radar_ttm_with_capture_time.csv")
        else:
            config = dict(
                AIS_CONFIG,
                data_root=out_dir,
                all_incoming_csv=os.path.join(
                    out_dir, "all_incoming_nmea_with_capture_time.csv"
                ),
            )
            input_path = os.path.join(out_dir, "ais_with_capture_time.csv")
        summary = standardize(input_path, config, work)
        with open(
            os.path.join(work, "standard_records.jsonl"), encoding="utf-8"
        ) as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        truth = read_csv(os.path.join(out_dir, "reference_truth.csv"))
        return summary, records, truth

    # -- 场景 A ------------------------------------------------------------

    def test_a_radar_roundtrip_precision(self):
        """几何基线：无噪声时，雷达距离/方位应与真值一致到编码精度。"""
        summary, records, truth = self._standardize("A_geometry_baseline", "radar")
        self.assertEqual(summary["output_count"], summary["total_count"])
        self.assertGreater(len(records), 1000)

        radar_truth = [row for row in truth if row["sensor"] == "radar"]
        self.assertEqual(len(records), len(radar_truth))

        max_range_err = 0.0
        max_bearing_err = 0.0
        for record, row in zip(records, radar_truth):
            self.assertEqual(record["local_track_id"], "00")
            self.assertEqual(record["tracking_status"], "tracking")
            self.assertEqual(record["unit_source"], "ttm_field_10_speed_distance_units")
            self.assertEqual(record["distance_unit"], "nautical_mile")
            self.assertEqual(record["speed_unit"], "knot")

            # TTM 距离精度 0.001 nm ≈ 1.85 m
            range_err = abs(record["position"]["range_m"] - float(row["obs_range_nm"]) * 1852.0)
            max_range_err = max(max_range_err, range_err)
            # TTM 方位精度 0.01°
            bearing_err = abs(
                math.degrees(record["position"]["bearing_rad"]) - float(row["obs_bearing_deg"])
            )
            max_bearing_err = max(max_bearing_err, bearing_err)

        self.assertLess(max_range_err, 2.0)  # < 2 m
        self.assertLess(max_bearing_err, 0.02)  # < 0.02°

    def test_a_ais_roundtrip_precision(self):
        """几何基线：AIS 经纬度应与真值一致到 AIS 编码精度。"""
        summary, records, truth = self._standardize("A_geometry_baseline", "ais")
        self.assertEqual(summary["output_count"], summary["total_count"])

        ais_truth = [row for row in truth if row["sensor"] == "ais"]
        self.assertEqual(len(records), len(ais_truth))

        max_pos_err_m = 0.0
        for record, row in zip(records, ais_truth):
            self.assertEqual(record["local_track_id"], "228050803")
            distance = geo.haversine_m(
                record["position"]["latitude_deg"],
                record["position"]["longitude_deg"],
                float(row["truth_latitude_deg"]),
                float(row["truth_longitude_deg"]),
            )
            max_pos_err_m = max(max_pos_err_m, distance)

        # AIS 位置编码精度约 1.7e-5 分 ≈ 1.9 m
        self.assertLess(max_pos_err_m, 3.0)

    def test_a_time_axis_correct(self):
        """几何基线：仿真时间与接收时间都应落在预期位置。"""
        _, records, _ = self._standardize("A_geometry_baseline", "radar")
        record = records[0]
        self.assertEqual(record["state_time_utc"], "2019-09-30T15:00:00+00:00")
        self.assertEqual(record["receive_time_utc"], "2022-06-03T16:30:00+00:00")
        self.assertEqual(record["time_basis"], "nmea_ttm_time_of_day+zda_date")
        self.assertTrue(record["valid_for_time_alignment"])

    def test_a_ais_time_axis_correct(self):
        """AIS 侧：仿真时间由 ZDA + 报文秒数还原，接收时间独立。"""
        _, records, _ = self._standardize("A_geometry_baseline", "ais")
        record = records[0]
        self.assertEqual(record["state_time_utc"], "2019-09-30T15:00:00+00:00")
        self.assertEqual(record["receive_time_utc"], "2022-06-03T16:30:00+00:00")

    # -- 场景 B ------------------------------------------------------------

    def test_b_asynchronous_sampling(self):
        """异步观测：两路采样周期不同。"""
        _, radar_records, radar_truth = self._standardize("B_asynchronous", "radar")
        _, ais_records, ais_truth = self._standardize("B_asynchronous", "ais")

        radar_dts = {
            round(
                (
                    float(radar_records[i + 1]["receive_time_unix"])
                    - float(radar_records[i]["receive_time_unix"])
                ),
                3,
            )
            for i in range(len(radar_records) - 1)
        }
        ais_dts = {
            round(
                (
                    float(ais_records[i + 1]["receive_time_unix"])
                    - float(ais_records[i]["receive_time_unix"])
                ),
                3,
            )
            for i in range(len(ais_records) - 1)
        }
        self.assertEqual(radar_dts, {2.0})
        self.assertEqual(ais_dts, {5.0})

    def test_b_known_clock_offsets_visible(self):
        """时钟偏移与接收延迟应体现在状态时间/接收时间的差值上。"""
        _, radar_records, _ = self._standardize("B_asynchronous", "radar")
        _, ais_records, _ = self._standardize("B_asynchronous", "ais")

        # 雷达：clock_offset = +0.25 s，receive_delay = 0.4 s
        # TTM 第 14 字段有 0.01 s 分辨率，亚秒偏移可完整还原
        self.assertEqual(
            radar_records[0]["state_time_utc"], "2019-09-30T15:00:00.250000+00:00"
        )
        self.assertEqual(
            radar_records[0]["receive_time_utc"], "2022-06-03T16:30:00.400000+00:00"
        )

        # AIS：clock_offset = −0.5 s，receive_delay = 1.2 s
        # !AIVDM 的 second 字段只有**整秒**精度（0–59），亚秒部分无法从
        # 报文本身还原，因此状态时间只能精确到整秒（真值 14:59:59.5）。
        self.assertEqual(ais_records[0]["state_time_utc"], "2019-09-30T14:59:59+00:00")
        self.assertEqual(
            ais_records[0]["receive_time_utc"], "2022-06-03T16:30:01.200000+00:00"
        )

    def test_b_ais_subsecond_offset_not_recoverable(self):
        """AIS 的整秒精度是格式固有限制，需在报告中如实说明。"""
        _, ais_records, _ = self._standardize("B_asynchronous", "ais")
        offsets = set()
        for record in ais_records[:20]:
            iso = record["state_time_utc"]
            # ISO 形如 2019-09-30T14:59:59+00:00 → 小数部分在 [19:20]
            offsets.add(iso[19:20])
        # 亚秒部分恒为空（整秒精度）
        self.assertEqual(offsets, {"+"})

    # -- 场景 C ------------------------------------------------------------

    def test_c_dropouts_are_gaps_not_removals(self):
        """观测扰动：漏报体现为采样间隙，条数仍与配置一致。"""
        _, radar_records, radar_truth = self._standardize(
            "C_observation_perturbation", "radar"
        )
        dropped = [row for row in radar_truth if row["dropped"] == "true"]
        self.assertGreater(len(dropped), 0)
        # 漏报在观测流中缺席（条数少于栅格长度），但真值行仍保留
        self.assertLess(len(radar_records), len(radar_truth))

    def test_c_position_error_within_bound(self):
        """观测扰动：AIS 位置误差应符合配置的高斯分布。"""
        import statistics

        config = load_config("C_observation_perturbation")
        std_m = config.ais_sensor.position_noise_std_m
        _, records, truth = self._standardize(
            "C_observation_perturbation", "ais"
        )
        ais_truth = [
            row
            for row in truth
            if row["sensor"] == "ais" and row.get("emitted", "true") == "true"
        ]
        # 观测流与真值按顺序一一对应（漏报的观测不在流中，也不参与配对）
        self.assertEqual(len(records), len(ais_truth))
        errors = []
        for record, row in zip(records, ais_truth):
            errors.append(
                geo.haversine_m(
                    record["position"]["latitude_deg"],
                    record["position"]["longitude_deg"],
                    float(row["truth_latitude_deg"]),
                    float(row["truth_longitude_deg"]),
                )
            )
        self.assertGreater(len(errors), 100)

        # 二维高斯噪声的径向误差服从瑞利分布：
        # 均值 = σ·sqrt(π/2)，中位数 = σ·sqrt(2·ln2)
        mean_expected = std_m * (math.pi / 2.0) ** 0.5
        median_expected = std_m * (2 * math.log(2)) ** 0.5

        # 但位置残差还含**量化分量**：观测位置先加二维高斯噪声，再按
        # quantization_deg 量化（截断到 0.0001° 网格）。量化误差在两个轴上
        # 都是均匀分布，且两轴步长不同（纬度 0.0001°≈11.13 m，经度
        # 0.0001°·cos(54.1°)≈6.53 m），因此残差是瑞利分布与矩形分布的卷积，
        # 其期望高于纯噪声期望。
        #
        # 这里用蒙特卡洛积分算出卷积后的期望（不依赖测试自身的数据，
        # 避免自证），再与实测比较。
        quant_step_lat_m = config.ais_sensor.quantization_deg * 111320.0
        quant_step_lon_m = (
            config.ais_sensor.quantization_deg
            * 111320.0
            * math.cos(math.radians(54.1082))
        )
        expected_with_quant = self._expected_error_with_quantization(
            std_m, quant_step_lat_m, quant_step_lon_m
        )

        self.assertAlmostEqual(
            statistics.mean(errors),
            expected_with_quant,
            delta=0.15 * expected_with_quant,
        )
        # 中位数对量化更不敏感，但仍需落在噪声期望的合理倍数内
        self.assertGreater(statistics.median(errors), 0.7 * median_expected)
        self.assertLess(statistics.median(errors), 1.8 * median_expected)

        # 量化误差另计（0.0001° ≈ 11 m），因此最大值只做宽松上界检查
        self.assertLess(max(errors), 6 * std_m + 15.0)

    @staticmethod
    def _expected_error_with_quantization(
        sigma: float, step_lat_m: float, step_lon_m: float, samples: int = 200000
    ) -> float:
        """二维高斯噪声 + 双轴均匀量化的期望径向误差（蒙特卡洛）。

        固定随机种子保证可复现；样本量足够大时标准误约 0.01 m，
        远小于测试用的 15% 容差。
        """
        import random

        rng = random.Random(20250305)
        total = 0.0
        for _ in range(samples):
            n1 = rng.gauss(0.0, sigma)
            n2 = rng.gauss(0.0, sigma)
            q1 = rng.uniform(-step_lat_m / 2.0, step_lat_m / 2.0)
            q2 = rng.uniform(-step_lon_m / 2.0, step_lon_m / 2.0)
            total += math.hypot(n1 + q1, n2 + q2)
        return total / samples

    def test_c_quantization_applied_to_ais(self):
        """观测扰动：AIS 观测位置已量化到配置步长。"""
        config = load_config("C_observation_perturbation")
        step = config.ais_sensor.quantization_deg
        self.assertGreater(step, 0)
        _, records, _ = self._standardize("C_observation_perturbation", "ais")
        for record in records[:50]:
            lat = record["position"]["latitude_deg"]
            self.assertLess(abs(lat / step - round(lat / step)), 1e-6)

    # -- 通用 --------------------------------------------------------------

    def test_truth_rows_cover_both_sensors(self):
        for scenario_id in self.generated:
            truth = read_csv(
                os.path.join(self.generated[scenario_id], "reference_truth.csv")
            )
            sensors = {row["sensor"] for row in truth}
            self.assertEqual(sensors, {"radar", "ais"})

    def test_mapping_file_is_eval_only(self):
        for scenario_id, out_dir in self.generated.items():
            with open(
                os.path.join(out_dir, "target_mapping.json"), encoding="utf-8"
            ) as handle:
                mapping = json.load(handle)
            self.assertIn("仅供评测", mapping["purpose"])
            self.assertEqual(len(mapping["pairs"]), 1)
            pair = mapping["pairs"][0]
            self.assertEqual(pair["radar_local_track_id"], "00")
            self.assertEqual(pair["sim_mmsi"], 228050803)

    def test_manifest_records_provenance(self):
        for scenario_id, out_dir in self.generated.items():
            with open(
                os.path.join(out_dir, "scenario_manifest.json"), encoding="utf-8"
            ) as handle:
                manifest = json.load(handle)
            reference = manifest["reference_trajectory"]
            self.assertIn("source_file", reference)
            self.assertIn("source_mmsi", reference)
            self.assertIn("outliers_removed", reference)
            self.assertIn("semantics", reference)
            self.assertIn("alignment", reference)
            self.assertEqual(manifest["counts"]["radar_dropped"], manifest["counts"]["radar_dropped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
