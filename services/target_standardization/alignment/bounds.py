"""位置误差上界计算。

## 模型

误差向量在雷达几何下分解为径向与切向两个分量：

    e = e_rad · r̂ + e_tan · t̂        r̂ = (sinθ, cosθ),  t̂ = (cosθ, −sinθ)

各分量的上限：

    |e_rad| ≤ 编码径向上限 + 噪声轴限
    |e_tan| ≤ 编码切向上限 + 噪声轴限

    平面上限 = hypot(径向上限, 切向上限)

**为什么噪声不按 √2 分摊到两轴**：噪声是二维各向同性的，但**逐轴**比较时
每轴的上限就是 k·σ 本身。若把 k·σ 分摊到两轴，逐轴上限会被低估（σ/√2），
导致"逐轴未超限"的记录被误判为超限。平面合成时两轴各自独立取到上限，
hypot 组合仍是有效的保守上界。

## 误差来源

  1. **报文编码量化** —— 距离/方位/经纬度字段的最小刻度，四舍五入误差 ±LSB/2；
     解码端的输出舍入（如 AIS 解码把经纬度舍入到 1e-5 度）单独计入。
  2. **传感器噪声** —— 位置噪声标准差 σ，逐轴取 k·σ 为上限（默认 k=3）。
  3. **观测坐标量化** —— 观测侧主动施加的量化步长，误差 ±步长/2。

## 界限的性质

高斯噪声**无界**，因此 k·σ 不是严格上界，只是置信区间上界。评测中会
如实报告超限点数与超出比例——超限比例应与置信水平一致（k=3 时约
0.27%/轴）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .config import AlignmentConfig, SourceConfig
from geodesy import METERS_PER_DEG_LAT_APPROX

# 1 海里 = 1852 m
NM_TO_M = 1852.0

_DISTANCE_UNIT_TO_M = {
    "nautical_mile": NM_TO_M,
    "kilometre": 1000.0,
    "statute_mile": 1609.344,
    "metre": 1.0,
}


@dataclass
class ErrorBound:
    """单条记录的位置误差上界。"""

    radial_m: float = 0.0
    tangential_m: float = 0.0
    planar_m: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "radial_m": self.radial_m,
            "tangential_m": self.tangential_m,
            "planar_m": self.planar_m,
            "components": dict(self.components),
            "notes": list(self.notes),
        }


class BoundCalculator:
    """按数据源与每条记录的实际距离计算位置误差上界。"""

    def __init__(self, config: AlignmentConfig):
        self.config = config

    # -- 编码量化 -----------------------------------------------------------

    def _encoding_radial_tangential(
        self, source: SourceConfig, range_m: Optional[float]
    ) -> tuple:
        """报文编码量化在径向/切向的分量（不含噪声）。"""
        encoding = source.encoding
        radial = 0.0
        tangential = 0.0
        components: Dict[str, float] = {}

        if source.source_type in ("radar", "radar_cutdata"):
            # 距离量化：沿径向
            if encoding.distance_lsb is not None:
                factor = _DISTANCE_UNIT_TO_M.get(
                    encoding.distance_unit or "", 1.0
                )
                lsb_m = encoding.distance_lsb * factor
                radial += lsb_m / 2.0
                components["encoding_distance_m"] = lsb_m / 2.0
            # 方位量化：沿切向，误差随距离线性放大
            if encoding.bearing_lsb_deg is not None and range_m is not None:
                value = math.radians(encoding.bearing_lsb_deg) * range_m / 2.0
                tangential += value
                components["encoding_bearing_m"] = value
        else:
            # 经纬度类观测：各向同性，整体计入径向
            iso = self._isotropic_encoding_m(source)
            if iso is not None:
                radial += iso
                components["encoding_position_m"] = iso

        return radial, tangential, components

    def _isotropic_encoding_m(self, source: SourceConfig) -> Optional[float]:
        """经纬度类观测的各向同性编码量化上界（米）。

        合成两部分：
          - 线上量化：±LSB/2 度；
          - 解码端输出舍入：±LSB/2 度。
        """
        encoding = source.encoding
        lat_lsb = encoding.position_lsb_deg or 0.0
        lon_lsb = encoding.position_lsb_deg or 0.0
        if encoding.position_lsb_deg is None:
            return None

        lat_m = lat_lsb / 2.0 * METERS_PER_DEG_LAT_APPROX
        lon_m = lon_lsb / 2.0 * METERS_PER_DEG_LAT_APPROX * math.cos(
            math.radians(self.config.site_frame.origin_latitude_deg)
        )
        return math.hypot(lat_m, lon_m)

    def _decoder_rounding_m(self, source: SourceConfig) -> Optional[float]:
        """解码端输出舍入引入的上界（米）。"""
        lsb = source.encoding.decoded_position_lsb_deg
        if lsb is None:
            return None
        lat_m = lsb / 2.0 * METERS_PER_DEG_LAT_APPROX
        lon_m = lsb / 2.0 * METERS_PER_DEG_LAT_APPROX * math.cos(
            math.radians(self.config.site_frame.origin_latitude_deg)
        )
        return math.hypot(lat_m, lon_m)

    # -- 噪声与观测量化 -----------------------------------------------------

    def _noise_axis_limit_m(self, source: SourceConfig) -> float:
        """噪声的逐轴上限（k·σ）。"""
        model = source.error_model
        if model.position_noise_std_m <= 0:
            return 0.0
        return model.position_noise_std_m * model.noise_sigma_multiplier

    def _observation_quantization_m(self, source: SourceConfig) -> Optional[float]:
        """观测侧主动量化的各向同性上界（米）。"""
        step = source.error_model.quantization_lsb_deg
        if not step:
            return None
        lat_m = step / 2.0 * METERS_PER_DEG_LAT_APPROX
        lon_m = step / 2.0 * METERS_PER_DEG_LAT_APPROX * math.cos(
            math.radians(self.config.site_frame.origin_latitude_deg)
        )
        return math.hypot(lat_m, lon_m)

    # -- 主入口 -------------------------------------------------------------

    def bound_for_record(self, record: Dict[str, Any]) -> Optional[ErrorBound]:
        """计算单条记录的位置误差上界。"""
        source = self.config.source_config(str(record.get("source_id")))
        if source is None:
            return None

        position = record.get("position") or {}
        range_m = position.get("range_m")
        if range_m is not None:
            range_m = float(range_m)

        radial, tangential, components = self._encoding_radial_tangential(
            source, range_m
        )

        decoder = self._decoder_rounding_m(source)
        if decoder is not None:
            radial += decoder
            components["decoder_rounding_m"] = decoder

        obs_quant = self._observation_quantization_m(source)
        if obs_quant is not None:
            radial += obs_quant
            components["observation_quantization_m"] = obs_quant

        # 噪声逐轴叠加（不按 √2 分摊，理由见模块文档）
        noise = self._noise_axis_limit_m(source)
        if noise:
            radial += noise
            tangential += noise
            components["sensor_noise_axis_m"] = noise

        planar = math.hypot(radial, tangential)
        return ErrorBound(
            radial_m=radial,
            tangential_m=tangential,
            planar_m=planar,
            components=components,
        )
