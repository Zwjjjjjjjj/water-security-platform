"""
Multi-Target Reference Tracks Generator
多目标参考航迹生成器：生成多个独立的物理目标运动轨迹
用于 2-diver crossing 等复杂场景验证
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from simulate_two_sonars import ReferenceTrack


class MultiTargetScenario:
    """
    多目标场景基类
    每个目标有独立的运动轨迹、速度、方向
    """
    
    def __init__(self, seed: int = 42):
        self.rng = np.random.default_rng(seed)
        self.targets: Dict[str, ReferenceTrack] = {}
    
    def add_target(self, target_id: str, trajectory: ReferenceTrack):
        self.targets[target_id] = trajectory
    
    def get_target_ids(self) -> List[str]:
        return list(self.targets.keys())
    
    def get_all_target_positions_at(self, t: float) -> Dict[str, Tuple[float, float]]:
        """获取所有目标在指定时间的位置"""
        positions = {}
        for tid, traj in self.targets.items():
            x, y, _, _ = traj.get_position_at(t)
            positions[tid] = (x, y)
        return positions


def create_parallel_diver_scenario(reference: ReferenceTrack, 
                                    seed: int = 42) -> MultiTargetScenario:
    """
    创建平行运动的两蛙人场景（Easy 1-diver 扩展为 2-diver parallel）
    
    两个蛙人平行运动，距离较远
    """
    rng = np.random.default_rng(seed)
    scenario = MultiTargetScenario(seed=seed)
    
    # Target 1 - 主参考航迹
    scenario.add_target('T1', reference)
    
    # Target 2 - 平行运动
    t2_x = reference.x.copy() + 30.0
    t2_y = reference.y.copy() + 50.0
    t2_trajectory = ReferenceTrack(reference.timestamps.copy(), t2_x, t2_y)
    scenario.add_target('T2', t2_trajectory)
    
    return scenario


def create_crossing_diver_scenario(reference: ReferenceTrack,
                                    seed: int = 42,
                                    crossing_time_s: float = None,
                                    min_distance_m: float = 5.0) -> MultiTargetScenario:
    """
    创建两蛙人交叉场景（2-diver crossing）
    
    两个蛙人在指定时间交叉，最小距离可以配置
    验证交叉后能否正确维持各自 ID（不发生 ID switch）
    """
    scaling = 0.015  # 像素到米的转换
    
    if crossing_time_s is None:
        crossing_time_s = (reference.timestamps[-1] + reference.timestamps[0]) / 2
    
    scenario = MultiTargetScenario(seed=seed)
    
    # Target 1 - 主参考航迹 (使用像素坐标)
    scenario.add_target('T1', reference)
    
    # Target 2 - 设计为在交叉点附近与 Target 1 接近
    # 在交叉之前，从反方向接近
    # 交叉之后，沿原方向继续
    
    n = reference.length
    
    # 找到交叉时间附近的索引
    cross_idx = np.argmin(np.abs(reference.timestamps - crossing_time_s))
    
    # Target 2 的起点
    t2_start_x = reference.x[cross_idx] - 80.0
    t2_start_y = reference.y[cross_idx] + 80.0
    
    # Target 2 的终点
    t2_end_x = reference.x[cross_idx] + 80.0
    t2_end_y = reference.y[cross_idx] - 80.0
    
    # 生成 Target 2 的轨迹
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
    
    t2_trajectory = ReferenceTrack(reference.timestamps.copy(), t2_x, t2_y)
    scenario.add_target('T2', t2_trajectory)
    
    return scenario


def create_three_diver_scenario(reference: ReferenceTrack,
                                 seed: int = 42) -> MultiTargetScenario:
    """
    创建三蛙人场景（多目标压力测试）
    
    三个蛙人分别在不同方向运动
    """
    scenario = MultiTargetScenario(seed=seed)
    
    # Target 1 - 主参考航迹
    scenario.add_target('T1', reference)
    
    # Target 2 - 垂直方向运动
    t2_x = np.full_like(reference.x, 400.0)
    t2_y = reference.y.copy()
    t2_trajectory = ReferenceTrack(reference.timestamps.copy(), t2_x, t2_y)
    scenario.add_target('T2', t2_trajectory)
    
    # Target 3 - 静止目标
    t3_x = np.full_like(reference.x, 450.0)
    t3_y = np.full_like(reference.y, 300.0)
    t3_trajectory = ReferenceTrack(reference.timestamps.copy(), t3_x, t3_y)
    scenario.add_target('T3', t3_trajectory)
    
    return scenario


def generate_multi_target_measurements(scenario: MultiTargetScenario,
                                        sonar,
                                        target_ids: List[str],
                                        sensor_id: str) -> List[Dict]:
    """
    从多目标场景生成虚拟声纳测量
    
    返回:
        测量列表，每个测量包含 timestamp, target_id, sensor_id 等
    """
    measurements = []
    
    # 合并所有目标的公共时间范围
    all_timestamps = scenario.targets[target_ids[0]].timestamps
    
    # 为每个目标独立生成测量
    for target_id in target_ids:
        traj = scenario.targets[target_id]
        
        # 生成测量时间点（基于声纳采样率）
        measurement_times = sonar.generate_measurement_times(
            all_timestamps[0], all_timestamps[-1]
        )
        
        for t in measurement_times:
            x, y, _, _ = traj.get_position_at(t)
            meas = sonar.generate_measurement(x, y, add_noise=True)
            
            if meas is None:
                continue
            
            record = {
                'timestamp': t,
                'sensor_id': sensor_id,
                'target_id': target_id,
                'range_m': meas['range_m'],
                'bearing_deg': meas['bearing_deg'],
                'x_common': meas['x_common'],
                'y_common': meas['y_common'],
                'measurement_available': True
            }
            measurements.append(record)
    
    return measurements