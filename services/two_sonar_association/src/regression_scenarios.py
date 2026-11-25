"""
Regression Benchmark Scenarios
回归测试场景定义

四种难度递进的关联验证场景，用于持续监测关联算法在多目标场景下的稳定性。

- Case 1: 单目标基线 —— 基本关联是否正确
- Case 2: 多目标分离运动 —— 多目标同时关联
- Case 3: 双目标靠近/交叉 —— 是否发生 ID Switch
- Case 4: 交叉 + 漏检 + 断轨 —— Global ID 能否恢复（最有价值）

注意：本阶段验收的是"跨声纳航迹对应关系 + Global ID 稳定性"，
不是 Track Fusion（位置估计精度）。后者属于下一阶段。
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from simulate_two_sonars import ReferenceTrack, VirtualSonar, DistractorTrajectory
from multi_target_scenario import MultiTargetScenario


@dataclass
class RegressionCase:
    """单个回归测试用例的配置"""
    name: str                          # 简短标识，如 "case1_single"
    display_name: str                  # 人类可读名称
    description: str                   # 主要验证内容
    n_targets: int                     # 真实目标数
    target_separation: str             # 分离方式：parallel / crossing / single
    sonar_b_miss_window: Optional[tuple] = None  # Sonar B 的强制漏检窗口 (t_start, t_end)
    sonar_b_track_break: bool = False  # 是否在 Sonar B 上强制断轨（让 Local Track 出现新 ID）
    n_distractors: int = 2             # 干扰数
    sonar_a_sigma_mult: float = 1.0    # Sonar A 噪声倍率
    sonar_b_sigma_mult: float = 1.0    # Sonar B 噪声倍率
    sonar_a_miss_prob: float = 0.03
    sonar_b_miss_prob: float = 0.05
    seed: int = 42

    def summary_dict(self) -> dict:
        return {
            'name': self.name,
            'display_name': self.display_name,
            'description': self.description,
            'n_targets': self.n_targets,
            'target_separation': self.target_separation,
            'sonar_b_miss_window': self.sonar_b_miss_window,
            'sonar_b_track_break': self.sonar_b_track_break,
            'n_distractors': self.n_distractors,
        }


# ============================================================
# Case 1: 单目标基线（基本关联是否正确）
# ============================================================
def build_case1_single() -> RegressionCase:
    """Case 1：单目标基线
    验证：基本关联是否正确
    仅 1 个 Diver，无干扰，最简单场景，用作 regression 底线。
    """
    return RegressionCase(
        name='case1_single',
        display_name='Case 1: 单目标基线',
        description='验证最基本的两声纳航迹对应关系能否建立',
        n_targets=1,
        target_separation='single',
        sonar_b_miss_window=None,
        sonar_b_track_break=False,
        n_distractors=0,
        sonar_a_sigma_mult=1.0,
        sonar_b_sigma_mult=1.0,
        sonar_a_miss_prob=0.03,
        sonar_b_miss_prob=0.05,
        seed=42
    )


# ============================================================
# Case 2: 多目标分离运动（多目标同时关联）
# ============================================================
def build_case2_separated() -> RegressionCase:
    """Case 2：多目标分离运动
    验证：3 个目标在空间中分得很开时，能否同时被两个声纳独立关联
    """
    return RegressionCase(
        name='case2_separated',
        display_name='Case 2: 多目标分离运动',
        description='验证 3 个目标在分离状态下能否同时被两个声纳独立关联',
        n_targets=3,
        target_separation='parallel',  # 平行 + 远距离
        sonar_b_miss_window=None,
        sonar_b_track_break=False,
        n_distractors=2,
        sonar_a_sigma_mult=1.0,
        sonar_b_sigma_mult=1.0,
        sonar_a_miss_prob=0.04,
        sonar_b_miss_prob=0.05,
        seed=43
    )


# ============================================================
# Case 3: 双目标靠近/交叉（是否发生 ID Switch）
# ============================================================
def build_case3_crossing() -> RegressionCase:
    """Case 3：双目标靠近/交叉
    验证：交叉过程中 ID 是否稳定
    """
    return RegressionCase(
        name='case3_crossing',
        display_name='Case 3: 双目标交叉',
        description='验证两个目标交叉过程中是否发生 ID Switch',
        n_targets=2,
        target_separation='crossing',
        sonar_b_miss_window=None,
        sonar_b_track_break=False,
        n_distractors=2,
        sonar_a_sigma_mult=1.0,
        sonar_b_sigma_mult=1.0,
        sonar_a_miss_prob=0.05,
        sonar_b_miss_prob=0.06,
        seed=44
    )


# ============================================================
# Case 4: 交叉 + 漏检 + 断轨（最有价值）
# ============================================================
def build_case4_crossing_with_break(
    crossing_time_s: float = 30.0,
    miss_window_duration: float = 8.0
) -> RegressionCase:
    """Case 4：交叉 + Sonar B 漏检窗口 + Sonar B 断轨

    这是最有价值的测试场景：
    - 两个蛙人在 crossing_time_s 附近交叉
    - Sonar B 在交叉前后 miss_window_duration 秒内强制漏检
    - 由于长时间漏检，Sonar B 的 Local Track 会被分割为多段
      （新 track ID 出现）

    验证重点：
    - 交叉过后 Global ID 能否正确恢复
    - Sonar B 上产生的新 Track ID 能否被正确归入原 Global Track
    - 不发生 ID Switch
    """
    return RegressionCase(
        name='case4_crossing_break',
        display_name='Case 4: 交叉 + 漏检 + 断轨',
        description='验证交叉 + 漏检 + Sonar B 断轨后 Global ID 能否恢复',
        n_targets=2,
        target_separation='crossing',
        sonar_b_miss_window=(crossing_time_s - miss_window_duration / 2,
                              crossing_time_s + miss_window_duration / 2),
        sonar_b_track_break=True,
        n_distractors=2,
        sonar_a_sigma_mult=1.0,
        sonar_b_sigma_mult=1.0,
        sonar_a_miss_prob=0.05,
        sonar_b_miss_prob=0.06,
        seed=45
    )


# ============================================================
# 构造场景 + 注入扰动
# ============================================================
def build_scenario_for_case(case: RegressionCase,
                            reference: ReferenceTrack) -> MultiTargetScenario:
    """
    根据 RegressionCase 构造多目标场景

    Case 1: 只放 1 个 T1 (复用 reference)
    Case 2: 3 个目标，互相远距离平行
    Case 3/4: 2 个目标，在 reference 中点附近交叉
    """
    if case.n_targets == 1:
        scenario = MultiTargetScenario(seed=case.seed)
        scenario.add_target('T1', reference)
        return scenario

    if case.target_separation == 'parallel':
        # Case 2: 3 个目标，远距离平行
        scenario = MultiTargetScenario(seed=case.seed)
        scenario.add_target('T1', reference)

        # T2: 沿参考轨迹偏移 +40m
        t2_x = reference.x.copy() + 40.0
        t2_y = reference.y.copy() + 60.0
        scenario.add_target('T2', ReferenceTrack(reference.timestamps.copy(), t2_x, t2_y))

        # T3: 沿参考轨迹偏移 -40m
        t3_x = reference.x.copy() - 40.0
        t3_y = reference.y.copy() + 80.0
        scenario.add_target('T3', ReferenceTrack(reference.timestamps.copy(), t3_x, t3_y))

        return scenario

    if case.target_separation == 'crossing':
        # Case 3/4: 2 个目标，在中点附近交叉
        n = reference.length
        cross_idx = n // 2

        scenario = MultiTargetScenario(seed=case.seed)
        scenario.add_target('T1', reference)

        # T2 从反向接近交叉点
        t2_start_x = reference.x[cross_idx] - 80.0
        t2_start_y = reference.y[cross_idx] + 80.0
        t2_end_x = reference.x[cross_idx] + 80.0
        t2_end_y = reference.y[cross_idx] - 80.0

        t2_x = np.zeros(n)
        t2_y = np.zeros(n)
        for i in range(n):
            if i <= cross_idx:
                w = i / max(1, cross_idx)
                t2_x[i] = t2_start_x + w * (reference.x[cross_idx] - t2_start_x)
                t2_y[i] = t2_start_y + w * (reference.y[cross_idx] - t2_start_y)
            else:
                w = (i - cross_idx) / max(1, (n - 1 - cross_idx))
                t2_x[i] = reference.x[cross_idx] + w * (t2_end_x - reference.x[cross_idx])
                t2_y[i] = reference.y[cross_idx] + w * (t2_end_y - reference.y[cross_idx])

        scenario.add_target('T2', ReferenceTrack(reference.timestamps.copy(), t2_x, t2_y))

        return scenario

    raise ValueError(f"Unknown target_separation: {case.target_separation}")


def generate_measurements_for_case(case: RegressionCase,
                                    scenario: MultiTargetScenario,
                                    sonar_a: VirtualSonar,
                                    sonar_b: VirtualSonar) -> tuple:
    """
    为用例生成两声纳的测量，可选地注入 Case 4 的漏检窗口

    返回:
        (meas_a_df, meas_b_df)
    """
    target_ids = scenario.get_target_ids()
    t_start = scenario.targets[target_ids[0]].timestamps[0]
    t_end = scenario.targets[target_ids[0]].timestamps[-1]

    meas_a_records = []
    meas_b_records = []

    miss_a_active = False  # 当前已为 Case 4 设计的 Sonar B 漏检窗口
    miss_b_active = False

    for target_id in target_ids:
        traj = scenario.targets[target_id]

        # Sonar A 测量（不受 Case 4 miss_window 影响）
        times_a = sonar_a.generate_measurement_times(t_start, t_end)
        for t in times_a:
            x, y, _, _ = traj.get_position_at(t)
            meas = sonar_a.generate_measurement(x, y, add_noise=True)
            if meas is None:
                continue
            meas_a_records.append({
                'timestamp': t,
                'sensor_id': 'SONAR_A',
                'target_id': target_id,
                'local_track_id': f'A_{target_id}',
                'range_m': meas['range_m'],
                'bearing_deg': meas['bearing_deg'],
                'x_common': meas['x_common'],
                'y_common': meas['y_common'],
                'measurement_available': True
            })

        # Sonar B 测量（如果 case 有 miss_window 则强制漏检）
        times_b = sonar_b.generate_measurement_times(t_start, t_end)
        for t in times_b:
            # 检查是否在 Sonar B 强制漏检窗口内
            if case.sonar_b_miss_window is not None:
                t_miss_start, t_miss_end = case.sonar_b_miss_window
                if t_miss_start <= t <= t_miss_end:
                    # 强制漏检
                    continue

            x, y, _, _ = traj.get_position_at(t)
            meas = sonar_b.generate_measurement(x, y, add_noise=True)
            if meas is None:
                continue
            meas_b_records.append({
                'timestamp': t,
                'sensor_id': 'SONAR_B',
                'target_id': target_id,
                'local_track_id': f'B_{target_id}',
                'range_m': meas['range_m'],
                'bearing_deg': meas['bearing_deg'],
                'x_common': meas['x_common'],
                'y_common': meas['y_common'],
                'measurement_available': True
            })

    return pd.DataFrame(meas_a_records), pd.DataFrame(meas_b_records)


def apply_track_break_to_sonar_b(case: RegressionCase,
                                   meas_b_df: pd.DataFrame) -> pd.DataFrame:
    """
    Case 4 注入点：在 Sonar B 上强制断轨

    在 miss_window 结束后，给 Sonar B 的所有 local_track_id 加上后缀，
    使 Local Tracker 把它当作新的 Track。

    这样可以让 Global Track Manager 真正测试：
    - 一个 Global Track 跨越多个 Local Track member 的能力
    - reassociation 后 Global ID 的恢复
    """
    if not case.sonar_b_track_break or case.sonar_b_miss_window is None:
        return meas_b_df

    t_miss_end = case.sonar_b_miss_window[1]

    df = meas_b_df.copy()

    def reassign_track_id(row):
        # 仅在 miss 窗口之后的时间点重新分配 Track ID
        if row['timestamp'] > t_miss_end:
            return f"{row['local_track_id']}_p2"
        return row['local_track_id']

    df['local_track_id'] = df.apply(reassign_track_id, axis=1)
    return df


def get_all_cases() -> List[RegressionCase]:
    """获取所有 4 个回归测试用例"""
    return [
        build_case1_single(),
        build_case2_separated(),
        build_case3_crossing(),
        build_case4_crossing_with_break(),
    ]
