"""航迹导出：把对齐后的逐点记录整理成**按传感器分组的完整航迹**。

## 为什么需要这一层

对齐流水线产出的是**逐条记录**（`aligned_records.csv`）与插值栅格
（`aligned_grid_records.csv`）。但航迹关联的输入单位是**航迹**：一个
目标在某个传感器上的完整运动序列。直接拿栅格做关联会有三个问题：

  1. 栅格是**均匀时间采样**的，不同传感器的采样率与起止范围不同，
     同一物理目标在不同传感器上的栅格点几乎不对齐；
  2. 栅格点是**插值产物**，不是真实观测。拿插值点做关联，等于把
     插值误差引入关联门限，掩盖真实的测量误差；
  3. 栅格丢掉了**观测本身的语义**（测量类型、误差上界、原始行号），
     关联后无法回溯。

因此本模块从**原始观测**出发，按 (传感器类型, 航迹号) 分组，
输出每条航迹的完整点序列 + 关联所需的运动学量与元数据。

## 三种航迹

  - `radar`    雷达（MTDSP CutDataTarget）
  - `ais`      AIS（MTDSP AIS_Trajectory）
  - `optical`  光电（MTDSP Photo_ScreenShot，Inf/TV 已分离）

## 运动学量的补齐

MTDSP 雷达与光电的原始数据**只有位置，没有速度与航向**；
AIS 两者都有。为了让三种航迹在关联时可比，本模块对缺项做**差分推导**：

  - 速度：相邻两点 ENU 位移 / 时间差（中点法，端点用单侧差分）
  - 航向：相邻两点 ENU 位移的方位角

推导量在输出中以 `speed_source` / `course_source` 标注为 `derived`，
与实测（`reported`）严格区分，**不混为一谈**。

## 雷达 900xxx 编号的特别处理

MTDSP 的 CDT（雷达检测）行用 `目标编号 = 900xxx` 内部号。经核验
（见 `standardization/radar_cutdata_adapter.py` 第 3 条），**该编号
不是稳定航迹号**：同一物理目标会在多个内部号之间切换，编号会整块
跳变。因此：

  - `900xxx` 分组出的序列**不是航迹**，本模块将其标记为
    `track_stability = "unstable_internal_id"`；
  - 同时提供 `radar_detections`（逐帧检测点，保留帧号）供下游做
    真正的跨帧聚类重建。

只有 `AT`（`目标编号` = MMSI）与 AIS、光电的编号才被视为稳定航迹号。

## 输出

  tracks/<sensor>/<track_id>.csv   每条航迹一个文件（完整点序列）
  tracks/track_index.csv           航迹清单（关联的候选集）
  tracks/track_summary.json        汇总统计
  tracks/radar_detections.csv      雷达逐帧检测点（未聚类）
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .spatial import STATUS_UNUSABLE_POSITION

#: 传感器类型 → 输出子目录名
SENSOR_DIRS = OrderedDict(
    (
        ("radar", "radar"),
        ("ais", "ais"),
        ("optical", "optical"),
    )
)

#: 雷达 CDT 行的内部编号前缀（不是稳定航迹号）
UNSTABLE_ID_PREFIX = "900"

#: 差分推导运动学量时的最大允许时间间隔（秒）。
#: 超过该间隔说明两点之间可能已经换了目标，推导速度不可信，置空。
MAX_DERIVE_DT_S = 30.0

#: 差分推导速度的物理上限（m/s）。超过则视为编号跳变，置空并标记。
MAX_PLAUSIBLE_SPEED_MPS = 60.0

#: 量化跳变检测：某段的位移超过本航迹**非零位移中位数**的该倍数时，
#: 判为量化/回传跳变而非真实运动，该段不参与运动学推导。
#:
#: 依据：光电测距是**整数米档位**，位置呈阶梯状 —— 连续若干点完全相同，
#: 然后突然跳 120–145 m。若按中点法跨跳变差分，推导速度会被虚增到
#: 12–13 m/s，而同期 AIS 实测仅 4.7 m/s。
QUANTIZATION_JUMP_FACTOR = 4.0

#: 判定"非零位移中位数"所需的最小非零段数（太少则不启用该判据）。
MIN_NONZERO_SEGMENTS_FOR_JUMP_DETECTION = 5

#: 一个点向两侧各最多回溯多少个段来寻找可用的速度估计。
MAX_SEGMENT_LOOKBACK = 2

#: 滑动窗口拟合速度的半窗宽（秒）与最小窗口跨度（秒）。
#:
#: 为什么用窗口拟合而非逐段差分：光电位置在**距离（1 m 档位）与方位
#: （0.1°，5.2 km 处约 9.19 m）**两个维度都被量化，逐段差分被量化噪声
#: 主导 —— 实测逐段位移在 0 / 8.3 / 16.3 / 51 / 121 / 145 m 之间跳，
#: 对应瞬时速度 0–29 m/s，而真实速度约 4.7 m/s（AIS 实测）。
#:
#: 在 ±30 s 窗口内对 (t, E)、(t, N) 各做一次最小二乘直线拟合，
#: 斜率即速度矢量。量化噪声按 1/√N 衰减，而真实匀速运动在窗口内
#: 保持线性，斜率不受量化影响。
VELOCITY_WINDOW_HALF_S = 30.0
VELOCITY_WINDOW_MIN_SPAN_S = 12.0
VELOCITY_WINDOW_MIN_POINTS = 4

TRACK_POINT_FIELDS = [
    "track_id",
    "sensor_type",
    "source_id",
    "point_index",
    "time_utc",
    "time_epoch_s",
    "latitude_deg",
    "longitude_deg",
    "east_m",
    "north_m",
    "up_m",
    "speed_mps",
    "speed_source",
    "course_deg",
    "course_source",
    "range_m",
    "bearing_deg",
    "position_type",
    "alignment_status",
    "error_bound_planar_m",
    "radar_measurement_kind",
    "radar_target_type",
    "radar_frame_index",
    "optical_device",
    "optical_record_kind",
    "optical_detection_index",
    "platform_heading_deg",
    "source_file",
    "source_row",
    "record_id",
]

TRACK_INDEX_FIELDS = [
    "track_id",
    "sensor_type",
    "source_id",
    "local_track_id",
    "track_kind",
    "track_stability",
    "point_count",
    "usable_point_count",
    "start_time_utc",
    "end_time_utc",
    "duration_s",
    "median_dt_s",
    "mean_speed_mps",
    "max_speed_mps",
    "mean_course_deg",
    "centroid_east_m",
    "centroid_north_m",
    "path_length_m",
    "straight_line_distance_m",
    "has_reported_speed",
    "has_reported_course",
    "quantization_jump_count",
    "quantization_jump_ratio",
    "first_record_id",
    "last_record_id",
]

RADAR_DETECTION_FIELDS = [
    "source_id",
    "radar_frame_index",
    "time_utc",
    "time_epoch_s",
    "local_track_id",
    "target_type",
    "measurement_kind",
    "range_m",
    "bearing_deg",
    "east_m",
    "north_m",
    "up_m",
    "latitude_deg",
    "longitude_deg",
    "error_bound_planar_m",
    "source_row",
]


def _parse_time(text: str) -> Optional[datetime]:
    """解析对齐流水线写出的时间戳（本地无时区，末尾可能带 "(local)"）。"""
    raw = (text or "").split(" (local)")[0].strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def _format_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _wrap_deg(value: float) -> float:
    return value % 360.0


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """把记录归一化为**扁平**形式。

    对齐流水线里的记录是嵌套 JSON（`range_m` / `bearing_rad` 在
    `position` 子字典内），而 `aligned_records.csv` 是扁平 CSV
    （同样的字段在顶层）。导出器要同时吃这两种输入，故在此统一。

    顶层已存在的键优先保留（扁平 CSV 已是最终形态）。
    """
    normalized = dict(record)
    position = record.get("position") or {}
    if isinstance(position, dict):
        for key in ("range_m", "bearing_rad", "latitude_deg", "longitude_deg"):
            if normalized.get(key) is None and position.get(key) is not None:
                normalized[key] = position[key]
    return normalized


class TrackPoint:
    """航迹上的一个观测点。"""

    __slots__ = (
        "record",
        "moment",
        "epoch_s",
        "east_m",
        "north_m",
        "up_m",
        "latitude_deg",
        "longitude_deg",
        "speed_mps",
        "course_deg",
        "speed_source",
        "course_source",
    )

    def __init__(self, record: Dict[str, Any], moment: datetime):
        self.record = record
        self.moment = moment
        # 本地无时区时间按 UTC 解读：只保证同场景内的相对时差确定。
        self.epoch_s = moment.replace(tzinfo=timezone.utc).timestamp()
        self.east_m = _to_float(record.get("east_m"))
        self.north_m = _to_float(record.get("north_m"))
        self.up_m = _to_float(record.get("up_m"))
        self.latitude_deg = _to_float(record.get("position_latitude_deg"))
        self.longitude_deg = _to_float(record.get("position_longitude_deg"))
        self.speed_mps = _to_float(record.get("speed_mps"))
        self.course_deg = (
            math.degrees(_to_float(record.get("course_rad")))
            if _to_float(record.get("course_rad")) is not None
            else None
        )
        self.speed_source = "reported" if self.speed_mps is not None else "none"
        self.course_source = "reported" if self.course_deg is not None else "none"


def _segment_displacements(
    points: Sequence[TrackPoint]
) -> List[Tuple[float, float, float]]:
    """逐段位移 → [(dt_s, 距离_m, 方位角_rad)]。"""
    segments: List[Tuple[float, float, float]] = []
    for index in range(len(points) - 1):
        first, second = points[index], points[index + 1]
        dt = second.epoch_s - first.epoch_s
        de = (second.east_m or 0.0) - (first.east_m or 0.0)
        dn = (second.north_m or 0.0) - (first.north_m or 0.0)
        distance = math.hypot(de, dn)
        course = math.atan2(de, dn) if distance > 0.0 else 0.0
        segments.append((dt, distance, course))
    return segments


def _detect_quantization_jumps(
    segments: Sequence[Tuple[float, float, float]]
) -> List[bool]:
    """标记哪些段是**量化跳变**（位置回传/档位跳变），而非真实运动。

    光电测距是整数米档位，位置呈阶梯状：连续若干点完全相同，然后突然
    跳 120–145 m。若不剔除，跳变段会把差分速度虚增数倍。

    判据：某段位移 > 本航迹**非零位移中位数** × `QUANTIZATION_JUMP_FACTOR`。
    非零位移中位数刻画"真实单步运动量"；真正的跳变远大于它。
    非零段太少时不启用该判据（返回全 False），避免把小样本误判。
    """
    count = len(segments)
    if count < MIN_NONZERO_SEGMENTS_FOR_JUMP_DETECTION:
        return [False] * count
    nonzero = [segment[1] for segment in segments if segment[1] > 0.0]
    if len(nonzero) < MIN_NONZERO_SEGMENTS_FOR_JUMP_DETECTION:
        return [False] * count
    baseline = _median(nonzero)
    if baseline is None or baseline <= 0.0:
        return [False] * count
    threshold = baseline * QUANTIZATION_JUMP_FACTOR
    return [segment[1] > threshold for segment in segments]


def _fit_velocity(
    points: Sequence[TrackPoint], index: int
) -> Optional[Tuple[float, float]]:
    """对点 index 用**滑动窗口最小二乘**拟合速度矢量 (vx, vy)。

    在以点 index 为中心、半宽 `VELOCITY_WINDOW_HALF_S` 的窗口内，
    对 (t, E) 与 (t, N) 各做一次直线拟合，斜率即速度分量。

    量化噪声在窗口内按 1/√N 衰减，真实匀速运动保持线性，
    因此斜率不受位置量化影响 —— 这是逐段差分做不到的。

    返回 None 表示窗口内数据不足或拟合不可信。
    """
    center = points[index].epoch_s
    window = [
        point
        for point in points
        if abs(point.epoch_s - center) <= VELOCITY_WINDOW_HALF_S
    ]
    if len(window) < VELOCITY_WINDOW_MIN_POINTS:
        return None
    times = [point.epoch_s - center for point in window]
    span = max(times) - min(times)
    if span < VELOCITY_WINDOW_MIN_SPAN_S:
        return None
    mean_t = sum(times) / len(times)
    variance = sum((value - mean_t) ** 2 for value in times)
    if variance <= 0.0:
        return None

    def slope(values: Sequence[float]) -> float:
        mean_v = sum(values) / len(values)
        numerator = sum(
            (t - mean_t) * (v - mean_v) for t, v in zip(times, values)
        )
        return numerator / variance

    easts = [point.east_m or 0.0 for point in window]
    norths = [point.north_m or 0.0 for point in window]
    return slope(easts), slope(norths)


def _derive_kinematics(points: Sequence[TrackPoint]) -> Dict[str, Any]:
    """补齐缺失的速度与航向。

    **主方法：滑动窗口最小二乘拟合**（`_fit_velocity`）。见
    `VELOCITY_WINDOW_HALF_S` 处的说明：位置量化会让逐段差分被噪声主导，
    窗口拟合才能得到稳定的速度估计。

    **兜底：逐段差分 + 跳变剔除**。当窗口内数据不足（短航迹、稀疏点）
    时，退回逐段差分，并剔除量化跳变段与物理不合理段。

    推导值以 `derived` 标注，与实测（`reported`）严格区分；无法可靠
    推导时保持缺失，不臆造。
    """
    count = len(points)
    segments = _segment_displacements(points)
    jumps = _detect_quantization_jumps(segments)

    # -- 兜底路径：逐段可用性 ----------------------------------------------
    usable: List[bool] = []
    for index, (dt, distance, _course) in enumerate(segments):
        ok = True
        if dt <= 0.0 or dt > MAX_DERIVE_DT_S:
            ok = False
        elif distance / dt > MAX_PLAUSIBLE_SPEED_MPS:
            ok = False
        elif jumps[index]:
            ok = False
        usable.append(ok)

    window_fitted = 0
    fallback_fitted = 0

    for index, point in enumerate(points):
        if point.speed_source == "reported" and point.course_source == "reported":
            continue

        # -- 主方法：窗口最小二乘 ------------------------------------------
        fit = _fit_velocity(points, index)
        if fit is not None:
            vx, vy = fit
            speed = math.hypot(vx, vy)
            if speed <= MAX_PLAUSIBLE_SPEED_MPS:
                if point.speed_source == "none":
                    point.speed_mps = speed
                    point.speed_source = "derived"
                if point.course_source == "none" and speed > 0.0:
                    point.course_deg = _wrap_deg(math.degrees(math.atan2(vx, vy)))
                    point.course_source = "derived"
                window_fitted += 1
                continue

        # -- 兜底：逐段差分 + 有限回溯 --------------------------------------
        candidates: List[int] = []
        for offset in range(MAX_SEGMENT_LOOKBACK + 1):
            for segment_index in (index - offset, index + offset):
                if 0 <= segment_index < len(segments) and segment_index not in candidates:
                    candidates.append(segment_index)
        for segment_index in candidates:
            if not usable[segment_index]:
                continue
            dt, distance, course = segments[segment_index]
            speed = distance / dt
            if point.speed_source == "none":
                point.speed_mps = speed
                point.speed_source = "derived"
            if point.course_source == "none" and distance > 0.0:
                point.course_deg = _wrap_deg(math.degrees(course))
                point.course_source = "derived"
            fallback_fitted += 1
            break

    jump_count = sum(1 for flag in jumps if flag)
    return {
        "segment_count": len(segments),
        "usable_segment_count": sum(1 for flag in usable if flag),
        "quantization_jump_count": jump_count,
        "quantization_jump_ratio": (
            round(jump_count / len(segments), 4) if segments else 0.0
        ),
        "window_fitted_count": window_fitted,
        "fallback_fitted_count": fallback_fitted,
    }


def _track_kind(sensor_type: str, local_track_id: str) -> str:
    """航迹类别：区分"真实目标的持续观测"与"不稳定内部编号的检测序列"。"""
    if sensor_type == "radar" and local_track_id.startswith(UNSTABLE_ID_PREFIX):
        return "radar_detection_sequence"
    return "sensor_track"


def _track_stability(sensor_type: str, local_track_id: str) -> str:
    """航迹号是否稳定可用作关联主键。"""
    if sensor_type == "radar" and local_track_id.startswith(UNSTABLE_ID_PREFIX):
        return "unstable_internal_id"
    return "stable"


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _inverse_project(
    east_m: float, north_m: float, origin_latitude_deg: float, origin_longitude_deg: float
) -> Tuple[float, float]:
    """现场 ENU → (纬度, 经度)，用与 transform 一致的切平面反解。"""
    radius = 6378137.0
    latitude = origin_latitude_deg + math.degrees(north_m / radius)
    longitude = origin_longitude_deg + math.degrees(
        east_m / (radius * math.cos(math.radians(origin_latitude_deg)))
    )
    return latitude, longitude


class TrackExporter:
    """从对齐记录导出按传感器分组的完整航迹。"""

    def __init__(
        self,
        origin_latitude_deg: float,
        origin_longitude_deg: float,
        frame_id: str = "site_enu",
    ):
        self.origin_latitude_deg = origin_latitude_deg
        self.origin_longitude_deg = origin_longitude_deg
        self.frame_id = frame_id

    # -- 主流程 ------------------------------------------------------------

    def build(
        self, records: Iterable[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
        """返回 (航迹清单, {航迹号 → 点序列}, 雷达逐帧检测点)。"""
        grouped: Dict[Tuple[str, str], List[TrackPoint]] = defaultdict(list)
        detections: List[Dict[str, Any]] = []

        for record in records:
            sensor_type = str(record.get("source_type") or "")
            if sensor_type not in SENSOR_DIRS:
                continue
            # 标准化层已判定该记录的空间表达不可用（如光电 DLDete 的超量程
            # 误检）：其位置数值本身是误检产物，进入航迹会污染关联门限，
            # 必须显式排除而不是照常收录。
            if str(record.get("alignment_status") or "") == STATUS_UNUSABLE_POSITION:
                continue
            moment = _parse_time(str(record.get("aligned_time_utc") or ""))
            if moment is None:
                continue
            point = TrackPoint(_normalize_record(record), moment)
            if point.east_m is None or point.north_m is None:
                continue
            if point.latitude_deg is None or point.longitude_deg is None:
                latitude, longitude = _inverse_project(
                    point.east_m,
                    point.north_m,
                    self.origin_latitude_deg,
                    self.origin_longitude_deg,
                )
                point.latitude_deg = latitude
                point.longitude_deg = longitude

            local_track_id = str(record.get("local_track_id") or "")
            grouped[(sensor_type, local_track_id)].append(point)

            if sensor_type == "radar":
                detections.append(
                    self._detection_row(_normalize_record(record), point)
                )

        tracks: List[Dict[str, Any]] = []
        track_points: Dict[str, List[Dict[str, Any]]] = {}

        for (sensor_type, local_track_id), points in sorted(grouped.items()):
            points.sort(key=lambda item: item.epoch_s)
            jump_stats = _derive_kinematics(points)
            track_id = f"{sensor_type}:{local_track_id}"
            rows = [self._point_row(track_id, sensor_type, index, point)
                    for index, point in enumerate(points)]
            track_points[track_id] = rows
            tracks.append(
                self._index_row(
                    track_id, sensor_type, local_track_id, points, jump_stats
                )
            )

        return tracks, track_points, detections

    # -- 行构造 ------------------------------------------------------------

    def _point_row(
        self, track_id: str, sensor_type: str, index: int, point: TrackPoint
    ) -> Dict[str, Any]:
        record = point.record
        return {
            "track_id": track_id,
            "sensor_type": sensor_type,
            "source_id": record.get("source_id"),
            "point_index": index,
            "time_utc": _format_time(point.moment),
            "time_epoch_s": round(point.epoch_s, 3),
            "latitude_deg": point.latitude_deg,
            "longitude_deg": point.longitude_deg,
            "east_m": point.east_m,
            "north_m": point.north_m,
            "up_m": point.up_m,
            "speed_mps": point.speed_mps,
            "speed_source": point.speed_source,
            "course_deg": point.course_deg,
            "course_source": point.course_source,
            "range_m": _to_float(record.get("range_m")),
            "bearing_deg": (
                math.degrees(_to_float(record.get("bearing_rad")))
                if _to_float(record.get("bearing_rad")) is not None
                else None
            ),
            "position_type": record.get("position_type"),
            "alignment_status": record.get("alignment_status"),
            "error_bound_planar_m": _to_float(record.get("error_bound_planar_m")),
            "radar_measurement_kind": record.get("radar_measurement_kind"),
            "radar_target_type": record.get("radar_target_type"),
            "radar_frame_index": _to_float(record.get("radar_frame_index")),
            "optical_device": record.get("optical_device"),
            "optical_record_kind": record.get("optical_record_kind"),
            "optical_detection_index": record.get("optical_detection_index"),
            "platform_heading_deg": _to_float(record.get("platform_heading_deg")),
            "source_file": record.get("source_file"),
            "source_row": record.get("source_row"),
            "record_id": record.get("record_id"),
        }

    def _index_row(
        self,
        track_id: str,
        sensor_type: str,
        local_track_id: str,
        points: Sequence[TrackPoint],
        jump_stats: Dict[str, Any],
    ) -> Dict[str, Any]:
        usable = [p for p in points if p.east_m is not None and p.north_m is not None]
        dts = [
            points[i + 1].epoch_s - points[i].epoch_s for i in range(len(points) - 1)
        ]
        speeds = [p.speed_mps for p in points if p.speed_mps is not None]
        courses = [p.course_deg for p in points if p.course_deg is not None]
        path_length = 0.0
        for i in range(len(points) - 1):
            path_length += math.hypot(
                (points[i + 1].east_m or 0.0) - (points[i].east_m or 0.0),
                (points[i + 1].north_m or 0.0) - (points[i].north_m or 0.0),
            )
        centroid_e = _mean([p.east_m for p in usable if p.east_m is not None])
        centroid_n = _mean([p.north_m for p in usable if p.north_m is not None])
        straight = 0.0
        if len(usable) >= 2:
            straight = math.hypot(
                (usable[-1].east_m or 0.0) - (usable[0].east_m or 0.0),
                (usable[-1].north_m or 0.0) - (usable[0].north_m or 0.0),
            )
        reported_speed = any(p.speed_source == "reported" for p in points)
        reported_course = any(p.course_source == "reported" for p in points)
        return {
            "track_id": track_id,
            "sensor_type": sensor_type,
            "source_id": points[0].record.get("source_id") if points else None,
            "local_track_id": local_track_id,
            "track_kind": _track_kind(sensor_type, local_track_id),
            "track_stability": _track_stability(sensor_type, local_track_id),
            "point_count": len(points),
            "usable_point_count": len(usable),
            "start_time_utc": _format_time(points[0].moment) if points else None,
            "end_time_utc": _format_time(points[-1].moment) if points else None,
            "duration_s": round(points[-1].epoch_s - points[0].epoch_s, 3) if points else None,
            "median_dt_s": round(_median(dts), 3) if dts else None,
            "mean_speed_mps": round(_mean(speeds), 4) if speeds else None,
            "max_speed_mps": round(max(speeds), 4) if speeds else None,
            "mean_course_deg": round(_mean(courses), 4) if courses else None,
            "centroid_east_m": round(centroid_e, 3) if centroid_e is not None else None,
            "centroid_north_m": round(centroid_n, 3) if centroid_n is not None else None,
            "path_length_m": round(path_length, 3),
            "straight_line_distance_m": round(straight, 3),
            "has_reported_speed": reported_speed,
            "has_reported_course": reported_course,
            "quantization_jump_count": jump_stats.get("quantization_jump_count", 0),
            "quantization_jump_ratio": jump_stats.get("quantization_jump_ratio", 0.0),
            "first_record_id": points[0].record.get("record_id") if points else None,
            "last_record_id": points[-1].record.get("record_id") if points else None,
        }

    def _detection_row(self, record: Dict[str, Any], point: TrackPoint) -> Dict[str, Any]:
        bearing = _to_float(record.get("bearing_rad"))
        return {
            "source_id": record.get("source_id"),
            "radar_frame_index": _to_float(record.get("radar_frame_index")),
            "time_utc": _format_time(point.moment),
            "time_epoch_s": round(point.epoch_s, 3),
            "local_track_id": record.get("local_track_id"),
            "target_type": record.get("radar_target_type"),
            "measurement_kind": record.get("radar_measurement_kind"),
            "range_m": _to_float(record.get("range_m")),
            "bearing_deg": math.degrees(bearing) if bearing is not None else None,
            "east_m": point.east_m,
            "north_m": point.north_m,
            "up_m": point.up_m,
            "latitude_deg": point.latitude_deg,
            "longitude_deg": point.longitude_deg,
            "error_bound_planar_m": _to_float(record.get("error_bound_planar_m")),
            "source_row": record.get("source_row"),
        }

    # -- 落盘 --------------------------------------------------------------

    def write(
        self,
        output_dir: str,
        tracks: Sequence[Dict[str, Any]],
        track_points: Dict[str, List[Dict[str, Any]]],
        detections: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        tracks_dir = os.path.join(output_dir, "tracks")
        os.makedirs(tracks_dir, exist_ok=True)

        written: List[str] = []
        for sensor_type, folder in SENSOR_DIRS.items():
            sensor_dir = os.path.join(tracks_dir, folder)
            os.makedirs(sensor_dir, exist_ok=True)
            for track in tracks:
                if track["sensor_type"] != sensor_type:
                    continue
                local_id = str(track["local_track_id"])
                safe = local_id.replace("/", "_").replace("\\", "_")
                path = os.path.join(sensor_dir, f"{safe}.csv")
                rows = track_points[track["track_id"]]
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle, fieldnames=TRACK_POINT_FIELDS, extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(rows)
                written.append(os.path.relpath(path, output_dir))

        index_path = os.path.join(tracks_dir, "track_index.csv")
        with open(index_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=TRACK_INDEX_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(tracks)

        detection_path = os.path.join(tracks_dir, "radar_detections.csv")
        with open(detection_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=RADAR_DETECTION_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(detections)

        by_sensor = defaultdict(list)
        for track in tracks:
            by_sensor[track["sensor_type"]].append(track)

        summary = {
            "frame_id": self.frame_id,
            "origin_latitude_deg": self.origin_latitude_deg,
            "origin_longitude_deg": self.origin_longitude_deg,
            "track_count": len(tracks),
            "point_count": sum(int(t["point_count"]) for t in tracks),
            "radar_detection_count": len(detections),
            "by_sensor": {
                sensor: {
                    "track_count": len(items),
                    "point_count": sum(int(i["point_count"]) for i in items),
                    "stable_track_count": sum(
                        1 for i in items if i["track_stability"] == "stable"
                    ),
                    "unstable_track_count": sum(
                        1 for i in items if i["track_stability"] != "stable"
                    ),
                    "tracks": [
                        {
                            "track_id": i["track_id"],
                            "local_track_id": i["local_track_id"],
                            "track_kind": i["track_kind"],
                            "track_stability": i["track_stability"],
                            "point_count": i["point_count"],
                            "duration_s": i["duration_s"],
                            "median_dt_s": i["median_dt_s"],
                            "mean_speed_mps": i["mean_speed_mps"],
                            "has_reported_speed": i["has_reported_speed"],
                        }
                        for i in items
                    ],
                }
                for sensor, items in by_sensor.items()
            },
            "notes": {
                "kinematics": (
                    "雷达与光电原始数据无速度/航向，输出中的 derived 值由相邻点"
                    "ENU 位移差分推导（中点法），与 reported 实测值严格区分。"
                    "时间间隔 >30 s 或推导速度 >60 m/s 时不推导（内部编号跳变）。"
                ),
                "radar_900xxx": (
                    "雷达 CDT 行的 目标编号=900xxx 是内部号，非稳定航迹号"
                    "（同一物理目标会在多个内部号间切换），故标记为 "
                    "track_stability=unstable_internal_id，不可直接用作关联主键；"
                    "跨帧重建请用 radar_detections.csv 做 (方位,距离) 聚类。"
                ),
                "optical_devices": (
                    "光电按设备分离为独立航迹（Inf / TV 后缀），Inf 与 TV "
                    "同时刻各自成轨，不会产生重复时间戳。"
                ),
            },
        }
        summary_path = os.path.join(tracks_dir, "track_summary.json")
        with open(summary_path, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        return {
            "tracks_dir": tracks_dir,
            "index": index_path,
            "summary": summary_path,
            "detections": detection_path,
            "track_files": written,
            "summary_data": summary,
        }


def export_tracks(
    records: Iterable[Dict[str, Any]],
    output_dir: str,
    origin_latitude_deg: float,
    origin_longitude_deg: float,
    frame_id: str = "site_enu",
) -> Dict[str, Any]:
    """便捷入口：构建并落盘航迹。"""
    exporter = TrackExporter(origin_latitude_deg, origin_longitude_deg, frame_id)
    tracks, track_points, detections = exporter.build(records)
    return exporter.write(output_dir, tracks, track_points, detections)
