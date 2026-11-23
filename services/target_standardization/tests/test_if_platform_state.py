"""雷达帧头（IF/IQ）平台自身状态接入对齐模块的单元测试。

覆盖：
  - IF CSV 的帧分组读取（航向角/速度只在部分行填写）；
  - `.dat` 二进制补齐 CSV 缺失的航向角/速度；
  - 帧号 / 时间双通道关联（帧号优先、时间兜底）；
  - 平台状态**不参与**方位→ENU 换算（回归保护）；
  - 未配置 `platform_state` 时既有行为完全不变。

依赖真实数据的测试在数据缺失时自动跳过。
"""

from __future__ import annotations

import csv
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment.if_platform_state import (  # noqa: E402
    IF_OFFSET_HEADING,
    IF_OFFSET_SPEED,
    IF_RECORD_SIZE,
    IfPlatformStateIndex,
    read_if_dat_records,
)
from alignment.spatial import SpatialAligner  # noqa: E402
from alignment.config import AlignmentConfig  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "mtdsp")
DATA_DIR2 = os.path.join(ROOT, "data", "mtdsp2")
IF_CSV = os.path.join(DATA_DIR, "IF_2003_AT_413203610.csv")
IF_CSV2 = os.path.join(DATA_DIR2, "IF_2003_AT_413203610.csv")

# 03-12 的 .dat 在工程目录之外（原始数据集解压位置）
DAT12 = "/root/data_mtdsp2/IF_2003_AT_413203610.dat"

HEADER = (
    "帧序号,脉冲序号,时间,航向角(°),方位码(0-449999),速度(nm/h),"
    "纬度(北纬)(°),经度(东经)(°)\n"
)

# 帧 1：航向与速度齐全（值取自真实数据）
ROW_FRAME1 = (
    "1,1,2025-03-05 10:35:35,4.943781E-42,3209,4.203895E-45,"
    "37.53297,121.4202\n"
    "1,2,,,3299,,\n"
)
# 帧 2：航向角缺失（模拟 03-12 包的情形）
ROW_FRAME2 = (
    "2,3,2025-03-05 10:35:38,,3174,,\n"
    "2,4,,,3264,,\n"
)


def _write_csv(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(HEADER)
        handle.write(body)
    return path


def _write_dat(path, records):
    """写出一个最小 `.dat`：每条记录只填航向角与速度两个 uint32 字段。"""
    with open(path, "wb") as handle:
        for heading, speed in records:
            chunk = bytearray(IF_RECORD_SIZE)
            struct.pack_into("<I", chunk, IF_OFFSET_HEADING, heading)
            struct.pack_into("<I", chunk, IF_OFFSET_SPEED, speed)
            handle.write(bytes(chunk))
    return path


class TestIfFrameReading(unittest.TestCase):
    def test_reads_frames_and_decodes_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1 + ROW_FRAME2)
            index = IfPlatformStateIndex.from_file(path)
            self.assertEqual(len(index), 2)
            f1 = index.by_frame_index(1)
            self.assertIsNotNone(f1)
            self.assertAlmostEqual(f1.heading_deg, 352.8, places=6)
            self.assertAlmostEqual(f1.speed_mps, 0.003, places=9)
            self.assertEqual(f1.state_time_raw, "2025-03-05 10:35:35")
            self.assertEqual(f1.pulse_count, 2)

    def test_frame_index_accepts_float_representation(self):
        """标准化层把帧号存成浮点数（37.0），必须与字符串 '37' 命中同一帧。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1)
            index = IfPlatformStateIndex.from_file(path)
            self.assertIsNotNone(index.by_frame_index(1.0))
            self.assertIsNotNone(index.by_frame_index("1"))
            self.assertIsNotNone(index.by_frame_index(1))

    def test_dat_recovers_missing_heading(self):
        """.dat 补齐 CSV 中缺失的航向角与速度。"""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _write_csv(
                os.path.join(tmp, "IF_x.csv"), ROW_FRAME1 + ROW_FRAME2
            )
            # 记录 0..3：帧 1 用 3528/3，帧 2 用 3531/9
            dat_path = _write_dat(
                os.path.join(tmp, "IF_x.dat"),
                [(3528, 3), (3528, 3), (3531, 9), (3531, 9)],
            )
            index = IfPlatformStateIndex.from_file(csv_path, dat_path=dat_path)
            f1 = index.by_frame_index(1)
            f2 = index.by_frame_index(2)
            self.assertAlmostEqual(f1.heading_deg, 352.8, places=6)
            # 帧 2 的 CSV 没有航向角，但 .dat 有
            self.assertAlmostEqual(f2.heading_deg, 353.1, places=6)
            self.assertAlmostEqual(f2.speed_mps, 0.009, places=9)

    def test_csv_value_takes_precedence_over_dat(self):
        """CSV 有值时以 CSV 为准（两者本就逐位相等）。"""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1)
            dat_path = _write_dat(
                os.path.join(tmp, "IF_x.dat"), [(3528, 3), (3528, 3)]
            )
            index = IfPlatformStateIndex.from_file(csv_path, dat_path=dat_path)
            self.assertAlmostEqual(index.by_frame_index(1).heading_deg, 352.8, places=6)


class TestMatching(unittest.TestCase):
    def _index(self, tmp):
        path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1 + ROW_FRAME2)
        return IfPlatformStateIndex.from_file(path)

    def test_frame_index_match_takes_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            match = index.lookup(frame_index=2, state_time_raw="2025-03-05 10:35:38")
            self.assertTrue(match.matched)
            self.assertEqual(match.match_basis, "frame_index")

    def test_time_fallback_when_frame_index_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            match = index.lookup(
                frame_index=999, state_time_raw="2025-03-05 10:35:39"
            )
            self.assertTrue(match.matched)
            self.assertEqual(match.match_basis, "time")
            self.assertEqual(match.frame.frame_index, "2")

    def test_time_fallback_respects_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = IfPlatformStateIndex.from_file(
                _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1 + ROW_FRAME2),
                match_tolerance_s=1.0,
            )
            match = index.lookup(
                frame_index=999, state_time_raw="2025-03-05 10:35:48"
            )
            self.assertFalse(match.matched)

    def test_no_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = self._index(tmp)
            match = index.lookup(frame_index=None, state_time_raw=None)
            self.assertFalse(match.matched)
            self.assertIsNone(match.match_basis)


class TestSpatialIntegration(unittest.TestCase):
    """平台状态必须**只作附加记录**，绝不改变方位→ENU 的结果。"""

    def _record(self, frame_index=1, state_time_raw="2025-03-05 10:35:35"):
        return {
            "source_id": "radar_test",
            "source_type": "radar_cutdata",
            "local_track_id": "413203610",
            "state_time_raw": state_time_raw,
            "radar_frame_index": frame_index,
            "position": {"range_m": 3875.0, "bearing_rad": 0.1, "bearing_reference": "T"},
        }

    def _config(self, platform_state=None):
        data = {
            "site_frame": {
                "origin_latitude_deg": 37.53297,
                "origin_longitude_deg": 121.41990,
            },
            "radar_station": {
                "station_latitude_deg": 37.53297,
                "station_longitude_deg": 121.41990,
            },
        }
        if platform_state is not None:
            data["platform_state"] = platform_state
        return AlignmentConfig.from_dict(data)

    def test_without_config_no_platform_fields(self):
        """未配置时记录上不出现平台字段，既有行为不变。"""
        aligner = SpatialAligner(self._config())
        record = self._record()
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "ok")
        self.assertNotIn("platform_heading_deg", record)

    def test_platform_state_attached_and_bearing_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1 + ROW_FRAME2)
            aligner = SpatialAligner(
                self._config(
                    {
                        "if_directory": tmp,
                        "if_filename": "IF_x.csv",
                        "match_tolerance_s": 10.0,
                    }
                )
            )
            record = self._record(frame_index=1)
            aligner.align_record(record)
            self.assertEqual(record["alignment_status"], "ok")
            self.assertAlmostEqual(record["platform_heading_deg"], 352.8, places=6)
            self.assertAlmostEqual(record["platform_speed_mps"], 0.003, places=9)
            self.assertEqual(record["platform_state_match_basis"], "frame_index")

            # 关键：ENU 结果与不接入时完全一致
            baseline = self._record(frame_index=1)
            SpatialAligner(self._config()).align_record(baseline)
            self.assertAlmostEqual(record["east_m"], baseline["east_m"], places=9)
            self.assertAlmostEqual(record["north_m"], baseline["north_m"], places=9)

    def test_platform_state_not_applied_to_bearing(self):
        """平台航向不参与方位换算：bearing_reference_used 仍是 T。"""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _write_csv(os.path.join(tmp, "IF_x.csv"), ROW_FRAME1)
            aligner = SpatialAligner(
                self._config(
                    {
                        "if_directory": tmp,
                        "if_filename": "IF_x.csv",
                    }
                )
            )
            record = self._record(frame_index=1)
            aligner.align_record(record)
            self.assertEqual(record["alignment_bearing_reference_used"], "T")
            self.assertFalse(record["alignment_install_heading_applied"])


class TestRealData(unittest.TestCase):
    """基于真实 MTDSP 数据的验证（数据缺失时跳过）。"""

    def test_03_05_frames_have_heading(self):
        if not os.path.exists(IF_CSV):
            self.skipTest("缺少 03-05 IF CSV")
        index = IfPlatformStateIndex.from_directory(DATA_DIR, "IF_2003_AT_413203610.csv")
        self.assertTrue(index.available)
        self.assertGreaterEqual(len(index), 127)
        headings = [f.heading_deg for f in index.frames if f.heading_deg is not None]
        self.assertGreater(len(headings), 100)
        self.assertAlmostEqual(min(headings), 352.0, places=3)
        self.assertAlmostEqual(max(headings), 353.9, places=3)

    def test_03_12_dat_recovers_missing_headings(self):
        """03-12 的 CSV 只有 6 帧有航向角，.dat 可补齐全部帧。"""
        if not (os.path.exists(IF_CSV2) and os.path.exists(DAT12)):
            self.skipTest("缺少 03-12 IF CSV 或 .dat")
        csv_only = IfPlatformStateIndex.from_directory(
            DATA_DIR2, "IF_2003_AT_413203610.csv"
        )
        with_dat = IfPlatformStateIndex.from_directory(
            DATA_DIR2, "IF_2003_AT_413203610.csv", dat_path=DAT12
        )
        csv_count = sum(1 for f in csv_only.frames if f.heading_deg is not None)
        dat_count = sum(1 for f in with_dat.frames if f.heading_deg is not None)
        self.assertLess(csv_count, len(csv_only.frames))
        self.assertEqual(dat_count, len(with_dat.frames))
        for frame in with_dat.frames:
            self.assertIsNotNone(frame.heading_deg)
            # 03-12 的 .dat 航向 raw 范围 3530..3539 → 353.0°..353.9°
            self.assertGreaterEqual(frame.heading_deg, 353.0 - 1e-6)
            self.assertLessEqual(frame.heading_deg, 353.9 + 1e-6)

    def test_dat_records_match_csv_where_present(self):
        """CSV 有值的行必须与 .dat 逐位相等。"""
        if not (os.path.exists(IF_CSV2) and os.path.exists(DAT12)):
            self.skipTest("缺少 03-12 IF CSV 或 .dat")
        records = read_if_dat_records(DAT12)
        self.assertGreater(len(records), 0)
        checked = 0
        with open(IF_CSV2, "r", encoding="utf-8-sig", newline="") as handle:
            for row_index, row in enumerate(csv.DictReader(handle)):
                if row_index >= len(records):
                    break
                value = (row.get("航向角(°)") or "").strip()
                if not value:
                    continue
                raw = struct.unpack("<I", struct.pack("<f", float(value)))[0]
                self.assertEqual(raw, records[row_index][0])
                checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
