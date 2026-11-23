"""CDT 跨帧重建：把逐帧检测点聚成稳定航迹。

## 为什么需要这一层

MTDSP 的 CDT 行用 `目标编号 = 900xxx` 内部号。经核验
（`standardization/radar_cutdata_adapter.py` 第 3 条），**该编号不是稳定
航迹号**：同一物理目标在多个内部号之间切换。`track_export.py` 已把
`900xxx` 分组标记为 `unstable_internal_id`，不可直接作关联主键。

做航迹关联前，必须先把逐帧检测点跨帧关联成稳定航迹：

  - 03-05 包有可靠的 RT 航迹（RT-1：107 行连续、逐段速度中位 0.25 m/s、
    与 AT 行无精确相等拷贝），**可直接使用**，不走本模块；
  - 03-12 包**无 RT 行**，必须走本模块重建。

## 方法（两步）

  1. **帧内去重**：同一帧内（方位 ±0.5°，距离 ±60 m）的检测视为同一次
     回波的多报，合并为一个簇心。容差依据：方位量化 0.1°、距离量化 1 m，
     5 km 处真实相邻目标的角间距通常 >1°。

  2. **跨帧匹配**：带速度预测的 Hungarian 全局匹配。
     每个活动航迹维护 `(rng, brg, vr, vb, t)`；预测位置
     `rng + vr·dt`，代价为极坐标距离 `√(dR² + (dB·52)²)`，
     门限随时间外推 `base + vmax·dt`（上限 600 m）。
     速度用指数平滑更新；超 `max_gap_s` 未更新则死亡；
     未匹配簇心则新生。

     用 Hungarian 而不用贪心：密集杂波下贪心会被近邻吸走，
     全局最优对"目标被近距离杂波包围"的情形更稳。

## 参数

在 03-12 包上用**光电确认关联真值**（`匹配编号=1` 的 14 个 CDT 检测）
标定。真值稀疏（横跨 86 帧、间隔 22–55 s）且场景密集（每帧 10–27 个检测），
碎裂不可完全避免；参数选择**宁可拆分、不错并** —— 并错两个目标比拆分
一个目标对关联的危害更大。

  - `DTHETA = 0.5°`，`DRANGE = 60 m`（帧内容差）
  - `BASE_GATE = 100 m`，`VMAX = 8 m/s`（跨帧门限）
  - `MAX_GAP_S = 60 s`（航迹死亡）
  - `V_SMOOTH = 0.7`（速度指数平滑系数）

## 输出

与 `track_export` 同格式（同一套列），便于关联阶段统一消费：

  - `track_kind = "radar_reconstructed"`，`track_stability = "reconstructed"`；
  - 航迹号为 `R<序号>`（重建序号，与传感器原始编号无关）；
  - 每个点保留来源检测的行号（`source_row`）与帧号，可回溯；
  - 航迹级质量：长度、平滑度（速度 RMS）、最大间隔，供关联加权/过滤。

## 不做什么

  - **不做自动偏差校准**：CDT 与 AIS 之间存在约 −2.4° 的系统性方位偏差
    （参考点基准差异，见 `association/bias.py`），本模块输出**纯测量**，
    偏差修正由关联阶段显式调用 `bias.apply_cdt_correction` 完成。
"""

from __future__ import annotations

import csv
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

# -- 参数（见模块文档"参数"一节） ------------------------------------------

#: 帧内去重容差：方位（度）、距离（米）
DTHETA_DEG = 0.5
DRANGE_M = 60.0

#: 跨帧匹配门限：基础值（米）+ 速度外推（米/秒 × 秒），上限 600 m
BASE_GATE_M = 100.0
VMAX_MPS = 8.0
MAX_GATE_M = 600.0

#: 航迹死亡：超此时长未更新则关闭
MAX_GAP_S = 60.0

#: 速度指数平滑系数
V_SMOOTH = 0.7

#: 1° 方位在典型作用距离（3–5 km）对应的横向米数，用于极坐标距离换算
METERS_PER_DEG = 52.0

#: 真值验证用的匹配容差（米）
TRUTH_MATCH_TOL_M = 150.0


def _parse_time(text: str) -> Optional[datetime]:
    raw = (text or "").split(" (local)")[0].strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _bearing_diff_deg(first: float, second: float) -> float:
    """两方位差（度），绕回处理到 (−180, 180]。"""
    diff = (first - second) % 360.0
    if diff > 180.0:
        diff -= 360.0
    return diff


def polar_distance_m(
    rng1: float, brg1: float, rng2: float, brg2: float
) -> float:
    """极坐标距离（米）：径向差与横向差（方位差 × 米/度）合成。"""
    return math.hypot(rng1 - rng2, _bearing_diff_deg(brg1, brg2) * METERS_PER_DEG)


# -- 数据结构 ---------------------------------------------------------------


@dataclass
class Cluster:
    """一帧内的一个簇（一次回波）。"""

    rng_m: float
    brg_deg: float
    members: List[int] = field(default_factory=list)


@dataclass
class ActiveTrack:
    """跨帧匹配中的活动航迹状态。"""

    rng_m: float
    brg_deg: float
    time: datetime
    vr_mps: float = 0.0
    vb_dps: float = 0.0
    segments: List[Tuple[float, float, float, List[int]]] = field(
        default_factory=list
    )


def cluster_frame(
    detections: Sequence[Tuple[int, float, float]],
    dtheta_deg: float = DTHETA_DEG,
    drange_m: float = DRANGE_M,
) -> List[Cluster]:
    """帧内去重：贪心单链聚类。

    `detections` 为 `(检测序号, 距离, 方位)`。返回簇心列表。
    """
    clusters: List[Cluster] = []
    for index, rng, brg in detections:
        placed = False
        for cluster in clusters:
            if abs(_bearing_diff_deg(cluster.brg_deg, brg)) <= dtheta_deg and abs(
                cluster.rng_m - rng
            ) <= drange_m:
                cluster.members.append(index)
                placed = True
                break
        if not placed:
            clusters.append(Cluster(rng_m=rng, brg_deg=brg, members=[index]))
    # 重算簇心为成员均值
    member_lookup = {index: (rng, brg) for index, rng, brg in detections}
    for cluster in clusters:
        rngs = [member_lookup[i][0] for i in cluster.members]
        brgs = [member_lookup[i][1] for i in cluster.members]
        cluster.rng_m = float(sum(rngs) / len(rngs))
        # 方位均值需处理绕回：以首成员为基准
        base = brgs[0]
        shifted = [base + _bearing_diff_deg(b, base) for b in brgs]
        cluster.brg_deg = (base + sum(shifted) / len(shifted)) % 360.0
    return clusters


class CdtTracker:
    """CDT 跨帧重建器：帧内去重 + 带速度预测的 Hungarian 匹配。"""

    def __init__(
        self,
        dtheta_deg: float = DTHETA_DEG,
        drange_m: float = DRANGE_M,
        base_gate_m: float = BASE_GATE_M,
        vmax_mps: float = VMAX_MPS,
        max_gate_m: float = MAX_GATE_M,
        max_gap_s: float = MAX_GAP_S,
        v_smooth: float = V_SMOOTH,
    ):
        self.dtheta_deg = dtheta_deg
        self.drange_m = drange_m
        self.base_gate_m = base_gate_m
        self.vmax_mps = vmax_mps
        self.max_gate_m = max_gate_m
        self.max_gap_s = max_gap_s
        self.v_smooth = v_smooth

    # -- 主流程 ------------------------------------------------------------

    def reconstruct(
        self, frames: List[Tuple[float, datetime, List[Dict[str, Any]]]]
    ) -> Dict[int, List[Dict[str, Any]]]:
        """输入按帧排序的检测：`[(帧号, 时间, [检测行])]`。

        检测行需含 `range_m` / `bearing_deg`。返回
        `{重建航迹号: [点（含来源检测引用）]}`。
        """
        tracks: Dict[int, List[Dict[str, Any]]] = {}
        active: Dict[int, ActiveTrack] = {}
        next_id = [0]

        for frame_index, moment, detections in frames:
            # 帧内去重
            pts = []
            for order, row in enumerate(detections):
                rng = _to_float(row.get("range_m"))
                brg = _to_float(row.get("bearing_deg"))
                if rng is None or brg is None:
                    continue
                pts.append((order, rng, brg))
            clusters = cluster_frame(pts, self.dtheta_deg, self.drange_m)

            # 修剪死亡航迹
            active = {
                pid: state
                for pid, state in active.items()
                if (moment - state.time).total_seconds() <= self.max_gap_s
            }

            matched_clusters: set = set()
            pids = list(active.keys())
            if pids and clusters:
                cost = np.full((len(pids), len(clusters)), np.inf)
                for i, pid in enumerate(pids):
                    state = active[pid]
                    dt = (moment - state.time).total_seconds()
                    if dt <= 0:
                        continue
                    pred_rng = state.rng_m + state.vr_mps * dt
                    pred_brg = (state.brg_deg + state.vb_dps * dt) % 360.0
                    gate = min(
                        self.base_gate_m + self.vmax_mps * dt, self.max_gate_m
                    )
                    for k, cluster in enumerate(clusters):
                        distance = polar_distance_m(
                            cluster.rng_m,
                            cluster.brg_deg,
                            pred_rng,
                            pred_brg,
                        )
                        if distance < gate:
                            cost[i, k] = distance
                if np.isfinite(cost).any():
                    try:
                        row_ind, col_ind = linear_sum_assignment(cost)
                    except ValueError:
                        row_ind, col_ind = [], []
                    for i, k in zip(row_ind, col_ind):
                        if np.isinf(cost[i, k]):
                            continue
                        pid = pids[i]
                        cluster = clusters[k]
                        state = active[pid]
                        dt = (moment - state.time).total_seconds()
                        if dt > 0:
                            state.vr_mps = self.v_smooth * state.vr_mps + (
                                1 - self.v_smooth
                            ) * (cluster.rng_m - state.rng_m) / dt
                            state.vb_dps = self.v_smooth * state.vb_dps + (
                                1 - self.v_smooth
                            ) * _bearing_diff_deg(
                                cluster.brg_deg, state.brg_deg
                            ) / dt
                        state.rng_m = cluster.rng_m
                        state.brg_deg = cluster.brg_deg
                        state.time = moment
                        state.segments.append(
                            (
                                frame_index,
                                cluster.rng_m,
                                cluster.brg_deg,
                                list(cluster.members),
                            )
                        )
                        point = self._make_point(
                            frame_index, moment, cluster, detections
                        )
                        tracks[pid].append(point)
                        matched_clusters.add(k)

            # 未匹配簇心 → 新生航迹
            for k, cluster in enumerate(clusters):
                if k in matched_clusters:
                    continue
                next_id[0] += 1
                pid = next_id[0]
                active[pid] = ActiveTrack(
                    rng_m=cluster.rng_m,
                    brg_deg=cluster.brg_deg,
                    time=moment,
                    segments=[
                        (
                            frame_index,
                            cluster.rng_m,
                            cluster.brg_deg,
                            list(cluster.members),
                        )
                    ],
                )
                tracks[pid] = [
                    self._make_point(frame_index, moment, cluster, detections)
                ]

        return tracks

    # -- 点构造 ------------------------------------------------------------

    @staticmethod
    def _make_point(
        frame_index: float,
        moment: datetime,
        cluster: Cluster,
        detections: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """簇心点：位置取簇心，来源取成员检测的第一行（可回溯）。"""
        first = detections[cluster.members[0]]
        epoch_s = moment.replace(tzinfo=timezone.utc).timestamp()
        east = _to_float(first.get("east_m"))
        north = _to_float(first.get("north_m"))
        # 簇心极坐标 → 用成员均值位置做近似（簇内散布小，误差可忽略）
        member_east = [
            _to_float(detections[i].get("east_m")) or 0.0
            for i in cluster.members
        ]
        member_north = [
            _to_float(detections[i].get("north_m")) or 0.0
            for i in cluster.members
        ]
        return {
            "radar_frame_index": frame_index,
            "time_utc": moment.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "time_epoch_s": round(epoch_s, 3),
            "local_track_id": first.get("local_track_id"),
            "target_type": first.get("target_type"),
            "measurement_kind": "radar_reconstructed",
            "range_m": round(cluster.rng_m, 2),
            "bearing_deg": round(cluster.brg_deg, 4),
            "east_m": round(sum(member_east) / len(member_east), 3),
            "north_m": round(sum(member_north) / len(member_north), 3),
            "up_m": _to_float(first.get("up_m")),
            "latitude_deg": _to_float(first.get("latitude_deg")),
            "longitude_deg": _to_float(first.get("longitude_deg")),
            "error_bound_planar_m": _to_float(
                first.get("error_bound_planar_m")
            ),
            "source_row": first.get("source_row"),
            "member_count": len(cluster.members),
            "member_source_rows": [
                detections[i].get("source_row") for i in cluster.members
            ],
        }


def load_detections(
    path: str, only_radar_detected: bool = True
) -> List[Tuple[float, datetime, List[Dict[str, Any]]]]:
    """从 `radar_detections.csv` 按帧分组：`[(帧号, 时间, [检测行])]`。

    默认只收 `measurement_kind == radar_detected` 的纯雷达检测：
    AT 快照拷贝（`ais_associated`）与 RT（`radar_tracked`）不参与重建，
    否则重建航迹会混入 AIS 自己跟自己比。
    """
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if only_radar_detected and str(
                row.get("measurement_kind") or ""
            ) != "radar_detected":
                continue
            rows.append(row)
    grouped: Dict[float, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        frame = _to_float(row.get("radar_frame_index"))
        if frame is None:
            continue
        grouped[frame].append(row)
    frames: List[Tuple[float, datetime, List[Dict[str, Any]]]] = []
    for frame in sorted(grouped):
        members = grouped[frame]
        moment = _parse_time(str(members[0].get("time_utc") or ""))
        if moment is None:
            continue
        frames.append((frame, moment, members))
    return frames
