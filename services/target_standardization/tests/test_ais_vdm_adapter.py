"""RadarPWN 包内 AIS（!AIVDM）适配器测试。

真实数据特征：
- 7719 条 !AIVDM 报文，全部为位置报文（类型 1/2/3），校验和全部通过；
- 解码后 76 个 MMSI；
- 报文 second 为**仿真时钟**的分钟内秒数（0–59），跨分钟回绕；
- 仿真时钟与记录时钟相差约 13 小时 16 分，速率差约 0.28%；
- lat=0 且 lon=0 表示位置不可用（哨兵值），报文本身有效。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization import schema, standardize  # noqa: E402
from standardization import radar_ttm_adapter  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "radarpwn")
VDM_CSV = os.path.join(DATA_DIR, "ais_with_capture_time.csv")
ALL_CSV = os.path.join(DATA_DIR, "all_incoming_nmea_with_capture_time.csv")

VDM_CONFIG = {
    "source_id": "radarpwn_ais_rostock_benign_03",
    "source_type": "ais_vdm",
    "session_id": "radarpwn-radarrostock-benign-03-20220603163049",
    "data_root": DATA_DIR,
    "frame_id": "wgs84",
    "simulation_date_binding": "zda_date",
    # (0,0) 是本数据集的疑似占位值，由来源配置显式声明
    "placeholder_positions": [[0.0, 0.0]],
}


class VdmRealDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(VDM_CSV):
            raise unittest.SkipTest("RadarPWN AIS 数据未就绪")
        cls.tmp = tempfile.TemporaryDirectory()
        config = dict(VDM_CONFIG, all_incoming_csv=ALL_CSV)
        cls.summary = standardize(VDM_CSV, config, os.path.join(cls.tmp.name, "out"))
        with open(os.path.join(cls.tmp.name, "out", "standard_records.jsonl"), encoding="utf-8") as handle:
            cls.records = [json.loads(line) for line in handle if line.strip()]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_messages_processed(self):
        """输入数 = 输出数 + 拒绝数，位置不可用的报文输出并标记而非丢弃。"""
        self.assertEqual(self.summary["output_count"], 7719)
        self.assertEqual(self.summary["rejected_count"], 0)
        self.assertEqual(self.summary["total_count"], 7719)
        self.assertEqual(self.summary["track_count"], 76)

    def test_position_unavailable_flagged_not_dropped(self):
        """(0,0) 由来源配置声明为疑似占位值 → 输出并标记，不丢弃。"""
        unavailable = [r for r in self.records if not r["position_available"]]
        self.assertEqual(len(unavailable), 18)
        for record in unavailable:
            self.assertIn(schema.ISSUE_MISSING_POSITION, record["issues"])
            self.assertFalse(record["valid_for_spatial_alignment"])
            self.assertIsNone(record["position"]["latitude_deg"])
            self.assertEqual(
                record["position_anomaly_reason"], "suspected_placeholder_position"
            )

    def test_placeholder_rule_is_source_configurable(self):
        """通用适配器不把 (0,0) 一律判为不可用；不声明时该点被视为有效位置。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(VDM_CONFIG)
            config.pop("placeholder_positions", None)
            summary = standardize(VDM_CSV, config, os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 7719)
            with open(
                os.path.join(tmp, "out", "standard_records.jsonl"), encoding="utf-8"
            ) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            # 未声明时，(0,0) 被视为普通有效位置
            self.assertTrue(all(r["position_available"] for r in records))
            self.assertTrue(
                all(r["position_anomaly"] is None for r in records)
            )

    def test_ais_standard_unavailable_encoding_is_generic(self):
        """AIS 标准的位置不可用编码（91/181）是通用规则，与来源配置无关。"""
        from standardization.ais_vdm_adapter import RadarPWNAisAdapter

        adapter = RadarPWNAisAdapter(VDM_CONFIG)
        valid, reason = adapter._assess_position(91.0, 181.0)
        self.assertFalse(valid)
        self.assertEqual(reason, "ais_unavailable_encoding")

        valid, reason = adapter._assess_position(54.2, 12.1)
        self.assertTrue(valid)
        self.assertIsNone(reason)

    def test_position_and_motion_fields(self):
        record = self.records[0]
        self.assertEqual(record["local_track_id"], "228050803")
        self.assertAlmostEqual(record["position"]["latitude_deg"], 54.260303, places=6)
        self.assertAlmostEqual(record["position"]["longitude_deg"], 12.048277, places=6)
        self.assertAlmostEqual(record["speed_mps"], 17.2 * 1852.0 / 3600.0, places=6)
        self.assertEqual(record["speed_unit"], "knot")
        self.assertEqual(record["speed_unit_source"], "ais_message_1_speed_over_ground")
        self.assertAlmostEqual(record["course_rad"], 6.003932627, places=8)
        self.assertIsNone(record["motion_reference"])

    def test_simulation_time_from_zda_and_vdm_second(self):
        """仿真 UTC 必须由 ZDA 日期 + 报文秒数还原，且与抓包时间分开保存。"""
        record = self.records[0]
        self.assertEqual(record["state_time_utc"], "2019-09-30T15:00:08+00:00")
        self.assertEqual(record["state_time_raw"], "150008.08")
        self.assertTrue(record["receive_time_utc"].startswith("2022-06-03T16:31:02"))
        self.assertEqual(record["receive_time_source"], "pcap_record_time")
        self.assertTrue(record["valid_for_time_alignment"])

    def test_clock_skew_within_seconds(self):
        """锚点推进与报文秒数的偏差应控制在秒级。"""
        skews = [
            r["zda_association"]["clock_skew_s"]
            for r in self.records
            if r["zda_association"].get("clock_skew_s") is not None
        ]
        self.assertTrue(skews)
        self.assertLessEqual(max(abs(s) for s in skews), 2.0)

    def test_all_records_have_association(self):
        self.assertTrue(
            all(r["zda_association"]["association"] == "ok" for r in self.records)
        )

    def test_raw_record_preserved(self):
        self.assertTrue(self.records[0]["raw_record"].startswith("!AIVDM"))
        self.assertEqual(self.records[0]["source_row"], 1)

    def test_no_utc_without_context(self):
        """没有 ZDA 上下文时不生成仿真 UTC。"""
        with tempfile.TemporaryDirectory() as tmp:
            config = dict(VDM_CONFIG)
            config.pop("all_incoming_csv", None)
            summary = standardize(VDM_CSV, config, os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 7719)
            with open(
                os.path.join(tmp, "out", "standard_records.jsonl"), encoding="utf-8"
            ) as handle:
                records = [json.loads(line) for line in handle if line.strip()]
            self.assertTrue(all(r["state_time_utc"] is None for r in records))
            self.assertTrue(all(r["valid_for_time_alignment"] is False for r in records))


class ClockMappingTests(unittest.TestCase):
    """抓包时钟 → 仿真时钟的映射。"""

    def _context(self):
        entries = [
            {
                "csv_row": 10,
                "time_of_day": "150002.00",
                "seconds_of_day": 54002.0,
                "day": "30",
                "month": "09",
                "year": "2019",
                "capture_time_unix": "1000.0",
            },
            {
                "csv_row": 20,
                "time_of_day": "150102.00",
                "seconds_of_day": 54102.0,
                "day": "30",
                "month": "09",
                "year": "2019",
                "capture_time_unix": "1100.0",
            },
        ]
        ctx = radar_ttm_adapter.time_fields.build_simulation_date_context(entries)
        ctx.entries = entries
        return ctx

    def test_anchor_is_nearest_not_later_zda(self):
        ctx = self._context()
        mapping = ctx.sim_seconds_for_capture(1050.0)
        self.assertEqual(mapping["anchor_zda_csv_row"], 10)
        self.assertAlmostEqual(mapping["sim_seconds_of_day"], 54052.0, places=6)

    def test_resolve_minute_second_picks_closest_candidate(self):
        """报文秒数与锚点推得的秒一致时，还原结果应与推得值相同。"""
        ctx = self._context()
        # 抓包 1050 s → 锚点推得仿真 54052 s（该分钟的第 52 秒）
        mapping = ctx.resolve_minute_second(1050.0, 52)
        self.assertAlmostEqual(mapping["sim_seconds_of_day"], 54052.0, places=6)
        self.assertAlmostEqual(mapping["clock_skew_s"], 0.0, places=6)
        self.assertEqual(mapping["minute_candidates"], [53940.0, 54000.0, 54060.0])

    def test_resolve_minute_second_across_minute_boundary(self):
        """推得值落在分钟边界附近时，用报文秒数选到正确的分钟。"""
        ctx = self._context()
        # 抓包 1057.9 s → 推得 54059.9 s，报文秒数 59 → 本分钟内
        mapping = ctx.resolve_minute_second(1057.9, 59)
        self.assertAlmostEqual(mapping["sim_seconds_of_day"], 54059.0, places=6)
        self.assertAlmostEqual(mapping["clock_skew_s"], 0.9, places=6)

    def test_resolve_minute_second_next_minute(self):
        """推得值刚过分钟边界、报文秒数归零时，应选中下一分钟。"""
        ctx = self._context()
        # 抓包 1058.2 s → 推得 54060.2 s，报文秒数 0 → 54060 + 0
        mapping = ctx.resolve_minute_second(1058.2, 0)
        self.assertAlmostEqual(mapping["sim_seconds_of_day"], 54060.0, places=6)
        self.assertAlmostEqual(mapping["clock_skew_s"], 0.2, places=6)

    def test_resolve_minute_second_boundary_case(self):
        ctx = self._context()
        # 仿真推得 54058 s，报文秒数 58 → 直接落在本分钟内
        mapping = ctx.resolve_minute_second(1056.0, 58)
        self.assertAlmostEqual(mapping["sim_seconds_of_day"], 54058.0, places=6)
        self.assertAlmostEqual(mapping["clock_skew_s"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
