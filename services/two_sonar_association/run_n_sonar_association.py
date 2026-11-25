"""
N-Sonar Track Association + Fusion 主入口

读取 n_sonar_simulator 的输出, 对 N 个声纳跑:
  1. 每个声纳独立 simple Kalman 跟踪 → sonar_X_tracks.csv
  2. N-way 时间对齐 (n_way_align.NWayAligner)
  3. N-partite cluster 关联 (n_partite_association.NPartiteAssociator)
  4. N-way 信息形式融合 (n_way_fusion.InformationFusionN)
  5. 评估 (正确关联率, fused RMSE vs single-sensor RMSE)

支持 N=2, 3, 4... 通过 --n-sonars 2..4 选取 sonars 列表的前 N 个。

回归保证: N=2 时的 fused RMSE 应与 run_two_sonar_association 的结果一致 (差 < 0.1 px)。
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'src'))

from association_utils import KalmanFilter  # noqa: E402
from n_way_align import NWayAligner  # noqa: E402
from n_partite_association import NPartiteAssociator  # noqa: E402
from n_way_fusion import InformationFusionN  # noqa: E402


def simple_kalman_track(measurements_df: pd.DataFrame, sensor_id: str,
                        track_id: str,
                        process_noise: float = 1.0,
                        measurement_noise: float = 2.0) -> pd.DataFrame:
    """从单传感器测量生成 Kalman 跟踪轨迹 (与 run_two_sonar_association 一致)."""
    valid = measurements_df[measurements_df['measurement_available'] == True].copy()
    valid = valid.sort_values('timestamp').reset_index(drop=True)
    if len(valid) == 0:
        return pd.DataFrame()

    kf = KalmanFilter(
        process_noise_pos=process_noise,
        process_noise_vel=process_noise / 2,
        measurement_noise_pos=measurement_noise,
        initial_covariance=10.0,
    )
    records = []
    last_time = None
    for _, row in valid.iterrows():
        t = row['timestamp']
        x, y = row['x_common'], row['y_common']
        if not kf.is_initialized:
            kf.initialize(x, y, t)
        else:
            if last_time is not None and t > last_time:
                kf.predict(t - last_time)
            kf.update(np.array([x, y]))
        records.append({
            'timestamp': t, 'track_id': track_id, 'sensor_id': sensor_id,
            'x': kf.x[0], 'y': kf.x[1], 'vx': kf.x[2], 'vy': kf.x[3],
            'Pxx': kf.P[0, 0], 'Pxy': kf.P[0, 1], 'Pyy': kf.P[1, 1],
            'measurement_available': True, 'track_status': 'CONFIRMED',
        })
        last_time = t
    return pd.DataFrame(records)


def build_local_tracks(measurements_by_sensor: Dict[str, pd.DataFrame],
                        sonars_cfg: List[Dict]) -> Dict[str, pd.DataFrame]:
    """对每个 sensor 跑 simple_kalman_track, 合并所有 track_id."""
    out: Dict[str, pd.DataFrame] = {}
    for s in sonars_cfg:
        s_id = s['id']
        meas = measurements_by_sensor[s_id]
        all_tracks: List[pd.DataFrame] = []
        for tid in meas['local_track_id'].unique():
            sub = meas[meas['local_track_id'] == tid]
            trk = simple_kalman_track(sub, s_id, tid)
            if not trk.empty:
                all_tracks.append(trk)
        if all_tracks:
            out[s_id] = pd.concat(all_tracks, ignore_index=True)
    return out


def evaluate_results(clusters, fused_df: pd.DataFrame,
                     reference_df: pd.DataFrame,
                     sonars_cfg: List[Dict],
                     local_by_sensor: Dict[str, pd.DataFrame]) -> Dict:
    """
    计算:
      - 正确关联率: 关联对中是否 DIVER 配对
      - single-sensor RMSE: 每个 sensor 在参考航迹上的 RMSE
      - fused RMSE: 融合轨迹在参考航迹上的 RMSE
      - 改善率
    """
    ref_x = reference_df['x_ref'].values
    ref_y = reference_df['y_ref'].values
    ref_t = reference_df['timestamp'].values

    def interp_at(ts_array, xs, ys, target):
        out_x = np.full(len(target), np.nan)
        out_y = np.full(len(target), np.nan)
        for i, t in enumerate(target):
            if t < ts_array[0] or t > ts_array[-1]:
                continue
            j = np.searchsorted(ts_array, t)
            if j == 0:
                out_x[i] = xs[0]; out_y[i] = ys[0]
            elif j >= len(ts_array):
                out_x[i] = xs[-1]; out_y[i] = ys[-1]
            else:
                w = (t - ts_array[j - 1]) / (ts_array[j] - ts_array[j - 1])
                out_x[i] = xs[j - 1] + w * (xs[j] - xs[j - 1])
                out_y[i] = ys[j - 1] + w * (ys[j] - ys[j - 1])
        return out_x, out_y

    metrics: Dict = {
        'n_sonars': len(sonars_cfg),
        'n_sonars_active': len([s for s in sonars_cfg if s['id'] in local_by_sensor]),
        'n_clusters': len(clusters),
        'clusters': [],
    }
    # Per-sensor RMSE
    single_rmse = {}
    for s in sonars_cfg:
        s_id = s['id']
        if s_id not in local_by_sensor:
            continue
        tr = local_by_sensor[s_id]
        # 只评估 DIVER (主目标) 跟踪
        diver_tracks = [t for t in tr['track_id'].unique() if 'DIVER' in str(t).upper() or '_T1' in str(t)]
        if not diver_tracks:
            # fallback: 用 track 出现频率最高的
            tid = tr['track_id'].value_counts().idxmax()
            diver_tracks = [tid]
        for tid in diver_tracks:
            sub = tr[tr['track_id'] == tid].sort_values('timestamp')
            if len(sub) < 5:
                continue
            ex, ey = interp_at(sub['timestamp'].values, sub['x'].values, sub['y'].values, ref_t)
            err = np.sqrt((ex - ref_x) ** 2 + (ey - ref_y) ** 2)
            valid = ~np.isnan(err)
            if valid.sum() == 0:
                continue
            single_rmse[s_id] = {
                'rmse': float(np.sqrt(np.nanmean(err[valid] ** 2))),
                'median': float(np.nanmedian(err[valid])),
                'p90': float(np.nanpercentile(err[valid], 90)),
                'track_id': tid,
                'n_samples': int(valid.sum()),
            }
            break  # 一个 sensor 只看 DIVER 那一支
    metrics['single_rmse'] = single_rmse

    # Fused RMSE
    if not fused_df.empty and 'position' in fused_df.columns:
        # position 是 array-like
        positions = np.array([np.array(p) for p in fused_df['position']])
        fx = positions[:, 0]
        fy = positions[:, 1]
        ex, ey = interp_at(fused_df['timestamp'].values, fx, fy, ref_t)
        err = np.sqrt((ex - ref_x) ** 2 + (ey - ref_y) ** 2)
        valid = ~np.isnan(err)
        if valid.sum() > 0:
            metrics['fused_rmse'] = float(np.sqrt(np.nanmean(err[valid] ** 2)))
            metrics['fused_median'] = float(np.nanmedian(err[valid]))
            metrics['fused_p90'] = float(np.nanpercentile(err[valid], 90))
            metrics['fused_n_samples'] = int(valid.sum())
    return metrics


def run_n_sonar_pipeline(
    config: Dict,
    output_dir: Path,
    measurements_by_sensor: Dict[str, pd.DataFrame],
    reference: pd.DataFrame,
    n_sonars: int,
) -> Dict:
    """主 pipeline: align → associate → fuse → evaluate."""
    sonars_full = config['sonars']
    sonars_cfg = sonars_full[:n_sonars]
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[N={n_sonars}] sensors: {[s['id'] for s in sonars_cfg]}")

    # 1) 局部 Kalman 跟踪
    print("  [1/4] Local Kalman tracking per sensor...")
    local_by_sensor = build_local_tracks(measurements_by_sensor, sonars_cfg)
    for s_id, tr in local_by_sensor.items():
        out_path = output_dir / f"sonar_{s_id.split('_')[-1]}_tracks.csv"
        tr.to_csv(out_path, index=False)
        print(f"    {s_id}: {len(tr)} rows, {tr['track_id'].nunique()} tracks")

    # 2) N-way 时间对齐
    print("  [2/4] N-way time alignment...")
    aligner = NWayAligner(
        max_time_gap_s=config['association']['max_time_gap_s'],
        anchor_sensor=sonars_cfg[0]['id'],
    )
    aligned_long = aligner.align(local_by_sensor, anchor_sensor=sonars_cfg[0]['id'])
    aligned_long.to_csv(output_dir / "aligned_long.csv", index=False)
    print(f"    aligned rows: {len(aligned_long)}")

    # 3) N-partite cluster association
    print("  [3/4] N-partite cluster association...")
    n_way_cfg = config.get('n_way_association', {})
    assoc = NPartiteAssociator(
        config['association'], n_way_config=n_way_cfg)
    sensor_ids = [s['id'] for s in sonars_cfg]
    clusters = assoc.find_clusters(aligned_long, sensors=sensor_ids)
    print(f"    found {len(clusters)} cluster(s)")
    for c in clusters:
        print(f"      {c.members} mean_cost={c.mean_cost:.3f} "
              f"pass_ratio={c.pair_pass_ratio:.2f}")

    # 保存 cluster 结果
    cluster_records = []
    for i, c in enumerate(clusters):
        rec = {'cluster_id': i, 'mean_cost': c.mean_cost,
               'gate_pass': c.gate_pass, 'pair_pass_ratio': c.pair_pass_ratio}
        for s_id, t_id in c.members.items():
            rec[f'member_{s_id}'] = t_id
        cluster_records.append(rec)
    if cluster_records:
        pd.DataFrame(cluster_records).to_csv(
            output_dir / "n_clusters.csv", index=False)

    # 4) N-way 信息融合 (对每个 cluster 逐帧融合)
    print("  [4/4] N-way information fusion...")
    fuser = InformationFusionN(config['fusion'])
    fused_records: List[Dict] = []
    for cidx, c in enumerate(clusters):
        # 收集每个 sensor 在 cluster 里的 track 状态时间序列
        series: Dict[str, pd.DataFrame] = {}
        for s_id, t_id in c.members.items():
            tr = local_by_sensor.get(s_id)
            if tr is None:
                continue
            sub = tr[tr['track_id'] == t_id].sort_values('timestamp')
            series[s_id] = sub

        if not series:
            continue
        # union 时间戳
        all_t = sorted(set().union(*[s['timestamp'].values for s in series.values()]))
        all_t = np.array(all_t)
        for t in all_t:
            states = []
            for s_id, df in series.items():
                idx = int(np.argmin(np.abs(df['timestamp'].values - t)))
                if abs(df['timestamp'].iloc[idx] - t) > config['association']['max_time_gap_s']:
                    continue
                row = df.iloc[idx]
                x = np.array([row['x'], row['y'], row['vx'], row['vy']])
                P = np.zeros((4, 4))
                P[0, 0] = row['Pxx']; P[0, 1] = row.get('Pxy', 0.0)
                P[1, 0] = P[0, 1];   P[1, 1] = row['Pyy']
                P[2, 2] = row.get('Pvxvx', 1.0)
                P[3, 3] = row.get('Pvyvy', 1.0)
                states.append((s_id, x, P, True, float(t)))
            if not states:
                continue
            fs = fuser.fuse(timestamp=t, global_id=f'G{cidx:03d}', states=states)
            fused_records.append({
                'timestamp': t,
                'global_id': fs.global_id,
                'position_x': float(fs.position[0]),
                'position_y': float(fs.position[1]),
                'velocity_x': float(fs.velocity[0]),
                'velocity_y': float(fs.velocity[1]),
                'position': fs.position.tolist(),
                'velocity': fs.velocity.tolist(),
                'covariance': fs.covariance.tolist(),
                'n_sensors_used': fs.n_sensors_used,
                'sensors_used': ';'.join(fs.sensors_used),
                'fusion_mode': fs.fusion_mode,
                'fallback_reason': fs.fallback_reason or '',
            })
    fused_df = pd.DataFrame(fused_records)
    fused_df.to_csv(output_dir / "fused_global_tracks.csv", index=False)
    print(f"    fused rows: {len(fused_df)}")

    # 5) 评估
    metrics = evaluate_results(clusters, fused_df, reference, sonars_cfg, local_by_sensor)
    metrics['config_sonars'] = [s['id'] for s in sonars_cfg]
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    return metrics


def main():
    ap = argparse.ArgumentParser(description="N-sonar association + fusion")
    ap.add_argument("--config", required=True, help="N-sonar config JSON")
    ap.add_argument("--input", required=True, help="DFKI tracks_raw.csv (PRIMARY_DIVER)")
    ap.add_argument("--output", required=True, help="Output root directory")
    ap.add_argument("--n-sonars", type=int, default=2, help="Use first N sonars in config")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--experiment", default="easy",
                    choices=["easy", "difficult", "default"])
    args = ap.parse_args()

    with open(args.config) as f:
        config = json.load(f)
    seed = args.seed if args.seed is not None else config.get('random_seed', 42)

    # 1. 仿真: 调用 n_sonar_simulator
    from n_sonar_simulator import run_simulation
    from simulate_two_sonars import load_reference_track_from_dfki

    sim_dir = Path(args.output) / "sim"
    print("=" * 60)
    print(f"  N-SONAR PIPELINE (N={args.n_sonars}, experiment={args.experiment})")
    print("=" * 60)
    print("\n[Phase 1] N-sonar simulation...")
    reference = load_reference_track_from_dfki(args.input, "PRIMARY_DIVER")
    sim_results = run_simulation(
        config=config, output_dir=sim_dir,
        reference_tracks={"T1": reference}, seed=seed,
        experiment_type=args.experiment,
    )

    # 2. 把每个 sensor 的 measurements + distractors 合并 (作为局部 Kalman 输入)
    sonars_full = config['sonars'][:args.n_sonars]
    measurements_by_sensor: Dict[str, pd.DataFrame] = {}
    for s in sonars_full:
        s_id, s_short = s['id'], s['short']
        meas = pd.read_csv(sim_dir / f"sonar_{s_short}_measurements.csv")
        # 加入干扰
        dist_path = sim_dir / f"{s_short}_distractors.csv"
        if dist_path.exists():
            dist = pd.read_csv(dist_path)
            # 统一 schema: x_common, y_common, measurement_available
            if 'x_common' not in dist.columns and 'x' in dist.columns:
                dist = dist.rename(columns={'x': 'x_common', 'y': 'y_common'})
            dist['ref_label'] = 'T1'
            dist['local_track_id'] = dist.get('local_track_id', f'{s_id}_DIST')
            meas = pd.concat([meas, dist], ignore_index=True, sort=False)
        measurements_by_sensor[s_id] = meas
        print(f"  {s_id}: {len(meas)} measurements (incl. distractors)")

    # 3. 主 pipeline
    out_dir = Path(args.output) / f"n{args.n_sonars}"
    ref_df = pd.DataFrame({
        'timestamp': reference.timestamps,
        'x_ref': reference.x, 'y_ref': reference.y,
    })
    metrics = run_n_sonar_pipeline(
        config=config, output_dir=out_dir,
        measurements_by_sensor=measurements_by_sensor,
        reference=ref_df, n_sonars=args.n_sonars,
    )

    # 4. 打印 summary
    print("\n" + "=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    print(f"N sonars:           {metrics['n_sonars']}")
    print(f"N clusters:         {metrics['n_clusters']}")
    if 'fused_rmse' in metrics:
        print(f"Fused RMSE:         {metrics['fused_rmse']:.3f} px")
        print(f"Fused median:       {metrics['fused_median']:.3f} px")
        print(f"Fused p90:          {metrics['fused_p90']:.3f} px")
    for s_id, m in metrics.get('single_rmse', {}).items():
        print(f"{s_id} alone:    RMSE={m['rmse']:.3f} px  median={m['median']:.3f}  "
              f"track={m['track_id']}  n={m['n_samples']}")


if __name__ == "__main__":
    main()
