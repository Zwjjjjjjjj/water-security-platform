"""MTDSP 三源一致性验证（verify_mtdsp_three_source）单元测试。

覆盖：
  - DLDete 像素 → 绝对方位换算的正确性；
  - AIS 轨迹插值不做外推；
  - 距离 + 方位双门限配对，拒绝距离近但方位远的误检；
  - 方位零点偏差 vs 站址偏移两种模型的判据（含判据退化处理）；
  - 跨数据集复现检验；
  - 关联门限建议。
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from geodesy import LocalOrigin  # noqa: E402
from verification import verify_mtdsp_three_source as vts  # noqa: E402


class PixelToAzimuthTest(unittest.TestCase):
    """DLDete 检测框像素 → 绝对方位。"""

    def test_center_pixel_gives_center_azimuth(self):
        center_az = 13.30
        hfov = 2.932
        x = vts.IMAGE_WIDTH_PX / 2.0
        azimuth = (center_az + (x - vts.IMAGE_WIDTH_PX / 2.0) / vts.IMAGE_WIDTH_PX * hfov)
        self.assertAlmostEqual(azimuth, center_az, places=9)

    def test_right_of_center_gives_larger_azimuth(self):
        """框在中心右侧 ⇒ 方位大于中心方位（真北顺时针为正）。"""
        center_az = 13.30
        hfov = 2.932
        x = vts.IMAGE_WIDTH_PX / 2.0 + 100.0
        azimuth = (center_az + (x - vts.IMAGE_WIDTH_PX / 2.0) / vts.IMAGE_WIDTH_PX * hfov)
        self.assertGreater(azimuth, center_az)
        self.assertAlmostEqual(azimuth - center_az, 100.0 / 1920.0 * hfov, places=9)

    def test_left_of_center_gives_smaller_azimuth(self):
        center_az = 13.30
        hfov = 2.932
        x = vts.IMAGE_WIDTH_PX / 2.0 - 100.0
        azimuth = (center_az + (x - vts.IMAGE_WIDTH_PX / 2.0) / vts.IMAGE_WIDTH_PX * hfov)
        self.assertLess(azimuth, center_az)

    def test_full_width_spans_full_fov(self):
        """左右边缘相差正好一个水平视场角。"""
        hfov = 2.932
        left = (13.30 + (0.0 - 960.0) / 1920.0 * hfov)
        right = (13.30 + (1920.0 - 960.0) / 1920.0 * hfov)
        self.assertAlmostEqual(right - left, hfov, places=9)


class InterpolateAisTest(unittest.TestCase):
    """AIS 轨迹插值。"""

    def _track(self):
        base = datetime(2025, 3, 12, 10, 13, 4)
        return [
            {"time": base, "latitude_deg": 37.5790, "longitude_deg": 121.415},
            {"time": base + timedelta(seconds=28), "latitude_deg": 37.5779, "longitude_deg": 121.416},
        ]

    def test_interpolates_midpoint(self):
        track = self._track()
        moment = track[0]["time"] + timedelta(seconds=14)
        latitude, longitude = vts.interpolate_ais(track, moment)
        self.assertAlmostEqual(latitude, 37.57845, places=9)
        self.assertAlmostEqual(longitude, 121.4155, places=9)

    def test_no_extrapolation_before_start(self):
        track = self._track()
        moment = track[0]["time"] - timedelta(seconds=1)
        self.assertIsNone(vts.interpolate_ais(track, moment))

    def test_no_extrapolation_after_end(self):
        """超出末条报告后必须返回 None —— 不能拿过期位置当参考真值。"""
        track = self._track()
        moment = track[-1]["time"] + timedelta(seconds=1)
        self.assertIsNone(vts.interpolate_ais(track, moment))

    def test_exact_endpoint_returns_value(self):
        track = self._track()
        latitude, longitude = vts.interpolate_ais(track, track[0]["time"])
        self.assertAlmostEqual(latitude, 37.5790, places=9)

    def test_empty_track_returns_none(self):
        self.assertIsNone(vts.interpolate_ais([], datetime(2025, 3, 12)))


class NearestAisTargetTest(unittest.TestCase):
    """距离 + 方位双门限配对。"""

    def setUp(self):
        self.origin = LocalOrigin(37.53295, 121.42020)
        base = datetime(2025, 3, 12, 10, 13, 30)
        # 目标 A：正北偏西，约 5000 m
        self.track_a = [
            {"time": base, "latitude_deg": 37.5779, "longitude_deg": 121.416},
        ]
        # 目标 B：按 MTDSP 03-05 实测情形构造 —— 距离仅比 A 近约 200 m，
        # 但方位差约 25°（413203610@3877m vs 413328560@3676m/329°）
        self.track_b = [
            {"time": base, "latitude_deg": 37.5642, "longitude_deg": 121.4090},
        ]
        self.tracks = {"A": self.track_a, "B": self.track_b}
        self.moment = base

    def test_picks_target_matching_both_range_and_bearing(self):
        # 观测：距离 5000 m、方位 ≈ 目标 A 的方位
        east, north = self.origin.to_enu(37.5779, 121.416)
        observed_range = math.hypot(east, north)
        observed_bearing = math.degrees(math.atan2(east, north)) % 360.0
        paired = vts._nearest_ais_target(
            self.tracks,
            self.moment,
            self.origin,
            observed_range_m=observed_range,
            observed_bearing_deg=observed_bearing,
        )
        self.assertIsNotNone(paired)
        self.assertEqual(paired[0], "A")

    def test_rejects_when_bearing_mismatch_but_range_close(self):
        """距离很近但方位差 25° ⇒ 必须拒绝配对。

        这是实测中真实出现的误配：A 目标 @3877 m 的检测被配给
        B 目标 @3680 m（仅差 200 m，但方位差 25°），产生 +34° 假偏差。
        """
        east, north = self.origin.to_enu(37.5779, 121.416)
        observed_range = math.hypot(east, north)
        observed_bearing = math.degrees(math.atan2(east, north)) % 360.0
        # 把方位偏转 25°
        paired = vts._nearest_ais_target(
            self.tracks,
            self.moment,
            self.origin,
            observed_range_m=observed_range,
            observed_bearing_deg=(observed_bearing + 25.0) % 360.0,
        )
        self.assertIsNone(paired, "方位差 25° 时不应配对到任何目标")

    def test_rejects_when_range_mismatch(self):
        east, north = self.origin.to_enu(37.5779, 121.416)
        observed_bearing = math.degrees(math.atan2(east, north)) % 360.0
        paired = vts._nearest_ais_target(
            self.tracks,
            self.moment,
            self.origin,
            observed_range_m=math.hypot(east, north) + 300.0,
            observed_bearing_deg=observed_bearing,
        )
        self.assertIsNone(paired)

    def test_tolerates_small_bias_within_gate(self):
        """真实光电零点偏差 5.5°，在 10° 门限内仍应能配上。"""
        east, north = self.origin.to_enu(37.5779, 121.416)
        observed_range = math.hypot(east, north)
        observed_bearing = math.degrees(math.atan2(east, north)) % 360.0
        paired = vts._nearest_ais_target(
            self.tracks,
            self.moment,
            self.origin,
            observed_range_m=observed_range,
            observed_bearing_deg=(observed_bearing + 5.5) % 360.0,
        )
        self.assertIsNotNone(paired)
        self.assertEqual(paired[0], "A")


class ModelDiscriminationTest(unittest.TestCase):
    """方位零点偏差 vs 站址偏移的判据。"""

    def _samples(self, kind, biases, ranges, bearings, mmsis=None):
        mmsis = mmsis or ["T1"] * len(biases)
        base = datetime(2025, 3, 12, 10, 13, 30)
        return [
            {
                "source_kind": kind,
                "mmsi": mmsis[index],
                "delta_bearing_deg": bias,
                "range_ref_m": ranges[index],
                "bearing_ref_deg": bearings[index],
                "time": base + timedelta(seconds=index),
            }
            for index, bias in enumerate(biases)
        ]

    def test_constant_bias_across_targets_detected(self):
        """各目标偏差一致 ⇒ 判定为可全局补偿。"""
        samples = self._samples(
            "optical",
            [5.68, 5.69, 5.67, 5.70],
            [4000.0, 5000.0, 6000.0, 7000.0],
            [10.0, 20.0, 30.0, 40.0],
            mmsis=["A", "B", "C", "D"],
        )
        result = vts.target_consistency(samples, "optical")
        self.assertEqual(result["target_count"], 4)
        self.assertTrue(result["consistent_across_targets"])

    def test_varying_bias_across_targets_detected(self):
        """各目标偏差不一致 ⇒ 逐目标偏置，不能全局补偿。"""
        samples = self._samples(
            "optical",
            [5.68, 30.92, 12.4],
            [4000.0, 3700.0, 5500.0],
            [10.0, 340.0, 90.0],
            mmsis=["A", "B", "C"],
        )
        result = vts.target_consistency(samples, "optical")
        self.assertFalse(result["consistent_across_targets"])

    def test_single_target_reports_degenerate(self):
        """单目标时判据必须明确报告退化，而不是给出假结论。"""
        samples = self._samples("optical", [5.68], [4000.0], [10.0])
        result = vts.target_consistency(samples, "optical")
        self.assertEqual(result["target_count"], 1)
        self.assertIsNone(result["consistent_across_targets"])
        self.assertIn("退化", result["interpretation"])

    def test_station_offset_criterion_degenerate_without_range_spread(self):
        """无距离跨度时站址判据必须报退化，不能说"站址偏移成立"。"""
        samples = self._samples("optical", [5.68] * 4, [4000.0] * 4, [10.0] * 4)
        result = vts.fit_station_offset(samples, "optical")
        self.assertTrue(result["criterion_degenerate"])
        self.assertIn("退化", result["interpretation"])

    def test_station_offset_criterion_active_with_range_spread(self):
        """有距离跨度且等效 ΔE 一致 ⇒ 判据有效。"""
        samples = self._samples(
            "optical",
            [5.68, 2.84, 1.90, 1.42],
            [4000.0, 8000.0, 12000.0, 16000.0],
            [0.0, 0.0, 0.0, 0.0],
        )
        result = vts.fit_station_offset(samples, "optical")
        self.assertFalse(result["criterion_degenerate"])

    def test_range_correlation_detects_inverse_dependence(self):
        """偏差 ∝ 1/R ⇒ 与 1/距离强相关 ⇒ 支持站址偏移解释。"""
        samples = self._samples(
            "optical",
            [5.68, 2.84, 1.90, 1.42],
            [4000.0, 8000.0, 12000.0, 16000.0],
            [0.0, 0.0, 0.0, 0.0],
        )
        result = vts.correlate_with_range(samples, "optical")
        self.assertGreater(abs(result["pearson_r_with_inverse_range"]), 0.9)

    def test_range_correlation_detects_independence(self):
        """偏差与距离无关 ⇒ 相关系数接近 0 ⇒ 指向零点偏差。"""
        samples = self._samples(
            "optical",
            [5.68, 5.69, 5.67, 5.70],
            [4000.0, 8000.0, 12000.0, 16000.0],
            [10.0, 20.0, 30.0, 40.0],
        )
        result = vts.correlate_with_range(samples, "optical")
        self.assertLess(abs(result["pearson_r_with_inverse_range"]), 0.5)


class CrossSourceGapTest(unittest.TestCase):
    """光电与雷达的源间直接比较（不依赖 AIS）。"""

    def test_gap_is_difference_of_biases(self):
        samples = [
            {"source_kind": "optical", "delta_bearing_deg": 5.68},
            {"source_kind": "radar", "delta_bearing_deg": -2.04},
        ]
        gap = vts.cross_source_gap(samples)
        self.assertAlmostEqual(gap["optical_minus_radar_deg"], 7.72, places=2)
        self.assertGreater(gap["gap_in_tangential_m_at_4km"], 500.0)

    def test_missing_source_reports_note(self):
        gap = vts.cross_source_gap([{"source_kind": "optical", "delta_bearing_deg": 5.68}])
        self.assertIn("note", gap)


class ReplicationTest(unittest.TestCase):
    """跨数据集复现检验。"""

    def _report(self, tag, optical_bias, radar_bias):
        def fit(bias):
            return {
                "sample_count": 10,
                "constant_bearing_bias_deg": bias,
                "bearing_residual_after_removal_deg": {"std": 0.1},
                "range_bias_m": 0.0,
                "range_residual_after_removal_m": {"std": 1.0},
            }

        return {
            "tag": tag,
            "zero_bias_model": {"optical": fit(optical_bias), "radar": fit(radar_bias)},
        }

    def test_replication_detected(self):
        reports = [
            self._report("A", 5.68, -2.04),
            self._report("B", 5.44, -2.10),
        ]
        result = vts.cross_dataset_replication(reports)
        self.assertTrue(result["by_source"]["optical"]["replicated"])
        self.assertTrue(result["by_source"]["radar"]["replicated"])

    def test_non_replication_detected(self):
        reports = [
            self._report("A", 5.68, -2.04),
            self._report("B", 12.00, -9.00),
        ]
        result = vts.cross_dataset_replication(reports)
        self.assertFalse(result["by_source"]["optical"]["replicated"])
        self.assertFalse(result["by_source"]["radar"]["replicated"])

    def test_single_dataset_not_replicable(self):
        reports = [self._report("A", 5.68, -2.04)]
        result = vts.cross_dataset_replication(reports)
        self.assertIsNone(result["by_source"]["optical"]["replicated"])


class GateRecommendationTest(unittest.TestCase):
    """关联门限建议。"""

    def _report(self, tag, bias, residual_std):
        return {
            "tag": tag,
            "zero_bias_model": {
                "optical": {
                    "sample_count": 10,
                    "constant_bearing_bias_deg": bias,
                    "bearing_residual_after_removal_deg": {"std": residual_std},
                    "range_bias_m": 5.0,
                    "range_residual_after_removal_m": {"std": 3.0},
                }
            },
        }

    def test_gate_is_three_sigma(self):
        reports = [self._report("A", 5.68, 0.2)]
        result = vts.association_gate_recommendation(reports)
        entry = result["per_source"]["optical"][0]
        self.assertAlmostEqual(entry["recommended_bearing_gate_deg"], 0.6, places=6)
        self.assertAlmostEqual(entry["recommended_range_gate_m"], 9.0, places=6)

    def test_systematic_bias_reported_separately(self):
        """系统偏差必须单独列出 —— 它决定"要不要补偿"，不是门限的一部分。"""
        reports = [self._report("A", 5.68, 0.2)]
        result = vts.association_gate_recommendation(reports)
        entry = result["per_source"]["optical"][0]
        self.assertAlmostEqual(entry["systematic_bearing_bias_deg"], 5.68, places=6)


class WrapTest(unittest.TestCase):
    """角度归一化。"""

    def test_wrap_keeps_small_negative(self):
        self.assertAlmostEqual(vts._wrap_deg(-5.0), -5.0, places=9)

    def test_wrap_handles_360_boundary(self):
        self.assertAlmostEqual(vts._wrap_deg(359.0), -1.0, places=9)

    def test_wrap_handles_large_difference(self):
        self.assertAlmostEqual(vts._wrap_deg(350.0), -10.0, places=9)


if __name__ == "__main__":
    unittest.main()
