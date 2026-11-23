"""MTDSP 雷达帧头（IF/IQ）航向角与速度解码的单元测试。

覆盖：
  - float32 次正规数编码的**可逆性**（CSV 字符串 ↔ 帧头 uint32）；
  - 唯一匹配偏移的穷举验证（若 .dat 可用）；
  - 航向角标度 0.1°/单位 与速度标度 1 mm/s/单位；
  - 空值、非法值、正常量级浮点数的拒绝；
  - 逆向编码与正向解码的一致性。

不依赖 .dat 二进制文件的测试在无二进制时自动跳过。
"""

from __future__ import annotations

import csv
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization.radar_if_header import (  # noqa: E402
    FLOAT32_SUBNORMAL_SCALE,
    HEADING_DEG_PER_RAW_UNIT,
    SPEED_MPS_PER_RAW_UNIT,
    decode_if_uint32,
    encode_if_uint32,
    is_valid_if_encoded_value,
    parse_if_heading_deg,
    parse_if_heading_raw,
    parse_if_speed_mps,
    parse_if_speed_nmh,
    parse_if_speed_raw,
)

DATA_DIR = os.path.join(ROOT, "data", "mtdsp")
DATA_DIR2 = os.path.join(ROOT, "data", "mtdsp2")
IF_CSV = os.path.join(DATA_DIR, "IF_2003_AT_413203610.csv")
IF_CSV2 = os.path.join(DATA_DIR2, "IF_2003_AT_413203610.csv")

FRAME_SIZE = 3664
OFFSET_HEADING = 20
OFFSET_SPEED = 60


class TestSubnormalEncoding(unittest.TestCase):
    """次正规数编码的基本性质。"""

    def test_scale_is_min_subnormal(self):
        self.assertEqual(FLOAT32_SUBNORMAL_SCALE, 2.0 ** -149)
        self.assertAlmostEqual(FLOAT32_SUBNORMAL_SCALE, 1.401298464324817e-45, places=30)

    def test_known_csv_strings_decode_to_expected_raw(self):
        """CSV 中真实出现过的字符串 → 已知的原始整数。"""
        cases = [
            ("4.943781E-42", 3528),   # 航向角
            ("4.203895E-45", 3),      # 速度
            ("1.261169E-44", 9),      # 速度
            ("4.932571E-42", 3520),
            ("3.643376E-44", 26),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(decode_if_uint32(text), expected)

    def test_roundtrip_all_plausible_raw_values(self):
        """对 0..2^16 的每一个原始整数，正向编码后都能被精确解回。"""
        for raw in range(0, 1 << 16):
            encoded = encode_if_uint32(raw)
            text = "%.7G" % encoded
            self.assertEqual(decode_if_uint32(text), raw, msg=f"raw={raw} text={text}")

    def test_roundtrip_uses_seven_significant_digits(self):
        """导出软件用 7 位有效数字打印，解码必须容忍该截断。"""
        for raw in (0, 1, 9, 26, 3520, 3528, 3539, 40000, 65535):
            encoded = encode_if_uint32(raw)
            self.assertEqual(decode_if_uint32("%.7G" % encoded), raw)
            self.assertEqual(decode_if_uint32("%.6E" % encoded), raw)

    def test_zero(self):
        self.assertEqual(decode_if_uint32("0"), 0)
        self.assertEqual(decode_if_uint32("0.000000E+00"), 0)
        self.assertEqual(parse_if_heading_deg("0"), 0.0)


class TestRejection(unittest.TestCase):
    """非本编码的值必须被拒绝，而不是被静默解释成别的数。"""

    def test_empty_and_none(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(decode_if_uint32(value))
                self.assertIsNone(parse_if_heading_deg(value))
                self.assertIsNone(parse_if_speed_mps(value))

    def test_non_numeric(self):
        for value in ("abc", "--", "NaN", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                self.assertIsNone(decode_if_uint32(value))

    def test_normal_magnitude_float_is_rejected(self):
        """正常量级的浮点数不是本编码，必须拒绝（避免把真实浮点读成乱码）。"""
        for value in ("352.8", "3.5", "1.0", "1e10", "0.001"):
            with self.subTest(value=value):
                self.assertIsNone(decode_if_uint32(value))
                self.assertFalse(is_valid_if_encoded_value(value))

    def test_negative_is_rejected(self):
        self.assertIsNone(decode_if_uint32("-4.943781E-42"))


class TestScaling(unittest.TestCase):
    """物理标度。"""

    def test_heading_scale_is_tenth_of_a_degree(self):
        self.assertEqual(HEADING_DEG_PER_RAW_UNIT, 0.1)
        self.assertAlmostEqual(parse_if_heading_deg("4.943781E-42"), 352.8, places=6)
        self.assertAlmostEqual(parse_if_heading_deg("4.932571E-42"), 352.0, places=6)
        self.assertAlmostEqual(parse_if_heading_deg("4.959195E-42"), 353.9, places=6)

    def test_speed_scale_is_one_millimetre_per_second(self):
        self.assertEqual(SPEED_MPS_PER_RAW_UNIT, 1e-3)
        self.assertAlmostEqual(parse_if_speed_mps("4.203895E-45"), 0.003, places=9)
        self.assertAlmostEqual(parse_if_speed_mps("1.261169E-44"), 0.009, places=9)

    def test_speed_nmh_conversion(self):
        mps = parse_if_speed_mps("1.261169E-44")
        nmh = parse_if_speed_nmh("1.261169E-44")
        self.assertAlmostEqual(nmh, mps * 3600.0 / 1852.0, places=12)
        # 0.009 m/s = 0.017495 nm/h （1 nm/h = 0.5144 m/s，故 nm/h 数值更小）
        self.assertAlmostEqual(nmh, 0.017495, places=6)

    def test_raw_accessors(self):
        self.assertEqual(parse_if_heading_raw("4.943781E-42"), 3528)
        self.assertEqual(parse_if_speed_raw("4.203895E-45"), 3)


class TestRealCsv(unittest.TestCase):
    """对真实 IF CSV 的解码。"""

    def _rows(self, path):
        if not os.path.exists(path):
            self.skipTest(f"缺少数据文件: {path}")
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_decoded_value_ranges(self):
        """解码后的取值范围应落在已核验的物理范围内。"""
        for path, tag in ((IF_CSV, "03-05"), (IF_CSV2, "03-12")):
            rows = self._rows(path)
            headings = [
                parse_if_heading_deg(r["航向角(°)"])
                for r in rows
                if (r.get("航向角(°)") or "").strip()
            ]
            speeds = [
                parse_if_speed_mps(r["速度(nm/h)"])
                for r in rows
                if (r.get("速度(nm/h)") or "").strip()
            ]
            with self.subTest(tag=tag):
                self.assertTrue(headings)
                self.assertTrue(speeds)
                self.assertTrue(all(h is not None for h in headings))
                self.assertTrue(all(s is not None for s in speeds))
                # 航向角：北偏西约 352~354°
                self.assertTrue(all(350.0 <= h <= 355.0 for h in headings))
                # 速度：0 ~ 26 mm/s
                self.assertTrue(all(0.0 <= s <= 0.026 + 1e-12 for s in speeds))
                self.assertLessEqual(max(speeds), 0.026 + 1e-12)

    def test_heading_values_are_quantized_to_tenth_degree(self):
        rows = self._rows(IF_CSV)
        raws = {
            parse_if_heading_raw(r["航向角(°)"])
            for r in rows
            if (r.get("航向角(°)") or "").strip()
        }
        self.assertTrue(raws)
        # 原始整数必须落在已核验的 3520..3539
        self.assertTrue(all(3520 <= v <= 3539 for v in raws))

    def test_speed_values_are_small_integers(self):
        rows = self._rows(IF_CSV)
        raws = {
            parse_if_speed_raw(r["速度(nm/h)"])
            for r in rows
            if (r.get("速度(nm/h)") or "").strip()
        }
        self.assertTrue(raws)
        self.assertTrue(all(0 <= v <= 26 for v in raws))


class TestBinaryParity(unittest.TestCase):
    """与雷达二进制帧头逐位比对（需要 .dat 文件）。"""

    DAT_CANDIDATES = [
        ("/tmp/mtdsp_raw/IF_2003_AT_413203610.dat", IF_CSV),
        ("/tmp/mtdsp_raw2/IF_2003_AT_413203610.dat", IF_CSV2),
    ]

    def _find(self):
        for dat, csvp in self.DAT_CANDIDATES:
            if os.path.exists(dat) and os.path.exists(csvp):
                return dat, csvp
        self.skipTest("未找到 IF_*.dat 二进制文件，跳过逐位比对")

    def test_exact_bitwise_parity(self):
        dat, csvp = self._find()
        with open(dat, "rb") as handle:
            data = handle.read()
        frames = len(data) // FRAME_SIZE
        with open(csvp, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), frames)

        def u32(offset, index):
            start = FRAME_SIZE * index + offset
            return struct.unpack("<I", data[start:start + 4])[0]

        heading_checked = speed_checked = 0
        for index, row in enumerate(rows):
            text = (row.get("航向角(°)") or "").strip()
            if text:
                self.assertEqual(decode_if_uint32(text), u32(OFFSET_HEADING, index))
                heading_checked += 1
            text = (row.get("速度(nm/h)") or "").strip()
            if text:
                self.assertEqual(decode_if_uint32(text), u32(OFFSET_SPEED, index))
                speed_checked += 1
        self.assertGreater(heading_checked, 0)
        self.assertGreater(speed_checked, 0)

    def test_offset_is_unique(self):
        """穷举全部 uint32 偏移：只有 +20 与 +60 满足恒等式。"""
        dat, csvp = self._find()
        with open(dat, "rb") as handle:
            data = handle.read()
        frames = len(data) // FRAME_SIZE
        with open(csvp, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))

        for column, expected_offset in (("航向角(°)", OFFSET_HEADING), ("速度(nm/h)", OFFSET_SPEED)):
            indexes = [
                i for i, r in enumerate(rows)
                if (r.get(column) or "").strip()
            ][:2000]
            if not indexes:
                continue
            wants = [decode_if_uint32(rows[i][column]) for i in indexes]
            matching = []
            for offset in range(0, FRAME_SIZE - 3):
                ok = all(
                    struct.unpack(
                        "<I", data[FRAME_SIZE * i + offset:FRAME_SIZE * i + offset + 4]
                    )[0] == want
                    for i, want in zip(indexes, wants)
                )
                if ok:
                    matching.append(offset)
            with self.subTest(column=column):
                self.assertEqual(matching, [expected_offset])


if __name__ == "__main__":
    unittest.main()
