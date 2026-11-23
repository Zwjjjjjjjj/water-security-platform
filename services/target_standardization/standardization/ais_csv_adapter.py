"""MTDSP AIS CSV 适配器。

输入：AIS_Trajectory_<MMSI>.csv（动态记录），辅以 AIS_ShipInformation_<MMSI>.csv（静态属性）。

已核验的实际 CSV 事实（第一阶段）：
- 编码 UTF-8 with BOM，分隔符 ","，换行 CRLF。
- 表头：MMSI, 经度(°), 纬度(°), 时间, 速度(kn), 船艏向(°), 对地航向(°),
  航行状态, 相对视角(°), 姿态信息, 浪高(m), 方位角(°), 距离(m)
- 时间原值为 "YYYY-MM-DD HH:MM:SS"（秒级，未使用 Excel 的分钟级显示）。
- 时间字符串不带时区标记 → 不默认套用北京时间或服务器时区。
- 速度单位由表头声明为节(kn)。
- 动态 CSV 的方位角/距离定义未确认 → 只作为扩展字段保留，位置以经纬度为主。
"""

from __future__ import annotations

import csv
import glob
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import schema, time_fields, units, validation


class AISCsvAdapter:
    """把 MTDSP AIS 动态 CSV 转换为公共标准记录。"""

    source_type = schema.SOURCE_TYPE_AIS

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.source_id = source_config["source_id"]
        self.session_id = source_config["session_id"]
        self.frame_id = source_config.get("frame_id", "wgs84")
        self.mapping = source_config.get("field_mapping", {})
        self.speed_unit = source_config.get("speed_unit", "K")
        self.timezone_known = source_config.get("timezone_known", False)
        self.timezone_name = source_config.get("timezone_name")
        self.static_dir = source_config.get("static_info_dir")

    # -- 读取 ---------------------------------------------------------------

    def iter_files(self, input_path: str) -> List[str]:
        if os.path.isdir(input_path):
            return sorted(glob.glob(os.path.join(input_path, "AIS_Trajectory_*.csv")))
        return [input_path]

    def iter_rows(self, path: str) -> Iterator[Tuple[int, Dict[str, str]]]:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, start=1):
                yield index, row

    # -- 静态属性 -----------------------------------------------------------

    def load_static_info(self, mmsi: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """按 MMSI 读取静态属性，返回 (属性字典, 来源文件)。"""
        if not self.static_dir:
            return {}, None
        path = os.path.join(self.static_dir, f"AIS_ShipInformation_{mmsi}.csv")
        if not os.path.exists(path):
            return {}, None
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = [r for r in csv.DictReader(handle) if any((v or "").strip() for v in r.values())]
        if not rows:
            return {}, path
        row = rows[0]
        static = {
            "static_time_raw": (row.get("时间") or "").strip() or None,
            "vessel_name": (row.get("船名") or "").strip() or None,
            "flag_country": (row.get("国籍") or "").strip() or None,
            "vessel_type": (row.get("类型") or "").strip() or None,
            "length_m": _to_float(row.get("船长(m)")),
            "width_m": _to_float(row.get("船宽(m)")),
            "positioning_device": (row.get("定位装置") or "").strip() or None,
            "destination": (row.get("目的地") or "").strip() or None,
        }
        return static, path

    # -- 主流程 -------------------------------------------------------------

    def run(self, input_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        records: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        log: List[str] = []
        static_cache: Dict[str, Dict[str, Any]] = {}

        for path in self.iter_files(input_path):
            file_count = 0
            for row_index, row in self.iter_rows(path):
                mmsi = (row.get(self.mapping.get("mmsi", "MMSI")) or "").strip()
                raw_record = dict(row)

                if not mmsi:
                    rejected.append(
                        self._rejected(path, row_index, raw_record, "missing_mmsi")
                    )
                    continue

                if mmsi not in static_cache:
                    static_cache[mmsi] = self.load_static_info(mmsi)[0]
                static = static_cache[mmsi]

                longitude = _to_float(row.get(self.mapping.get("longitude", "经度(°)")))
                latitude = _to_float(row.get(self.mapping.get("latitude", "纬度(°)")))
                time_raw = (row.get(self.mapping.get("time", "时间")) or "").strip()
                speed_raw = _to_float(row.get(self.mapping.get("speed", "速度(kn)")))
                heading_deg = _to_float(row.get(self.mapping.get("heading", "船艏向(°)")))
                course_deg = _to_float(row.get(self.mapping.get("course", "对地航向(°)")))
                nav_status_raw = (row.get(self.mapping.get("navigation_status", "航行状态")) or "").strip()

                speed_mps = units.speed_to_mps(speed_raw, self.speed_unit) if speed_raw is not None else None
                course_rad = units.deg_to_rad(course_deg) if course_deg is not None else None
                heading_rad = units.deg_to_rad(heading_deg) if heading_deg is not None else None

                reason = validation.validate_source_values(
                    longitude_deg=longitude,
                    latitude_deg=latitude,
                    speed_mps=speed_mps,
                    course_rad=course_rad,
                    heading_rad=heading_rad,
                    state_time_raw=time_raw,
                    time_zone_confirmed=self.timezone_known,
                )
                if reason is not None:
                    rejected.append(self._rejected(path, row_index, raw_record, reason, mmsi))
                    continue

                time_block = time_fields.time_fields_for_csv(
                    state_time_raw=time_raw,
                    timezone_known=self.timezone_known,
                    timezone_name=self.timezone_name,
                )

                nav_status = self._navigation_status(nav_status_raw)

                record = schema.new_record(
                    source_id=self.source_id,
                    source_type=self.source_type,
                    session_id=self.session_id,
                    local_track_id=mmsi,
                    state_time_raw=time_block["state_time_raw"],
                    state_time_utc=time_block["state_time_utc"],
                    time_basis=time_block["time_basis"],
                    time_domain=time_block["time_domain"],
                    time_precision=time_block["time_precision"],
                    receive_time_utc=time_block["receive_time_utc"],
                    receive_time_source=time_block["receive_time_source"],
                    position_type=schema.POSITION_TYPE_LATLON,
                    frame_id=self.frame_id,
                    position={
                        "longitude_deg": longitude,
                        "latitude_deg": latitude,
                    },
                    speed_mps=round(speed_mps, 6) if speed_mps is not None else None,
                    course_rad=round(course_rad, 9) if course_rad is not None else None,
                    course_reference="T",  # AIS 对地航向以真北为参考
                    motion_reference=None,  # AIS 未区分对地/对水速度来源，保持未知
                    heading_rad=round(heading_rad, 9) if heading_rad is not None else None,
                    tracking_status=None,
                    navigation_status=nav_status,
                    source_file=path,
                    source_row=row_index,
                    raw_record=raw_record,
                )

                # 扩展字段
                record["mmsi"] = mmsi
                record["speed_raw"] = speed_raw
                record["speed_unit"] = units.speed_unit_name(self.speed_unit)
                record["speed_unit_source"] = self.config.get(
                    "speed_unit_source", "config_declared"
                )
                record["heading_raw"] = heading_deg
                record["course_raw"] = course_deg
                record["navigation_status_raw"] = nav_status_raw or None
                record["relative_view_deg"] = _to_float(row.get(self.mapping.get("relative_view", "相对视角(°)")))
                record["attitude_info_raw"] = (row.get(self.mapping.get("attitude", "姿态信息")) or "").strip() or None
                record["wave_height_m"] = _to_float(row.get(self.mapping.get("wave_height", "浪高(m)")))
                record["bearing_deg_unverified"] = _to_float(row.get(self.mapping.get("bearing", "方位角(°)")))
                record["distance_m_unverified"] = _to_float(row.get(self.mapping.get("distance", "距离(m)")))
                record["static_info"] = static
                record["static_info_source"] = (
                    os.path.join(self.static_dir, f"AIS_ShipInformation_{mmsi}.csv")
                    if self.static_dir
                    else None
                )

                if not static:
                    schema.add_issue(record, schema.ISSUE_STATIC_INFO_MISSING)
                if not self.timezone_known:
                    schema.add_issue(record, schema.ISSUE_TIMEZONE_UNKNOWN)

                validation.validate_common(record)
                validation.assess_validity(record)
                records.append(record)
                file_count += 1

            log.append(f"ais: 文件 {os.path.basename(path)} 记录 {file_count}")

        log.append(f"ais: 输出 {len(records)}，拒绝 {len(rejected)}")
        return records, rejected, log

    # -- 内部实现 -----------------------------------------------------------

    @staticmethod
    def _navigation_status(raw: Optional[str]) -> Optional[str]:
        if raw is None:
            return None
        text = raw.strip()
        if text in ("", "--", "-"):
            return None
        return text

    def _rejected(
        self,
        path: str,
        row_index: int,
        raw_record: Dict[str, Any],
        reason: str,
        mmsi: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "session_id": self.session_id,
            "record_id": schema.make_record_id(
                self.source_id, path, row_index, session_id=self.session_id
            ),
            "source_file": path,
            "source_row": row_index,
            "local_track_id": mmsi,
            "reason": reason,
            "issue": validation.reject_reason_to_issue(reason),
            "raw_record": raw_record,
        }


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "--", "-", "null", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None
