"""MTDSP 光电适配器（optical_photo_adapter）单元测试。

覆盖：
  - 文件名解析（设备 / MMSI / 焦距 / 俯仰 / 中心方位 / 时间戳）；
  - `.txt` 设备解算元数据 → 主目标记录（range_bearing + 真北方位）；
  - `_DLDete.txt` AI 检测 → 画面内检测记录（类型标签 / 匹配编号 / 测距）；
  - 字段数为 7 的检测行（无测距）不产出位置观测；
  - 距离超限的检测被标记为误检且不可用于空间对齐；
  - 距离来源判定（AIS 快照拷贝 vs 光电独立测量）；
  - 非法输入被拒绝且给出原因；
  - 时区未确认时不产出 state_time_utc。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization import schema  # noqa: E402
from standardization.optical_photo_adapter import (  # noqa: E402
    RANGE_SOURCE_AIS_SNAPSHOT,
    RANGE_SOURCE_INDEPENDENT,
    RANGE_SOURCE_UNKNOWN,
    RECORD_KIND_DETECTION,
    RECORD_KIND_PRIMARY,
    OpticalPhotoAdapter,
    device_scoped_track_id,
)

META_NAME = (
    "Photo_ScreenShot_Inf_2002_AIS_0_413203610_5.00_0.000_13.30_20250305103643_2"
)

META_BODY = """光电设备型号:DS-2ZTD816CX-HB/YT
光电设备编码:PHK003
象元(um):17
水平视场角(°):2.932
俯仰视场角(°):2.3490
目标距离(M):3877.70
焦距:5.00
真实高度(M):159.00
真实宽度(M):198.48
俯仰角度(°):0.00
中心点方位(°):13.30
最大方位(°):14.77
最小方位(°):11.83
"""

# 9 字段：含匹配编号与测距
DETECTION_FULL = "2,[民用]Civil Ship(Cargo),0.92,1007.00,545.00,594.00,123.00,1,33452,2\n"
# 7 字段：无匹配编号与测距
DETECTION_NO_RANGE = "1,[民用]Civil Ship(Fishing),0.33,1175.00,500.00,157.00,47.00\n"


def _config(**overrides):
    base = {
        "source_id": "mtdsp_optical_test",
        "source_type": "optical",
        "session_id": "mtdsp-test",
        "timezone_known": False,
        "timezone_name": None,
        "station_latitude_deg": 37.53297,
        "station_longitude_deg": 121.42020,
        "max_range_m": 20000.0,
        "image_width_px": 1920,
        "image_height_px": 1080,
    }
    base.update(overrides)
    return base


def _write(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


class OpticalPhotoAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------

    def _one_package(self, meta_body=META_BODY, detection_body=DETECTION_FULL):
        _write(self.dir, META_NAME + ".txt", meta_body)
        if detection_body is not None:
            _write(self.dir, META_NAME + "_DLDete.txt", detection_body)
        return OpticalPhotoAdapter(_config())

    def _records(self, adapter):
        records, rejected, _log = adapter.run(self.dir)
        return records, rejected

    def _all(self, adapter):
        return adapter.run(self.dir)

    # -- 主目标记录 ---------------------------------------------------------

    def test_primary_record_from_metadata(self):
        adapter = self._one_package()
        records, rejected = self._records(adapter)
        primaries = [r for r in records if r["optical_record_kind"] == RECORD_KIND_PRIMARY]
        self.assertEqual(len(primaries), 1)
        self.assertEqual(len(rejected), 0)
        record = primaries[0]

        self.assertEqual(record["source_type"], schema.SOURCE_TYPE_OPTICAL)
        self.assertEqual(record["position_type"], schema.POSITION_TYPE_RANGE_BEARING)
        self.assertEqual(record["local_track_id"], "413203610@Inf")
        self.assertEqual(record["optical_primary_mmsi"], "413203610")
        self.assertEqual(record["optical_device"], "Inf")
        self.assertEqual(record["optical_band"], "infrared")
        self.assertEqual(record["optical_device_model"], "DS-2ZTD816CX-HB/YT")

        # 距离与方位
        self.assertAlmostEqual(record["position"]["range_m"], 3877.70, places=6)
        self.assertAlmostEqual(record["position"]["bearing_rad"], 13.30 * 3.141592653589793 / 180.0, places=9)
        self.assertEqual(record["position"]["bearing_reference"], schema.REFERENCE_TRUE)

        # 时间取自文件名时间戳
        self.assertEqual(record["state_time_raw"], "2025-03-05 10:36:43")
        self.assertEqual(record["optical_capture_time_raw"], "2025-03-05 10:36:43")

        # 文件名第 4/5/6 段 = 焦距/俯仰/中心方位，与元数据一致
        self.assertAlmostEqual(record["optical_filename_focal"], 5.00, places=6)
        self.assertAlmostEqual(record["optical_filename_pitch"], 0.0, places=6)
        self.assertAlmostEqual(record["optical_filename_azimuth"], 13.30, places=6)
        self.assertAlmostEqual(record["optical_focal_length_mm"], 5.00, places=6)
        self.assertAlmostEqual(record["optical_min_azimuth_deg"], 11.83, places=6)
        self.assertAlmostEqual(record["optical_max_azimuth_deg"], 14.77, places=6)

    def test_timezone_unknown_leaves_utc_empty(self):
        adapter = self._one_package()
        records, _ = self._records(adapter)
        for record in records:
            self.assertIsNone(record["state_time_utc"])
            self.assertIn(schema.ISSUE_TIMEZONE_UNKNOWN, record["issues"])

    # -- 检测记录 -----------------------------------------------------------

    def test_detection_record_fields(self):
        adapter = self._one_package()
        records, _ = self._records(adapter)
        detections = [r for r in records if r["optical_record_kind"] == RECORD_KIND_DETECTION]
        self.assertEqual(len(detections), 1)
        record = detections[0]

        self.assertEqual(record["local_track_id"], "optical_det_2@Inf")
        self.assertEqual(record["optical_detection_index"], 2)
        self.assertEqual(record["optical_target_index"], 2)
        self.assertEqual(record["optical_match_id"], "1")
        self.assertEqual(record["optical_type_category"], "民用")
        self.assertEqual(record["optical_type_label"], "Civil Ship(Cargo)")
        self.assertAlmostEqual(record["optical_confidence"], 0.92, places=6)
        self.assertAlmostEqual(record["position"]["range_m"], 33452.0, places=6)
        self.assertAlmostEqual(record["optical_box_center_x_px"], 1007.0, places=6)
        self.assertAlmostEqual(record["optical_box_height_px"], 123.0, places=6)

    def test_detection_without_range_is_dropped(self):
        """字段数为 7 的行没有测距，不能当作位置观测。"""
        adapter = self._one_package(detection_body=DETECTION_NO_RANGE)
        records, _ = self._records(adapter)
        detections = [r for r in records if r["optical_record_kind"] == RECORD_KIND_DETECTION]
        self.assertEqual(len(detections), 0)
        self.assertEqual(len(records), 1)  # 仅主目标

    def test_out_of_range_detection_is_marked_and_invalid(self):
        """距离超限的检测是误检：保留记录但不可用于空间对齐。"""
        body = "2,[民用]Civil Ship(Cargo),0.92,1007.00,545.00,594.00,123.00,1,280743,2\n"
        adapter = self._one_package(detection_body=body)
        records, _ = self._records(adapter)
        detections = [r for r in records if r["optical_record_kind"] == RECORD_KIND_DETECTION]
        self.assertEqual(len(detections), 1)
        record = detections[0]
        self.assertTrue(record["optical_detection_out_of_range"])
        self.assertFalse(record["valid_for_spatial_alignment"])

    def test_in_range_detection_is_valid(self):
        body = "2,[民用]Civil Ship(Cargo),0.92,1007.00,545.00,594.00,123.00,1,3878,2\n"
        adapter = self._one_package(detection_body=body)
        records, _ = self._records(adapter)
        detections = [r for r in records if r["optical_record_kind"] == RECORD_KIND_DETECTION]
        self.assertEqual(len(detections), 1)
        self.assertFalse(detections[0]["optical_detection_out_of_range"])
        self.assertTrue(detections[0]["valid_for_spatial_alignment"])

    # -- 距离来源判定 -------------------------------------------------------

    def test_range_source_unknown_without_ais_index(self):
        adapter = self._one_package()
        records, _ = self._records(adapter)
        primaries = [r for r in records if r["optical_record_kind"] == RECORD_KIND_PRIMARY]
        self.assertEqual(primaries[0]["optical_range_source"], RANGE_SOURCE_UNKNOWN)

    def test_range_source_ais_snapshot_and_independent(self):
        """与 AIS 库精确相等 → 快照拷贝；否则 → 光电独立测量。"""
        ais_dir = os.path.join(self.dir, "ais")
        os.makedirs(ais_dir)
        with open(
            os.path.join(ais_dir, "AIS_Trajectory_413203610.csv"),
            "w",
            encoding="utf-8-sig",
        ) as handle:
            handle.write("MMSI,经度(°),纬度(°),时间,速度(kn),距离(m)\n")
            handle.write("413203610,121.426,37.5675,2025-03-05 10:36:43,0.0,3877.70\n")

        adapter = self._one_package()
        adapter.ais_range_index = {3877.70}
        self.assertEqual(adapter._classify_range_source(3877.70), RANGE_SOURCE_AIS_SNAPSHOT)
        self.assertEqual(adapter._classify_range_source(3878.15), RANGE_SOURCE_INDEPENDENT)
        self.assertEqual(adapter._classify_range_source(None), RANGE_SOURCE_UNKNOWN)

    # -- 拒绝与边界 ---------------------------------------------------------

    def test_unparsable_filename_is_rejected(self):
        _write(self.dir, "Photo_ScreenShot_Bogus_xxx.txt", META_BODY)
        adapter = OpticalPhotoAdapter(_config())
        records, rejected, _log = self._all(adapter)
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "unparsable_photo_filename")

    def test_missing_range_in_metadata_is_rejected(self):
        body = META_BODY.replace("目标距离(M):3877.70\n", "")
        adapter = self._one_package(meta_body=body)
        records, rejected, _log = self._all(adapter)
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "missing_required_optical_field")

    def test_negative_range_is_rejected(self):
        """元数据距离非法：主目标记录被拒绝。

        检测记录有自己独立的测距字段，且其方位来自中心点方位（另行校验），
        因此仍可产出；但距离超量程者不可用于空间对齐。
        """
        body = META_BODY.replace("目标距离(M):3877.70", "目标距离(M):-10.0")
        adapter = self._one_package(meta_body=body)
        records, rejected, _log = self._all(adapter)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(
            [r for r in records if r["optical_record_kind"] == RECORD_KIND_PRIMARY], []
        )

    def test_out_of_range_azimuth_is_rejected(self):
        """设备中心方位非法：主目标与沿用该方位的检测记录一并被拒绝。"""
        body = META_BODY.replace("中心点方位(°):13.30", "中心点方位(°):361.5")
        adapter = self._one_package(meta_body=body)
        records, rejected, _log = self._all(adapter)
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 1)

    def test_empty_directory_returns_no_records(self):
        adapter = OpticalPhotoAdapter(_config())
        records, rejected, log = adapter.run(self.dir)
        self.assertEqual(len(records), 0)
        self.assertEqual(len(rejected), 0)
        self.assertTrue(any("未找到光电文件" in line for line in log))

    def test_missing_detection_file_still_yields_primary(self):
        adapter = self._one_package(detection_body=None)
        records, rejected = self._records(adapter)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(rejected), 0)
        self.assertEqual(records[0]["optical_record_kind"], RECORD_KIND_PRIMARY)

    # -- Inf / TV 设备隔离 --------------------------------------------------

    def test_inf_and_tv_get_distinct_track_ids(self):
        """Inf 与 TV 是两个独立设备，必须是两条独立航迹。

        若共用同一 local_track_id，光电航迹点序列会出现重复时间戳且
        同刻有两个不同位置，下游无法判定取舍。
        """
        tv_name = META_NAME.replace("_Inf_", "_TV_")
        _write(self.dir, META_NAME + ".txt", META_BODY)
        _write(self.dir, tv_name + ".txt", META_BODY)
        _write(self.dir, META_NAME + "_DLDete.txt", DETECTION_FULL)
        _write(self.dir, tv_name + "_DLDete.txt", DETECTION_FULL)

        adapter = OpticalPhotoAdapter(_config())
        records, _rejected = self._records(adapter)

        ids = {r["local_track_id"] for r in records}
        self.assertIn("413203610@Inf", ids)
        self.assertIn("413203610@TV", ids)
        self.assertIn("optical_det_2@Inf", ids)
        self.assertIn("optical_det_2@TV", ids)
        # 设备隔离后，原始 MMSI 语义仍然保留在专用字段中
        for record in records:
            if record["optical_record_kind"] == RECORD_KIND_PRIMARY:
                self.assertEqual(record["optical_primary_mmsi"], "413203610")

    def test_device_scoped_track_id_helper(self):
        self.assertEqual(device_scoped_track_id("413203610", "Inf"), "413203610@Inf")
        self.assertEqual(device_scoped_track_id("413203610", "TV"), "413203610@TV")
        # 设备未知时退回原编号，不臆造设备标识
        self.assertEqual(device_scoped_track_id("413203610", None), "413203610")
        self.assertEqual(device_scoped_track_id("413203610", ""), "413203610")
        # 已经带过设备标识时不重复追加
        self.assertEqual(
            device_scoped_track_id("413203610@Inf", "TV"), "413203610@Inf"
        )

    def test_same_timestamp_two_devices_produce_two_tracks(self):
        """同时刻 Inf/TV 两条记录：各自成轨，不产生重复时间戳。"""
        tv_name = META_NAME.replace("_Inf_", "_TV_")
        _write(self.dir, META_NAME + ".txt", META_BODY)
        _write(self.dir, tv_name + ".txt", META_BODY)

        adapter = OpticalPhotoAdapter(_config())
        records, _rejected = self._records(adapter)
        primaries = [
            r for r in records if r["optical_record_kind"] == RECORD_KIND_PRIMARY
        ]
        self.assertEqual(len(primaries), 2)

        by_track = {}
        for record in primaries:
            by_track.setdefault(record["local_track_id"], []).append(
                record["state_time_raw"]
            )
        # 每条航迹内部时间戳不重复
        for track_id, times in by_track.items():
            self.assertEqual(len(times), len(set(times)), f"{track_id} 内部有重复时间戳")
        # 两条航迹各只有 1 个点
        self.assertEqual(len(by_track), 2)
        for times in by_track.values():
            self.assertEqual(len(times), 1)


if __name__ == "__main__":
    unittest.main()
