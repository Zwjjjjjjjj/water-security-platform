"""
Local Track Adapter - Run Independent Kalman Trackers on Sonar Measurements
在每个声纳的测量数据上独立运行 Kalman 跟踪器
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG
from association_utils import KalmanFilter


@dataclass
class LocalTrack:
    """本地航迹"""
    track_id: str
    sensor_id: str
    states: List[np.ndarray] = field(default_factory=list)
    covariances: List[np.ndarray] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    measurements: List[np.ndarray] = field(default_factory=list)
    measurement_available: List[bool] = field(default_factory=list)
    hits: int = 0
    misses: int = 0
    is_confirmed: bool = False
    is_coasted: bool = False
    
    def add_state(self, timestamp: float, state: np.ndarray, 
                  covariance: np.ndarray, measurement: Optional[np.ndarray] = None,
                  has_measurement: bool = True):
        """添加一个状态"""
        self.timestamps.append(timestamp)
        self.states.append(state)
        self.covariances.append(covariance)
        self.measurements.append(measurement if measurement is not None else np.array([np.nan, np.nan]))
        self.measurement_available.append(has_measurement)
        
        if has_measurement:
            self.hits += 1
        else:
            self.misses += 1
    
    def get_position_at(self, idx: int) -> Tuple[float, float]:
        """获取指定索引的位置"""
        if idx < len(self.states):
            return self.states[idx][0], self.states[idx][1]
        return np.nan, np.nan
    
    def get_velocity_at(self, idx: int) -> Tuple[float, float]:
        """获取指定索引的速度"""
        if idx < len(self.states):
            return self.states[idx][2], self.states[idx][3]
        return np.nan, np.nan
    
    def to_dataframe(self) -> pd.DataFrame:
        """转换为 DataFrame"""
        records = []
        for i in range(len(self.timestamps)):
            state = self.states[i]
            cov = self.covariances[i]
            
            record = {
                'timestamp': self.timestamps[i],
                'track_id': self.track_id,
                'sensor_id': self.sensor_id,
                'x': state[0] if len(state) > 0 else np.nan,
                'y': state[1] if len(state) > 1 else np.nan,
                'vx': state[2] if len(state) > 2 else np.nan,
                'vy': state[3] if len(state) > 3 else np.nan,
                'Pxx': cov[0, 0] if cov.shape[0] > 0 else np.nan,
                'Pxy': cov[0, 1] if cov.shape[0] > 0 else np.nan,
                'Pyy': cov[1, 1] if cov.shape[0] > 0 else np.nan,
                'Pvxvx': cov[2, 2] if cov.shape[0] > 2 else np.nan,
                'Pvyvy': cov[3, 3] if cov.shape[0] > 3 else np.nan,
                'hits': self.hits,
                'misses': self.misses,
                'measurement_available': self.measurement_available[i],
                'track_status': 'CONFIRMED' if self.is_confirmed else ('TENTATIVE' if self.hits >= 3 else 'INIT'),
                'track_quality': min(1.0, self.hits / max(1, self.hits + self.misses))
            }
            records.append(record)
        
        return pd.DataFrame(records)


class LocalTracker:
    """本地航迹跟踪器"""
    
    def __init__(self, sensor_id: str, config: Dict, track_id_prefix: str = ''):
        """
        初始化本地跟踪器
        
        参数:
            sensor_id: 传感器ID
            config: 跟踪器配置
            track_id_prefix: 航迹ID前缀
        """
        self.sensor_id = sensor_id
        self.track_id_prefix = track_id_prefix
        
        # Kalman 滤波器参数
        self.process_noise_pos = config.get('process_noise_position_mps2', 1.0)
        self.process_noise_vel = config.get('process_noise_velocity_mps2', 0.5)
        self.measurement_noise = config.get('measurement_noise_position_m', 2.0)
        self.initial_covariance = config.get('initial_state_covariance', 10.0)
        
        # 航迹管理参数
        self.min_hits_to_confirm = config.get('min_hits_to_confirm', 3)
        self.max_coast_frames = config.get('max_coast_frames', 3)
        self.max_misses_before_delete = config.get('max_misses_before_delete', 5)
        self.gating_threshold = config.get('gating_threshold', 9.21)  # Chi-squared 95%
        
        # 活跃航迹
        self.tracks: Dict[str, LocalTrack] = {}
        self.next_track_id = 1
        
        # 测量噪声参数（用于门控）
        self.measurement_noise_pos = config.get('measurement_noise_position_m', 2.0)
    
    def create_new_track(self, timestamp: float, measurement: np.ndarray) -> LocalTrack:
        """创建新航迹"""
        track_id = f"{self.track_id_prefix}{self.sensor_id}_{self.next_track_id:03d}"
        self.next_track_id += 1
        
        track = LocalTrack(track_id=track_id, sensor_id=self.sensor_id)
        
        # 初始化 Kalman 滤波器
        kf = KalmanFilter(
            process_noise_pos=self.process_noise_pos,
            process_noise_vel=self.process_noise_vel,
            measurement_noise_pos=self.measurement_noise,
            initial_covariance=self.initial_covariance
        )
        
        # 使用第一个测量初始化
        kf.initialize(measurement[0], measurement[1], timestamp)
        
        track.add_state(timestamp, kf.x.copy(), kf.P.copy(), measurement, True)
        track.kf = kf
        
        self.tracks[track_id] = track
        return track
    
    def predict_all_tracks(self, timestamp: float):
        """预测所有活跃航迹"""
        for track in self.tracks.values():
            if not track.is_confirmed and track.hits < self.min_hits_to_confirm:
                # 未确认航迹不预测
                continue
            
            # 计算时间间隔
            dt = timestamp - track.timestamps[-1]
            
            # 预测
            kf = track.kf
            kf.predict(dt)
            
            # 添加预测状态
            track.add_state(
                timestamp, 
                kf.x.copy(), 
                kf.P.copy(), 
                measurement=None,
                has_measurement=False
            )
            track.is_coasted = True
    
    def update_track_with_measurement(self, track: LocalTrack, 
                                      timestamp: float, 
                                      measurement: np.ndarray,
                                      dt: float = None):
        """使用测量值更新航迹"""
        kf = track.kf
        
        # 预测（如果需要）
        if dt is not None and dt > 0:
            kf.predict(dt)
        
        # 更新
        kf.update(measurement)
        
        # 添加更新后的状态
        track.add_state(
            timestamp,
            kf.x.copy(),
            kf.P.copy(),
            measurement,
            True
        )
        track.is_coasted = False
        
        # 检查确认状态
        if track.hits >= self.min_hits_to_confirm:
            track.is_confirmed = True
    
    def gate_measurement(self, track: LocalTrack, measurement: np.ndarray) -> bool:
        """门控检查 - 判断测量是否属于该航迹"""
        # 使用 Mahalanobis 距离
        # H @ x 得到预测的位置 [x, y]
        predicted_pos = track.kf.H @ track.kf.x
        innovation = measurement - predicted_pos
        
        # 创新协方差: H @ P @ H^T + R
        P_pos = track.kf.P[:2, :2]  # 位置部分的协方差
        S = track.kf.H @ track.kf.P @ track.kf.H.T + track.kf.R
        
        # Mahalanobis 距离
        try:
            S_inv = np.linalg.inv(S)
            d2 = innovation @ S_inv @ innovation
            return d2 < self.gating_threshold
        except np.linalg.LinAlgError:
            # 使用欧氏距离作为后备
            d = np.sqrt(np.sum(innovation**2))
            return d < 3 * self.measurement_noise_pos
    
    def process_measurements(self, measurements: pd.DataFrame) -> List[LocalTrack]:
        """
        处理一组测量数据
        
        参数:
            measurements: DataFrame，包含 timestamp, x_common, y_common, measurement_available
        """
        # 按时间排序
        measurements = measurements.sort_values('timestamp').reset_index(drop=True)
        
        # 获取所有时间点
        all_timestamps = sorted(measurements['timestamp'].unique())
        
        for timestamp in all_timestamps:
            # 获取该时间点的测量
            meas_mask = measurements['timestamp'] == timestamp
            time_meas = measurements[meas_mask].copy()
            
            # 获取有效的测量
            valid_meas = time_meas[time_meas['measurement_available'] == True]
            
            if len(valid_meas) == 0:
                # 纯预测
                continue
            
            # 预测所有航迹
            self.predict_all_tracks(timestamp)
            
            # 为每个测量分配航迹
            assigned_tracks = set()
            
            for _, meas_row in valid_meas.iterrows():
                meas = np.array([meas_row['x_common'], meas_row['y_common']])
                
                # 检查是否匹配已有航迹
                best_track = None
                best_distance = float('inf')
                
                for track_id, track in self.tracks.items():
                    if track_id in assigned_tracks:
                        continue
                    
                    if not track.is_confirmed and track.hits >= 1:
                        # 只考虑已确认或有初始点的航迹
                        pass
                    
                    if self.gate_measurement(track, meas):
                        # 计算距离
                        pred_x, pred_y = track.kf.get_position()
                        dist = np.sqrt((meas[0] - pred_x)**2 + (meas[1] - pred_y)**2)
                        
                        if dist < best_distance:
                            best_distance = dist
                            best_track = track_id
                
                if best_track is not None:
                    # 更新已有航迹
                    dt = timestamp - self.tracks[best_track].timestamps[-1]
                    self.update_track_with_measurement(
                        self.tracks[best_track],
                        timestamp, meas, dt
                    )
                    assigned_tracks.add(best_track)
                else:
                    # 创建新航迹
                    new_track = self.create_new_track(timestamp, meas)
                    assigned_tracks.add(new_track.track_id)
            
            # 删除过期的航迹
            self.delete_stale_tracks()
        
        return list(self.tracks.values())
    
    def delete_stale_tracks(self):
        """删除过时航迹"""
        to_delete = []
        
        for track_id, track in self.tracks.items():
            if track.misses > self.max_misses_before_delete:
                to_delete.append(track_id)
        
        for track_id in to_delete:
            del self.tracks[track_id]
    
    def get_all_tracks_dataframe(self) -> pd.DataFrame:
        """获取所有航迹的 DataFrame"""
        all_records = []
        
        for track in self.tracks.values():
            df = track.to_dataframe()
            all_records.append(df)
        
        if len(all_records) > 0:
            return pd.concat(all_records, ignore_index=True)
        return pd.DataFrame()


def run_local_tracking(measurements_df: pd.DataFrame, 
                       sensor_id: str,
                       tracker_config: Dict,
                       track_id_prefix: str = '') -> pd.DataFrame:
    """
    运行本地航迹跟踪
    
    参数:
        measurements_df: 测量数据 DataFrame
        sensor_id: 传感器ID
        tracker_config: 跟踪器配置
        track_id_prefix: 航迹ID前缀
    
    返回:
        包含所有本地航迹的 DataFrame
    """
    tracker = LocalTracker(sensor_id, tracker_config, track_id_prefix)
    
    # 处理测量
    tracks = tracker.process_measurements(measurements_df)
    
    # 获取结果
    result_df = tracker.get_all_tracks_dataframe()
    
    return result_df


def main():
    """测试函数"""
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description='Run local tracking on sonar measurements')
    parser.add_argument('--input', type=str, required=True,
                       help='Input measurements CSV')
    parser.add_argument('--output', type=str, required=True,
                       help='Output tracks CSV')
    parser.add_argument('--sensor-id', type=str, default='SONAR_A',
                       help='Sensor ID')
    parser.add_argument('--config', type=str, 
                       default=DEFAULT_CONFIG,
                       help='Config file')
    
    args = parser.parse_args()
    
    # 加载配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # 加载测量
    measurements = pd.read_csv(args.input)
    
    print(f"Processing {len(measurements)} measurements from {args.sensor_id}")
    
    # 运行跟踪
    tracks_df = run_local_tracking(
        measurements,
        args.sensor_id,
        config['kalman_filter'],
        ''
    )
    
    # 保存结果
    tracks_df.to_csv(args.output, index=False)
    print(f"Saved {len(tracks_df)} track states to {args.output}")
    print(f"Number of tracks: {tracks_df['track_id'].nunique()}")


if __name__ == '__main__':
    main()
