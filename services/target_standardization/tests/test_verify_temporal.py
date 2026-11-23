"""MTDSP 时间对齐验证（verify_mtdsp_temporal）单元测试。

覆盖：
  - `_bracket` 严格复现对齐模块的语义（含重复时间戳与末点边界）；
  - 独立栅格重算与模块产出一致；
  - gap 拒绝判定一致；
  - 时钟一致性检查；
  - 跨源时序残差。
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from verification import verify_mtdsp_temporal as vt  # noqa: E402


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


class BracketTest(unittest.TestCase):
    """`_bracket` 必须严格复现对齐模块的语义。"""

    def test_exact_match_returns_same_index(self):
        times = [_dt(f"2025-03-12 10:00:0{i}") for i in range(5)]
        low, high = vt._bracket(times, times[2])
        self.assertEqual(low, high)
        self.assertEqual(low, 2)

    def test_between_returns_bracket(self):
        times = [_dt(f"2025-03-12 10:00:0{i}") for i in range(5)]
        low, high = vt._bracket(times, _dt("2025-03-12 10:00:02") + timedelta(seconds=0.5))
        self.assertEqual(low, 2)
        self.assertEqual(high, 3)

    def test_last_point_is_never_exact(self):
        """模块的末点永远不会被判为精确点。

        二分收敛到 (n-2, n-1)，`times[n-2] != target` ⇒ 返回区间而非
        (low, low)。若该间隔超限，末点栅格时刻被判 gap_exceeds_limit。
        这是模块的既定行为，独立验证必须原样复现。
        """
        times = [_dt(f"2025-03-12 10:00:0{i}") for i in range(5)]
        low, high = vt._bracket(times, times[-1])
        self.assertNotEqual(low, high)
        self.assertEqual(high, len(times) - 1)

    def test_duplicate_timestamps_resolve_deterministically(self):
        """重复时间戳时二分结果确定且可复现。

        MTDSP 03-12 光电 Inf/TV 双设备共享时间戳但位置不同，
        模块的行为由二分决定 —— 独立实现必须复现，否则产生假差异。
        实测行为（times = [0s, 5s, 10s, 10s, 15s]）：
          - 落在重复段**之前**的栅格时刻 ⇒ bracket 指向重复段的**第一条**；
          - 正好等于重复时刻 ⇒ 指向重复段的**最后一条**（exact）。
        """
        times = [
            vt._parse_local("2025-03-12 10:00:00"),
            vt._parse_local("2025-03-12 10:00:05"),
            vt._parse_local("2025-03-12 10:00:10"),
            vt._parse_local("2025-03-12 10:00:10"),
            vt._parse_local("2025-03-12 10:00:15"),
        ]
        low, high = vt._bracket(times, vt._parse_local("2025-03-12 10:00:07"))
        self.assertEqual((low, high), (1, 2))

        low, high = vt._bracket(times, vt._parse_local("2025-03-12 10:00:10"))
        self.assertEqual((low, high), (3, 3))

        low, high = vt._bracket(times, vt._parse_local("2025-03-12 10:00:12"))
        self.assertEqual((low, high), (3, 4))


class IndependentGridTest(unittest.TestCase):
    """独立栅格重算。"""

    def _records(self):
        base = _dt("2025-03-12 10:00:00")
        rows = []
        for index in range(5):
            rows.append(
                {
                    "source_id": "src",
                    "local_track_id": "T1",
                    "state_time_raw": (base + timedelta(seconds=index * 2)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "east_m": str(index * 10.0),
                    "north_m": str(index * 100.0),
                }
            )
        return rows

    def test_grid_generated_at_one_second(self):
        grids = vt.independent_grid(self._records())
        points = grids[("src", "T1")]
        self.assertGreater(len(points), 0)
        moments = [p[0] for p in points]
        # 至少包含若干 1 s 间隔的栅格点
        gaps = [
            (moments[i + 1] - moments[i]).total_seconds()
            for i in range(len(moments) - 1)
        ]
        self.assertTrue(all(abs(g - 1.0) < 1e-6 for g in gaps))

    def test_interpolated_value_is_linear_in_enu(self):
        grids = vt.independent_grid(self._records())
        points = {p[0]: (p[1], p[2]) for p in grids[("src", "T1")]}
        # 原始点 0 s (0,0)、2 s (10,100)：1 s 处应为 (5,50)
        moment = _dt("2025-03-12 10:00:01")
        self.assertAlmostEqual(points[moment][0], 5.0, places=9)
        self.assertAlmostEqual(points[moment][1], 50.0, places=9)

    def test_gap_exceeding_limit_produces_no_points(self):
        """间隔超过 max_interpolation_gap_s ⇒ 该区间不产生栅格点。"""
        base = _dt("2025-03-12 10:00:00")
        rows = [
            {
                "source_id": "src",
                "local_track_id": "T1",
                "state_time_raw": base.strftime("%Y-%m-%d %H:%M:%S"),
                "east_m": "0.0",
                "north_m": "0.0",
            },
            {
                "source_id": "src",
                "local_track_id": "T1",
                "state_time_raw": (base + timedelta(seconds=60)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "east_m": "60.0",
                "north_m": "0.0",
            },
        ]
        grids = vt.independent_grid(rows)
        points = grids[("src", "T1")]
        # 只应有首点与末点（末点因 bracket 落在超长区间而无插值点）
        for moment, _, _ in points:
            delta = (moment - base).total_seconds()
            self.assertTrue(delta == 0 or delta == 60, f"意外栅格点 {delta}s")

    def test_single_observation_kept(self):
        rows = [
            {
                "source_id": "src",
                "local_track_id": "T1",
                "state_time_raw": "2025-03-12 10:00:00",
                "east_m": "1.0",
                "north_m": "2.0",
            }
        ]
        grids = vt.independent_grid(rows)
        self.assertEqual(len(grids[("src", "T1")]), 1)


class GridVerificationTest(unittest.TestCase):
    """模块产出与独立重算的比对判据。"""

    def test_identical_grids_pass(self):
        moment = _dt("2025-03-12 10:00:01")
        grid_rows = [
            {
                "source_id": "src",
                "local_track_id": "T1",
                "time_utc": "2025-03-12 10:00:01",
                "east_m": "5.0",
                "north_m": "50.0",
                "interpolation_method": "linear",
            }
        ]
        independent = {("src", "T1"): [(moment, 5.0, 50.0)]}
        result = vt.verify_grid(grid_rows, independent)
        self.assertTrue(result["passed"])
        self.assertEqual(result["only_in_module"], 0)
        self.assertEqual(result["only_in_independent"], 0)

    def test_position_mismatch_fails(self):
        moment = _dt("2025-03-12 10:00:01")
        grid_rows = [
            {
                "source_id": "src",
                "local_track_id": "T1",
                "time_utc": "2025-03-12 10:00:01",
                "east_m": "5.0",
                "north_m": "50.0",
                "interpolation_method": "linear",
            }
        ]
        independent = {("src", "T1"): [(moment, 6.0, 50.0)]}
        result = vt.verify_grid(grid_rows, independent)
        self.assertFalse(result["passed"])
        self.assertAlmostEqual(result["max_position_residual_m"], 1.0, places=6)

    def test_rejected_points_counted_separately(self):
        """`interpolation_method=none` 的候选时刻不参与位置比对，
        但必须被独立重算同样拒绝。"""
        grid_rows = [
            {
                "source_id": "src",
                "local_track_id": "T1",
                "time_utc": "2025-03-12 10:00:05",
                "east_m": "",
                "north_m": "",
                "interpolation_method": "none",
                "interpolation_reason": "gap_exceeds_limit",
            }
        ]
        independent = {("src", "T1"): []}
        result = vt.verify_grid(grid_rows, independent)
        self.assertEqual(result["module_rejected_grid_points"], 1)
        self.assertEqual(result["rejection_inconsistent_count"], 0)


class ClockConsistencyTest(unittest.TestCase):
    """时钟一致性检查的统计逻辑。"""

    def test_stats_computed(self):
        stats = vt._stats([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(stats["count"], 4)
        self.assertAlmostEqual(stats["median"], 2.5, places=9)
        self.assertAlmostEqual(stats["mean"], 2.5, places=9)

    def test_empty_stats(self):
        self.assertEqual(vt._stats([])["count"], 0)

    def test_parse_local_handles_local_suffix(self):
        moment = vt._parse_local("2025-03-12 10:00:01.000 (local)")
        self.assertEqual(moment, _dt("2025-03-12 10:00:01"))

    def test_parse_local_handles_fraction(self):
        moment = vt._parse_local("2025-03-12 10:00:01.500")
        self.assertEqual(moment, _dt("2025-03-12 10:00:01") + timedelta(seconds=0.5))


if __name__ == "__main__":
    unittest.main()
