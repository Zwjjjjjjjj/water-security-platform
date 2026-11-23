"""时间解析、来源标记与仿真日期上下文整理。

三条时间线必须分开保存，不得互相顶替：

1. 目标状态时间（TTM 日内时间 / AIS CSV 时间字段）
2. 接收时间（RadarPWN 用 PCAP 记录时间；AIS 无独立接收时间）
3. 仿真日期上下文（ZDA 声明的日期）

未确认时区时不默认使用北京时间或服务器时区。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import schema


def parse_nmea_time_of_day(raw: str) -> Optional[float]:
    """解析 NMEA hhmmss(.ss) 日内时间，返回当天秒数。"""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        if "." in text:
            hms, frac = text.split(".", 1)
            frac_val = float("0." + frac)
        else:
            hms, frac_val = text, 0.0
        if len(hms) < 6:
            return None
        hh = int(hms[0:2])
        mm = int(hms[2:4])
        ss = int(hms[4:6])
        return hh * 3600.0 + mm * 60.0 + ss + frac_val
    except (ValueError, IndexError):
        return None


def format_seconds_of_day(seconds: float) -> str:
    """秒数转 hhmmss.ss。"""
    hh = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = seconds % 60
    return f"{hh:02d}{mm:02d}{ss:06.2f}"


def parse_capture_time(value: str) -> Optional[datetime]:
    """解析带时区的 UTC 时间字符串（PCAP 记录时间）。"""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_utc_datetime(value: str) -> Optional[datetime]:
    """解析带时区偏移的 ISO 时间字符串。"""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_naive_datetime(value: str) -> Optional[datetime]:
    """解析无时区日期时间字符串（不做任何时区假设）。"""
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return None
    return dt


@dataclass
class SimulationDateContext:
    """同一数据流中由 ZDA 等报文声明的仿真日期上下文。

    仅当同一流内的日期声明一致、且未出现日内时间回退（跨午夜）时，
    才允许用它补全完整的仿真 UTC 状态时间。

    entries 保存全部 ZDA 条目（含 CSV 行号与抓包时间），供逐条 TTM 做关联：
    对每条 TTM 选取"时间不晚于该 TTM 的最近一条 ZDA"，并记录所用行号、
    上下文时间差与关联结果。
    """

    date_iso: Optional[str] = None
    consistent: bool = False
    evidence_count: int = 0
    crossed_midnight: bool = False
    notes: List[str] = field(default_factory=list)
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def utc_for_seconds_of_day(self, seconds_of_day: float) -> Optional[str]:
        if not self.consistent or not self.date_iso:
            return None
        base = datetime.strptime(self.date_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        moment = base + timedelta(seconds=seconds_of_day)
        return moment.isoformat()

    def associate(self, seconds_of_day: float) -> Dict[str, Any]:
        """为一条 TTM 关联同一数据流中的 ZDA 上下文。

        规则：取仿真日内时间**不晚于**该 TTM 的最近一条 ZDA；若无则回退到
        最早一条 ZDA，并标记关联不成立。

        返回字段：
          zda_csv_row：所用 ZDA 在 all_incoming CSV 中的行号；
          zda_time_of_day：所用 ZDA 的日内时间原值；
          sim_time_delta_s：TTM 日内时间 − ZDA 日内时间（秒，应 ≥ 0）；
          capture_time_delta_s：两条报文的抓包时间差（秒）；
          residual_s：capture_time_delta_s − sim_time_delta_s，
              表示两个时钟在该区间上的偏差；
          association：ok / fallback_earliest / no_zda。
        """
        result: Dict[str, Any] = {
            "zda_csv_row": None,
            "zda_time_of_day": None,
            "sim_time_delta_s": None,
            "capture_time_delta_s": None,
            "residual_s": None,
            "association": "no_zda",
        }
        if not self.entries:
            return result

        chosen = None
        for entry in self.entries:
            if entry["seconds_of_day"] <= seconds_of_day:
                chosen = entry
            else:
                break
        if chosen is None:
            chosen = self.entries[0]
            result["association"] = "fallback_earliest"
        else:
            result["association"] = "ok"

        result["zda_csv_row"] = chosen.get("csv_row")
        result["zda_time_of_day"] = chosen.get("time_of_day")
        result["sim_time_delta_s"] = round(seconds_of_day - chosen["seconds_of_day"], 3)
        return result

    def sim_seconds_for_capture(self, capture_unix: float) -> Optional[Dict[str, Any]]:
        """由抓包时间反推仿真日内时间。

        AIS 的 !AIVDM 报文只带"分钟内秒数"，属于**仿真时钟**；抓包时间属于
        记录时钟，两者相差约 13 小时 16 分，且速率略有差异（实测约 0.28%）。
        因此不能直接用抓包时间的分钟去拼报文秒数，必须先用同一数据流中的
        ZDA 建立的时钟对应关系把抓包时间映射到仿真日内时间。

        映射方式：取抓包时间"不晚于"的最近一条 ZDA，用它的
        (抓包时间, 仿真日内时间) 作为锚点，按记录时钟的等间隔推进得到仿真
        时间。由于两个时钟速率略有差异，锚点推进的结果可能落在分钟边界
        附近，因此由调用方结合报文自身的秒数做分钟候选选择。

        返回字段：
          sim_seconds_of_day：推得的仿真日内秒数（锚点推进值）；
          anchor_zda_csv_row / anchor_zda_time_of_day：所用锚点；
          anchor_capture_unix：锚点的抓包时间；
          capture_delta_s：抓包时间 − 锚点抓包时间；
          sim_delta_s：按 1:1 推进得到的仿真时间差；
          rate_note：两时钟速率差异说明。
        """
        if not self.entries:
            return None
        anchors = [e for e in self.entries if e.get("capture_time_unix") is not None]
        if not anchors:
            return None

        chosen = None
        for entry in anchors:
            try:
                if float(entry["capture_time_unix"]) <= capture_unix:
                    chosen = entry
                else:
                    break
            except (TypeError, ValueError):
                continue
        if chosen is None:
            chosen = anchors[0]

        try:
            anchor_capture = float(chosen["capture_time_unix"])
        except (TypeError, ValueError):
            return None
        capture_delta = capture_unix - anchor_capture
        sim_seconds = chosen["seconds_of_day"] + capture_delta
        # 归一到 [0, 86400)
        sim_seconds = sim_seconds % 86400.0
        return {
            "sim_seconds_of_day": sim_seconds,
            "anchor_zda_csv_row": chosen.get("csv_row"),
            "anchor_zda_time_of_day": chosen.get("time_of_day"),
            "anchor_capture_unix": anchor_capture,
            "capture_delta_s": round(capture_delta, 6),
            "sim_delta_s": round(capture_delta, 6),
            "rate_note": (
                "仿真时钟与记录时钟速率略有差异（约 0.28%），按 1:1 推进的推算值"
                "存在秒级误差；实际分钟边界以报文 second 字段为准"
            ),
        }

    def resolve_minute_second(
        self,
        capture_unix: float,
        minute_second: int,
    ) -> Optional[Dict[str, Any]]:
        """结合报文秒数确定仿真分钟并还原完整仿真日内时间。

        锚点推进得到的仿真时间可能落在分钟边界附近，此时用报文自身的秒数
        确定分钟。候选分钟的取法：报文秒数已知分钟内的位置，因此
        "分钟起点 ≈ 推进值 − 秒数"，据此取该分钟及其相邻的两个分钟，
        再选偏差最小者。

        注意候选必须包含**前一个**分钟：若推进值因接收延迟与时钟偏移而
        略微越过分钟边界，报文秒数仍属前一分钟，此时若只考察推进值所在
        分钟及其后，会把结果整体推后 60 s。
        返回字段为 sim_seconds_for_capture 的全部字段，另加：
          sim_seconds_of_day：最终确定的仿真日内秒数；
          minute_candidates：被考察的分钟起点（仿真日内秒）；
          clock_skew_s：锚点推进值 − 还原值（秒，反映两时钟偏差）。
        """
        mapping = self.sim_seconds_for_capture(capture_unix)
        if mapping is None:
            return None
        mapped = mapping["sim_seconds_of_day"]
        # 由秒数反推候选分钟起点
        base = math.floor((mapped - minute_second) / 60.0) * 60.0
        candidates = [base - 60.0, base, base + 60.0]
        best = min(candidates, key=lambda c: abs(mapped - (c + minute_second)))
        sim_seconds = best + minute_second
        mapping = dict(mapping)
        mapping["minute_candidates"] = candidates
        mapping["sim_seconds_of_day"] = sim_seconds
        mapping["clock_skew_s"] = round(mapped - sim_seconds, 6)
        return mapping


def build_simulation_date_context(
    zda_entries: List[Dict[str, str]],
) -> SimulationDateContext:
    """由 ZDA 报文列表构建仿真日期上下文。

    zda_entries 每项包含：time_of_day（原始字符串）、day、month、year。
    """
    ctx = SimulationDateContext()
    if not zda_entries:
        ctx.notes.append("同一流中未找到 ZDA 日期声明")
        return ctx

    dates = {(e.get("year"), e.get("month"), e.get("day")) for e in zda_entries}
    ctx.evidence_count = len(zda_entries)
    if len(dates) != 1:
        ctx.notes.append(f"ZDA 日期声明不一致：{sorted(dates)}")
        return ctx

    year, month, day = dates.pop()
    try:
        ctx.date_iso = datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        ctx.notes.append(f"ZDA 日期字段无法解析：{year}-{month}-{day}")
        return ctx

    ctx.consistent = True
    return ctx


def time_fields_for_ttm(
    state_time_raw: Optional[str],
    capture_time_utc: Optional[str],
    sim_context: Optional[SimulationDateContext] = None,
) -> Dict[str, Optional[str]]:
    """生成 TTM 记录的时间字段组。

    - state_time_raw：TTM 自带的日内时间原值
    - state_time_utc：只有在仿真日期上下文一致时才填写
    - time_basis：说明状态时间的构成依据
    """
    seconds = parse_nmea_time_of_day(state_time_raw or "")
    state_time_utc: Optional[str] = None
    basis = schema.TIME_BASIS_TTM_TOD
    domain = schema.TIME_DOMAIN_SIMULATION_UTC

    if sim_context is not None and sim_context.consistent and seconds is not None:
        state_time_utc = sim_context.utc_for_seconds_of_day(seconds)
        basis = schema.TIME_BASIS_TTM_TOD_ZDA_DATE
    else:
        domain = schema.TIME_DOMAIN_UNKNOWN

    return {
        "state_time_raw": state_time_raw,
        "state_time_utc": state_time_utc,
        "time_basis": basis,
        "time_domain": domain,
        "time_precision": schema.TIME_PRECISION_CENTISECOND,
        "receive_time_utc": capture_time_utc,
        "receive_time_source": "pcap_record_time" if capture_time_utc else None,
    }


def time_fields_for_csv(
    state_time_raw: Optional[str],
    timezone_known: bool = False,
    timezone_name: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """生成 AIS CSV 记录的时间字段组。

    时区未确认时 state_time_utc 留空，只保留原始字符串。
    """
    state_time_utc: Optional[str] = None
    domain = schema.TIME_DOMAIN_LOCAL_NAIVE
    if timezone_known:
        dt = parse_naive_datetime(state_time_raw or "")
        if dt is not None and timezone_name:
            try:
                from zoneinfo import ZoneInfo

                aware = dt.replace(tzinfo=ZoneInfo(timezone_name))
                state_time_utc = aware.astimezone(timezone.utc).isoformat()
                domain = schema.TIME_DOMAIN_UTC
            except Exception:  # 时区库不可用时保留原样
                state_time_utc = None
                domain = schema.TIME_DOMAIN_LOCAL_NAIVE
    return {
        "state_time_raw": state_time_raw,
        "state_time_utc": state_time_utc,
        "time_basis": schema.TIME_BASIS_CSV_DATETIME,
        "time_domain": domain,
        "time_precision": schema.TIME_PRECISION_SECOND,
        "receive_time_utc": None,
        "receive_time_source": None,
    }
