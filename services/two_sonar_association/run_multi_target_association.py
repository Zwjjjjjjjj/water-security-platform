#!/usr/bin/env python3
"""
Run Multi-Target Two Sonar Track-to-Track Association
多目标双声纳航迹级关联主运行脚本

支持场景:
- easy: 1 diver + distractors（regression test）
- difficult: 1 diver + distractors，高噪声
- crossing_2diver: 2 diver 交叉场景
- parallel_2diver: 2 diver 平行场景
- three_diver: 3 diver 多目标压力测试
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
from multi_target_scenario import (
    MultiTargetScenario, create_parallel_diver_scenario,
    create_crossing_diver_scenario, create_three_diver_scenario
)
from association_manager import (
    TrackCentricAssociationManager, process_aligned_pairs_with_association
)
from global_track_manager import GlobalTrackManager


def setup_experiment(config: dict, experiment_type: str) -> dict:
    """根据实验类型调整配置"""
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
    
    elif experiment_type == 'crossing_2diver':
        # 2-diver crossing: 中等噪声
        exp_config['sonar_A']['sigma_range_m'] *= 1.0
        exp_config['sonar_A']['sigma_bearing_deg'] *= 1.0
        exp_config['sonar_B']['sigma_range_m'] *= 1.0
        exp_config['sonar_B']['sigma_bearing_deg'] *= 1.0
        exp_config['sonar_A']['miss_probability'] = 0.05
        exp_config['sonar_B']['miss_probability'] = 0.06
        
    elif experiment_type == 'parallel_2diver':
        # 2-diver parallel: 低噪声
        exp_config['sonar_A']['sigma_range_m'] *= 0.8
        exp_config['sonar_A']['sigma_bearing_deg'] *= 0.8
        exp_config['sonar_B']['sigma_range_m'] *= 0.8
        exp_config['sonar_B']['sigma_bearing_deg'] *= 0.8
        exp_config['sonar_A']['miss_probability'] = 0.03
        exp_config['sonar_B']['miss_probability'] = 0.04
    
    elif experiment_type == 'three_diver':
        # 3-diver: 中等噪声
        exp_config['sonar_A']['sigma_range_m'] *= 1.0
        exp_config['sonar_A']['sigma_bearing_deg'] *= 1.0
        exp_config['sonar_B']['sigma_range_m'] *= 1.0
        exp_config['sonar_B']['sigma_bearing_deg'] *= 1.0
        exp_config['sonar_A']['miss_probability'] = 0.04
        exp_config['sonar_B']['miss_probability'] = 0.05
    
    exp_config['experiment_type'] = experiment_type
    return exp_config


def simple_kalman_track(measurements_df: pd.DataFrame, 
                        sensor_id: str,
                        track_id: str,
                        process_noise: float = 1.0,
                        measurement_noise: float = 2.0) -> pd.DataFrame:
    """简化的 Kalman 跟踪"""
    valid_meas = measurements_df[measurements_df['measurement_available'] == True].copy()
    valid_meas = valid_meas.sort_values('timestamp').reset_index(drop=True)
    
    if len(valid_meas) == 0:
        return pd.DataFrame()
    
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


def run_multi_target_experiment(input_csv: str, output_dir: str,
                                 config: dict, experiment_type: str,
                                 track_id: str = 'PRIMARY_DIVER',
                                 seed: int = None,
                                 n_divers: int = 2):
    """
    运行多目标航迹关联实验
    
    通用流程：
    1. 加载参考航迹
    2. 生成多目标场景
    3. 生成独立虚拟声纳测量（每个声纳独立 RNG）
    4. 每个目标的测量独立运行 Kalman Tracker
    5. 所有 Local Track 平等进入关联（不区分 DIVER/DIST）
    6. N×M Cost Matrix + Hungarian
    7. Track-Centric Association Manager 维护状态
    8. Global Track Manager 维护跨时间跨声纳的 GLOBAL_ID
    9. 评估（含 ID switch 检测）
    """
    print("=" * 70)
    print(f"MULTI-TARGET TWO SONAR ASSOCIATION - {experiment_type.upper()}")
    print("=" * 70)
    print()
    
    if seed is None:
        seed = config.get('random_seed', 42)
    rng = np.random.default_rng(seed)
    
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figures_dir = output_path / 'figures'
    figures_dir.mkdir(exist_ok=True)
    
    # ============================================================
    # Step 1: 加载参考航迹 + 构建多目标场景
    # ============================================================
    print("[Step 1] Building multi-target scenario...")
    reference = load_reference_track_from_dfki(input_csv, track_id)
    print(f"  Reference track loaded: {reference.length} points")
    
    # 构建场景
    if experiment_type in ['parallel_2diver', 'crossing_2diver', 'three_diver']:
        if experiment_type == 'parallel_2diver':
            scenario = create_parallel_diver_scenario(reference, seed)
        elif experiment_type == 'crossing_2diver':
            scenario = create_crossing_diver_scenario(reference, seed)
        else:  # three_diver
            scenario = create_three_diver_scenario(reference, seed)
        
        # 保存多目标参考航迹
        for tid, traj in scenario.targets.items():
            traj.to_csv(output_path / f'reference_track_{tid}.csv')
        
        print(f"  Targets in scenario: {scenario.get_target_ids()}")
    
    else:
        # 单目标场景（easy/difficult）使用单目标
        scenario = MultiTargetScenario(seed=seed)
        scenario.add_target('T1', reference)
        reference.to_csv(output_path / 'reference_track.csv')
        print(f"  Targets in scenario: ['T1']")
    
    target_ids = scenario.get_target_ids()
    print()
    
    # ============================================================
    # Step 2: 生成两个独立虚拟声纳的测量
    # ============================================================
    print("[Step 2] Generating virtual sonar measurements...")
    
    sonar_a_config = config['sonar_A']
    sonar_b_config = config['sonar_B']
    
    # A 和 B 使用独立的 RNG（保证测量噪声独立）
    sonar_a_rng = np.random.default_rng(seed + 100)
    sonar_b_rng = np.random.default_rng(seed + 200)
    
    sonar_a = VirtualSonar(sonar_a_config, 'SONAR_A', sonar_a_rng)
    sonar_b = VirtualSonar(sonar_b_config, 'SONAR_B', sonar_b_rng)
    
    # A 测量
    meas_a_records = []
    for target_id in target_ids:
        traj = scenario.targets[target_id]
        measurement_times = sonar_a.generate_measurement_times(
            traj.timestamps[0], traj.timestamps[-1]
        )
        for t in measurement_times:
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
    
    meas_a_df = pd.DataFrame(meas_a_records)
    meas_a_df.to_csv(output_path / 'sonar_A_measurements.csv', index=False)
    print(f"  Sonar A: {len(meas_a_df)} measurements, "
          f"{meas_a_df['measurement_available'].sum()} available")
    
    # B 测量
    meas_b_records = []
    for target_id in target_ids:
        traj = scenario.targets[target_id]
        measurement_times = sonar_b.generate_measurement_times(
            traj.timestamps[0], traj.timestamps[-1]
        )
        for t in measurement_times:
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
    
    meas_b_df = pd.DataFrame(meas_b_records)
    meas_b_df.to_csv(output_path / 'sonar_B_measurements.csv', index=False)
    print(f"  Sonar B: {len(meas_b_df)} measurements, "
          f"{meas_b_df['measurement_available'].sum()} available")
    print()
    
    # ============================================================
    # Step 3: 生成干扰航迹（distractors）
    # ============================================================
    print("[Step 3] Generating distractor tracks...")
    
    distractors = config.get('distractors', {})
    
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
                        'x_common': meas['x_common'],
                        'y_common': meas['y_common'],
                        'measurement_available': True
                    })
    
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
                        'x_common': meas['x_common'],
                        'y_common': meas['y_common'],
                        'measurement_available': True
                    })
    
    print(f"  Generated {len(dist_a_meas)} A distractors, {len(dist_b_meas)} B distractors")
    if dist_a_meas:
        pd.DataFrame(dist_a_meas).to_csv(output_path / 'A_distractors.csv', index=False)
    if dist_b_meas:
        pd.DataFrame(dist_b_meas).to_csv(output_path / 'B_distractors.csv', index=False)
    print()
    
    # ============================================================
    # Step 4: 运行本地跟踪 - 每个测量序列独立跟踪
    # ============================================================
    print("[Step 4] Running local tracking...")
    
    kalman_config = config['kalman_filter']
    meas_noise = kalman_config.get('measurement_noise_position_m', 2.0)
    proc_noise = kalman_config.get('process_noise_position_mps2', 1.0)
    
    # 所有 local_track_id 列表
    all_a_track_ids = sorted(meas_a_df['local_track_id'].unique())
    all_b_track_ids = sorted(meas_b_df['local_track_id'].unique())
    
    # Sonar A 跟踪
    tracks_a_list = []
    for tid in all_a_track_ids:
        meas_subset = meas_a_df[meas_a_df['local_track_id'] == tid]
        if len(meas_subset) < 3:
            continue
        track_df = simple_kalman_track(
            meas_subset, 'SONAR_A', tid,
            process_noise=proc_noise,
            measurement_noise=meas_noise
        )
        if len(track_df) > 0:
            tracks_a_list.append(track_df)
    
    # Sonar A 干扰
    if dist_a_meas:
        dist_a_df = pd.DataFrame(dist_a_meas)
        for dname in dist_a_df['source_track_id'].unique():
            meas_subset = dist_a_df[dist_a_df['source_track_id'] == dname]
            track_df = simple_kalman_track(
                meas_subset, 'SONAR_A', dname,
                process_noise=proc_noise * 1.5,
                measurement_noise=meas_noise * 1.5
            )
            if len(track_df) > 5:
                tracks_a_list.append(track_df)
    
    tracks_a_df = pd.concat(tracks_a_list, ignore_index=True) if tracks_a_list else pd.DataFrame()
    tracks_a_df.to_csv(output_path / 'sonar_A_tracks.csv', index=False)
    print(f"  Sonar A: {tracks_a_df['track_id'].nunique()} tracks")
    print(f"  A track IDs: {sorted(tracks_a_df['track_id'].unique())}")
    
    # Sonar B 跟踪
    tracks_b_list = []
    for tid in all_b_track_ids:
        meas_subset = meas_b_df[meas_b_df['local_track_id'] == tid]
        if len(meas_subset) < 3:
            continue
        track_df = simple_kalman_track(
            meas_subset, 'SONAR_B', tid,
            process_noise=proc_noise,
            measurement_noise=meas_noise
        )
        if len(track_df) > 0:
            tracks_b_list.append(track_df)
    
    # Sonar B 干扰
    if dist_b_meas:
        dist_b_df = pd.DataFrame(dist_b_meas)
        for dname in dist_b_df['source_track_id'].unique():
            meas_subset = dist_b_df[dist_b_df['source_track_id'] == dname]
            track_df = simple_kalman_track(
                meas_subset, 'SONAR_B', dname,
                process_noise=proc_noise * 1.5,
                measurement_noise=meas_noise * 1.5
            )
            if len(track_df) > 5:
                tracks_b_list.append(track_df)
    
    tracks_b_df = pd.concat(tracks_b_list, ignore_index=True) if tracks_b_list else pd.DataFrame()
    tracks_b_df.to_csv(output_path / 'sonar_B_tracks.csv', index=False)
    print(f"  Sonar B: {tracks_b_df['track_id'].nunique()} tracks")
    print(f"  B track IDs: {sorted(tracks_b_df['track_id'].unique())}")
    
    # 合并所有 tracks
    tracks_all_df = pd.concat([tracks_a_df, tracks_b_df], ignore_index=True)
    tracks_all_df.to_csv(output_path / 'all_local_tracks.csv', index=False)
    print()
    
    # ============================================================
    # Step 5: 时间对齐（不区分 ID）
    # ============================================================
    print("[Step 5] Aligning tracks in common time reference...")
    
    track_ids_a = sorted(tracks_a_df['track_id'].unique())
    track_ids_b = sorted(tracks_b_df['track_id'].unique())
    
    # 使用 A 和 B 所有时间戳的并集作为公共基准
    all_timestamps = sorted(set(tracks_a_df['timestamp'].unique()) | 
                            set(tracks_b_df['timestamp'].unique()))
    
    aligned_pairs = []
    
    for track_a_id in track_ids_a:
        track_a_data = tracks_a_df[tracks_a_df['track_id'] == track_a_id].sort_values('timestamp')
        
        for track_b_id in track_ids_b:
            track_b_data = tracks_b_df[tracks_b_df['track_id'] == track_b_id].sort_values('timestamp')
            
            # 在并集时间戳上插值
            for t in all_timestamps:
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
                
                pos_dist = np.sqrt((a_row['x'] - b_row['x'])**2 + (a_row['y'] - b_row['y'])**2)
                vel_diff = np.sqrt((a_row['vx'] - b_row['vx'])**2 + (a_row['vy'] - b_row['vy'])**2)
                
                aligned_pairs.append({
                    'timestamp': t,
                    'track_a_id': track_a_id,
                    'track_b_id': track_b_id,
                    'Ax': a_row['x'], 'Ay': a_row['y'],
                    'Avx': a_row['vx'], 'Avy': a_row['vy'],
                    'Bx': b_row['x'], 'By': b_row['y'],
                    'Bvx': b_row['vx'], 'Bvy': b_row['vy'],
                    'time_gap': 0.0,
                    'position_distance': pos_dist,
                    'velocity_difference': vel_diff
                })
    
    aligned_df = pd.DataFrame(aligned_pairs)
    aligned_df.to_csv(output_path / 'aligned_track_pairs.csv', index=False)
    print(f"  Generated {len(aligned_df)} aligned pairs")
    print(f"  Unique timestamps: {aligned_df['timestamp'].nunique()}")
    print()
    
    # ============================================================
    # Step 6: Track-Centric Association Manager
    # ============================================================
    print("[Step 6] Running Track-Centric Association Manager...")
    
    assoc_manager = TrackCentricAssociationManager(config['association'])
    
    # 计算轨迹级代价
    cost_data = []
    for track_a_id in track_ids_a:
        for track_b_id in track_ids_b:
            pairs = aligned_df[
                (aligned_df['track_a_id'] == track_a_id) &
                (aligned_df['track_b_id'] == track_b_id)
            ]
            if len(pairs) == 0:
                continue
            
            mean_pos_dist = pairs['position_distance'].mean()
            mean_vel_diff = pairs['velocity_difference'].mean()
            gate_pass = (pairs['position_distance'] < config['association'].get('position_gate_m', 30.0)).mean()
            
            pos_norm = config['association'].get('cost_normalization', {}).get('position_threshold_m', 20.0)
            cost = min(1.0, mean_pos_dist / pos_norm)
            
            overall_pass = (
                mean_pos_dist < config['association'].get('position_gate_m', 30.0) and
                mean_vel_diff < config['association'].get('velocity_gate_mps', 15.0) and
                gate_pass >= config['association'].get('min_gate_pass_ratio', 0.5)
            )
            
            cost_data.append({
                'track_a_id': track_a_id,
                'track_b_id': track_b_id,
                'mean_position_distance': mean_pos_dist,
                'mean_velocity_difference': mean_vel_diff,
                'gate_pass_ratio': gate_pass,
                'cost': cost if overall_pass else 1.0,
                'overall_pass': overall_pass
            })
    
    cost_df = pd.DataFrame(cost_data)
    cost_df.to_csv(output_path / 'association_cost_matrix.csv', index=False)
    
    # 构造每对预计算的轨迹级代价（用于窗口内的 Hungarian）
    # 每个 aligned pair 行都标注其 (a,b) pair 的轨迹级 cost
    track_pair_cost = {}
    for _, r in cost_df.iterrows():
        track_pair_cost[(r['track_a_id'], r['track_b_id'])] = r['cost']
    
    aligned_df_with_track_cost = aligned_df.copy()
    aligned_df_with_track_cost['cost'] = aligned_df_with_track_cost.apply(
        lambda r: track_pair_cost.get((r['track_a_id'], r['track_b_id']), 1.0),
        axis=1
    )
    
    # 运行 track-centric 关联（按时间窗口）
    timestamps = sorted(aligned_df_with_track_cost['timestamp'].unique())
    
    cost_threshold = config['association'].get('max_cost_for_tentative', 0.7)
    
    for ts in timestamps:
        window_data = aligned_df_with_track_cost[
            aligned_df_with_track_cost['timestamp'] == ts
        ].copy()
        
        assoc_manager.process_time_window(ts, window_data, cost_threshold)
    
    history_df = assoc_manager.get_history_dataframe()
    summary_df = assoc_manager.get_associations_summary()
    reassociation_df = assoc_manager.get_reassociation_log()
    
    history_df.to_csv(output_path / 'association_history.csv', index=False)
    summary_df.to_csv(output_path / 'association_summary.csv', index=False)
    if len(reassociation_df) > 0:
        reassociation_df.to_csv(output_path / 'reassociation_log.csv', index=False)
    
    print(f"  Tracks registered: {len(summary_df)}")
    confirmed_count = len(summary_df[summary_df['current_status'] == 'CONFIRMED']) // 2
    print(f"  Confirmed associations: {confirmed_count}")
    print()
    
    # ============================================================
    # Step 7: Global Track Manager
    # ============================================================
    print("[Step 7] Running Global Track Manager...")
    
    global_mgr = GlobalTrackManager(config.get('global_track', {}))
    
    # 注册所有 Local Track
    for _, row in tracks_all_df.iterrows():
        global_mgr.register_local_track(row['sensor_id'], row['track_id'], row['timestamp'])
    
    # 从 confirmed 关联更新 Global Track
    confirmed_pairs = assoc_manager.get_confirmed_associations()
    
    # 获取 track states (用于 update_from_association)
    track_states = assoc_manager.track_states
    
    # 在每个时间点运行 Global Track 更新
    timestamps = sorted(aligned_df['timestamp'].unique())
    for ts in timestamps:
        # 找出当前时间 confirmed 关联对
        window_confirmed = []
        for ta, tb in confirmed_pairs:
            # 检查是否在当前窗口
            pair_data = aligned_df[
                (aligned_df['track_a_id'] == ta) &
                (aligned_df['track_b_id'] == tb)
            ]
            if len(pair_data) > 0:
                min_t = pair_data['timestamp'].min()
                max_t = pair_data['timestamp'].max()
                if min_t <= ts <= max_t:
                    window_confirmed.append((ta, tb))
        
        global_mgr.update_from_association(ts, window_confirmed, track_states)
        global_mgr.update_global_status(ts)
    
    # 保存 Global Track 结果
    global_tracks_df = global_mgr.get_global_tracks_dataframe()
    local_to_global_df = global_mgr.get_local_to_global_map()
    merge_log_df = global_mgr.get_merge_log()
    
    global_tracks_df.to_csv(output_path / 'global_tracks.csv', index=False)
    local_to_global_df.to_csv(output_path / 'local_to_global.csv', index=False)
    if len(merge_log_df) > 0:
        merge_log_df.to_csv(output_path / 'global_track_merges.csv', index=False)
    
    print(f"  Global tracks created: {len(global_tracks_df)}")
    print(f"  Local tracks mapped: {len(local_to_global_df)}")
    print()
    
    # ============================================================
    # Step 8: 评估
    # ============================================================
    print("[Step 8] Evaluating multi-target results...")
    
    # 构建 GT 映射（仅用于 evaluation，不参与关联决策）
    gt_mapping = {}
    
    # Sonar A 看到的 Local Track -> target_id
    for target_id in target_ids:
        gt_mapping[f'A_{target_id}'] = target_id
        gt_mapping[f'B_{target_id}'] = target_id
    
    # 调用多目标评估
    from evaluate_multi_target import evaluate_multi_target_association
    
    associations_results = []
    for ta, tb in confirmed_pairs:
        assoc_record = {
            'track_a_id': ta,
            'track_b_id': tb,
            'status': 'CONFIRMED'
        }
        # 找 cost info
        c = cost_df[(cost_df['track_a_id'] == ta) & (cost_df['track_b_id'] == tb)]
        if len(c) > 0:
            assoc_record['cost'] = c.iloc[0]['cost']
            assoc_record['mean_position_distance'] = c.iloc[0]['mean_position_distance']
        associations_results.append(assoc_record)
    
    associations_results_df = pd.DataFrame(associations_results)
    if len(associations_results_df) > 0:
        associations_results_df.to_csv(output_path / 'association_results.csv', index=False)
    
    eval_results = evaluate_multi_target_association(
        local_to_global_df,
        gt_mapping,
        tracks_all_df,
        associations_results_df,
        str(output_path / 'multi_target_evaluation.json')
    )
    
    # 打印结果
    metrics = eval_results.get('metrics', {})
    print(f"  Global tracks: {metrics.get('num_global_tracks', 0)}")
    print(f"  GT targets: {metrics.get('num_gt_targets', 0)}")
    print(f"  ID switch count: {metrics.get('id_switch_count', 0)}")
    print(f"  ID switch rate: {metrics.get('id_switch_rate', 0):.2%}")
    print(f"  Fragmentation rate: {metrics.get('fragmentation_rate', 0):.2%}")
    print(f"  GT target spread: {metrics.get('gt_target_spread', {})}")
    print()
    
    # ============================================================
    # Step 9: 可视化
    # ============================================================
    print("[Step 9] Generating visualizations...")
    
    try:
        from plot_association import plot_all_results
        
        plot_all_results(
            str(output_path), config,
            reference.to_dataframe(),
            tracks_a_df, tracks_b_df,
            associations_results_df, cost_df,
            aligned_df, history_df,
            f"{experiment_type.upper()} Experiment"
        )
    except Exception as e:
        print(f"  Warning: Plotting failed: {e}")
    
    print()
    print("=" * 70)
    print("MULTI-TARGET EXPERIMENT COMPLETE")
    print("=" * 70)
    
    return {
        'experiment_type': experiment_type,
        'metrics': metrics,
        'eval_results': eval_results,
        'config': {
            'sonar_A': config['sonar_A'],
            'sonar_B': config['sonar_B']
        }
    }


def main():
    parser = argparse.ArgumentParser(
        description='Run Multi-Target Two Sonar Association'
    )
    parser.add_argument('--experiment', type=str, required=True,
                       choices=['easy', 'difficult', 'crossing_2diver',
                                'parallel_2diver', 'three_diver'],
                       help='Experiment type')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Configuration file')
    parser.add_argument('--input', type=str,
                       default=DEFAULT_REFERENCE_TRACK,
                       help='Input tracks CSV')
    parser.add_argument('--output', type=str, default=None,
                       help='Output directory')
    parser.add_argument('--track-id', type=str, default='PRIMARY_DIVER',
                       help='Track ID to use as reference')
    parser.add_argument('--seed', type=int, default=None,
                       help='Random seed')
    
    args = parser.parse_args()
    
    # 加载配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # 实验配置
    config = setup_experiment(config, args.experiment)
    
    if args.seed is not None:
        config['random_seed'] = args.seed
    
    # 输出目录
    if args.output is None:
        args.output = str(OUTPUT_ROOT / f'experiment_{args.experiment}')
    
    # 运行
    results = run_multi_target_experiment(
        input_csv=args.input,
        output_dir=args.output,
        config=config,
        experiment_type=args.experiment,
        track_id=args.track_id,
        seed=args.seed
    )
    
    print("\n" + "=" * 70)
    print("FINAL METRICS SUMMARY")
    print("=" * 70)
    metrics = results.get('metrics', {})
    print(f"Global tracks: {metrics.get('num_global_tracks', 0)}")
    print(f"GT targets: {metrics.get('num_gt_targets', 0)}")
    print(f"ID switch count: {metrics.get('id_switch_count', 0)}")
    print(f"ID switch rate: {metrics.get('id_switch_rate', 0):.2%}")


if __name__ == '__main__':
    main()