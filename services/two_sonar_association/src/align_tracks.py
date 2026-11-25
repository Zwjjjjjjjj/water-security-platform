"""
Track Alignment - Align Tracks from Two Sonars in Common Time Reference
将两个声纳的航迹对齐到公共时间参考框架
"""
import numpy as np
import pandas as pd
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG


@dataclass
class AlignedTrackPair:
    """对齐的航迹对"""
    timestamp: float
    track_a_id: str
    track_b_id: str
    
    # Track A 状态
    Ax: float
    Ay: float
    Avx: float
    Avy: float
    APxx: float
    APyy: float
    
    # Track B 状态
    Bx: float
    By: float
    Bvx: float
    Bvy: float
    BPxx: float
    BPyy: float
    
    # 元数据
    time_gap_s: float
    has_A: bool
    has_B: bool
    
    # 距离指标
    position_distance: float = np.nan
    velocity_difference: float = np.nan
    heading_A: float = np.nan
    heading_B: float = np.nan
    heading_difference: float = np.nan


class TrackAligner:
    """航迹对齐器"""
    
    def __init__(self, config: Dict):
        """
        初始化航迹对齐器
        
        参数:
            config: 包含 max_time_gap_s 的配置
        """
        self.max_time_gap_s = config.get('max_time_gap_s', 0.15)
    
    def get_track_states_at_time(self, track_data: pd.DataFrame, 
                                 target_time: float,
                                 use_interpolation: bool = True) -> Optional[Dict]:
        """
        获取航迹在指定时间的估计状态
        
        参数:
            track_data: 航迹数据（包含 timestamp, x, y, vx, vy, Pxx, Pyy）
            target_time: 目标时间
            use_interpolation: 是否使用插值
        """
        timestamps = track_data['timestamp'].values
        
        # 找到最近的时刻
        if len(timestamps) == 0:
            return None
        
        # 精确匹配
        exact_idx = np.where(np.abs(timestamps - target_time) < 1e-6)[0]
        if len(exact_idx) > 0:
            idx = exact_idx[0]
            return {
                'x': track_data.iloc[idx]['x'],
                'y': track_data.iloc[idx]['y'],
                'vx': track_data.iloc[idx]['vx'],
                'vy': track_data.iloc[idx]['vy'],
                'Pxx': track_data.iloc[idx]['Pxx'],
                'Pyy': track_data.iloc[idx]['Pyy'],
                'timestamp': timestamps[idx],
                'is_predicted': not track_data.iloc[idx].get('measurement_available', True)
            }
        
        # 插值
        if use_interpolation:
            # 找到前后两个时刻
            before_idx = np.where(timestamps <= target_time)[0]
            after_idx = np.where(timestamps >= target_time)[0]
            
            if len(before_idx) == 0:
                # target_time 之前没有数据
                idx = after_idx[0]
                return {
                    'x': track_data.iloc[idx]['x'],
                    'y': track_data.iloc[idx]['y'],
                    'vx': track_data.iloc[idx]['vx'],
                    'vy': track_data.iloc[idx]['vy'],
                    'Pxx': track_data.iloc[idx]['Pxx'],
                    'Pyy': track_data.iloc[idx]['Pyy'],
                    'timestamp': timestamps[idx],
                    'is_predicted': True
                }
            
            if len(after_idx) == 0:
                # target_time 之后没有数据
                idx = before_idx[-1]
                return {
                    'x': track_data.iloc[idx]['x'],
                    'y': track_data.iloc[idx]['y'],
                    'vx': track_data.iloc[idx]['vx'],
                    'vy': track_data.iloc[idx]['vy'],
                    'Pxx': track_data.iloc[idx]['Pxx'],
                    'Pyy': track_data.iloc[idx]['Pyy'],
                    'timestamp': timestamps[idx],
                    'is_predicted': True
                }
            
            # 线性插值
            i1 = before_idx[-1]
            i2 = after_idx[0]
            
            t1, t2 = timestamps[i1], timestamps[i2]
            
            if abs(t2 - t1) < 1e-10:
                # 非常接近，使用前一个值
                return {
                    'x': track_data.iloc[i1]['x'],
                    'y': track_data.iloc[i1]['y'],
                    'vx': track_data.iloc[i1]['vx'],
                    'vy': track_data.iloc[i1]['vy'],
                    'Pxx': track_data.iloc[i1]['Pxx'],
                    'Pyy': track_data.iloc[i1]['Pyy'],
                    'timestamp': target_time,
                    'is_predicted': True
                }
            
            w = (target_time - t1) / (t2 - t1)
            
            x1, y1 = track_data.iloc[i1]['x'], track_data.iloc[i1]['y']
            x2, y2 = track_data.iloc[i2]['x'], track_data.iloc[i2]['y']
            vx1, vy1 = track_data.iloc[i1]['vx'], track_data.iloc[i1]['vy']
            vx2, vy2 = track_data.iloc[i2]['vx'], track_data.iloc[i2]['vy']
            
            return {
                'x': x1 + w * (x2 - x1),
                'y': y1 + w * (y2 - y1),
                'vx': vx1 + w * (vx2 - vx1),
                'vy': vy1 + w * (vy2 - vy1),
                'Pxx': track_data.iloc[i1]['Pxx'],  # 使用前一个的协方差
                'Pyy': track_data.iloc[i1]['Pyy'],
                'timestamp': target_time,
                'is_predicted': True
            }
        
        return None
    
    def align_two_tracks(self, track_a: pd.DataFrame, 
                         track_b: pd.DataFrame) -> pd.DataFrame:
        """
        对齐两个航迹
        
        返回:
            包含对齐状态的 DataFrame
        """
        # 获取公共时间范围
        t_min = max(track_a['timestamp'].min(), track_b['timestamp'].min())
        t_max = min(track_a['timestamp'].max(), track_b['timestamp'].max())
        
        # 生成公共时间点（使用较粗的步长以减少计算）
        dt = 0.1  # 100ms
        common_times = np.arange(t_min, t_max, dt)
        
        aligned_pairs = []
        
        # 为每个 Track A 中的唯一航迹
        track_ids_a = track_a['track_id'].unique()
        track_ids_b = track_b['track_id'].unique()
        
        for track_a_id in track_ids_a:
            track_a_data = track_a[track_a['track_id'] == track_a_id]
            
            for track_b_id in track_ids_b:
                track_b_data = track_b[track_b['track_id'] == track_b_id]
                
                for t in common_times:
                    state_a = self.get_track_states_at_time(track_a_data, t)
                    state_b = self.get_track_states_at_time(track_b_data, t)
                    
                    if state_a is None or state_b is None:
                        continue
                    
                    # 计算时间差
                    time_gap = abs(state_a['timestamp'] - state_b['timestamp'])
                    
                    if time_gap > self.max_time_gap_s:
                        continue
                    
                    # 计算距离
                    pos_dist = np.sqrt((state_a['x'] - state_b['x'])**2 + 
                                      (state_a['y'] - state_b['y'])**2)
                    vel_diff = np.sqrt((state_a['vx'] - state_b['vx'])**2 + 
                                       (state_a['vy'] - state_b['vy'])**2)
                    
                    # 计算航向
                    heading_a = np.degrees(np.arctan2(state_a['vy'], state_a['vx']))
                    heading_b = np.degrees(np.arctan2(state_b['vy'], state_b['vx']))
                    
                    # 角度差（处理wrap-around）
                    heading_diff = abs(heading_a - heading_b)
                    if heading_diff > 180:
                        heading_diff = 360 - heading_diff
                    
                    pair = AlignedTrackPair(
                        timestamp=t,
                        track_a_id=track_a_id,
                        track_b_id=track_b_id,
                        Ax=state_a['x'],
                        Ay=state_a['y'],
                        Avx=state_a['vx'],
                        Avy=state_a['vy'],
                        APxx=state_a['Pxx'],
                        APyy=state_a['Pyy'],
                        Bx=state_b['x'],
                        By=state_b['y'],
                        Bvx=state_b['vx'],
                        Bvy=state_b['vy'],
                        BPxx=state_b['Pxx'],
                        BPyy=state_b['Pyy'],
                        time_gap_s=time_gap,
                        has_A=True,
                        has_B=True,
                        position_distance=pos_dist,
                        velocity_difference=vel_diff,
                        heading_A=heading_a,
                        heading_B=heading_b,
                        heading_difference=heading_diff
                    )
                    
                    aligned_pairs.append(pair)
        
        if len(aligned_pairs) == 0:
            return pd.DataFrame()
        
        # 转换为 DataFrame
        df = pd.DataFrame([{
            'timestamp': p.timestamp,
            'track_a_id': p.track_a_id,
            'track_b_id': p.track_b_id,
            'Ax': p.Ax, 'Ay': p.Ay,
            'Avx': p.Avx, 'Avy': p.Avy,
            'Bx': p.Bx, 'By': p.By,
            'Bvx': p.Bvx, 'Bvy': p.Bvy,
            'time_gap': p.time_gap_s,
            'position_distance': p.position_distance,
            'velocity_difference': p.velocity_difference,
            'heading_A': p.heading_A,
            'heading_B': p.heading_B,
            'heading_difference': p.heading_difference
        } for p in aligned_pairs])
        
        return df
    
    def align_all_tracks(self, tracks_a: pd.DataFrame, 
                         tracks_b: pd.DataFrame) -> pd.DataFrame:
        """
        对齐所有航迹的所有时间点
        
        返回:
            对齐后的航迹对 DataFrame
        """
        aligned_pairs = []
        
        track_ids_a = tracks_a['track_id'].unique()
        track_ids_b = tracks_b['track_id'].unique()
        
        for track_a_id in track_ids_a:
            track_a_data = tracks_a[tracks_a['track_id'] == track_a_id]
            
            for track_b_id in track_ids_b:
                track_b_data = tracks_b[tracks_b['track_id'] == track_b_id]
                
                # 获取公共时间范围
                t_min_a = track_a_data['timestamp'].min()
                t_max_a = track_a_data['timestamp'].max()
                t_min_b = track_b_data['timestamp'].min()
                t_max_b = track_b_data['timestamp'].max()
                
                # 检查 NaN
                if pd.isna(t_min_a) or pd.isna(t_max_a) or pd.isna(t_min_b) or pd.isna(t_max_b):
                    continue
                
                t_min = max(t_min_a, t_min_b)
                t_max = min(t_max_a, t_max_b)
                
                if t_min >= t_max:
                    continue
                
                # 生成公共时间点
                dt = 0.1  # 100ms 步长
                # 安全地生成时间点
                n_points = int((t_max - t_min) / dt) + 1
                if n_points <= 0 or n_points > 10000:  # 限制点数
                    continue
                common_times = np.linspace(t_min, t_max, n_points)
                
                for t in common_times:
                    state_a = self.get_track_states_at_time(track_a_data, t)
                    state_b = self.get_track_states_at_time(track_b_data, t)
                    
                    if state_a is None and state_b is None:
                        continue
                    
                    record = {
                        'timestamp': t,
                        'track_a_id': track_a_id,
                        'track_b_id': track_b_id,
                        'time_gap': abs(state_a['timestamp'] - state_b['timestamp']) if state_a and state_b else np.nan
                    }
                    
                    if state_a:
                        record.update({
                            'Ax': state_a['x'], 'Ay': state_a['y'],
                            'Avx': state_a['vx'], 'Avy': state_a['vy'],
                            'has_A': True
                        })
                    else:
                        record.update({
                            'Ax': np.nan, 'Ay': np.nan,
                            'Avx': np.nan, 'Avy': np.nan,
                            'has_A': False
                        })
                    
                    if state_b:
                        record.update({
                            'Bx': state_b['x'], 'By': state_b['y'],
                            'Bvx': state_b['vx'], 'Bvy': state_b['vy'],
                            'has_B': True
                        })
                    else:
                        record.update({
                            'Bx': np.nan, 'By': np.nan,
                            'Bvx': np.nan, 'Bvy': np.nan,
                            'has_B': False
                        })
                    
                    # 计算距离
                    if state_a and state_b:
                        record['position_distance'] = np.sqrt(
                            (state_a['x'] - state_b['x'])**2 + 
                            (state_a['y'] - state_b['y'])**2
                        )
                        record['velocity_difference'] = np.sqrt(
                            (state_a['vx'] - state_b['vx'])**2 + 
                            (state_a['vy'] - state_b['vy'])**2
                        )
                        
                        # 航向
                        heading_a = np.degrees(np.arctan2(state_a['vy'], state_a['vx']))
                        heading_b = np.degrees(np.arctan2(state_b['vy'], state_b['vx']))
                        record['heading_A'] = heading_a
                        record['heading_B'] = heading_b
                        heading_diff = abs(heading_a - heading_b)
                        if heading_diff > 180:
                            heading_diff = 360 - heading_diff
                        record['heading_difference'] = heading_diff
                    else:
                        record['position_distance'] = np.nan
                        record['velocity_difference'] = np.nan
                        record['heading_A'] = np.nan
                        record['heading_B'] = np.nan
                        record['heading_difference'] = np.nan
                    
                    aligned_pairs.append(record)
        
        if len(aligned_pairs) == 0:
            return pd.DataFrame()
        
        return pd.DataFrame(aligned_pairs)


def align_tracks_main(tracks_a_path: str, tracks_b_path: str,
                      output_path: str, config: Dict):
    """
    主函数：对齐两组航迹
    """
    # 加载航迹
    tracks_a = pd.read_csv(tracks_a_path)
    tracks_b = pd.read_csv(tracks_b_path)
    
    print(f"Loaded {len(tracks_a)} track states from A")
    print(f"Loaded {len(tracks_b)} track states from B")
    print(f"Track IDs A: {tracks_a['track_id'].unique()}")
    print(f"Track IDs B: {tracks_b['track_id'].unique()}")
    
    # 创建对齐器
    aligner = TrackAligner(config['association'])
    
    # 对齐
    aligned_df = aligner.align_all_tracks(tracks_a, tracks_b)
    
    print(f"Generated {len(aligned_df)} aligned track pairs")
    
    # 保存
    aligned_df.to_csv(output_path, index=False)
    print(f"Saved aligned pairs to {output_path}")
    
    return aligned_df


def main():
    """命令行入口"""
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description='Align tracks from two sonars')
    parser.add_argument('--tracks-a', type=str, required=True,
                       help='Path to Sonar A tracks CSV')
    parser.add_argument('--tracks-b', type=str, required=True,
                       help='Path to Sonar B tracks CSV')
    parser.add_argument('--output', type=str, required=True,
                       help='Output aligned pairs CSV')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Config file')
    
    args = parser.parse_args()
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    align_tracks_main(args.tracks_a, args.tracks_b, args.output, config)


if __name__ == '__main__':
    main()
