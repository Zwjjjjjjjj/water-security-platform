#!/usr/bin/env python3
"""
Summary visualization for Case 5.5 (1.1/1.2 优化探索)
展示:
- 各优化版本的 RMSE 对比 (vs baseline)
- A vs B 误差相关性
- 各模式对 RMSE 贡献
- P inflation 验证
"""
import sys, json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

BASE = Path(__file__).resolve().parent
REGRESSION_DIR = BASE / 'outputs' / 'regression_benchmark'
FIG_DIR = REGRESSION_DIR / 'figures_fusion'
FIG_DIR.mkdir(parents=True, exist_ok=True)


def fig_optimization_summary():
    """优化探索总结图"""
    fig, axes = plt.subplots(2, 2, figsize=(16, 11))

    # ===== Subplot 1: 各优化版本 RMSE 对比 (case1) =====
    ax = axes[0, 0]
    versions = ['Baseline\n(no extrap)', '1.1 单边外推\ndt≤0.06s', '1.1 对称外推\nv2', '1.1 v3\n(safe gate)',
                '1.2 inflation\n=44x', '1.3 outlier\nthr=1.5', '1.3 pick\nmin(P)', 'oracle\nmin(A,B,F)']
    rmses = [9.956, 10.041, 10.093, 10.042, 9.956, 9.911, 9.956, 9.033]
    colors = ['green' if r < 9.956 else ('gray' if abs(r-9.956)<0.01 else 'salmon') for r in rmses]
    bars = ax.barh(versions, rmses, color=colors, edgecolor='black')
    ax.axvline(9.956, color='green', linestyle='--', linewidth=2, label='Baseline 9.956')
    ax.axvline(9.033, color='blue', linestyle='--', linewidth=2, label='Oracle limit 9.033')
    ax.set_xlabel('RMSE (m)', fontsize=11)
    ax.set_title('Optimization Attempts - case1 RMSE', fontsize=12, fontweight='bold')
    ax.legend(loc='lower right', fontsize=9)
    ax.grid(True, alpha=0.3, axis='x')
    for i, v in enumerate(rmses):
        offset = 0.05 if v > 9.5 else -0.15
        ax.text(v + offset, i, f'{v:.3f}', va='center', fontsize=9)
    ax.set_xlim(8.8, 10.3)

    # ===== Subplot 2: 各模式对 RMSE² 贡献 (case1) =====
    ax = axes[0, 1]
    case = 'case1_single'
    cd = REGRESSION_DIR / case
    fused = pd.read_csv(cd / 'fused_global_tracks.csv')
    ref = pd.read_csv(cd / 'reference_track_T1.csv')
    ref_t = ref['timestamp'].values
    ref_x = ref['x_ref'].values
    ref_y = ref['y_ref'].values
    errs = []
    for _, r in fused.iterrows():
        if pd.isna(r['x_fused']): continue
        t = r['timestamp']
        idx = int(np.argmin(np.abs(ref_t - t)))
        if abs(ref_t[idx] - t) > 0.1: continue
        e = (r['x_fused']-ref_x[idx])**2 + (r['y_fused']-ref_y[idx])**2
        errs.append((r['fusion_mode'], e))
    df_e = pd.DataFrame(errs, columns=['mode','sq_err'])
    contrib = df_e.groupby('mode')['sq_err'].agg(['sum', 'count', 'mean'])
    contrib['total_rmse2'] = df_e['sq_err'].sum() / len(df_e)
    contrib['pct'] = 100 * (contrib['sum'] / len(df_e)) / contrib['total_rmse2']

    modes = contrib.index.tolist()
    pct = contrib['pct'].values
    color_map = {'BOTH_MEASURED': '#2ca02c', 'A_ONLY': '#1f77b4', 'B_ONLY': '#d62728'}
    bar_colors = [color_map.get(m, 'gray') for m in modes]
    wedges, texts, autotexts = ax.pie(pct, labels=modes, colors=bar_colors,
                                       autopct=lambda p: f'{p:.1f}%\n({p*contrib["total_rmse2"].iloc[0]/100:.0f} var)',
                                       startangle=90, textprops={'fontsize': 10})
    ax.set_title('case1: RMSE² Contribution by Mode\nBOTH 50%, A 25%, B 25%',
                fontsize=12, fontweight='bold')

    # ===== Subplot 3: A vs B 测量误差相关性 =====
    ax = axes[1, 0]
    a = pd.read_csv(cd / 'sonar_A_tracks.csv')
    b = pd.read_csv(cd / 'sonar_B_tracks.csv')
    recs = []
    for _, r in fused.iterrows():
        if pd.isna(r['x_fused']): continue
        t = r['timestamp']
        idx = int(np.argmin(np.abs(ref_t - t)))
        if abs(ref_t[idx] - t) > 0.1: continue
        ia = int(np.argmin(np.abs(a['timestamp'].values - t)))
        ib = int(np.argmin(np.abs(b['timestamp'].values - t)))
        e_a = np.sqrt((a.iloc[ia]['x']-ref_x[idx])**2 + (a.iloc[ia]['y']-ref_y[idx])**2)
        e_b = np.sqrt((b.iloc[ib]['x']-ref_x[idx])**2 + (b.iloc[ib]['y']-ref_y[idx])**2)
        recs.append((e_a, e_b))
    df_r = pd.DataFrame(recs, columns=['e_a','e_b'])
    ax.scatter(df_r['e_a'], df_r['e_b'], alpha=0.5, s=15, color='steelblue', edgecolor='none')
    # 拟合线
    z = np.polyfit(df_r['e_a'], df_r['e_b'], 1)
    xs = np.linspace(df_r['e_a'].min(), df_r['e_a'].max(), 100)
    ax.plot(xs, np.polyval(z, xs), 'r--', alpha=0.7, label=f'fit: y={z[0]:.2f}x+{z[1]:.2f}')
    ax.set_title('1:1 line (perfect calibration)', fontsize=10, alpha=0.7)
    corr = df_r['e_a'].corr(df_r['e_b'])
    ax.set_xlabel('Sonar A Error (m)', fontsize=11)
    ax.set_ylabel('Sonar B Error (m)', fontsize=11)
    ax.set_title(f'A vs B Error Correlation - rho={corr:.3f}\n(High correlation limits fusion gain)',
                fontsize=12, fontweight='bold')
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 25)
    ax.set_ylim(0, 25)

    # ===== Subplot 4: P inflation 验证 =====
    ax = axes[1, 1]
    # 真实 |e|² vs P 自报
    real_sq = []
    kalman_p = []
    for df_src, label in [(a, 'A'), (b, 'B')]:
        for _, r in df_src.iterrows():
            if pd.isna(r['x']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.15: continue
            real_sq.append((r['x']-ref_x[idx])**2 + (r['y']-ref_y[idx])**2)
            kalman_p.append(r['Pxx'] + r['Pyy'])
    real_sq = np.array(real_sq)
    kalman_p = np.array(kalman_p)
    ax.scatter(kalman_p, real_sq, alpha=0.5, s=15, color='coral', edgecolor='none')
    # y=x 线
    ax.plot([0, 5], [0, 5], 'k:', alpha=0.5, label='y=x (perfect)')
    # 真实 inflation 线
    inflation = real_sq.mean() / kalman_p.mean()
    ax.plot([0, 5], [0, 5*inflation], 'b--', alpha=0.7,
           label=f'Mean inflation: {inflation:.1f}x')
    ax.set_xlabel('Kalman self-reported P trace (m²)', fontsize=11)
    ax.set_ylabel('Real measurement |e|² (m²)', fontsize=11)
    ax.set_title(f'P Inflation - Kalman P is {inflation:.0f}x smaller than reality\n(P_self ~ 1.2 vs P_real ~ {inflation*1.2:.0f})',
                fontsize=12, fontweight='bold')
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 5)
    ax.set_ylim(0, 300)

    plt.suptitle('Case 5.5 - Optimization Diagnosis (Why fuse-layer hits a ceiling)',
                fontsize=15, fontweight='bold')
    plt.tight_layout()
    out = FIG_DIR / '02_optimization_summary.png'
    plt.savefig(out, dpi=130, bbox_inches='tight')
    plt.close()
    return out


def main():
    print('Generating optimization summary figure...')
    out = fig_optimization_summary()
    print(f'Saved: {out}')


if __name__ == '__main__':
    main()
