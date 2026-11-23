"""公共标准记录结构定义。

两类数据源（RadarPWN 雷达 TTM、MTDSP AIS CSV）经各自适配器标准化后，
都输出同一结构的记录，供下一阶段时空对齐模块使用。

设计原则：
- 只做"表达统一"，不做坐标转换、时钟偏移修正或轨迹平滑。
- 缺失值保持缺失（None），不填 0。
- 每条记录都能回溯到来源文件与原始行号。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 枚举取值
# ---------------------------------------------------------------------------

SOURCE_TYPE_RADAR = "radar"
SOURCE_TYPE_AIS = "ais"
SOURCE_TYPE_AIS_VDM = "ais_vdm"
# MTDSP 雷达检测结果表（CutDataTarget.csv）。
# 位置表达与雷达完全同构（range_bearing + 真北方位），因此在**对齐模块**
# 中与 SOURCE_TYPE_RADAR 走同一条转换路径；但在**标准化模块**中需要独立的
# 适配器与字段语义（目标类型 / 测量来源 / 检测框），故单列一个 source_type。
SOURCE_TYPE_RADAR_CUTDATA = "radar_cutdata"
# MTDSP 光电观测（Photo_ScreenShot*.txt / *_DLDete.txt）。
# 位置表达同样是 range_bearing + 真北方位（光电与雷达同址部署），因此在
# 对齐模块中与 SOURCE_TYPE_RADAR 走同一条转换路径；但字段语义（设备解算的
# 主目标 vs 画面内 AI 检测、匹配编号、目标类型标签）完全不同，故单列。
SOURCE_TYPE_OPTICAL = "optical"

POSITION_TYPE_RANGE_BEARING = "range_bearing"
POSITION_TYPE_LATLON = "latlon"

TIME_DOMAIN_UTC = "utc"
TIME_DOMAIN_SIMULATION_UTC = "simulation_utc"
TIME_DOMAIN_LOCAL_NAIVE = "local_naive"
TIME_DOMAIN_UNKNOWN = "unknown"

TIME_BASIS_TTM_TOD = "nmea_ttm_time_of_day"
TIME_BASIS_TTM_TOD_ZDA_DATE = "nmea_ttm_time_of_day+zda_date"
TIME_BASIS_CSV_DATETIME = "csv_datetime_string"
TIME_BASIS_CAPTURE_ONLY = "capture_time_only"

TIME_PRECISION_SECOND = "second"
TIME_PRECISION_CENTISECOND = "centisecond"
TIME_PRECISION_MICROSECOND = "microsecond"
TIME_PRECISION_UNKNOWN = "unknown"

REFERENCE_TRUE = "T"
REFERENCE_MAGNETIC = "M"
REFERENCE_RELATIVE = "R"

TRACKING_STATUS_ACQUIRING = "acquiring"      # Q
TRACKING_STATUS_TRACKING = "tracking"        # T
TRACKING_STATUS_LOST = "lost"                # L

# 统一字段顺序（写出 JSONL 时保持稳定，便于 diff 与人工核对）
FIELD_ORDER = [
    "schema_version",
    # 来源与编号
    "source_id",
    "source_type",
    "session_id",
    "local_track_id",
    "record_id",
    # 状态时间
    "state_time_raw",
    "state_time_utc",
    "time_basis",
    "time_domain",
    "time_precision",
    # 接收时间
    "receive_time_utc",
    "receive_time_source",
    # 位置
    "position_type",
    "frame_id",
    "position",
    # 运动状态
    "speed_mps",
    "course_rad",
    "course_reference",
    "motion_reference",
    "heading_rad",
    # 状态信息
    "tracking_status",
    "navigation_status",
    # 质量信息
    "issues",
    "valid_for_spatial_alignment",
    "valid_for_time_alignment",
    # 原始依据
    "source_file",
    "source_row",
    "raw_record",
]

# 允许出现在 issues 中的取值
ISSUE_REJECTED = "rejected"          # 记录本身已进 rejected 文件
ISSUE_BAD_CHECKSUM = "bad_checksum"
ISSUE_BAD_FORMAT = "bad_format"
ISSUE_INVALID_POSITION = "invalid_position"
ISSUE_INVALID_VALUE = "invalid_value"
ISSUE_MISSING_POSITION = "missing_position"
ISSUE_MISSING_SPEED = "missing_speed"
ISSUE_MISSING_STATE_TIME = "missing_state_time"
ISSUE_UTC_TIME_UNKNOWN = "utc_time_unknown"
ISSUE_MOTION_REFERENCE_UNKNOWN = "motion_reference_unknown"
ISSUE_DUPLICATE = "duplicate_record"
ISSUE_POSITION_JUMP = "position_jump"
ISSUE_POSITION_JUMP_UNCERTAIN = "position_jump_uncertain"
ISSUE_STATIC_INFO_MISSING = "static_info_missing"
ISSUE_TIMEZONE_UNKNOWN = "timezone_unknown"
ISSUE_UNMAPPED_FIELDS = "unmapped_fields"


def make_record_id(
    source_id: str,
    source_file: str,
    source_row: Any,
    session_id: Optional[str] = None,
) -> str:
    """由来源、批次、文件与原始行号稳定生成 record_id。

    - source_file 必须是相对于数据根目录的稳定路径（见 pipeline 的
      relpath 归一化），否则移动文件夹会改变 ID。
    - session_id 纳入键中，使同一编号在不同采集批次复用时 record_id 不相同。
    - 同一输入重复处理时 record_id 不变，便于跨次运行比对。
    """
    key = f"{source_id}|{session_id or ''}|{source_file}|{source_row}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def new_record(**kwargs: Any) -> Dict[str, Any]:
    """创建一条空的标准记录（所有字段先置 None）。"""
    record: Dict[str, Any] = {name: None for name in FIELD_ORDER}
    record["schema_version"] = SCHEMA_VERSION
    record["issues"] = []
    record["source_file"] = kwargs.pop("source_file", None)
    record["source_row"] = kwargs.pop("source_row", None)
    record["raw_record"] = kwargs.pop("raw_record", None)
    record["record_id"] = make_record_id(
        str(kwargs.get("source_id", "")),
        str(record["source_file"]),
        record["source_row"],
        session_id=str(kwargs.get("session_id") or ""),
    )
    for key, value in kwargs.items():
        if key not in record:
            raise KeyError(f"未知字段: {key}")
        record[key] = value
    return record


def order_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """按 FIELD_ORDER 重排输出键。"""
    ordered = {name: record.get(name) for name in FIELD_ORDER}
    for key in record:  # 扩展字段排在标准字段之后
        if key not in ordered:
            ordered[key] = record[key]
    return ordered


def tracking_status_from_raw(raw: Optional[str]) -> Optional[str]:
    """TTM 状态字符映射：Q=捕获中 / T=跟踪 / L=丢失。"""
    if not raw:
        return None
    mapping = {
        "Q": TRACKING_STATUS_ACQUIRING,
        "T": TRACKING_STATUS_TRACKING,
        "L": TRACKING_STATUS_LOST,
    }
    return mapping.get(raw.strip().upper())


def issues_of(record: Dict[str, Any]) -> List[str]:
    return list(record.get("issues") or [])


def add_issue(record: Dict[str, Any], code: str) -> None:
    issues = record.setdefault("issues", [])
    if code not in issues:
        issues.append(code)


def describe_position_fields(record: Dict[str, Any]) -> List[str]:
    """返回 position 字典中实际可用的键，供下游判断可用性。"""
    position = record.get("position") or {}
    return [k for k, v in position.items() if v is not None]
