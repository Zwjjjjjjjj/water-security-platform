"""空间对齐：把各数据源的位置统一到同一现场 ENU 坐标系。

输入：标准化模块输出的公共记录（JSONL）。
输出：在每条记录上**新增** `east_m` / `north_m` / `up_m` /
      `alignment_frame_id` / 转换状态与参数版本，原有字段全部保留。

雷达路径：
    距离 + 方位 → 站址切平面相对东/北 → 站址 ECEF 叠加 → 现场 ENU

    方位参考的处理：
      T（真北）：方位已是真方位，直接使用。**不得**再叠加安装方向或本船艏向。
      M（磁北）：需叠加磁差；磁差未配置时该记录转换失败并记录原因。
      R（相对艏向）：需叠加本船艏向；本船艏向未配置时转换失败并记录原因。

    位置参考点偏移（reference_point_offset_*）在站址位置上单独叠加。

AIS 路径：
    经纬度 → ECEF → 现场 ENU
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Tuple

from .config import AlignmentConfig, SourceConfig
from .if_platform_state import DEFAULT_MATCH_TOLERANCE_S, IfPlatformStateIndex
from .transform import Enu, SiteFrame, latlon_to_enu, radar_polar_to_enu

# 转换状态
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_NO_POSITION = "no_position"
STATUS_UNKNOWN_BEARING_REFERENCE = "unknown_bearing_reference"
# 标准化层已判定该记录的空间表达不可用（如光电 DLDete 的超量程误检）。
# 这类记录的位置字段虽然有数值，但数值本身是误检产物，进入 ENU 后会污染
# 跨源比对与航迹关联，因此必须显式排除而不是照常转换。
STATUS_UNUSABLE_POSITION = "unusable_position"


class SpatialAligner:
    """把公共记录的位置转换到现场 ENU。"""

    def __init__(self, config: AlignmentConfig):
        self.config = config
        frame = config.site_frame
        self.frame = SiteFrame(
            frame_id=frame.frame_id,
            datum=frame.datum,
            origin_latitude_deg=frame.origin_latitude_deg,
            origin_longitude_deg=frame.origin_longitude_deg,
            origin_height_m=frame.origin_height_m,
        )
        # 雷达帧头（IF/IQ）平台自身状态索引：可为 None（不接入）。
        self.platform_state_index = None
        platform_cfg = config.platform_state
        if platform_cfg:
            self.platform_state_index = IfPlatformStateIndex.from_directory(
                platform_cfg.get("if_directory", ""),
                platform_cfg.get("if_filename"),
                match_tolerance_s=float(
                    platform_cfg.get("match_tolerance_s", DEFAULT_MATCH_TOLERANCE_S)
                ),
                dat_path=self._resolve_dat_path(platform_cfg),
            )

    @staticmethod
    def _resolve_dat_path(platform_cfg: Dict[str, Any]) -> Optional[str]:
        """解析 `.dat` 二进制路径（可选，用于补齐 CSV 缺失的帧头状态）。"""
        configured = platform_cfg.get("if_dat_path")
        if not configured:
            return None
        if os.path.isabs(configured):
            return configured if os.path.exists(configured) else None
        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        candidate = os.path.join(project_root, configured)
        return candidate if os.path.exists(candidate) else None

    # -- 主入口 -------------------------------------------------------------

    def align_records(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """就地给记录补充 ENU 字段，返回统计。"""
        stats = {
            "total": len(records),
            "converted": 0,
            "failed": 0,
            "no_position": 0,
            "unusable_position": 0,
            "failure_reasons": {},
            "by_source": {},
        }
        for record in records:
            self.align_record(record)
            source_id = str(record.get("source_id"))
            bucket = stats["by_source"].setdefault(
                source_id,
                {
                    "total": 0,
                    "converted": 0,
                    "failed": 0,
                    "no_position": 0,
                    "unusable_position": 0,
                },
            )
            bucket["total"] += 1
            status = record.get("alignment_status")
            if status == STATUS_OK:
                stats["converted"] += 1
                bucket["converted"] += 1
            elif status == STATUS_NO_POSITION:
                stats["no_position"] += 1
                bucket["no_position"] += 1
            elif status == STATUS_UNUSABLE_POSITION:
                # 标准化层已判定的误检/不可用位置：不计入失败，
                # 单独统计以便区分"缺少前提"与"数据本身不可用"。
                stats["unusable_position"] += 1
                bucket["unusable_position"] += 1
            else:
                stats["failed"] += 1
                bucket["failed"] += 1
                reason = record.get("alignment_failure_reason", "unknown")
                stats["failure_reasons"][reason] = (
                    stats["failure_reasons"].get(reason, 0) + 1
                )
        return stats

    def align_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """转换单条记录，字段就地写入。"""
        record["alignment_frame_id"] = self.config.site_frame.frame_id
        record["alignment_parameter_version"] = self.config.parameter_version
        record["alignment_config_id"] = self.config.alignment_config_id

        source_type = record.get("source_type")
        # 前置条件检查：缺少站址或坐标基准时，无法给出任何位置
        blocking = self._blocking_problems()
        if blocking:
            record["alignment_status"] = STATUS_FAILED
            record["alignment_failure_reason"] = blocking[0]
            record["alignment_missing_requirements"] = list(blocking)
            return

        # 标准化层已判定空间表达不可用的记录（如光电 DLDete 超量程误检）
        # 不得进入 ENU 转换：其位置数值是误检产物，转换后只会污染下游。
        if record.get("valid_for_spatial_alignment") is False:
            record["alignment_status"] = STATUS_UNUSABLE_POSITION
            record["alignment_failure_reason"] = "position_marked_unusable_at_standardization"
            record["alignment_missing_requirements"] = []
            return

        if source_type == "ais":
            self._align_ais(record)
        elif source_type in ("radar", "radar_cutdata", "optical"):
            # 光电与雷达同址部署，距离/真方位的定义方式完全一致，
            # 因此走同一条转换路径。区别只在视轴标定（见 optical_azimuth_bias）。
            self._align_radar(record)
        else:
            record["alignment_status"] = STATUS_FAILED
            record["alignment_failure_reason"] = f"unsupported_source_type:{source_type}"
            record["alignment_missing_requirements"] = [
                f"unsupported_source_type:{source_type}"
            ]
        return record

    def _blocking_problems(self) -> list:
        """阻断空间转换的前置条件缺失（缺站址、缺坐标基准等）。"""
        problems: list = []
        site = self.config.site_frame
        if site.origin_latitude_deg is None or site.origin_longitude_deg is None:
            problems.append("missing_site_frame_origin")
        station = self.config.radar_station
        if (
            station.station_latitude_deg is None
            or station.station_longitude_deg is None
        ):
            problems.append("missing_radar_station_position")
        return problems

    # -- AIS ----------------------------------------------------------------

    def _align_ais(self, record: Dict[str, Any]) -> None:
        position = record.get("position") or {}
        latitude = position.get("latitude_deg")
        longitude = position.get("longitude_deg")
        if latitude is None or longitude is None:
            record["alignment_status"] = STATUS_NO_POSITION
            record["alignment_failure_reason"] = "missing_latlon"
            return
        enu = latlon_to_enu(
            self.frame,
            float(latitude),
            float(longitude),
            position.get("height_m", 0.0) or 0.0,
        )
        self._write_enu(record, enu, "latlon_to_ecef_to_enu")

    # -- 雷达 ---------------------------------------------------------------

    def _align_radar(self, record: Dict[str, Any]) -> None:
        position = record.get("position") or {}
        range_m = position.get("range_m")
        bearing_rad = position.get("bearing_rad")
        if range_m is None or bearing_rad is None:
            record["alignment_status"] = STATUS_NO_POSITION
            record["alignment_failure_reason"] = "missing_range_or_bearing"
            return

        # 方位参考字段：标准化模块把它放在 position 内（与 range/bearing 同级），
        # 同时兼容记录顶层的写法。
        bearing_reference = str(
            position.get("bearing_reference")
            or record.get("bearing_reference")
            or "T"
        ).upper()
        bearing_true_rad = self._to_true_bearing(record, bearing_rad, bearing_reference)
        if bearing_true_rad is None:
            record["alignment_status"] = STATUS_FAILED
            record["alignment_failure_reason"] = (
                f"bearing_reference_{bearing_reference.lower()}_not_convertible"
            )
            return

        station = self.config.radar_station
        # 站址（不含位置参考点偏移）
        enu = radar_polar_to_enu(
            self.frame,
            station_latitude_deg=station.station_latitude_deg,
            station_longitude_deg=station.station_longitude_deg,
            station_height_m=station.station_height_m,
            range_m=float(range_m),
            bearing_true_rad=bearing_true_rad,
        )
        # 位置参考点偏移单独叠加：偏移量定义在站址处的 ENU 上，
        # 因此直接加到结果上，而不是先把米换算成经纬度再参与站址定位
        # （后者会引入换算误差）。
        enu = Enu(
            east_m=enu.east_m + station.reference_point_offset_east_m,
            north_m=enu.north_m + station.reference_point_offset_north_m,
            up_m=enu.up_m + station.reference_point_offset_up_m,
        )
        self._write_enu(record, enu, "polar_to_ecef_to_enu")
        record["alignment_bearing_reference_used"] = bearing_reference
        record["alignment_install_heading_applied"] = False
        record["alignment_station_latitude_deg"] = station.station_latitude_deg
        record["alignment_station_longitude_deg"] = station.station_longitude_deg
        record["alignment_reference_point_offset_applied_m"] = {
            "east": station.reference_point_offset_east_m,
            "north": station.reference_point_offset_north_m,
            "up": station.reference_point_offset_up_m,
        }
        self._attach_platform_state(record)

    # -- 平台自身状态（IF/IQ 帧头）---------------------------------------

    def _attach_platform_state(self, record: Dict[str, Any]) -> None:
        """把雷达平台自身的航向角与速度关联到记录上。

        这两个量描述的是**平台自身**（固定岸基站的 GNSS 状态），
        不是被观测目标。因此它们**只作附加记录**，绝不参与方位到 ENU
        的换算 —— 实测已证明方位基准是真北固定基准，与平台艏向无关。

        未配置 `platform_state` 或索引为空时，记录上不出现这些字段，
        既有行为完全不变。
        """
        index = self.platform_state_index
        if index is None or not index.available:
            return
        match = index.lookup(
            frame_index=record.get("radar_frame_index"),
            state_time_raw=record.get("state_time_raw"),
        )
        record["platform_state_match_basis"] = match.match_basis
        if not match.matched:
            return
        frame = match.frame
        record["platform_heading_deg"] = frame.heading_deg
        record["platform_speed_mps"] = frame.speed_mps
        record["platform_state_frame_index"] = frame.frame_index
        record["platform_state_time_raw"] = frame.state_time_raw
        record["platform_state_time_delta_s"] = match.time_delta_s

    def _to_true_bearing(
        self, record: Dict[str, Any], bearing_rad: float, reference: str
    ) -> Optional[float]:
        """把报文方位换算为真方位（弧度）。

        T：已是真方位，原样返回（不叠加安装方向或本船艏向）。
        M：加磁差；R：加本船艏向。缺少换算参数时返回 None。
        """
        if reference == "T":
            return bearing_rad
        if reference == "M":
            variation = self.config.radar_station.magnetic_variation_deg
            if variation is None:
                return None
            return bearing_rad + math.radians(variation)
        if reference == "R":
            heading = self.config.own_ship_heading_deg
            if heading is None:
                return None
            return bearing_rad + math.radians(heading)
        return None

    # -- 输出 ---------------------------------------------------------------

    @staticmethod
    def _write_enu(
        record: Dict[str, Any], enu, method: str
    ) -> None:
        record["east_m"] = enu.east_m
        record["north_m"] = enu.north_m
        record["up_m"] = enu.up_m
        record["alignment_status"] = STATUS_OK
        record["alignment_method"] = method
        record.pop("alignment_failure_reason", None)
