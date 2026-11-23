#!/usr/bin/env python3
"""主目标图 v2：解释回折 -> 按连续片段绘制；只保留413相关；R154单独；图例去重."""
import json, os, sys, csv, math
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from association.associate import _corrected, _parse_time, _to_float, load_candidates

def xy(t):
    pts = _corrected(t, apply=True)
    return pts  # (epoch, e, n)

def split_segments(pts, max_gap_s=60.0, max_jump_m=150.0):
    """按时间间隙 / 空间跳变切分为连续片段，跨段不连线."""
    segs, cur = [], []
    prev = None
    for p in pts:
        if prev is not None:
            dt = p[0] - prev[0]
            dd = math.hypot(p[1]-prev[1], p[2]-prev[2])
            if dt > max_gap_s or dd > max_jump_m:
                if cur: segs.append(cur)
                cur = []
        cur.append(p)
        prev = p
    if cur: segs.append(cur)
    return segs

def plot_track_segments(ax, pts, color, lw=2.0, ls="-", alpha=0.95, marker_size=18):
    segs = split_segments(pts)
    for s in segs:
        x = [p[1] for p in s]; y = [p[2] for p in s]
        if len(s) >= 2:
            ax.plot(x, y, color=color, lw=lw, ls=ls, alpha=alpha)
        else:
            ax.scatter(x, y, color=color, s=marker_size, marker="x", alpha=alpha)
    # 起止标记只标首段首点/末段末点
    if segs:
        ax.scatter([segs[0][0][1]], [segs[0][0][2]], color=color, s=28, marker="o", zorder=5)
        ax.scatter([segs[-1][-1][1]], [segs[-1][-1][2]], color=color, s=28, marker="X", zorder=5)
    return segs

def radar_pure_points(alignment_dir):
    """只取 radar:413203610 的纯测量点（排除 ais_associated 快照，避免混轨回折）."""
    path = os.path.join(alignment_dir, "tracks/radar/413203610.csv")
    pts = []
    with open(path, encoding="utf-8", newline="") as h:
        for r in csv.DictReader(h):
            if r.get("radar_measurement_kind") != "radar_detected":
                continue
            t = _parse_time(r.get("time_utc") or "")
            e = _to_float(r.get("east_m")); n = _to_float(r.get("north_m"))
            if t is None or e is None or n is None:
                continue
            pts.append((t.timestamp(), e, n))
    pts.sort()
    # 偏差修正（与关联评分一致：雷达 -2.4° 绕站心旋转）
    from association.associate import _rotated_points, RADAR_BIAS_DEG
    return _rotated_points(pts, RADAR_BIAS_DEG)

for assoc_dir in ["output/association/mtdsp_yantai_optical",
                  "output/association/mtdsp2_yantai_optical"]:
    prim = json.load(open(os.path.join(assoc_dir, "primary_targets.json"), encoding="utf-8"))
    summ = json.load(open(os.path.join(assoc_dir, "association_summary.json"), encoding="utf-8"))
    cands = load_candidates(summ["alignment_dir"], min_points=3, use_reconstructed=True)
    by_id = {t.track_id: t for s in cands.values() for t in s}
    out_dir = os.path.join(assoc_dir, "plots")
    scen = prim["scenario"]
    # ---- 主图：只保留 413203610 相关 ----
    fig, ax = plt.subplots(figsize=(10, 8))
    # AIS 413
    t = by_id.get("ais:413203610")
    if t: plot_track_segments(ax, xy(t), "tab:blue", lw=2.4)
    # 光电主目标 Inf/TV（位置几乎重合，实线+虚线区分）
    t = by_id.get("optical:413203610@Inf")
    if t: plot_track_segments(ax, xy(t), "tab:green", lw=2.0, ls="-")
    t = by_id.get("optical:413203610@TV")
    if t: plot_track_segments(ax, xy(t), "tab:olive", lw=1.6, ls="--")
    # 雷达整轨纯测量（分段，不连线跨间隙/跳变）
    pure = radar_pure_points(summ["alignment_dir"])
    segs = plot_track_segments(ax, pure, "darkred", lw=2.2)
    # 重建主碎片（03-05: R13/R16/R21；03-12: 413组，不含R154）
    main_frags = [f for f in prim["main_fragments"] if f != "radar_reconstructed:R154"]
    for tid in sorted(main_frags):
        tt = by_id.get(tid)
        if tt: plot_track_segments(ax, xy(tt), "red", lw=1.4, ls=":")
    ax.set_xlabel("East (m, bias-corrected)")
    ax.set_ylabel("North (m, bias-corrected)")
    ax.set_title(f"{scen} MAIN 413203610: radar-pure segments + AIS + optical (folds removed)")
    ax.axis("equal")
    # 去重图例：每类只一项
    handles = [
        Line2D([0],[0], color="darkred", lw=2.2, label=f"radar:413203610 pure ({len(pure)}pts, {len(segs)}segs)"),
        Line2D([0],[0], color="tab:blue", lw=2.4, label="ais:413203610"),
        Line2D([0],[0], color="tab:green", lw=2.0, label="optical:413203610@Inf"),
        Line2D([0],[0], color="tab:olive", lw=1.6, ls="--", label="optical:413203610@TV"),
        Line2D([0],[0], color="red", lw=1.4, ls=":", label=f"recon frags x{len(main_frags)} (R-group)"),
    ]
    ax.legend(handles=handles, fontsize=8, loc="best")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "primary_main_413_segments.png"), dpi=150)
    plt.close(fig)
    print("wrote", assoc_dir, "primary_main_413_segments.png",
          f"| radar-pure {len(pure)}pts -> {len(segs)} segs")
    # ---- R154 单独展示（仅03-12）----
    if scen == "03-12":
        fig, ax = plt.subplots(figsize=(9, 7))
        t = by_id.get("ais:412332820")
        if t: plot_track_segments(ax, xy(t), "tab:blue", lw=2.4)
        t = by_id.get("radar_reconstructed:R154")
        if t: plot_track_segments(ax, xy(t), "darkred", lw=2.2)
        ax.set_xlabel("East (m, bias-corrected)")
        ax.set_ylabel("North (m, bias-corrected)")
        ax.set_title("03-12 SECONDARY R154 <-> ais:412332820 (med 32.4m / mean 33.1m / ov 5)")
        ax.axis("equal")
        handles = [
            Line2D([0],[0], color="darkred", lw=2.2, label="radar_reconstructed:R154 (26pts)"),
            Line2D([0],[0], color="tab:blue", lw=2.4, label="ais:412332820 (30pts)"),
        ]
        ax.legend(handles=handles, fontsize=9, loc="best")
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "primary_R154_secondary.png"), dpi=150)
        plt.close(fig)
        print("wrote", assoc_dir, "primary_R154_secondary.png")
