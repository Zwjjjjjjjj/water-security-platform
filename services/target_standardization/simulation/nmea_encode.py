"""NMEA 报文编码：TTM、ZDA、RMC、AIS !AIVDM。

编码严格按各报文的**标准布局**：

TTM（手册第 N 字段 ↔ 剥离 `$RATTM` 后索引 N-1）：
      1 目标编号 / 2 距离 / 3 方位 / 4 方位参考 / 5 速度 / 6 航向 /
      7 航向参考 / 8 CPA 距离 / 9 CPA 时间 / 10 Speed-distance units /
     11 目标名称 / 12 跟踪状态 / 13 参考目标标记 / 14 日内时间 / 15 获取类型

第 10 字段同时规定距离与速度单位：N=海里+节 / K=千米+千米每小时 / S=英里+英里每小时。
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple


def nmea_checksum(body: str) -> str:
    """计算 NMEA 校验和（body 不含 '$' 与 '*checksum'）。"""
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def _sentence(body: str) -> str:
    """拼装带校验和的完整报文。"""
    return f"{body}*{nmea_checksum(body)}"


def format_time_of_day(seconds_of_day: float, decimals: int = 2) -> str:
    """秒数 → NMEA 日内时间字符串 hhmmss.ss。"""
    seconds_of_day = float(seconds_of_day) % 86400.0
    hours = int(seconds_of_day // 3600)
    minutes = int((seconds_of_day % 3600) // 60)
    seconds = seconds_of_day % 60
    # 秒位与小数位分开格式化：秒用 2 位定点，小数部分按 decimals 位
    whole_seconds = int(seconds)
    fraction = seconds - whole_seconds
    text = f"{hours:02d}{minutes:02d}{whole_seconds:02d}"
    if decimals > 0:
        # 去掉定点格式里的整数位 "0"，只保留小数点及其后若干位
        text += f"{fraction:.{decimals}f}"[1:]
    return text


def format_latitude(latitude_deg: float, decimals: int = 4) -> str:
    """纬度 → ddmm.mmmm。"""
    hemisphere = "N" if latitude_deg >= 0 else "S"
    magnitude = abs(latitude_deg)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60.0
    return f"{degrees:02d}{minutes:0{3 + decimals}.{decimals}f}", hemisphere


def format_longitude(longitude_deg: float, decimals: int = 4) -> str:
    """经度 → dddmm.mmmm。"""
    hemisphere = "E" if longitude_deg >= 0 else "W"
    magnitude = abs(longitude_deg)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60.0
    return f"{degrees:03d}{minutes:0{3 + decimals}.{decimals}f}", hemisphere


def encode_ttm(
    *,
    target_number: str,
    distance_nm: float,
    bearing_deg: float,
    bearing_reference: str = "T",
    speed_kn: float = 0.0,
    course_deg: float = 0.0,
    course_reference: str = "T",
    cpa_distance_nm: float = 0.0,
    cpa_time_min: float = 0.0,
    unit: str = "N",
    target_name: str = "",
    tracking_status: str = "T",
    reference_target: str = "",
    time_of_day: str = "",
    acquisition_type: str = "A",
) -> str:
    """编码一条 TTM 报文。

    time_of_day 直接传 NMEA 日内时间字符串（hhmmss.ss）。
    """
    body = (
        f"$RATTM,{target_number},{distance_nm:.3f},{bearing_deg:.2f},"
        f"{bearing_reference},{speed_kn:.1f},{course_deg:.1f},{course_reference},"
        f"{cpa_distance_nm:.3f},{cpa_time_min:.1f},{unit},{target_name},"
        f"{tracking_status},{reference_target},{time_of_day},{acquisition_type}"
    )
    return _sentence(body)


def encode_zda(
    *,
    time_of_day: str,
    day: int,
    month: int,
    year: int,
    local_zone_hours: int = 0,
    local_zone_minutes: int = 0,
) -> str:
    """编码一条 ZDA 报文（日期上下文来源）。"""
    body = (
        f"$RAZDA,{time_of_day},{day:02d},{month:02d},{year:04d},"
        f"{local_zone_hours:02d},{local_zone_minutes:02d}"
    )
    return _sentence(body)


def encode_rmc(
    *,
    time_of_day: str,
    latitude_deg: float,
    longitude_deg: float,
    speed_kn: float,
    course_deg: float,
    date_ddmmyy: str,
) -> str:
    """编码一条 RMC 报文（本船位置/时间上下文）。"""
    lat_value, lat_hemi = format_latitude(latitude_deg)
    lon_value, lon_hemi = format_longitude(longitude_deg)
    body = (
        f"$GPRMC,{time_of_day},A,{lat_value},{lat_hemi},{lon_value},{lon_hemi},"
        f"{speed_kn:.1f},{course_deg:.1f},{date_ddmmyy},,,A"
    )
    return _sentence(body)


def encode_ais_vdm(
    *,
    mmsi: int,
    latitude_deg: float,
    longitude_deg: float,
    speed_kn: float,
    course_deg: float,
    heading_deg: int,
    second_of_minute: int,
    status: int = 0,
    radio_channel: str = "B",
    accuracy: bool = True,
    raim: bool = False,
    talker_id: str = "AI",
) -> str:
    """编码一条 AIS 位置报文（!AIVDM，消息类型 1）。

    second_of_minute 是**仿真时钟**的分钟内秒数（0–59），与真实 RadarPWN
    数据的语义一致。
    """
    import pyais
    from pyais.messages import MessageType1

    message = MessageType1(
        msg_type=1,
        repeat=0,
        mmsi=int(mmsi),
        status=status,
        turn=0,
        speed=float(speed_kn),
        accuracy=accuracy,
        lon=float(longitude_deg),
        lat=float(latitude_deg),
        course=float(course_deg) % 360.0,
        heading=int(heading_deg) % 360,
        second=int(second_of_minute) % 60,
        maneuver=0,
        spare_1=0,
        raim=raim,
        radio=0,
    )
    sentences = pyais.encode_msg(
        message, talker_id=talker_id, sentence_type="VDM", radio_channel=radio_channel
    )
    return sentences[0]


def verify_sentence(sentence: str) -> bool:
    """校验报文校验和是否正确。"""
    if "*" not in sentence:
        return False
    body, checksum = sentence.split("*", 1)
    try:
        expected = int(checksum.strip()[:2], 16)
    except ValueError:
        return False
    calculated = 0
    for char in body[1:]:
        calculated ^= ord(char)
    return calculated == expected
