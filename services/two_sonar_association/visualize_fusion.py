#!/usr/bin/env python3
"""
Visualization for Track Fusion Results (Case 5)
4 cases × N figures: local tracks, fused vs reference, fusion mode distribution, etc.

Output: outputs/regression_benchmark/figures_fusion/<case>/*.png
"""
import sys
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).parent / 'src'))

BASE = Path(__file__).resolve().parent
REGRESSION_DIR = BASE / 'outputs' / 'regression_benchmark'
FIG_DIR = REGRESSION_DIR / 'figures_fusion'
FIG_DIR.mkdir(parents=True, exist_ok=True)

CASES = ['case1_single', 'case2_separated', 'case3_crossing', 'case4_crossing_break']
CASE_TITLES = {
    'case1_single': 'Case 1: Single Target',
    'case2_separated': 'Case 2: Separated Targets',
    'case3_crossing': 'Case 3: Crossing Targets',
    'case4_crossing_break': 'Case 4: Crossing + Track Break',
}

# 颜色方案
COLOR_REF = 'black'
COLOR_A = '#1f77b4'    # blue
COLOR_B = '#d62728'    # red
COLOR_FUSED = '#2ca02c' # green
COLOR_PRED = '#ff7f0e'  # orange


def fig_geometry_with_fusion(case_dir, case, title):
    """图 1: 传感器布局 + 参考航迹 + 融合航迹 (主图)"""
    fig, ax = plt.subplots(figsize=(14, 11))

    # 传感器位置
    config = json.load(open(BASE / 'config' / 'two_sonar_association.json'))
    sa = config['sonar_A']
    sb = config['sonar_B']
    ax.scatter(sa['x_m'], sa['y_m'], c=COLOR_A, s=300, marker='^', zorder=20,
              label=f"Sonar A", edgecolor='black', linewidth=2)
    ax.scatter(sb['x_m'], sb['y_m'], c=COLOR_B, s=300, marker='s', zorder=20,
              label=f"Sonar B", edgecolor='black', linewidth=2)
    # 朝向箭头
    for s, color, name in [(sa, COLOR_A, 'A'), (sb, COLOR_B, 'B')]:
        h = np.radians(s['heading_deg'])
        ax.arrow(s['x_m'], s['y_m'], 30*np.sin(h), 30*np.cos(h),
                head_width=5, head_length=3, fc=color, ec=color, alpha=0.6, zorder=19)

    # 参考航迹 T1, T2...
    ref_files = sorted(case_dir.glob('reference_track_T*.csv'))
    for i, rf in enumerate(ref_files):
        ref = pd.read_csv(rf)
        color = ['black', 'magenta', 'cyan', 'brown'][i % 4]
        ax.plot(ref['x_ref'], ref['y_ref'], color=color, linewidth=2.5,
               linestyle='-', label=f"Ref {rf.stem.split('_')[-1]}", zorder=10)
        ax.scatter(ref['x_ref'].iloc[0], ref['y_ref'].iloc[0], color=color, s=100,
                  marker='*', zorder=11)

    # Local tracks (A, B)
    for sensor, color, prefix in [(COLOR_A, COLOR_A, 'A'), (COLOR_B, COLOR_B, 'B')]:
        tf = case_dir / f'sonar_{prefix}_tracks.csv'
        if tf.exists():
            tracks = pd.read_csv(tf)
            for tid in tracks['track_id'].unique():
                tr = tracks[tracks['track_id']==tid].sort_values('timestamp')
                ls = '-' if 'DIVER' in tid or 'T1' in tid or 'T2' in tid else ':'
                ax.plot(tr['x'], tr['y'], color=color, alpha=0.4, linewidth=1.2,
                       linestyle=ls, zorder=5)

    # Fused tracks
    fused = pd.read_csv(case_dir / 'fused_global_tracks.csv')
    for gid in fused['global_id'].unique():
        g = fused[fused['global_id']==gid].dropna(subset=['x_fused']).sort_values('timestamp')
        ax.plot(g['x_fused'], g['y_fused'], color=COLOR_FUSED, linewidth=2.8,
               label=f"Fused {gid}", zorder=15)
        ax.scatter(g['x_fused'].iloc[0], g['y_fused'].iloc[0], color=COLOR_FUSED,
                  s=120, marker='o', edgecolor='black', zorder=16)

    ax.set_xlabel('X (m)', fontsize=13)
    ax.set_ylabel('Y (m)', fontsize=13)
    ax.set_title(f'{title} — Sensor Geometry & Fused Tracks', fontsize=15)
    ax.legend(loc='best', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_aspect('equal', adjustable='box')
    plt.tight_layout()
    out = FIG_DIR / case / '01_geometry_fused.png'
    out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=130, bbox_inches='tight')
    plt.close()
    return out


def fig_fused_vs_reference_error(case_dir, case, title):
    """图 2: 融合后位置 vs 参考航迹的时间序列误差"""
    fused = pd.read_csv(case_dir / 'fused_global_tracks.csv')
    a = pd.read_csv(case_dir / 'sonar_A_tracks.csv')
    b = pd.read_csv(case_dir / 'sonar_B_tracks.csv')

    # 取 T1 参考
    ref_files = sorted(case_dir.glob('reference_track_T*.csv'))
    targets = {}
    for rf in ref_files:
        tid = rf.stem.split('_')[-1]
        targets[tid] = pd.read_csv(rf)

    n_targets = len(targets)
    if n_targets == 0:
        return None
    fig, axes = plt.subplots(n_targets, 1, figsize=(14, 4*n_targets), sharex=True)
    if n_targets == 1:
        axes = [axes]

    for ax, (tid, ref) in zip(axes, targets.items()):
        # 找对应的 fused global
        ref_t = ref['timestamp'].values
        ref_x = ref['x_ref'].values
        ref_y = ref['y_ref'].values

        # 误差 (融合)
        errs_f = []
        ts_f = []
        modes = []
        for _, r in fused.iterrows():
            if pd.isna(r['x_fused']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            e = np.sqrt((r['x_fused']-ref_x[idx])**2 + (r['y_fused']-ref_y[idx])**2)
            errs_f.append(e)
            ts_f.append(t)
            modes.append(r['fusion_mode'])

        # 误差 (A)
        errs_a = []
        ts_a = []
        for _, r in a.iterrows():
            if pd.isna(r['x']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            e = np.sqrt((r['x']-ref_x[idx])**2 + (r['y']-ref_y[idx])**2)
            errs_a.append(e)
            ts_a.append(t)
        # 误差 (B)
        errs_b = []
        ts_b = []
        for _, r in b.iterrows():
            if pd.isna(r['x']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            e = np.sqrt((r['x']-ref_x[idx])**2 + (r['y']-ref_y[idx])**2)
            errs_b.append(e)
            ts_b.append(t)

        # 画线
        ax.plot(ts_a, errs_a, '-', color=COLOR_A, alpha=0.4, linewidth=1.0, label='Sonar A')
        ax.plot(ts_b, errs_b, '-', color=COLOR_B, alpha=0.4, linewidth=1.0, label='Sonar B')
        # Fused 按 mode 上色
        ts_f = np.array(ts_f)
        errs_f = np.array(errs_f)
        modes = np.array(modes)
        for mode, color, marker, ms in [('BOTH_MEASURED', COLOR_FUSED, 'o', 18),
                                         ('A_ONLY', COLOR_A, '^', 30),
                                         ('B_ONLY', COLOR_B, 's', 30)]:
            mask = modes == mode
            if mask.sum() > 0:
                ax.scatter(ts_f[mask], errs_f[mask], c=color, s=ms, marker=marker,
                          edgecolor='black', linewidth=0.5, alpha=0.7,
                          label=f'Fused {mode}')

        # RMSE 标注
        if len(errs_f) > 0:
            rmse_f = np.sqrt(np.mean(np.array(errs_f)**2))
            rmse_a = np.sqrt(np.mean(np.array(errs_a)**2)) if errs_a else 0
            rmse_b = np.sqrt(np.mean(np.array(errs_b)**2)) if errs_b else 0
            ax.axhline(rmse_f, color=COLOR_FUSED, linestyle='--', alpha=0.5,
                      label=f'Fused RMSE={rmse_f:.2f}')
            ax.axhline(rmse_a, color=COLOR_A, linestyle=':', alpha=0.5,
                      label=f'A RMSE={rmse_a:.2f}')
            ax.axhline(rmse_b, color=COLOR_B, linestyle=':', alpha=0.5,
                      label=f'B RMSE={rmse_b:.2f}')

        ax.set_ylabel('Position Error (m)', fontsize=11)
        ax.set_title(f'{title} — Error vs Reference {tid}', fontsize=12)
        ax.legend(loc='upper right', fontsize=8, ncol=3)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0)

    axes[-1].set_xlabel('Time (s)', fontsize=12)
    plt.tight_layout()
    out = FIG_DIR / case / '02_error_vs_ref.png'
    plt.savefig(out, dpi=120, bbox_inches='tight')
    plt.close()
    return out


def fig_fusion_mode_pie(case_dir, case, title):
    """图 3: 融合模式饼图 + fallback reason"""
    fused = pd.read_csv(case_dir / 'fused_global_tracks.csv')
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # 饼图 1: fusion mode
    mode_counts = fused['fusion_mode'].value_counts()
    colors = {'BOTH_MEASURED': COLOR_FUSED, 'A_ONLY': COLOR_A, 'B_ONLY': COLOR_B, 'NONE': 'gray'}
    c = [colors.get(m, 'gray') for m in mode_counts.index]
    axes[0].pie(mode_counts.values, labels=mode_counts.index, colors=c,
               autopct='%1.1f%%', startangle=90, textprops={'fontsize': 12})
    axes[0].set_title(f'{title} — Fusion Mode Distribution', fontsize=13)

    # 饼图 2: fallback reason
    fbr = fused['fallback_reason'].fillna('NO_FALLBACK').value_counts()
    fbr_colors = {'NO_FALLBACK': 'lightgray',
                  'outlier_rejected_B': '#ff7f0e',
                  'outlier_rejected_A': '#9467bd',
                  'degenerate_covariance': 'lightcoral',
                  'position_fusion_singular': 'salmon',
                  'no_local_tracks': 'darkgray'}
    c2 = [fbr_colors.get(r, 'gray') for r in fbr.index]
    axes[1].pie(fbr.values, labels=fbr.index, colors=c2,
               autopct='%1.1f%%', startangle=90, textprops={'fontsize': 10})
    axes[1].set_title(f'{title} — Fallback Reason Distribution', fontsize=13)

    plt.tight_layout()
    out = FIG_DIR / case / '03_fusion_mode_pie.png'
    plt.savefig(out, dpi=120, bbox_inches='tight')
    plt.close()
    return out


def fig_covariance_evolution(case_dir, case, title):
    """图 4: 融合后协方差 trace 时序"""
    fused = pd.read_csv(case_dir / 'fused_global_tracks.csv')
    a = pd.read_csv(case_dir / 'sonar_A_tracks.csv')
    b = pd.read_csv(case_dir / 'sonar_B_tracks.csv')

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    # 位置 Pxx 演化
    ax = axes[0]
    ax.plot(a['timestamp'], a['Pxx'], color=COLOR_A, alpha=0.5, label='A Pxx')
    ax.plot(b['timestamp'], b['Pxx'], color=COLOR_B, alpha=0.5, label='B Pxx')
    if 'Pxx_fused' in fused.columns:
        ax.plot(fused['timestamp'], fused['Pxx_fused'], color=COLOR_FUSED, linewidth=2,
               label='Fused Pxx')
    ax.set_ylabel('Pxx (m²)', fontsize=11)
    ax.set_title(f'{title} — Position Covariance Pxx Evolution', fontsize=12)
    ax.legend(loc='upper right', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_yscale('log')

    # 速度 Pvxvx 演化
    ax = axes[1]
    if 'Pvxvx' in a.columns:
        ax.plot(a['timestamp'], a['Pvxvx'], color=COLOR_A, alpha=0.5, label='A Pvxvx')
    if 'Pvxvx' in b.columns:
        ax.plot(b['timestamp'], b['Pvxvx'], color=COLOR_B, alpha=0.5, label='B Pvxvx')
    if 'Pvxvx_fused' in fused.columns:
        ax.plot(fused['timestamp'], fused['Pvxvx_fused'], color=COLOR_FUSED, linewidth=2,
               label='Fused Pvxvx')
    ax.set_xlabel('Time (s)', fontsize=11)
    ax.set_ylabel('Pvxvx (m²/s²)', fontsize=11)
    ax.set_title(f'{title} — Velocity Covariance Pvxvx Evolution', fontsize=12)
    ax.legend(loc='upper right', fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.set_yscale('log')

    plt.tight_layout()
    out = FIG_DIR / case / '04_covariance_evolution.png'
    plt.savefig(out, dpi=120, bbox_inches='tight')
    plt.close()
    return out


def fig_fusion_mode_timeline(case_dir, case, title):
    """图 5: 融合模式时序（颜色条带）"""
    fused = pd.read_csv(case_dir / 'fused_global_tracks.csv')

    fig, ax = plt.subplots(figsize=(14, 4))
    modes = fused['fusion_mode'].fillna('NONE').values
    ts = fused['timestamp'].values

    # 颜色映射
    color_map = {'BOTH_MEASURED': 2, 'A_ONLY': 1, 'B_ONLY': 0, 'NONE': -1}
    for i in range(len(ts)-1):
        mode = modes[i]
        c = color_map.get(mode, -1)
        if c == 2: color = COLOR_FUSED
        elif c == 1: color = COLOR_A
        elif c == 0: color = COLOR_B
        else: color = 'lightgray'
        ax.axvspan(ts[i], ts[i+1], color=color, alpha=0.6, linewidth=0)

    # 加 legend
    patches = [mpatches.Patch(color=COLOR_FUSED, alpha=0.6, label='BOTH_MEASURED'),
              mpatches.Patch(color=COLOR_A, alpha=0.6, label='A_ONLY'),
              mpatches.Patch(color=COLOR_B, alpha=0.6, label='B_ONLY')]
    ax.legend(handles=patches, loc='upper right', fontsize=11)
    ax.set_xlabel('Time (s)', fontsize=12)
    ax.set_title(f'{title} — Fusion Mode Timeline', fontsize=13)
    ax.set_yticks([])
    ax.set_xlim(ts[0], ts[-1])
    ax.grid(True, alpha=0.3, axis='x')

    plt.tight_layout()
    out = FIG_DIR / case / '05_fusion_mode_timeline.png'
    plt.savefig(out, dpi=120, bbox_inches='tight')
    plt.close()
    return out


def fig_4case_comparison():
    """图 6: 4 case 的 RMSE 对比柱状图"""
    fig, axes = plt.subplots(2, 2, figsize=(16, 11))

    data = []
    for case in CASES:
        cd = REGRESSION_DIR / case
        fused = pd.read_csv(cd / 'fused_global_tracks.csv')
        # 找 T1 ref
        rf = cd / 'reference_track_T1.csv'
        if not rf.exists():
            continue
        ref = pd.read_csv(rf)
        ref_t = ref['timestamp'].values
        ref_x = ref['x_ref'].values
        ref_y = ref['y_ref'].values

        errs_f, errs_a, errs_b = [], [], []
        for _, r in fused.iterrows():
            if pd.isna(r['x_fused']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            errs_f.append((r['x_fused']-ref_x[idx])**2 + (r['y_fused']-ref_y[idx])**2)
        a = pd.read_csv(cd / 'sonar_A_tracks.csv')
        for _, r in a.iterrows():
            if pd.isna(r['x']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            errs_a.append((r['x']-ref_x[idx])**2 + (r['y']-ref_y[idx])**2)
        b = pd.read_csv(cd / 'sonar_B_tracks.csv')
        for _, r in b.iterrows():
            if pd.isna(r['x']): continue
            t = r['timestamp']
            idx = int(np.argmin(np.abs(ref_t - t)))
            if abs(ref_t[idx] - t) > 0.1: continue
            errs_b.append((r['x']-ref_x[idx])**2 + (r['y']-ref_y[idx])**2)

        data.append({
            'case': CASE_TITLES[case].split(':')[1].strip(),
            'rmse_fused': np.sqrt(np.mean(errs_f)) if errs_f else 0,
            'rmse_a': np.sqrt(np.mean(errs_a)) if errs_a else 0,
            'rmse_b': np.sqrt(np.mean(errs_b)) if errs_b else 0,
        })

    df = pd.DataFrame(data)
    cases_short = df['case'].tolist()
    x = np.arange(len(cases_short))
    w = 0.27

    # Subplot 1: RMSE
    ax = axes[0, 0]
    ax.bar(x - w, df['rmse_fused'], w, label='Fused', color=COLOR_FUSED, edgecolor='black')
    ax.bar(x, df['rmse_a'], w, label='Sonar A', color=COLOR_A, edgecolor='black')
    ax.bar(x + w, df['rmse_b'], w, label='Sonar B', color=COLOR_B, edgecolor='black')
    ax.set_xticks(x)
    ax.set_xticklabels(cases_short, fontsize=10)
    ax.set_ylabel('RMSE (m)', fontsize=11)
    ax.set_title('Position RMSE by Case', fontsize=12)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis='y')
    for i, v in enumerate(df['rmse_fused']):
        ax.text(i-w, v+0.3, f'{v:.2f}', ha='center', fontsize=9)

    # Subplot 2: Fusion Gain
    ax = axes[0, 1]
    df['best_single'] = df[['rmse_a', 'rmse_b']].min(axis=1)
    df['gain'] = 1 - df['rmse_fused'] / df['best_single']
    colors = [COLOR_FUSED if g > 0 else 'salmon' for g in df['gain']]
    ax.bar(x, df['gain'], color=colors, edgecolor='black')
    ax.axhline(0, color='black', linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(cases_short, fontsize=10)
    ax.set_ylabel('Fusion Gain', fontsize=11)
    ax.set_title('Fusion Gain (>0 = Fused beats best single)', fontsize=12)
    ax.grid(True, alpha=0.3, axis='y')
    for i, v in enumerate(df['gain']):
        ax.text(i, v + 0.005 if v >= 0 else v - 0.01, f'{v:+.3f}', ha='center', fontsize=10)

    # Subplot 3: Fallback & Outlier rates
    ax = axes[1, 0]
    x = np.arange(len(cases_short))
    w = 0.35
    fb = []
    o_rate = []
    for case in CASES:
        cd = REGRESSION_DIR / case
        f = pd.read_csv(cd / 'fused_global_tracks.csv')
        n_total = len(f)
        n_fb = f['fallback_reason'].notna().sum()
        fb.append(n_fb / n_total if n_total else 0)
        # Outlier rejection = outlier 原因 / total
        n_outlier = f['fallback_reason'].fillna('').str.contains('outlier').sum()
        o_rate.append(n_outlier / n_total if n_total else 0)
    ax.bar(x - w/2, fb, w, color=COLOR_PRED, edgecolor='black', label='Fallback rate')
    ax.bar(x + w/2, o_rate, w, color='salmon', edgecolor='black', label='Outlier rejection rate')
    ax.set_xticks(x)
    ax.set_xticklabels(cases_short, fontsize=10)
    ax.set_ylabel('Rate', fontsize=11)
    ax.set_title('Fallback & Outlier Rejection Rates', fontsize=12)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis='y')
    ax.set_ylim(0, 1.0)
    for i, (v1, v2) in enumerate(zip(fb, o_rate)):
        ax.text(i - w/2, v1+0.02, f'{v1*100:.1f}%', ha='center', fontsize=9)
        ax.text(i + w/2, v2+0.02, f'{v2*100:.1f}%', ha='center', fontsize=9)

    # Subplot 4: 协方差对比
    ax = axes[1, 1]
    a_only_p = []
    b_only_p = []
    fused_p = []
    for case in CASES:
        cd = REGRESSION_DIR / case
        a = pd.read_csv(cd / 'sonar_A_tracks.csv')
        b = pd.read_csv(cd / 'sonar_B_tracks.csv')
        fused = pd.read_csv(cd / 'fused_global_tracks.csv')
        a_only_p.append((a['Pxx']+a['Pyy']).mean())
        b_only_p.append((b['Pxx']+b['Pyy']).mean())
        fused_p.append(fused['pos_trace'].mean() if 'pos_trace' in fused.columns else 0)
    ax.bar(x - w, a_only_p, w, color=COLOR_A, edgecolor='black', label='A P trace')
    ax.bar(x, b_only_p, w, color=COLOR_B, edgecolor='black', label='B P trace')
    ax.bar(x + w, fused_p, w, color=COLOR_FUSED, edgecolor='black', label='Fused P trace')
    ax.set_xticks(x)
    ax.set_xticklabels(cases_short, fontsize=10)
    ax.set_ylabel('Mean Position P Trace (m²)', fontsize=11)
    ax.set_title('Covariance P Trace Comparison', fontsize=12)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis='y')
    for i, (v1, v2, v3) in enumerate(zip(a_only_p, b_only_p, fused_p)):
        ax.text(i - w, v1+0.05, f'{v1:.2f}', ha='center', fontsize=9)
        ax.text(i, v2+0.05, f'{v2:.2f}', ha='center', fontsize=9)
        ax.text(i + w, v3+0.05, f'{v3:.2f}', ha='center', fontsize=9)

    plt.suptitle('Case 5: Track Fusion Benchmark (4 cases)', fontsize=15)
    plt.tight_layout()
    out = FIG_DIR / '00_4case_overview.png'
    plt.savefig(out, dpi=130, bbox_inches='tight')
    plt.close()
    return out


def fig_4case_fused_tracks():
    """图 7: 4 case 融合航迹总览 (2x2 subplots)"""
    fig, axes = plt.subplots(2, 2, figsize=(18, 14))
    axes = axes.flatten()

    for ax, case in zip(axes, CASES):
        cd = REGRESSION_DIR / case
        ref_files = sorted(cd.glob('reference_track_T*.csv'))

        # 参考
        for rf in ref_files:
            ref = pd.read_csv(rf)
            color = ['black', 'magenta', 'cyan', 'brown'][ref_files.index(rf) % 4]
            ax.plot(ref['x_ref'], ref['y_ref'], color=color, linewidth=2,
                   linestyle='--', alpha=0.7, label=f"Ref {rf.stem.split('_')[-1]}")
            ax.scatter(ref['x_ref'].iloc[0], ref['y_ref'].iloc[0], color=color,
                      s=80, marker='*', zorder=5)

        # Fused
        fused = pd.read_csv(cd / 'fused_global_tracks.csv')
        for gid in fused['global_id'].unique():
            g = fused[fused['global_id']==gid].dropna(subset=['x_fused'])
            ax.plot(g['x_fused'], g['y_fused'], color=COLOR_FUSED, linewidth=2.2,
                   label=f'Fused {gid}', alpha=0.9)

        # Local A/B
        for sensor, color, prefix in [(COLOR_A, COLOR_A, 'A'), (COLOR_B, COLOR_B, 'B')]:
            tf = cd / f'sonar_{prefix}_tracks.csv'
            if tf.exists():
                tracks = pd.read_csv(tf)
                for tid in tracks['track_id'].unique():
                    tr = tracks[tracks['track_id']==tid]
                    ls = '-' if 'T1' in tid or 'T2' in tid else ':'
                    ax.plot(tr['x'], tr['y'], color=color, alpha=0.35, linewidth=0.9,
                           linestyle=ls)

        ax.set_title(CASE_TITLES[case], fontsize=13)
        ax.set_xlabel('X (m)', fontsize=10)
        ax.set_ylabel('Y (m)', fontsize=10)
        ax.legend(loc='best', fontsize=8)
        ax.grid(True, alpha=0.3)
        ax.set_aspect('equal', adjustable='box')

    plt.suptitle('Case 5: All 4 Cases — Fused Tracks Overview', fontsize=16)
    plt.tight_layout()
    out = FIG_DIR / '01_4case_tracks_overview.png'
    plt.savefig(out, dpi=130, bbox_inches='tight')
    plt.close()
    return out


def main():
    print('='*60)
    print('Track Fusion Visualization')
    print('='*60)
    outputs = []
    for case in CASES:
        cd = REGRESSION_DIR / case
        if not (cd / 'fused_global_tracks.csv').exists():
            print(f'  [SKIP] {case}: no fused_global_tracks.csv')
            continue
        title = CASE_TITLES[case]
        print(f'\n{case}:')
        out = fig_geometry_with_fusion(cd, case, title)
        print(f'  [1/5] {out.name}')
        outputs.append(out)
        out = fig_fused_vs_reference_error(cd, case, title)
        print(f'  [2/5] {out.name}')
        outputs.append(out)
        out = fig_fusion_mode_pie(cd, case, title)
        print(f'  [3/5] {out.name}')
        outputs.append(out)
        out = fig_covariance_evolution(cd, case, title)
        print(f'  [4/5] {out.name}')
        outputs.append(out)
        out = fig_fusion_mode_timeline(cd, case, title)
        print(f'  [5/5] {out.name}')
        outputs.append(out)

    print('\n4-case comparison:')
    out = fig_4case_comparison()
    print(f'  [1/2] {out.name}')
    outputs.append(out)
    out = fig_4case_fused_tracks()
    print(f'  [2/2] {out.name}')
    outputs.append(out)

    print('\n' + '='*60)
    print(f'Total: {len(outputs)} figures')
    print(f'Output dir: {FIG_DIR}')
    print('='*60)


if __name__ == '__main__':
    main()
