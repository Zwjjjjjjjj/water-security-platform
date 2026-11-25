"""
N-Partite Cluster Association

把 N 个传感器的本地航迹关联到全局 cluster (每个 cluster 包含每个传感器恰好一条航迹)。

设计:
  1. 复用现有 `TrackAssociation.compute_track_level_cost` 算两两 cost,
     但输入需要 A/B-prefixed 的 DataFrame, 所以临时重塑 long-format。
  2. 搜索 N-partite cluster: 用 "anchor + extension" 策略。
     - 选 anchor_sensor (默认 SONAR_A) 的每条 track, 对其它每个 sensor 跑
       sub-Hungarian 找最佳匹配, 组成 N 元组 candidate cluster
     - 评分: mean(pair_costs), 门控: 所有 pair_cost < soft_gate
  3. 输出: list of (sensor_id, track_id) tuples per cluster, 每条 track 在每时刻
     只能属于一个 cluster (用 "track 已使用" 集合避免复用)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from itertools import product
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from track_association import TrackAssociation  # noqa: E402


@dataclass
class Cluster:
    """N-partite cluster: 每个 sensor 一条 track"""
    members: Dict[str, str]   # sensor_id -> track_id
    pair_costs: Dict[Tuple[str, str], float]   # (s1,s2) -> cost
    mean_cost: float
    gate_pass: bool
    pair_pass_ratio: float


def _reshape_for_pair_cost(aligned_long: pd.DataFrame,
                            sensor_a: str, track_a: str,
                            sensor_b: str, track_b: str) -> pd.DataFrame:
    """
    把 long-format (columns: timestamp, sensor_id, track_id, x, y, ...)
    切成 (sensor_a, track_a) vs (sensor_b, track_b) 两个时间序列, 重塑成
    `compute_track_level_cost` 期望的 schema:
        track_a_id, track_b_id, Ax, Ay, ..., Bx, By, ...
    """
    a = aligned_long[
        (aligned_long['sensor_id'] == sensor_a) & (aligned_long['track_id'] == track_a)
    ].copy().sort_values('timestamp')
    b = aligned_long[
        (aligned_long['sensor_id'] == sensor_b) & (aligned_long['track_id'] == track_b)
    ].copy().sort_values('timestamp')

    if a.empty or b.empty:
        return pd.DataFrame()

    a = a.rename(columns={
        'x': 'Ax', 'y': 'Ay', 'vx': 'Avx', 'vy': 'Avy',
        'Pxx': 'APxx', 'Pyy': 'APyy', 'has_meas': 'has_A',
        't_local': 't_a',
    })
    b = b.rename(columns={
        'x': 'Bx', 'y': 'By', 'vx': 'Bvx', 'vy': 'Bvy',
        'Pxx': 'BPxx', 'Pyy': 'BPyy', 'has_meas': 'has_B',
        't_local': 't_b',
    })

    # 外连接: 取并集, 缺一边填 NaN
    merged = pd.merge(
        a[['timestamp', 'Ax', 'Ay', 'Avx', 'Avy', 'APxx', 'APyy', 'has_A', 't_a']],
        b[['timestamp', 'Bx', 'By', 'Bvx', 'Bvy', 'BPxx', 'BPyy', 'has_B', 't_b']],
        on='timestamp', how='outer'
    ).sort_values('timestamp').reset_index(drop=True)
    merged['track_a_id'] = track_a
    merged['track_b_id'] = track_b

    # 补全 compute_track_level_cost 期望的列:
    # position_distance / velocity_difference / heading_difference
    # 仅当两边都有效时计算
    pos_a = np.column_stack([merged['Ax'].values, merged['Ay'].values])
    pos_b = np.column_stack([merged['Bx'].values, merged['By'].values])
    valid = ~(np.isnan(pos_a).any(axis=1) | np.isnan(pos_b).any(axis=1))
    pos_dist = np.full(len(merged), np.nan)
    vel_diff = np.full(len(merged), np.nan)
    head_diff = np.full(len(merged), np.nan)
    if valid.any():
        pos_dist[valid] = np.linalg.norm(pos_a[valid] - pos_b[valid], axis=1)
        vel_a = np.column_stack([merged['Avx'].values, merged['Avy'].values])
        vel_b = np.column_stack([merged['Bvx'].values, merged['Bvy'].values])
        vel_diff[valid] = np.linalg.norm(vel_a[valid] - vel_b[valid], axis=1)
        head_a = np.degrees(np.arctan2(merged['Avx'].values, merged['Avy'].values))
        head_b = np.degrees(np.arctan2(merged['Bvx'].values, merged['Bvy'].values))
        head_diff[valid] = np.abs(head_a[valid] - head_b[valid])
        head_diff[valid] = np.minimum(head_diff[valid], 360.0 - head_diff[valid])
    merged['position_distance'] = pos_dist
    merged['velocity_difference'] = vel_diff
    merged['heading_difference'] = head_diff
    return merged


class NPartiteAssociator:
    def __init__(self, association_config: Dict,
                 n_way_config: Optional[Dict] = None):
        self.associator = TrackAssociation(association_config)
        self.anchor_sensor = (n_way_config or {}).get('anchor_sensor_id', 'SONAR_A')
        self.pair_gate_soft_factor = (n_way_config or {}).get('pair_gate_soft_factor', 1.2)
        self.cluster_min_pair_pass_ratio = (n_way_config or {}).get(
            'cluster_min_pair_pass_ratio', 0.7)
        self.max_candidates = (n_way_config or {}).get('max_clique_candidates', 256)

    def _track_ids_per_sensor(self, aligned_long: pd.DataFrame) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for s, sub in aligned_long.groupby('sensor_id'):
            out[s] = sorted(sub['track_id'].unique().tolist())
        return out

    def _pair_cost(self, aligned_long: pd.DataFrame,
                    s_a: str, t_a: str, s_b: str, t_b: str) -> Tuple[float, bool]:
        df = _reshape_for_pair_cost(aligned_long, s_a, t_a, s_b, t_b)
        if df.empty or len(df) < 3:
            return float('inf'), False
        info = self.associator.compute_track_level_cost(df, t_a, t_b)
        if info is None or not info.get('overall_pass', False):
            return float('inf'), False
        return float(info['total_cost']), True

    def find_clusters(self, aligned_long: pd.DataFrame,
                      sensors: Optional[Sequence[str]] = None
                      ) -> List[Cluster]:
        """
        返回所有 N-partite cluster。
        策略: 以 anchor_sensor 的每条 track 为中心, 对其它 sensors 跑 sub-Hungarian
              选 (s1, t1), (s2, t2), ..., 组成 cluster。
        """
        if aligned_long.empty:
            return []
        ids = self._track_ids_per_sensor(aligned_long)
        if sensors is None:
            sensors = sorted(ids.keys())
        if self.anchor_sensor not in sensors:
            sensors = [self.anchor_sensor] + [s for s in sensors if s != self.anchor_sensor]
        n = len(sensors)
        if n < 2:
            return []

        anchor_tracks = ids.get(self.anchor_sensor, [])
        used: Set[Tuple[str, str]] = set()
        clusters: List[Cluster] = []

        # 软门限 = pair_pass_ratio * association_threshold (经验值)
        soft_gate = self.pair_gate_soft_factor * self.associator.association_threshold

        for t_anchor in anchor_tracks:
            members: Dict[str, str] = {self.anchor_sensor: t_anchor}
            used.add((self.anchor_sensor, t_anchor))
            pair_costs: Dict[Tuple[str, str], float] = {}
            pair_passes: List[bool] = []

            for s in sensors:
                if s == self.anchor_sensor:
                    continue
                # sub-Hungarian: between members[anchor]=t_anchor (held) and s's tracks
                row_costs: List[float] = []
                pass_mask: List[bool] = []
                for t_other in ids[s]:
                    if (s, t_other) in used:
                        row_costs.append(float('inf'))
                        pass_mask.append(False)
                        continue
                    c, ok = self._pair_cost(aligned_long, self.anchor_sensor, t_anchor, s, t_other)
                    row_costs.append(c)
                    pass_mask.append(ok)

                if not row_costs:
                    continue
                # 也限制: 只在 < soft_gate 中挑
                masked = [c if (p and c < soft_gate) else float('inf')
                          for c, p in zip(row_costs, pass_mask)]
                # Hungarian: 1 row x N cols → 直接选 argmin
                best_idx = int(np.argmin(masked))
                if masked[best_idx] >= 1e5:
                    continue
                best_t = ids[s][best_idx]
                members[s] = best_t
                used.add((s, best_t))
                pair_costs[(self.anchor_sensor, s)] = float(masked[best_idx])
                pair_passes.append(True)

            if len(members) < n:
                # 该 anchor track 没有 N 个 sensor 全部匹配上, 跳过
                # (members 中只填了部分)
                for s, t in members.items():
                    used.discard((s, t))
                continue

            n_pairs = len(pair_costs)
            n_pass = sum(1 for k, v in pair_costs.items() if v < soft_gate)
            pass_ratio = n_pass / max(n_pairs, 1)
            mean_cost = float(np.mean(list(pair_costs.values()))) if pair_costs else float('inf')
            gate_ok = pass_ratio >= self.cluster_min_pair_pass_ratio
            clusters.append(Cluster(
                members=members,
                pair_costs=pair_costs,
                mean_cost=mean_cost,
                gate_pass=gate_ok,
                pair_pass_ratio=pass_ratio,
            ))

        # 按 mean_cost 升序, 选最优; 一条 track 已被用过的 cluster 跳过
        clusters.sort(key=lambda c: c.mean_cost)
        final: List[Cluster] = []
        used_final: Set[Tuple[str, str]] = set()
        for c in clusters:
            if any((s, t) in used_final for s, t in c.members.items()):
                continue
            if not c.gate_pass:
                continue
            for s, t in c.members.items():
                used_final.add((s, t))
            final.append(c)
            if len(final) >= self.max_candidates:
                break
        return final
