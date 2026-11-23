"""航迹导出模块的单元测试。

重点覆盖三件事：

  1. **分组正确性** —— 三种传感器各自成轨；光电 Inf/TV 不混。
  2. **运动学推导正确性** —— 位置量化下仍能恢复真实速度。
     这是关键回归：逐段差分会把速度虚增到 5.4 m/s（真值 4.72），
     窗口最小二乘才能得到 4.75 m/s。
  3. **不稳定编号的标记** —— 雷达 900xxx 不得被当作可用关联主键。
"""

from __future__ import annotations

import csv
import math
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alignment.track_export import (  # noqa: E402
    TrackExporter,
    _detect_quantization_jumps,
    _derive_kinematics,
    _segment_displacements,
)

ORIGIN_LAT = 37.53297
ORIGIN_LON = 121.41990


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (local)"


def _record(
    index: int,
    sensor_type: str,
    local_track_id: str,
    moment: datetime,
    east_m: float,
    north_m: float,
    **extra,
) -> dict:
    record = {
        "record_id": f"r{index}",
        "source_id": f"src_{sensor_type}",
        "source_type": sensor_type,
        "local_track_id": local_track_id,
        "aligned_time_utc": _iso(moment),
        "alignment_status": "ok",
        "position_type": "range_bearing",
        "east_m": east_m,
        "north_m": north_m,
        "up_m": 0.0,
        "source_row": index,
    }
    record.update(extra)
    return record


class TestTrackGrouping(unittest.TestCase):
    """三种传感器各自成轨，光电 Inf/TV 不混。"""

    def setUp(self):
        self.exporter = TrackExporter(ORIGIN_LAT, ORIGIN_LON)
        self.start = datetime(2025, 3, 12, 10, 12, 33)

    def test_three_sensors_produce_three_groups(self):
        records = [
            _record(0, "radar", "413203610", self.start, 0.0, 5000.0),
            _record(1, "ais", "413203610", self.start, 0.0, 5000.0),
            _record(2, "optical", "413203610@Inf", self.start, 0.0, 5000.0),
        ]
        tracks, points, _ = self.exporter.build(records)
        by_sensor = {t["sensor_type"] for t in tracks}
        self.assertEqual(by_sensor, {"radar", "ais", "optical"})
        self.assertEqual(len(tracks), 3)
        self.assertEqual(len(points["radar:413203610"]), 1)

    def test_optical_inf_and_tv_are_separate_tracks(self):
        """Inf 与 TV 同一时刻各成一条航迹，不产生重复时间戳。"""
        records = []
        for offset in range(3):
            moment = self.start + timedelta(seconds=5 * offset)
            records.append(
                _record(offset, "optical", "413203610@Inf", moment,
                        float(offset), 5000.0)
            )
            records.append(
                _record(100 + offset, "optical", "413203610@TV", moment,
                        float(offset), 5000.0)
            )
        tracks, points, _ = self.exporter.build(records)
        ids = {t["track_id"] for t in tracks}
        self.assertIn("optical:413203610@Inf", ids)
        self.assertIn("optical:413203610@TV", ids)
        for track_id in ("optical:413203610@Inf", "optical:413203610@TV"):
            times = [row["time_utc"] for row in points[track_id]]
            self.assertEqual(len(times), len(set(times)), "同一航迹内时间戳重复")

    def test_radar_900xxx_marked_unstable(self):
        """900xxx 是内部号，不是稳定航迹号。"""
        records = [
            _record(i, "radar", "900001", self.start + timedelta(seconds=3 * i),
                    float(i), 5000.0)
            for i in range(5)
        ]
        tracks, _, _ = self.exporter.build(records)
        track = tracks[0]
        self.assertEqual(track["track_stability"], "unstable_internal_id")
        self.assertEqual(track["track_kind"], "radar_detection_sequence")

    def test_mmsi_tracks_marked_stable(self):
        records = [
            _record(i, "radar", "413203610", self.start + timedelta(seconds=3 * i),
                    float(i), 5000.0)
            for i in range(5)
        ]
        tracks, _, _ = self.exporter.build(records)
        self.assertEqual(tracks[0]["track_stability"], "stable")
        self.assertEqual(tracks[0]["track_kind"], "sensor_track")

    def test_unusable_positions_excluded(self):
        """标准化层判定的误检不得进入航迹。"""
        records = [
            _record(0, "optical", "413203610@Inf", self.start, 0.0, 5000.0),
            dict(
                _record(1, "optical", "optical_det_1@TV", self.start, 100.0, 5000.0),
                alignment_status="unusable_position",
            ),
        ]
        tracks, _, _ = self.exporter.build(records)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["track_id"], "optical:413203610@Inf")

    def test_detections_kept_separately(self):
        """雷达逐帧检测点单独输出，供下游跨帧聚类重建。"""
        records = [
            _record(i, "radar", "900001", self.start + timedelta(seconds=3 * i),
                    float(i), 5000.0, radar_measurement_kind="radar_detected",
                    radar_target_type="CDT", radar_frame_index=i)
            for i in range(4)
        ]
        _, _, detections = self.exporter.build(records)
        self.assertEqual(len(detections), 4)
        self.assertEqual(detections[0]["target_type"], "CDT")


class TestQuantizationJumpDetection(unittest.TestCase):
    """量化跳变识别。"""

    def test_large_jump_detected(self):
        segments = [(5.0, 0.0, 0.0), (5.0, 8.0, 0.0), (5.0, 9.0, 0.0),
                     (5.0, 8.0, 0.0), (5.0, 130.0, 0.0), (5.0, 8.0, 0.0),
                     (5.0, 9.0, 0.0)]
        jumps = _detect_quantization_jumps(segments)
        self.assertTrue(jumps[4])
        self.assertFalse(jumps[1])

    def test_too_few_segments_no_detection(self):
        segments = [(5.0, 0.0, 0.0), (5.0, 900.0, 0.0)]
        self.assertEqual(_detect_quantization_jumps(segments), [False, False])


class TestKinematicsDerivation(unittest.TestCase):
    """运动学推导：位置量化下仍要恢复真实速度。"""

    def setUp(self):
        self.exporter = TrackExporter(ORIGIN_LAT, ORIGIN_LON)

    def _quantized_track(self, true_speed_mps: float = 4.72, count: int = 60):
        """构造一条量化位置的真实匀速航迹。

        光电测距是整数米档位、方位是 0.1° 档位，位置因此呈阶梯状：
        连续若干点完全相同，然后跳一步。真实运动是匀速直线。
        """
        records = []
        start = datetime(2025, 3, 12, 10, 12, 33)
        course_rad = math.radians(140.0)
        for index in range(count):
            t = index * 5.0
            true_east = true_speed_mps * t * math.sin(course_rad)
            true_north = true_speed_mps * t * math.cos(course_rad)
            # 量化：距离取整米，方位取 0.1 度
            range_m = math.hypot(true_east, true_north)
            bearing = math.degrees(math.atan2(true_east, true_north))
            quant_range = round(range_m)
            quant_bearing = round(bearing * 10) / 10.0
            east = quant_range * math.sin(math.radians(quant_bearing))
            north = quant_range * math.cos(math.radians(quant_bearing))
            records.append(
                _record(index, "optical", "413203610@Inf",
                        start + timedelta(seconds=t), east, north)
            )
        return records

    def test_window_fit_recovers_true_speed(self):
        """窗口最小二乘必须恢复真实速度（逐段差分做不到）。"""
        records = self._quantized_track(true_speed_mps=4.72)
        tracks, points, _ = self.exporter.build(records)
        track = tracks[0]
        # 量化会引入误差，但应在 15% 以内
        self.assertAlmostEqual(
            float(track["mean_speed_mps"]), 4.72, delta=4.72 * 0.15
        )
        # 不能像逐段差分那样虚增到 5.4 以上
        self.assertLess(float(track["mean_speed_mps"]), 5.2)

    def test_all_points_have_derived_speed(self):
        records = self._quantized_track()
        tracks, points, _ = self.exporter.build(records)
        rows = points["optical:413203610@Inf"]
        derived = [r for r in rows if r["speed_source"] == "derived"]
        self.assertGreaterEqual(len(derived), len(rows) * 0.9)

    def test_reported_speed_not_overwritten(self):
        """实测速度不得被推导值覆盖。"""
        records = []
        start = datetime(2025, 3, 12, 10, 12, 33)
        for index in range(10):
            records.append(
                _record(index, "ais", "413203610",
                        start + timedelta(seconds=10 * index),
                        0.0, 5000.0 + 4.7 * 10 * index,
                        speed_mps=4.7, course_rad=0.0)
            )
        tracks, points, _ = self.exporter.build(records)
        rows = points["ais:413203610"]
        for row in rows:
            self.assertEqual(row["speed_source"], "reported")
            self.assertAlmostEqual(row["speed_mps"], 4.7, places=6)
        self.assertTrue(tracks[0]["has_reported_speed"])

    def test_short_track_falls_back_without_crashing(self):
        """点数不足时窗口拟合不可用，回退路径也不能崩。"""
        records = [
            _record(i, "optical", "413203610@Inf",
                    datetime(2025, 3, 12, 10, 12, 33) + timedelta(seconds=5 * i),
                    float(i * 8), 5000.0)
            for i in range(3)
        ]
        tracks, points, _ = self.exporter.build(records)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(points["optical:413203610@Inf"]), 3)

    def test_stationary_track_gives_zero_speed(self):
        records = [
            _record(i, "optical", "413203610@Inf",
                    datetime(2025, 3, 12, 10, 12, 33) + timedelta(seconds=5 * i),
                    0.0, 5000.0)
            for i in range(20)
        ]
        tracks, _, _ = self.exporter.build(records)
        self.assertAlmostEqual(float(tracks[0]["mean_speed_mps"]), 0.0, places=6)


class TestNestedAndFlatRecordsAgree(unittest.TestCase):
    """流水线的嵌套 JSON 与落盘的扁平 CSV 必须给出同样的航迹。

    回归起因：流水线里的记录是嵌套 JSON（`range_m` 在 `position` 内），
    而 `aligned_records.csv` 是扁平 CSV（`range_m` 在顶层）。导出器
    最初只认后者，导致经流水线产出的航迹丢失极坐标字段。
    """

    def setUp(self):
        self.exporter = TrackExporter(ORIGIN_LAT, ORIGIN_LON)
        self.moment = datetime(2025, 3, 12, 10, 12, 33)

    def _nested(self):
        return {
            "record_id": "n1",
            "source_id": "src_optical",
            "source_type": "optical",
            "local_track_id": "413203610@Inf",
            "aligned_time_utc": _iso(self.moment),
            "alignment_status": "ok",
            "position_type": "range_bearing",
            "position": {
                "range_m": 3877.7,
                "bearing_rad": 0.23212879051524585,
                "bearing_reference": "T",
            },
            "east_m": 892.06,
            "north_m": 3773.69,
            "up_m": -2.36,
        }

    def _flat(self):
        return {
            "record_id": "f1",
            "source_id": "src_optical",
            "source_type": "optical",
            "local_track_id": "413203610@Inf",
            "aligned_time_utc": _iso(self.moment),
            "alignment_status": "ok",
            "position_type": "range_bearing",
            "range_m": 3877.7,
            "bearing_rad": 0.23212879051524585,
            "east_m": 892.06,
            "north_m": 3773.69,
            "up_m": -2.36,
        }

    def test_polar_fields_survive_nested_input(self):
        _, points, _ = self.exporter.build([self._nested()])
        row = points["optical:413203610@Inf"][0]
        self.assertAlmostEqual(row["range_m"], 3877.7, places=3)
        self.assertAlmostEqual(row["bearing_deg"], 13.3, places=2)

    def test_nested_and_flat_produce_identical_rows(self):
        _, nested_points, _ = self.exporter.build([self._nested()])
        _, flat_points, _ = self.exporter.build([self._flat()])
        nested_row = dict(nested_points["optical:413203610@Inf"][0])
        flat_row = dict(flat_points["optical:413203610@Inf"][0])
        for row in (nested_row, flat_row):
            row.pop("record_id", None)
        self.assertEqual(nested_row, flat_row)


class TestTrackExportFiles(unittest.TestCase):
    """落盘结构。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.exporter = TrackExporter(ORIGIN_LAT, ORIGIN_LON)
        start = datetime(2025, 3, 12, 10, 12, 33)
        records = []
        for index in range(6):
            records.append(
                _record(index, "radar", "413203610",
                        start + timedelta(seconds=3 * index),
                        float(index * 10), 5000.0)
            )
            records.append(
                _record(50 + index, "ais", "413203610",
                        start + timedelta(seconds=3 * index),
                        float(index * 10), 5000.0, speed_mps=3.3)
            )
            records.append(
                _record(100 + index, "optical", "413203610@TV",
                        start + timedelta(seconds=3 * index),
                        float(index * 10), 5000.0)
            )
        self.tracks, self.points, self.detections = self.exporter.build(records)
        self.result = self.exporter.write(
            self.tmp, self.tracks, self.points, self.detections
        )

    def test_expected_files_exist(self):
        for relative in (
            "tracks/radar/413203610.csv",
            "tracks/ais/413203610.csv",
            "tracks/optical/413203610@TV.csv",
            "tracks/track_index.csv",
            "tracks/track_summary.json",
            "tracks/radar_detections.csv",
        ):
            self.assertTrue(
                os.path.exists(os.path.join(self.tmp, relative)), relative
            )

    def test_track_csv_has_kinematics_columns(self):
        path = os.path.join(self.tmp, "tracks/radar/413203610.csv")
        with open(path, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 6)
        self.assertIn("speed_mps", rows[0])
        self.assertIn("speed_source", rows[0])
        self.assertIn("course_deg", rows[0])

    def test_index_lists_all_tracks(self):
        path = os.path.join(self.tmp, "tracks/track_index.csv")
        with open(path, encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {r["sensor_type"] for r in rows}, {"radar", "ais", "optical"}
        )

    def test_summary_counts(self):
        import json

        with open(self.result["summary"], encoding="utf-8") as handle:
            summary = json.load(handle)
        self.assertEqual(summary["track_count"], 3)
        self.assertEqual(summary["point_count"], 18)
        self.assertIn("radar", summary["by_sensor"])


if __name__ == "__main__":
    unittest.main()
