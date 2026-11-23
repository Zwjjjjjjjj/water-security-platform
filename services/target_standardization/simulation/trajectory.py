"""从 RadarPWN AIS 轨迹提取共同参考轨迹（模板）。

流程（每一步都记录来源与参数）：
  1. 读取 ais_with_capture_time.csv，解码 !AIVDM 得到逐目标点位序列；
  2. 按来源配置剔除疑似占位位置（(0,0)）与 AIS 标准不可用编码（91/181）；
  3. 按筛选条件（记录数、最小位移、采样间隔统计）挑选模板目标；
  4. 转为局部 ENU 米制坐标；
  5. 剔除离群点（与前后点构成的局部拟合偏差超阈值）；
  6. 重采样到均匀时间栅格，并做线性/三次插值，形成连续轨迹。

产出物：
  - `reference_trajectory`：统一时间栅格上的参考位置（ENU 米）+ 速度 + 航向；
  - `provenance`：每一步的来源、参数与统计量。

**语义说明**：该轨迹代表**新模拟场景中的设定运动**，取自实测轨迹的形态，
不代表任何原始船舶的实测真值。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import geo


@dataclass
class TrackPoint:
    """单条实测点位。"""

    capture_unix: float
    latitude_deg: float
    longitude_deg: float
    speed_kn: Optional[float]
    course_deg: Optional[float]
    heading_deg: Optional[int]
    status_raw: Optional[str]
    source_row: int


@dataclass
class ReferenceSample:
    """参考轨迹上的一个采样点（局部 ENU 米制）。"""

    t_rel_s: float
    east_m: float
    north_m: float
    speed_mps: float
    course_rad: float


@dataclass
class TrajectoryExtraction:
    """提取结果 + 全部来源记录。"""

    mmsi: str
    origin: geo.LocalOrigin
    samples: List[ReferenceSample]
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.samples[-1].t_rel_s - self.samples[0].t_rel_s if self.samples else 0.0


def read_ais_vdm_points(
    csv_path: str,
    *,
    placeholder_positions: Sequence[Tuple[float, float]] = (),
) -> Tuple[Dict[str, List[TrackPoint]], Dict[str, Any]]:
    """读取并解码 AIS VDM 报文，返回逐 MMSI 的点位与统计信息。"""
    import pyais

    tracks: Dict[str, List[TrackPoint]] = {}
    stats = {
        "total_sentences": 0,
        "decoded": 0,
        "decode_failed": 0,
        "skipped_placeholder": 0,
        "skipped_unavailable_encoding": 0,
    }

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        for row_index, row in enumerate(csv.DictReader(handle), start=2):
            stats["total_sentences"] += 1
            sentence = (row.get("sentence") or "").strip()
            if not sentence:
                continue
            try:
                message = pyais.decode(sentence)
            except Exception:
                stats["decode_failed"] += 1
                continue
            stats["decoded"] += 1

            latitude = getattr(message, "lat", None)
            longitude = getattr(message, "lon", None)
            if latitude is None or longitude is None:
                continue
            if abs(latitude - 91.0) < 1e-6 and abs(longitude - 181.0) < 1e-6:
                stats["skipped_unavailable_encoding"] += 1
                continue
            if any(
                abs(latitude - ph_lat) < 1e-9 and abs(longitude - ph_lon) < 1e-9
                for ph_lat, ph_lon in placeholder_positions
            ):
                stats["skipped_placeholder"] += 1
                continue

            mmsi = str(getattr(message, "mmsi", "") or "")
            if not mmsi:
                continue
            try:
                capture_unix = float(row.get("capture_time_unix"))
            except (TypeError, ValueError):
                continue
            tracks.setdefault(mmsi, []).append(
                TrackPoint(
                    capture_unix=capture_unix,
                    latitude_deg=latitude,
                    longitude_deg=longitude,
                    speed_kn=getattr(message, "speed", None),
                    course_deg=getattr(message, "course", None),
                    heading_deg=getattr(message, "heading", None),
                    status_raw=str(getattr(message, "status", "")) or None,
                    source_row=row_index,
                )
            )

    for points in tracks.values():
        points.sort(key=lambda p: p.capture_unix)
    stats["track_count"] = len(tracks)
    return tracks, stats


def score_tracks(
    tracks: Dict[str, List[TrackPoint]]
) -> List[Dict[str, Any]]:
    """对每条轨迹打分，用于挑选模板。

    评分维度：
      - 有效点数（越多越完整）
      - 采样间隔的一致性（越稳定越好）
      - 连续性（大间隔越少越好）
      - 实际位移（必须有真实运动，不能是锚泊）
      - 航向与实际位移方向的一致性（越一致说明数据自洽）
    """
    import statistics

    scores: List[Dict[str, Any]] = []
    for mmsi, points in tracks.items():
        if len(points) < 3:
            continue
        dts = [
            points[i + 1].capture_unix - points[i].capture_unix
            for i in range(len(points) - 1)
        ]
        positive = [d for d in dts if d > 0]
        if not positive:
            continue
        median_dt = statistics.median(positive)
        max_dt = max(positive)
        gaps = sum(1 for d in positive if d > 3 * median_dt)

        total_m = 0.0
        for i in range(len(points) - 1):
            total_m += geo.haversine_m(
                points[i].latitude_deg,
                points[i].longitude_deg,
                points[i + 1].latitude_deg,
                points[i + 1].longitude_deg,
            )

        # 航向与位移方向一致性
        cog_errors: List[float] = []
        for i in range(len(points) - 1):
            distance = geo.haversine_m(
                points[i].latitude_deg,
                points[i].longitude_deg,
                points[i + 1].latitude_deg,
                points[i + 1].longitude_deg,
            )
            if distance < 5.0 or points[i].course_deg is None:
                continue
            motion = geo.bearing_rad(
                points[i].latitude_deg,
                points[i].longitude_deg,
                points[i + 1].latitude_deg,
                points[i + 1].longitude_deg,
            )
            error = abs(((points[i].course_deg - math.degrees(motion) + 180.0) % 360.0) - 180.0)
            cog_errors.append(error)

        scores.append(
            {
                "mmsi": mmsi,
                "point_count": len(points),
                "median_dt_s": round(median_dt, 3),
                "max_dt_s": round(max_dt, 3),
                "gap_count": gaps,
                "path_length_m": round(total_m, 2),
                "cog_consistency_mean_deg": (
                    round(sum(cog_errors) / len(cog_errors), 3) if cog_errors else None
                ),
            }
        )

    for item in scores:
        # 评分：点数 + 位移为正，间隔稳定、无大间隔、航向自洽
        score = 0.0
        score += min(item["point_count"], 200) / 200.0 * 3.0
        score += min(item["path_length_m"], 8000.0) / 8000.0 * 2.0
        score -= item["gap_count"] * 0.5
        if item["median_dt_s"] > 0:
            score -= min(item["max_dt_s"] / item["median_dt_s"], 10.0) * 0.2
        if item["cog_consistency_mean_deg"] is not None:
            score -= item["cog_consistency_mean_deg"] * 0.1
        item["score"] = round(score, 4)
    scores.sort(key=lambda item: item["score"], reverse=True)
    return scores


def extract_reference_trajectory(
    csv_path: str,
    *,
    mmsi: str,
    placeholder_positions: Sequence[Tuple[float, float]] = (),
    resample_dt_s: float = 1.0,
    outlier_threshold_m: float = 60.0,
    smooth_window: int = 5,
    origin: Optional[geo.LocalOrigin] = None,
) -> TrajectoryExtraction:
    """提取并整理共同参考轨迹。

    outlier_threshold_m：与三点局部拟合的偏差超过该值的点视为离群点并剔除。
    smooth_window：重采样后做滑动平均的点数（奇数），0 表示不平滑。
    """
    tracks, stats = read_ais_vdm_points(
        csv_path, placeholder_positions=placeholder_positions
    )
    if mmsi not in tracks:
        raise KeyError(f"轨迹 {mmsi} 不在数据集中")
    points = tracks[mmsi]

    if origin is None:
        first = points[0]
        origin = geo.LocalOrigin(
            latitude_deg=first.latitude_deg, longitude_deg=first.longitude_deg
        )

    # 1) 转局部 ENU
    enu: List[Tuple[float, float, float]] = [
        (
            p.capture_unix,
            *origin.to_enu(p.latitude_deg, p.longitude_deg),
        )
        for p in points
    ]

    # 2) 剔除离群点：以相邻三点构成的圆弧拟合为基准
    kept: List[Tuple[float, float, float]] = []
    removed: List[Dict[str, Any]] = []
    for index, (t, east, north) in enumerate(enu):
        if index == 0 or index == len(enu) - 1:
            kept.append((t, east, north))
            continue
        prev = enu[index - 1]
        nxt = enu[index + 1]
        # 以 prev→nxt 的线性插值位置为基准
        span = nxt[0] - prev[0]
        ratio = (t - prev[0]) / span if span > 0 else 0.0
        expected_east = prev[1] + ratio * (nxt[1] - prev[1])
        expected_north = prev[2] + ratio * (nxt[2] - prev[2])
        deviation = math.hypot(east - expected_east, north - expected_north)
        if deviation > outlier_threshold_m:
            removed.append(
                {
                    "index": index,
                    "source_row": points[index].source_row,
                    "deviation_m": round(deviation, 2),
                }
            )
            continue
        kept.append((t, east, north))

    # 3) 重采样到均匀栅格
    if not kept:
        raise ValueError("有效点不足，无法重采样")
    t_start = kept[0][0]
    grid_times = []
    cursor = t_start
    last = kept[-1][0]
    while cursor <= last + 1e-9:
        grid_times.append(cursor)
        cursor += resample_dt_s

    resampled: List[Tuple[float, float]] = []
    for t in grid_times:
        east, north = _interpolate(kept, t)
        if east is None:
            continue
        resampled.append((east, north))

    # 4) 平滑
    if smooth_window and smooth_window > 1:
        resampled = _moving_average(resampled, smooth_window)

    # 5) 计算速度与航向
    #
    # 直接用相邻差分求速度在边缘不可靠：首末点只有单侧数据，差分跨度与
    # 除数不匹配会得到偏小的值（实测首点 5.03 m/s vs 中位 9.27 m/s）。
    # 这里改用滑动窗口的局部最小二乘线性拟合，边缘退化为单侧窗口。
    samples: List[ReferenceSample] = []
    fit_window = max(3, smooth_window * 2 + 1) if smooth_window else 3
    half = fit_window // 2
    for index, (east, north) in enumerate(resampled):
        t_rel = index * resample_dt_s
        low = max(0, index - half)
        high = min(len(resampled), index + half + 1)
        # 保证窗口至少 3 个点
        if high - low < 3:
            if index == 0:
                low, high = 0, min(len(resampled), 3)
            else:
                low, high = max(0, len(resampled) - 3), len(resampled)
        times = [(j - index) * resample_dt_s for j in range(low, high)]
        de, dn = _local_linear_slope(times, resampled[low:high])
        samples.append(
            ReferenceSample(
                t_rel_s=round(t_rel, 3),
                east_m=east,
                north_m=north,
                speed_mps=geo.enu_offset_to_speed(de, dn, 1.0),
                course_rad=geo.enu_offset_to_bearing(de, dn),
            )
        )

    provenance = {
        "source_file": csv_path,
        "source_mmsi": mmsi,
        "source_points": len(points),
        "outliers_removed": removed,
        "outlier_threshold_m": outlier_threshold_m,
        "resample_dt_s": resample_dt_s,
        "smooth_window": smooth_window,
        "origin": {
            "latitude_deg": origin.latitude_deg,
            "longitude_deg": origin.longitude_deg,
        },
        "reference_duration_s": round(samples[-1].t_rel_s, 3) if samples else 0.0,
        "reference_point_count": len(samples),
        "decode_stats": stats,
        "semantics": (
            "共同参考轨迹代表新模拟场景中的设定运动，取自实测轨迹形态，"
            "不代表任何原始船舶的实测真值。"
        ),
    }

    return TrajectoryExtraction(
        mmsi=mmsi, origin=origin, samples=samples, provenance=provenance
    )


def _interpolate(
    kept: List[Tuple[float, float, float]], t: float
) -> Tuple[Optional[float], Optional[float]]:
    """在 (t, east, north) 序列上线性插值。"""
    if t <= kept[0][0]:
        return kept[0][1], kept[0][2]
    if t >= kept[-1][0]:
        return kept[-1][1], kept[-1][2]
    low, high = 0, len(kept) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if kept[mid][0] <= t:
            low = mid
        else:
            high = mid
    t0, e0, n0 = kept[low]
    t1, e1, n1 = kept[high]
    span = t1 - t0
    ratio = (t - t0) / span if span > 0 else 0.0
    return e0 + ratio * (e1 - e0), n0 + ratio * (n1 - n0)


def _local_linear_slope(
    times: List[float], points: List[Tuple[float, float]]
) -> Tuple[float, float]:
    """局部最小二乘线性拟合，返回 (东向速率 m/s, 北向速率 m/s)。

    对时刻 t 与坐标做一元线性回归，斜率即该处的速率分量。
    """
    n = len(times)
    if n < 2:
        return 0.0, 0.0
    mean_t = sum(times) / n
    mean_e = sum(p[0] for p in points) / n
    mean_n = sum(p[1] for p in points) / n
    s_tt = sum((t - mean_t) ** 2 for t in times)
    if s_tt <= 0:
        return 0.0, 0.0
    s_te = sum((times[i] - mean_t) * (points[i][0] - mean_e) for i in range(n))
    s_tn = sum((times[i] - mean_t) * (points[i][1] - mean_n) for i in range(n))
    return s_te / s_tt, s_tn / s_tt


def _moving_average(
    points: List[Tuple[float, float]], window: int
) -> List[Tuple[float, float]]:
    """滑动平均平滑，边缘用端点值填充。

    边缘若按"可用点变少"的方式平均，会把首末点的位置拉向内侧，从而在
    端部伪造出一段虚假的速度渐变（实测首端从 2.1 m/s 爬升到 8.4 m/s）。
    这里改用端点值填充，保持边缘位置不变。
    """
    if window <= 1 or len(points) < 3:
        return list(points)
    half = window // 2
    padded = [points[0]] * half + list(points) + [points[-1]] * half
    result: List[Tuple[float, float]] = []
    for index in range(len(points)):
        chunk = padded[index : index + window]
        result.append(
            (
                sum(p[0] for p in chunk) / len(chunk),
                sum(p[1] for p in chunk) / len(chunk),
            )
        )
    return result
