#!/usr/bin/env python3
"""航迹关联可视化：ENU 总览 + 配对残差细节。

产物 (output/association/<场景>/plots/)：
  - enu_overview.png：三源候选轨迹总览（已关联加粗，未关联淡色）
  - pair_residuals.png：每个 accepted 对的重叠段距离随时间变化
  - score_matrix.png：候选对中位距离矩阵（行=radar/optical, 列=AIS）

用法：
    python3 run_visualize_association.py --association-dir output/association/mtdsp_yantai_optical
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from association.associate import (  # noqa: E402
    _corrected,
    _interp_pos,
    load_candidates,
)


def _read_csv(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, encoding="utf-8", newline="") as h:
        return list(csv.DictReader(h))


def _track_xy(track, corrected=True):
    pts = _corrected(track, apply=corrected) if corrected else track.points
    return [p[1] for p in pts], [p[2] for p in pts]


def plot_overview(cands, accepted_pairs, out_path, title):
    fig, ax = plt.subplots(figsize=(10, 8))
    # 未关联淡色打底
    paired_ids = set()
    for p in accepted_pairs:
        paired_ids.add(p["track_a"])
        paired_ids.add(p["track_b"])
    for sensor, color in [("ais", "tab:blue"), ("radar", "tab:red"), ("optical", "tab:green")]:
        for t in cands.get(sensor, []):
            x, y = _track_xy(t, corrected=True)
            if t.track_id in paired_ids:
                continue
            ax.plot(x, y, color=color, alpha=0.25, linewidth=1.0)
    # 已关联加粗 + 标注中位距离
    cmap = plt.cm.tab10
    for k, p in enumerate(accepted_pairs):
        c = cmap(k % 10)
        for tid in (p["track_a"], p["track_b"]):
            tr = next((t for s in cands.values() for t in s if t.track_id == tid), None)
            if tr is None:
                continue
            x, y = _track_xy(tr, corrected=True)
            ax.plot(x, y, color=c, alpha=0.95, linewidth=2.0, label=f"{tid} ({p['median_m']}m)")
            ax.scatter([x[0]], [y[0]], color=c, s=30, marker="o")
            ax.scatter([x[-1]], [y[-1]], color=c, s=30, marker="x")
    ax.set_xlabel("East (m, bias-corrected)")
    ax.set_ylabel("North (m, bias-corrected)")
    ax.set_title(title + " (corrected ENU, o=start x=end)")
    ax.axis("equal")
    ax.legend(fontsize=7, loc="best")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def pair_residual_series(ta, tb):
    """返回 (times, distances)：以点数少的一方为基准插值另一方（修正后）。"""
    pa = _corrected(ta, apply=True)
    pb = _corrected(tb, apply=True)
    if len(pa) <= len(pb):
        base, other = pa, pb
    else:
        base, other = pb, pa
    ts, ds = [], []
    for epoch, e, n in base:
        pos = _interp_pos(other, epoch)
        if pos is None:
            continue
        ts.append(epoch)
        ds.append(math.hypot(pos[0] - e, pos[1] - n))
    return ts, ds


def plot_residuals(cands, accepted_pairs, out_path, title):
    by_id = {t.track_id: t for s in cands.values() for t in s}
    n = len(accepted_pairs)
    if n == 0:
        fig, ax = plt.subplots(figsize=(8, 2))
        ax.text(0.5, 0.5, "no accepted pairs", ha="center")
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return
    fig, axes = plt.subplots(n, 1, figsize=(10, 2.6 * n), sharex=False)
    if n == 1:
        axes = [axes]
    t0 = None
    for ax, p in zip(axes, accepted_pairs):
        ta, tb = by_id.get(p["track_a"]), by_id.get(p["track_b"])
        if ta is None or tb is None:
            continue
        ts, ds = pair_residual_series(ta, tb)
        if t0 is None and ts:
            t0 = ts[0]
        rel = [t - (t0 or 0) for t in ts]
        ax.plot(rel, ds, marker=".", linewidth=1.0)
        ax.axhline(p["median_m"], color="red", linestyle="--",
                   label=f"median {p['median_m']}m (ov={p['overlap']})")
        ax.set_ylabel("dist (m)")
        ax.set_title(f"{p['track_a']} <-> {p['track_b']}")
        ax.legend(fontsize=8)
    axes[-1].set_xlabel("seconds since first pair point")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_score_matrix(cands, out_path, title, sensor_a="radar", sensor_b="ais"):
    from association.associate import pair_score

    ta_list = cands.get(sensor_a, [])
    tb_list = cands.get(sensor_b, [])
    if not ta_list or not tb_list:
        return
    mat = np.full((len(ta_list), len(tb_list)), np.nan)
    for i, a in enumerate(ta_list):
        for j, b in enumerate(tb_list):
            s = pair_score(a, b, apply_bias_correction=True)
            if s.get("ok"):
                mat[i, j] = s["median_m"]
    fig, ax = plt.subplots(figsize=(max(6, len(tb_list) * 1.6), max(4, len(ta_list) * 0.55)))
    im = ax.imshow(mat, aspect="auto", cmap="viridis_r", vmin=0, vmax=600)
    ax.set_xticks(range(len(tb_list)))
    ax.set_xticklabels([t.track_id for t in tb_list], rotation=30, ha="right", fontsize=7)
    ax.set_yticks(range(len(ta_list)))
    ax.set_yticklabels([t.track_id for t in ta_list], fontsize=7)
    for i in range(len(ta_list)):
        for j in range(len(tb_list)):
            if not np.isnan(mat[i, j]):
                ax.text(j, i, f"{mat[i, j]:.0f}", ha="center", va="center",
                        fontsize=6, color="white" if mat[i, j] < 300 else "black")
    fig.colorbar(im, ax=ax, label="median dist (m, corrected)")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--association-dir", required=True)
    ap.add_argument("--alignment-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    args = ap.parse_args()

    assoc_dir = args.association_dir
    with open(os.path.join(assoc_dir, "association_summary.json"), encoding="utf-8") as h:
        summary = __import__("json").load(h)
    align_dir = args.alignment_dir or summary["alignment_dir"]
    out_dir = args.output_dir or os.path.join(assoc_dir, "plots")
    os.makedirs(out_dir, exist_ok=True)

    cands = load_candidates(align_dir, min_points=3, use_reconstructed=True)
    scenario = os.path.basename(os.path.normpath(assoc_dir))

    # 三组 accepted 汇总到总览；细节图按组分开
    for label in ["radar_ais", "optical_ais", "radar_optical"]:
        rows = _read_csv(os.path.join(assoc_dir, f"{label}_accepted.csv"))
        # csv 读出全是 str，转回数值
        for r in rows:
            for k in ("median_m", "mean_m", "min_m", "max_m"):
                r[k] = float(r[k]) if r.get(k) not in (None, "") else None
            r["overlap"] = int(r["overlap"]) if r.get("overlap") not in (None, "") else 0
        plot_overview(cands, rows, os.path.join(out_dir, f"enu_overview_{label}.png"),
                      f"{scenario} {label}")
        plot_residuals(cands, rows, os.path.join(out_dir, f"pair_residuals_{label}.png"),
                       f"{scenario} {label}")
    plot_score_matrix(cands, os.path.join(out_dir, "score_matrix_radar_ais.png"),
                      f"{scenario} radar-ais median (m)", "radar", "ais")
    plot_score_matrix(cands, os.path.join(out_dir, "score_matrix_optical_ais.png"),
                      f"{scenario} optical-ais median (m)", "optical", "ais")
    print("wrote plots to", os.path.relpath(out_dir, ROOT))
    print(sorted(os.listdir(out_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
