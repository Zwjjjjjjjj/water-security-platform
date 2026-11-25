"""
N-Way Information Fusion

对 N 个 (sensor, x, P, has_meas, t_meas) 状态做信息形式加和融合。

关键不变量: N=2 时与现有 `track_fusion.CovarianceWeightedFusion.fuse()` 等价。

公式 (信息形式):
    P_xy_f = (sum_i P_i_xy^-1)^-1
    pos_f  = P_xy_f @ (sum_i P_i_xy^-1 @ pos_i)
    v      同上 (用 P_ii 的速度块)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


@dataclass
class NFusedState:
    timestamp: float
    global_id: str
    position: np.ndarray
    velocity: np.ndarray
    covariance: np.ndarray          # 4x4
    n_sensors_used: int
    sensors_used: List[str]
    fusion_mode: str                # 'NONE' / 'SINGLE' / 'N_MEASURED' / 'OUTLIER_REJECTED'
    is_valid: bool = True
    fallback_reason: Optional[str] = None


def _safe_inv(P: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    return np.linalg.inv(P + eps * np.eye(P.shape[0]))


def _extrapolate_state(x: np.ndarray, P: np.ndarray,
                        t_meas: float, t_target: float,
                        Q_base: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """与 track_fusion._extrapolate_state 一致: x += v*dt, P = FPF^T + Q*|dt|"""
    dt = t_target - t_meas
    if abs(dt) < 1e-9:
        return x, P
    F = np.eye(4)
    F[0, 2] = dt
    F[1, 3] = dt
    x_new = x.copy()
    x_new[0] += x[2] * dt
    x_new[1] += x[3] * dt
    P_new = F @ P @ F.T + Q_base * abs(dt)
    return x_new, P_new


class InformationFusionN:
    """
    N 元信息形式融合。
    `states` 列表的每个元素:
        (sensor_id: str, x: np.ndarray (4,), P: np.ndarray (4,4),
         has_meas: bool, t_meas: Optional[float])
    """

    def __init__(self, config: Dict):
        self.eps = config.get('regularization_eps', 1e-6)
        self.outlier_sigma = config.get('n_way_outlier_sigma', 2.5)
        self.min_sensors_for_outlier = config.get('n_way_min_sensors_for_outlier_reject', 3)
        self.extrapolation_enabled = config.get('extrapolation_enabled', True)
        self.max_extrapolation_s = config.get('max_extrapolation_s', 0.06)
        self.position_inflation = config.get('position_inflation', 1.0)
        self.process_noise_pos = config.get('process_noise_pos', 1.0)
        self.process_noise_vel = config.get('process_noise_vel', 0.5)
        self._Q_base = np.diag([
            self.process_noise_pos ** 2, self.process_noise_pos ** 2,
            self.process_noise_vel ** 2, self.process_noise_vel ** 2,
        ])

    def fuse(self, timestamp: float, global_id: str,
             states: Sequence[Tuple]) -> NFusedState:
        measured = [s for s in states if s[3]]   # has_meas True
        if not measured:
            return NFusedState(
                timestamp=timestamp, global_id=global_id,
                position=np.array([np.nan, np.nan]),
                velocity=np.array([np.nan, np.nan]),
                covariance=np.full((4, 4), np.nan),
                n_sensors_used=0, sensors_used=[],
                fusion_mode='NONE', is_valid=False,
                fallback_reason='no_measurements',
            )

        # 单传感器
        if len(measured) == 1:
            sensor_id, x, P, _, t_meas = measured[0]
            return NFusedState(
                timestamp=timestamp, global_id=global_id,
                position=x[:2].copy(), velocity=x[2:4].copy(),
                covariance=P.copy(),
                n_sensors_used=1, sensors_used=[sensor_id],
                fusion_mode=f'SINGLE_{sensor_id}', is_valid=True,
            )

        # N >= 2: 对称外推到 t_mid (中位)
        ts = [s[4] for s in measured if s[4] is not None]
        if self.extrapolation_enabled and len(ts) == len(measured):
            t_mid = float(np.median(ts))
        else:
            t_mid = timestamp

        extrapolated: List[Tuple[str, np.ndarray, np.ndarray]] = []
        for sensor_id, x, P, has_meas, t_meas in measured:
            if (self.extrapolation_enabled and t_meas is not None
                    and abs(t_mid - t_meas) <= self.max_extrapolation_s):
                x_e, P_e = _extrapolate_state(x, P, t_meas, t_mid, self._Q_base)
            else:
                x_e, P_e = x, P
            extrapolated.append((sensor_id, x_e, P_e))

        # Outlier reject: 仅当 >= min_sensors_for_outlier 时启用
        rejected: List[str] = []
        if len(extrapolated) >= self.min_sensors_for_outlier:
            kept, rejected = self._outlier_reject(extrapolated)
        else:
            kept = extrapolated

        # 信息加和
        if len(kept) == 1:
            sensor_id, x, P = kept[0]
            return NFusedState(
                timestamp=timestamp, global_id=global_id,
                position=x[:2].copy(), velocity=x[2:4].copy(),
                covariance=P.copy(),
                n_sensors_used=1, sensors_used=[sensor_id],
                fusion_mode='OUTLIER_REJECTED', is_valid=True,
                fallback_reason='n_way_outlier_reject',
            )

        info_pos = np.zeros((2, 2))
        pos_sum = np.zeros(2)
        info_vel = np.zeros((2, 2))
        vel_sum = np.zeros(2)
        for sensor_id, x, P in kept:
            P_xy = P[:2, :2] + self.eps * np.eye(2)
            P_v = P[2:4, 2:4] + self.eps * np.eye(2)
            try:
                info_pos += _safe_inv(P_xy, self.eps)
                pos_sum += _safe_inv(P_xy, self.eps) @ x[:2]
                info_vel += _safe_inv(P_v, self.eps)
                vel_sum += _safe_inv(P_v, self.eps) @ x[2:4]
            except np.linalg.LinAlgError:
                continue

        try:
            P_xy_f = np.linalg.inv(info_pos)
            pos_f = P_xy_f @ pos_sum
        except np.linalg.LinAlgError:
            # 退化: 取 trace 最小的
            best = min(kept, key=lambda s: np.trace(s[2]))
            return NFusedState(
                timestamp=timestamp, global_id=global_id,
                position=best[1][:2].copy(), velocity=best[1][2:4].copy(),
                covariance=best[2].copy(),
                n_sensors_used=1, sensors_used=[best[0]],
                fusion_mode='SINGLE_FALLBACK', is_valid=True,
                fallback_reason='position_fusion_singular',
            )

        try:
            P_v_f = np.linalg.inv(info_vel)
            vel_f = P_v_f @ vel_sum
        except np.linalg.LinAlgError:
            P_v_f = np.zeros((2, 2))
            vel_f = np.mean([s[1][2:4] for s in kept], axis=0)

        P_f = np.zeros((4, 4))
        P_f[:2, :2] = P_xy_f
        P_f[2:4, 2:4] = P_v_f

        return NFusedState(
            timestamp=timestamp, global_id=global_id,
            position=pos_f, velocity=vel_f, covariance=P_f,
            n_sensors_used=len(kept),
            sensors_used=[s[0] for s in kept],
            fusion_mode='N_MEASURED' if not rejected else 'N_MEASURED_REJECTED',
            is_valid=True,
            fallback_reason=(','.join(rejected) if rejected else None),
        )

    def _outlier_reject(self,
                          extrapolated: List[Tuple[str, np.ndarray, np.ndarray]]
                          ) -> Tuple[List[Tuple[str, np.ndarray, np.ndarray]], List[str]]:
        """
        对每条记录, 把它当作"待评估", 用其它 N-1 条做信息融合, 计算 |x - x_f| / σ_f;
        如果 > self.outlier_sigma, 标记为 outlier。
        """
        if len(extrapolated) < 3:
            return extrapolated, []
        # 第一遍: 全部融合得到 σ_f
        info_pos_full = sum(_safe_inv(s[2][:2, :2] + self.eps * np.eye(2)) for s in extrapolated)
        pos_sum_full = sum(
            _safe_inv(s[2][:2, :2] + self.eps * np.eye(2)) @ s[1][:2] for s in extrapolated
        )
        P_xy_full = np.linalg.inv(info_pos_full)
        pos_full = P_xy_full @ pos_sum_full
        sigma_full = np.sqrt(np.trace(P_xy_full))

        kept: List[Tuple[str, np.ndarray, np.ndarray]] = []
        rejected: List[str] = []
        for sensor_id, x, P in extrapolated:
            d = np.linalg.norm(x[:2] - pos_full)
            if d > self.outlier_sigma * sigma_full:
                rejected.append(sensor_id)
            else:
                kept.append((sensor_id, x, P))
        # 至少保留 2 个
        if len(kept) < 2:
            return extrapolated, []
        return kept, rejected
