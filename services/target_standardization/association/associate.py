"""航迹关联：跨传感器航迹配对。

## 输入（全部来自已有产物，不重新对齐）

  - `tracks/track_index.csv` + `tracks/<sensor>/<id>.csv`：稳定航迹
    （AIS / 光电 / 雷达 AT 与 RT）
  - `tracks/radar_reconstructed/*.csv`：CDT 跨帧重建航迹（无 RT 时用）
  - 雷达侧只用**纯测量点**：`radar_measurement_kind != ais_associated`
    （AT 快照拷贝不参与，避免 AIS 自己跟自己比）

## 方法

  1. 时间可比性断言（`timebase.check_timebase`，相对本地基准即可）；
  2. 对每对跨源航迹求重叠段残差：把短轨每点插值到长轨（容差
     `MAX_INTERP_DT_S`），得平面距离序列 → 中位/均值/最大；
     航向取轨迹级 `mean_course_deg` 差（绕回），速度取均值差；
  3. 门限粗筛 + Hungarian 全局最优指派（每对传感器分别做）；
  4. 输出关联对 + 落选原因，可回溯到点。

## 门限依据（实测，不拍脑袋）

  - AIS 纯雷达同目标：03-12 中位 144 m、03-05 中位 167 m；
    最近异船 ≥600 m ⇒ 平面门限取 300 m。
  - 光电同目标（未去方位偏差）：03-05 为 358 m、03-12 为 406~496 m；
    最近异船 604~617 m ⇒ 平面门限取 550 m，且必须加航向门限
    （同目标航向差 ≤10°，异船通常更大）辅助。
  - RT-1 距所有 AIS ≥612 m ⇒ 正常落选，这是对的（RT 是另一目标）。

## 偏差处理

  - 默认**应用显式系统差修正**（调用方可关闭）：
    光电方位偏差约 +5.5°（03-05 为 +5.68°、03-12 为 +5.44°，
    跨数据集极差仅 0.24°，设备级常量）；
    雷达 CDT 方位偏差约 −2.4°（03-05 稳定 −2.404°±0.200°）。
    修正 = ENU 平面绕站心旋转（等价于极坐标方位修正，因 ENU 原点即站心），
    只作用于评分时的 ENU，不改原航迹。
    依据见 `association/bias.py` 与三源一致性验证结论。
  - 调用方可显式传入 `optical_bias_deg` / `radar_bias_deg`
    覆盖默认值，或 `apply_bias_correction=False` 关闭。
"""

from __future__ import annotations

import csv
import math
import os
import glob
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

# -- 参数（见模块文档"门限依据"） -------------------------------------------

#: 插值容差：短轨点与长轨相邻点时间差超过此值则该点不参与评分
MAX_INTERP_DT_S = 15.0

#: 插值跨度上限：长轨包络该点的两端点时间跨度超过此值则不插值。
#: AIS 报告稀疏（03-05 包平均间隔约 85 s），跨度必须容忍分钟级；
#: 平面门限仍会兜住外推误差，不会误收。
MAX_INTERP_SPAN_S = 300.0

#: 最小重叠点数：少于此数不评分（证据不足）
MIN_OVERLAP_POINTS = 3

#: 航向门限只在双方都有稳定航向时启用：任一方平均速度低于此值
#: （锚泊/漂航，航向无定义）则跳过航向门限
COURSE_MIN_SPEED_MPS = 1.0

#: 平面距离门限（米）。修正后实测：
#: radar-ais 同目标 ≤150 m，最近异船 ≥294 m ⇒ 取 200 m；
#: optical-ais 同目标 ≤121 m，最近异船 ≥1014 m ⇒ 取 300 m；
#: radar-optical 同目标主对 ≤174 m（含 det 轨），碎片杂波 289~593 m ⇒
#: 取 600 m，但 median > 300 m 的对标记低置信（真值不完备，不敢收紧）。
GATE_RADAR_AIS_M = 200.0
GATE_OPTICAL_AIS_M = 300.0
GATE_RADAR_OPTICAL_M = 600.0
#: radar-optical 低置信阈值（米）
GATE_RADAR_OPTICAL_LOWCONF_M = 300.0

#: 航向差门限（度）
GATE_COURSE_DEG = 15.0

#: 速度差门限（米/秒）
GATE_SPEED_MPS = 3.0

#: 系统差修正默认值（方位偏差 = 实测 − 真值，度）。
#: 光电约 +5.5°（03-05 为 +5.68°、03-12 为 +5.44°，跨数据集极差仅 0.24°，
#: 设备级常量）；雷达 CDT 约 −2.4°（03-05 稳定 −2.404°±0.200°）。
#: 修正只旋转评分用 ENU（绕站心/原点），不改原航迹。
OPTICAL_BIAS_DEG = 5.5
RADAR_BIAS_DEG = -2.4


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


def _rotated_points(
    points: Sequence[Tuple[float, float, float]], bias_deg: float
) -> List[Tuple[float, float, float]]:
    """ENU 绕原点（站心）做数学旋转 +bias_deg 度。

    约定：`bias_deg = 实测方位 − 真值方位`（见模块文档）。
    经验证：`bearing_corrected = bearing_measured − bias` 恰等于
    ENU 平面数学逆时针旋转 +bias 度（03-05/03-12 实测闭合）。
    只用于评分，不改原航迹。
    """
    if not bias_deg:
        return list(points)
    angle = math.radians(bias_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return [
        (
            epoch,
            east * cos_a - north * sin_a,
            east * sin_a + north * cos_a,
        )
        for epoch, east, north in points
    ]


def _corrected(
    track: "AssocTrack",
    optical_bias_deg: float = OPTICAL_BIAS_DEG,
    radar_bias_deg: float = RADAR_BIAS_DEG,
    apply: bool = True,
) -> List[Tuple[float, float, float]]:
    if not apply:
        return track.points
    if track.sensor == "optical":
        return _rotated_points(track.points, optical_bias_deg)
    if track.sensor == "radar":
        return _rotated_points(track.points, radar_bias_deg)
    return track.points


def _course_diff_deg(first: Optional[float], second: Optional[float]) -> Optional[float]:
    if first is None or second is None:
        return None
    diff = abs((first - second) % 360.0)
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


@dataclass
class AssocTrack:
    """参与关联的一条航迹（纯测量点序列）。"""

    track_id: str
    sensor: str  # 'ais' | 'radar' | 'optical'
    points: List[Tuple[float, float, float]] = field(default_factory=list)
    mean_speed: Optional[float] = None
    mean_course: Optional[float] = None
    point_count: int = 0
    raw_point_count: int = 0  # 过滤前点数（诊断混合航迹用）

    @property
    def start_s(self) -> Optional[float]:
        return self.points[0][0] if self.points else None

    @property
    def end_s(self) -> Optional[float]:
        return self.points[-1][0] if self.points else None


def _interp_pos(
    seq: Sequence[Tuple[float, float, float]],
    epoch: float,
    max_dt: float = MAX_INTERP_DT_S,
    max_span: float = MAX_INTERP_SPAN_S,
) -> Optional[Tuple[float, float]]:
    """线性插值 seq 到 epoch； bracketing 点时间差超 max_dt 则返回 None。"""
    if not seq:
        return None
    if epoch < seq[0][0] or epoch > seq[-1][0]:
        return None
    import bisect

    times = [p[0] for p in seq]
    i = bisect.bisect_left(times, epoch)
    if i < len(seq) and seq[i][0] == epoch:
        return seq[i][1], seq[i][2]
    if i == 0 or i >= len(seq):
        return None
    before, after = seq[i - 1], seq[i]
    if after[0] == before[0]:
        return before[1], before[2]
    # 插值跨度上限：AIS 稀疏时包络跨度可达分钟级，允许插值，
    # 平面门限兜住外推误差
    if after[0] - before[0] > max_span:
        return None
    if min(abs(before[0] - epoch), abs(after[0] - epoch)) > max_dt:
        return None
    ratio = (epoch - before[0]) / (after[0] - before[0])
    return (
        before[1] + ratio * (after[1] - before[1]),
        before[2] + ratio * (after[2] - before[2]),
    )


def pair_score(
    first: AssocTrack,
    second: AssocTrack,
    max_dt: float = MAX_INTERP_DT_S,
    optical_bias_deg: float = OPTICAL_BIAS_DEG,
    radar_bias_deg: float = RADAR_BIAS_DEG,
    apply_bias_correction: bool = True,
) -> Dict[str, Any]:
    """求两条航迹重叠段残差。

    用点数少的一方为基准向另一方插值。返回距离序列统计 + 航向/速度差。

    评分前先做显式系统差修正（ENU 绕站心旋转，见 `_corrected`）：
    光电默认 +5.5°、雷达默认 −2.4°，AIS 不动。`apply_bias_correction=False`
    可关闭（用于消融对比）。
    """
    if not first.points or not second.points:
        return {"overlap": 0, "ok": False, "reason": "empty_track"}
    first_pts = _corrected(
        first, optical_bias_deg, radar_bias_deg, apply_bias_correction
    )
    second_pts = _corrected(
        second, optical_bias_deg, radar_bias_deg, apply_bias_correction
    )
    # 用点数少的一方为基准
    if len(first_pts) <= len(second_pts):
        base_pts, other_pts = first_pts, second_pts
    else:
        base_pts, other_pts = second_pts, first_pts
    distances: List[float] = []
    for epoch, east, north in base_pts:
        pos = _interp_pos(other_pts, epoch, max_dt)
        if pos is None:
            continue
        distances.append(math.hypot(pos[0] - east, pos[1] - north))
    if len(distances) < MIN_OVERLAP_POINTS:
        return {"overlap": len(distances), "ok": False, "reason": "insufficient_overlap"}
    distances.sort()
    mid = distances[len(distances) // 2]
    result: Dict[str, Any] = {
        "overlap": len(distances),
        "ok": True,
        "median_m": round(mid, 1),
        "mean_m": round(sum(distances) / len(distances), 1),
        "min_m": round(distances[0], 1),
        "max_m": round(distances[-1], 1),
    }
    course_diff = _course_diff_deg(first.mean_course, second.mean_course)
    if course_diff is not None:
        result["course_diff_deg"] = round(course_diff, 2)
    if first.mean_speed is not None and second.mean_speed is not None:
        result["speed_diff_mps"] = round(abs(first.mean_speed - second.mean_speed), 3)
    return result


def _gate_for(sensor_a: str, sensor_b: str) -> float:
    pair = {sensor_a, sensor_b}
    if pair == {"radar", "ais"}:
        return GATE_RADAR_AIS_M
    if pair == {"optical", "ais"}:
        return GATE_OPTICAL_AIS_M
    return GATE_RADAR_OPTICAL_M


def associate_pair(
    tracks_a: List[AssocTrack],
    tracks_b: List[AssocTrack],
    gate_m: Optional[float] = None,
    gate_course_deg: float = GATE_COURSE_DEG,
    gate_speed_mps: float = GATE_SPEED_MPS,
    optical_bias_deg: float = OPTICAL_BIAS_DEG,
    radar_bias_deg: float = RADAR_BIAS_DEG,
    apply_bias_correction: bool = True,
    allow_many_to_one: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """两组航迹做指派（默认 Hungarian 一对一）。

    返回 `(accepted, rejected)`：accepted 为指派成功的对（含 score），
    rejected 为被门限筛掉的候选（含原因）。

    - 评分前先做显式系统差修正（光电默认 +5.5°、雷达默认 −2.4°，
      见 `pair_score`）；`apply_bias_correction=False` 可关闭做消融对比。
    - `allow_many_to_one=True` 时退化为"每条 A 取最优 B"（B 端可重复命中）：
      用于光电 Inf/TV 双设备（同一目标两条轨）与雷达重建碎片（同一目标多段）
      的情形；此时 Hungarian 的一对一约束是错的。
    """
    if not tracks_a or not tracks_b:
        return [], []
    if gate_m is None:
        gate_m = _gate_for(tracks_a[0].sensor, tracks_b[0].sensor)
    n, m = len(tracks_a), len(tracks_b)
    cost = np.full((n, m), np.inf)
    scores: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for i, first in enumerate(tracks_a):
        for j, second in enumerate(tracks_b):
            score = pair_score(
                first,
                second,
                optical_bias_deg=optical_bias_deg,
                radar_bias_deg=radar_bias_deg,
                apply_bias_correction=apply_bias_correction,
            )
            scores[(i, j)] = score
            if not score.get("ok"):
                continue
            if score["median_m"] > gate_m:
                continue
            course_diff = score.get("course_diff_deg")
            # 锚泊/漂航（任一方平均速度低于阈值）时航向无定义，跳过航向门限
            course_gated = (
                course_diff is not None
                and (first.mean_speed or 0.0) >= COURSE_MIN_SPEED_MPS
                and (second.mean_speed or 0.0) >= COURSE_MIN_SPEED_MPS
            )
            if course_gated and course_diff > gate_course_deg:
                continue
            speed_diff = score.get("speed_diff_mps")
            if speed_diff is not None and speed_diff > gate_speed_mps:
                continue
            cost[i, j] = score["median_m"]
    accepted: List[Dict[str, Any]] = []
    used_b: set = set()
    assignments: List[Tuple[int, int]] = []
    if allow_many_to_one:
        # 每条 A 取最优 B（B 端可重复命中）：用于 Inf/TV 双设备与重建碎片
        for i in range(n):
            row = cost[i]
            if not np.isfinite(row).any():
                continue
            j = int(np.nanargmin(np.where(np.isfinite(row), row, np.inf)))
            assignments.append((i, j))
    elif np.isfinite(cost).any():
        try:
            row_ind, col_ind = linear_sum_assignment(cost)
            assignments = [
                (int(i), int(j))
                for i, j in zip(row_ind, col_ind)
                if not np.isinf(cost[i, j])
            ]
        except ValueError:
            # 不可行矩阵（某行/列全 inf）时退回贪心：按代价从小到大依次指派
            finite_pairs = sorted(
                (
                    (cost[i, j], i, j)
                    for i in range(n)
                    for j in range(m)
                    if np.isfinite(cost[i, j])
                ),
                key=lambda item: item[0],
            )
            used_a: set = set()
            used_b_greedy: set = set()
            for _, i, j in finite_pairs:
                if i in used_a or j in used_b_greedy:
                    continue
                assignments.append((i, j))
                used_a.add(i)
                used_b_greedy.add(j)
    if assignments:
        for i, j in assignments:
            score = scores[(i, j)]
            accepted.append(
                {
                    "track_a": tracks_a[i].track_id,
                    "track_b": tracks_b[j].track_id,
                    "median_m": score["median_m"],
                    "mean_m": score["mean_m"],
                    "min_m": score["min_m"],
                    "max_m": score["max_m"],
                    "overlap": score["overlap"],
                    "course_diff_deg": score.get("course_diff_deg"),
                    "speed_diff_mps": score.get("speed_diff_mps"),
                    "confidence": (
                        "low"
                        if {
                            tracks_a[i].sensor,
                            tracks_b[j].sensor,
                        }
                        == {"radar", "optical"}
                        and score["median_m"]
                        > GATE_RADAR_OPTICAL_LOWCONF_M
                        else "high"
                    ),
                }
            )
            used_b.add(j)
    rejected: List[Dict[str, Any]] = []
    for (i, j), score in scores.items():
        if any(a["track_a"] == tracks_a[i].track_id and a["track_b"] == tracks_b[j].track_id for a in accepted):
            continue
        reason = score.get("reason", "")
        if score.get("ok"):
            course_diff = score.get("course_diff_deg")
            course_gated_here = (
                course_diff is not None
                and (tracks_a[i].mean_speed or 0.0) >= COURSE_MIN_SPEED_MPS
                and (tracks_b[j].mean_speed or 0.0) >= COURSE_MIN_SPEED_MPS
            )
            if score["median_m"] > gate_m:
                reason = f"median {score['median_m']}m > gate {gate_m}m"
            elif course_gated_here and course_diff > gate_course_deg:
                reason = f"course {course_diff}° > gate {gate_course_deg}°"
            elif score.get("speed_diff_mps") is not None and score["speed_diff_mps"] > gate_speed_mps:
                reason = f"speed {score['speed_diff_mps']}m/s > gate {gate_speed_mps}m/s"
            elif score["overlap"] < MIN_OVERLAP_POINTS:
                reason = "insufficient_overlap"
            else:
                reason = "not_selected_by_hungarian"
        rejected.append(
            {
                "track_a": tracks_a[i].track_id,
                "track_b": tracks_b[j].track_id,
                "overlap": score.get("overlap", 0),
                "median_m": score.get("median_m"),
                "reason": reason,
            }
        )
    return accepted, rejected


# -- 航迹加载 ---------------------------------------------------------------


def _read_index(tracks_dir: str) -> List[Dict[str, Any]]:
    path = os.path.join(tracks_dir, "track_index.csv")
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_candidates(
    alignment_dir: str,
    min_points: int = 3,
    use_reconstructed: bool = True,
) -> Dict[str, List[AssocTrack]]:
    """从对齐输出目录加载关联候选。

    规则：

      - AIS：stable 且点数 ≥ min_points；
      - 雷达：RT（`radar:<id>` 全纯测量）整条；MMSI 混合航迹只取
        `radar_measurement_kind != ais_associated` 的点；900xxx 不用
        （由重建航迹代替）；重建航迹全部纳入；
      - 光电：stable 且点数 ≥ min_points（Inf/TV 已分离）。
    """
    tracks_dir = os.path.join(alignment_dir, "tracks")
    index = _read_index(tracks_dir)
    by_id = {row["track_id"]: row for row in index}
    out: Dict[str, List[AssocTrack]] = {"ais": [], "radar": [], "optical": []}

    def read_points(sensor: str, local_id: str) -> List[Dict[str, Any]]:
        # 光电文件名含 @，雷达数字 id 均可直接拼接
        path = os.path.join(tracks_dir, sensor, f"{local_id}.csv")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    for row in index:
        if row.get("track_stability") != "stable":
            continue
        sensor = row.get("sensor_type")
        if sensor not in out:
            continue
        local_id = row.get("local_track_id") or ""
        if sensor == "radar" and local_id.startswith("900"):
            continue  # 内部号不用，重建航迹代替
        rows = read_points(sensor, local_id)
        if not rows:
            continue
        points: List[Tuple[float, float, float]] = []
        for item in rows:
            if sensor == "radar" and item.get("radar_measurement_kind") == "ais_associated":
                continue  # 快照拷贝不参与
            moment = _parse_time(item.get("time_utc") or "")
            east = _to_float(item.get("east_m"))
            north = _to_float(item.get("north_m"))
            if moment is None or east is None or north is None:
                continue
            points.append((moment.timestamp(), east, north))
        points.sort()
        if len(points) < min_points:
            continue
        out[sensor].append(
            AssocTrack(
                track_id=row["track_id"],
                sensor=sensor,
                points=points,
                mean_speed=_to_float(row.get("mean_speed_mps")),
                mean_course=_to_float(row.get("mean_course_deg")),
                point_count=len(points),
                raw_point_count=int(row.get("point_count") or 0),
            )
        )

    if use_reconstructed:
        recon_index = os.path.join(tracks_dir, "radar_reconstructed_index.csv")
        if os.path.exists(recon_index):
            with open(recon_index, encoding="utf-8", newline="") as handle:
                recon_rows = list(csv.DictReader(handle))
            for row in recon_rows:
                tid = row.get("track_id") or ""
                short = tid.split(":")[-1] if ":" in tid else tid
                path = os.path.join(tracks_dir, "radar_reconstructed", f"{short}.csv")
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8", newline="") as handle:
                    items = list(csv.DictReader(handle))
                points = []
                for item in items:
                    moment = _parse_time(item.get("time_utc") or "")
                    east = _to_float(item.get("east_m"))
                    north = _to_float(item.get("north_m"))
                    if moment is None or east is None or north is None:
                        continue
                    points.append((moment.timestamp(), east, north))
                points.sort()
                if len(points) < min_points:
                    continue
                # 重建航迹暂无速度/航向（点序列可事后补，这里先空着）
                out["radar"].append(
                    AssocTrack(
                        track_id=tid,
                        sensor="radar",
                        points=points,
                        mean_speed=None,
                        mean_course=None,
                        point_count=len(points),
                        raw_point_count=len(points),
                    )
                )
    return out
