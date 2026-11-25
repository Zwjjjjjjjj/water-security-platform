"""
Plot Association Results - Visualization for Track-to-Track Association
双声纳航迹关联结果可视化
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Set
from enum import Enum
from dataclasses import dataclass
from pathlib import Path
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D


def plot_sensor_geometry(config: dict, reference_track: pd.DataFrame,
                        output_path: str, experiment_name: str = ""):
    """
    绘制传感器几何布局图
    
    显示:
    - Sonar A 和 Sonar B 的位置
    - 参考 diver 航迹
    - 两台声纳的朝向
    """
    fig, ax = plt.subplots(figsize=(12, 10))
    
    # 提取配置
    sonar_a = config['sonar_A']
    sonar_b = config['sonar_B']
    
    # 绘制声纳位置
    ax.scatter(sonar_a['x_m'], sonar_a['y_m'], c='blue', s=200, marker='^',
               label=f"Sonar A (x={sonar_a['x_m']:.1f}, y={sonar_a['y_m']:.1f})", zorder=10)
    ax.scatter(sonar_b['x_m'], sonar_b['y_m'], c='red', s=200, marker='s',
               label=f"Sonar B (x={sonar_b['x_m']:.1f}, y={sonar_b['y_m']:.1f})", zorder=10)
    
    # 绘制声纳朝向
    def draw_fov(x, y, heading_deg, color, label, length=50):
        heading_rad = np.radians(heading_deg)
        dx = length * np.sin(heading_rad)
        dy = length * np.cos(heading_rad)
        ax.arrow(x, y, dx, dy, head_width=10, head_length=5, 
                fc=color, ec=color, alpha=0.7)
        # 绘制视场扇形
        fov_angle = 60  # 假设60度视场
        theta1 = np.radians(heading_deg - fov_angle/2)
        theta2 = np.radians(heading_deg + fov_angle/2)
        theta = np.linspace(theta1, theta2, 20)
        r_fov = np.linspace(0, 80, 20)
        theta_grid, r_grid = np.meshgrid(theta, r_fov)
        x_fov = x + r_grid * np.sin(theta_grid)
        y_fov = y + r_grid * np.cos(theta_grid)
        ax.contourf(x_fov, y_fov, r_grid, levels=[0, 80], alpha=0.1, colors=[color])
    
    draw_fov(sonar_a['x_m'], sonar_a['y_m'], sonar_a['heading_deg'], 'blue', 'Sonar A FOV')
    draw_fov(sonar_b['x_m'], sonar_b['y_m'], sonar_b['heading_deg'], 'red', 'Sonar B FOV')
    
    # 绘制参考航迹
    if len(reference_track) > 0:
        ax.plot(reference_track['x_ref'], reference_track['y_ref'], 
                'g-', linewidth=2, label='Reference Diver Track', alpha=0.8)
        
        # 标注起点和终点
        ax.scatter(reference_track['x_ref'].iloc[0], reference_track['y_ref'].iloc[0],
                  c='green', s=150, marker='o', zorder=8, edgecolors='black')
        ax.scatter(reference_track['x_ref'].iloc[-1], reference_track['y_ref'].iloc[-1],
                  c='green', s=150, marker='*', zorder=8, edgecolors='black')
    
    # 设置坐标轴
    ax.set_xlabel('X Position (pixel units)', fontsize=12)
    ax.set_ylabel('Y Position (pixel units)', fontsize=12)
    ax.set_title(f'Sensor Geometry - {experiment_name}\n'
                f'Sonar A: ({sonar_a["x_m"]:.1f}, {sonar_a["y_m"]:.1f}) @ {sonar_a["heading_deg"]}°\n'
                f'Sonar B: ({sonar_b["x_m"]:.1f}, {sonar_b["y_m"]:.1f}) @ {sonar_b["heading_deg"]}°',
                fontsize=14)
    ax.legend(loc='upper right', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal', adjustable='box')
    
    # 添加配置信息
    info_text = (f"Sonar A: σ_range={sonar_a['sigma_range_m']:.2f}, "
                 f"σ_bearing={sonar_a['sigma_bearing_deg']:.2f}°, "
                 f"miss={sonar_a['miss_probability']*100:.1f}%\n"
                 f"Sonar B: σ_range={sonar_b['sigma_range_m']:.2f}, "
                 f"σ_bearing={sonar_b['sigma_bearing_deg']:.2f}°, "
                 f"miss={sonar_b['miss_probability']*100:.1f}%")
    ax.text(0.02, 0.02, info_text, transform=ax.transAxes, fontsize=9,
           verticalalignment='bottom', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved sensor geometry plot: {output_path}")


def plot_local_tracks(tracks_a: pd.DataFrame, tracks_b: pd.DataFrame,
                      output_path: str, experiment_name: str = ""):
    """
    绘制本地航迹
    
    显示:
    - Sonar A 的所有航迹（包括 DIVER 和 distractors）
    - Sonar B 的所有航迹（包括 DIVER 和 distractors）
    """
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    
    # 定义颜色
    colors = {
        'DIVER': 'green',
        'DIST_01': 'orange',
        'DIST_02': 'purple'
    }
    
    # Sonar A
    ax = axes[0]
    for track_id in tracks_a['track_id'].unique():
        track_data = tracks_a[tracks_a['track_id'] == track_id].sort_values('timestamp')
        
        # 确定颜色
        color = 'gray'
        for key, c in colors.items():
            if key in track_id:
                color = c
                break
        
        label = track_id
        linestyle = '-' if 'DIVER' in track_id else '--'
        linewidth = 2.5 if 'DIVER' in track_id else 1.5
        alpha = 1.0 if 'DIVER' in track_id else 0.6
        
        ax.plot(track_data['x'], track_data['y'], color=color, 
               linestyle=linestyle, linewidth=linewidth, alpha=alpha, label=label)
        
        # 绘制起点
        ax.scatter(track_data['x'].iloc[0], track_data['y'].iloc[0],
                  c=color, s=50, marker='o', zorder=5)
    
    ax.set_xlabel('X Position (pixel units)', fontsize=12)
    ax.set_ylabel('Y Position (pixel units)', fontsize=12)
    ax.set_title(f'Sonar A Local Tracks - {experiment_name}', fontsize=14)
    ax.legend(loc='best', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal', adjustable='box')
    
    # Sonar B
    ax = axes[1]
    for track_id in tracks_b['track_id'].unique():
        track_data = tracks_b[tracks_b['track_id'] == track_id].sort_values('timestamp')
        
        color = 'gray'
        for key, c in colors.items():
            if key in track_id:
                color = c
                break
        
        label = track_id
        linestyle = '-' if 'DIVER' in track_id else '--'
        linewidth = 2.5 if 'DIVER' in track_id else 1.5
        alpha = 1.0 if 'DIVER' in track_id else 0.6
        
        ax.plot(track_data['x'], track_data['y'], color=color,
               linestyle=linestyle, linewidth=linewidth, alpha=alpha, label=label)
        ax.scatter(track_data['x'].iloc[0], track_data['y'].iloc[0],
                  c=color, s=50, marker='o', zorder=5)
    
    ax.set_xlabel('X Position (pixel units)', fontsize=12)
    ax.set_ylabel('Y Position (pixel units)', fontsize=12)
    ax.set_title(f'Sonar B Local Tracks - {experiment_name}', fontsize=14)
    ax.legend(loc='best', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal', adjustable='box')
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved local tracks plot: {output_path}")


def plot_track_association(tracks_a: pd.DataFrame, tracks_b: pd.DataFrame,
                          associations: pd.DataFrame,
                          output_path: str, experiment_name: str = ""):
    """
    绘制航迹关联结果
    
    显示:
    - 成功关联的航迹对用绿色连接
    - 未关联的干扰航迹用灰色弱化
    """
    fig, ax = plt.subplots(figsize=(14, 10))
    
    # 颜色映射
    track_colors_a = plt.cm.Blues(np.linspace(0.4, 0.9, len(tracks_a['track_id'].unique())))
    track_colors_b = plt.cm.Reds(np.linspace(0.4, 0.9, len(tracks_b['track_id'].unique())))
    
    # 绘制 Sonar A 航迹
    for i, track_id in enumerate(sorted(tracks_a['track_id'].unique())):
        track_data = tracks_a[tracks_a['track_id'] == track_id].sort_values('timestamp')
        
        is_diver = 'DIVER' in track_id
        color = 'steelblue' if is_diver else 'lightgray'
        linewidth = 3 if is_diver else 1
        alpha = 1.0 if is_diver else 0.4
        
        ax.plot(track_data['x'], track_data['y'], color=color,
               linewidth=linewidth, alpha=alpha, label=f"A: {track_id}")
        
        if is_diver:
            ax.scatter(track_data['x'].iloc[0], track_data['y'].iloc[0],
                      c='steelblue', s=100, marker='o', zorder=10)
    
    # 绘制 Sonar B 航迹
    for i, track_id in enumerate(sorted(tracks_b['track_id'].unique())):
        track_data = tracks_b[tracks_b['track_id'] == track_id].sort_values('timestamp')
        
        is_diver = 'DIVER' in track_id
        color = 'coral' if is_diver else 'lightgray'
        linewidth = 3 if is_diver else 1
        alpha = 1.0 if is_diver else 0.4
        linestyle = '-' if is_diver else '--'
        
        ax.plot(track_data['x'], track_data['y'], color=color,
               linewidth=linewidth, alpha=alpha, linestyle=linestyle,
               label=f"B: {track_id}")
        
        if is_diver:
            ax.scatter(track_data['x'].iloc[0], track_data['y'].iloc[0],
                      c='coral', s=100, marker='s', zorder=10)
    
    # 绘制成功关联对
    accepted_pairs = []
    if len(associations) > 0:
        for _, assoc in associations.iterrows():
            track_a_id = assoc['track_a_id']
            track_b_id = assoc['track_b_id']
            
            # 获取两条航迹的中点作为连接位置
            track_a_data = tracks_a[tracks_a['track_id'] == track_a_id]
            track_b_data = tracks_b[tracks_b['track_id'] == track_b_id]
            
            if len(track_a_data) > 0 and len(track_b_data) > 0:
                mid_x = (track_a_data['x'].mean() + track_b_data['x'].mean()) / 2
                mid_y = (track_a_data['y'].mean() + track_b_data['y'].mean()) / 2
                
                # 绘制连接线
                ax.plot([track_a_data['x'].mean(), track_b_data['x'].mean()],
                       [track_a_data['y'].mean(), track_b_data['y'].mean()],
                       'g-', linewidth=3, alpha=0.7)
                
                accepted_pairs.append((track_a_id, track_b_id))
    
    # 统计信息
    total_possible = len(tracks_a['track_id'].unique()) * len(tracks_b['track_id'].unique())
    accepted_count = len(accepted_pairs)
    
    ax.set_xlabel('X Position (pixel units)', fontsize=12)
    ax.set_ylabel('Y Position (pixel units)', fontsize=12)
    ax.set_title(f'Track Association Results - {experiment_name}\n'
                f'Accepted pairs: {accepted_count} / {total_possible}',
                fontsize=14)
    ax.legend(loc='upper right', fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal', adjustable='box')
    
    # 添加图例说明
    legend_elements = [
        Line2D([0], [0], color='steelblue', linewidth=3, label='A: DIVER'),
        Line2D([0], [0], color='coral', linewidth=3, label='B: DIVER'),
        Line2D([0], [0], color='green', linewidth=3, label='Association'),
        Line2D([0], [0], color='lightgray', linewidth=1, linestyle='--', label='Distractors (dimmed)')
    ]
    ax.legend(handles=legend_elements, loc='lower right', fontsize=10)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved track association plot: {output_path}")


def plot_cost_matrix(cost_matrix: pd.DataFrame, output_path: str,
                     experiment_name: str = ""):
    """
    绘制关联代价矩阵热力图
    """
    if len(cost_matrix) == 0:
        print("No cost matrix data to plot")
        return
    
    # 创建透视表
    pivot = cost_matrix.pivot_table(
        values='cost',
        index='track_a_id',
        columns='track_b_id',
        aggfunc='first'
    )
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    # 绘制热力图
    im = ax.imshow(pivot.values, cmap='RdYlGn_r', aspect='auto', vmin=0, vmax=1)
    
    # 设置标签
    ax.set_xticks(np.arange(len(pivot.columns)))
    ax.set_yticks(np.arange(len(pivot.index)))
    ax.set_xticklabels(pivot.columns, rotation=45, ha='right')
    ax.set_yticklabels(pivot.index)
    
    # 添加数值标签
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            value = pivot.values[i, j]
            if not np.isinf(value):
                text = ax.text(j, i, f'{value:.2f}',
                             ha="center", va="center", color="black", fontsize=9)
            else:
                text = ax.text(j, i, '∞',
                             ha="center", va="center", color="gray", fontsize=9)
    
    ax.set_xlabel('Track B ID', fontsize=12)
    ax.set_ylabel('Track A ID', fontsize=12)
    ax.set_title(f'Association Cost Matrix - {experiment_name}', fontsize=14)
    
    # 颜色条
    cbar = plt.colorbar(im, ax=ax)
    cbar.set_label('Association Cost (lower is better)', fontsize=10)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved cost matrix plot: {output_path}")


def plot_distance_vs_time(aligned_pairs: pd.DataFrame, 
                          diver_pair: Tuple[str, str],
                          output_path: str, experiment_name: str = "",
                          gate_threshold: float = 30.0):
    """
    绘制 DIVER-DIVER 位置距离随时间变化
    """
    # 筛选 DIVER 航迹对（兼容 A_DIVER、B_DIVER、A_T1、B_T1 等命名）
    diver_a_ids = ['A_DIVER', 'A_T1', 'A_Target1', 'A_TARGET1']
    diver_b_ids = ['B_DIVER', 'B_T1', 'B_Target1', 'B_TARGET1']
    
    # 找到 diver 航迹
    diver_pairs = aligned_pairs[
        aligned_pairs['track_a_id'].isin(diver_a_ids) &
        aligned_pairs['track_b_id'].isin(diver_b_ids)
    ]
    
    if len(diver_pairs) == 0:
        # 退化处理：找任何一对实际被关联的航迹
        # 取第一对 (a,b)
        first_pair = aligned_pairs.groupby(['track_a_id', 'track_b_id']).size().reset_index().iloc[0]
        diver_pairs = aligned_pairs[
            (aligned_pairs['track_a_id'] == first_pair['track_a_id']) &
            (aligned_pairs['track_b_id'] == first_pair['track_b_id'])
        ]
        if len(diver_pairs) == 0:
            print("No pairs found for distance plot")
            return
    
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    
    # 位置距离
    ax = axes[0]
    ax.plot(diver_pairs['timestamp'], diver_pairs['position_distance'], 
           'b-', linewidth=1.5, alpha=0.7, label='Position Distance')
    
    # 添加门限线
    ax.axhline(y=gate_threshold, color='r', linestyle='--', linewidth=2, 
              label=f'Gate Threshold ({gate_threshold}m)')
    
    # 统计信息
    mean_dist = diver_pairs['position_distance'].mean()
    median_dist = diver_pairs['position_distance'].median()
    ax.axhline(y=mean_dist, color='g', linestyle=':', linewidth=2,
              label=f'Mean ({mean_dist:.1f}m)')
    
    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_ylabel('Position Distance (m)', fontsize=12)
    ax.set_title(f'DIVER-DIVER Position Distance vs Time - {experiment_name}', fontsize=14)
    ax.legend(loc='best')
    ax.grid(True, alpha=0.3)
    
    # 速度差异
    ax = axes[1]
    if 'velocity_difference' in diver_pairs.columns:
        vel_data = diver_pairs['velocity_difference'].dropna()
        time_data = diver_pairs.loc[vel_data.index, 'timestamp']
        
        ax.plot(time_data, vel_data, 'purple', linewidth=1.5, alpha=0.7, 
               label='Velocity Difference')
        
        mean_vel = vel_data.mean()
        ax.axhline(y=mean_vel, color='orange', linestyle=':', linewidth=2,
                  label=f'Mean ({mean_vel:.1f} m/s)')
    
    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_ylabel('Velocity Difference (m/s)', fontsize=12)
    ax.set_title('DIVER-DIVER Velocity Difference vs Time', fontsize=14)
    ax.legend(loc='best')
    ax.grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved distance vs time plot: {output_path}")


def plot_association_timeline(history: pd.DataFrame, output_path: str,
                             experiment_name: str = ""):
    """
    绘制关联状态时间线
    
    显示 UNASSOCIATED -> TENTATIVE -> CONFIRMED -> COASTING -> LOST 的状态转换
    """
    if len(history) == 0:
        print("No history data to plot")
        return
    
    # 兼容多种 history 格式
    # 新格式: track_id, matched_partner
    # 旧格式: track_a_id, track_b_id
    
    if 'track_id' in history.columns and 'matched_partner' in history.columns:
        # 新格式：以单个 track_id 视角
        # 选取参与 confirmed 关联的 track 作为代表
        confirmed = history[history['status_after'] == 'CONFIRMED']
        if len(confirmed) > 0:
            # 找出现频率最高的 track
            top_track = confirmed['track_id'].value_counts().idxmax()
            diver_history = history[history['track_id'] == top_track].copy()
        else:
            top_track = history['track_id'].value_counts().idxmax()
            diver_history = history[history['track_id'] == top_track].copy()
    else:
        # 旧格式
        if 'track_a_id' in history.columns:
            diver_history = history[
                (history['track_a_id'].str.contains('DIVER', na=False)) &
                (history['track_b_id'].str.contains('DIVER', na=False))
            ]
        else:
            diver_history = history.copy()
    
    if len(diver_history) == 0:
        diver_history = history.copy()
    
    fig, ax = plt.subplots(figsize=(14, 6))
    
    # 状态颜色映射
    status_colors = {
        'UNASSOCIATED': 'gray',
        'TENTATIVE': 'orange',
        'CONFIRMED': 'green',
        'COASTING': 'blue',
        'LOST': 'red'
    }
    
    status_numeric = {
        'UNASSOCIATED': 0,
        'TENTATIVE': 1,
        'CONFIRMED': 2,
        'COASTING': 1.5,
        'LOST': 1
    }
    
    diver_history = diver_history.sort_values('timestamp')
    
    times = diver_history['timestamp'].values
    statuses = diver_history['status_after'].values
    
    numeric_status = [status_numeric.get(s, 0) for s in statuses]
    
    ax.plot(times, numeric_status, 'b-', linewidth=2, alpha=0.7)
    ax.scatter(times, numeric_status, c=[status_colors.get(s, 'gray') for s in statuses],
              s=30, zorder=5)
    
    for i in range(len(times) - 1):
        status = statuses[i]
        color = status_colors.get(status, 'gray')
        ax.fill_between([times[i], times[i+1]], 0, numeric_status[i],
                       alpha=0.2, color=color)
    
    ax.set_yticks([0, 1, 1.5, 2])
    ax.set_yticklabels(['UNASSOCIATED', 'TENTATIVE/LOST', 'COASTING', 'CONFIRMED'])
    
    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_ylabel('Association Status', fontsize=12)
    ax.set_title(f'Association Timeline - {experiment_name}', fontsize=14)
    ax.grid(True, alpha=0.3, axis='y')
    
    legend_elements = [
        mpatches.Patch(color='green', alpha=0.3, label='CONFIRMED'),
        mpatches.Patch(color='orange', alpha=0.3, label='TENTATIVE'),
        mpatches.Patch(color='blue', alpha=0.3, label='COASTING'),
        mpatches.Patch(color='gray', alpha=0.3, label='UNASSOCIATED'),
        mpatches.Patch(color='red', alpha=0.3, label='LOST')
    ]
    ax.legend(handles=legend_elements, loc='best')
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"Saved association timeline plot: {output_path}")


def plot_all_results(output_dir: str, config: dict,
                    reference_track: pd.DataFrame,
                    tracks_a: pd.DataFrame, tracks_b: pd.DataFrame,
                    associations: pd.DataFrame,
                    cost_matrix: pd.DataFrame,
                    aligned_pairs: pd.DataFrame,
                    history: pd.DataFrame,
                    experiment_name: str = ""):
    """
    绘制所有结果图
    """
    figures_dir = Path(output_dir) / 'figures'
    figures_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 传感器几何布局
    plot_sensor_geometry(
        config, reference_track,
        str(figures_dir / '01_sensor_geometry.png'),
        experiment_name
    )
    
    # 2. 本地航迹
    plot_local_tracks(
        tracks_a, tracks_b,
        str(figures_dir / '02_local_tracks.png'),
        experiment_name
    )
    
    # 3. 航迹关联结果
    plot_track_association(
        tracks_a, tracks_b, associations,
        str(figures_dir / '03_track_association.png'),
        experiment_name
    )
    
    # 4. 代价矩阵
    if len(cost_matrix) > 0:
        plot_cost_matrix(
            cost_matrix,
            str(figures_dir / '04_association_cost_matrix.png'),
            experiment_name
        )
    
    # 5. 距离随时间变化
    if len(aligned_pairs) > 0:
        plot_distance_vs_time(
            aligned_pairs, ('A_DIVER', 'B_DIVER'),
            str(figures_dir / '05_distance_vs_time.png'),
            experiment_name,
            gate_threshold=config['association'].get('position_gate_m', 30.0)
        )
    
    # 6. 关联时间线
    if len(history) > 0:
        plot_association_timeline(
            history,
            str(figures_dir / '06_association_timeline.png'),
            experiment_name
        )
    
    print(f"\nAll figures saved to {figures_dir}")


def main():
    """测试函数"""
    import argparse
    import json
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from two_sonar_paths import DEFAULT_CONFIG

    parser = argparse.ArgumentParser(description='Plot association results')
    parser.add_argument('--output-dir', type=str, required=True,
                       help='Output directory with results')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Config file')
    parser.add_argument('--experiment', type=str, default='Test',
                       help='Experiment name')
    
    args = parser.parse_args()
    
    output_dir = Path(args.output_dir)
    
    # 加载配置
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # 加载数据
    reference_track = pd.read_csv(output_dir / 'reference_track.csv')
    
    try:
        tracks_a = pd.read_csv(output_dir / 'sonar_A_tracks.csv')
    except:
        tracks_a = pd.DataFrame()
    
    try:
        tracks_b = pd.read_csv(output_dir / 'sonar_B_tracks.csv')
    except:
        tracks_b = pd.DataFrame()
    
    try:
        associations = pd.read_csv(output_dir / 'association_results.csv')
    except:
        associations = pd.DataFrame()
    
    try:
        cost_matrix = pd.read_csv(output_dir / 'association_cost_matrix.csv')
    except:
        cost_matrix = pd.DataFrame()
    
    try:
        aligned_pairs = pd.read_csv(output_dir / 'aligned_track_pairs.csv')
    except:
        aligned_pairs = pd.DataFrame()
    
    try:
        history = pd.read_csv(output_dir / 'association_history.csv')
    except:
        history = pd.DataFrame()
    
    # 绘制所有图
    plot_all_results(
        str(output_dir), config,
        reference_track,
        tracks_a, tracks_b,
        associations, cost_matrix,
        aligned_pairs, history,
        args.experiment
    )


if __name__ == '__main__':
    main()
