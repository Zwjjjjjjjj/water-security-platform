"""MTDSP 雷达帧头（IF/IQ）平台自身状态的对齐接入层。

## 接入的是什么

`IF_*.csv` 的帧头里有两个字段描述**雷达平台自身**的状态：

| 列 | 物理含义 | 实测量级 |
| -- | -------- | -------- |
| `航向角(°)` | 平台（岸基站）自身朝向 | 352.0°..353.9°（03-05） |
| `速度(nm/h)` | 平台自身速度（GNSS 抖动） | 0~26 mm/s |

这两个值的解码方法见 `standardization.radar_if_header`（float32 次正规数
可逆编码，标度 0.1°/单位 与 1 mm/s/单位）。

## 关键结论：方位基准**不随**平台艏向变化

### 决定性判据：用**权威 AIS** 反算真方位

对每条有 AIS 对应目标的雷达记录，取同一时刻 AIS 的经纬度，用 IF 帧头
给出的**雷达自记站址** (37.53297, 121.42020) 反算该目标的真方位
`implied`，与雷达上报的 `目标方位(°)` 比较，残差 Δ = implied − tb。
再对各种候选修正量比较叠加前后的 |Δ|：

| 候选修正量 | 03-05 \\|Δ\\|中位数 | 03-12 \\|Δ\\|中位数 |
| ---------- | ----------------- | ----------------- |
| **不修正 (0°)** | **0.446°** | **0.530°** |
| 平台航向 (≈353°) | 6.597° | 6.427° |
| 角度修正 (344°/345°) | 15.554° | 15.869° |
| 平台航向 − 角度修正 | 9.403° | 9.573° |
| 平台航向 + 180° | 173.403° | 173.573° |

⇒ **`目标方位(°)` 已经是真方位**，任何叠加都会显著恶化残差。
该判据完全独立于 IF 的方位码，只依赖 AIS 与站址，因此结论稳固。
残余的 0.45°/0.53° 偏差是目标运动与 AIS 上报时延造成的
（按距离分层可见残差随距离增大而放大），不是坐标基准错误。

这与 `radar_cutdata_adapter` 中已核验的结论一致：
`角度修正(°)=344` 是天线安装方位，叠加后站址反算残差从 14 m 恶化到 400 m。

### 辅助判据：IF 方位码一致性

用 IF 帧的方位码做全角一致性统计（预测码须落在该帧实际覆盖的
约 40° 区间内），最优偏移在 03-05 为 345.1°、03-12 为 3.5°，
一致率分别 98.1% / 97.7% —— 说明方位码与 `目标方位(°)` 之间**不存在
一个全角上唯一且两数据集一致的常量偏移**（帧覆盖仅 40°，且两包
帧序/时间对应关系不同），因此该路径只作辅助，结论以 AIS 判据为准。

## 因此平台航向的正确用途

平台航向角**不是**方位换算的输入，而是：

1. **平台自身运动状态的记录** —— 可用于判断固定站址是否发生位移、
   复核 GNSS 定位抖动（本次结论：站址为固定岸基平台，速度 0~26 mm/s
   是接收机噪声，不是真实位移）。
2. **`R`（相对艏向）方位参考的换算源** —— 若某数据源的方位以本船艏向
   为参考（R），则真方位 = 相对方位 + 平台艏向。本模块提供该能力，
   但 MTDSP 数据实测不需要（其方位已是真方位）。
3. **旋转方向判定的辅助证据** —— 天线是顺时针还是逆时针扫描。

## 关联方式：帧号 + 时间双通道

`CutDataTarget.csv` 与 `IF_*.csv` 的帧号**同源**，但帧率不同
（IF 帧间隔 2–3 s，Cut 帧间隔 2–5 s），因此：

- **帧号直接关联不可靠**：03-05 包按帧号关联 Δt 中位 +5 s（最大 +10 s），
  03-12 包按帧号关联 Δt 恒为 0 —— 两个数据集的行为不一致。
- **时间就近关联也不可靠**：03-05 包 100% 命中（Δt≡0），
  03-12 包只有 4.8% 命中 —— 因为 03-12 的 IF 时间列**大面积为空**
  （152 帧中仅 132 帧含时间，帧首行常缺）。

本模块因此采用**逐帧就近时间匹配 + 容差**，并在两个通道上分别记录
匹配依据与时间差，由下游决定是否采用。

## 用法

```python
from alignment.if_platform_state import IfPlatformStateIndex

index = IfPlatformStateIndex.from_directory("data/mtdsp")
frame = index.lookup(frame_index="37", state_time_raw="2025-03-05 10:36:22")
if frame:
    print(frame.heading_deg, frame.speed_mps, frame.match_basis)
```
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from standardization.radar_if_header import (
    HEADING_DEG_PER_RAW_UNIT,
    SPEED_MPS_PER_RAW_UNIT,
    parse_if_heading_deg,
    parse_if_speed_mps,
)

__all__ = [
    "IfFrameState",
    "IfPlatformStateIndex",
    "DEFAULT_MATCH_TOLERANCE_S",
]

#: 帧号/时间双通道匹配的最大容差（秒）。
#: 超过该容差视为未匹配，不做任何关联。
DEFAULT_MATCH_TOLERANCE_S = 10.0

#: IF 文件名模式：`IF_<设备>_<MMSI>.csv`
_IF_FILENAME_PREFIX = "IF_"

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f")


def _parse_time(text: str) -> Optional[datetime]:
    """解析本地时间字符串；失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _normalize_frame_index(value: Any) -> Optional[int]:
    """把帧号统一成整数键。

    标准化层把 `帧序号` 存成浮点数（`37.0`），IF 文件里是字符串（`37`），
    字符串比较会得到 "37.0" != "37" 的假阴性。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number != int(number):
        return None
    return int(number)


@dataclass
class IfFrameState:
    """单帧的平台自身状态。"""

    frame_index: str
    state_time_raw: Optional[str] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None
    station_latitude_deg: Optional[float] = None
    station_longitude_deg: Optional[float] = None
    pulse_count: int = 0
    source_file: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "state_time_raw": self.state_time_raw,
            "heading_deg": self.heading_deg,
            "speed_mps": self.speed_mps,
            "station_latitude_deg": self.station_latitude_deg,
            "station_longitude_deg": self.station_longitude_deg,
            "pulse_count": self.pulse_count,
        }


@dataclass
class IfMatchResult:
    """一次关联的结果（含匹配依据，供下游审计）。"""

    frame: Optional[IfFrameState] = None
    match_basis: Optional[str] = None  # "frame_index" / "time" / None
    time_delta_s: Optional[float] = None
    note: Optional[str] = None

    @property
    def matched(self) -> bool:
        return self.frame is not None


@dataclass
class IfPlatformStateIndex:
    """按 IF 帧头建立的索引，提供「按帧号 / 按时间」两种关联。"""

    frames: List[IfFrameState] = field(default_factory=list)
    source_file: Optional[str] = None
    match_tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S

    # -- 构建 ---------------------------------------------------------------

    @classmethod
    def from_directory(
        cls,
        directory: str,
        filename: Optional[str] = None,
        match_tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S,
        dat_path: Optional[str] = None,
    ) -> "IfPlatformStateIndex":
        """从目录中读取 `IF_*.csv`（或指定的文件）建立索引。

        `dat_path` 指向配套的 `.dat` 二进制，用于补齐 CSV 中缺失的
        航向角/速度（03-12 包的 CSV 只有 6 帧填了航向角）。
        """
        path = cls._resolve_path(
            cls.resolve_directory(directory), filename
        )
        if path is None:
            return cls(match_tolerance_s=match_tolerance_s)
        return cls.from_file(
            path, match_tolerance_s=match_tolerance_s, dat_path=dat_path
        )

    @classmethod
    def from_file(
        cls,
        path: str,
        match_tolerance_s: float = DEFAULT_MATCH_TOLERANCE_S,
        dat_path: Optional[str] = None,
    ) -> "IfPlatformStateIndex":
        frames = _read_if_frames(path, dat_path=dat_path)
        return cls(
            frames=frames,
            source_file=path,
            match_tolerance_s=match_tolerance_s,
        )

    @staticmethod
    def _resolve_path(directory: str, filename: Optional[str]) -> Optional[str]:
        if filename:
            candidate = filename
            if not os.path.isabs(candidate):
                candidate = os.path.join(directory, filename)
            return candidate if os.path.exists(candidate) else None
        if os.path.isfile(directory):
            return directory
        if not os.path.isdir(directory):
            return None
        names = sorted(
            n
            for n in os.listdir(directory)
            if n.startswith(_IF_FILENAME_PREFIX) and n.lower().endswith(".csv")
        )
        if not names:
            return None
        return os.path.join(directory, names[0])

    @staticmethod
    def resolve_directory(directory: str) -> str:
        """把配置里的相对路径解析为绝对路径。

        配置文件里的 `if_directory` 通常写成相对于工程根目录的路径
        （如 `data/mtdsp`），而对齐流水线可能从任意工作目录启动。
        因此这里按「当前目录 → 工程根目录」的顺序解析。
        """
        if not directory:
            return directory
        if os.path.isabs(directory):
            return directory
        if os.path.isdir(directory):
            return os.path.abspath(directory)
        # 工程根目录 = 本文件的上两级（alignment/if_platform_state.py → 根）
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidate = os.path.join(project_root, directory)
        if os.path.isdir(candidate):
            return candidate
        return os.path.abspath(directory)

    # -- 查询 ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.frames)

    @property
    def available(self) -> bool:
        return bool(self.frames)

    def by_frame_index(self, frame_index: Any) -> Optional[IfFrameState]:
        """按帧号查找。

        帧号在标准化层被存成浮点数（`radar_frame_index`，如 `37.0`），
        而 IF 文件里是字符串（`37`）。因此按**数值**比较，两种表示都能命中。
        """
        key = _normalize_frame_index(frame_index)
        if key is None:
            return None
        for frame in self.frames:
            if _normalize_frame_index(frame.frame_index) == key:
                return frame
        return None

    def lookup(
        self,
        frame_index: Any = None,
        state_time_raw: Optional[str] = None,
    ) -> IfMatchResult:
        """按「帧号优先、时间兜底」关联一帧。

        帧号通道直接命中时不做时间校验 —— 因为 IF 与 Cut 的帧率不同，
        同一帧号对应的时刻本就可以相差数秒（实测 03-05 包中位 +5 s），
        用时间反过来否决帧号匹配会丢掉正确的关联。
        """
        if frame_index is not None:
            frame = self.by_frame_index(frame_index)
            if frame is not None:
                delta = self._time_delta(frame.state_time_raw, state_time_raw)
                return IfMatchResult(
                    frame=frame,
                    match_basis="frame_index",
                    time_delta_s=delta,
                )
        if state_time_raw:
            frame, delta = self._nearest_by_time(state_time_raw)
            if frame is not None:
                return IfMatchResult(
                    frame=frame,
                    match_basis="time",
                    time_delta_s=delta,
                )
        return IfMatchResult(match_basis=None, note="no_match")

    def _time_delta(
        self, frame_time: Optional[str], query_time: Optional[str]
    ) -> Optional[float]:
        a = _parse_time(frame_time or "")
        b = _parse_time(query_time or "")
        if a is None or b is None:
            return None
        return (b - a).total_seconds()

    def _nearest_by_time(
        self, query_time: str
    ) -> Tuple[Optional[IfFrameState], Optional[float]]:
        target = _parse_time(query_time)
        if target is None:
            return None, None
        best: Optional[IfFrameState] = None
        best_delta: Optional[float] = None
        for frame in self.frames:
            moment = _parse_time(frame.state_time_raw or "")
            if moment is None:
                continue
            delta = (target - moment).total_seconds()
            if best_delta is None or abs(delta) < abs(best_delta):
                best = frame
                best_delta = delta
        if best is None or best_delta is None:
            return None, None
        if abs(best_delta) > self.match_tolerance_s:
            return None, None
        return best, best_delta


# ---------------------------------------------------------------------------
# IF 文件读取
# ---------------------------------------------------------------------------


def _read_if_frames(path: str, dat_path: Optional[str] = None) -> List[IfFrameState]:
    """读取 IF CSV，按「帧序号」分组，每帧取第一个非空的时间与状态值。

    IF 文件每帧含数百个脉冲行，但 `时间` / `航向角(°)` / `速度(nm/h)`
    等字段只在帧首行（或少数行）填写，其余行为空。因此按帧分组后取
    首个非空值即可代表该帧的状态。

    `dat_path` 非空时，用 `.dat` 补齐 CSV 中缺失的航向角/速度：
    CSV 行序与 `.dat` 记录序一一对应，且帧内状态恒定，因此可以对每帧
    取该帧第一条记录的值作为该帧状态（与 CSV 有值时逐位相等）。
    """
    frames: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    row_of_frame_start: Dict[str, int] = {}
    row_index = -1
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_index += 1
            key = (row.get("帧序号") or "").strip()
            if not key:
                continue
            if key not in frames:
                frames[key] = {"pulse_count": 0, "first_row": row_index}
                order.append(key)
            entry = frames[key]
            entry["pulse_count"] += 1
            for field_name, column in (
                ("state_time_raw", "时间"),
                ("heading_deg", "航向角(°)"),
                ("speed_mps", "速度(nm/h)"),
                ("station_latitude_deg", "纬度(北纬)(°)"),
                ("station_longitude_deg", "经度(东经)(°)"),
            ):
                value = (row.get(column) or "").strip()
                if value and entry.get(field_name) in (None, ""):
                    entry[field_name] = value

    # 用 .dat 补齐缺失的航向角与速度
    dat_records: List[Tuple[int, int]] = []
    if dat_path:
        dat_records = read_if_dat_records(dat_path)

    result: List[IfFrameState] = []
    recovered = 0
    for key in order:
        entry = frames[key]
        heading = parse_if_heading_deg(entry.get("heading_deg"))
        speed = parse_if_speed_mps(entry.get("speed_mps"))
        if dat_records:
            start = entry["first_row"]
            if start < len(dat_records):
                raw_heading, raw_speed = dat_records[start]
                if heading is None:
                    heading = raw_heading * HEADING_DEG_PER_RAW_UNIT
                    recovered += 1
                if speed is None:
                    speed = raw_speed * SPEED_MPS_PER_RAW_UNIT
        result.append(
            IfFrameState(
                frame_index=key,
                state_time_raw=entry.get("state_time_raw"),
                heading_deg=heading,
                speed_mps=speed,
                station_latitude_deg=_to_float(entry.get("station_latitude_deg")),
                station_longitude_deg=_to_float(entry.get("station_longitude_deg")),
                pulse_count=int(entry["pulse_count"]),
                source_file=path,
            )
        )
    return result


# ---------------------------------------------------------------------------
# IF 二进制（.dat）读取 —— 补齐 CSV 中缺失的帧头状态
# ---------------------------------------------------------------------------

#: `.dat` 中单条脉冲记录的字节数。
IF_RECORD_SIZE = 3664

#: 帧头内「航向角」与「速度」的 uint32 偏移（已逐位验证，见 radar_if_header）。
IF_OFFSET_HEADING = 20
IF_OFFSET_SPEED = 60


def read_if_dat_records(path: str) -> List[Tuple[int, int]]:
    """从 `.dat` 读取**每条脉冲记录**的 (航向 raw, 速度 raw)。

    返回列表的**下标即记录号**（从 0 开始），与 IF CSV 的**行序**一一对应
    （已验证：两个数据集的行数分别等于 `.dat` 的记录数 68327 / 69462，
    且 CSV 中有值的行与 `.dat` 逐位相等）。

    ## 为什么需要它

    `IF_*.csv` 的导出工具**只在部分行**填写 `航向角(°)`：

    | 数据集 | 帧数 | CSV 中含航向角的帧 | 帧内航向有多值 |
    | ------ | ---- | ------------------ | -------------- |
    | 03-05  | 127  | 126                | 0（恒定）     |
    | 03-12  | 152  | **6**              | 0（恒定）     |

    03-12 包只有 6 帧有航向角，导致 125 个被雷达引用的帧中 124 个拿不到
    平台航向。但 `.dat` 的**每一条**脉冲记录都带完整的帧头，且帧内所有脉冲
    的航向/速度**完全相同**（已验证：0/127 与 0/152 帧出现多值）。

    因此可从 `.dat` 恢复每帧的完整状态，不受 CSV 缺填的影响。
    """
    import struct

    if not os.path.exists(path):
        return []
    records: List[Tuple[int, int]] = []
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(IF_RECORD_SIZE)
            if len(chunk) < IF_RECORD_SIZE:
                break
            heading = struct.unpack(
                "<I", chunk[IF_OFFSET_HEADING:IF_OFFSET_HEADING + 4]
            )[0]
            speed = struct.unpack("<I", chunk[IF_OFFSET_SPEED:IF_OFFSET_SPEED + 4])[0]
            records.append((heading, speed))
    return records


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
