#!/usr/bin/env python3
"""
Track Fusion Architecture / Flow Diagram
展示数据流: Local Tracks (A, B) -> CovarianceWeightedFusion -> Fused Global Track
突出 1.1+1.2+1.3 引入的新增模块
"""
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

BASE = Path(__file__).resolve().parent
OUT = BASE / 'outputs' / 'regression_benchmark' / 'figures_fusion' / '03_architecture_diagram.png'
OUT.parent.mkdir(parents=True, exist_ok=True)


def main():
    fig, ax = plt.subplots(figsize=(16, 10))
    ax.set_xlim(0, 16)
    ax.set_ylim(0, 10)
    ax.axis('off')

    # 颜色
    COLOR_DATA = '#E8F4FD'        # 浅蓝（数据）
    COLOR_PROCESS = '#FFF4E6'     # 浅橙（处理）
    COLOR_FUSION = '#E6F7E6'      # 浅绿（融合）
    COLOR_NEW = '#FFE6F0'         # 浅粉（1.1/1.2/1.3 新增）
    COLOR_BORDER = 'black'

    # ===== Title =====
    ax.text(8, 9.5, 'Track Fusion Architecture (Case 5)',
           fontsize=18, fontweight='bold', ha='center')
    ax.text(8, 9.0, 'Local Tracks (A, B) -> CovarianceWeightedFusion -> Fused Global Track',
           fontsize=11, ha='center', style='italic', color='gray')

    # ===== 输入: Local Tracks =====
    # A tracks
    box = FancyBboxPatch((0.3, 6.5), 3.0, 1.6, boxstyle='round,pad=0.1',
                         facecolor=COLOR_DATA, edgecolor=COLOR_BORDER, linewidth=1.5)
    ax.add_patch(box)
    ax.text(1.8, 7.7, 'Local Track A', fontsize=12, fontweight='bold', ha='center')
    ax.text(1.8, 7.3, 'Kalman: x_a, P_a, t_a', fontsize=9, ha='center', family='monospace')
    ax.text(1.8, 6.95, '(5Hz, sensor A)', fontsize=9, ha='center', color='gray')

    # B tracks
    box = FancyBboxPatch((0.3, 4.0), 3.0, 1.6, boxstyle='round,pad=0.1',
                         facecolor=COLOR_DATA, edgecolor=COLOR_BORDER, linewidth=1.5)
    ax.add_patch(box)
    ax.text(1.8, 5.2, 'Local Track B', fontsize=12, fontweight='bold', ha='center')
    ax.text(1.8, 4.8, 'Kalman: x_b, P_b, t_b', fontsize=9, ha='center', family='monospace')
    ax.text(1.8, 4.45, '(4.4Hz, sensor B)', fontsize=9, ha='center', color='gray')

    # ===== 中间处理: extract_state =====
    box = FancyBboxPatch((4.5, 4.8), 3.0, 2.5, boxstyle='round,pad=0.1',
                         facecolor=COLOR_PROCESS, edgecolor=COLOR_BORDER, linewidth=1.5)
    ax.add_patch(box)
    ax.text(6.0, 7.0, '_extract_state()', fontsize=12, fontweight='bold', ha='center')
    ax.text(6.0, 6.5, 'find nearest frame', fontsize=9, ha='center')
    ax.text(6.0, 6.2, 'in time window [t-0.12, t+0.12]', fontsize=9, ha='center', family='monospace')
    ax.text(6.0, 5.8, 'return: (x, P, has_meas, t_meas)', fontsize=9, ha='center', family='monospace')
    # 1.1 新增标注
    ax.text(6.0, 5.35, '[1.1] returns 4-tuple with t_meas', fontsize=8, ha='center',
           color='#C71585', style='italic')

    # 箭头
    arr = FancyArrowPatch((3.3, 7.3), (4.5, 6.3), arrowstyle='->', mutation_scale=15, color='black')
    ax.add_patch(arr)
    arr = FancyArrowPatch((3.3, 4.8), (4.5, 6.0), arrowstyle='->', mutation_scale=15, color='black')
    ax.add_patch(arr)

    # ===== 1.1 状态外推 (新增模块) =====
    box = FancyBboxPatch((4.5, 2.2), 3.0, 1.8, boxstyle='round,pad=0.1',
                         facecolor=COLOR_NEW, edgecolor='red', linewidth=2.0)
    ax.add_patch(box)
    ax.text(6.0, 3.7, '[1.1] State Extrapolation', fontsize=11, fontweight='bold', ha='center', color='red')
    ax.text(6.0, 3.3, '_extrapolate_state()', fontsize=10, ha='center', family='monospace')
    ax.text(6.0, 2.9, 'x += v * dt', fontsize=8, ha='center', family='monospace')
    ax.text(6.0, 2.6, 'P = F*P*F.T + Q*|dt|', fontsize=8, ha='center', family='monospace')
    ax.text(6.0, 2.35, 'Default: OFF (no gain)', fontsize=8, ha='center', color='gray', style='italic')

    # 箭头
    arr = FancyArrowPatch((6.0, 4.8), (6.0, 4.0), arrowstyle='->', mutation_scale=12, color='red')
    ax.add_patch(arr)

    # ===== 主融合函数 fuse() =====
    box = FancyBboxPatch((8.5, 4.0), 3.5, 3.3, boxstyle='round,pad=0.1',
                         facecolor=COLOR_FUSION, edgecolor='green', linewidth=2.5)
    ax.add_patch(box)
    ax.text(10.25, 7.05, 'fuse()', fontsize=13, fontweight='bold', ha='center', color='darkgreen')
    ax.text(10.25, 6.6, 'Covariance-Weighted Fusion', fontsize=10, ha='center')

    # 子步骤
    ax.text(8.7, 6.2, '1) Outlier check', fontsize=9, ha='left', family='monospace')
    ax.text(8.7, 5.85, '   d_pos / sqrt(P_a+P_b) > thr', fontsize=8, ha='left', family='monospace')
    ax.text(8.7, 5.55, '2) [1.2] inflate P_check', fontsize=8, ha='left', family='monospace', color='#C71585')
    ax.text(8.7, 5.25, '3) [1.3] outlier_pick_min_p', fontsize=8, ha='left', family='monospace', color='#C71585')
    ax.text(8.7, 4.95, '4) Information matrix', fontsize=8, ha='left', family='monospace')
    ax.text(8.7, 4.65, '   P_f = (P_a^-1 + P_b^-1)^-1', fontsize=8, ha='left', family='monospace')
    ax.text(8.7, 4.35, '5) pos_f = P_f @ (P_a^-1 x_a + P_b^-1 x_b)', fontsize=8, ha='left', family='monospace')

    # 箭头 (extrapolation -> fuse)
    arr = FancyArrowPatch((7.5, 3.1), (8.5, 5.5), arrowstyle='->', mutation_scale=15, color='red')
    ax.add_patch(arr)
    ax.text(8.0, 4.2, 'extrapolated\n(x, P)', fontsize=8, ha='center', family='monospace', color='red')

    # ===== 输出: Fused Global Track =====
    box = FancyBboxPatch((13.5, 4.5), 2.3, 2.3, boxstyle='round,pad=0.1',
                         facecolor=COLOR_FUSION, edgecolor='darkgreen', linewidth=2.0)
    ax.add_patch(box)
    ax.text(14.65, 6.4, 'Fused Global Track', fontsize=11, fontweight='bold', ha='center', color='darkgreen')
    ax.text(14.65, 5.9, 'x_fused, P_fused', fontsize=9, ha='center', family='monospace')
    ax.text(14.65, 5.5, 'v_fused', fontsize=9, ha='center', family='monospace')
    ax.text(14.65, 5.15, 'fusion_mode', fontsize=9, ha='center', family='monospace')
    ax.text(14.65, 4.8, 'fallback_reason', fontsize=9, ha='center', family='monospace')

    arr = FancyArrowPatch((12.0, 5.6), (13.5, 5.6), arrowstyle='->', mutation_scale=15, color='green')
    ax.add_patch(arr)

    # ===== 底部: 配置 / 输出 =====
    # 配置
    box = FancyBboxPatch((4.5, 0.3), 6.0, 1.3, boxstyle='round,pad=0.1',
                         facecolor='#F0F0F0', edgecolor='black', linewidth=1.0)
    ax.add_patch(box)
    ax.text(7.5, 1.3, 'config/fusion.json (7 new params)', fontsize=10, fontweight='bold', ha='center')
    ax.text(7.5, 1.0, 'extrapolation_enabled, max_extrapolation_s,', fontsize=8, ha='center', family='monospace')
    ax.text(7.5, 0.75, 'process_noise_pos/vel, position_inflation,', fontsize=8, ha='center', family='monospace')
    ax.text(7.5, 0.5, 'outlier_sigma_threshold, outlier_pick_min_p', fontsize=8, ha='center', family='monospace')

    # 输出 CSV
    box = FancyBboxPatch((11.5, 0.3), 4.3, 1.3, boxstyle='round,pad=0.1',
                         facecolor='#F0F0F0', edgecolor='black', linewidth=1.0)
    ax.add_patch(box)
    ax.text(13.65, 1.3, 'Output: fused_global_tracks.csv', fontsize=10, fontweight='bold', ha='center')
    ax.text(13.65, 1.0, 'case{N}/fused_global_tracks.csv', fontsize=8, ha='center', family='monospace')
    ax.text(13.65, 0.75, '+ figures_fusion/ (22 PNG figures)', fontsize=8, ha='center', family='monospace')
    ax.text(13.65, 0.5, '+ benchmark_comparison.md', fontsize=8, ha='center', family='monospace')

    # ===== 关键诊断框 =====
    box = FancyBboxPatch((0.3, 0.3), 3.7, 1.3, boxstyle='round,pad=0.1',
                         facecolor='#FFE6E6', edgecolor='red', linewidth=1.0)
    ax.add_patch(box)
    ax.text(2.15, 1.3, 'Root Cause Diagnosis', fontsize=10, fontweight='bold', ha='center', color='red')
    ax.text(2.15, 1.0, 'A vs B error corr = 0.93', fontsize=8, ha='center', family='monospace')
    ax.text(2.15, 0.75, 'P inflation = 44x', fontsize=8, ha='center', family='monospace')
    ax.text(2.15, 0.5, 'Oracle limit = 9.033 (only 9% gain)', fontsize=8, ha='center', family='monospace')

    # 左侧 legend
    legend_y = 9.5
    patches = [mpatches.Patch(color=COLOR_DATA, label='Data (tracks)'),
              mpatches.Patch(color=COLOR_PROCESS, label='Process (existing)'),
              mpatches.Patch(color=COLOR_FUSION, label='Fusion (core)'),
              mpatches.Patch(color=COLOR_NEW, label='1.1 New module')]
    ax.legend(handles=patches, loc='upper right', bbox_to_anchor=(0.85, 1.0), fontsize=9)

    plt.tight_layout()
    plt.savefig(OUT, dpi=130, bbox_inches='tight')
    plt.close()
    print(f'Saved: {OUT}')


if __name__ == '__main__':
    main()
