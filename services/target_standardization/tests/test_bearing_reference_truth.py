"""**回归保护**：平台航向绝不能叠加到雷达方位上。

用权威 AIS 反算真方位（不依赖 IF 方位码），检验各种候选修正量下
的方位残差：

  - "不修正"必须是最优；
  - 叠加平台航向、叠加 `角度修正` 都必须**显著恶化**残差。

这组断言把"平台航向不参与方位→ENU 换算"这一结论固化下来，
防止后人误把它当成方位修正项。
"""

from __future__ import annotations

import collections
import csv
import math
import os
import struct
import sys
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from standardization.radar_if_header import decode_if_uint32  # noqa: E402

DATA_DIR = os.path.join(ROOT, "data", "mtdsp")
DATA_DIR2 = os.path.join(ROOT, "data", "mtdsp2")

#: IF 帧头给出的雷达自记站址（权威，123/152 帧完全一致）
STATION_LAT = 37.53297
STATION_LON = 121.42020

R_EARTH = 6378137.0


def _parse_time(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _enu(lat, lon):
    dlat = math.radians(lat - STATION_LAT)
    dlon = math.radians(lon - STATION_LON)
    east = R_EARTH * dlon * math.cos(math.radians((lat + STATION_LAT) / 2))
    north = R_EARTH * dlat
    return east, north


def _bearing(east, north):
    return math.degrees(math.atan2(east, north)) % 360.0


def _wrap(deg):
    return (deg + 180.0) % 360.0 - 180.0


def _load_ais(directory):
    tracks = collections.defaultdict(list)
    for name in sorted(os.listdir(directory)):
        if not name.startswith("AIS_Trajectory_"):
            continue
        mmsi = name.replace("AIS_Trajectory_", "").replace(".csv", "")
        with open(os.path.join(directory, name), encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                moment = _parse_time(row.get("时间"))
                if moment is None:
                    continue
                tracks[mmsi].append(
                    (moment, float(row["纬度(°)"]), float(row["经度(°)"]))
                )
    for key in tracks:
        tracks[key].sort(key=lambda item: item[0])
    return tracks


def _ais_at(tracks, mmsi, moment):
    sequence = tracks.get(mmsi)
    if not sequence or moment < sequence[0][0] or moment > sequence[-1][0]:
        return None
    low, high = 0, len(sequence) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if sequence[mid][0] <= moment:
            low = mid
        else:
            high = mid
    t0, la0, ln0 = sequence[low]
    t1, la1, ln1 = sequence[high]
    if t1 == t0:
        return la0, ln0
    ratio = (moment - t0).total_seconds() / (t1 - t0).total_seconds()
    return la0 + (la1 - la0) * ratio, ln0 + (ln1 - ln0) * ratio


def _platform_heading(directory):
    """IF CSV 的平台航向均值（度）。"""
    path = os.path.join(directory, "IF_2003_AT_413203610.csv")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    starts = [
        i for i, row in enumerate(rows) if (row.get("帧序号") or "").strip()
    ]
    values = []
    for k, i in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(rows)
        for j in range(i, end):
            raw = decode_if_uint32((rows[j].get("航向角(°)") or "").strip())
            if raw is not None:
                values.append(raw * 0.1)
    return sum(values) / len(values) if values else None


def _residual_stats(directory):
    """返回 (候选修正量 → |Δ| 中位数列表) 与样本数。"""
    cd_path = os.path.join(directory, "CutDataTarget.csv")
    tracks = _load_ais(directory)
    heading = _platform_heading(directory)
    angle_correction = 344.0

    samples = []
    with open(cd_path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            moment = _parse_time(row.get("起始时间(帧)"))
            if moment is None:
                continue
            try:
                true_bearing = float(row["目标方位(°)"])
            except (ValueError, KeyError):
                continue
            mmsi = (row.get("目标编号") or "").strip()
            if not mmsi.isdigit():
                continue          # 900xxx 内部检测号无 AIS 对应
            position = _ais_at(tracks, mmsi, moment)
            if position is None:
                continue
            east, north = _enu(*position)
            samples.append((true_bearing, _bearing(east, north)))

    candidates = {
        "none": 0.0,
        "platform_heading": heading,
        "angle_correction": angle_correction,
        "heading_minus_correction": heading - angle_correction,
    }
    stats = {}
    for name, correction in candidates.items():
        residuals = sorted(
            abs(_wrap(implied - (true_bearing + correction)))
            for true_bearing, implied in samples
        )
        stats[name] = residuals[len(residuals) // 2] if residuals else None
    return stats, len(samples)


class TestPlatformHeadingNotAppliedToBearing(unittest.TestCase):
    """方位基准是真北：叠加平台航向必须恶化残差。"""

    def _check(self, directory, label):
        if not os.path.exists(os.path.join(directory, "CutDataTarget.csv")):
            self.skipTest(f"缺少 {label} 数据")
        stats, count = _residual_stats(directory)
        self.assertGreater(count, 20, "可比对样本过少")

        baseline = stats["none"]
        self.assertIsNotNone(baseline)
        # 不修正时方位残差应在半度量级以内
        self.assertLess(baseline, 1.0, f"{label}: 不修正的残差中位数 {baseline}° 过大")

        # 叠加平台航向必须显著恶化
        self.assertGreater(
            stats["platform_heading"], baseline * 5.0,
            f"{label}: 叠加平台航向后残差 {stats['platform_heading']}° "
            f"相对不修正的 {baseline}° 改善了，结论有误",
        )
        # 叠加角度修正必须显著恶化
        self.assertGreater(
            stats["angle_correction"], baseline * 5.0,
            f"{label}: 叠加角度修正后残差 {stats['angle_correction']}° 未恶化",
        )
        # "不修正"必须优于所有其他候选
        for name, value in stats.items():
            if name == "none":
                continue
            self.assertLess(
                baseline, value,
                f"{label}: 候选 {name} ({value}°) 优于不修正 ({baseline}°)",
            )

    def test_03_05_bearing_needs_no_correction(self):
        self._check(DATA_DIR, "03-05")

    def test_03_12_bearing_needs_no_correction(self):
        self._check(DATA_DIR2, "03-12")


class TestSpatialAlignerNeverAppliesPlatformHeading(unittest.TestCase):
    """对齐层：平台航向只作附加记录，绝不改变 ENU 结果。"""

    def _config(self, platform_state=None):
        from alignment.config import AlignmentConfig

        data = {
            "site_frame": {
                "origin_latitude_deg": 37.53297,
                "origin_longitude_deg": 121.41990,
            },
            "radar_station": {
                "station_latitude_deg": 37.53297,
                "station_longitude_deg": 121.41990,
                "install_heading_deg": 344.0,
            },
            # 本船艏向也刻意给一个非零值，确保它没被误用
            "own_ship_heading_deg": 352.96,
        }
        if platform_state is not None:
            data["platform_state"] = platform_state
        return AlignmentConfig.from_dict(data)

    def _record(self):
        return {
            "source_id": "radar_test",
            "source_type": "radar_cutdata",
            "local_track_id": "413203610",
            "state_time_raw": "2025-03-05 10:36:22",
            "radar_frame_index": 37.0,
            "position": {
                "range_m": 3875.0,
                "bearing_rad": math.radians(30.0),
                "bearing_reference": "T",
            },
        }

    def test_install_heading_and_own_heading_are_not_applied(self):
        """即使配置了安装方位与本船艏向，T 参考方位也不得被叠加。"""
        from alignment.spatial import SpatialAligner

        aligner = SpatialAligner(self._config())
        record = self._record()
        aligner.align_record(record)
        self.assertEqual(record["alignment_status"], "ok")
        self.assertEqual(record["alignment_bearing_reference_used"], "T")
        self.assertFalse(record["alignment_install_heading_applied"])

        baseline = self._record()
        SpatialAligner(self._config()).align_record(baseline)
        self.assertAlmostEqual(record["east_m"], baseline["east_m"], places=9)
        self.assertAlmostEqual(record["north_m"], baseline["north_m"], places=9)

    def test_platform_state_does_not_change_enu(self):
        """接入平台状态前后，ENU 结果必须逐位相同。"""
        import os
        import tempfile

        from alignment.spatial import SpatialAligner

        header = (
            "帧序号,脉冲序号,时间,航向角(°),方位码(0-449999),速度(nm/h),"
            "纬度(北纬)(°),经度(东经)(°)\n"
        )
        body = (
            "37,1,2025-03-05 10:36:22,4.943781E-42,3209,4.203895E-45,"
            "37.53297,121.4202\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "IF_x.csv"), "w", encoding="utf-8") as h:
                h.write(header)
                h.write(body)
            with_state = self._config(
                {"if_directory": tmp, "if_filename": "IF_x.csv"}
            )
            record = self._record()
            SpatialAligner(with_state).align_record(record)
            self.assertAlmostEqual(record["platform_heading_deg"], 352.8, places=6)

            baseline = self._record()
            SpatialAligner(self._config()).align_record(baseline)
            self.assertAlmostEqual(record["east_m"], baseline["east_m"], places=9)
            self.assertAlmostEqual(record["north_m"], baseline["north_m"], places=9)


if __name__ == "__main__":
    unittest.main()
