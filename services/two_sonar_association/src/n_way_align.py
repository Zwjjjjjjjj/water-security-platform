"""
N-Way Time Alignment

把 N 个传感器 (任意数量) 的本地航迹对齐到公共时间轴。

设计:
  - 输入: dict[sensor_id] -> tracks_df (列: timestamp, track_id, x, y, vx, vy, Pxx, Pyy, measurement_available)
  - 输出: long-format DataFrame, 每一行 = (global_t, sensor_id, local_track_id, state, has_meas, t_local)
  - 对齐策略: 选一个 anchor 传感器 (默认 SONAR_A, 或采样率最高的),
    用它的 timestamp 作为公共时间轴, 其它传感器在 ±max_time_gap_s 内找最近邻。
    输出 schema 兼容 N=2 (此时退化到 A↔B 一对一)。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


@dataclass
class NAlignedRow:
    """N-way 对齐的一行"""
    timestamp: float
    sensor_id: str
    track_id: str
    x: float
    y: float
    vx: float
    vy: float
    Pxx: float
    Pyy: float
    has_meas: bool
    t_local: float       # 实际取到的本地时间
    time_gap_s: float    # |t_local - global_t|


class NWayAligner:
    def __init__(self, max_time_gap_s: float = 0.15,
                 anchor_sensor: Optional[str] = None,
                 dt: float = 0.1):
        self.max_time_gap_s = max_time_gap_s
        self.anchor_sensor = anchor_sensor
        self.dt = dt

    def _state_at(self, track_df: pd.DataFrame, target_t: float) -> Optional[dict]:
        ts = track_df['timestamp'].values
        if len(ts) == 0:
            return None
        # 最近邻
        idx = int(np.argmin(np.abs(ts - target_t)))
        dt = abs(ts[idx] - target_t)
        if dt > self.max_time_gap_s:
            return None
        row = track_df.iloc[idx]
        return {
            'x': float(row['x']), 'y': float(row['y']),
            'vx': float(row['vx']), 'vy': float(row['vy']),
            'Pxx': float(row['Pxx']), 'Pyy': float(row['Pyy']),
            'has_meas': bool(row.get('measurement_available', True)),
            't_local': float(ts[idx]),
            'time_gap_s': float(dt),
        }

    def align(self,
              tracks_by_sensor: Dict[str, pd.DataFrame],
              anchor_sensor: Optional[str] = None) -> pd.DataFrame:
        """
        tracks_by_sensor: {sensor_id: tracks_df}
            tracks_df 必需列: timestamp, track_id, x, y, vx, vy, Pxx, Pyy
                            可选列: measurement_available (默认 True)
        """
        if not tracks_by_sensor:
            return pd.DataFrame()
        anchor = anchor_sensor or self.anchor_sensor or next(iter(tracks_by_sensor))
        if anchor not in tracks_by_sensor:
            anchor = next(iter(tracks_by_sensor))

        anchor_df = tracks_by_sensor[anchor]
        t_min = anchor_df['timestamp'].min()
        t_max = anchor_df['timestamp'].max()
        common_times = np.arange(t_min, t_max, self.dt)

        rows: List[dict] = []
        for t in common_times:
            for sensor_id, df in tracks_by_sensor.items():
                for tid in df['track_id'].unique():
                    track_df = df[df['track_id'] == tid]
                    s = self._state_at(track_df, t)
                    if s is None:
                        continue
                    rows.append({
                        'timestamp': t,
                        'sensor_id': sensor_id,
                        'track_id': tid,
                        'x': s['x'], 'y': s['y'],
                        'vx': s['vx'], 'vy': s['vy'],
                        'Pxx': s['Pxx'], 'Pyy': s['Pyy'],
                        'has_meas': s['has_meas'],
                        't_local': s['t_local'],
                        'time_gap_s': s['time_gap_s'],
                    })

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    # ---------- 兼容老接口 ----------
    def align_two_for_backcompat(self,
                                  tracks_a: pd.DataFrame,
                                  tracks_b: pd.DataFrame,
                                  id_a: str = "A",
                                  id_b: str = "B") -> pd.DataFrame:
        """
        把 A、B 航迹对齐成 "aligned_pairs" 老 schema (与原 align_tracks 输出兼容).
        旧版被 association/fusion 链路依赖, 用 long-format 重建它。
        """
        out = self.align({id_a: tracks_a, id_b: tracks_b}, anchor_sensor=id_a)
        if out.empty:
            return pd.DataFrame()
        # pivot: each (timestamp, track_id) pair across both sensors
        a = out[out['sensor_id'] == id_a].rename(columns={
            'x': 'Ax', 'y': 'Ay', 'vx': 'Avx', 'vy': 'Avy',
            'Pxx': 'APxx', 'Pyy': 'APyy', 'has_meas': 'has_A',
            't_local': 't_a', 'time_gap_s': 'time_gap_a',
        })
        b = out[out['sensor_id'] == id_b].rename(columns={
            'x': 'Bx', 'y': 'By', 'vx': 'Bvx', 'vy': 'Bvy',
            'Pxx': 'BPxx', 'Pyy': 'BPyy', 'has_meas': 'has_B',
            't_local': 't_b', 'time_gap_s': 'time_gap_b',
        })
        merged = a.merge(
            b, on=['timestamp', 'track_id'], suffixes=('_a', '_b')
        )
        # 在 A/B 各自的 track_id 可能不同名, 这里做笛卡尔: cross join on timestamp
        a = a.drop(columns='track_id')
        b = b.drop(columns='track_id')
        merged = a.merge(b, on='timestamp')
        merged = merged.rename(columns={
            'track_id_a': 'track_a_id', 'track_id_b': 'track_b_id'
        })
        return merged
