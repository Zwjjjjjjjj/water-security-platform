"""字段检查、异常分类与有效性判定。

两类异常：
- 拒绝（rejected）：无法可靠解析或数值非法，记录写入 rejected_records.jsonl。
- 输出并标记限制（issues）：可输出但字段缺失或依据不足。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from . import schema


def check_latitude(value: Optional[float]) -> bool:
    return value is not None and -90.0 <= value <= 90.0


def check_longitude(value: Optional[float]) -> bool:
    return value is not None and -180.0 <= value <= 180.0


def check_speed(value: Optional[float]) -> bool:
    return value is not None and value >= 0.0


def check_course(value: Optional[float]) -> bool:
    return value is None or 0.0 <= value <= 360.0


def validate_common(record: Dict[str, Any]) -> List[str]:
    """通用字段检查，追加 issues 并返回新增的 issue 列表。"""
    added: List[str] = []

    def mark(code: str) -> None:
        if code not in record["issues"]:
            record["issues"].append(code)
            added.append(code)

    position_type = record.get("position_type")
    position = record.get("position") or {}

    if position_type == schema.POSITION_TYPE_LATLON:
        lon = position.get("longitude_deg")
        lat = position.get("latitude_deg")
        if lat is None or lon is None:
            mark(schema.ISSUE_MISSING_POSITION)
        elif not (check_latitude(lat) and check_longitude(lon)):
            mark(schema.ISSUE_INVALID_POSITION)
    elif position_type == schema.POSITION_TYPE_RANGE_BEARING:
        rng = position.get("range_m")
        brg = position.get("bearing_rad")
        if rng is None or brg is None:
            mark(schema.ISSUE_MISSING_POSITION)
        elif rng < 0.0:
            mark(schema.ISSUE_INVALID_POSITION)
        elif not (0.0 <= brg <= 2 * 3.141592653589793):
            mark(schema.ISSUE_INVALID_POSITION)

    if record.get("speed_mps") is None:
        mark(schema.ISSUE_MISSING_SPEED)
    elif not check_speed(record["speed_mps"]):
        mark(schema.ISSUE_INVALID_VALUE)

    if record.get("course_rad") is not None and not check_course(
        record["course_rad"] * 57.29577951308232
    ):
        mark(schema.ISSUE_INVALID_VALUE)

    if record.get("state_time_utc") is None:
        if not record.get("state_time_raw"):
            mark(schema.ISSUE_MISSING_STATE_TIME)
        else:
            mark(schema.ISSUE_UTC_TIME_UNKNOWN)

    if record.get("motion_reference") is None:
        mark(schema.ISSUE_MOTION_REFERENCE_UNKNOWN)

    return added


def assess_validity(record: Dict[str, Any]) -> None:
    """依据实际可用字段设置两个有效性标记。

    判定条件（逐条列出，避免"数值有效"被误读为"可直接对齐"）：

    valid_for_spatial_alignment 为真，当且仅当：
      - position_type = latlon：经度与纬度均存在，且在合法范围内；
      - position_type = range_bearing：距离与方位均存在，距离非负、方位在
        [0, 2π) 内；
      - 并且该位置表达所需的参考信息完整（经纬度需已知 frame_id；
        距离方位需已知方位参考 T/M/R）。

    注意：该标记只表示**本记录自身**的空间表达完整可用。跨源对齐还需要
    数据集层面的条件（雷达外参、本船位置、坐标系换算关系等），这些不齐全
    时记录在 spatial_alignment_requirements_missing 中。

    valid_for_time_alignment 为真，当且仅当：
      - state_time_utc 存在（有完整 UTC 状态时间）；
      - time_precision 已声明且不是 unknown；
      - time_basis 已声明。
    注意：时间有效不代表具备状态预测条件；速度缺失或运动参考未知时，
    相关限制记录在 issues 中。
    """
    position_type = record.get("position_type")
    position = record.get("position") or {}
    missing_requirements: List[str] = []

    if position_type == schema.POSITION_TYPE_LATLON:
        lon = position.get("longitude_deg")
        lat = position.get("latitude_deg")
        spatial_ok = (
            lon is not None
            and lat is not None
            and check_latitude(lat)
            and check_longitude(lon)
        )
        if not record.get("frame_id"):
            missing_requirements.append("frame_id")
    elif position_type == schema.POSITION_TYPE_RANGE_BEARING:
        rng = position.get("range_m")
        brg = position.get("bearing_rad")
        spatial_ok = (
            rng is not None
            and brg is not None
            and rng >= 0.0
            and 0.0 <= brg <= 2 * math.pi
        )
        if not position.get("bearing_reference"):
            missing_requirements.append("bearing_reference")
        # 距离方位要落到统一坐标系，还需要本船位置与雷达外参
        missing_requirements.extend(["own_ship_position", "radar_extrinsics"])
    else:
        spatial_ok = False
        missing_requirements.append("position_type")

    record["valid_for_spatial_alignment"] = bool(spatial_ok)
    record["spatial_alignment_requirements_missing"] = sorted(set(missing_requirements))

    time_ok = bool(
        record.get("state_time_utc")
        and record.get("time_precision")
        and record.get("time_precision") != schema.TIME_PRECISION_UNKNOWN
        and record.get("time_basis")
    )
    record["valid_for_time_alignment"] = time_ok


def validate_source_values(
    *,
    longitude_deg: Optional[float],
    latitude_deg: Optional[float],
    speed_mps: Optional[float],
    course_rad: Optional[float],
    heading_rad: Optional[float],
    state_time_raw: Optional[str],
    time_zone_confirmed: bool,
) -> Optional[str]:
    """在构造记录之前做硬性检查，返回拒绝原因（None 表示通过）。"""
    if longitude_deg is None or latitude_deg is None:
        return "missing_latlon"
    if not check_latitude(latitude_deg):
        return "latitude_out_of_range"
    if not check_longitude(longitude_deg):
        return "longitude_out_of_range"
    if speed_mps is not None and not check_speed(speed_mps):
        return "negative_speed"
    if course_rad is not None and not check_course(course_rad * 57.29577951308232):
        return "course_out_of_range"
    if heading_rad is not None and not check_course(heading_rad * 57.29577951308232):
        return "heading_out_of_range"
    if not state_time_raw:
        return "missing_state_time"
    return None


def reject_reason_to_issue(reason: str) -> str:
    mapping = {
        "missing_latlon": schema.ISSUE_MISSING_POSITION,
        "latitude_out_of_range": schema.ISSUE_INVALID_POSITION,
        "longitude_out_of_range": schema.ISSUE_INVALID_POSITION,
        "negative_speed": schema.ISSUE_INVALID_VALUE,
        "course_out_of_range": schema.ISSUE_INVALID_VALUE,
        "heading_out_of_range": schema.ISSUE_INVALID_VALUE,
        "missing_state_time": schema.ISSUE_MISSING_STATE_TIME,
    }
    return mapping.get(reason, schema.ISSUE_BAD_FORMAT)


def detect_duplicates(records: List[Dict[str, Any]], key_fields: List[str]) -> None:
    """标记重复记录（默认不删除）。"""
    seen: Dict[tuple, int] = {}
    for record in records:
        key = tuple(str(record.get(f)) for f in key_fields)
        if key in seen:
            if schema.ISSUE_DUPLICATE not in record["issues"]:
                record["issues"].append(schema.ISSUE_DUPLICATE)
            record["duplicate_of_first_record_id"] = seen[key]
        else:
            seen[key] = record["record_id"]


class QuantizationSpec:
    """坐标存储量化分辨率。

    依据必须来自数据说明或已核验的原始 CSV 数值分辨率，**不得**由相邻位置
    差值反推：相邻差值同时包含量化误差与真实运动，反推结果可能远大于真实
    分辨率，从而掩盖真实跳变。

    resolution_deg 为该字段的存储步长（度）。
    confident 为 False 时表示依据不可靠，此时不自动扣除裕度，只保留原始量并
    标记判定不确定。
    """

    def __init__(
        self,
        resolution_deg: Optional[float] = None,
        *,
        basis: Optional[str] = None,
        confident: bool = True,
        axis: str = "longitude",
    ):
        self.resolution_deg = resolution_deg
        self.basis = basis
        self.confident = confident and resolution_deg is not None
        self.axis = axis

    def margin_m(self, latitude_deg: Optional[float]) -> Optional[float]:
        """该字段量化造成的最大位置误差（米）。

        经度与纬度按各自方向换算：1° 纬度 ≈ 111320 m，1° 经度 ≈
        111320·cos(lat) m。两者不能互相替代——在 37.56° 附近 0.001° 经度约
        88 m，而 0.001° 纬度约 111 m。
        """
        if not self.confident or self.resolution_deg is None:
            return None
        if latitude_deg is None:
            return None
        if self.resolution_deg <= 0:
            return 0.0
        if self.axis == "latitude":
            return self.resolution_deg * 111320.0
        return self.resolution_deg * 111320.0 * math.cos(math.radians(latitude_deg))

    def describe(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "resolution_deg": self.resolution_deg,
            "basis": self.basis,
            "confident": self.confident,
        }


def detect_position_jumps(
    records: List[Dict[str, Any]],
    *,
    max_speed_mps: float = 15.0,
    min_dt_s: float = 1.0,
    lon_quantization: Optional[QuantizationSpec] = None,
    lat_quantization: Optional[QuantizationSpec] = None,
) -> None:
    """按目标编号检测位置跳变，只报告不修改轨迹。

    判定同时保留三组量，便于人工复核：
      - raw_distance_m / raw_speed_mps：由原始经纬度直接计算；
      - quantization_margin_m：按配置量化分辨率换算；
      - adjusted_speed_mps：扣除裕度后的速度。

    判定规则：
      - 量化依据可靠（confident=True）：adjusted_speed_mps 超过阈值才告警；
      - 量化依据不可靠：不自动扣除裕度，raw 与 adjusted 并列，告警标记为
        `uncertain`，由人工复核。

    时间依据优先使用 UTC 状态时间；没有完整 UTC 时退回原始本地时间
    （同一来源同一日，相对间隔仍然可比），并在记录中注明所用时间依据。
    """
    from datetime import datetime

    by_track: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if record.get("position_type") != schema.POSITION_TYPE_LATLON:
            continue
        by_track.setdefault(str(record.get("local_track_id")), []).append(record)

    for track_records in by_track.values():
        track_records.sort(key=lambda r: (r.get("state_time_utc") or r.get("state_time_raw") or ""))
        previous: Optional[Dict[str, Any]] = None
        previous_time: Optional[datetime] = None
        for record in track_records:
            moment = _parse_iso(record.get("state_time_utc")) or _parse_naive(
                record.get("state_time_raw")
            )
            basis = "state_time_utc" if _parse_iso(record.get("state_time_utc")) else "state_time_raw"
            if previous is not None and moment is not None and previous_time is not None:
                dt = (moment - previous_time).total_seconds()
                if dt >= min_dt_s:
                    lon_margin = (
                        lon_quantization.margin_m(record["position"].get("latitude_deg"))
                        if lon_quantization
                        else None
                    )
                    lat_margin = (
                        lat_quantization.margin_m(record["position"].get("latitude_deg"))
                        if lat_quantization
                        else None
                    )
                    margins = [m for m in (lon_margin, lat_margin) if m is not None]
                    margin = math.hypot(*margins) if len(margins) == 2 else (margins[0] if margins else None)
                    reliable = bool(
                        lon_quantization
                        and lat_quantization
                        and lon_quantization.confident
                        and lat_quantization.confident
                    )

                    dist = _haversine_m(
                        previous["position"]["latitude_deg"],
                        previous["position"]["longitude_deg"],
                        record["position"]["latitude_deg"],
                        record["position"]["longitude_deg"],
                    )
                    raw_speed = dist / dt
                    if margin is None or not reliable:
                        adjusted_speed = raw_speed
                    else:
                        adjusted_speed = max(0.0, dist - margin) / dt

                    exceeded = adjusted_speed > max_speed_mps
                    if exceeded:
                        if schema.ISSUE_POSITION_JUMP not in record["issues"]:
                            record["issues"].append(schema.ISSUE_POSITION_JUMP)
                        if not reliable:
                            if schema.ISSUE_POSITION_JUMP_UNCERTAIN not in record["issues"]:
                                record["issues"].append(schema.ISSUE_POSITION_JUMP_UNCERTAIN)
                        record["position_jump"] = {
                            "raw_distance_m": round(dist, 2),
                            "raw_speed_mps": round(raw_speed, 3),
                            "quantization_margin_m": round(margin, 2) if margin is not None else None,
                            "adjusted_speed_mps": round(adjusted_speed, 3),
                            "dt_s": round(dt, 3),
                            "threshold_mps": max_speed_mps,
                            "time_basis": basis,
                            "quantization_reliable": reliable,
                            "uncertain": not reliable,
                            "quantization": {
                                "longitude": lon_quantization.describe() if lon_quantization else None,
                                "latitude": lat_quantization.describe() if lat_quantization else None,
                            },
                        }
            previous = record
            previous_time = moment


def _parse_iso(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_naive(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    radius = 6371008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))
