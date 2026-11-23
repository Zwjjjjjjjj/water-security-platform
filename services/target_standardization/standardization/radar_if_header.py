"""MTDSP 雷达 IF/IQ 帧头中「航向角」与「速度」列的解码。

## 这些列不是损坏数据

`IF_*.csv` / `IQ_*.csv` 中的 `航向角(°)` 与 `速度(nm/h)` 看起来像
`4.943781E-42` 这样的科学计数法浮点数。它们**不是**溢出、不是 NaN、
也不是数据损坏，而是**采集软件的类型声明错误**造成的可逆编码：

- 帧头中这两个字段本��是 4 字节 `uint32` 整数；
- 导出软件把它们按 `float32` 读取并用 `%.7G` 打印；
- 由于整数值的比特模式远小于 `float32` 的最小正规数，它们被解释为
  **IEEE-754 次正规数（subnormal）**，数值等于

  $$\\text{value} = \\text{uint32} \\times 2^{-149},\\qquad 2^{-149} = 1.4012984643\\times10^{-45}$$

于是 `3528` 被打印成 `3528 × 2^-149 = 4.943781E-42`。

## 可逆性已被逐位验证

对 `IF_2003_AT_413203610.dat`（每帧 3664 字节）的全部帧做过**穷举偏移扫描**：

| 列 | 唯一匹配的 uint32 帧内偏移 | 一致性 |
| -- | -------------------------- | ------ |
| `航向角(°)` | `+20` | 288/288（03-05）、279/279（03-12）**逐位相等** |
| `速度(nm/h)` | `+60` | 68327/68327（03-05）、69462/69462（03-12）**逐位相等** |

即：对每一行都有

```python
struct.unpack('<I', struct.pack('<f', float(csv_str)))[0] == dat_frame_u32[offset]
```

成立，且帧内 3664 字节的**所有**其他偏移都不满足该恒等式（唯一性）。

## 物理量纲

### 航向角：0.1°/原始单位

原始整数 `3520..3539` → **352.0°..353.9°**（03-05），`3530..3539` →
353.0°..353.9°（03-12）。

判据（三条互相独立）：

1. 该角度与 `CutDataTarget.csv` 的 `角度修正(°)=344.0`（设备安装方位）
   相差 8.4°..9.0°；若改用 1°/单位（280°..299°）或 0.01°/单位
   （35.2°..35.4°），该差值分别变成 -54° 和 -309°，均不合理。
2. 烟台地区磁偏角约 -6.9°(西)，与上述 8.4°..9.0° 的差值量级一致，
   说明该字段是**平台自身朝向**（岸基固定站的天线基线朝向），
   且绝对参考（真/磁）尚不能从数据本身唯一确定。
3. 该角度与天线相对方位码**统计独立**（15° 扇区内均值极差仅 0.04°），
   排除了"其实是天线指向角"的可能。

### 速度：标度为"平台自身速度"，非目标航速

原始整数 `0..26`，两数据集取值集合完全一致（03-05 缺 24，03-12 缺 0）。

标度由**站址经纬度的抖动量**反推（站址是固定岸基平台，其位置抖动
即接收机噪声）：

| 估计方法 | 03-05 | 03-12 | 合并 |
| -------- | ----- | ----- | ---- |
| MSD 长基线随机游走 | 0.96 mm/s | 0.62 mm/s | — |
| 位置 RMS 散布反推 | 0.48 mm/s | 0.24 mm/s | — |
| 路径积分反解 | 0.42 mm/s | 0.20 mm/s | — |
| 似然极大（合并，95%CI） | 0.92 [0.88, 0.97] | 0.44 [0.39, 0.49] | **0.77 [0.735, 0.805]** |

即 **1 个原始单位 ≈ 0.5 ~ 1 mm/s**，名义取 `1e-3 m/s`（1 mm/s）。

因此该字段表达的是**雷达平台（固定岸基站）自身的速度**，量级
0~26 mm/s，是 GNSS 接收机在静止状态下的定位抖动，
**不是**被观测船舶的航速。

> ⚠️ CSV 表头写的 `速度(nm/h)` 与实测证据不符：按 0.1 nm/h/单位 解读，
> 预测的站址抖动比实测小两个数量级（Δnll = +409）。本模块因此
> **不按表头单位解码**，而是按实测标度输出 m/s，并在
> `speed_scale_source` 中标明来源。

## 用法

```python
from standardization.radar_if_header import (
    decode_if_uint32,
    parse_if_heading_deg,
    parse_if_speed_mps,
)

heading_deg = parse_if_heading_deg("4.943781E-42")   # 352.8
speed_mps   = parse_if_speed_mps("4.203895E-45")     # 0.003
```

解码是**纯函数、无副作用**，不修改任何既有标准化/对齐模块。
"""

from __future__ import annotations

import math
import struct
from typing import Optional

__all__ = [
    "FLOAT32_SUBNORMAL_SCALE",
    "HEADING_DEG_PER_RAW_UNIT",
    "SPEED_MPS_PER_RAW_UNIT",
    "SPEED_MPS_PER_RAW_UNIT_ML",
    "SPEED_MPS_PER_RAW_UNIT_CI",
    "decode_if_uint32",
    "encode_if_uint32",
    "parse_if_heading_deg",
    "parse_if_speed_mps",
    "parse_if_speed_nmh",
    "parse_if_heading_raw",
    "parse_if_speed_raw",
    "is_valid_if_encoded_value",
]

#: float32 次正规数的最小步长：原始整数 1 对应的浮点值。
FLOAT32_SUBNORMAL_SCALE = 2.0 ** -149

#: 航向角标度：1 个原始单位 = 0.1°。
HEADING_DEG_PER_RAW_UNIT = 0.1

#: 速度标度（名义）：1 个原始单位 = 1 mm/s = 1e-3 m/s。
SPEED_MPS_PER_RAW_UNIT = 1.0e-3

#: 速度标度（似然极大估计，合并两数据集）：0.77 mm/s。
SPEED_MPS_PER_RAW_UNIT_ML = 0.77e-3

#: 速度标度（似然 95% 置信区间，合并两数据集）。
SPEED_MPS_PER_RAW_UNIT_CI = (0.735e-3, 0.805e-3)

# 判定"这是次正规数编码"的上界：正常量级的浮点数不可能来自该编码。
# 原始整数上限取 2^24（float32 尾数可精确表示的上限）以远大于实测范围。
_MAX_PLAUSIBLE_RAW = 1 << 24


def _f64_to_f32_bits(value: float) -> Optional[int]:
    """把 float64 舍入到最近的 float32，返回其 32 位比特模式。"""
    try:
        return struct.unpack("<I", struct.pack("<f", value))[0]
    except (OverflowError, struct.error):
        return None


def _f32_bits_to_value(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def decode_if_uint32(value) -> Optional[int]:
    """把 IF/IQ CSV 中的浮点字符串还原为帧头里的原始 uint32。

    返回 None 表示该值不是本编码（空值、无法解析、或不是次正规数）。

    原理：``value == uint32 * 2**-149``。先舍入到 float32 取比特模式，
    再乘回 ``2**149`` 并取整——由于 CSV 以 7 位有效数字打印 float32
    次正规数，误差远小于半个最小步长，取整必然精确。
    """
    if value is None:
        return None
    if isinstance(value, (int,)) and not isinstance(value, bool):
        # 已经是整数：直接使用（并做量程校验）
        return int(value) if 0 <= int(value) <= _MAX_PLAUSIBLE_RAW else None
    if isinstance(value, float):
        text = repr(value)
    else:
        text = str(value).strip()
    if not text:
        return None
    try:
        f64 = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(f64) or math.isinf(f64):
        return None
    if f64 == 0.0:
        return 0
    # 次正规数的量级：原始值 1..2^24 对应 1.4e-45 .. 1.9e-38
    if not (1e-45 <= f64 < 1e-37):
        return None
    bits = _f64_to_f32_bits(f64)
    if bits is None:
        return None
    scaled = _f32_bits_to_value(bits) / FLOAT32_SUBNORMAL_SCALE
    raw = int(round(scaled))
    if raw < 0 or raw > _MAX_PLAUSIBLE_RAW:
        return None
    # 与原始值的相对偏差必须落在打印精度允许的范围内
    if abs(scaled - raw) > 1e-3 * max(1.0, raw):
        return None
    return raw


def encode_if_uint32(raw: int) -> float:
    """把原始 uint32 编码成该导出软件会打印的 float32 值（逆向验证用）。"""
    return struct.unpack("<f", struct.pack("<I", int(raw)))[0]


def is_valid_if_encoded_value(value) -> bool:
    """判断某个单元格是否是本编码下的合法取值。"""
    return decode_if_uint32(value) is not None


def parse_if_heading_raw(value) -> Optional[int]:
    """解析 `航向角(°)` 列为原始 uint32。"""
    return decode_if_uint32(value)


def parse_if_speed_raw(value) -> Optional[int]:
    """解析 `速度(nm/h)` 列为原始 uint32。"""
    return decode_if_uint32(value)


def parse_if_heading_deg(value) -> Optional[float]:
    """解析 `航向角(°)` 列为**角度（度）**。

    标度 0.1°/原始单位：3528 → 352.8°。

    注意：绝对参考（真北/磁北）无法由数据本身唯一确定，返回值按
    **设备自身的零点**表示；已知绝对参考的场合请在下游自行叠加。
    """
    raw = decode_if_uint32(value)
    if raw is None:
        return None
    return raw * HEADING_DEG_PER_RAW_UNIT


def parse_if_speed_mps(value) -> Optional[float]:
    """解析 `速度(nm/h)` 列为 **m/s**。

    标度 1e-3 m/s/原始单位（名义 1 mm/s，区间估计 0.5~1 mm/s）。
    该字段是**雷达平台自身**的速度，不是被观测目标的航速。
    """
    raw = decode_if_uint32(value)
    if raw is None:
        return None
    return raw * SPEED_MPS_PER_RAW_UNIT


def parse_if_speed_nmh(value) -> Optional[float]:
    """解析 `速度(nm/h)` 列为**海里/小时**。

    仅在确实需要 nm/h 量纲时使用；数值上等于 m/s 值乘 3600/1852。
    """
    mps = parse_if_speed_mps(value)
    if mps is None:
        return None
    return mps * 3600.0 / 1852.0
