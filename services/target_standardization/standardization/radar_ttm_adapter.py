"""RadarPWN 雷达 TTM 适配器。

输入：radar_ttm_with_capture_time.csv（每行 = PCAP 记录时间 + 原始 TTM 报文），
可选辅以 all_incoming_nmea_with_capture_time.csv（用于建立仿真日期上下文）。

TTM 字段布局（按厂家手册中的标准 TTM 布局解析，字段编号自"目标编号"起算为 1）：

    字段 | 含义
    -----+--------------------------------------------------
      1  | 目标编号（保留前导零）
      2  | 距离
      3  | 方位（度）
      4  | 方位参考 T/M/R
      5  | 速度
      6  | 航向（度）
      7  | 航向参考 T/M/R
      8  | CPA 距离
      9  | CPA 时间
     10  | Speed/distance units（N=海里+节 / K=千米+千米每小时 / S=英里+英里每小时）
     11  | 目标名称
     12  | 跟踪状态 Q/T/L
     13  | 参考目标标记
     14  | 日内时间（仿真 UTC 日内时间）
     15  | 获取类型 A/M

索引计数说明：
- 本文件中的 field_mapping 使用 0-based 索引，且**不含**开头的 `$RATTM`，
  即 `target_number: 0` 对应报文第 1 个字段。报文本体首字段 "$RATTM" 已在
  解析时剥离。因此手册中的"第 10 字段"对应索引 9。
- 第 10 字段同时给出距离单位与速度单位，本数据集该字段为 'N'
  （海里 + 节），因此不需要任何单位假定。
"""

from __future__ import annotations

import csv
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pynmea2

from . import schema, time_fields, units


class RadarTTMAdapter:
    """把 RadarPWN TTM 报文流转换为公共标准记录。"""

    source_type = schema.SOURCE_TYPE_RADAR

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.source_id = source_config["source_id"]
        self.session_id = source_config["session_id"]
        self.frame_id = source_config.get("frame_id", "radar_local_polar")
        self.mapping = source_config.get("field_mapping", {})
        self.date_binding = source_config.get("simulation_date_binding", "zda_date")
        # 单位：TTM 报文第 10 字段（Speed/distance units）同时给出距离与速度单位；
        # 配置仅作为兜底声明，不再作为"假定"。
        self.unit_field = self.mapping.get("speed_distance_units", 9)
        self.distance_unit_field = self.mapping.get("distance_unit", 9)
        self.unit_fallback = source_config.get("unit_fallback")
        self.time_fields = source_config.get("time_fields", {})

    # -- 读取 ---------------------------------------------------------------

    def iter_source_rows(self, input_path: str) -> Iterator[Dict[str, Any]]:
        with open(input_path, "r", encoding="utf-8-sig", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), start=1):
                yield {"row_index": index, "row": row}

    # -- 主流程 -------------------------------------------------------------

    def run(
        self,
        input_path: str,
        sim_context: Optional[time_fields.SimulationDateContext] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """返回 (标准记录, 拒绝记录, 处理日志行)。"""
        records: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        log: List[str] = []

        for entry in self.iter_source_rows(input_path):
            row = entry["row"]
            row_index = entry["row_index"]
            sentence = (row.get("sentence") or "").strip()

            if not sentence:
                rejected.append(
                    self._rejected(
                        input_path, row_index, sentence, "empty_sentence", sentence
                    )
                )
                continue

            if not self._is_ttm(sentence):
                rejected.append(
                    self._rejected(
                        input_path, row_index, sentence, "not_a_ttm_sentence", sentence
                    )
                )
                continue

            checksum_ok = self._checksum_ok(sentence)
            if not checksum_ok:
                rejected.append(
                    self._rejected(
                        input_path, row_index, sentence, "bad_checksum", sentence
                    )
                )
                continue

            try:
                pynmea2.parse(sentence)
            except Exception as exc:  # pynmea2 解析异常
                rejected.append(
                    self._rejected(
                        input_path,
                        row_index,
                        sentence,
                        f"parse_error: {type(exc).__name__}",
                        sentence,
                    )
                )
                continue

            fields = self._split_fields(sentence)
            record = self._build_record(
                input_path=input_path,
                row_index=row_index,
                row=row,
                sentence=sentence,
                fields=fields,
                sim_context=sim_context,
            )
            if record is None:
                rejected.append(
                    self._rejected(
                        input_path, row_index, sentence, "invalid_field_value", sentence
                    )
                )
                continue
            records.append(record)

        log.append(f"radar: 输入行 {len(records) + len(rejected)}，输出 {len(records)}，拒绝 {len(rejected)}")
        return records, rejected, log

    # -- 内部实现 -----------------------------------------------------------

    def _rejected(
        self,
        input_path: str,
        row_index: int,
        sentence: str,
        reason: str,
        raw: str,
    ) -> Dict[str, Any]:
        from .validation import reject_reason_to_issue

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
            "issue": reject_reason_to_issue(reason),
            "raw_record": raw or sentence,
        }

    @staticmethod
    def _is_ttm(sentence: str) -> bool:
        body = sentence.split("*")[0]
        parts = body.strip("$").split(",")
        return bool(parts) and parts[0].endswith("TTM")

    @staticmethod
    def _checksum_ok(sentence: str) -> bool:
        if "*" not in sentence:
            return False
        body, checksum = sentence.split("*", 1)
        checksum = checksum.strip()
        try:
            expected = int(checksum[:2], 16)
        except ValueError:
            return False
        calculated = 0
        for char in body[1:]:
            calculated ^= ord(char)
        return calculated == expected

    @staticmethod
    def _split_fields(sentence: str) -> List[str]:
        """剥离 `$RATTM` 首字段与 `*校验和`，返回其余字段（0-based 对应字段 1..15）。"""
        body = sentence.split("*")[0]
        return body.strip("$").split(",")[1:]

    def _build_record(
        self,
        *,
        input_path: str,
        row_index: int,
        row: Dict[str, Any],
        sentence: str,
        fields: List[str],
        sim_context: Optional[time_fields.SimulationDateContext],
    ) -> Optional[Dict[str, Any]]:
        m = self.mapping

        def get(key: str, default: int = -1) -> Optional[str]:
            index = m.get(key, default)
            if index < 0 or index >= len(fields):
                return None
            value = fields[index].strip()
            return value or None

        target_number = get("target_number", 0)
        distance_raw = get("distance", 1)
        bearing_raw = get("bearing", 2)
        brg_ref = get("bearing_reference", 3)
        speed_raw = get("speed", 4)
        course_raw = get("course", 5)
        course_ref = get("course_reference", 6)
        cpa_distance_raw = get("cpa_distance", 7)
        cpa_time_raw = get("cpa_time", 8)
        target_name = get("target_name", 10)
        status_raw = get("tracking_status", 11)
        reference_target = get("reference_target", 12)
        time_of_day = get("time_of_day", 13)
        acquisition = get("acquisition_type", 14)

        # 单位：优先取报文自带的 Speed/distance units（第 10 字段，索引 9）
        speed_distance_units_raw = get("speed_distance_units", 9)
        distance_unit_raw = get("distance_unit", 9)

        if target_number is None:
            return None

        distance_m = self._to_float(distance_raw)
        bearing_deg = self._to_float(bearing_raw)
        speed_kn = self._to_float(speed_raw)
        course_deg = self._to_float(course_raw)

        if distance_m is None or bearing_deg is None:
            return None

        unit_pair = units.ttm_unit_pair(speed_distance_units_raw)
        unit_source = "ttm_field_10_speed_distance_units"
        if unit_pair is None:
            unit_pair = units.ttm_unit_pair(distance_unit_raw)
            unit_source = "ttm_field_10_unit_of_distance"
        if unit_pair is None and self.unit_fallback:
            unit_pair = units.ttm_unit_pair(self.unit_fallback)
            unit_source = "config_unit_fallback"
        if unit_pair is None:
            return None

        distance_unit_name, distance_factor, speed_unit_name, speed_factor = unit_pair
        range_m = float(distance_m) * distance_factor
        bearing_rad = units.deg_to_rad(bearing_deg)
        speed_mps = float(speed_kn) * speed_factor if speed_kn is not None else None
        course_rad = units.deg_to_rad(course_deg)

        if bearing_rad is None or range_m < 0.0:
            return None

        capture_time_utc = row.get("capture_time_utc")
        time_block = time_fields.time_fields_for_ttm(
            state_time_raw=time_of_day,
            capture_time_utc=capture_time_utc,
            sim_context=sim_context if self.date_binding == "zda_date" else None,
        )
        association = self._associate(time_of_day, sim_context, row)

        record = schema.new_record(
            source_id=self.source_id,
            source_type=self.source_type,
            session_id=self.session_id,
            local_track_id=target_number,
            state_time_raw=time_block["state_time_raw"],
            state_time_utc=time_block["state_time_utc"],
            time_basis=time_block["time_basis"],
            time_domain=time_block["time_domain"],
            time_precision=time_block["time_precision"],
            receive_time_utc=time_block["receive_time_utc"],
            receive_time_source=time_block["receive_time_source"],
            position_type=schema.POSITION_TYPE_RANGE_BEARING,
            frame_id=self.frame_id,
            position={
                "range_m": round(range_m, 3),
                "bearing_rad": round(bearing_rad, 9),
                "bearing_reference": brg_ref,
            },
            speed_mps=round(speed_mps, 6) if speed_mps is not None else None,
            course_rad=round(course_rad, 9) if course_rad is not None else None,
            course_reference=course_ref,
            motion_reference=None,  # 速度参考（对地/对水）未在报文中声明，保持未知
            heading_rad=None,
            tracking_status=schema.tracking_status_from_raw(status_raw),
            navigation_status=None,
            source_file=input_path,
            source_row=row_index,
            raw_record=sentence,
        )

        # 扩展字段：保留原值与辅助语义，便于回查
        record["target_number_raw"] = target_number
        record["distance_raw"] = distance_raw
        record["distance_unit_raw"] = distance_unit_raw
        record["speed_distance_units_raw"] = speed_distance_units_raw
        record["bearing_raw"] = bearing_raw
        record["speed_raw"] = speed_raw
        record["distance_unit"] = distance_unit_name
        record["speed_unit"] = speed_unit_name
        record["unit_source"] = unit_source
        record["course_raw"] = course_raw
        record["target_name"] = target_name
        record["tracking_status_raw"] = status_raw
        record["reference_target_raw"] = reference_target
        record["acquisition_type_raw"] = acquisition
        record["cpa_distance_raw"] = cpa_distance_raw
        record["cpa_time_raw"] = cpa_time_raw
        record["checksum_valid"] = True
        record["capture_time_unix_raw"] = row.get("capture_time_unix")
        record["receive_time_unix"] = self._to_float(row.get("capture_time_unix"))
        record["zda_association"] = association

        # 校验与有效性判定
        from . import validation

        validation.validate_common(record)
        if status_raw is None:
            schema.add_issue(record, schema.ISSUE_INVALID_VALUE)
        validation.assess_validity(record)
        return record

    @staticmethod
    def _to_float(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _associate(
        time_of_day: Optional[str],
        sim_context: Optional[time_fields.SimulationDateContext],
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        """关联同一数据流中的 ZDA 上下文，并计算两个时钟的偏差。"""
        if sim_context is None:
            return {
                "zda_csv_row": None,
                "zda_time_of_day": None,
                "sim_time_delta_s": None,
                "capture_time_delta_s": None,
                "residual_s": None,
                "association": "no_context",
            }
        seconds = time_fields.parse_nmea_time_of_day(time_of_day or "")
        if seconds is None:
            return {
                "zda_csv_row": None,
                "zda_time_of_day": None,
                "sim_time_delta_s": None,
                "capture_time_delta_s": None,
                "residual_s": None,
                "association": "no_zda",
            }

        result = sim_context.associate(seconds)

        # 用所选 ZDA 的抓包时间与本条 TTM 的抓包时间之差，
        # 减去仿真日内时间之差，得到两个时钟在该区间上的偏差（秒）。
        capture_unix = row.get("capture_time_unix")
        zda_row = result.get("zda_csv_row")
        zda_entry = None
        for entry in sim_context.entries:
            if entry.get("csv_row") == zda_row:
                zda_entry = entry
                break
        if capture_unix and zda_entry and zda_entry.get("capture_time_unix"):
            try:
                capture_delta = float(capture_unix) - float(zda_entry["capture_time_unix"])
            except (TypeError, ValueError):
                capture_delta = None
            if capture_delta is not None:
                result["capture_time_delta_s"] = round(capture_delta, 6)
                sim_delta = result.get("sim_time_delta_s")
                if sim_delta is not None:
                    result["residual_s"] = round(capture_delta - sim_delta, 6)
        return result


def build_simulation_context(
    all_incoming_csv: Optional[str],
) -> Tuple[Optional[time_fields.SimulationDateContext], List[str]]:
    """从 all_incoming CSV 中的 ZDA 报文建立仿真日期上下文。

    关联规则（不假设两组记录一一对应）：
      对每条 TMM，按其仿真日内时间在**同一数据流**中选取"时间不晚于该 TTM 的
      最近一条 ZDA"，用该 ZDA 的日期声明与 TTM 日内时间组合出仿真 UTC。
    同时检查：日期声明是否冲突、日内时间是否回退、是否跨午夜。
    """
    log: List[str] = []
    if not all_incoming_csv:
        log.append("radar: 未提供 all_incoming CSV，仿真日期上下文不可用")
        return None, log

    entries: List[Dict[str, Any]] = []
    with open(all_incoming_csv, "r", encoding="utf-8-sig", newline="") as handle:
        for row_index, row in enumerate(csv.DictReader(handle), start=2):
            sentence = (row.get("sentence") or "").strip()
            if not sentence or "ZDA" not in sentence.split(",")[0]:
                continue
            parts = sentence.split("*")[0].strip("$").split(",")
            if len(parts) < 5:
                continue
            seconds = time_fields.parse_nmea_time_of_day(parts[1])
            if seconds is None:
                continue
            entries.append(
                {
                    "csv_row": row_index,
                    "time_of_day": parts[1],
                    "seconds_of_day": seconds,
                    "day": parts[2],
                    "month": parts[3],
                    "year": parts[4],
                    "capture_time_unix": row.get("capture_time_unix"),
                }
            )

    context = time_fields.build_simulation_date_context(entries)
    context.entries = entries

    if entries:
        seconds_list = [e["seconds_of_day"] for e in entries]
        rollback = sum(
            1 for a, b in zip(seconds_list, seconds_list[1:]) if b < a
        )
        crossed_midnight = rollback > 0
        context.crossed_midnight = crossed_midnight
        if crossed_midnight:
            context.consistent = False
            context.notes.append(
                f"ZDA 日内时间出现 {rollback} 处回绕，跨午夜处理未核验，不生成完整仿真 UTC"
            )
        log.append(
            f"radar: ZDA 条目 {len(entries)} 条，日期集合 "
            f"{sorted({(e['year'], e['month'], e['day']) for e in entries})}，"
            f"日内时间回退 {rollback} 处，跨午夜={crossed_midnight}，"
            f"上下文可用={context.consistent}"
        )
    else:
        log.append("radar: 未找到可解析的 ZDA 日内时间，仿真日期上下文不可用")

    for note in context.notes:
        log.append(f"radar: 上下文说明 {note}")
    return context, log
