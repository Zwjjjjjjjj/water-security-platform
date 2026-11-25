"""
Track Fusion - Cross-Sonar Covariance-Weighted Position Fusion
跨声纳协方差加权位置融合

输入：两个 Local Track 的 Kalman 状态 + 协方差 + confirmed 关联对
输出：单一 Global Fused Track（位置/速度/协方差/置信度）
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from pathlib import Path
import sys
import json
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_FUSION_CONFIG, OUTPUT_ROOT


@dataclass
class FusedState:
    """融合后的单帧状态"""
    timestamp: float
    global_id: str
    position: np.ndarray            # [x, y]
    velocity: np.ndarray            # [vx, vy]
    covariance: np.ndarray          # 4x4
    n_sensors_used: int             # 1 or 2
    sensors_used: List[str]         # ['SONAR_A', 'SONAR_B'] 等
    fusion_mode: str                # 'A_ONLY' / 'B_ONLY' / 'BOTH_MEASURED' / 'BOTH_PREDICTED' / 'FALLBACK' / 'NONE'
    is_valid: bool = True
    fallback_reason: Optional[str] = None


class CovarianceWeightedFusion:
    """
    信息形式协方差加权融合器

    位置:  P_xy_f = (P_xy_a^-1 + P_xy_b^-1)^-1
           pos_f   = P_xy_f @ (P_xy_a^-1 @ pos_a + P_xy_b^-1 @ pos_b)
    速度:  同上

    [2026-10-09] 状态外推增强 (1.1 时间对齐精度提升)
        在 _extract_state 阶段对每个 local track 的最近一帧做外推
        x(t_target) = x(t_meas) + v * (t_target - t_meas)
        P(t_target) = F @ P(t_meas) @ F^T + Q_base * |dt|
        其中 dt = t_target - t_meas ∈ [-0.12, +0.12]s
    """

    def __init__(self, config: Dict):
        self.eps = config.get('regularization_eps', 1e-6)
        self.min_det_threshold = config.get('min_determinant', 1e-8)
        self.max_dt_for_fusion_s = config.get('max_dt_for_fusion_s', 0.5)
        self.innovation_outlier_sigma = config.get('innovation_outlier_sigma', 3.0)
        self.fallback_strategy = config.get('fallback_strategy', 'lower_trace')
        # 状态外推参数 (与 LocalTracker.KalmanFilter 保持一致)
        self.process_noise_pos = config.get('process_noise_pos', 1.0)
        self.process_noise_vel = config.get('process_noise_vel', 0.5)
        # 是否启用外推 (默认 True)
        self.extrapolation_enabled = config.get('extrapolation_enabled', True)
        # 外推最大时长 (s) - 超过则不外推 (退化为"取最近帧")
        # 实验: 0.12s 外推让 P 增大超过位置修正收益，0.06s 是平衡点
        self.max_extrapolation_s = config.get('max_extrapolation_s', 0.06)
        # 状态外推 Q_base (用于外推协方差增大)
        self._Q_base = np.diag([
            self.process_noise_pos ** 2,
            self.process_noise_pos ** 2,
            self.process_noise_vel ** 2,
            self.process_noise_vel ** 2
        ])
        # [2026-10-09] 1.2 协方差膨胀因子 (Covariance Inflation)
        # 根因: measurement_noise_pos=2.0 配置偏小, 真实声纳测量噪声约 10px
        # 导致 Kalman 自报 P 比真实小 ~44x, outlier reject / fusion 失效
        # inflation_factor = E[|e|²] / E[P_xx + P_yy] ≈ 44x (case1 实测)
        # 应用:
        #   1. outlier check σ_combined 门限: 真实 σ * σ_sigma ≈ 10px * 3 = 30px
        #   2. fusion gain 在 inflation 后才能真实反映 A/B 独立性
        #   3. 但 inflation 也会降低 outlier rejection 敏感度 (门限变宽)
        # 策略: inflation 仅用于 outlier check, 不改变融合本身的 P (保持数值稳定)
        self.position_inflation = config.get('position_inflation', 1.0)
        # [2026-10-09] 1.3 自适应 outlier 门限
        # 经验最优: outlier_sigma_threshold = 1.5 - 2.0 (σ_K 单位)
        # 默认 3.0 时门限 4.6px 太宽, 很多 outlier 没被检测
        # 设小一些: 1.5 → 门限 2.3px, 让更多帧触发 outlier reject
        # 配合 fallback_reason 记录, 后续可调
        self.outlier_sigma_threshold = config.get('outlier_sigma_threshold', 3.0)
        # 是否在 outlier reject 时选 min(P) 的一边 (替代默认的 min(trace))
        # 实验: 这能改善 0.045 RMSE
        self.outlier_pick_min_p = config.get('outlier_pick_min_p', False)

    def _extrapolate_state(self, x: np.ndarray, P: np.ndarray,
                           t_meas: float, t_target: float) -> Tuple[np.ndarray, np.ndarray, float]:
        """
        把 t_meas 时刻的 Kalman 状态外推到 t_target 时刻。

        位置: x_new = x + v * dt  (恒速假设)
        协方差: P_new = F @ P @ F^T + Q_base * |dt|
                Q_base 与 LocalTracker.KalmanFilter 内部一致 (Q_base * dt 缩放)

        [2026-10-09] 改进: 增加 max_extrapolation_s 阈值
        - 短时外推 (<= 0.06s) 有效，期望改善 RMSE
        - 长时外推 (> 0.06s) 协方差增加超过测量噪声改善的收益，回退到不外推
          (实测 0.12s 外推导致 outlier rejection 飙升, P 增大的负效应 > 位置修正的正效应)

        参数:
            x: t_meas 时刻的 4D 状态 [x, y, vx, vy]
            P: t_meas 时刻的 4x4 协方差
            t_meas: 测量时间戳
            t_target: 目标融合时刻

        返回:
            (x_new, P_new, dt)
            - dt > max_extrapolation_s 时返回 (x, P, dt) 不外推
        """
        dt = float(t_target) - float(t_meas)
        if abs(dt) > self.max_extrapolation_s:
            return x, P, dt
        # 状态外推 (恒速)
        x_new = x.copy()
        x_new[0] = x[0] + x[2] * dt
        x_new[1] = x[1] + x[3] * dt
        # 速度不变
        # 协方差外推
        F = np.eye(4)
        F[0, 2] = dt
        F[1, 3] = dt
        # 过程噪声增量 (与 Kalman 内部 Q_base * dt 一致)
        Q = self._Q_base * abs(dt)
        P_new = F @ P @ F.T + Q
        return x_new, P_new, dt

    def fuse(self, timestamp: float, global_id: str,
             state_a: Optional[Tuple],
             state_b: Optional[Tuple]) -> FusedState:
        """
        融合一帧

        参数:
            timestamp: 当前时间
            global_id: 全局ID
            state_a: (x[4], P[4x4], has_measurement[, t_meas]) 或 None
                     t_meas 可选 (2026-10-09 增加): 用于对称外推到融合中点
            state_b: 同上

        [2026-10-09] 1.1 状态外推增强
            关键洞察: 单边外推会拉偏 A_ONLY/B_ONLY 帧 (上一版退化 -0.085 RMSE)
            修复: 用两侧 t_meas 的中点作为融合时刻 (对称外推)
            - 有 t_meas: t_mid = (t_a + t_b) / 2, A 外推 dt_a = t_mid - t_a,
              B 外推 dt_b = t_mid - t_b, dt_a + dt_b = 0 (对称)
            - 无 t_meas (旧调用方): 回退到原行为 (t = timestamp, dt=0)
            - 仍然受 max_extrapolation_s 约束
        """
        # 解构 state, 兼容 3-tuple 和 4-tuple
        def unpack(state):
            if state is None:
                return None
            if len(state) == 4:
                return state  # (x, P, meas, t_meas)
            else:  # len == 3
                return (*state, None)  # 补 None 给 t_meas

        sa = unpack(state_a)
        sb = unpack(state_b)

        has_a = sa is not None
        has_b = sb is not None

        if not has_a and not has_b:
            return FusedState(
                timestamp=timestamp, global_id=global_id,
                position=np.array([np.nan, np.nan]),
                velocity=np.array([np.nan, np.nan]),
                covariance=np.full((4, 4), np.nan),
                n_sensors_used=0, sensors_used=[],
                fusion_mode='NONE', is_valid=False,
                fallback_reason='no_local_tracks'
            )

        if has_a and not has_b:
            x, P, _, _ = sa
            return FusedState(
                timestamp=timestamp, global_id=global_id,
                position=x[:2].copy(), velocity=x[2:4].copy(),
                covariance=P.copy(),
                n_sensors_used=1, sensors_used=['SONAR_A'],
                fusion_mode='A_ONLY', is_valid=True
            )

        if has_b and not has_a:
            x, P, _, _ = sb
            return FusedState(
                timestamp=timestamp, global_id=global_id,
                position=x[:2].copy(), velocity=x[2:4].copy(),
                covariance=P.copy(),
                n_sensors_used=1, sensors_used=['SONAR_B'],
                fusion_mode='B_ONLY', is_valid=True
            )

        # 两边都有
        x_a_raw, P_a_raw, meas_a, t_a = sa
        x_b_raw, P_b_raw, meas_b, t_b = sb

        # 1) 对称外推到融合中点
        #    关键: dt_a + dt_b = 0  →  一边外推 +dt, 另一边 -dt
        #    效果: A 和 B 在同一个 t_mid 时刻比较, 时间错位消除
        #    [2026-10-09 v3] 安全门限: 仅当 d_pos < 3σ 时外推, 避免 B 的 outlier
        #    把 A 拉偏 (实测 0.12s 外推让 RMSE 退化 0.085)
        if (self.extrapolation_enabled and
            t_a is not None and t_b is not None):
            t_mid = 0.5 * (t_a + t_b)
            dt_a = t_mid - t_a  # 范围 [-0.06, +0.06]
            dt_b = t_mid - t_b  # = -dt_a
            if abs(dt_a) <= self.max_extrapolation_s:
                # v3: 用未外推的 A、B 位置做粗 outlier 检查
                d_pos_raw = x_a_raw[:2] - x_b_raw[:2]
                S_raw = P_a_raw[:2, :2] + P_b_raw[:2, :2]
                try:
                    d2_raw = float(d_pos_raw @ np.linalg.solve(S_raw, d_pos_raw))
                    sigma_raw = np.sqrt(max(d2_raw, 0.0))
                except np.linalg.LinAlgError:
                    sigma_raw = float('inf')
                # 安全的: σ < 2 才外推, 否则 A、B 中至少一边有 outlier
                if sigma_raw <= 2.0:
                    x_a, P_a, _ = self._extrapolate_state(x_a_raw, P_a_raw, t_a, t_mid)
                    x_b, P_b, _ = self._extrapolate_state(x_b_raw, P_b_raw, t_b, t_mid)
                else:
                    # A 或 B 有 outlier, 不外推 (保持原状态)
                    x_a, P_a, x_b, P_b = x_a_raw, P_a_raw, x_b_raw, P_b_raw
            else:
                # dt 太大, 不外推
                x_a, P_a, x_b, P_b = x_a_raw, P_a_raw, x_b_raw, P_b_raw
        else:
            # 旧调用方, 不外推
            x_a, P_a, x_b, P_b = x_a_raw, P_a_raw, x_b_raw, P_b_raw

        # 2) 创新野值检测（仅当两边都有 measurement）
        # [2026-10-09] 1.2: 用膨胀后的 P_xy 做 outlier check
        #    inflation=44x 时, 门限 3.0 * sqrt(inflation * mean_P) ≈ 3.0 * 10 = 30px
        #    这比原始 3px 门限宽松得多 (原始 σ_combined 几乎总是 < 3px)
        #    宽松的 outlier check 允许更多 BOTH_MEASURED 帧参与融合
        # [2026-10-09] 1.3: 自适应门限 outlier_sigma_threshold
        #    默认 3.0 -> 门限 4.6px, 太少 outlier 触发
        #    设 1.5-2.0 -> 门限 2.3-3.1px, 触发更多 outlier
        if meas_a and meas_b:
            d_pos = x_a[:2] - x_b[:2]
            # 用膨胀后的 P_xy 做 outlier check (1.2 inflation 生效时)
            P_a_xy_check = P_a[:2, :2] * self.position_inflation + self.eps * np.eye(2)
            P_b_xy_check = P_b[:2, :2] * self.position_inflation + self.eps * np.eye(2)
            S = P_a_xy_check + P_b_xy_check
            try:
                d2 = float(d_pos @ np.linalg.solve(S, d_pos))
                sigma_combined = np.sqrt(max(d2, 0.0))
                # 自适应门限: 用 outlier_sigma_threshold (单位: σ_K 标准化)
                # σ_K = sqrt(P_A + P_B), 默认约 1.5
                # 门限 = outlier_sigma_threshold (默认 3.0) -> 4.5px
                threshold = self.outlier_sigma_threshold
                if sigma_combined > threshold:
                    # 一边有野值 → 用 P 较小的一边 (1.3 outlier_pick_min_p)
                    if self.outlier_pick_min_p:
                        if P_a[:2,:2].trace() < P_b[:2,:2].trace():
                            chosen = ('A_ONLY', x_a, P_a, 'outlier_rejected_B_pick_min_p')
                        else:
                            chosen = ('B_ONLY', x_b, P_b, 'outlier_rejected_A_pick_min_p')
                    else:
                        if np.trace(P_a[:2, :2]) < np.trace(P_b[:2, :2]):
                            chosen = ('A_ONLY', x_a, P_a, 'outlier_rejected_B')
                        else:
                            chosen = ('B_ONLY', x_b, P_b, 'outlier_rejected_A')
                    return FusedState(
                        timestamp=timestamp, global_id=global_id,
                        position=chosen[1][:2].copy(),
                        velocity=chosen[1][2:4].copy(),
                        covariance=chosen[2].copy(),
                        n_sensors_used=1,
                        sensors_used=[chosen[0].split('_')[0]],
                        fusion_mode=chosen[0],
                        is_valid=True,
                        fallback_reason=chosen[3]
                    )
            except np.linalg.LinAlgError:
                pass  # 退化到下一步

        # 3) 协方差正则化
        P_a_xy = P_a[:2, :2] + self.eps * np.eye(2)
        P_b_xy = P_b[:2, :2] + self.eps * np.eye(2)
        P_a_v  = P_a[2:4, 2:4] + self.eps * np.eye(2)
        P_b_v  = P_b[2:4, 2:4] + self.eps * np.eye(2)

        # 4) 行列式检查
        if (np.linalg.det(P_a_xy) < self.min_det_threshold or
            np.linalg.det(P_b_xy) < self.min_det_threshold or
            np.linalg.det(P_a_v)  < self.min_det_threshold or
            np.linalg.det(P_b_v)  < self.min_det_threshold):
            # 退化协方差 → 用 trace 较小的
            if np.trace(P_a) < np.trace(P_b):
                chosen = ('A_ONLY', x_a, P_a, 'degenerate_covariance')
            else:
                chosen = ('B_ONLY', x_b, P_b, 'degenerate_covariance')
            return FusedState(
                timestamp=timestamp, global_id=global_id,
                position=chosen[1][:2].copy(),
                velocity=chosen[1][2:4].copy(),
                covariance=chosen[2].copy(),
                n_sensors_used=1,
                sensors_used=[chosen[0].split('_')[0]],
                fusion_mode=chosen[0],
                is_valid=True,
                fallback_reason=chosen[3]
            )

        # 5) 信息矩阵融合
        #    位置
        try:
            P_xy_f = np.linalg.inv(
                np.linalg.inv(P_a_xy) + np.linalg.inv(P_b_xy)
            )
            pos_f = P_xy_f @ (
                np.linalg.solve(P_a_xy, x_a[:2]) +
                np.linalg.solve(P_b_xy, x_b[:2])
            )
        except np.linalg.LinAlgError:
            if np.trace(P_a[:2, :2]) < np.trace(P_b[:2, :2]):
                chosen = ('A_ONLY', x_a, P_a, 'position_fusion_singular')
            else:
                chosen = ('B_ONLY', x_b, P_b, 'position_fusion_singular')
            return FusedState(
                timestamp=timestamp, global_id=global_id,
                position=chosen[1][:2].copy(),
                velocity=chosen[1][2:4].copy(),
                covariance=chosen[2].copy(),
                n_sensors_used=1,
                sensors_used=[chosen[0].split('_')[0]],
                fusion_mode=chosen[0],
                is_valid=True,
                fallback_reason=chosen[3]
            )

        #    速度
        try:
            P_v_f = np.linalg.inv(
                np.linalg.inv(P_a_v) + np.linalg.inv(P_b_v)
            )
            vel_f = P_v_f @ (
                np.linalg.solve(P_a_v, x_a[2:4]) +
                np.linalg.solve(P_b_v, x_b[2:4])
            )
        except np.linalg.LinAlgError:
            P_v_f = np.zeros((2, 2))
            vel_f = 0.5 * (x_a[2:4] + x_b[2:4])

        # 6) 组装 4x4
        P_f = np.zeros((4, 4))
        P_f[:2, :2] = P_xy_f
        P_f[2:4, 2:4] = P_v_f
        # 位置-速度互相关：保守置 0（无跨通道相关先验）

        mode = 'BOTH_MEASURED' if (meas_a and meas_b) else 'BOTH_PREDICTED'

        return FusedState(
            timestamp=timestamp, global_id=global_id,
            position=pos_f, velocity=vel_f,
            covariance=P_f,
            n_sensors_used=2, sensors_used=['SONAR_A', 'SONAR_B'],
            fusion_mode=mode, is_valid=True
        )


class TrackFusionEngine:
    """
    Track Fusion 引擎：消费 Local Track 的 Kalman 状态
    + Global Track Manager 的 local→global 映射
    → 输出 fused_global_tracks.csv
    """

    def __init__(self, config: Dict):
        self.fusion_config = config.get('fusion', {})
        self.fuser = CovarianceWeightedFusion(self.fusion_config)
        self.fused_tracks: Dict[str, Dict[float, FusedState]] = {}

    def ingest(self,
               global_to_locals: Dict[str, Dict[str, str]],
               local_track_states: Dict[str, pd.DataFrame],
               local_to_global_sensor: Dict[str, str]) -> pd.DataFrame:
        """
        一次性融合所有 global track

        参数:
            global_to_locals: {global_id: {sensor_id: local_track_id}}
            local_track_states: {local_track_id: DataFrame from LocalTrack.to_dataframe()}

        返回:
            fused_global_tracks DataFrame
        """
        all_records = []

        for gid, sensor_to_local in global_to_locals.items():
            local_dfs = {}
            for sensor, local_id in sensor_to_local.items():
                if local_id in local_track_states:
                    local_dfs[sensor] = local_track_states[local_id]

            if not local_dfs:
                continue

            # 收集所有时间戳（union）
            all_ts = set()
            for df in local_dfs.values():
                all_ts.update(df['timestamp'].tolist())
            all_ts = sorted(all_ts)

            self.fused_tracks[gid] = {}

            for t in all_ts:
                state_a = self._extract_state(local_dfs.get('SONAR_A'), t)
                state_b = self._extract_state(local_dfs.get('SONAR_B'), t)

                fused = self.fuser.fuse(t, gid, state_a, state_b)
                self.fused_tracks[gid][t] = fused

                all_records.append(self._to_record(fused))

        return pd.DataFrame(all_records)

    def _extract_state(self, df: Optional[pd.DataFrame], t: float,
                        time_tolerance_s: float = 0.12) -> Optional[Tuple]:
        """从 local track DataFrame 提取 (state, P, has_meas, t_meas) at time t

        策略：取 df 中 timestamp 与 t 最近的行（前提是差值 < time_tolerance_s），
        如果启用了状态外推 (CovarianceWeightedFusion.extrapolation_enabled)，
        则把该行外推到目标时间 t。
        这是必要的——因为两个声纳的采样率不同（5Hz vs 4.7Hz），
        它们的 timestamps 几乎不会完全相等。

        [2026-10-09] 改进：返回 4-tuple (x, P, has_meas, t_meas)
        - t_meas 用于 fuse() 判断 dt_a vs dt_b 的对称性
        - fuse() 根据两侧 dt 的对称性决定是否外推 (避免单边外推拉偏)
        """
        if df is None or len(df) == 0:
            return None
        # 找最近的一行
        diffs = (df['timestamp'] - t).abs()
        idx = int(diffs.idxmin())
        if diffs.iloc[idx] > time_tolerance_s:
            return None
        r = df.iloc[idx]
        if pd.isna(r['x']) or pd.isna(r['Pxx']):
            return None
        x = np.array([r['x'], r['y'], r['vx'], r['vy']], dtype=float)
        P = np.array([
            [r['Pxx'], r['Pxy'] if not pd.isna(r['Pxy']) else 0.0, 0, 0],
            [r['Pxy'] if not pd.isna(r['Pxy']) else 0.0, r['Pyy'], 0, 0],
            [0, 0, r['Pvxvx'] if not pd.isna(r['Pvxvx']) else 0.0, 0],
            [0, 0, 0, r['Pvyvy'] if not pd.isna(r['Pvyvy']) else 0.0],
        ], dtype=float)
        has_meas = bool(r.get('measurement_available', False))
        t_meas = float(r['timestamp'])
        # 状态外推 (旧逻辑保留, 但 fuse() 会再用 dt_a/dt_b 对称性二次判断)
        if self.fuser.extrapolation_enabled:
            x, P, _dt = self.fuser._extrapolate_state(x, P, t_meas, t)
        return (x, P, has_meas, t_meas)

    def _to_record(self, fs: FusedState) -> Dict:
        return {
            'timestamp': fs.timestamp,
            'global_id': fs.global_id,
            'x_fused': fs.position[0],
            'y_fused': fs.position[1],
            'vx_fused': fs.velocity[0],
            'vy_fused': fs.velocity[1],
            'Pxx_fused': fs.covariance[0, 0],
            'Pxy_fused': fs.covariance[0, 1],
            'Pyy_fused': fs.covariance[1, 1],
            'Pvxvx_fused': fs.covariance[2, 2],
            'Pvyvy_fused': fs.covariance[3, 3],
            'n_sensors_used': fs.n_sensors_used,
            'sensors_used': ';'.join(fs.sensors_used),
            'fusion_mode': fs.fusion_mode,
            'is_valid': fs.is_valid,
            'fallback_reason': fs.fallback_reason,
            'pos_trace': float(fs.covariance[0, 0] + fs.covariance[1, 1])
        }


def evaluate_fusion_accuracy(fused_df: pd.DataFrame,
                              reference_df: pd.DataFrame,
                              ref_id: str = 'T1',
                              time_tolerance_s: float = 0.1) -> Dict:
    """
    评估：融合位置精度 vs 参考航迹

    参数:
        fused_df: 融合输出 (含 x_fused, y_fused, timestamp, is_valid, global_id)
        reference_df: 参考航迹 (含 timestamp, x_ref, y_ref)
        ref_id: 用于日志标识
        time_tolerance_s: 时间匹配容差
    """
    if len(fused_df) == 0 or len(reference_df) == 0:
        return {
            'ref_id': ref_id,
            'rmse_position_m': np.nan,
            'mean_position_error_m': np.nan,
            'max_position_error_m': np.nan,
            'median_position_error_m': np.nan,
            'coverage': 0.0,
            'n_matched': 0
        }

    # 过滤无效
    valid = fused_df[fused_df['is_valid'] == True].copy()  # noqa: E712
    if len(valid) == 0:
        return {
            'ref_id': ref_id,
            'rmse_position_m': np.nan,
            'mean_position_error_m': np.nan,
            'max_position_error_m': np.nan,
            'median_position_error_m': np.nan,
            'coverage': 0.0,
            'n_matched': 0
        }

    ref_t = reference_df['timestamp'].values
    ref_x = reference_df['x_ref'].values
    ref_y = reference_df['y_ref'].values

    errors = []
    matched = 0
    for _, r in valid.iterrows():
        t = r['timestamp']
        if pd.isna(r['x_fused']):
            continue
        idx = int(np.argmin(np.abs(ref_t - t)))
        if abs(ref_t[idx] - t) > time_tolerance_s:
            continue
        dx = r['x_fused'] - ref_x[idx]
        dy = r['y_fused'] - ref_y[idx]
        errors.append(np.sqrt(dx*dx + dy*dy))
        matched += 1

    if not errors:
        return {
            'ref_id': ref_id,
            'rmse_position_m': np.nan,
            'mean_position_error_m': np.nan,
            'max_position_error_m': np.nan,
            'median_position_error_m': np.nan,
            'coverage': 0.0,
            'n_matched': 0
        }

    errs = np.array(errors)
    return {
        'ref_id': ref_id,
        'rmse_position_m': float(np.sqrt(np.mean(errs**2))),
        'mean_position_error_m': float(np.mean(errs)),
        'max_position_error_m': float(np.max(errs)),
        'median_position_error_m': float(np.median(errs)),
        'coverage': matched / len(reference_df),
        'n_matched': int(matched)
    }


def evaluate_single_sonar_accuracy(sonar_tracks_df: pd.DataFrame,
                                    reference_df: pd.DataFrame,
                                    ref_id: str = 'T1',
                                    time_tolerance_s: float = 0.1) -> Dict:
    """
    单声纳精度评估（用于对比融合增益）
    """
    if len(sonar_tracks_df) == 0 or len(reference_df) == 0:
        return {'ref_id': ref_id, 'rmse_position_m': np.nan, 'n_matched': 0}

    ref_t = reference_df['timestamp'].values
    ref_x = reference_df['x_ref'].values
    ref_y = reference_df['y_ref'].values

    errors = []
    matched = 0
    for _, r in sonar_tracks_df.iterrows():
        t = r['timestamp']
        if pd.isna(r['x']):
            continue
        idx = int(np.argmin(np.abs(ref_t - t)))
        if abs(ref_t[idx] - t) > time_tolerance_s:
            continue
        dx = r['x'] - ref_x[idx]
        dy = r['y'] - ref_y[idx]
        errors.append(np.sqrt(dx*dx + dy*dy))
        matched += 1

    if not errors:
        return {'ref_id': ref_id, 'rmse_position_m': np.nan, 'n_matched': 0}

    return {
        'ref_id': ref_id,
        'rmse_position_m': float(np.sqrt(np.mean(np.array(errors)**2))),
        'n_matched': int(matched)
    }


def run_case(case_dir: Path, config: Dict) -> Dict:
    """
    跑一个 case 的 Track Fusion

    返回:
        {
            'case': 'case1_single',
            'fused_csv': '...',
            'per_target_metrics': [...],
            'overall_metrics': {...},
            'fusion_gain': ...,
            'fallback_rate': ...,
            'outlier_rejection_rate': ...
        }
    """
    case_dir = Path(case_dir)

    # 1) 加载
    local_to_global_df = pd.read_csv(case_dir / 'local_to_global.csv')
    sonar_a_tracks = pd.read_csv(case_dir / 'sonar_A_tracks.csv')
    sonar_b_tracks = pd.read_csv(case_dir / 'sonar_B_tracks.csv')

    # 2) 推断每个 local_track_id 属于哪个 sensor
    local_to_sensor: Dict[str, str] = {}
    for _, r in local_to_global_df.iterrows():
        lid = r['local_track_id']
        if lid.startswith('A_'):
            local_to_sensor[lid] = 'SONAR_A'
        elif lid.startswith('B_'):
            local_to_sensor[lid] = 'SONAR_B'

    # 3) 索引 local track states
    local_states: Dict[str, pd.DataFrame] = {}
    for tid, grp in sonar_a_tracks.groupby('track_id'):
        local_states[tid] = grp.sort_values('timestamp').reset_index(drop=True)
    for tid, grp in sonar_b_tracks.groupby('track_id'):
        local_states[tid] = grp.sort_values('timestamp').reset_index(drop=True)

    # 4) 构造 global → {sensor → local_id}
    global_to_locals: Dict[str, Dict[str, str]] = {}
    for _, r in local_to_global_df.iterrows():
        gid = r['global_id']
        lid = r['local_track_id']
        sensor = local_to_sensor[lid]
        global_to_locals.setdefault(gid, {})[sensor] = lid

    # 5) 融合
    engine = TrackFusionEngine(config)
    fused_df = engine.ingest(global_to_locals, local_states, local_to_sensor)

    # 6) 保存
    out_csv = case_dir / 'fused_global_tracks.csv'
    fused_df.to_csv(out_csv, index=False)
    print(f"  [{case_dir.name}] saved {len(fused_df)} fused states → {out_csv.name}")

    # 7) 评估（找所有 reference_track_T*.csv）
    ref_files = sorted(case_dir.glob('reference_track_T*.csv'))
    per_target_metrics = []

    for ref_path in ref_files:
        ref = pd.read_csv(ref_path)
        # ref file 是 reference_track_T1.csv → T1
        ref_id = ref_path.stem.replace('reference_track_', '')

        # 找对应的 global_id
        # T1 对应 A_T1 → G001, B_T1 → G001
        # 通过 local_to_global 找到第一个 local_track_id 包含此 ref_id 的 global_id
        target_gid = None
        for _, r in local_to_global_df.iterrows():
            if ref_id in r['local_track_id']:
                target_gid = r['global_id']
                break

        if target_gid is None:
            continue

        # 取出该 global 的融合输出
        gid_fused = fused_df[fused_df['global_id'] == target_gid].copy()

        # 评估融合
        fusion_metrics = evaluate_fusion_accuracy(gid_fused, ref, ref_id=ref_id)

        # 评估单声纳
        a_lid = None
        b_lid = None
        for _, r in local_to_global_df.iterrows():
            if r['global_id'] == target_gid:
                if r['local_track_id'].startswith('A_'):
                    a_lid = r['local_track_id']
                elif r['local_track_id'].startswith('B_'):
                    b_lid = r['local_track_id']

        a_metrics = None
        b_metrics = None
        if a_lid and a_lid in local_states:
            a_metrics = evaluate_single_sonar_accuracy(local_states[a_lid], ref, ref_id=ref_id)
        if b_lid and b_lid in local_states:
            b_metrics = evaluate_single_sonar_accuracy(local_states[b_lid], ref, ref_id=ref_id)

        # 计算 fusion gain
        single_rmses = []
        if a_metrics and not np.isnan(a_metrics['rmse_position_m']):
            single_rmses.append(a_metrics['rmse_position_m'])
        if b_metrics and not np.isnan(b_metrics['rmse_position_m']):
            single_rmses.append(b_metrics['rmse_position_m'])
        if single_rmses and not np.isnan(fusion_metrics['rmse_position_m']):
            best_single = min(single_rmses)
            fusion_gain = 1.0 - fusion_metrics['rmse_position_m'] / best_single
        else:
            fusion_gain = np.nan
        fusion_metrics['rmse_position_A_m'] = a_metrics['rmse_position_m'] if a_metrics else np.nan
        fusion_metrics['rmse_position_B_m'] = b_metrics['rmse_position_m'] if b_metrics else np.nan
        fusion_metrics['fusion_gain'] = float(fusion_gain) if not np.isnan(fusion_gain) else np.nan

        per_target_metrics.append(fusion_metrics)

    # 8) 整体统计
    if len(per_target_metrics) > 0:
        overall = {
            'case': case_dir.name,
            'n_targets': len(per_target_metrics),
            'mean_rmse_fused_m': float(np.nanmean([m['rmse_position_m'] for m in per_target_metrics])),
            'mean_rmse_A_m': float(np.nanmean([m['rmse_position_A_m'] for m in per_target_metrics])),
            'mean_rmse_B_m': float(np.nanmean([m['rmse_position_B_m'] for m in per_target_metrics])),
            'mean_fusion_gain': float(np.nanmean([m['fusion_gain'] for m in per_target_metrics])),
            'mean_coverage': float(np.nanmean([m['coverage'] for m in per_target_metrics])),
        }
    else:
        overall = {
            'case': case_dir.name,
            'n_targets': 0,
            'mean_rmse_fused_m': np.nan,
            'mean_rmse_A_m': np.nan,
            'mean_rmse_B_m': np.nan,
            'mean_fusion_gain': np.nan,
            'mean_coverage': 0.0,
        }

    # 8.5) 理论协方差改善：P_fused < min(P_A, P_B) 的比例
    #    这是融合算法**数学上**正确的验证——独立于真实测量误差
    both_df = fused_df[fused_df['fusion_mode'].isin(['BOTH_MEASURED', 'BOTH_PREDICTED'])].copy()
    if len(both_df) > 0:
        # 重新加载 local track 获取 A、B 协方差
        trace_improved = 0
        n_compared = 0
        for _, r in both_df.iterrows():
            t = r['timestamp']
            gid = r['global_id']
            # 找对应的 local track states
            a_state = engine._extract_state(
                local_states.get(global_to_locals.get(gid, {}).get('SONAR_A')), t
            )
            b_state = engine._extract_state(
                local_states.get(global_to_locals.get(gid, {}).get('SONAR_B')), t
            )
            if a_state is None or b_state is None:
                continue
            pa_trace = a_state[1][0, 0] + a_state[1][1, 1]
            pb_trace = b_state[1][0, 0] + b_state[1][1, 1]
            pf_trace = r['pos_trace']
            if pf_trace < min(pa_trace, pb_trace):
                trace_improved += 1
            n_compared += 1
        overall['theoretical_cov_improved_rate'] = (
            float(trace_improved / n_compared) if n_compared > 0 else 0.0
        )
        overall['mean_fused_pos_trace'] = float(both_df['pos_trace'].mean())
    else:
        overall['theoretical_cov_improved_rate'] = 0.0
        overall['mean_fused_pos_trace'] = np.nan

    # 9) Fallback rate & Outlier rejection rate
    if len(fused_df) > 0:
        n_total = len(fused_df)
        n_both = len(fused_df[fused_df['fusion_mode'].isin(['BOTH_MEASURED', 'BOTH_PREDICTED'])])
        n_fallback = len(fused_df[fused_df['fusion_mode'].isin(['A_ONLY', 'B_ONLY'])])
        n_outlier = len(fused_df[fused_df['fallback_reason'].notna() &
                                (fused_df['fallback_reason'].str.contains('outlier', na=False))])
        overall['fallback_rate'] = float(n_fallback / n_total) if n_total > 0 else 0.0
        overall['outlier_rejection_rate'] = float(n_outlier / n_total) if n_total > 0 else 0.0
        overall['n_fused_states'] = int(n_total)
        overall['n_both_sensors'] = int(n_both)
        overall['n_fallback'] = int(n_fallback)
    else:
        overall['fallback_rate'] = 0.0
        overall['outlier_rejection_rate'] = 0.0
        overall['n_fused_states'] = 0
        overall['n_both_sensors'] = 0
        overall['n_fallback'] = 0

    return {
        'case': case_dir.name,
        'fused_csv': str(out_csv),
        'per_target': per_target_metrics,
        'overall': overall
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Run Track Fusion on regression benchmark cases')
    parser.add_argument('--cases-root', type=str,
                        default=str(OUTPUT_ROOT / 'regression_benchmark'),
                        help='Root directory of regression cases')
    parser.add_argument('--config', type=str,
                        default=DEFAULT_FUSION_CONFIG,
                        help='Fusion config JSON')
    parser.add_argument('--output', type=str,
                        default=str(OUTPUT_ROOT / 'regression_benchmark' / 'case5_summary.json'),
                        help='Output summary JSON')
    parser.add_argument('--cases', type=str, nargs='*', default=None,
                        help='Specific cases to run (default: all)')
    args = parser.parse_args()

    # 加载 fusion 配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    print(f"Loaded fusion config: regularization_eps={config['fusion'].get('regularization_eps')}, "
          f"innovation_outlier_sigma={config['fusion'].get('innovation_outlier_sigma')}")

    cases_root = Path(args.cases_root)
    if args.cases:
        case_dirs = [cases_root / c for c in args.cases]
    else:
        case_dirs = sorted([d for d in cases_root.iterdir() if d.is_dir() and d.name.startswith('case')])

    print(f"Running Track Fusion on {len(case_dirs)} cases:")
    all_results = []
    for case_dir in case_dirs:
        if not case_dir.exists():
            print(f"  [SKIP] {case_dir.name} (not found)")
            continue
        print(f"\n=== {case_dir.name} ===")
        try:
            result = run_case(case_dir, config)
            all_results.append(result)
            o = result['overall']
            print(f"  mean RMSE: fused={o.get('mean_rmse_fused_m', np.nan):.3f}, "
                  f"A={o.get('mean_rmse_A_m', np.nan):.3f}, "
                  f"B={o.get('mean_rmse_B_m', np.nan):.3f} m")
            print(f"  fusion gain = {o.get('mean_fusion_gain', np.nan):.3f}, "
                  f"coverage = {o.get('mean_coverage', 0):.3f}, "
                  f"fallback_rate = {o.get('fallback_rate', 0):.3f}")
        except Exception as e:
            print(f"  [ERROR] {case_dir.name}: {e}")
            import traceback
            traceback.print_exc()

    # 写总 summary
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        'generated_at': pd.Timestamp.now().isoformat(),
        'config_path': args.config,
        'cases_root': str(cases_root),
        'n_cases': len(all_results),
        'cases': all_results
    }
    # 转 numpy 类型为 Python 原生
    def to_jsonable(o):
        if isinstance(o, dict):
            return {k: to_jsonable(v) for k, v in o.items()}
        elif isinstance(o, list):
            return [to_jsonable(v) for v in o]
        elif isinstance(o, (np.integer,)):
            return int(o)
        elif isinstance(o, (np.floating,)):
            return float(o) if not np.isnan(o) else None
        elif isinstance(o, (np.ndarray,)):
            return o.tolist()
        elif isinstance(o, float) and np.isnan(o):
            return None
        return o
    summary = to_jsonable(summary)

    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"\n[OK] Summary saved to {output_path}")


if __name__ == '__main__':
    main()
