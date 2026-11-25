#!/usr/bin/env python3
"""
Run Regression Benchmark - 4 Cases
回归测试基准：4 个难度递进的关联场景

Case 1: 单目标基线          —— 基本关联是否正确
Case 2: 多目标分离运动      —— 多目标同时关联
Case 3: 双目标靠近/交叉     —— 是否发生 ID Switch
Case 4: 交叉 + 漏检 + 断轨  —— Global ID 能否恢复（最有价值）

输出：
- 每个 case 一个 output 目录（含完整 CSV + Figures）
- 一张合并的指标对比表（CSV + Markdown）
- 一份 REGRESSION_BENCHMARK_REPORT.md 报告

注意：当前阶段评价的是"跨声纳航迹对应关系 + Global ID 稳定性"，
不是位置估计精度（那是 Track Fusion，下一阶段）。
"""
import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'src'))
from two_sonar_paths import DEFAULT_CONFIG, DEFAULT_REFERENCE_TRACK, OUTPUT_ROOT

from simulate_two_sonars import load_reference_track_from_dfki
from regression_scenarios import (
    get_all_cases, RegressionCase,
    build_scenario_for_case,
    generate_measurements_for_case,
    apply_track_break_to_sonar_b
)
from regression_metrics import compute_all_metrics, metrics_to_table_row
from association_utils import KalmanFilter
from multi_target_scenario import create_parallel_diver_scenario, create_crossing_diver_scenario


# ============================================================
# 复用 run_multi_target_association.py 的内部工具
# ============================================================
def simple_kalman_track(measurements_df: pd.DataFrame,
                        sensor_id: str,
                        track_id: str,
                        process_noise: float = 1.0,
                        measurement_noise: float = 2.0) -> pd.DataFrame:
    """简化的 Kalman 跟踪（与 run_multi_target_association.py 保持一致）"""
    valid_meas = measurements_df[measurements_df['measurement_available'] == True].copy()
    valid_meas = valid_meas.sort_values('timestamp').reset_index(drop=True)

    if len(valid_meas) == 0:
        return pd.DataFrame()

    kf = KalmanFilter(
        process_noise_pos=process_noise,
        process_noise_vel=process_noise / 2,
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


def run_case(case: RegressionCase,
              input_csv: str,
              output_dir: str,
              base_config: dict,
              seed: int = None) -> Dict:
    """
    运行单个回归测试用例

    简化版 pipeline：
    1. 加载参考航迹
    2. 根据 case 构造多目标场景
    3. 生成 Sonar A/B 测量（可选地注入 Case 4 的漏检窗口）
    4. Sonar B 可选地强制断轨（让 Local Tracker 产生新 Track ID）
    5. 跑 Local Kalman Tracker
    6. 时间对齐 + N×M Cost + Hungarian
    7. Track-Centric Association Manager
    8. Global Track Manager
    9. 输出评价 JSON
    """
    print(f"\n{'='*70}")
    print(f"RUNNING: {case.display_name}")
    print(f"{'='*70}")
    print(f"Description: {case.description}")
    print(f"Config: {case.summary_dict()}")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    figures_dir = output_path / 'figures'
    figures_dir.mkdir(exist_ok=True)

    if seed is None:
        seed = case.seed

    rng = np.random.default_rng(seed)

    # 加载参考航迹
    reference = load_reference_track_from_dfki(input_csv, 'PRIMARY_DIVER')

    # 构建场景
    scenario = build_scenario_for_case(case, reference)
    target_ids = scenario.get_target_ids()
    print(f"  Targets: {target_ids}")

    # 保存参考航迹
    for tid, traj in scenario.targets.items():
        traj.to_csv(output_path / f'reference_track_{tid}.csv')

    # ============================================================
    # 生成测量
    # ============================================================
    sonar_a_config = base_config['sonar_A'].copy()
    sonar_b_config = base_config['sonar_B'].copy()

    sonar_a_config['sigma_range_m'] *= case.sonar_a_sigma_mult
    sonar_a_config['sigma_bearing_deg'] *= case.sonar_a_sigma_mult
    sonar_a_config['miss_probability'] = case.sonar_a_miss_prob

    sonar_b_config['sigma_range_m'] *= case.sonar_b_sigma_mult
    sonar_b_config['sigma_bearing_deg'] *= case.sonar_b_sigma_mult
    sonar_b_config['miss_probability'] = case.sonar_b_miss_prob

    sonar_a_rng = np.random.default_rng(seed + 100)
    sonar_b_rng = np.random.default_rng(seed + 200)

    from simulate_two_sonars import VirtualSonar
    sonar_a = VirtualSonar(sonar_a_config, 'SONAR_A', sonar_a_rng)
    sonar_b = VirtualSonar(sonar_b_config, 'SONAR_B', sonar_b_rng)

    meas_a_df, meas_b_df = generate_measurements_for_case(
        case, scenario, sonar_a, sonar_b
    )

    # Case 4 注入：Sonar B 强制断轨
    meas_b_df = apply_track_break_to_sonar_b(case, meas_b_df)

    print(f"  Sonar A measurements: {len(meas_a_df)}")
    print(f"  Sonar B measurements: {len(meas_b_df)}")
    if case.sonar_b_miss_window:
        print(f"  Sonar B miss window: {case.sonar_b_miss_window}")
    if case.sonar_b_track_break:
        unique_b_ids = sorted(meas_b_df['local_track_id'].unique())
        print(f"  Sonar B track IDs after break: {unique_b_ids}")

    meas_a_df.to_csv(output_path / 'sonar_A_measurements.csv', index=False)
    meas_b_df.to_csv(output_path / 'sonar_B_measurements.csv', index=False)

    # ============================================================
    # 跑 Local Kalman Tracker
    # ============================================================
    kalman_config = base_config['kalman_filter']
    meas_noise = kalman_config.get('measurement_noise_position_m', 2.0)
    proc_noise = kalman_config.get('process_noise_position_mps2', 1.0)

    all_a_track_ids = sorted(meas_a_df['local_track_id'].unique())
    all_b_track_ids = sorted(meas_b_df['local_track_id'].unique())

    tracks_a_list = []
    for tid in all_a_track_ids:
        subset = meas_a_df[meas_a_df['local_track_id'] == tid]
        if len(subset) < 3:
            continue
        track_df = simple_kalman_track(subset, 'SONAR_A', tid,
                                        process_noise=proc_noise,
                                        measurement_noise=meas_noise)
        if len(track_df) > 0:
            tracks_a_list.append(track_df)
    tracks_a_df = pd.concat(tracks_a_list, ignore_index=True) if tracks_a_list else pd.DataFrame()

    tracks_b_list = []
    for tid in all_b_track_ids:
        subset = meas_b_df[meas_b_df['local_track_id'] == tid]
        if len(subset) < 3:
            continue
        track_df = simple_kalman_track(subset, 'SONAR_B', tid,
                                        process_noise=proc_noise,
                                        measurement_noise=meas_noise)
        if len(track_df) > 0:
            tracks_b_list.append(track_df)
    tracks_b_df = pd.concat(tracks_b_list, ignore_index=True) if tracks_b_list else pd.DataFrame()

    tracks_a_df.to_csv(output_path / 'sonar_A_tracks.csv', index=False)
    tracks_b_df.to_csv(output_path / 'sonar_B_tracks.csv', index=False)

    tracks_all_df = pd.concat([tracks_a_df, tracks_b_df], ignore_index=True)
    tracks_all_df.to_csv(output_path / 'all_local_tracks.csv', index=False)

    print(f"  Sonar A tracks: {sorted(tracks_a_df['track_id'].unique())}")
    print(f"  Sonar B tracks: {sorted(tracks_b_df['track_id'].unique())}")

    # ============================================================
    # 时间对齐
    # ============================================================
    track_ids_a = sorted(tracks_a_df['track_id'].unique())
    track_ids_b = sorted(tracks_b_df['track_id'].unique())
    all_timestamps = sorted(set(tracks_a_df['timestamp'].unique()) |
                             set(tracks_b_df['timestamp'].unique()))

    aligned_pairs = []
    for track_a_id in track_ids_a:
        track_a_data = tracks_a_df[tracks_a_df['track_id'] == track_a_id].sort_values('timestamp')
        for track_b_id in track_ids_b:
            track_b_data = tracks_b_df[tracks_b_df['track_id'] == track_b_id].sort_values('timestamp')

            for t in all_timestamps:
                # A 插值
                a_before = track_a_data[track_a_data['timestamp'] <= t]
                a_after = track_a_data[track_a_data['timestamp'] >= t]
                if len(a_before) == 0 or len(a_after) == 0:
                    continue
                a_idx_b = a_before['timestamp'].idxmax()
                a_idx_a = a_after['timestamp'].idxmin()
                a_row_b = track_a_data.loc[a_idx_b]
                a_row_a = track_a_data.loc[a_idx_a]
                if a_row_b['timestamp'] == a_row_a['timestamp']:
                    a_row = a_row_b
                else:
                    w = (t - a_row_b['timestamp']) / (a_row_a['timestamp'] - a_row_b['timestamp'])
                    a_row = a_row_b.copy()
                    for col in ['x', 'y', 'vx', 'vy', 'Pxx', 'Pyy', 'Pvxvx', 'Pvyvy']:
                        if col in track_a_data.columns:
                            a_row[col] = a_row_b[col] + w * (a_row_a[col] - a_row_b[col])

                # B 插值
                b_before = track_b_data[track_b_data['timestamp'] <= t]
                b_after = track_b_data[track_b_data['timestamp'] >= t]
                if len(b_before) == 0 or len(b_after) == 0:
                    continue
                b_idx_b = b_before['timestamp'].idxmax()
                b_idx_a = b_after['timestamp'].idxmin()
                b_row_b = track_b_data.loc[b_idx_b]
                b_row_a = track_b_data.loc[b_idx_a]
                if b_row_b['timestamp'] == b_row_a['timestamp']:
                    b_row = b_row_b
                else:
                    w = (t - b_row_b['timestamp']) / (b_row_a['timestamp'] - b_row_b['timestamp'])
                    b_row = b_row_b.copy()
                    for col in ['x', 'y', 'vx', 'vy', 'Pxx', 'Pyy', 'Pvxvx', 'Pvyvy']:
                        if col in track_b_data.columns:
                            b_row[col] = b_row_b[col] + w * (b_row_a[col] - b_row_b[col])

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
    print(f"  Aligned pairs: {len(aligned_df)}")

    # ============================================================
    # Track-Centric Association
    # ============================================================
    from association_manager import TrackCentricAssociationManager

    assoc_manager = TrackCentricAssociationManager(base_config['association'])

    # 计算轨迹级 cost
    cost_data = []
    for ta in track_ids_a:
        for tb in track_ids_b:
            pairs = aligned_df[(aligned_df['track_a_id'] == ta) & (aligned_df['track_b_id'] == tb)]
            if len(pairs) == 0:
                continue
            mean_pos = pairs['position_distance'].mean()
            mean_vel = pairs['velocity_difference'].mean()
            gate_pass = (pairs['position_distance'] < base_config['association'].get('position_gate_m', 30.0)).mean()
            pos_norm = base_config['association'].get('cost_normalization', {}).get('position_threshold_m', 20.0)
            cost = min(1.0, mean_pos / pos_norm)
            overall_pass = (
                mean_pos < base_config['association'].get('position_gate_m', 30.0) and
                mean_vel < base_config['association'].get('velocity_gate_mps', 15.0) and
                gate_pass >= base_config['association'].get('min_gate_pass_ratio', 0.5)
            )
            cost_data.append({
                'track_a_id': ta,
                'track_b_id': tb,
                'mean_position_distance': mean_pos,
                'mean_velocity_difference': mean_vel,
                'gate_pass_ratio': gate_pass,
                'cost': cost if overall_pass else 1.0,
                'overall_pass': overall_pass
            })

    cost_df = pd.DataFrame(cost_data)
    cost_df.to_csv(output_path / 'association_cost_matrix.csv', index=False)

    # 跟踪级 cost 字典
    track_pair_cost = {}
    for _, r in cost_df.iterrows():
        track_pair_cost[(r['track_a_id'], r['track_b_id'])] = r['cost']

    aligned_with_cost = aligned_df.copy()
    aligned_with_cost['cost'] = aligned_with_cost.apply(
        lambda r: track_pair_cost.get((r['track_a_id'], r['track_b_id']), 1.0),
        axis=1
    )

    timestamps = sorted(aligned_with_cost['timestamp'].unique())
    cost_threshold = base_config['association'].get('max_cost_for_tentative', 0.7)

    for ts in timestamps:
        window_data = aligned_with_cost[aligned_with_cost['timestamp'] == ts].copy()
        assoc_manager.process_time_window(ts, window_data, cost_threshold)

    history_df = assoc_manager.get_history_dataframe()
    summary_df = assoc_manager.get_associations_summary()
    reassociation_df = assoc_manager.get_reassociation_log()

    history_df.to_csv(output_path / 'association_history.csv', index=False)
    summary_df.to_csv(output_path / 'association_summary.csv', index=False)
    if len(reassociation_df) > 0:
        reassociation_df.to_csv(output_path / 'reassociation_log.csv', index=False)

    print(f"  Association summary: {len(summary_df)} tracks")

    # ============================================================
    # Global Track Manager
    # ============================================================
    from global_track_manager import GlobalTrackManager

    global_mgr = GlobalTrackManager(base_config.get('global_track', {}))

    for _, row in tracks_all_df.iterrows():
        global_mgr.register_local_track(row['sensor_id'], row['track_id'], row['timestamp'])

    confirmed_pairs = assoc_manager.get_confirmed_associations()
    track_states = assoc_manager.track_states

    for ts in timestamps:
        window_confirmed = []
        for ta, tb in confirmed_pairs:
            pair_data = aligned_df[(aligned_df['track_a_id'] == ta) & (aligned_df['track_b_id'] == tb)]
            if len(pair_data) > 0:
                min_t = pair_data['timestamp'].min()
                max_t = pair_data['timestamp'].max()
                if min_t <= ts <= max_t:
                    window_confirmed.append((ta, tb))
        global_mgr.update_from_association(ts, window_confirmed, track_states)
        # 关键：每一帧结束后做一次"单边 reconcile"——
        # 处理 Sonar B 断轨后 B_T1_p2 这类新 track 想重新挂回老 Global Track 的场景
        global_mgr.reconcile_from_track_states(ts, track_states)
        global_mgr.update_global_status(ts)

    global_tracks_df = global_mgr.get_global_tracks_dataframe()
    local_to_global_df = global_mgr.get_local_to_global_map()
    merge_log_df = global_mgr.get_merge_log()

    global_tracks_df.to_csv(output_path / 'global_tracks.csv', index=False)
    local_to_global_df.to_csv(output_path / 'local_to_global.csv', index=False)
    if len(merge_log_df) > 0:
        merge_log_df.to_csv(output_path / 'global_track_merges.csv', index=False)

    print(f"  Global tracks: {len(global_tracks_df)}")
    print(f"  Local→Global mappings: {len(local_to_global_df)}")

    # ============================================================
    # 计算所有 9 项指标
    # ============================================================
    gt_mapping = {}
    for target_id in target_ids:
        gt_mapping[f'A_{target_id}'] = target_id
        gt_mapping[f'B_{target_id}'] = target_id
        # Case 4 中 Sonar B 断轨后的新 track id
        if case.sonar_b_track_break:
            gt_mapping[f'B_{target_id}_p2'] = target_id

    metrics = compute_all_metrics(str(output_path), gt_mapping)

    # 保存指标
    metrics_full = {
        'case': case.summary_dict(),
        'metrics': metrics,
    }
    with open(output_path / 'regression_metrics.json', 'w') as f:
        json.dump(metrics_full, f, indent=2, default=str)

    print(f"\n  METRICS for {case.display_name}:")
    for k, v in metrics.items():
        print(f"    {k}: {v}")

    return metrics_full


def build_comparison_table(all_results: List[Dict]) -> pd.DataFrame:
    """构建跨 case 的对比表"""
    rows = []
    for r in all_results:
        case_name = r['case']['display_name']
        row = metrics_to_table_row(r['metrics'])
        # 在字典前插入 Case 字段
        new_row = {'Case': case_name}
        new_row.update(row)
        rows.append(new_row)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(
        description='Run 4-case regression benchmark for two sonar association'
    )
    parser.add_argument('--config', type=str,
                        default=DEFAULT_CONFIG,
                        help='Base configuration file')
    parser.add_argument('--input', type=str,
                        default=DEFAULT_REFERENCE_TRACK,
                        help='Input tracks CSV')
    parser.add_argument('--output-root', type=str,
                        default=str(OUTPUT_ROOT / 'regression_benchmark'),
                        help='Output root directory')
    parser.add_argument('--seed', type=int, default=None,
                        help='Override default seed')
    parser.add_argument('--cases', type=str, nargs='+', default=None,
                        help='Run only specified cases (case1_single, case2_separated, '
                             'case3_crossing, case4_crossing_break)')
    args = parser.parse_args()

    # 加载配置
    with open(args.config, 'r') as f:
        base_config = json.load(f)

    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    # 获取所有 cases
    all_cases = get_all_cases()
    if args.cases:
        all_cases = [c for c in all_cases if c.name in args.cases]

    # 跑每个 case
    all_results = []
    for case in all_cases:
        case_output_dir = output_root / case.name
        result = run_case(
            case=case,
            input_csv=args.input,
            output_dir=str(case_output_dir),
            base_config=base_config,
            seed=args.seed if args.seed is not None else case.seed
        )
        result['case_dir'] = str(case_output_dir)
        all_results.append(result)

    # 构建对比表
    comparison_df = build_comparison_table(all_results)
    comparison_df.to_csv(output_root / 'benchmark_comparison.csv', index=False)

    # 打印对比表
    print("\n" + "=" * 100)
    print("REGRESSION BENCHMARK COMPARISON TABLE")
    print("=" * 100)
    print(comparison_df.to_string(index=False))
    print("=" * 100)

    # 保存对比表的 Markdown 版本
    md_path = output_root / 'benchmark_comparison.md'
    with open(md_path, 'w') as f:
        f.write("# Regression Benchmark Comparison Table\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("## 10 Metrics × 4 Cases\n\n")

        # 手动构建 markdown 表格（避免依赖 tabulate）
        cols = list(comparison_df.columns)
        f.write('| ' + ' | '.join(['Case'] + [c for c in cols if c != 'Case']) + ' |\n')
        f.write('|' + '|'.join(['---'] * (len(cols) + 1)) + '|\n')
        for _, row in comparison_df.iterrows():
            values = [str(row['Case'])] + [str(row[c]) for c in cols if c != 'Case']
            f.write('| ' + ' | '.join(values) + ' |\n')

        f.write("\n## Notes\n\n")
        f.write("- **N/A** indicates the metric is not applicable for that case\n")
        f.write("  (e.g., Reassociation Success when no track went through COASTING/LOST/track-break).\n")
        f.write("- **Association Recall < 1** in Case 4 is by design: the Association Manager intentionally\n")
        f.write("  rejects CONFIRMED→other switches to prevent ID switches. The Global Track Manager\n")
        f.write("  is what recovers the identity for the new tracklet. See Global Track Recall = 1.0.\n")
        f.write("- 验收阈值未硬编码——这是一个 regression benchmark，")
        f.write("用来比较不同版本的算法，而非 pass/fail。\n")
        f.write("- 等真实双声纳数据到位后，再根据现场指标确定阈值。\n")

    print(f"\nComparison table saved to: {md_path}")
    print(f"All case outputs in: {output_root}")


if __name__ == '__main__':
    main()
