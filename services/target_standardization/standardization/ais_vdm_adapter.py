"""RadarPWN 包内 AIS（!AIVDM 报文流）适配器。

输入：ais_with_capture_time.csv（每行 = PCAP 记录时间 + 原始 !AIVDM 报文）。

已核验的事实：
- 7719 条 !AIVDM 报文，全部为 AIS 位置报文（消息类型 1/2/3），校验和全部通过。
- 解码后得到 76 个 MMSI 的动态位置轨迹。
- 报文内的 `second` 是**分钟内秒数**（0–59），跨分钟回绕，不能直接当作
  日内时间；每条报文必须结合抓包时间所在的分钟与同一数据流的 ZDA 日期
  才能还原完整的仿真 UTC。
- 报文本身不含经纬度以外的参考信息；速度单位为节，对地航向与船首向可分别获得。
- 位置有效性的一个关键前提：lat/lon 同时为 0 表示"位置不可用"，须排除。

时间处理与雷达 TTM 侧一致：仿真时间与抓包时间分开保存，两条时间线不互相换算。
"""

from __future__ import annotations

import csv
from typing import Any, Dict, List, Optional, Tuple

import pyais

from . import schema, time_fields, units, validation


class RadarPWNAisAdapter:
    """把 RadarPWN 包内的 !AIVDM 报文流转换为公共标准记录。"""

    source_type = schema.SOURCE_TYPE_AIS

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.source_id = source_config["source_id"]
        self.session_id = source_config["session_id"]
        self.frame_id = source_config.get("frame_id", "wgs84")
        self.date_binding = source_config.get("simulation_date_binding", "zda_date")
        self.min_lat = source_config.get("valid_lat_min", -90.0)
        self.max_lat = source_config.get("valid_lat_max", 90.0)
        self.min_lon = source_config.get("valid_lon_min", -180.0)
        self.max_lon = source_config.get("valid_lon_max", 180.0)
        # AIS 标准的位置不可用编码（通用规则，不可配置关闭）
        self.unavailable_lat = 91.0
        self.unavailable_lon = 181.0
        # 本数据集中的疑似占位位置，由来源配置显式声明
        self.placeholder_positions = {
            tuple(pair)
            for pair in (source_config.get("placeholder_positions") or [])
        }

    # -- 读取 ---------------------------------------------------------------

    def iter_source_rows(self, input_path: str):
        with open(input_path, "r", encoding="utf-8-sig", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), start=1):
                yield index, row

    # -- 主流程 -------------------------------------------------------------

    def run(
        self,
        input_path: str,
        sim_context: Optional[time_fields.SimulationDateContext] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        records: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        log: List[str] = []
        decoded = 0

        for row_index, row in self.iter_source_rows(input_path):
            sentence = (row.get("sentence") or "").strip()
            if not sentence:
                rejected.append(
                    self._rejected(input_path, row_index, sentence, "empty_sentence")
                )
                continue

            try:
                message = pyais.decode(sentence)
            except Exception as exc:
                rejected.append(
                    self._rejected(
                        input_path, row_index, sentence, f"decode_error: {type(exc).__name__}"
                    )
                )
                continue
            decoded += 1

            mmsi = str(getattr(message, "mmsi", "") or "")
            if not mmsi:
                rejected.append(
                    self._rejected(input_path, row_index, sentence, "missing_mmsi")
                )
                continue

            latitude = getattr(message, "lat", None)
            longitude = getattr(message, "lon", None)
            speed_kn = getattr(message, "speed", None)
            course_deg = getattr(message, "course", None)
            heading_deg = getattr(message, "heading", None)
            nav_raw = getattr(message, "status", None)

            # 位置有效性判定分两层：
            #  1) AIS 标准的位置不可用编码：纬度 91°、经度 181°（通用规则）；
            #  2) (0, 0) 只是本数据集中的**疑似占位值**，由来源配置显式声明，
            #     通用适配器不把 (0, 0) 一律判为不可用。
            position_valid, position_anomaly = self._assess_position(latitude, longitude)

            capture_time_utc = row.get("capture_time_utc")
            capture_unix = row.get("capture_time_unix")

            time_block = self._time_block(
                message=message,
                capture_time_utc=capture_time_utc,
                capture_unix=capture_unix,
                sim_context=sim_context,
            )

            if position_valid and not (self.min_lat <= latitude <= self.max_lat):
                rejected.append(
                    self._rejected(input_path, row_index, sentence, "latitude_out_of_range")
                )
                continue
            if position_valid and not (self.min_lon <= longitude <= self.max_lon):
                rejected.append(
                    self._rejected(input_path, row_index, sentence, "longitude_out_of_range")
                )
                continue

            speed_mps = (
                units.speed_to_mps(speed_kn, "K") if speed_kn is not None else None
            )
            course_rad = units.deg_to_rad(course_deg) if course_deg is not None else None
            heading_rad = units.deg_to_rad(heading_deg) if heading_deg is not None else None

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
                position=(
                    {"longitude_deg": longitude, "latitude_deg": latitude}
                    if position_valid
                    else {"longitude_deg": None, "latitude_deg": None}
                ),
                speed_mps=round(speed_mps, 6) if speed_mps is not None else None,
                course_rad=round(course_rad, 9) if course_rad is not None else None,
                course_reference="T",  # AIS 对地航向以真北为参考
                motion_reference=None,  # AIS 未区分对地/对水速度来源
                heading_rad=round(heading_rad, 9) if heading_rad is not None else None,
                tracking_status=None,
                navigation_status=_navigation_status(nav_raw),
                source_file=input_path,
                source_row=row_index,
                raw_record=sentence,
            )

            record["mmsi"] = mmsi
            record["ais_msg_type"] = getattr(message, "msg_type", None)
            record["speed_raw"] = speed_kn
            record["speed_unit"] = "knot"
            record["speed_unit_source"] = "ais_message_1_speed_over_ground"
            record["course_raw"] = course_deg
            record["heading_raw"] = heading_deg
            record["navigation_status_raw"] = str(nav_raw) if nav_raw is not None else None
            record["accuracy_raw"] = getattr(message, "accuracy", None)
            record["raim_raw"] = getattr(message, "raim", None)
            record["communication_state_raw"] = getattr(message, "communication_state_raw", None)
            record["capture_time_unix_raw"] = capture_unix
            record["receive_time_unix"] = (
                float(capture_unix) if capture_unix is not None else None
            )
            record["vdm_minute_second"] = getattr(message, "second", None)
            record["zda_association"] = time_block["zda_association"]
            record["position_available"] = position_valid
            record["position_anomaly"] = position_anomaly

            validation.validate_common(record)
            if not position_valid:
                schema.add_issue(record, schema.ISSUE_MISSING_POSITION)
                if position_anomaly:
                    record["position_anomaly_reason"] = position_anomaly
            validation.assess_validity(record)
            records.append(record)

        log.append(
            f"ais_vdm: 输入行 {len(records) + len(rejected)}，解码成功 {decoded}，"
            f"输出 {len(records)}，拒绝 {len(rejected)}"
        )
        return records, rejected, log

    # -- 内部实现 -----------------------------------------------------------

    def _assess_position(
        self, latitude: Optional[float], longitude: Optional[float]
    ) -> Tuple[bool, Optional[str]]:
        """判定位置有效性，返回 (是否有效, 异常原因)。

        两层规则：
        1. AIS 标准的位置不可用编码（纬度 91°、经度 181°）→ 位置无效；
        2. 命中来源配置声明的疑似占位位置（本数据集的 (0, 0)）→ 位置无效，
           并注明异常原因；未在配置中声明时不做此判定。
        """
        if latitude is None or longitude is None:
            return False, "missing_position"
        if abs(latitude - self.unavailable_lat) < 1e-6 and abs(
            longitude - self.unavailable_lon
        ) < 1e-6:
            return False, "ais_unavailable_encoding"
        for ph_lat, ph_lon in self.placeholder_positions:
            if abs(latitude - ph_lat) < 1e-9 and abs(longitude - ph_lon) < 1e-9:
                return False, "suspected_placeholder_position"
        return True, None

    def _time_block(
        self,
        *,
        message: Any,
        capture_time_utc: Optional[str],
        capture_unix: Optional[str],
        sim_context: Optional[time_fields.SimulationDateContext],
    ) -> Dict[str, Any]:
        """还原 AIS 状态时间。

        !AIVDM 的 `second` 只是分钟内秒数（0–59），且属于**仿真时钟**；
        抓包时间属于记录时钟，两者相差约 13 小时 16 分且速率略有差异。
        因此：
          1) 用同一数据流的 ZDA 锚点把抓包时间映射到仿真日内时间；
          2) 用报文自身的 second 在相邻两个分钟候选中选择偏差最小者，
             确定仿真分钟；
          3) 仿真 UTC = ZDA 日期 + 还原后的仿真日内时间。
        """
        minute_second = getattr(message, "second", None)
        capture_dt = time_fields.parse_capture_time(capture_time_utc or "")

        state_time_raw: Optional[str] = None
        seconds_of_day: Optional[float] = None
        association: Dict[str, Any] = {
            "zda_csv_row": None,
            "zda_time_of_day": None,
            "sim_time_delta_s": None,
            "capture_time_delta_s": None,
            "residual_s": None,
            "association": "no_context",
        }

        mapping = None
        if minute_second is not None and capture_unix is not None and sim_context is not None:
            try:
                mapping = sim_context.resolve_minute_second(
                    float(capture_unix), int(minute_second)
                )
            except (TypeError, ValueError):
                mapping = None

        if mapping is not None:
            seconds_of_day = mapping["sim_seconds_of_day"]
            association = {
                "anchor_zda_csv_row": mapping.get("anchor_zda_csv_row"),
                "zda_csv_row": mapping.get("anchor_zda_csv_row"),
                "zda_time_of_day": mapping.get("anchor_zda_time_of_day"),
                "capture_delta_s": mapping.get("capture_delta_s"),
                "clock_skew_s": mapping.get("clock_skew_s"),
                "association": "ok",
                "method": "zda_anchor_plus_vdm_minute_second",
                "rate_note": mapping.get("rate_note"),
            }
            hours = int(seconds_of_day // 3600)
            minutes = int((seconds_of_day % 3600) // 60)
            seconds = seconds_of_day % 60
            state_time_raw = f"{hours:02d}{minutes:02d}{int(seconds):02d}.{int(minute_second):02d}"
        elif minute_second is not None and capture_dt is not None:
            # 无仿真日期上下文时，只能给出记录时钟的日内时间原值
            state_time_raw = capture_dt.strftime("%H%M%S") + f".{int(minute_second):02d}"
            association["association"] = "no_context"

        state_time_utc: Optional[str] = None
        time_basis = schema.TIME_BASIS_TTM_TOD
        time_domain = schema.TIME_DOMAIN_UNKNOWN

        if sim_context is not None and sim_context.consistent and seconds_of_day is not None:
            state_time_utc = sim_context.utc_for_seconds_of_day(seconds_of_day)
            time_basis = schema.TIME_BASIS_TTM_TOD_ZDA_DATE
            time_domain = schema.TIME_DOMAIN_SIMULATION_UTC

        return {
            "state_time_raw": state_time_raw,
            "state_time_utc": state_time_utc,
            "time_basis": time_basis,
            "time_domain": time_domain,
            "time_precision": schema.TIME_PRECISION_SECOND,
            "receive_time_utc": capture_time_utc,
            "receive_time_source": "pcap_record_time" if capture_time_utc else None,
            "zda_association": association,
        }

    def _rejected(
        self,
        input_path: str,
        row_index: int,
        sentence: str,
        reason: str,
    ) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "session_id": self.session_id,
            "record_id": schema.make_record_id(
                self.source_id, input_path, row_index, session_id=self.session_id
            ),
            "source_file": input_path,
            "source_row": row_index,
            "reason": reason,
            "issue": validation.reject_reason_to_issue(reason),
            "raw_record": sentence,
        }




def _navigation_status(raw: Any) -> Optional[str]:
    """把 AIS 航行状态枚举转成可读文本。"""
    if raw is None:
        return None
    name = getattr(raw, "name", None)
    if name:
        return str(name)
    return str(raw)
