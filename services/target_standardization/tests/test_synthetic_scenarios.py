"""编号复用与时区确认场景测试。

这两项在真实数据中不存在（雷达只有一个编号 00 且全部为 T；AIS 未确认时区），
因此单独构造并明确标注为测试数据。
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


def nmea_checksum(body: str) -> str:
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def make_ttm(target, tod, status="T"):
    body = f"$RATTM,{target},0.1,9.7,T,4.0,2.7,T,0.0,0.3,N,TGT{target},{status},,{tod},A"
    return f"{body}*{nmea_checksum(body)}"


RADAR_CONFIG = {
    "source_id": "test_radar",
    "source_type": "radar",
    "session_id": "session-A",
    "frame_id": "radar_local_polar",
    "speed_unit": "K",
    "distance_unit": "N",
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
        "distance_unit": 9,
        "target_name": 10,
        "tracking_status": 11,
        "status_reason": 12,
        "time_of_day": 13,
        "acquisition_type": 14,
    },
}

AIS_CONFIG = {
    "source_id": "test_ais",
    "source_type": "ais",
    "session_id": "ais-session",
    "frame_id": "wgs84",
    "speed_unit": "K",
    "timezone_known": False,
    "field_mapping": {
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
    },
}


class TrackIdReuseTests(unittest.TestCase):
    """同一编号在不同采集批次中复用时，靠 session_id 区分。"""

    def _write_csv(self, path, sentences):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("capture_time_unix,capture_time_utc,sentence_type,checksum_valid,sentence\n")
            for index, sentence in enumerate(sentences):
                handle.write(
                    f"1654273908.9{index},2022-06-03T16:31:48.95{index}000+00:00,TTM,True,\"{sentence}\"\n"
                )

    def test_same_number_different_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "ttm.csv")
            self._write_csv(csv_path, [make_ttm("00", "150054.00"), make_ttm("00", "150057.00")])

            config_a = dict(RADAR_CONFIG, session_id="session-A")
            config_b = dict(RADAR_CONFIG, session_id="session-B")

            out_a = os.path.join(tmp, "a")
            out_b = os.path.join(tmp, "b")
            standardize(csv_path, config_a, out_a)
            standardize(csv_path, config_b, out_b)

            rec_a = json.loads(open(os.path.join(out_a, "standard_records.jsonl"), encoding="utf-8").readline())
            rec_b = json.loads(open(os.path.join(out_b, "standard_records.jsonl"), encoding="utf-8").readline())

            self.assertEqual(rec_a["local_track_id"], "00")
            self.assertEqual(rec_b["local_track_id"], "00")
            self.assertNotEqual(rec_a["session_id"], rec_b["session_id"])
            # 编号相同但 session 不同 → record_id 必须不同
            self.assertNotEqual(rec_a["record_id"], rec_b["record_id"])

    def test_record_id_stable_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "ttm.csv")
            self._write_csv(csv_path, [make_ttm("00", "150054.00")])
            out1 = os.path.join(tmp, "1")
            out2 = os.path.join(tmp, "2")
            standardize(csv_path, RADAR_CONFIG, out1)
            standardize(csv_path, RADAR_CONFIG, out2)
            r1 = json.loads(open(os.path.join(out1, "standard_records.jsonl"), encoding="utf-8").readline())
            r2 = json.loads(open(os.path.join(out2, "standard_records.jsonl"), encoding="utf-8").readline())
            self.assertEqual(r1["record_id"], r2["record_id"])


class TimezoneConfirmedTests(unittest.TestCase):
    """时区一旦确认，state_time_utc 才填写。"""

    def _write_csv(self, path):
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(
                "MMSI,经度(°),纬度(°),时间,速度(kn),船艏向(°),对地航向(°),航行状态,相对视角(°),姿态信息,浪高(m),方位角(°),距离(m)\r\n"
            )
            handle.write(
                "412331720,121.442,37.6125,2025-03-05 09:29:37,9.5,266,215.2,在航,182.25,0,0.00,12.247,9032.54\r\n"
            )

    def test_utc_filled_when_timezone_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "AIS_Trajectory_412331720.csv")
            self._write_csv(csv_path)
            config = dict(AIS_CONFIG, timezone_known=True, timezone_name="Asia/Shanghai")
            summary = standardize(csv_path, config, os.path.join(tmp, "out"))
            self.assertEqual(summary["output_count"], 1)
            record = json.loads(
                open(os.path.join(tmp, "out", "standard_records.jsonl"), encoding="utf-8").readline()
            )
            self.assertEqual(record["state_time_utc"], "2025-03-05T01:29:37+00:00")
            self.assertEqual(record["time_domain"], schema.TIME_DOMAIN_UTC)
            self.assertTrue(record["valid_for_time_alignment"])
            self.assertNotIn(schema.ISSUE_TIMEZONE_UNKNOWN, record["issues"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
