"""标准化模块验收测试。

覆盖：
- 单位换算与人工计算一致（含 TTM 第 10 字段 Speed/distance units）
- TTM 记录结构（编号、状态、原始报文保留）
- 时间依据（抓包时间与仿真时间分开保存、ZDA 关联）
- 异常处理（坏校验和、非法位置、空字段、多目标）
- AIS 字段映射与静态属性补充
- 位置跳变检测（粗量化正常航行 / 粗量化下真实跳变）
- record_id 稳定性（相对路径 + session_id）
- valid_for_* 判定条件
- 数量可追溯：输入数 = 输出数 + 拒绝数
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

from standardization import schema, standardize, units  # noqa: E402
from standardization import radar_ttm_adapter, validation  # noqa: E402

RADAR_DIR = os.path.join(ROOT, "data", "radarpwn")
AIS_DIR = os.path.join(ROOT, "data", "mtdsp")

AIS_HEADER = [
    "MMSI",
    "经度(°)",
    "纬度(°)",
    "时间",
    "速度(kn)",
    "船艏向(°)",
    "对地航向(°)",
    "航行状态",
    "相对视角(°)",
    "姿态信息",
    "浪高(m)",
    "方位角(°)",
    "距离(m)",
]


def nmea_checksum(body: str) -> str:
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def make_ttm(
    target="00",
    distance="0.1",
    bearing="9.7",
    brg_ref="T",
    speed="4.0",
    course="2.7",
    cog_ref="T",
    units_field="N",
    name="TGT00",
    status="T",
    tod="150054.00",
) -> str:
    """按标准 TTM 布局构造报文（手册第 N 字段 → 索引 N-1）。

    字段：1 编号 / 2 距离 / 3 方位 / 4 方位参考 / 5 速度 / 6 航向 /
    7 航向参考 / 8 CPA 距离 / 9 CPA 时间 / 10 Speed-distance units /
    11 名称 / 12 状态 / 13 参考目标标记 / 14 日内时间 / 15 获取类型
    """
    body = (
        f"$RATTM,{target},{distance},{bearing},{brg_ref},{speed},{course},"
        f"{cog_ref},0.0,0.3,{units_field},{name},{status},,{tod},A"
    )
    return f"{body}*{nmea_checksum(body)}"


RADAR_FIELD_MAPPING = {
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
}

AIS_FIELD_MAPPING = {
    "mmsi": "MMSI",
    "longitude": "经度(°)",
    "latitude": "纬度(°)",
    "time": "时间",
    "speed": "速度(kn)",
    "heading": "船艏向(°)",
    "course": "对地航向(°)",
    "navigation_status": "航行状态",
    "relative_view": "相对视角(°)",
    "attitude": "姿态信息",
    "wave_height": "浪高(m)",
    "bearing": "方位角(°)",
    "distance": "距离(m)",
}


def radar_config(**overrides):
    config = {
        "source_id": "test_radar",
        "source_type": "radar",
        "session_id": "test-session",
        "frame_id": "radar_local_polar",
        "simulation_date_binding": "zda_date",
        "field_mapping": dict(RADAR_FIELD_MAPPING),
    }
    config.update(overrides)
    return config


def ais_config(**overrides):
    config = {
        "source_id": "test_ais",
        "source_type": "ais",
        "session_id": "test-ais-session",
        "frame_id": "wgs84",
        "speed_unit": "K",
        "speed_unit_source": "csv_header_declared",
        "timezone_known": False,
        "field_mapping": dict(AIS_FIELD_MAPPING),
    }
    config.update(overrides)
    return config


def write_ttm_csv(path: str, sentences):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(
            "capture_time_unix,capture_time_utc,sentence_type,checksum_valid,sentence\n"
        )
        for index, sentence in enumerate(sentences):
            handle.write(
                f"1654273908.9{index},2022-06-03T16:31:48.95{index}000+00:00,TTM,True,\"{sentence}\"\n"
            )


def write_ais_csv(path: str, rows, header=None):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(",".join(header or AIS_HEADER) + "\r\n")
        for row in rows:
            handle.write(",".join(row) + "\r\n")


def read_jsonl(path: str):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class UnitTests(unittest.TestCase):
    def test_distance_and_speed(self):
        self.assertAlmostEqual(units.distance_to_meters(0.1, "N"), 185.2, places=6)
        self.assertAlmostEqual(units.distance_to_meters(1.0, "K"), 1000.0, places=6)
        self.assertAlmostEqual(units.distance_to_meters(1.0, "M"), 1.0, places=6)
        self.assertAlmostEqual(units.speed_to_mps(4.0, "K"), 4.0 * 1852.0 / 3600.0, places=9)
        self.assertAlmostEqual(units.speed_to_mps(1.0, "M"), 1.0, places=9)
        self.assertAlmostEqual(units.speed_to_mps(3.6, "N"), 1.0, places=9)

    def test_angle(self):
        self.assertAlmostEqual(units.deg_to_rad(9.7), 0.169296937, places=9)
        self.assertAlmostEqual(units.deg_to_rad(2.7), 0.047123890, places=9)

    def test_unknown_unit_returns_none(self):
        self.assertIsNone(units.distance_to_meters(1.0, "X"))
        self.assertIsNone(units.speed_to_mps(1.0, None))

    def test_ttm_unit_pair_table(self):
        """TTM 第 10 字段同时规定距离与速度单位。"""
        n = units.ttm_unit_pair("N")
        self.assertEqual(n[0], "nautical_mile")
        self.assertEqual(n[1], 1852.0)
        self.assertEqual(n[2], "knot")
        self.assertAlmostEqual(n[3], 1852.0 / 3600.0, places=9)

        k = units.ttm_unit_pair("K")
        self.assertEqual(k[0], "kilometre")
        self.assertEqual(k[1], 1000.0)
        self.assertEqual(k[2], "kilometre_per_hour")
        self.assertAlmostEqual(k[3], 1000.0 / 3600.0, places=9)

        s = units.ttm_unit_pair("S")
        self.assertEqual(s[0], "statute_mile")
        self.assertAlmostEqual(s[1], 1609.344, places=6)
        self.assertEqual(s[2], "statute_mile_per_hour")
        self.assertAlmostEqual(s[3], 1609.344 / 3600.0, places=9)

        self.assertIsNone(units.ttm_unit_pair("X"))
        self.assertIsNone(units.ttm_unit_pair(None))


class RadarAdapterTests(unittest.TestCase):
    def _run(self, sentences, sim_context=None, config=None):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "ttm.csv")
            write_ttm_csv(csv_path, sentences)
            adapter = radar_ttm_adapter.RadarTTMAdapter(config or radar_config())
            return adapter.run(csv_path, sim_context=sim_context)

    def test_single_record_mapping(self):
        records, rejected, _ = self._run([make_ttm()])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(rejected), 0)
        record = records[0]
        self.assertEqual(record["local_track_id"], "00")
        self.assertAlmostEqual(record["position"]["range_m"], 185.2, places=3)
        self.assertAlmostEqual(record["position"]["bearing_rad"], 0.169296937, places=9)
        self.assertEqual(record["position"]["bearing_reference"], "T")
        self.assertAlmostEqual(record["speed_mps"], 2.057778, places=6)
        self.assertAlmostEqual(record["course_rad"], 0.047123890, places=9)
        self.assertEqual(record["course_reference"], "T")
        self.assertEqual(record["tracking_status"], "tracking")
        self.assertIsNone(record["motion_reference"])
        self.assertEqual(record["raw_record"], make_ttm())
        self.assertTrue(record["valid_for_spatial_alignment"])
        self.assertFalse(record["valid_for_time_alignment"])  # 无仿真日期上下文

    def test_units_come_from_ttm_field_not_assumed(self):
        """第 10 字段给出单位，不应出现 speed_unit_assumed。"""
        records, _, _ = self._run([make_ttm(units_field="N")])
        record = records[0]
        self.assertEqual(record["unit_source"], "ttm_field_10_speed_distance_units")
        self.assertEqual(record["distance_unit"], "nautical_mile")
        self.assertEqual(record["speed_unit"], "knot")
        self.assertNotIn("speed_unit_assumed", record)

    def test_units_field_k_and_s(self):
        records, _, _ = self._run([make_ttm(units_field="K", distance="1.0", speed="36")])
        record = records[0]
        self.assertAlmostEqual(record["position"]["range_m"], 1000.0, places=6)
        self.assertAlmostEqual(record["speed_mps"], 10.0, places=9)
        self.assertEqual(record["speed_unit"], "kilometre_per_hour")

        records, _, _ = self._run([make_ttm(units_field="S", distance="1.0", speed="1")])
        record = records[0]
        self.assertAlmostEqual(record["position"]["range_m"], 1609.344, places=3)
        self.assertAlmostEqual(record["speed_mps"], 1609.344 / 3600.0, places=9)

    def test_status_mapping(self):
        sentences = [make_ttm(status="Q"), make_ttm(status="T"), make_ttm(status="L")]
        records, _, _ = self._run(sentences)
        self.assertEqual(
            [r["tracking_status"] for r in records],
            ["acquiring", "tracking", "lost"],
        )
        self.assertEqual([r["tracking_status_raw"] for r in records], ["Q", "T", "L"])

    def test_multiple_targets_and_leading_zeros(self):
        sentences = [make_ttm(target="00"), make_ttm(target="07"), make_ttm(target="12")]
        records, _, _ = self._run(sentences)
        self.assertEqual([r["local_track_id"] for r in records], ["00", "07", "12"])
        self.assertEqual(len({r["record_id"] for r in records}), 3)

    def test_bad_checksum_rejected(self):
        broken = make_ttm()[:-2] + "00"
        records, rejected, _ = self._run([make_ttm(), broken])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "bad_checksum")
        self.assertEqual(rejected[0]["raw_record"], broken)

    def test_illegal_position_rejected(self):
        records, rejected, _ = self._run([make_ttm(distance="notanumber")])
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "invalid_field_value")

    def test_empty_speed_marked_not_zero(self):
        records, rejected, _ = self._run([make_ttm(speed="")])
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["speed_mps"])
        self.assertIn(schema.ISSUE_MISSING_SPEED, records[0]["issues"])
        self.assertEqual(len(rejected), 0)

    def test_simulation_date_context_fills_utc(self):
        context = radar_ttm_adapter.time_fields.SimulationDateContext(
            date_iso="2019-09-30", consistent=True, evidence_count=1
        )
        records, _, _ = self._run([make_ttm()], sim_context=context)
        record = records[0]
        self.assertEqual(record["state_time_utc"], "2019-09-30T15:00:54+00:00")
        self.assertEqual(record["time_basis"], schema.TIME_BASIS_TTM_TOD_ZDA_DATE)
        self.assertTrue(record["receive_time_utc"].startswith("2022-06-03T16:31:48.95"))
        self.assertEqual(record["receive_time_source"], "pcap_record_time")
        self.assertTrue(record["valid_for_time_alignment"])


class ZdaAssociationTests(unittest.TestCase):
    """ZDA 关联规则与偏差计算。"""

    def _context(self):
        entries = [
            {
                "csv_row": 10,
                "time_of_day": "150002.00",
                "seconds_of_day": 54002.0,
                "day": "30",
                "month": "09",
                "year": "2019",
                "capture_time_unix": "1654273857.260555",
            },
            {
                "csv_row": 20,
                "time_of_day": "151815.00",
                "seconds_of_day": 55095.0,
                "day": "30",
                "month": "09",
                "year": "2019",
                "capture_time_unix": "1654274947.170927",
            },
        ]
        ctx = radar_ttm_adapter.time_fields.build_simulation_date_context(entries)
        ctx.entries = entries
        return ctx

    def test_association_picks_latest_not_later_zda(self):
        ctx = self._context()
        result = ctx.associate(54100.0)
        self.assertEqual(result["association"], "ok")
        self.assertEqual(result["zda_csv_row"], 10)
        self.assertEqual(result["sim_time_delta_s"], 98.0)

    def test_association_falls_back_when_earlier_than_first(self):
        ctx = self._context()
        result = ctx.associate(53000.0)
        self.assertEqual(result["association"], "fallback_earliest")
        self.assertEqual(result["zda_csv_row"], 10)

    def test_residual_is_capture_delta_minus_sim_delta(self):
        ctx = self._context()
        row = {"capture_time_unix": "1654273857.260555"}
        result = radar_ttm_adapter.RadarTTMAdapter._associate("150002.00", ctx, row)
        self.assertEqual(result["association"], "ok")
        self.assertAlmostEqual(result["capture_time_delta_s"], 0.0, places=6)
        self.assertAlmostEqual(result["sim_time_delta_s"], 0.0, places=6)
        self.assertAlmostEqual(result["residual_s"], 0.0, places=6)

    def test_no_context_reported(self):
        result = radar_ttm_adapter.RadarTTMAdapter._associate(
            "150054.00", None, {"capture_time_unix": "1.0"}
        )
        self.assertEqual(result["association"], "no_context")

    def test_date_conflict_disables_context(self):
        entries = [
            {"time_of_day": "150002.00", "day": "30", "month": "09", "year": "2019"},
            {"time_of_day": "150003.00", "day": "01", "month": "10", "year": "2019"},
        ]
        ctx = radar_ttm_adapter.time_fields.build_simulation_date_context(entries)
        self.assertFalse(ctx.consistent)
        self.assertTrue(any("不一致" in note for note in ctx.notes))


class PositionJumpTests(unittest.TestCase):
    """位置跳变：粗量化正常航行不告警，粗量化下的真实跳变要告警。"""

    def _record(self, mmsi, time_raw, lon, lat, track="T1"):
        record = schema.new_record(
            source_id="s",
            source_type=schema.SOURCE_TYPE_AIS,
            session_id="sess",
            local_track_id=mmsi,
            state_time_raw=time_raw,
            position_type=schema.POSITION_TYPE_LATLON,
            frame_id="wgs84",
            position={"longitude_deg": lon, "latitude_deg": lat},
        )
        record["track"] = track
        return record

    def test_coarse_quantization_normal_motion_no_alert(self):
        """经度 0.001°、纬度 0.0001° 粗量化下，真实 6 kn 航行不应告警。"""
        records = []
        # 每 60 s 前进约 185 m（≈6 kn）
        for i in range(6):
            lon = 121.400 + 0.0017 * i
            lat = 37.5600 + 0.00017 * i
            records.append(
                self._record(
                    "412331720",
                    f"2025-03-05 09:{29 + i:02d}:00",
                    round(lon, 3),
                    round(lat, 4),
                )
            )
        lon_q = validation.QuantizationSpec(0.001, basis="csv", confident=True)
        lat_q = validation.QuantizationSpec(0.0001, basis="csv", confident=True)
        validation.detect_position_jumps(
            records, max_speed_mps=15.0, lon_quantization=lon_q, lat_quantization=lat_q
        )
        self.assertEqual(
            sum(1 for r in records if schema.ISSUE_POSITION_JUMP in r["issues"]), 0
        )

    def test_real_jump_under_coarse_quantization_is_alerted(self):
        """同样粗量化下，单点大幅偏离必须告警。"""
        records = [
            self._record("412331720", "2025-03-05 09:29:00", 121.4000, 37.5600),
            self._record("412331720", "2025-03-05 09:30:00", 121.4017, 37.56017),
            self._record("412331720", "2025-03-05 09:31:00", 121.4200, 37.5700),  # 跳变
        ]
        lon_q = validation.QuantizationSpec(0.001, basis="csv", confident=True)
        lat_q = validation.QuantizationSpec(0.0001, basis="csv", confident=True)
        validation.detect_position_jumps(
            records, max_speed_mps=15.0, lon_quantization=lon_q, lat_quantization=lat_q
        )
        jumped = [r for r in records if schema.ISSUE_POSITION_JUMP in r["issues"]]
        self.assertEqual(len(jumped), 1)
        detail = jumped[0]["position_jump"]
        # 原始量与调整后量都要保留
        self.assertIn("raw_distance_m", detail)
        self.assertIn("raw_speed_mps", detail)
        self.assertIn("quantization_margin_m", detail)
        self.assertIn("adjusted_speed_mps", detail)
        self.assertTrue(detail["quantization_reliable"])
        self.assertGreater(detail["raw_speed_mps"], detail["adjusted_speed_mps"])

    def test_unreliable_quantization_marks_uncertain(self):
        """量化依据不可靠时不自动扣除裕度，标记 uncertain。"""
        records = [
            self._record("412331720", "2025-03-05 09:29:00", 121.4000, 37.5600),
            self._record("412331720", "2025-03-05 09:30:00", 121.4200, 37.5700),
        ]
        lon_q = validation.QuantizationSpec(0.001, basis="unknown", confident=False)
        lat_q = validation.QuantizationSpec(0.0001, basis="unknown", confident=False)
        validation.detect_position_jumps(
            records, max_speed_mps=15.0, lon_quantization=lon_q, lat_quantization=lat_q
        )
        jumped = [r for r in records if schema.ISSUE_POSITION_JUMP in r["issues"]]
        self.assertEqual(len(jumped), 1)
        self.assertIn(schema.ISSUE_POSITION_JUMP_UNCERTAIN, jumped[0]["issues"])
        detail = jumped[0]["position_jump"]
        self.assertFalse(detail["quantization_reliable"])
        self.assertTrue(detail["uncertain"])
        self.assertEqual(detail["adjusted_speed_mps"], detail["raw_speed_mps"])

    def test_quantization_margin_uses_separate_lon_lat_scales(self):
        """经度与纬度按各自纬度处的米/度换算，不能统一用同一数值。"""
        lon_q = validation.QuantizationSpec(0.001, confident=True, axis="longitude")
        lat_q = validation.QuantizationSpec(0.001, confident=True, axis="latitude")
        lon_margin = lon_q.margin_m(37.56)
        lat_margin = lat_q.margin_m(37.56)
        # 纬度方向不经 cos 修正，经度方向要乘 cos(lat)
        self.assertAlmostEqual(lat_margin, 111.32, places=2)
        self.assertAlmostEqual(lon_margin, 111.32 * math.cos(math.radians(37.56)), places=2)
        self.assertNotAlmostEqual(lon_margin, lat_margin, places=1)
        self.assertGreater(lat_margin / lon_margin, 1.0)

    def test_no_quantization_config_is_unreliable(self):
        spec = validation.QuantizationSpec(None)
        self.assertFalse(spec.confident)
        self.assertIsNone(spec.margin_m(37.56))


class RecordIdStabilityTests(unittest.TestCase):
    """record_id 使用相对数据根目录的稳定路径，并纳入 session_id。"""

    def test_record_id_same_when_folder_moved(self):
        sentences = [make_ttm()]
        ids = []
        for root in ("dir_a", "dir_b"):
            with tempfile.TemporaryDirectory() as tmp:
                data_root = os.path.join(tmp, root)
                os.makedirs(data_root)
                csv_path = os.path.join(data_root, "ttm.csv")
                write_ttm_csv(csv_path, sentences)
                config = radar_config(data_root=data_root)
                out = os.path.join(tmp, "out")
                standardize(csv_path, config, out)
                records = read_jsonl(os.path.join(out, "standard_records.jsonl"))
                ids.append(records[0]["record_id"])
                self.assertEqual(records[0]["source_file"], "ttm.csv")
        self.assertEqual(ids[0], ids[1])

    def test_record_id_differs_across_sessions(self):
        sentences = [make_ttm()]
        ids = []
        for session in ("session-A", "session-B"):
            with tempfile.TemporaryDirectory() as tmp:
                csv_path = os.path.join(tmp, "ttm.csv")
                write_ttm_csv(csv_path, sentences)
                out = os.path.join(tmp, "out")
                standardize(csv_path, radar_config(session_id=session), out)
                records = read_jsonl(os.path.join(out, "standard_records.jsonl"))
                ids.append(records[0]["record_id"])
        self.assertNotEqual(ids[0], ids[1])

    def test_record_id_stable_across_runs(self):
        sentences = [make_ttm()]
        ids = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                csv_path = os.path.join(tmp, "ttm.csv")
                write_ttm_csv(csv_path, sentences)
                out = os.path.join(tmp, "out")
                standardize(csv_path, radar_config(), out)
                records = read_jsonl(os.path.join(out, "standard_records.jsonl"))
                ids.append(records[0]["record_id"])
        self.assertEqual(ids[0], ids[1])


class ValidityConditionTests(unittest.TestCase):
    """valid_for_* 的具体判定条件。"""

    def test_radar_spatial_valid_but_requires_extrinsics(self):
        record = schema.new_record(
            source_id="s",
            source_type=schema.SOURCE_TYPE_RADAR,
            session_id="sess",
            local_track_id="00",
            position_type=schema.POSITION_TYPE_RANGE_BEARING,
            frame_id="radar_local_polar",
            position={"range_m": 185.2, "bearing_rad": 0.169, "bearing_reference": "T"},
            state_time_utc="2019-09-30T15:00:54+00:00",
            time_basis=schema.TIME_BASIS_TTM_TOD_ZDA_DATE,
            time_precision=schema.TIME_PRECISION_CENTISECOND,
        )
        validation.assess_validity(record)
        self.assertTrue(record["valid_for_spatial_alignment"])
        self.assertTrue(record["valid_for_time_alignment"])
        missing = record["spatial_alignment_requirements_missing"]
        self.assertIn("own_ship_position", missing)
        self.assertIn("radar_extrinsics", missing)

    def test_time_invalid_without_utc(self):
        record = schema.new_record(
            source_id="s",
            source_type=schema.SOURCE_TYPE_AIS,
            session_id="sess",
            local_track_id="412331720",
            state_time_raw="2025-03-05 09:29:37",
            position_type=schema.POSITION_TYPE_LATLON,
            frame_id="wgs84",
            position={"longitude_deg": 121.4, "latitude_deg": 37.56},
            time_basis=schema.TIME_BASIS_CSV_DATETIME,
            time_precision=schema.TIME_PRECISION_SECOND,
        )
        validation.assess_validity(record)
        self.assertTrue(record["valid_for_spatial_alignment"])
        self.assertFalse(record["valid_for_time_alignment"])

    def test_time_invalid_with_unknown_precision(self):
        record = schema.new_record(
            source_id="s",
            source_type=schema.SOURCE_TYPE_AIS,
            session_id="sess",
            local_track_id="412331720",
            state_time_utc="2025-03-05T01:29:37+00:00",
            position_type=schema.POSITION_TYPE_LATLON,
            frame_id="wgs84",
            position={"longitude_deg": 121.4, "latitude_deg": 37.56},
            time_basis=schema.TIME_BASIS_CSV_DATETIME,
            time_precision=schema.TIME_PRECISION_UNKNOWN,
        )
        validation.assess_validity(record)
        self.assertFalse(record["valid_for_time_alignment"])

    def test_ais_latlon_has_no_missing_requirements(self):
        record = schema.new_record(
            source_id="s",
            source_type=schema.SOURCE_TYPE_AIS,
            session_id="sess",
            local_track_id="412331720",
            position_type=schema.POSITION_TYPE_LATLON,
            frame_id="wgs84",
            position={"longitude_deg": 121.4, "latitude_deg": 37.56},
        )
        validation.assess_validity(record)
        self.assertEqual(record["spatial_alignment_requirements_missing"], [])


class AISCsvAdapterTests(unittest.TestCase):
    def test_field_mapping_and_timezone_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                ["412331720", "121.442", "37.6125", "2025-03-05 09:29:37", "9.5", "266", "215.2", "在航", "182.25", "0", "0.00", "12.247", "9032.54"]
            ]
            path = os.path.join(tmp, "AIS_Trajectory_412331720.csv")
            write_ais_csv(path, rows)
            summary = standardize(path, ais_config(), os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 1)
            self.assertEqual(summary["rejected_count"], 0)
            records = read_jsonl(os.path.join(tmp, "out", "standard_records.jsonl"))
            record = records[0]
            self.assertEqual(record["local_track_id"], "412331720")
            self.assertEqual(record["state_time_raw"], "2025-03-05 09:29:37")
            self.assertIsNone(record["state_time_utc"])
            self.assertIn(schema.ISSUE_TIMEZONE_UNKNOWN, record["issues"])
            self.assertFalse(record["valid_for_time_alignment"])
            self.assertTrue(record["valid_for_spatial_alignment"])
            self.assertAlmostEqual(record["speed_mps"], 9.5 * 1852.0 / 3600.0, places=6)
            self.assertAlmostEqual(record["course_rad"], math.radians(215.2), places=9)
            self.assertAlmostEqual(record["heading_rad"], math.radians(266.0), places=9)
            self.assertEqual(record["navigation_status"], "在航")
            self.assertEqual(record["speed_unit_source"], "csv_header_declared")
            self.assertAlmostEqual(record["bearing_deg_unverified"], 12.247, places=6)
            self.assertIsNone(record["position"].get("range_m"))

    def test_out_of_range_latitude_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                ["412331720", "121.442", "999.0", "2025-03-05 09:29:37", "9.5", "266", "215.2", "在航", "182.25", "0", "0.00", "12.247", "9032.54"]
            ]
            path = os.path.join(tmp, "AIS_Trajectory_412331720.csv")
            write_ais_csv(path, rows)
            summary = standardize(path, ais_config(), os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 0)
            self.assertEqual(summary["rejected_count"], 1)
            rejected = read_jsonl(os.path.join(tmp, "out", "rejected_records.jsonl"))[0]
            self.assertEqual(rejected["reason"], "latitude_out_of_range")
            self.assertEqual(rejected["raw_record"]["纬度(°)"], "999.0")

    def test_static_info_joined_by_mmsi(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                ["412752690", "121.419", "37.5692", "2025-03-05 09:30:51", "0", "354", "0", "锚泊", "182.01", "0", "0.00", "359.209", "4023.36"]
            ]
            path = os.path.join(tmp, "AIS_Trajectory_412752690.csv")
            write_ais_csv(path, rows)
            static_path = os.path.join(tmp, "AIS_ShipInformation_412752690.csv")
            with open(static_path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write("时间,MMSI,船名,国籍,类型,船长(m),船宽(m),定位装置,目的地\r\n")
                handle.write(
                    "2025-03-05 09:30:49,412752690,EN JI 5,中国,杂货船,97,16,GPS,YANTAI\r\n"
                )
            standardize(path, ais_config(static_info_dir=tmp), os.path.join(tmp, "out"))
            records = read_jsonl(os.path.join(tmp, "out", "standard_records.jsonl"))
            record = records[0]
            self.assertEqual(record["static_info"]["vessel_name"], "EN JI 5")
            self.assertEqual(record["static_info"]["vessel_type"], "杂货船")
            self.assertAlmostEqual(record["static_info"]["length_m"], 97.0)
            self.assertNotIn(schema.ISSUE_STATIC_INFO_MISSING, record["issues"])

    def test_duplicate_flagged_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = ["412331720", "121.442", "37.6125", "2025-03-05 09:29:37", "9.5", "266", "215.2", "在航", "182.25", "0", "0.00", "12.247", "9032.54"]
            path = os.path.join(tmp, "AIS_Trajectory_412331720.csv")
            write_ais_csv(path, [row, list(row)])
            standardize(path, ais_config(), os.path.join(tmp, "out"))
            records = read_jsonl(os.path.join(tmp, "out", "standard_records.jsonl"))
            self.assertEqual(len(records), 2)
            self.assertIn(schema.ISSUE_DUPLICATE, records[1]["issues"])


class RealDataTests(unittest.TestCase):
    """对真实数据运行，核对数量与关键字段。"""

    def test_radar_real_data(self):
        ttm_csv = os.path.join(RADAR_DIR, "radar_ttm_with_capture_time.csv")
        all_csv = os.path.join(RADAR_DIR, "all_incoming_nmea_with_capture_time.csv")
        if not os.path.exists(ttm_csv):
            self.skipTest("雷达数据未就绪")
        config = radar_config(
            source_id="radarpwn_radar_rostock_benign_03",
            session_id="radarpwn-radarrostock-benign-03-20220603163049",
            data_root=RADAR_DIR,
            all_incoming_csv=all_csv,
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary = standardize(ttm_csv, config, os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 383)
            self.assertEqual(summary["rejected_count"], 0)
            self.assertEqual(summary["total_count"], 383)
            self.assertEqual(summary["track_ids"], ["00"])
            records = read_jsonl(os.path.join(tmp, "out", "standard_records.jsonl"))
            first = records[0]
            self.assertAlmostEqual(first["position"]["range_m"], 185.2, places=3)
            self.assertAlmostEqual(first["position"]["bearing_rad"], 0.169296937, places=9)
            self.assertAlmostEqual(first["speed_mps"], 2.057778, places=6)
            self.assertAlmostEqual(first["course_rad"], 0.047123890, places=9)
            self.assertEqual(first["tracking_status"], "tracking")
            self.assertEqual(first["unit_source"], "ttm_field_10_speed_distance_units")
            self.assertEqual(first["state_time_utc"], "2019-09-30T15:00:54+00:00")
            self.assertTrue(first["receive_time_utc"].startswith("2022-06-03T16:31:48"))
            self.assertTrue(all(r["raw_record"].startswith("$RATTM") for r in records))
            # 每条记录都带 ZDA 关联信息
            self.assertTrue(all(r["zda_association"]["association"] == "ok" for r in records))
            self.assertTrue(all(r["zda_association"]["zda_csv_row"] is not None for r in records))

    def test_ais_real_data(self):
        if not os.path.isdir(AIS_DIR):
            self.skipTest("AIS 数据未就绪")
        config = ais_config(
            source_id="mtdsp_ais_20250305103638",
            session_id="mtdsp-20250305103638",
            data_root=AIS_DIR,
            static_info_dir=AIS_DIR,
            position_quantization={
                "longitude": {
                    "resolution_deg": 0.001,
                    "basis": "verified_csv_decimal_places",
                    "confident": True,
                },
                "latitude": {
                    "resolution_deg": 0.0001,
                    "basis": "verified_csv_decimal_places",
                    "confident": True,
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            summary = standardize(AIS_DIR, config, os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 260)
            self.assertEqual(summary["rejected_count"], 0)
            self.assertEqual(summary["total_count"], 260)
            self.assertEqual(summary["track_count"], 6)
            self.assertEqual(summary["valid_for_time_alignment"], 0)
            records = read_jsonl(os.path.join(tmp, "out", "standard_records.jsonl"))
            self.assertTrue(all(r["state_time_utc"] is None for r in records))
            self.assertTrue(all(r["receive_time_utc"] is None for r in records))
            self.assertEqual(records[0]["state_time_raw"], "2025-03-05 10:35:23")
            self.assertEqual(records[0]["position"]["longitude_deg"], 121.392)
            self.assertEqual(records[0]["position"]["latitude_deg"], 37.5649)
            self.assertEqual(records[0]["speed_raw"], 12.0)
            self.assertEqual(records[0]["raw_record"]["MMSI"], "412283000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
