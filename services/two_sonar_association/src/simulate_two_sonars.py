"""
Simulate Two Sonars - Virtual Sonar Measurement Generation
从真实航迹生成两个虚拟声纳的观测数据
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
import json
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG, DEFAULT_REFERENCE_TRACK, OUTPUT_ROOT
from association_utils import (
    polar_to_cartesian, cartesian_to_polar, normalize_angle,
    KalmanFilter, save_csv
)


class ReferenceTrack:
    """参考航迹类"""
    
    def __init__(self, timestamps: np.ndarray, x: np.ndarray, y: np.ndarray):
        """
        初始化参考航迹
        
        参数:
            timestamps: 时间戳（秒，相对于起始时间）
            x, y: 位置坐标（米）
        """
        # 去除重复和无效数据
        valid_mask = ~np.isnan(x) & ~np.isnan(y)
        # 同时保持时间单调
        unique_mask = np.diff(timestamps) > 0
        unique_mask = np.concatenate([[True], unique_mask])
        
        mask = valid_mask & unique_mask
        
        self.timestamps = timestamps[mask]
        self.x = x[mask]
        self.y = y[mask]
        
        # 计算速度（使用中心差分）
        self.vx = np.zeros_like(self.x)
        self.vy = np.zeros_like(self.y)
        
        for i in range(len(self.x)):
            if i == 0:
                dt = self.timestamps[1] - self.timestamps[0]
                self.vx[0] = (self.x[1] - self.x[0]) / dt if dt > 0 else 0
                self.vy[0] = (self.y[1] - self.y[0]) / dt if dt > 0 else 0
            elif i == len(self.x) - 1:
                dt = self.timestamps[-1] - self.timestamps[-2]
                self.vx[-1] = (self.x[-1] - self.x[-2]) / dt if dt > 0 else 0
                self.vy[-1] = (self.y[-1] - self.y[-2]) / dt if dt > 0 else 0
            else:
                dt = self.timestamps[i+1] - self.timestamps[i-1]
                self.vx[i] = (self.x[i+1] - self.x[i-1]) / dt if dt > 0 else 0
                self.vy[i] = (self.y[i+1] - self.y[i-1]) / dt if dt > 0 else 0
        
        self.length = len(self.timestamps)
    
    def get_position_at(self, t: float) -> Tuple[float, float, float, float]:
        """获取指定时间的插值位置"""
        if t <= self.timestamps[0]:
            return self.x[0], self.y[0], self.vx[0], self.vy[0]
        if t >= self.timestamps[-1]:
            return self.x[-1], self.y[-1], self.vx[-1], self.vy[-1]
        
        # 线性插值
        idx = np.searchsorted(self.timestamps, t)
        if idx == 0:
            return self.x[0], self.y[0], self.vx[0], self.vy[0]
        if idx >= len(self.timestamps):
            return self.x[-1], self.y[-1], self.vx[-1], self.vy[-1]
        
        t1, t2 = self.timestamps[idx-1], self.timestamps[idx]
        w = (t - t1) / (t2 - t1) if t2 > t1 else 0
        
        x = self.x[idx-1] + w * (self.x[idx] - self.x[idx-1])
        y = self.y[idx-1] + w * (self.y[idx] - self.y[idx-1])
        vx = self.vx[idx-1] + w * (self.vx[idx] - self.vx[idx-1])
        vy = self.vy[idx-1] + w * (self.vy[idx] - self.vy[idx-1])
        
        return x, y, vx, vy
    
    def to_dataframe(self) -> pd.DataFrame:
        """转换为 DataFrame"""
        return pd.DataFrame({
            'timestamp': self.timestamps,
            'x_ref': self.x,
            'y_ref': self.y,
            'vx_ref': self.vx,
            'vy_ref': self.vy
        })
    
    def to_csv(self, output_path: str):
        """保存到 CSV"""
        self.to_dataframe().to_csv(output_path, index=False)


class VirtualSonar:
    """虚拟声纳类"""
    
    def __init__(self, config: Dict, sensor_id: str, rng: np.random.Generator):
        """
        初始化虚拟声纳
        
        参数:
            config: 声纳配置字典
            sensor_id: 传感器ID
            rng: numpy 随机数生成器
        """
        self.sensor_id = sensor_id
        self.x = config['x_m']
        self.y = config['y_m']
        self.heading_deg = config['heading_deg']
        self.sample_rate_hz = config['sample_rate_hz']
        self.time_offset_s = config.get('time_offset_s', 0.0)
        self.sigma_range_m = config['sigma_range_m']
        self.sigma_bearing_deg = config['sigma_bearing_deg']
        self.miss_probability = config.get('miss_probability', 0.0)
        self.rng = rng
        
        # 采样间隔
        self.dt = 1.0 / self.sample_rate_hz
        
        # 测量记录
        self.measurements = []
    
    def generate_measurement_times(self, t_start: float, t_end: float) -> np.ndarray:
        """生成测量时间点"""
        # 第一个测量时间 = 起始时间 + time_offset
        t_first = t_start + self.time_offset_s
        times = [t_first]
        
        t = t_first + self.dt
        while t <= t_end:
            times.append(t)
            t += self.dt
        
        return np.array(times)
    
    def generate_measurement(self, x: float, y: float, 
                            add_noise: bool = True) -> Optional[Dict]:
        """
        生成单个测量
        
        参数:
            x, y: 目标在公共坐标系中的位置
            add_noise: 是否添加测量噪声
        """
        # 检查是否漏检
        if self.rng.random() < self.miss_probability:
            return None
        
        # 计算相对于传感器的极坐标
        range_m, bearing_deg = cartesian_to_polar(
            x, y, self.x, self.y, self.heading_deg
        )
        
        if range_m < 0.1:  # 太近的目标
            range_m = 0.1
        
        # 添加测量噪声
        if add_noise:
            range_noise = self.rng.normal(0, self.sigma_range_m)
            bearing_noise = self.rng.normal(0, self.sigma_bearing_deg)
            
            range_m += range_noise
            bearing_deg = normalize_angle(bearing_deg + bearing_noise)
        
        # 转换回笛卡尔坐标
        x_common, y_common = polar_to_cartesian(
            range_m, bearing_deg, self.x, self.y, self.heading_deg
        )
        
        return {
            'range_m': range_m,
            'bearing_deg': bearing_deg,
            'x_common': x_common,
            'y_common': y_common,
            'measurement_available': True
        }
    
    def simulate_from_reference(self, reference: ReferenceTrack,
                                add_noise: bool = True) -> pd.DataFrame:
        """
        从参考航迹生成测量
        
        参数:
            reference: 参考航迹
            add_noise: 是否添加噪声
        """
        self.measurements = []
        
        # 生成测量时间点
        t_start = reference.timestamps[0]
        t_end = reference.timestamps[-1]
        measurement_times = self.generate_measurement_times(t_start, t_end)
        
        for t in measurement_times:
            x, y, vx, vy = reference.get_position_at(t)
            
            meas = self.generate_measurement(x, y, add_noise)
            
            record = {
                'timestamp': t,
                'sensor_id': self.sensor_id,
                'time_offset_s': self.time_offset_s,
                'sample_rate_hz': self.sample_rate_hz
            }
            
            if meas is not None:
                record.update(meas)
            else:
                record['range_m'] = np.nan
                record['bearing_deg'] = np.nan
                record['x_common'] = np.nan
                record['y_common'] = np.nan
                record['measurement_available'] = False
            
            self.measurements.append(record)
        
        return pd.DataFrame(self.measurements)


class DistractorTrajectory:
    """干扰航迹生成器"""
    
    def __init__(self, config: Dict, sensor_id: str, rng: np.random.Generator):
        self.config = config
        self.sensor_id = sensor_id
        self.rng = rng
        
        # 基础参数
        self.start_offset_s = config.get('start_offset_s', 0.0)
        self.base_speed_mps = config.get('base_speed_mps', 25.0)
        self.heading_offset_deg = config.get('heading_offset_deg', 0.0)
        self.noise_multiplier = config.get('noise_multiplier', 1.0)
        
        # 交叉行为配置
        self.crossing_behavior = config.get('crossing_behavior', False)
        self.crossing_start_s = config.get('crossing_start_s', 20.0)
        self.crossing_duration_s = config.get('crossing_duration_s', 8.0)
        self.crossing_proximity_m = config.get('crossing_proximity_m', 8.0)
        
        # 状态
        self.traj_x = []
        self.traj_y = []
        self.traj_vx = []
        self.traj_vy = []
        self.traj_heading = []
        self.traj_timestamps = []
    
    def generate(self, ref_trajectory: ReferenceTrack, 
                 ref_start_x: float, ref_start_y: float,
                 duration_s: float = None) -> Tuple[np.ndarray, np.ndarray]:
        """
        生成干扰航迹
        
        参数:
            ref_trajectory: 参考航迹（用于获取时间范围）
            ref_start_x, ref_start_y: 参考航迹起始位置（用于初始化干扰位置）
            duration_s: 持续时间（秒），默认使用参考航迹时长
        
        返回:
            (timestamps, positions) 元组
        """
        if duration_s is None:
            duration_s = ref_trajectory.timestamps[-1] - ref_trajectory.timestamps[0]
        
        t_start = ref_trajectory.timestamps[0] + self.start_offset_s
        t_end = t_start + duration_s
        
        # 采样率
        sample_rate = 5.0  # Hz
        dt = 1.0 / sample_rate
        timestamps = np.arange(t_start, t_end, dt)
        
        # 初始化位置 - 偏移参考起始点
        angle_offset = np.radians(self.heading_offset_deg + self.rng.uniform(-30, 30))
        initial_distance = self.rng.uniform(30, 60)
        
        x = ref_start_x + initial_distance * np.sin(angle_offset)
        y = ref_start_y + initial_distance * np.cos(angle_offset)
        
        # 基础航向
        base_heading = self.heading_offset_deg + self.rng.uniform(-20, 20)
        
        for i, t in enumerate(timestamps):
            # 检查是否进入交叉阶段
            crossing_factor = 0.0
            if self.crossing_behavior:
                t_since_start = t - t_start
                if self.crossing_start_s <= t_since_start <= self.crossing_start_s + self.crossing_duration_s:
                    # 余弦过渡
                    phase = (t_since_start - self.crossing_start_s) / self.crossing_duration_s * np.pi
                    crossing_factor = np.cos(phase)  # -1 to 1
            
            # 计算向参考轨迹靠近的程度
            if self.crossing_behavior and crossing_factor < 0:
                # 接近参考轨迹
                approach_strength = abs(crossing_factor) * 0.5
                
                # 获取当前参考位置
                ref_x, ref_y, _, _ = ref_trajectory.get_position_at(t)
                
                # 方向朝向参考
                dx = ref_x - x
                dy = ref_y - y
                dist = np.sqrt(dx**2 + dy**2)
                if dist > 1.0:
                    dx, dy = dx/dist, dy/dist
                
                # 添加朝向参考的速度分量
                approach_vx = approach_strength * self.base_speed_mps * dx
                approach_vy = approach_strength * self.base_speed_mps * dy
            else:
                approach_vx = 0
                approach_vy = 0
            
            # 基础速度方向
            heading_rad = np.radians(base_heading + self.rng.uniform(-10, 10))
            vx_base = self.base_speed_mps * np.sin(heading_rad)
            vy_base = self.base_speed_mps * np.cos(heading_rad)
            
            # 总速度
            vx = vx_base + approach_vx
            vy = vy_base + approach_vy
            
            # 更新位置
            x_new = x + vx * dt
            y_new = y + vy * dt
            
            self.traj_timestamps.append(t)
            self.traj_x.append(x)
            self.traj_y.append(y)
            self.traj_vx.append(vx)
            self.traj_vy.append(vy)
            self.traj_heading.append(np.degrees(np.arctan2(vy, vx)))
            
            x, y = x_new, y_new
        
        return np.array(self.traj_timestamps), np.column_stack([self.traj_x, self.traj_y])


def load_reference_track_from_dfki(csv_path: str, track_id: str = 'PRIMARY_DIVER') -> ReferenceTrack:
    """
    从 DFKI 数据加载参考航迹
    
    参数:
        csv_path: tracks_raw.csv 路径
        track_id: 要提取的航迹ID
    """
    df = pd.read_csv(csv_path)
    
    # 筛选目标航迹
    track_df = df[df['track_id'] == track_id].copy()
    track_df = track_df[track_df['measurement_available'] == True]
    
    # 解析时间戳
    def parse_timestamp(ts_str):
        try:
            dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            return dt.timestamp()
        except:
            return float(ts_str)
    
    track_df['timestamp_s'] = track_df['timestamp_utc'].apply(parse_timestamp)
    
    # 使用 fan_u_meas_m 和 fan_v_meas_m 作为位置（米制）
    # 注意：DFKI 使用像素单位，配置中说明了 1 pixel ~= 0.015m
    # 但这里我们使用原始像素值作为相对位置
    
    # 如果有米制坐标，使用米制；否则使用像素
    if 'fan_u_meas_m' in track_df.columns:
        x = track_df['fan_u_meas_m'].values
        y = track_df['fan_v_meas_m'].values
    else:
        # 回退到像素坐标
        x = track_df['fan_u_pred_m'].values
        y = track_df['fan_v_pred_m'].values
    
    timestamps = track_df['timestamp_s'].values
    # 转换为相对时间
    timestamps = timestamps - timestamps[0]
    
    return ReferenceTrack(timestamps, x, y)


def load_reference_track_from_gt(csv_path: str) -> ReferenceTrack:
    """
    从 gt_evaluation.csv 加载参考航迹（使用 GT 位置）
    """
    df = pd.read_csv(csv_path)
    
    # 使用 GT 位置
    x = df['gt_x'].values
    y = df['gt_y'].values
    
    # 解析 frame_id 获取时间
    # 假设帧率约为 5 Hz
    timestamps = np.arange(len(x)) * 0.2
    
    return ReferenceTrack(timestamps, x, y)


def main():
    """主函数 - 测试航迹仿真"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Simulate two sonars from reference track')
    parser.add_argument('--config', type=str, 
                       default=DEFAULT_CONFIG,
                       help='Configuration file path')
    parser.add_argument('--input', type=str, 
                       default=DEFAULT_REFERENCE_TRACK,
                       help='Input tracks CSV file')
    parser.add_argument('--output', type=str,
                       default=str(OUTPUT_ROOT / 'experiment_easy'),
                       help='Output directory')
    parser.add_argument('--track-id', type=str, default='PRIMARY_DIVER',
                       help='Track ID to use as reference')
    parser.add_argument('--experiment', type=str, default='easy',
                       choices=['easy', 'difficult'],
                       help='Experiment type')
    parser.add_argument('--seed', type=int, default=None,
                       help='Random seed (overrides config)')
    
    args = parser.parse_args()
    
    # 加载配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # 获取随机种子
    seed = args.seed if args.seed is not None else config['random_seed']
    rng = np.random.default_rng(seed)
    
    # 加载参考航迹
    print(f"Loading reference track: {args.track_id}")
    reference = load_reference_track_from_dfki(args.input, args.track_id)
    print(f"Reference track loaded: {reference.length} points")
    print(f"Time range: {reference.timestamps[0]:.2f}s - {reference.timestamps[-1]:.2f}s")
    print(f"Position range X: [{reference.x.min():.1f}, {reference.x.max():.1f}]")
    print(f"Position range Y: [{reference.y.min():.1f}, {reference.y.max():.1f}]")
    
    # 保存参考航迹
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    reference.to_csv(output_dir / 'reference_track.csv')
    print(f"Reference track saved to {output_dir / 'reference_track.csv'}")
    
    # 创建输出目录
    figures_dir = output_dir / 'figures'
    figures_dir.mkdir(exist_ok=True)
    
    # 创建虚拟声纳
    sonar_a_config = config['sonar_A'].copy()
    sonar_b_config = config['sonar_B'].copy()
    
    # 根据实验类型调整配置
    if args.experiment == 'difficult':
        # 增加噪声和漏检
        sonar_a_config['sigma_range_m'] *= 1.5
        sonar_a_config['sigma_bearing_deg'] *= 1.5
        sonar_b_config['sigma_range_m'] *= 1.5
        sonar_b_config['sigma_bearing_deg'] *= 1.5
        sonar_a_config['miss_probability'] = 0.08
        sonar_b_config['miss_probability'] = 0.10
        print("Running DIFFICULT experiment with increased noise and miss rate")
    else:
        print("Running EASY experiment with low noise")
    
    sonar_a = VirtualSonar(sonar_a_config, 'SONAR_A', rng)
    sonar_b = VirtualSonar(sonar_b_config, 'SONAR_B', rng)
    
    # 生成测量
    print("\nGenerating sonar A measurements...")
    meas_a_df = sonar_a.simulate_from_reference(reference, add_noise=True)
    meas_a_df.to_csv(output_dir / 'sonar_A_measurements.csv', index=False)
    print(f"Sonar A: {len(meas_a_df)} measurements, "
          f"{meas_a_df['measurement_available'].sum()} available")
    
    print("Generating sonar B measurements...")
    meas_b_df = sonar_b.simulate_from_reference(reference, add_noise=True)
    meas_b_df.to_csv(output_dir / 'sonar_B_measurements.csv', index=False)
    print(f"Sonar B: {len(meas_b_df)} measurements, "
          f"{meas_b_df['measurement_available'].sum()} available")
    
    # 生成干扰航迹
    print("\nGenerating distractor tracks...")
    distractors = config.get('distractors', {})
    
    dist_a_records = []
    dist_b_records = []
    
    # A_DIST_01
    if 'A_DIST_01' in distractors:
        dist_rng_a = np.random.default_rng(seed + 100)
        dist_gen = DistractorTrajectory(distractors['A_DIST_01'], 'A_DIST_01', dist_rng_a)
        dist_t, dist_pos = dist_gen.generate(
            reference, reference.x[0], reference.y[0]
        )
        
        for i in range(len(dist_t)):
            meas = sonar_a.generate_measurement(dist_pos[i, 0], dist_pos[i, 1], add_noise=True)
            if meas:
                record = {
                    'timestamp': dist_t[i],
                    'sensor_id': 'SONAR_A',
                    'local_track_id': 'A_DIST_01',
                    'x': meas['x_common'],
                    'y': meas['y_common'],
                    'vx': 0, 'vy': 0,  # 简化处理
                    'measurement_available': True
                }
                dist_a_records.append(record)
    
    # A_DIST_02
    if 'A_DIST_02' in distractors:
        dist_rng_a2 = np.random.default_rng(seed + 200)
        dist_gen2 = DistractorTrajectory(distractors['A_DIST_02'], 'A_DIST_02', dist_rng_a2)
        dist_t2, dist_pos2 = dist_gen2.generate(
            reference, reference.x[0], reference.y[0]
        )
        
        for i in range(len(dist_t2)):
            meas = sonar_a.generate_measurement(dist_pos2[i, 0], dist_pos2[i, 1], add_noise=True)
            if meas:
                record = {
                    'timestamp': dist_t2[i],
                    'sensor_id': 'SONAR_A',
                    'local_track_id': 'A_DIST_02',
                    'x': meas['x_common'],
                    'y': meas['y_common'],
                    'vx': 0, 'vy': 0,
                    'measurement_available': True
                }
                dist_a_records.append(record)
    
    # B_DIST_01
    if 'B_DIST_01' in distractors:
        dist_rng_b = np.random.default_rng(seed + 300)
        dist_gen_b = DistractorTrajectory(distractors['B_DIST_01'], 'B_DIST_01', dist_rng_b)
        dist_t_b, dist_pos_b = dist_gen_b.generate(
            reference, reference.x[0], reference.y[0]
        )
        
        for i in range(len(dist_t_b)):
            meas = sonar_b.generate_measurement(dist_pos_b[i, 0], dist_pos_b[i, 1], add_noise=True)
            if meas:
                record = {
                    'timestamp': dist_t_b[i],
                    'sensor_id': 'SONAR_B',
                    'local_track_id': 'B_DIST_01',
                    'x': meas['x_common'],
                    'y': meas['y_common'],
                    'vx': 0, 'vy': 0,
                    'measurement_available': True
                }
                dist_b_records.append(record)
    
    # B_DIST_02
    if 'B_DIST_02' in distractors:
        dist_rng_b2 = np.random.default_rng(seed + 400)
        dist_gen_b2 = DistractorTrajectory(distractors['B_DIST_02'], 'B_DIST_02', dist_rng_b2)
        dist_t_b2, dist_pos_b2 = dist_gen_b2.generate(
            reference, reference.x[0], reference.y[0]
        )
        
        for i in range(len(dist_t_b2)):
            meas = sonar_b.generate_measurement(dist_pos_b2[i, 0], dist_pos_b2[i, 1], add_noise=True)
            if meas:
                record = {
                    'timestamp': dist_t_b2[i],
                    'sensor_id': 'SONAR_B',
                    'local_track_id': 'B_DIST_02',
                    'x': meas['x_common'],
                    'y': meas['y_common'],
                    'vx': 0, 'vy': 0,
                    'measurement_available': True
                }
                dist_b_records.append(record)
    
    print(f"Generated {len(dist_a_records)} A distractors, {len(dist_b_records)} B distractors")
    
    # 保存干扰航迹
    if dist_a_records:
        pd.DataFrame(dist_a_records).to_csv(output_dir / 'A_distractors.csv', index=False)
    if dist_b_records:
        pd.DataFrame(dist_b_records).to_csv(output_dir / 'B_distractors.csv', index=False)
    
    print("\n=== Simulation Complete ===")
    print(f"Output directory: {output_dir}")


if __name__ == '__main__':
    main()
