#!/usr/bin/env python3
"""主目标提取可视化：主目标 vs 被滤碎片 + 测量距离（米）."""
import json, os, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from association.associate import _corrected, load_candidates

def xy(t):
    pts = _corrected(t, apply=True)
    return [p[1] for p in pts], [p[2] for p in pts]

for assoc_dir in ["output/association/mtdsp_yantai_optical",
                  "output/association/mtdsp2_yantai_optical"]:
    prim = json.load(open(os.path.join(assoc_dir, "primary_targets.json"), encoding="utf-8"))
    summ = json.load(open(os.path.join(assoc_dir, "association_summary.json"), encoding="utf-8"))
    cands = load_candidates(summ["alignment_dir"], min_points=3, use_reconstructed=True)
    by_id = {t.track_id: t for s in cands.values() for t in s}
    out_dir = os.path.join(assoc_dir, "plots")
    scen = prim["scenario"]
    main_set = set(prim["main_radar_full"]) | set(prim["main_fragments"])
    filt_set = set(prim["filtered_fragments"])
    # 1) ENU 总览：主目标加粗 + 碎片淡灰
    fig, ax = plt.subplots(figsize=(10, 8))
    for tid in sorted(filt_set):
        t = by_id.get(tid)
        if t is None: continue
        x, y = xy(t)
        ax.plot(x, y, color="gray", alpha=0.25, linewidth=0.8)
    # 主雷达碎片红、整轨深红、AIS蓝、光电绿
    for tid in prim["main_radar_full"]:
        t = by_id.get(tid)
        if t:
            x, y = xy(t)
            ax.plot(x, y, color="darkred", linewidth=2.5, label=f"{tid} MAIN")
    for tid in prim["main_fragments"]:
        t = by_id.get(tid)
        if t:
            x, y = xy(t)
            ax.plot(x, y, color="red", linewidth=1.8, linestyle="--", label=f"{tid}")
    for p in summ["associations"]["radar_ais"]["accepted"] + summ["associations"]["optical_ais"]["accepted"]:
        for tid, c in ((p["track_b"], "blue"), (p["track_a"], "green")):
            if "ais:" in tid or "optical:" in tid:
                t = by_id.get(tid)
                if t and tid not in [l.get_label() for l in ax.lines]:
                    x, y = xy(t)
                    ax.plot(x, y, color=c, linewidth=2.0, alpha=0.9, label=f"{tid} ({p['median_m']}m)")
    ax.set_xlabel("East (m, bias-corrected)")
    ax.set_ylabel("North (m, bias-corrected)")
    ax.set_title(f"{scen} PRIMARY 413203610 (red) vs FILTERED {prim['n_filtered']} frags (gray)")
    ax.axis("equal")
    ax.legend(fontsize=6, loc="best", ncol=2)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "primary_enu_overview.png"), dpi=150)
    plt.close(fig)
    # 2) 距离柱状图：主 vs 被滤
    d = prim["distances_m"]
    labels, vals, cols = [], [], []
    for row in d["radar_ais"]:
        labels.append(f"R-A\n{row[0].split(':')[-1][:8]}"); vals.append(row[2]); cols.append("tab:red")
    for row in d["optical_ais"]:
        labels.append(f"O-A\n{row[0].split(':')[-1][:10]}"); vals.append(row[2]); cols.append("tab:green")
    for row in d["radar_optical_main"]:
        labels.append(f"R-O\n{row[0].split(':')[-1][:8]}"); vals.append(row[2]); cols.append("tab:blue")
    for row in d["radar_optical_filtered"]:
        labels.append(f"FILT\n{row[0].split(':')[-1][:8]}"); vals.append(row[2]); cols.append("lightgray")
    fig, ax = plt.subplots(figsize=(max(10, len(vals)*0.7), 5))
    bars = ax.bar(range(len(vals)), vals, color=cols)
    ax.axhline(300, color="k", linestyle=":", label="filter gate 300m")
    for i, v in enumerate(vals):
        ax.text(i, v + 8, f"{v:.0f}m", ha="center", fontsize=7)
    ax.set_xticks(range(len(vals))); ax.set_xticklabels(labels, fontsize=7)
    ax.set_ylabel("median distance (m)")
    ax.set_title(f"{scen} measured distances: MAIN (color) vs FILTERED (gray)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "primary_distances.png"), dpi=150)
    plt.close(fig)
    print("wrote", assoc_dir, "primary_enu_overview.png + primary_distances.png")
