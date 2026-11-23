"""时间整理与异步匹配。

流程：
  1. **已知时钟偏移修正**：按来源配置把各源的状态时间修正到公共时间基准；
  2. **按数据源、按航迹独立整理**：每条航迹单独成序列，避免跨目标插值；
  3. **插值到公共时间栅格**：记录插值前后的两条源记录及其间隔；超过
     `max_interpolation_gap_s` 则该栅格点标记为不可用；
  4. **精度限制如实保留**：各源的时间精度（雷达 TTM 0.01 s、AIS 整秒）
     记录在输出中，插值结果不声称高于源精度。

## 插值方式

两种方式都实现，由配置 `interpolation_mode` 选择：

  - `enu_first`（**首轮基线**）：先把源记录转到现场 ENU，再对 ENU 坐标
    （east/north）做线性插值。实现简单、与真值同坐标系，但对转弯段
    会把弧线拉成弦（弦长 < 弧长），转弯越急偏差越大。
  - `observation_domain`：在各自的观测域内插值（雷达插距离与方位，
    AIS 插经纬度），插完再各自转到 ENU。转弯段更贴合，但两路进入
    ENU 的路径不同，逐点可比性弱于 `enu_first`。

两种方式都**不做外推**：超出航迹首末点范围标记为不可用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import (
    INTERP_ENU_FIRST,
    AlignmentConfig,
    SourceConfig,
)
from .spatial import STATUS_UNUSABLE_POSITION


def parse_iso(value: str) -> Optional[float]:
    """ISO 时间 → Unix 秒。"""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_local_naive(value: str) -> Optional[float]:
    """本地无时区时间字符串 → Unix 秒（按 UTC 解读，仅用作相对基准）。

    用于**相对时间基准**模式：时区未确认时，绝对 UTC 不可知，但把本地
    时刻按同一约定解读后，源间的时间差是确定的。返回值**不代表**真实 UTC
    时刻，只用于同场景内的相对时间计算。
    """
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            continue
    return parse_iso(value)


def format_iso(epoch: float) -> str:
    """Unix 秒 → ISO 时间。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def format_relative(epoch: float, reference_local_datetime: Optional[str]) -> str:
    """相对秒 → 可读的本地时间标签。

    相对时间基准模式下，epoch 是"相对参考时刻的秒数"，直接用
    `format_iso` 会得到 1970 年的日期。这里把参考时刻加上相对秒数，
    按本地时间格式输出，并显式带上 `local` 标记，避免被误读为 UTC。
    """
    if not reference_local_datetime:
        return f"relative+{epoch:.3f}s"
    base = parse_local_naive(reference_local_datetime)
    if base is None:
        return f"relative+{epoch:.3f}s"
    moment = datetime.fromtimestamp(base + epoch, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " (local)"


def seconds_of_day(epoch: float) -> float:
    """Unix 秒 → UTC 日内秒。"""
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return (
        moment.hour * 3600
        + moment.minute * 60
        + moment.second
        + moment.microsecond / 1e6
    )


@dataclass
class TrackPoint:
    """整理后的一条源观测点。"""

    source_id: str
    source_type: str
    local_track_id: str
    state_epoch: float
    state_time_utc: str
    receive_epoch: Optional[float]
    position: Dict[str, Any]
    speed_mps: Optional[float]
    course_rad: Optional[float]
    source_row: Optional[int]
    time_resolution_s: float
    raw_record: Optional[str] = None
    # 源记录转到现场 ENU 后的位置（enu_first 插值用）
    east_m: Optional[float] = None
    north_m: Optional[float] = None
    up_m: Optional[float] = None
    enu_status: Optional[str] = None
    enu_failure_reason: Optional[str] = None


@dataclass
class GridPoint:
    """公共时间栅格上的一个点。"""

    time_epoch: float
    time_utc: str
    source_id: str
    source_type: str
    local_track_id: str
    position: Dict[str, Any]
    speed_mps: Optional[float]
    course_rad: Optional[float]
    interpolation: Dict[str, Any] = field(default_factory=dict)
    # enu_first 插值直接给出 ENU；observation_domain 需要后续转换
    east_m: Optional[float] = None
    north_m: Optional[float] = None
    up_m: Optional[float] = None


class TemporalAligner:
    """时间整理与异步匹配。"""

    def __init__(self, config: AlignmentConfig, spatial_aligner=None):
        self.config = config
        # 空间对齐器：enu_first 插值需要先把源记录转到现场 ENU
        self.spatial_aligner = spatial_aligner

    # -- 步骤 1：时钟偏移修正 ------------------------------------------------

    def corrected_state_epoch(
        self, record: Dict[str, Any], source_config: Optional[SourceConfig]
    ) -> Optional[float]:
        """取记录的观测状态时间并修正已知时钟偏移。

        修正顺序：
          1. 减去已知时钟偏移（读数比基准快多少就减多少）；
          2. 减去已知的量化偏差（报文时间字段量化时丢掉的亚秒部分）。

        偏移方向：配置的 `known_clock_offset_s` 表示该源读数比公共基准快多少，
        因此修正后的公共时刻 = 原始读数 − 偏移。
        """
        if source_config is not None and not source_config.time.use_state_time_utc:
            return None
        # 相对时间基准模式：时区未确认时用本地时刻的相对秒数。
        # 前提是同场景各源共享同一设备时钟（见 SourceTimeConfig 注释）。
        if source_config is not None and source_config.time.use_relative_time_base:
            reference = source_config.time.reference_local_datetime
            if reference:
                base = parse_local_naive(reference)
                raw_local = parse_local_naive(str(record.get("state_time_raw") or ""))
                if base is not None and raw_local is not None:
                    epoch = raw_local - base
                    epoch -= source_config.time.known_clock_offset_s
                    epoch -= source_config.time.known_quantum_bias_s
                    return epoch
        raw = record.get("state_time_utc")
        epoch = parse_iso(str(raw)) if raw else None
        if epoch is None and source_config is not None:
            if source_config.time.fallback_to_receive_time:
                raw = record.get("receive_time_utc")
                epoch = parse_iso(str(raw)) if raw else None
        if epoch is None:
            return None
        if source_config is not None:
            epoch -= source_config.time.known_clock_offset_s
            epoch -= source_config.time.known_quantum_bias_s
        return epoch

    def time_missing_requirements(
        self, record: Dict[str, Any], source_config: Optional[SourceConfig]
    ) -> list:
        """该记录缺失的时间对齐前提（无可用时间基准等）。

        返回非空时，该记录**不可用于时间对齐**——它不会被放进任何插值序列。
        """
        missing: list = []
        if source_config is not None and source_config.time.use_relative_time_base:
            # 相对时间基准模式：只要能解析出本地时刻与参考时刻就有可用基准。
            reference = source_config.time.reference_local_datetime
            raw_local = parse_local_naive(str(record.get("state_time_raw") or ""))
            if reference and raw_local is not None:
                return missing
            missing.append("no_relative_time_base")
            return missing
        if source_config is not None and not source_config.time.use_state_time_utc:
            # 配置明确不使用状态时间，但记录里也没有接收时间可回退
            raw = record.get("receive_time_utc")
            if not raw or parse_iso(str(raw)) is None:
                missing.append("no_reliable_time_base")
            return missing
        if not record.get("state_time_utc"):
            fallback = record.get("receive_time_utc")
            if not fallback or parse_iso(str(fallback)) is None:
                missing.append("no_reliable_time_base")
        return missing

    # -- 步骤 2：按源、按航迹分组 -------------------------------------------

    def build_tracks(
        self, records: Sequence[Dict[str, Any]]
    ) -> Dict[Tuple[str, str, str], List[TrackPoint]]:
        """按 (source_id, source_type, local_track_id) 分组并按时间排序。"""
        tracks: Dict[Tuple[str, str, str], List[TrackPoint]] = {}
        for record in records:
            source_id = str(record.get("source_id"))
            source_type = str(record.get("source_type"))
            local_track_id = str(record.get("local_track_id"))
            source_config = self.config.source_config(source_id)

            state_epoch = self.corrected_state_epoch(record, source_config)
            if state_epoch is None:
                # 无可用时间基准 → 记录不可用于时间对齐，原因写回记录
                missing = self.time_missing_requirements(record, source_config)
                record.setdefault("alignment_missing_requirements", [])
                for item in missing:
                    if item not in record["alignment_missing_requirements"]:
                        record["alignment_missing_requirements"].append(item)
                continue
            position = record.get("position") or {}
            if (
                position.get("latitude_deg") is None
                and position.get("range_m") is None
            ):
                continue

            # 标准化层已判定该记录的空间表达不可用（如光电 DLDete 超量程
            # 误检）：其位置数值本身就是误检产物，若留在插值序列里，
            # 会把极端坐标插值成栅格点，污染下游的跨源比对与航迹关联。
            # 因此这类记录不建轨迹。
            #
            # 注意与"空间转换失败"区分：后者是【缺少前提】（如磁差未配置），
            # 记录本身是真实观测，保留轨迹并由插值标记 source_enu_unavailable，
            # 让下游仍能看见"该航迹存在但暂不可用"。
            if record.get("alignment_status") == STATUS_UNUSABLE_POSITION:
                continue

            receive_epoch = None
            if record.get("receive_time_utc"):
                receive_epoch = parse_iso(str(record["receive_time_utc"]))

            point = TrackPoint(
                source_id=source_id,
                source_type=source_type,
                local_track_id=local_track_id,
                state_epoch=state_epoch,
                state_time_utc=str(record.get("state_time_utc") or ""),
                receive_epoch=receive_epoch,
                position=dict(position),
                speed_mps=record.get("speed_mps"),
                course_rad=record.get("course_rad"),
                source_row=record.get("source_row"),
                time_resolution_s=(
                    source_config.time.time_resolution_s if source_config else 1.0
                ),
                raw_record=record.get("raw_record"),
                east_m=record.get("east_m"),
                north_m=record.get("north_m"),
                up_m=record.get("up_m"),
                enu_status=record.get("alignment_status"),
                enu_failure_reason=record.get("alignment_failure_reason"),
            )
            key = (source_id, source_type, local_track_id)
            tracks.setdefault(key, []).append(point)

        for points in tracks.values():
            points.sort(key=lambda p: p.state_epoch)
        return tracks

    # -- 步骤 3：插值到公共栅格 ---------------------------------------------

    def grid_times(self, tracks: Dict[Any, List[TrackPoint]]) -> List[float]:
        """公共时间栅格。

        起点取"数据所在日期 + 配置的日内秒"。用数据自身日期而不是系统当前
        日期，避免数据集与运行环境日期不一致时栅格落到错误的日期上。

        相对时间基准模式下，epoch 是"相对参考时刻的秒数"（小数值），
        此时不能再套用"日内秒 → 当日 Unix 秒"的换算，直接从最早观测点
        起步、按步长递增即可。
        """
        if not tracks:
            return []
        earliest = min(points[0].state_epoch for points in tracks.values())
        latest = max(points[-1].state_epoch for points in tracks.values())
        if self._relative_mode():
            start_epoch = earliest
        else:
            start_epoch = _epoch_for_seconds_of_day(
                self.config.time_grid_start_seconds_of_day, earliest
            )
            if start_epoch < earliest:
                start_epoch = earliest
        times: List[float] = []
        cursor = start_epoch
        while cursor <= latest + 1e-9:
            times.append(cursor)
            cursor += self.config.output_time_step_s
        return times

    def _relative_mode(self) -> bool:
        """是否所有已配置数据源都使用相对时间基准。"""
        sources = self.config.sources
        if not sources:
            return False
        return all(
            source.time.use_relative_time_base
            or source.time.use_state_time_utc
            for source in sources
        ) and any(source.time.use_relative_time_base for source in sources)

    def _format_grid_time(self, grid_time: float) -> str:
        """栅格时刻的可读标签。相对模式下用本地时间 + (local) 标记。"""
        if self._relative_mode():
            reference = None
            for source in self.config.sources:
                if source.time.use_relative_time_base:
                    reference = source.time.reference_local_datetime
                    break
            return format_relative(grid_time, reference)
        return format_iso(grid_time)

    def grid_times_for_track(
        self, points: Sequence[TrackPoint]
    ) -> List[float]:
        """单条航迹自身的栅格时刻。

        公共栅格覆盖**全部**航迹的时间跨度时，会为每条短航迹产生大量
        "超出航迹范围"的不可用记录（例如 MTDSP：6 个 AIS MMSI 与 9 个雷达
        目标分散在 09:29–12:44，公共跨度 3 小时 15 分，而单条航迹只有
        几分钟）。按航迹自身范围生成栅格可把这些冗余记录降到 0，
        且不改变任何可用点的插值结果。
        """
        if not points:
            return []
        earliest = points[0].state_epoch
        latest = points[-1].state_epoch
        step = self.config.output_time_step_s
        if step <= 0:
            return []
        times: List[float] = []
        cursor = earliest
        while cursor <= latest + 1e-9:
            times.append(cursor)
            cursor += step
        return times

    def interpolate_track(
        self, points: Sequence[TrackPoint], grid: Sequence[float]
    ) -> List[GridPoint]:
        """在单条航迹内做区间线性插值。

        超出首末点范围 → 外推禁用（标记不可用）。
        源记录间隔超过 max_interpolation_gap_s → 该区间标记不可用。
        """
        results: List[GridPoint] = []
        if not points:
            return results

        times = [p.state_epoch for p in points]
        for grid_time in grid:
            if grid_time < times[0] - 1e-9 or grid_time > times[-1] + 1e-9:
                results.append(
                    self._unavailable(points[0], grid_time, "outside_track_range")
                )
                continue

            low, high = self._bracket(times, grid_time)
            before = points[low]
            after = points[high]

            if low == high:
                results.append(self._exact_point(before, grid_time))
                continue

            gap = times[high] - times[low]
            if gap > self.config.max_interpolation_gap_s:
                results.append(
                    self._unavailable(
                        before, grid_time, "gap_exceeds_limit", gap=gap, after=after
                    )
                )
                continue

            ratio = (grid_time - times[low]) / gap if gap > 0 else 0.0
            results.append(
                self._interpolated_point(before, after, ratio, grid_time, gap)
            )
        return results

    def _exact_point(self, point: TrackPoint, grid_time: float) -> GridPoint:
        return GridPoint(
            time_epoch=grid_time,
            time_utc=self._format_grid_time(grid_time),
            source_id=point.source_id,
            source_type=point.source_type,
            local_track_id=point.local_track_id,
            position=dict(point.position),
            speed_mps=point.speed_mps,
            course_rad=point.course_rad,
            east_m=point.east_m,
            north_m=point.north_m,
            up_m=point.up_m,
            interpolation={
                "method": "exact",
                "mode": self.config.interpolation_mode,
                "available": True,
                "gap_s": 0.0,
                "before_source_row": point.source_row,
                "after_source_row": point.source_row,
                "before_state_time_utc": point.state_time_utc,
                "after_state_time_utc": point.state_time_utc,
                "time_resolution_s": point.time_resolution_s,
            },
        )

    def _interpolated_point(
        self,
        before: TrackPoint,
        after: TrackPoint,
        ratio: float,
        grid_time: float,
        gap: float,
    ) -> GridPoint:
        mode = self.config.interpolation_mode
        base = {
            "method": "linear",
            "mode": mode,
            "available": True,
            "gap_s": gap,
            "ratio": ratio,
            "before_source_row": before.source_row,
            "after_source_row": after.source_row,
            "before_state_time_utc": before.state_time_utc,
            "after_state_time_utc": after.state_time_utc,
            "time_resolution_s": before.time_resolution_s,
        }

        if mode == INTERP_ENU_FIRST:
            east = _interp_scalar(before.east_m, after.east_m, ratio)
            north = _interp_scalar(before.north_m, after.north_m, ratio)
            up = _interp_scalar(before.up_m, after.up_m, ratio)
            if east is None or north is None:
                # 源记录没有可用 ENU（空间转换失败）→ 该栅格点不可用
                return self._unavailable(
                    before,
                    grid_time,
                    "source_enu_unavailable",
                    gap=gap,
                    after=after,
                )
            return GridPoint(
                time_epoch=grid_time,
                time_utc=self._format_grid_time(grid_time),
                source_id=before.source_id,
                source_type=before.source_type,
                local_track_id=before.local_track_id,
                position={},
                speed_mps=_interp_scalar(before.speed_mps, after.speed_mps, ratio),
                course_rad=_interp_angle(
                    before.course_rad, after.course_rad, ratio
                ),
                east_m=east,
                north_m=north,
                up_m=up,
                interpolation=base,
            )

        # observation_domain：在各自观测域内插值
        return GridPoint(
            time_epoch=grid_time,
            time_utc=self._format_grid_time(grid_time),
            source_id=before.source_id,
            source_type=before.source_type,
            local_track_id=before.local_track_id,
            position=self._interpolate_position(
                before.source_type, before, after, ratio
            ),
            speed_mps=_interp_scalar(before.speed_mps, after.speed_mps, ratio),
            course_rad=_interp_angle(before.course_rad, after.course_rad, ratio),
            interpolation=base,
        )

    @staticmethod
    def _bracket(times: Sequence[float], target: float) -> Tuple[int, int]:
        low, high = 0, len(times) - 1
        while high - low > 1:
            mid = (low + high) // 2
            if times[mid] <= target:
                low = mid
            else:
                high = mid
        if abs(times[low] - target) < 1e-9:
            return low, low
        return low, high

    @staticmethod
    def _interpolate_position(
        source_type: str,
        before: TrackPoint,
        after: TrackPoint,
        ratio: float,
    ) -> Dict[str, Any]:
        """在各自的观测域内插值。

        雷达与光电：距离、方位（角度按最短路径）—— 光电与雷达同址部署，
        位置表达方式完全一致，必须走同一分支；
        AIS：经纬度（线性插值）。
        """
        if source_type in ("radar", "radar_cutdata", "optical"):
            return {
                "range_m": _interp_scalar(
                    before.position.get("range_m"),
                    after.position.get("range_m"),
                    ratio,
                ),
                "bearing_rad": _interp_angle(
                    before.position.get("bearing_rad"),
                    after.position.get("bearing_rad"),
                    ratio,
                ),
                "bearing_reference": before.position.get("bearing_reference"),
            }
        return {
            "latitude_deg": _interp_scalar(
                before.position.get("latitude_deg"),
                after.position.get("latitude_deg"),
                ratio,
            ),
            "longitude_deg": _interp_scalar(
                before.position.get("longitude_deg"),
                after.position.get("longitude_deg"),
                ratio,
            ),
        }

    def _unavailable(
        self,
        before: TrackPoint,
        grid_time: float,
        reason: str,
        gap: Optional[float] = None,
        after: Optional[TrackPoint] = None,
    ) -> GridPoint:
        return GridPoint(
            time_epoch=grid_time,
            time_utc=self._format_grid_time(grid_time),
            source_id=before.source_id,
            source_type=before.source_type,
            local_track_id=before.local_track_id,
            position={},
            speed_mps=None,
            course_rad=None,
            interpolation={
                "method": "none",
                "available": False,
                "reason": reason,
                "gap_s": gap,
                "before_source_row": before.source_row,
                "after_source_row": after.source_row if after else None,
                "before_state_time_utc": before.state_time_utc,
                "after_state_time_utc": after.state_time_utc if after else None,
                "time_resolution_s": before.time_resolution_s,
            },
        )


def _interp_scalar(
    before: Optional[float], after: Optional[float], ratio: float
) -> Optional[float]:
    if before is None or after is None:
        return before if before is not None else after
    return before + ratio * (after - before)


def _interp_angle(
    before: Optional[float], after: Optional[float], ratio: float
) -> Optional[float]:
    if before is None or after is None:
        return before if before is not None else after
    delta = (after - before + math.pi) % (2 * math.pi) - math.pi
    return (before + ratio * delta) % (2 * math.pi)


def _epoch_for_seconds_of_day(value: float, reference_epoch: float) -> float:
    """把 UTC 日内秒转为对应日期当日的 Unix 秒。

    日期取 `reference_epoch` 所在日期——即数据自身的日期，而不是系统当前
    日期，避免数据集与运行环境日期不一致时栅格落到错误的日期上。
    """
    day = datetime.fromtimestamp(reference_epoch, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return day.timestamp() + value
