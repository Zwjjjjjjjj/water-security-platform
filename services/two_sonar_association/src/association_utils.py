"""
Track-to-Track Association Utilities
双声纳航迹关联工具函数
"""
import numpy as np
from typing import Tuple, Optional, Dict, Any
from datetime import datetime
import math


def normalize_angle(angle_deg: float) -> float:
    """将角度归一化到 [-180, 180) 范围"""
    while angle_deg >= 180:
        angle_deg -= 360
    while angle_deg < -180:
        angle_deg += 360
    return angle_deg


def angle_difference_deg(a: float, b: float) -> float:
    """计算两个角度之间的最小差值（度）"""
    diff = normalize_angle(a - b)
    return diff


def position_distance(x1: float, y1: float, x2: float, y2: float) -> float:
    """计算两点之间的欧氏距离"""
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def velocity_difference(vx1: float, vy1: float, vx2: float, vy2: float) -> float:
    """计算两个速度向量之间的欧氏距离"""
    return math.sqrt((vx2 - vx1) ** 2 + (vy2 - vy1) ** 2)


def compute_heading_from_velocity(vx: float, vy: float) -> float:
    """从速度分量计算航向角（度）"""
    if abs(vx) < 1e-10 and abs(vy) < 1e-10:
        return 0.0
    return math.degrees(math.atan2(vy, vx))


def polar_to_cartesian(range_m: float, bearing_deg: float, 
                       sensor_x: float, sensor_y: float, 
                       sensor_heading_deg: float) -> Tuple[float, float]:
    """
    将极坐标 (range, bearing) 转换为公共坐标系中的笛卡尔坐标
    
    参数:
        range_m: 距离（米）
        bearing_deg: 方位角（度），相对于传感器heading方向
        sensor_x, sensor_y: 传感器在公共坐标系中的位置
        sensor_heading_deg: 传感器朝向（度），0度通常指北/正Y方向
    
    返回:
        (x, y): 公共坐标系中的坐标
    """
    # bearing 相对于传感器朝向，顺时针为正
    absolute_bearing_deg = sensor_heading_deg + bearing_deg
    absolute_bearing_rad = math.radians(absolute_bearing_deg)
    
    # 计算目标在公共坐标系中的位置
    x = sensor_x + range_m * math.sin(absolute_bearing_rad)
    y = sensor_y + range_m * math.cos(absolute_bearing_rad)
    
    return x, y


def cartesian_to_polar(x: float, y: float,
                       sensor_x: float, sensor_y: float,
                       sensor_heading_deg: float) -> Tuple[float, float]:
    """
    将笛卡尔坐标转换为传感器的极坐标
    
    返回:
        (range_m, bearing_deg): 距离和方位角（相对于传感器朝向）
    """
    dx = x - sensor_x
    dy = y - sensor_y
    
    range_m = math.sqrt(dx ** 2 + dy ** 2)
    
    # 计算相对于正北方向的方位角
    abs_bearing_rad = math.atan2(dx, dy)
    
    # 相对于传感器朝向的方位角
    bearing_deg = normalize_angle(math.degrees(abs_bearing_rad) - sensor_heading_deg)
    
    return range_m, bearing_deg


def mahalanobis_distance(delta_x: np.ndarray, cov_matrix: np.ndarray) -> float:
    """
    计算马氏距离
    
    参数:
        delta_x: 状态差异向量
        cov_matrix: 合并协方差矩阵
    
    返回:
        马氏距离
    """
    # 使用数值稳定的方法求解
    try:
        # S = P_A + P_B (位置部分)
        # 使用 Cholesky 分解或 SVD 求解
        L = np.linalg.cholesky(cov_matrix)
        y = np.linalg.solve(L, delta_x)
        return np.sqrt(np.sum(y ** 2))
    except np.linalg.LinAlgError:
        # 如果 Cholesky 失败，使用伪逆
        pinv = np.linalg.pinv(cov_matrix)
        return np.sqrt(delta_x @ pinv @ delta_x)


def align_timestamps(timestamps_a: np.ndarray, timestamps_b: np.ndarray, 
                     max_gap_s: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    对齐两个传感器的时间戳
    
    参数:
        timestamps_a: 传感器A的时间戳数组
        timestamps_b: 传感器B的时间戳数组
        max_gap_s: 最大允许时间间隔（秒）
    
    返回:
        (common_timestamps, indices_a, indices_b): 公共时间戳及对应索引
    """
    common_times = []
    indices_a = []
    indices_b = []
    
    i, j = 0, 0
    while i < len(timestamps_a) and j < len(timestamps_b):
        t_a = timestamps_a[i]
        t_b = timestamps_b[j]
        
        gap = abs(t_a - t_b)
        
        if gap <= max_gap_s:
            common_times.append((t_a + t_b) / 2)  # 使用中点时间
            indices_a.append(i)
            indices_b.append(j)
            
            # 移动时间较晚的指针
            if t_a <= t_b:
                i += 1
            else:
                j += 1
        elif t_a < t_b:
            i += 1
        else:
            j += 1
    
    return np.array(common_times), np.array(indices_a), np.array(indices_b)


def parse_timestamp_to_seconds(timestamp_str: str, reference_time: Optional[datetime] = None) -> float:
    """
    将 ISO 格式时间戳转换为相对于参考时间的秒数
    """
    if isinstance(timestamp_str, (int, float)):
        return float(timestamp_str)
    
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        if reference_time is None:
            reference_time = dt.replace(second=0, microsecond=0)
        return (dt - reference_time).total_seconds()
    except:
        return float(timestamp_str)


def compute_track_statistics(positions: np.ndarray, velocities: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """
    计算航迹统计信息
    """
    if len(positions) == 0:
        return {}
    
    stats = {
        'num_points': len(positions),
        'mean_x': np.mean(positions[:, 0]),
        'mean_y': np.mean(positions[:, 1]),
        'std_x': np.std(positions[:, 0]),
        'std_y': np.std(positions[:, 1]),
    }
    
    if velocities is not None and len(velocities) > 0:
        speeds = np.sqrt(velocities[:, 0]**2 + velocities[:, 1]**2)
        stats['mean_speed'] = np.mean(speeds)
        stats['std_speed'] = np.std(speeds)
        headings = np.degrees(np.arctan2(velocities[:, 1], velocities[:, 0]))
        stats['mean_heading'] = np.mean(headings)
    
    return stats


class KalmanFilter:
    """
    简单的2D Kalman Filter用于航迹跟踪
    
    状态向量: [x, y, vx, vy]
    """
    
    def __init__(self, process_noise_pos: float = 1.0, 
                 process_noise_vel: float = 0.5,
                 measurement_noise_pos: float = 2.0,
                 initial_covariance: float = 10.0):
        """
        初始化 Kalman Filter
        
        参数:
            process_noise_pos: 位置过程噪声 (m/s^2)
            process_noise_vel: 速度过程噪声 (m/s^2)
            measurement_noise_pos: 位置测量噪声 (m)
            initial_covariance: 初始状态协方差
        """
        self.dt = 0.2  # 默认采样间隔，会在预测时更新
        
        # 状态转移矩阵 (恒定速度模型)
        self.F = np.array([
            [1, 0, 1, 0],
            [0, 1, 0, 1],
            [0, 0, 1, 0],
            [0, 0, 0, 1]
        ])
        
        # 过程噪声协方差
        self.Q_base = np.array([
            [process_noise_pos**2, 0, 0, 0],
            [0, process_noise_pos**2, 0, 0],
            [0, 0, process_noise_vel**2, 0],
            [0, 0, 0, process_noise_vel**2]
        ])
        
        # 测量矩阵 (只测量位置)
        self.H = np.array([
            [1, 0, 0, 0],
            [0, 1, 0, 0]
        ])
        
        # 测量噪声协方差
        self.R = np.eye(2) * measurement_noise_pos**2
        
        # 初始状态和协方差
        self.x = None
        self.P = None
        self.initial_covariance = initial_covariance
        
        # 跟踪状态
        self.hits = 0
        self.misses = 0
        self.is_initialized = False
    
    def initialize(self, x: float, y: float, timestamp: float):
        """使用第一个测量值初始化滤波器"""
        # 假设初始速度为零
        self.x = np.array([x, y, 0, 0])
        self.P = np.eye(4) * self.initial_covariance
        self.last_timestamp = timestamp
        self.is_initialized = True
        self.hits = 1
        self.misses = 0
    
    def predict(self, dt: float = None) -> Tuple[np.ndarray, np.ndarray]:
        """
        预测步骤
        
        返回:
            (predicted_state, predicted_covariance)
        """
        if not self.is_initialized:
            return None, None
        
        if dt is not None:
            self.dt = dt
            # 更新状态转移矩阵
            self.F = np.array([
                [1, 0, dt, 0],
                [0, 1, 0, dt],
                [0, 0, 1, 0],
                [0, 0, 0, 1]
            ])
        
        # 过程噪声缩放
        dt2 = self.dt ** 2
        dt3 = dt2 * self.dt / 2
        Q = np.array([
            [dt2/2, 0, dt, 0],
            [0, dt2/2, 0, dt],
            [dt, 0, dt**2, 0],
            [0, dt, 0, dt**2]
        ])
        
        # 简化的过程噪声模型
        Q = self.Q_base * self.dt
        
        # 预测
        self.x = self.F @ self.x
        self.P = self.F @ self.P @ self.F.T + Q
        
        return self.x.copy(), self.P.copy()
    
    def update(self, z: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        更新步骤
        
        参数:
            z: 测量值 [x, y]
        
        返回:
            (updated_state, updated_covariance)
        """
        if not self.is_initialized:
            return None, None
        
        # 创新向量
        y = z - self.H @ self.x
        
        # 创新协方差
        S = self.H @ self.P @ self.H.T + self.R
        
        # 卡尔曼增益
        try:
            K = self.P @ self.H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            K = self.P @ self.H.T @ np.linalg.pinv(S)
        
        # 更新状态和协方差
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ self.H) @ self.P
        
        self.hits += 1
        
        return self.x.copy(), self.P.copy()
    
    def predict_measurement(self) -> np.ndarray:
        """预测在公共时刻的测量"""
        if not self.is_initialized:
            return None
        return self.H @ self.x
    
    def get_position(self) -> Tuple[float, float]:
        """获取当前位置估计"""
        if not self.is_initialized:
            return 0, 0
        return self.x[0], self.x[1]
    
    def get_velocity(self) -> Tuple[float, float]:
        """获取当前速度估计"""
        if not self.is_initialized:
            return 0, 0
        return self.x[2], self.x[3]


def create_empty_output_directory(base_path: str) -> str:
    """创建带时间戳的输出目录"""
    from pathlib import Path
    import os
    
    output_dir = Path(base_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    figures_dir = output_dir / 'figures'
    figures_dir.mkdir(parents=True, exist_ok=True)
    
    return str(output_dir)


def load_config(config_path: str) -> Dict[str, Any]:
    """加载 JSON 配置文件"""
    import json
    with open(config_path, 'r') as f:
        return json.load(f)


def save_csv(data: list, columns: list, output_path: str):
    """保存 CSV 文件"""
    import csv
    
    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data)


def validate_association_sanity(sonar_a_meas: np.ndarray, 
                                 sonar_b_meas: np.ndarray,
                                 sonar_a_config: Dict,
                                 sonar_b_config: Dict) -> Dict[str, bool]:
    """
    验证航迹关联的 sanity checks
    
    返回:
        dict of check results
    """
    checks = {}
    
    # Check 1: A/B measurement noise 是否真正独立
    if 'sigma_range_m' in sonar_a_config and 'sigma_range_m' in sonar_b_config:
        noise_same = (sonar_a_config['sigma_range_m'] == sonar_b_config['sigma_range_m'] and
                     sonar_a_config['sigma_bearing_deg'] == sonar_b_config['sigma_bearing_deg'])
        checks['noise_independent'] = not noise_same  # 应该不同
    
    # Check 2: A/B timestamp 是否不同
    if len(sonar_a_meas) > 0 and len(sonar_b_meas) > 0:
        times_a = sonar_a_meas[:, 0]
        times_b = sonar_b_meas[:, 0]
        times_match = np.allclose(times_a[:min(len(times_a), len(times_b))], 
                                  times_b[:min(len(times_a), len(times_b))], 
                                  atol=0.001)
        checks['timestamps_different'] = not times_match  # 应该在某种程度上不同
    
    # Check 4: 公共坐标转换验证 (无噪声时应该恢复到同一位置)
    # 这个检查需要在仿真时验证
    
    return checks
