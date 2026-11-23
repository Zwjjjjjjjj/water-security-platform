"""MTDSP 雷达检测适配器（radar_cutdata_adapter）单元测试。

覆盖：
  - 三类目标类型（AT / CDT / RT）的语义分类正确；
  - 极坐标 → 标准记录的字段映射与单位换算；
  - 非法输入被拒绝且给出原因；
  - 时区未确认时不产出 state_time_utc；
  - 检测框语义随目标类型变化（CDT=雷达检测框，其余=光电视场）。
"""

from __future__ import annotations

import csv
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization import schema  # noqa: E402
from standardization.radar_cutdata_adapter import (  # noqa: E402
    TARGET_TYPE_MEASUREMENT_KIND,
    RadarCutDataAdapter,
)

COLUMNS = [
    "目标类型",
    "目标编号",
    "目标方位(°)",
    "目标距离(米)",
    "起始时间(帧)",
    "截止时间(帧)",
    "帧序号",
    "角度修正(°)",
    "距离修正(米)",
    "[外接矩形]起始角度(°)",
    "[外接矩形]结束角度(°)",
    "[外接矩形]起始距离(米)",
    "[外接矩形]结束距离(米)",
    "匹配编号",
    "光电匹配时间",
    "是否为切片目标",
]


def _row(**overrides):
    base = {
        "目标类型": "CDT",
        "目标编号": "900001",
        "目标方位(°)": "352.234",
        "目标距离(米)": "3328.8",
        "起始时间(帧)": "2025-03-05 10:35:58",
        "截止时间(帧)": "2025-03-05 10:36:58",
        "帧序号": "1",
        "角度修正(°)": "344.0",
        "距离修正(米)": "0",
        "[外接矩形]起始角度(°)": "351.0",
        "[外接矩形]结束角度(°)": "353.0",
        "[外接矩形]起始距离(米)": "3300.0",
        "[外接矩形]结束距离(米)": "3350.0",
        "匹配编号": "",
        "光电匹配时间": "",
        "是否为切片目标": "否",
    }
    base.update(overrides)
    return base


def _write_csv(rows):
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8-sig", newline=""
    )
    writer = csv.DictWriter(handle, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    handle.close()
    return handle.name


def _adapter(**overrides):
    config = {
        "source_id": "mtdsp_radar_test",
        "session_id": "mtdsp_test_session",
        "timezone_known": False,
        "station_latitude_deg": 37.53297,
        "station_longitude_deg": 121.42020,
        "install_heading_deg": 344.0,
        "antenna_beamwidth_deg": 1.2,
        "range_resolution_m": 2.5,
    }
    config.update(overrides)
    return RadarCutDataAdapter(config)


class RadarCutDataAdapterTests(unittest.TestCase):
    def _run(self, rows):
        path = _write_csv(rows)
        try:
            return _adapter().run(path)
        finally:
            os.unlink(path)

    # -- 目标类型语义 -------------------------------------------------------

    def test_measurement_kind_per_target_type(self):
        """三类目标的测量来源必须区分开。

        `AT` 行的方位/距离是**继承自 AIS** 的（已逐点核实差值为
        0.000°/0.0 m），绝不能被当作雷达测量精度来评估。
        """
        self.assertEqual(TARGET_TYPE_MEASUREMENT_KIND["AT"], "ais_associated")
        self.assertEqual(TARGET_TYPE_MEASUREMENT_KIND["CDT"], "radar_detected")
        self.assertEqual(TARGET_TYPE_MEASUREMENT_KIND["RT"], "radar_tracked")

        records, _, _ = self._run(
            [
                _row(**{"目标类型": "AT", "目标编号": "413203610"}),
                _row(**{"目标类型": "CDT", "目标编号": "900001"}),
                _row(**{"目标类型": "RT", "目标编号": "1"}),
            ]
        )
        kinds = {r["radar_target_type"]: r["radar_measurement_kind"] for r in records}
        self.assertEqual(kinds["AT"], "ais_associated")
        self.assertEqual(kinds["CDT"], "radar_detected")
        self.assertEqual(kinds["RT"], "radar_tracked")

    def test_source_type_is_radar_for_truth_bucketing(self):
        """source_type 保持 "radar"，才能与真值评测的 sensor 分桶对齐。"""
        records, _, _ = self._run([_row()])
        self.assertEqual(records[0]["source_type"], schema.SOURCE_TYPE_RADAR)
        self.assertEqual(records[0]["radar_source_format"], "mtdsp_cutdata")

    # -- 字段映射 -----------------------------------------------------------

    def test_polar_position_mapping(self):
        """极坐标 → 标准记录：单位与参考基准正确。"""
        records, _, _ = self._run([_row()])
        record = records[0]
        self.assertEqual(record["position_type"], schema.POSITION_TYPE_RANGE_BEARING)
        position = record["position"]
        self.assertAlmostEqual(position["range_m"], 3328.8, places=6)
        self.assertAlmostEqual(
            position["bearing_rad"], math.radians(352.234), places=9
        )
        # 真方位参考（不是 M/R）
        self.assertEqual(position["bearing_reference"], schema.REFERENCE_TRUE)
        # 角度修正**不叠加**到方位上（叠加会使站址反算残差从 14 m 恶化到 400 m）
        self.assertFalse(record["radar_angle_correction_applied"])
        self.assertAlmostEqual(record["radar_bearing_deg"], 352.234, places=6)

    def test_station_and_install_heading_recorded_not_applied(self):
        """站址与天线安装方位只记录，不参与换算。"""
        records, _, _ = self._run([_row()])
        record = records[0]
        self.assertAlmostEqual(record["radar_station_latitude_deg"], 37.53297)
        self.assertAlmostEqual(record["radar_station_longitude_deg"], 121.42020)
        self.assertAlmostEqual(record["radar_install_heading_deg"], 344.0)
        self.assertAlmostEqual(record["radar_antenna_beamwidth_deg"], 1.2)
        self.assertAlmostEqual(record["radar_range_resolution_m"], 2.5)

    def test_detection_box_kind_depends_on_target_type(self):
        """CDT 的外接矩形是雷达检测框；AT/RT 的是光电视场范围。"""
        records, _, _ = self._run(
            [
                _row(**{"目标类型": "CDT"}),
                _row(**{"目标类型": "AT", "目标编号": "413203610"}),
            ]
        )
        by_type = {r["radar_target_type"]: r for r in records}
        self.assertEqual(
            by_type["CDT"]["radar_detection_box"]["kind"], "radar_detection"
        )
        self.assertEqual(
            by_type["AT"]["radar_detection_box"]["kind"], "optical_field_of_view"
        )
        # 框的四角应被保留
        box = by_type["CDT"]["radar_detection_box"]
        self.assertAlmostEqual(box["bearing_min_deg"], 351.0)
        self.assertAlmostEqual(box["range_max_m"], 3350.0)

    def test_tracking_status_by_target_type(self):
        """AT/RT 是跟踪态，CDT 是单次检测。"""
        records, _, _ = self._run(
            [
                _row(**{"目标类型": "AT"}),
                _row(**{"目标类型": "CDT"}),
                _row(**{"目标类型": "RT"}),
            ]
        )
        by_type = {r["radar_target_type"]: r for r in records}
        self.assertEqual(
            by_type["AT"]["tracking_status"], schema.TRACKING_STATUS_TRACKING
        )
        self.assertEqual(
            by_type["RT"]["tracking_status"], schema.TRACKING_STATUS_TRACKING
        )
        self.assertIsNone(by_type["CDT"]["tracking_status"])

    # -- 时间 ---------------------------------------------------------------

    def test_timezone_unknown_leaves_utc_empty(self):
        """时区未确认时不产出 state_time_utc，并记录问题。"""
        records, _, _ = self._run([_row()])
        record = records[0]
        self.assertIsNone(record["state_time_utc"])
        self.assertEqual(record["state_time_raw"], "2025-03-05 10:35:58")
        self.assertIn(schema.ISSUE_TIMEZONE_UNKNOWN, record.get("issues", []))

    def test_timezone_known_fills_utc(self):
        """时区已知时才填 state_time_utc。"""
        path = _write_csv([_row()])
        try:
            adapter = _adapter(timezone_known=True, timezone_name="Asia/Shanghai")
            records, _, _ = adapter.run(path)
        finally:
            os.unlink(path)
        self.assertIsNotNone(records[0]["state_time_utc"])
        self.assertNotIn(
            schema.ISSUE_TIMEZONE_UNKNOWN, records[0].get("issues", [])
        )

    # -- 拒绝与校验 ---------------------------------------------------------

    def test_invalid_rows_rejected_with_reason(self):
        """非法行被拒绝并给出原因，不能静默通过。"""
        rows = [
            _row(),  # 正常
            _row(**{"目标方位(°)": ""}),  # 缺方位
            _row(**{"目标距离(米)": "-5"}),  # 负距离
            _row(**{"目标方位(°)": "361.0"}),  # 方位越界
            _row(**{"目标类型": ""}),  # 缺目标类型
        ]
        records, rejected, _ = self._run(rows)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(rejected), 4)
        for item in rejected:
            self.assertTrue(item["reason"])
            self.assertIn("issue", item)

    def test_missing_time_rejected(self):
        """缺起始时间的行不可用于时间对齐，应被拒绝。"""
        records, rejected, _ = self._run([_row(**{"起始时间(帧)": ""})])
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 1)

    def test_bearing_exactly_zero_accepted(self):
        """方位 0° 是合法值（正北），不能被误判为缺失。"""
        records, rejected, _ = self._run([_row(**{"目标方位(°)": "0"})])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(rejected), 0)
        self.assertAlmostEqual(records[0]["position"]["bearing_rad"], 0.0)

    def test_record_ids_unique_and_stable(self):
        """记录 ID 必须唯一，且同一输入两次运行结果一致。"""
        rows = [_row(), _row(**{"帧序号": "2"})]
        path = _write_csv(rows)
        try:
            first, _, _ = _adapter().run(path)
            second, _, _ = _adapter().run(path)
        finally:
            os.unlink(path)
        ids = [r["record_id"] for r in first]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(ids, [r["record_id"] for r in second])

    def test_frame_index_preserved_for_downsampling(self):
        """帧号必须保留 —— 相邻两帧共享时间戳是正常的帧率降采样，
        只有 frame_index 能区分二者。"""
        records, _, _ = self._run(
            [
                _row(**{"帧序号": "7", "目标距离(米)": "3328.8"}),
                _row(**{"帧序号": "8", "目标距离(米)": "3329.4"}),
            ]
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(
            sorted(r["radar_frame_index"] for r in records), [7.0, 8.0]
        )
        # 两条时间戳相同但记录 ID 不同
        self.assertEqual(records[0]["state_time_raw"], records[1]["state_time_raw"])
        self.assertNotEqual(records[0]["record_id"], records[1]["record_id"])

    def test_raw_record_preserved(self):
        """原始行必须保留，便于追溯。"""
        records, _, _ = self._run([_row()])
        raw = records[0]["raw_record"]
        self.assertEqual(raw["目标类型"], "CDT")
        self.assertEqual(raw["目标编号"], "900001")


class TestMtdspDatasetGeometryFacts(unittest.TestCase):
    """锁定 MTDSP 真实数据上已核验的几何事实。

    这些断言来自对原始 CSV 的逐条核验（2026-11-06 复核），
    作用是防止"CDT 相对 AIS 偏 165 m"这类错误结论回归。

    事实链：
      1. IF_*.csv 帧头（123 帧）记录雷达自记站址 = (37.53297, 121.42020)；
      2. AIS 行内（经纬度 + 方位角 + 距离）三元组反解的参考站 ≈ 121.41990；
      3. 同文件内 CDT vs AT（光电确认关联）Δ方位 ≈ −2.42°；
      4. x_center/y_center 是归一化图像坐标（0–1），不是米；
      5. CDT 的"目标方位/目标距离"落在检测框区间内且位于区间中部。
    """

    DATA_DIR = os.path.join(ROOT, "data", "mtdsp")
    CUT = os.path.join(DATA_DIR, "CutDataTarget.csv")
    IF = os.path.join(DATA_DIR, "IF_2003_AT_413203610.csv")

    @classmethod
    def setUpClass(cls):
        if not (os.path.exists(cls.CUT) and os.path.exists(cls.IF)):
            raise unittest.SkipTest("MTDSP 原始数据不在工作区，跳过数据集事实回归")
        with open(cls.CUT, "r", encoding="utf-8-sig", newline="") as handle:
            cls.cut = list(csv.DictReader(handle))
        with open(cls.IF, "r", encoding="utf-8-sig", newline="") as handle:
            cls.if_rows = list(csv.DictReader(handle))

    def test_radar_self_reported_station_is_authoritative(self):
        """IF 帧头记录雷达自记站址，且 123 帧完全一致。"""
        frames = [r for r in self.if_rows if r["帧序号"]]
        self.assertEqual(len(frames), 127)
        latitudes = {r["纬度(北纬)(°)"] for r in frames if r["纬度(北纬)(°)"]}
        longitudes = {r["经度(东经)(°)"] for r in frames if r["经度(东经)(°)"]}
        self.assertEqual(latitudes, {"37.53297"})
        self.assertEqual(longitudes, {"121.4202"})

    def test_cdt_vs_at_bearing_offset_is_systematic(self):
        """同文件内 CDT（雷达实测）相对 AT（AIS 拷贝）有稳定的方位差。

        这是"两套坐标基准差异"的直接证据，不是检测框的固有偏移。
        """
        cdt = [
            r
            for r in self.cut
            if r["目标类型"] == "CDT" and r["目标编号"] == "413203610"
        ]
        at = [
            r
            for r in self.cut
            if r["目标类型"] == "AT" and r["目标编号"] == "413203610"
        ]
        self.assertTrue(cdt and at)
        at_polar = [(float(r["目标方位(°)"]), float(r["目标距离(米)"])) for r in at]
        offsets = []
        for row in cdt:
            bearing = float(row["目标方位(°)"])
            best = min(at_polar, key=lambda p: abs((bearing - p[0] + 180) % 360 - 180))
            offsets.append((bearing - best[0] + 180) % 360 - 180)
        offsets.sort()
        median = offsets[len(offsets) // 2]
        # 稳定在 −2.4° 附近；若此断言失败，说明数据或口径发生变化
        self.assertAlmostEqual(median, -2.42, delta=0.15)

    def test_box_center_coordinates_are_normalized_not_metres(self):
        """x_center/y_center 是归一化图像坐标（0–1），不是米。

        防止再次出现"检测框中心偏 165 m"这类把像素当米数的错误。
        """
        cdt = [r for r in self.cut if r["目标类型"] == "CDT"]
        self.assertTrue(cdt)
        for row in cdt:
            self.assertGreaterEqual(float(row["x_center"]), 0.0)
            self.assertLessEqual(float(row["x_center"]), 1.0)
            self.assertGreaterEqual(float(row["y_center"]), 0.0)
            self.assertLessEqual(float(row["y_center"]), 1.0)

    def test_cdt_target_position_is_box_centre(self):
        """CDT 的目标方位/目标距离落在检测框内且位于区间中部。

        说明检测框几何本身自洽 —— 问题出在参考点基准，不在框。
        """
        cdt = [r for r in self.cut if r["目标类型"] == "CDT"]
        bearing_positions = []
        range_positions = []
        for row in cdt:
            a0 = float(row["[外接矩形]起始角度(°)"])
            a1 = float(row["[外接矩形]结束角度(°)"])
            d0 = float(row["[外接矩形]起始距离(米)"])
            d1 = float(row["[外接矩形]结束距离(米)"])
            bearing = float(row["目标方位(°)"])
            distance = float(row["目标距离(米)"])
            lo_b, hi_b = min(a0, a1), max(a0, a1)
            lo_d, hi_d = min(d0, d1), max(d0, d1)
            self.assertLessEqual(lo_b, bearing)
            self.assertLessEqual(bearing, hi_b)
            self.assertLessEqual(lo_d, distance)
            self.assertLessEqual(distance, hi_d)
            bearing_positions.append((bearing - lo_b) / (hi_b - lo_b))
            range_positions.append((distance - lo_d) / (hi_d - lo_d))
        bearing_positions.sort()
        range_positions.sort()
        self.assertAlmostEqual(
            bearing_positions[len(bearing_positions) // 2], 0.51, delta=0.06
        )
        self.assertAlmostEqual(
            range_positions[len(range_positions) // 2], 0.51, delta=0.06
        )


if __name__ == "__main__":
    unittest.main()
