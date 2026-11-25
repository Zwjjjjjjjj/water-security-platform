#!/usr/bin/env python3
"""
Run Two Sonar Track-to-Track Association
主运行脚本：执行完整的双声纳航迹关联流程

Usage:
    python run_two_sonar_association.py --experiment easy
    python run_two_sonar_association.py --experiment difficult
"""
import numpy as np
import pandas as pd
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent / 'src'))
from two_sonar_paths import OUTPUT_ROOT, DEFAULT_CONFIG, DEFAULT_REFERENCE_TRACK

from simulate_two_sonars import (
    ReferenceTrack, VirtualSonar, DistractorTrajectory,
    load_reference_track_from_dfki
)
from association_utils import KalmanFilter, normalize_angle


def setup_experiment(config: dict, experiment_type: str) -> dict:
    """
    根据实验类型调整配置
    """
    exp_config = config.copy()
    
    if experiment_type == 'easy':
        # Easy: 低噪声，低漏检率
        exp_config['sonar_A']['sigma_range_m'] *= 0.8
        exp_config['sonar_A']['sigma_bearing_deg'] *= 0.8
        exp_config['sonar_B']['sigma_range_m'] *= 0.8
        exp_config['sonar_B']['sigma_bearing_deg'] *= 0.8
        exp_config['sonar_A']['miss_probability'] = 0.02
        exp_config['sonar_B']['miss_probability'] = 0.03
        
    elif experiment_type == 'difficult':
        # Difficult: 高噪声，高漏检率
        exp_config['sonar_A']['sigma_range_m'] *= 1.5
        exp_config['sonar_A']['sigma_bearing_deg'] *= 1.5
        exp_config['sonar_B']['sigma_range_m'] *= 1.5
        exp_config['sonar_B']['sigma_bearing_deg'] *= 1.5
        exp_config['sonar_A']['miss_probability'] = 0.10
        exp_config['sonar_B']['miss_probability'] = 0.12
    
    exp_config['experiment_type'] = experiment_type
    return exp_config


def simple_kalman_track(measurements_df: pd.DataFrame, 
                        sensor_id: str,
                        track_id: str,
                        process_noise: float = 1.0,
                        measurement_noise: float = 2.0) -> pd.DataFrame:
    """
    简化的 Kalman 跟踪：直接跟踪测量点序列
    """
    # 只处理有效的测量
    valid_meas = measurements_df[measurements_df['measurement_available'] == True].copy()
    valid_meas = valid_meas.sort_values('timestamp').reset_index(drop=True)
    
    if len(valid_meas) == 0:
        return pd.DataFrame()
    
    # 初始化 Kalman 滤波器
    kf = KalmanFilter(
        process_noise_pos=process_noise,
        process_noise_vel=process_noise/2,
        measurement_noise_pos=measurement_noise,
        initial_covariance=10.0
    )
    
    records = []
    last_time = None
    
    for idx, row in valid_meas.iterrows():
        t = row['timestamp']
        x, y = row['x_common'], row['y_common']
        
        if not kf.is_initialized:
            kf.initialize(x, y, t)
            state = kf.x.copy()
            cov = kf.P.copy()
        else:
            if last_time is not None:
                dt = t - last_time
                if dt > 0:
                    kf.predict(dt)
            
            kf.update(np.array([x, y]))
            state = kf.x.copy()
            cov = kf.P.copy()
        
        records.append({
            'timestamp': t,
            'track_id': track_id,
            'sensor_id': sensor_id,
            'x': state[0],
            'y': state[1],
            'vx': state[2],
            'vy': state[3],
            'Pxx': cov[0, 0],
            'Pxy': cov[0, 1],
            'Pyy': cov[1, 1],
            'Pvxvx': cov[2, 2],
            'Pvyvy': cov[3, 3],
            'hits': kf.hits,
            'misses': kf.misses,
            'measurement_available': True,
            'track_status': 'CONFIRMED',
            'track_quality': 0.8
        })
        
        last_time = t
    
    return pd.DataFrame(records)


def run_experiment(input_csv: str, output_dir: str, 
                  config: dict, experiment_type: str,
                  track_id: str = 'PRIMARY_DIVER',
                  seed: int = None):
    """
    运行完整实验流程
    """
    print("=" * 60)
    print(f"TWO SONAR TRACK-TO-TRACK ASSOCIATION - {experiment_type.upper()} EXPERIMENT")
    print("=" * 60)
    print()
    
    # 设置随机种子
    if seed is None:
        seed = config.get('random_seed', 42)
    rng = np.random.default_rng(seed)
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figures_dir = output_path / 'figures'
    figures_dir.mkdir(parents=True, exist_ok=True)
    
    # ============================================================
    # Step 1: 加载参考航迹
    # ============================================================
    print("[Step 1] Loading reference track...")
    reference = load_reference_track_from_dfki(input_csv, track_id)
    print(f"  Reference track loaded: {reference.length} points")
    print(f"  Time range: {reference.timestamps[0]:.2f}s - {reference.timestamps[-1]:.2f}s")
    print(f"  Position X: [{reference.x.min():.1f}, {reference.x.max():.1f}]")
    print(f"  Position Y: [{reference.y.min():.1f}, {reference.y.max():.1f}]")
    
    # 保存参考航迹
    reference.to_csv(output_path / 'reference_track.csv')
    print(f"  Saved to {output_path / 'reference_track.csv'}")
    print()
    
    # ============================================================
    # Step 2: 生成虚拟声纳测量
    # ============================================================
    print("[Step 2] Generating virtual sonar measurements...")
    
    sonar_a_config = config['sonar_A']
    sonar_b_config = config['sonar_B']
    
    sonar_a = VirtualSonar(sonar_a_config, 'SONAR_A', rng)
    sonar_b = VirtualSonar(sonar_b_config, 'SONAR_B', rng)
    
    # 生成 A 测量
    meas_a_df = sonar_a.simulate_from_reference(reference, add_noise=True)
    meas_a_df['source_track_id'] = 'A_DIVER'
    meas_a_df.to_csv(output_path / 'sonar_A_measurements.csv', index=False)
    print(f"  Sonar A: {len(meas_a_df)} measurements, "
          f"{meas_a_df['measurement_available'].sum()} available")
    
    # 生成 B 测量
    meas_b_df = sonar_b.simulate_from_reference(reference, add_noise=True)
    meas_b_df['source_track_id'] = 'B_DIVER'
    meas_b_df.to_csv(output_path / 'sonar_B_measurements.csv', index=False)
    print(f"  Sonar B: {len(meas_b_df)} measurements, "
          f"{meas_b_df['measurement_available'].sum()} available")
    print()
    
    # ============================================================
    # Step 3: 生成干扰航迹
    # ============================================================
    print("[Step 3] Generating distractor tracks...")
    
    distractors = config.get('distractors', {})
    
    # 生成 A 干扰测量
    dist_a_meas = []
    for dist_name, dist_config in distractors.items():
        if dist_name.startswith('A_'):
            dist_rng = np.random.default_rng(seed + int(hash(dist_name) % 1000))
            dist_gen = DistractorTrajectory(dist_config, dist_name, dist_rng)
            t, pos = dist_gen.generate(reference, reference.x[0], reference.y[0])
            
            for i in range(len(t)):
                meas = sonar_a.generate_measurement(pos[i, 0], pos[i, 1], add_noise=True)
                if meas:
                    dist_a_meas.append({
                        'timestamp': t[i],
                        'sensor_id': 'SONAR_A',
                        'source_track_id': dist_name,
                        'range_m': meas['range_m'],
                        'bearing_deg': meas['bearing_deg'],
                        'x_common': meas['x_common'],
                        'y_common': meas['y_common'],
                        'measurement_available': True
                    })
    
    # 生成 B 干扰测量
    dist_b_meas = []
    for dist_name, dist_config in distractors.items():
        if dist_name.startswith('B_'):
            dist_rng = np.random.default_rng(seed + int(hash(dist_name) % 2000))
            dist_gen = DistractorTrajectory(dist_config, dist_name, dist_rng)
            t, pos = dist_gen.generate(reference, reference.x[0], reference.y[0])
            
            for i in range(len(t)):
                meas = sonar_b.generate_measurement(pos[i, 0], pos[i, 1], add_noise=True)
                if meas:
                    dist_b_meas.append({
                        'timestamp': t[i],
                        'sensor_id': 'SONAR_B',
                        'source_track_id': dist_name,
                        'range_m': meas['range_m'],
                        'bearing_deg': meas['bearing_deg'],
                        'x_common': meas['x_common'],
                        'y_common': meas['y_common'],
                        'measurement_available': True
                    })
    
    print(f"  Generated {len(dist_a_meas)} A distractors, {len(dist_b_meas)} B distractors")
    
    # 保存干扰测量
    if dist_a_meas:
        pd.DataFrame(dist_a_meas).to_csv(output_path / 'A_distractors.csv', index=False)
    if dist_b_meas:
        pd.DataFrame(dist_b_meas).to_csv(output_path / 'B_distractors.csv', index=False)
    print()
    
    # ============================================================
    # Step 4: 运行简单 Kalman 跟踪
    # ============================================================
    print("[Step 4] Running local tracking...")
    
    kalman_config = config['kalman_filter']
    meas_noise = kalman_config.get('measurement_noise_position_m', 2.0)
    proc_noise = kalman_config.get('process_noise_position_mps2', 1.0)
    
    # Sonar A 跟踪 - DIVER
    tracks_a_diver = simple_kalman_track(
        meas_a_df, 'SONAR_A', 'A_DIVER',
        process_noise=proc_noise,
        measurement_noise=meas_noise
    )
    
    # Sonar A 跟踪 - 干扰
    tracks_a_list = [tracks_a_diver]
    if dist_a_meas:
        dist_a_df = pd.DataFrame(dist_a_meas)
        for dist_name in dist_a_df['source_track_id'].unique():
            dist_track = simple_kalman_track(
                dist_a_df[dist_a_df['source_track_id'] == dist_name],
                'SONAR_A', dist_name,
                process_noise=proc_noise * 1.5,
                measurement_noise=meas_noise * 1.5
            )
            if len(dist_track) > 5:  # 只保留足够长的航迹
                tracks_a_list.append(dist_track)
    
    tracks_a_df = pd.concat(tracks_a_list, ignore_index=True) if tracks_a_list else pd.DataFrame()
    tracks_a_df.to_csv(output_path / 'sonar_A_tracks.csv', index=False)
    print(f"  Sonar A tracks: {tracks_a_df['track_id'].nunique()} tracks")
    print(f"  Track IDs A: {sorted(tracks_a_df['track_id'].unique())}")
    
    # Sonar B 跟踪 - DIVER
    tracks_b_diver = simple_kalman_track(
        meas_b_df, 'SONAR_B', 'B_DIVER',
        process_noise=proc_noise,
        measurement_noise=meas_noise
    )
    
    # Sonar B 跟踪 - 干扰
    tracks_b_list = [tracks_b_diver]
    if dist_b_meas:
        dist_b_df = pd.DataFrame(dist_b_meas)
        for dist_name in dist_b_df['source_track_id'].unique():
            dist_track = simple_kalman_track(
                dist_b_df[dist_b_df['source_track_id'] == dist_name],
                'SONAR_B', dist_name,
                process_noise=proc_noise * 1.5,
                measurement_noise=meas_noise * 1.5
            )
            if len(dist_track) > 5:
                tracks_b_list.append(dist_track)
    
    tracks_b_df = pd.concat(tracks_b_list, ignore_index=True) if tracks_b_list else pd.DataFrame()
    tracks_b_df.to_csv(output_path / 'sonar_B_tracks.csv', index=False)
    print(f"  Sonar B tracks: {tracks_b_df['track_id'].nunique()} tracks")
    print(f"  Track IDs B: {sorted(tracks_b_df['track_id'].unique())}")
    print()
    
    # ============================================================
    # Step 5: 时间对齐
    # ============================================================
    print("[Step 5] Aligning tracks in common time reference...")
    
    track_ids_a = tracks_a_df['track_id'].unique()
    track_ids_b = tracks_b_df['track_id'].unique()
    
    aligned_pairs = []
    
    for track_a_id in track_ids_a:
        track_a_data = tracks_a_df[tracks_a_df['track_id'] == track_a_id]
        
        for track_b_id in track_ids_b:
            track_b_data = tracks_b_df[tracks_b_df['track_id'] == track_b_id]
            
            # 获取公共时间范围
            t_min = max(track_a_data['timestamp'].min(), track_b_data['timestamp'].min())
            t_max = min(track_a_data['timestamp'].max(), track_b_data['timestamp'].max())
            
            if t_min >= t_max:
                continue
            
            # 采样点
            n_points = min(100, int((t_max - t_min) / 0.2) + 1)
            if n_points <= 0:
                continue
            common_times = np.linspace(t_min, t_max, n_points)
            
            for t in common_times:
                # 插值 A
                a_before = track_a_data[track_a_data['timestamp'] <= t]
                a_after = track_a_data[track_a_data['timestamp'] >= t]
                
                if len(a_before) == 0 or len(a_after) == 0:
                    continue
                
                a_idx_before = a_before['timestamp'].idxmax()
                a_idx_after = a_after['timestamp'].idxmin()
                
                a_row_before = track_a_data.loc[a_idx_before]
                a_row_after = track_a_data.loc[a_idx_after]
                
                if a_row_before['timestamp'] == a_row_after['timestamp']:
                    a_row = a_row_before
                else:
                    w = (t - a_row_before['timestamp']) / (a_row_after['timestamp'] - a_row_before['timestamp'])
                    a_row = a_row_before.copy()
                    for col in ['x', 'y', 'vx', 'vy', 'Pxx', 'Pyy', 'Pvxvx', 'Pvyvy']:
                        if col in track_a_data.columns:
                            a_row[col] = a_row_before[col] + w * (a_row_after[col] - a_row_before[col])
                
                # 插值 B
                b_before = track_b_data[track_b_data['timestamp'] <= t]
                b_after = track_b_data[track_b_data['timestamp'] >= t]
                
                if len(b_before) == 0 or len(b_after) == 0:
                    continue
                
                b_idx_before = b_before['timestamp'].idxmax()
                b_idx_after = b_after['timestamp'].idxmin()
                
                b_row_before = track_b_data.loc[b_idx_before]
                b_row_after = track_b_data.loc[b_idx_after]
                
                if b_row_before['timestamp'] == b_row_after['timestamp']:
                    b_row = b_row_before
                else:
                    w = (t - b_row_before['timestamp']) / (b_row_after['timestamp'] - b_row_before['timestamp'])
                    b_row = b_row_before.copy()
                    for col in ['x', 'y', 'vx', 'vy', 'Pxx', 'Pyy', 'Pvxvx', 'Pvyvy']:
                        if col in track_b_data.columns:
                            b_row[col] = b_row_before[col] + w * (b_row_after[col] - b_row_before[col])
                
                # 计算距离
                pos_dist = np.sqrt((a_row['x'] - b_row['x'])**2 + (a_row['y'] - b_row['y'])**2)
                vel_diff = np.sqrt((a_row['vx'] - b_row['vx'])**2 + (a_row['vy'] - b_row['vy'])**2)
                
                aligned_pairs.append({
                    'timestamp': t,
                    'track_a_id': track_a_id,
                    'track_b_id': track_b_id,
                    'Ax': a_row['x'], 'Ay': a_row['y'],
                    'Avx': a_row['vx'], 'Avy': a_row['vy'],
                    'APxx': a_row.get('Pxx', 1.0), 'APyy': a_row.get('Pyy', 1.0),
                    'Bx': b_row['x'], 'By': b_row['y'],
                    'Bvx': b_row['vx'], 'Bvy': b_row['vy'],
                    'BPxx': b_row.get('Pxx', 1.0), 'BPyy': b_row.get('Pyy', 1.0),
                    'time_gap': 0.0,
                    'position_distance': pos_dist,
                    'velocity_difference': vel_diff
                })
    
    aligned_df = pd.DataFrame(aligned_pairs)
    aligned_df.to_csv(output_path / 'aligned_track_pairs.csv', index=False)
    print(f"  Generated {len(aligned_df)} aligned track pairs")
    print()
    
    # ============================================================
    # Step 6: 计算航迹级关联代价
    # ============================================================
    print("[Step 6] Running track-to-track association...")
    
    assoc_config = config['association']
    
    # 计算每对航迹的代价
    cost_data = []
    
    for track_a_id in track_ids_a:
        for track_b_id in track_ids_b:
            pairs = aligned_df[
                (aligned_df['track_a_id'] == track_a_id) &
                (aligned_df['track_b_id'] == track_b_id)
            ]
            
            if len(pairs) == 0:
                continue
            
            # 计算统计量
            mean_pos_dist = pairs['position_distance'].mean()
            median_pos_dist = pairs['position_distance'].median()
            p90_pos_dist = pairs['position_distance'].quantile(0.9)
            mean_vel_diff = pairs['velocity_difference'].mean()
            
            # 门控通过率
            gate_pass = (pairs['position_distance'] < assoc_config.get('position_gate_m', 30.0)).mean()
            
            # 归一化代价
            pos_norm = assoc_config.get('cost_normalization', {}).get('position_threshold_m', 20.0)
            vel_norm = assoc_config.get('cost_normalization', {}).get('velocity_threshold_mps', 10.0)
            
            cost = min(1.0, mean_pos_dist / pos_norm)
            
            # 检查门控
            overall_pass = (
                mean_pos_dist < assoc_config.get('position_gate_m', 30.0) and
                mean_vel_diff < assoc_config.get('velocity_gate_mps', 15.0) and
                gate_pass >= assoc_config.get('min_gate_pass_ratio', 0.5)
            )
            
            cost_data.append({
                'track_a_id': track_a_id,
                'track_b_id': track_b_id,
                'num_aligned_points': len(pairs),
                'mean_position_distance': mean_pos_dist,
                'median_position_distance': median_pos_dist,
                'p90_position_distance': p90_pos_dist,
                'mean_velocity_difference': mean_vel_diff,
                'gate_pass_ratio': gate_pass,
                'cost': cost if overall_pass else 1.0,
                'overall_pass': overall_pass
            })
    
    cost_df = pd.DataFrame(cost_data)
    cost_df.to_csv(output_path / 'association_cost_matrix.csv', index=False)
    print(f"  Computed costs for {len(cost_df)} track pairs")
    
    # ============================================================
    # Step 7: Hungarian 分配
    # ============================================================
    from scipy.optimize import linear_sum_assignment
    
    if len(cost_df) > 0:
        # 构建代价矩阵
        n_a = len(track_ids_a)
        n_b = len(track_ids_b)
        cost_matrix = np.full((n_a, n_b), np.inf)
        
        for _, row in cost_df.iterrows():
            i = list(track_ids_a).index(row['track_a_id'])
            j = list(track_ids_b).index(row['track_b_id'])
            cost_matrix[i, j] = row['cost']
        
        print(f"  Cost matrix shape: {cost_matrix.shape}")
        
        # Hungarian
        try:
            row_ind, col_ind = linear_sum_assignment(cost_matrix)
            
            valid_associations = []
            for i, j in zip(row_ind, col_ind):
                if i < n_a and j < n_b:
                    cost = cost_matrix[i, j]
                    if cost < np.inf and cost < assoc_config.get('association_threshold', 0.6):
                        track_a = track_ids_a[i]
                        track_b = track_ids_b[j]
                        
                        # 验证 overall_pass
                        pair_cost = cost_df[
                            (cost_df['track_a_id'] == track_a) &
                            (cost_df['track_b_id'] == track_b)
                        ]
                        
                        if len(pair_cost) > 0 and pair_cost.iloc[0]['overall_pass']:
                            valid_associations.append({
                                'track_a_id': track_a,
                                'track_b_id': track_b,
                                'cost': cost,
                                'status': 'CONFIRMED',
                                'mean_position_distance': pair_cost.iloc[0]['mean_position_distance'],
                                'mean_velocity_difference': pair_cost.iloc[0]['mean_velocity_difference'],
                                'gate_pass_ratio': pair_cost.iloc[0]['gate_pass_ratio']
                            })
            
            associations_df = pd.DataFrame(valid_associations)
            print(f"  Hungarian assignments: {len(valid_associations)}")
            
            for assoc in valid_associations:
                print(f"    {assoc['track_a_id']} <-> {assoc['track_b_id']}: cost={assoc['cost']:.4f}")
            
        except Exception as e:
            print(f"  Hungarian failed: {e}")
            associations_df = pd.DataFrame()
    else:
        associations_df = pd.DataFrame()
    
    if len(associations_df) > 0:
        associations_df.to_csv(output_path / 'association_results.csv', index=False)
    print()
    
    # ============================================================
    # Step 8: 评估
    # ============================================================
    print("[Step 8] Evaluating results...")
    
    # GT 映射
    ground_truth = {
        'A_DIVER': 'B_DIVER'
    }
    
    # 计算指标
    num_gt = len([v for v in ground_truth.values() if v is not None])
    
    if len(associations_df) > 0:
        true_assoc = sum(1 for _, row in associations_df.iterrows() 
                        if ground_truth.get(row['track_a_id']) == row['track_b_id'])
        false_assoc = len(associations_df) - true_assoc
    else:
        true_assoc = 0
        false_assoc = 0
    
    missed_assoc = num_gt - true_assoc
    
    # DIVER 距离统计
    diver_pairs = cost_df[
        cost_df['track_a_id'].str.contains('DIVER') & 
        cost_df['track_b_id'].str.contains('DIVER')
    ]
    
    if len(diver_pairs) > 0:
        diver_mean_dist = diver_pairs['mean_position_distance'].iloc[0]
        diver_median_dist = diver_pairs['median_position_distance'].iloc[0]
        diver_p90_dist = diver_pairs['p90_position_distance'].iloc[0]
    else:
        diver_mean_dist = np.nan
        diver_median_dist = np.nan
        diver_p90_dist = np.nan
    
    results = {
        'experiment_type': experiment_type,
        'random_seed': seed,
        'sonar_A_config': config['sonar_A'],
        'sonar_B_config': config['sonar_B'],
        'metrics': {
            'num_associations': len(associations_df),
            'num_correct': true_assoc,
            'num_incorrect': false_assoc,
            'num_missed': missed_assoc,
            'num_gt_pairs': num_gt,
            'true_association_rate': true_assoc / num_gt if num_gt > 0 else 0.0,
            'false_association_rate': false_assoc / max(1, len(associations_df)),
            'missed_association_rate': missed_assoc / num_gt if num_gt > 0 else 0.0,
            'association_precision': true_assoc / max(1, len(associations_df)),
            'association_recall': true_assoc / num_gt if num_gt > 0 else 0.0,
            'diver_mean_distance': diver_mean_dist,
            'diver_median_distance': diver_median_dist,
            'diver_p90_distance': diver_p90_dist
        }
    }
    
    # 保存评估结果
    with open(output_path / 'evaluation_summary.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    # 生成报告
    report = []
    report.append("=" * 60)
    report.append("TRACK-TO-TRACK ASSOCIATION EVALUATION REPORT")
    report.append("=" * 60)
    report.append(f"Experiment Type: {experiment_type}")
    report.append(f"Random Seed: {seed}")
    report.append("")
    report.append("METRICS:")
    report.append(f"  Total Associations: {results['metrics']['num_associations']}")
    report.append(f"  Correct Associations: {results['metrics']['num_correct']}")
    report.append(f"  Incorrect Associations: {results['metrics']['num_incorrect']}")
    report.append(f"  Missed Associations: {results['metrics']['num_missed']}")
    report.append(f"  True Association Rate: {results['metrics']['true_association_rate']:.2%}")
    report.append(f"  False Association Rate: {results['metrics']['false_association_rate']:.2%}")
    report.append(f"  Missed Association Rate: {results['metrics']['missed_association_rate']:.2%}")
    report.append(f"  Association Precision: {results['metrics']['association_precision']:.2%}")
    report.append(f"  Association Recall: {results['metrics']['association_recall']:.2%}")
    report.append("")
    report.append("DIVER-DIVER DISTANCE:")
    report.append(f"  Mean: {diver_mean_dist:.2f}" if not np.isnan(diver_mean_dist) else "  Mean: N/A")
    report.append(f"  Median: {diver_median_dist:.2f}" if not np.isnan(diver_median_dist) else "  Median: N/A")
    report.append(f"  P90: {diver_p90_dist:.2f}" if not np.isnan(diver_p90_dist) else "  P90: N/A")
    report.append("=" * 60)
    
    report_text = "\n".join(report)
    print(report_text)
    
    with open(output_path / 'evaluation_report.txt', 'w') as f:
        f.write(report_text)
    
    # ============================================================
    # Step 9: 可视化
    # ============================================================
    print("\n[Step 9] Generating visualizations...")
    
    try:
        from plot_association import plot_all_results
        
        # 创建空的 history dataframe
        history_df = pd.DataFrame()
        
        plot_all_results(
            str(output_path), config,
            reference.to_dataframe(),
            tracks_a_df, tracks_b_df,
            associations_df, cost_df,
            aligned_df, history_df,
            f"{experiment_type.upper()} Experiment"
        )
    except Exception as e:
        print(f"  Warning: Plotting failed with error: {e}")
        import traceback
        traceback.print_exc()
    
    print()
    
    # ============================================================
    # 完成
    # ============================================================
    print("=" * 60)
    print("EXPERIMENT COMPLETE")
    print("=" * 60)
    print(f"Output directory: {output_path}")
    print()
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description='Run Two Sonar Track-to-Track Association'
    )
    parser.add_argument('--experiment', type=str, default='easy',
                       choices=['easy', 'difficult'],
                       help='Experiment type')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Configuration file')
    parser.add_argument('--input', type=str,
                       default=DEFAULT_REFERENCE_TRACK,
                       help='Input tracks CSV')
    parser.add_argument('--output', type=str, default=None,
                       help='Output directory (default: outputs/experiment_X)')
    parser.add_argument('--track-id', type=str, default='PRIMARY_DIVER',
                       help='Track ID to use as reference')
    parser.add_argument('--seed', type=int, default=None,
                       help='Random seed (overrides config)')
    
    args = parser.parse_args()
    
    # 加载配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # 设置实验配置
    config = setup_experiment(config, args.experiment)
    
    # 设置随机种子
    if args.seed is not None:
        config['random_seed'] = args.seed
    
    # 设置输出目录
    if args.output is None:
        args.output = str(OUTPUT_ROOT / f'experiment_{args.experiment}')
    
    # 运行实验
    results = run_experiment(
        input_csv=args.input,
        output_dir=args.output,
        config=config,
        experiment_type=args.experiment,
        track_id=args.track_id,
        seed=args.seed
    )
    
    # 打印关键指标
    print("\n" + "=" * 60)
    print("KEY METRICS SUMMARY")
    print("=" * 60)
    metrics = results.get('metrics', {})
    print(f"True Association Rate: {metrics.get('true_association_rate', 0):.2%}")
    print(f"False Association Rate: {metrics.get('false_association_rate', 0):.2%}")
    print(f"Missed Association Rate: {metrics.get('missed_association_rate', 0):.2%}")
    print(f"Association Precision: {metrics.get('association_precision', 0):.2%}")
    print(f"Association Recall: {metrics.get('association_recall', 0):.2%}")
    print(f"DIVER-DIVER Mean Distance: {metrics.get('diver_mean_distance', np.nan):.2f}")


if __name__ == '__main__':
    main()
