"""关联预备模块测试。

覆盖三条要求：

  1. CDT 跨帧重建（`cdt_tracker`）：帧内去重 + 跨帧匹配；
  2. 跨源坐标系统差（`bias`）：只核查、不自动估计；
  3. 统一时间基准（`timebase`）：三路可比断言。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from association.bias import (  # noqa: E402
    apply_cdt_correction,
    bearing_residual_deg,
    interpolate_ais_polar,
    summarize_cdt_bias,
)
from association.cdt_tracker import (  # noqa: E402
    CdtTracker,
    cluster_frame,
    polar_distance_m,
)
from association.timebase import check_timebase  # noqa: E402


def _det(order, rng, brg, **extra):
    row = {
        "range_m": rng,
        "bearing_deg": brg,
        "east_m": rng,
        "north_m": 0.0,
        "source_row": order,
        "local_track_id": "900001",
        "target_type": "CDT",
    }
    row.update(extra)
    return row


class TestClusterFrame(unittest.TestCase):
    """帧内去重。"""

    def test_close_detections_merge(self):
        pts = [(0, 5000.0, 350.0), (1, 5005.0, 350.1)]
        clusters = cluster_frame(pts, dtheta_deg=0.5, drange_m=60.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0].members), [0, 1])

    def test_distant_detections_stay_separate(self):
        pts = [(0, 5000.0, 350.0), (1, 6000.0, 355.0)]
        clusters = cluster_frame(pts, dtheta_deg=0.5, drange_m=60.0)
        self.assertEqual(len(clusters), 2)

    def test_bearing_wraparound(self):
        pts = [(0, 5000.0, 359.9), (1, 5005.0, 0.1)]
        clusters = cluster_frame(pts, dtheta_deg=0.5, drange_m=60.0)
        self.assertEqual(len(clusters), 1)


class TestCdtTracker(unittest.TestCase):
    """跨帧匹配：匀速直线目标不断轨。"""

    def _frames(self, n=10, start_brg=350.0, speed_dps=0.05):
        base = datetime(2025, 3, 12, 10, 12, 33)
        frames = []
        for i in range(n):
            moment = base + timedelta(seconds=5 * i)
            brg = (start_brg + speed_dps * 5 * i) % 360.0
            # 目标 + 一个远处杂波
            frames.append(
                (
                    float(i + 1),
                    moment,
                    [_det(0, 5000.0, brg), _det(1, 6000.0, 10.0)],
                )
            )
        return frames

    def test_straight_target_stays_one_track(self):
        tracker = CdtTracker()
        tracks = tracker.reconstruct(self._frames())
        # 目标应基本不断轨（允许杂波自成短轨）
        longest = max(len(v) for v in tracks.values())
        self.assertEqual(longest, 10)

    def test_output_point_has_traceability(self):
        tracker = CdtTracker()
        tracks = tracker.reconstruct(self._frames(n=3))
        point = tracks[1][0]
        self.assertIn("member_source_rows", point)
        self.assertIn("radar_frame_index", point)
        self.assertEqual(point["measurement_kind"], "radar_reconstructed")

    def test_gap_beyond_max_gap_kills_track(self):
        base = datetime(2025, 3, 12, 10, 12, 33)
        frames = [
            (1.0, base, [_det(0, 5000.0, 350.0)]),
            (2.0, base + timedelta(seconds=300), [_det(0, 5000.0, 350.1)]),
        ]
        tracker = CdtTracker(max_gap_s=60.0)
        tracks = tracker.reconstruct(frames)
        self.assertEqual(len(tracks), 2)


class TestBias(unittest.TestCase):
    """系统差：只核查、不自动估计。"""

    def test_interpolation_inside_range(self):
        seq = [(0.0, 350.0, 5000.0), (10.0, 351.0, 4990.0)]
        brg, rng = interpolate_ais_polar(seq, 5.0)
        self.assertAlmostEqual(brg, 350.5, places=6)
        self.assertAlmostEqual(rng, 4995.0, places=6)

    def test_interpolation_out_of_range_returns_none(self):
        seq = [(0.0, 350.0, 5000.0), (10.0, 351.0, 4990.0)]
        self.assertIsNone(interpolate_ais_polar(seq, 20.0))
        self.assertIsNone(interpolate_ais_polar(seq, -5.0))

    def test_bearing_residual_wraps(self):
        self.assertAlmostEqual(bearing_residual_deg(1.0, 359.0), 2.0)
        self.assertAlmostEqual(bearing_residual_deg(359.0, 1.0), -2.0)

    def test_stable_evidence_gives_stable_verdict(self):
        rows = [
            {
                "目标类型": "CDT",
                "匹配编号": "1",
                "目标编号": "413203610",
                "帧序号": str(i),
                "起始时间(帧)": "2025-03-12 10:14:00",
                "目标方位(°)": str(355.6),
                "目标距离(米)": str(4850.0),
            }
            for i in range(10)
        ]
        ais = {
            "413203610": [
                (1741745580.0, 358.0, 4830.0),
                (1741745700.0, 358.0, 4830.0),
            ]
        }
        report = summarize_cdt_bias(rows, ais)
        self.assertEqual(report["pair_count"], 10)
        self.assertEqual(report["verdict"], "stable")
        self.assertAlmostEqual(report["mean_dbrg"], -2.4, places=1)

    def test_sparse_evidence_gives_inconclusive(self):
        rows = [
            {
                "目标类型": "CDT",
                "匹配编号": "1",
                "目标编号": "413203610",
                "帧序号": "1",
                "起始时间(帧)": "2025-03-12 10:14:00",
                "目标方位(°)": "355.6",
                "目标距离(米)": "4850.0",
            }
        ]
        ais = {"413203610": [(1741745580.0, 358.0, 4830.0), (1741745700.0, 358.0, 4830.0)]}
        report = summarize_cdt_bias(rows, ais)
        self.assertEqual(report["verdict"], "inconclusive")

    def test_correction_is_explicit_and_non_destructive(self):
        rows = [{"bearing_deg": 355.6, "range_m": 4850.0}]
        corrected = apply_cdt_correction(
            rows, delta_bearing_deg=-2.404, delta_range_m=17.7
        )
        # 原输入不改动
        self.assertEqual(rows[0]["bearing_deg"], 355.6)
        # 修正 = 实测 − delta
        self.assertAlmostEqual(
            corrected[0]["bearing_deg"], (355.6 + 2.404) % 360.0
        )
        self.assertAlmostEqual(corrected[0]["range_m"], 4850.0 - 17.7)
        self.assertTrue(corrected[0]["bias_corrected"])


class TestTimebase(unittest.TestCase):
    """时间基准：三路可比断言。"""

    def _record(self, sensor, time_utc):
        return {
            "source_type": sensor,
            "aligned_time_utc": time_utc,
            "time_basis": "csv_datetime_string",
        }

    def test_comparable_local_timebase(self):
        records = [
            self._record("radar", "2025-03-12 10:12:33.000 (local)"),
            self._record("ais", "2025-03-12 10:12:34.000 (local)"),
            self._record("optical", "2025-03-12 10:12:37.000 (local)"),
        ]
        result = check_timebase(records)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["reasons"], [])

    def test_missing_sensor_fails_explicitly(self):
        records = [self._record("radar", "2025-03-12 10:12:33.000 (local)")]
        result = check_timebase(records)
        self.assertFalse(result["comparable"])
        self.assertTrue(any("ais" in reason for reason in result["reasons"]))

    def test_mismatched_basis_fails_explicitly(self):
        records = [
            dict(
                self._record("radar", "2025-03-12 10:12:33.000 (local)"),
                time_basis="csv_datetime_string",
            ),
            dict(
                self._record("ais", "2025-03-12 10:12:34.000"),
                time_basis="vdm_second",
            ),
            self._record("optical", "2025-03-12 10:12:37.000 (local)"),
        ]
        result = check_timebase(records)
        self.assertFalse(result["comparable"])


if __name__ == "__main__":
    unittest.main()
