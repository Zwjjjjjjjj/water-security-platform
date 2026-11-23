"""时空对齐模块的配置结构。

首轮全部使用**已知参数**；参数自动估计放到后续阶段。

配置内容：
  - 现场坐标原点、坐标基准（frame_id / datum）
  - 雷达站位置、安装方向与位置参考点偏移
  - 各数据源的已知时间偏移
  - 各数据源的**报文编码精度**（用于计算位置误差上界）
  - 各数据源的**传感器误差模型**（噪声标准差、量化步长）
  - 输出时间间隔、最大允许插值间隔、插值方式

设计原则：配置是模块唯一的外部参数来源。更换输入、现场原点或传感器参数
只需改本文件，不需要改代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MODULE_VERSION = "1.0.0"

# 插值方式
INTERP_ENU_FIRST = "enu_first"
INTERP_OBSERVATION_DOMAIN = "observation_domain"


@dataclass
class SiteFrameConfig:
    """现场坐标基准。"""

    frame_id: str = "site_enu"
    datum: str = "WGS84"
    origin_latitude_deg: float = 54.108200
    origin_longitude_deg: float = 12.056800
    origin_height_m: float = 0.0
    description: Optional[str] = None

    def validate(self) -> List[str]:
        """返回不可用原因列表；为空表示配置可用。"""
        problems: List[str] = []
        if self.origin_latitude_deg is None or self.origin_longitude_deg is None:
            problems.append("missing_site_frame_origin")
        else:
            if not -90.0 <= float(self.origin_latitude_deg) <= 90.0:
                problems.append("invalid_site_frame_origin_latitude")
            if not -180.0 <= float(self.origin_longitude_deg) <= 180.0:
                problems.append("invalid_site_frame_origin_longitude")
        if self.datum.upper() != "WGS84":
            problems.append(f"unsupported_datum:{self.datum}")
        return problems


@dataclass
class RadarStationConfig:
    """雷达站参数。

    install_heading_deg：雷达天线安装方向（真北顺时针）。
    标准 TTM 报文的方位已是真方位（参考 T），因此该参数**不参与**方位换算，
    仅作记录与追溯。

    reference_point_offset_east_m / _north_m / _up_m：位置参考点相对站址的偏移，
    在站址位置上单独叠加。

    magnetic_variation_deg：磁北参考换算所需的磁差；未配置时 M 参考方位无法
    转换为真方位，相关记录会被标记为不可用。
    """

    station_latitude_deg: Optional[float] = 54.108200
    station_longitude_deg: Optional[float] = 12.056800
    station_height_m: float = 0.0
    install_heading_deg: float = 0.0
    reference_point_offset_east_m: float = 0.0
    reference_point_offset_north_m: float = 0.0
    reference_point_offset_up_m: float = 0.0
    magnetic_variation_deg: Optional[float] = None

    def validate(self) -> List[str]:
        problems: List[str] = []
        if self.station_latitude_deg is None or self.station_longitude_deg is None:
            problems.append("missing_radar_station_position")
        else:
            if not -90.0 <= float(self.station_latitude_deg) <= 90.0:
                problems.append("invalid_radar_station_latitude")
            if not -180.0 <= float(self.station_longitude_deg) <= 180.0:
                problems.append("invalid_radar_station_longitude")
        return problems


@dataclass
class EncodingPrecision:
    """报文的**编码精度**（用于计算位置误差上界）。

    distance_lsb + distance_unit：距离字段的最小刻度（编码时四舍五入到该刻度，
    因此单次编码误差上界为刻度的一半）。
    bearing_lsb_deg：方位字段的最小刻度（度）。
    position_lsb_deg：位置字段（AIS 经纬度）的**线上**最小刻度（度）。
    decoded_position_lsb_deg：解码端输出的最小刻度（度）。AIS 解码会把经纬度
    舍入到 1e-5 度，这一步的误差同样计入上界。
    """

    distance_lsb: Optional[float] = None
    distance_unit: Optional[str] = None
    bearing_lsb_deg: Optional[float] = None
    position_lsb_deg: Optional[float] = None
    decoded_position_lsb_deg: Optional[float] = None


@dataclass
class SensorErrorModel:
    """传感器误差模型（先验，用于把已知误差计入位置误差上界）。

    position_noise_std_m：位置噪声标准差（米）。零均值高斯下取 3σ 作为上界。
    quantization_lsb_deg：观测坐标量化步长（度）。单次量化误差上界为步长的一半。
    """

    position_noise_std_m: float = 0.0
    quantization_lsb_deg: float = 0.0
    noise_sigma_multiplier: float = 3.0


@dataclass
class SourceTimeConfig:
    """单个数据源的时间修正参数。

    known_clock_offset_s：已知的时钟偏移（秒）。正表示该源的读数比公共时间
    基准**快**多少。因此修正后的公共时刻 = 原始读数 − 偏移。

    time_resolution_s：该源时间字段的**分辨率**（秒）。用于如实标注不可还原
    的亚秒部分，不参与修正。

    time_quantum_s：该源时间字段的**量化步长**（秒，0 表示不量化）。报文里
    时间字段只能表达整数秒时为 1.0。

    known_quantum_bias_s：量化引入的**已知恒定偏差**（秒），默认 0。
    量化步长为 1 s 且对亚秒部分向整分方向截断时，若已知时钟偏移的亚秒部分
    为 −f，则读数比真实时刻早 f 秒，即 bias = −f。
    该值必须显式配置而不是自动推断：截断方向（向零/向负）与偏移符号的
    组合决定偏差，能从消息本身恢复的只有"读数所在的那个整秒"，被丢掉的那
    一半无法从报文反推，因此只能作为**已知先验**给出。
    """

    known_clock_offset_s: float = 0.0
    time_resolution_s: float = 1.0
    time_quantum_s: float = 0.0
    known_quantum_bias_s: float = 0.0
    use_state_time_utc: bool = True
    fallback_to_receive_time: bool = True
    # 相对时间基准模式
    #
    # 当该源的时区未确认（state_time_utc 为空）但**与同场景其他源共享同一
    # 时钟基准**时，绝对 UTC 时刻虽不可知，源间的**相对**时间差仍然确定。
    # 此时可启用相对时间基准：用 state_time_raw 解析出的本地时刻，
    # 按本场景统一的参考时刻（reference_local_datetime）折算成"相对秒"。
    #
    # 适用前提：同一对齐场景内的所有源使用同一设备时钟。
    # 该模式下产出的时间标记为 time_domain="relative_local"，**不是 UTC**，
    # 不能与外部数据直接比较；只能用于同场景内的插值与异步匹配。
    use_relative_time_base: bool = False
    # 场景参考时刻（本地时间字符串 "YYYY-MM-DD HH:MM:SS"）。
    # 各源的相对时刻 = (本记录本地时刻 − 参考时刻) 的秒数。
    reference_local_datetime: Optional[str] = None
    # 配置说明性字段（不参与计算），原样保留以便追溯
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceConfig:
    """单个数据源的完整配置。"""

    source_id: str
    source_type: str
    time: SourceTimeConfig
    encoding: EncodingPrecision = field(default_factory=EncodingPrecision)
    error_model: SensorErrorModel = field(default_factory=SensorErrorModel)
    # 期望的局部标识字段（雷达目标号 / MMSI）；仅作记录与核对
    local_id_field: Optional[str] = None
    # 说明性字段（不参与计算），原样保留以便追溯
    notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlignmentConfig:
    """对齐总配置。"""

    alignment_config_id: str = "alignment_v1"
    parameter_version: str = "known_params_v1"
    module_version: str = MODULE_VERSION
    site_frame: SiteFrameConfig = field(default_factory=SiteFrameConfig)
    radar_station: RadarStationConfig = field(default_factory=RadarStationConfig)
    sources: List[SourceConfig] = field(default_factory=list)
    # 公共时间栅格间隔（秒）
    output_time_step_s: float = 1.0
    # 允许的最大插值间隔（秒）；超过则该栅格点标为不可用
    max_interpolation_gap_s: float = 10.0
    # 插值方式：enu_first（基线，先转 ENU 再对位置线性插值）/
    # observation_domain（在各自观测域内插值后再转 ENU）
    interpolation_mode: str = INTERP_ENU_FIRST
    # 本船艏向（度，真北顺时针）；方位参考为 R 时需要
    own_ship_heading_deg: Optional[float] = None
    # 公共时间栅格起点（仿真日内时间，秒）
    time_grid_start_seconds_of_day: float = 54000.0
    # 时间基准说明（记录时区状态、相对时基是否可用及其证据）。
    # 该字段是**说明性**的，不参与任何计算 —— 时间行为仍由各源的
    # SourceTimeConfig 决定。此处单独记录是为了让"绝对时区未知"
    # 这一事实成为配置的显式声明，而不是隐含在 notes 里。
    time_basis: Dict[str, Any] = field(default_factory=dict)
    # 雷达帧头（IF/IQ）平台自身状态的接入配置。
    #
    # 该配置只影响**附加信息**：把 IF 帧头里的平台自身航向角与速度
    # 关联到雷达记录上（字段 `platform_heading_deg` / `platform_speed_mps`）。
    #
    # ⚠️ 平台航向**不参与**方位→ENU 的换算。实测证据：用**权威 AIS**
    # 反算真方位（IF 帧头给出的雷达自记站址 37.53297, 121.42020），
    # "不修正"时方位残差中位数仅 0.446°(03-05) / 0.530°(03-12)；
    # 叠加平台航向则恶化到 6.60° / 6.43°，叠加 `角度修正(°)=344`
    # 恶化到 15.55° / 15.87° ⇒ `目标方位(°)` 已是真方位基准，
    # 与平台艏向无关。把它当成方位修正会引入错误。
    #
    # 未配置（None）时不接入，既有行为完全不变。
    platform_state: Optional[Dict[str, Any]] = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "AlignmentConfig":
        data = dict(data)
        if "site_frame" in data:
            data["site_frame"] = SiteFrameConfig(**data["site_frame"])
        if "radar_station" in data:
            data["radar_station"] = RadarStationConfig(**data["radar_station"])
        sources: List[SourceConfig] = []
        for item in data.get("sources", []):
            item = dict(item)
            time_data = dict(item.pop("time", {}))
            # 未被识别的键保留为注释，便于配置文件写说明而不被丢弃
            time_notes = {
                key: time_data.pop(key)
                for key in list(time_data)
                if key not in SourceTimeConfig.__dataclass_fields__
            }
            time_cfg = SourceTimeConfig(**time_data)
            if time_notes:
                time_cfg.notes = time_notes
            encoding = EncodingPrecision(**item.pop("encoding", {}))
            error_model = SensorErrorModel(**item.pop("error_model", {}))
            source_id = item.pop("source_id")
            source_type = item.pop("source_type")
            sources.append(
                SourceConfig(
                    source_id=source_id,
                    source_type=source_type,
                    time=time_cfg,
                    encoding=encoding,
                    error_model=error_model,
                    **item,
                )
            )
        data["sources"] = sources
        return AlignmentConfig(**data)

    def source_config(self, source_id: str) -> Optional[SourceConfig]:
        for item in self.sources:
            if item.source_id == source_id:
                return item
        return None

    def validate(self) -> List[str]:
        """整体配置校验，返回不可用原因列表。"""
        problems: List[str] = []
        problems.extend(self.site_frame.validate())
        problems.extend(self.radar_station.validate())
        if self.interpolation_mode not in (
            INTERP_ENU_FIRST,
            INTERP_OBSERVATION_DOMAIN,
        ):
            problems.append(f"unknown_interpolation_mode:{self.interpolation_mode}")
        if self.output_time_step_s <= 0:
            problems.append("invalid_output_time_step_s")
        if self.max_interpolation_gap_s <= 0:
            problems.append("invalid_max_interpolation_gap_s")
        if not self.sources:
            problems.append("no_sources_configured")
        return problems
