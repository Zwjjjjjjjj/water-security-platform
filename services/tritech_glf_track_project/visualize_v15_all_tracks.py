"""
Visualize ALL v15 tracks (PRIMARY_DIVER + CANDIDATE_*) for the four DFKI sets.

For each set we produce:
  1. set_<i>_all_tracks_v15.png  — single panel:
        - GT path (gray, with time-coloured scatter)
        - each candidate track as its own coloured polyline
        - status overlay (CONFIRMED / TENTATIVE / TRACK_LOST / etc.)
  2. set_<i>_tracks_split_v15.png — 2×2 panel:
        - top-left  : GT-only
        - top-right : PRIMARY_DIVER vs GT
        - bot-left  : all CANDIDATE_* overlaid
        - bot-right: per-frame error bars per track
  3. all_sets_tracks_overview.png — 2×2 panel showing all four sets side by side

Output: /root/dfki_output_v15/visualizations/
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V15, DFKI_ROOT
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Circle

ROOT = Path(str(DFKI_OUTPUT_V15))
GTROOT = Path(str(DFKI_ROOT))
OUT = ROOT / "visualizations"
OUT.mkdir(parents=True, exist_ok=True)

SETS = [1, 2, 3, 4]

# Consistent palette for the four sets
SET_COLORS = {
    1: "#3b82f6",   # blue
    2: "#10b981",   # green
    3: "#f59e0b",   # amber
    4: "#ef4444",   # red
}

# Status → colour
STATUS_STYLE = {
    "CONFIRMED":       {"c": "#3b82f6", "m": "o", "label": "CONFIRMED"},
    "TENTATIVE":       {"c": "#f59e0b", "m": "*", "label": "TENTATIVE"},
    "DETECTOR_ORACLE": {"c": "#10b981", "m": "s", "label": "DETECTOR_ORACLE"},
    "LOST_RECOVERED":  {"c": "#22d3ee", "m": "D", "label": "LOST_RECOVERED"},
    "TRACK_LOST":      {"c": "#ef4444", "m": "x", "label": "TRACK_LOST"},
    "MISS":            {"c": "#9ca3af", "m": "P", "label": "MISS"},
}


# ────────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────────
def load_set(s: int):
    base = ROOT / f"dfki_set_{s}"
    gt = pd.read_csv(GTROOT / f"set_{s}/tracking_sonar_coordinates_{s}.csv")
    gt["ts"] = gt["image_name"].str.replace(".jpg", "", regex=False).astype(float)
    gt = gt.sort_values("ts").reset_index(drop=True)

    ev = pd.read_csv(base / "gt_evaluation.csv")
    summ = json.load(open(base / "gt_evaluation_summary.json"))
    tracks = pd.read_csv(base / "tracks_raw.csv")
    return {"gt": gt, "eval": ev, "summary": summ, "tracks": tracks}


def polar_background(ax, x_range, y_range):
    ax.set_facecolor("#f8f9fa")
    cx = (x_range[0] + x_range[1]) / 2
    cy = (y_range[0] + y_range[1]) / 2
    for r in [100, 200, 300, 400]:
        ax.add_patch(Circle((cx, cy), r, fill=False, color="gray",
                            alpha=0.18, linestyle=":", linewidth=0.8))
    ax.axhline(cy, color="gray", alpha=0.15, lw=0.5)
    ax.axvline(cx, color="gray", alpha=0.15, lw=0.5)


def gt_path(ax, gt, cmap="plasma"):
    """Draw GT path with time-coloured scatter."""
    gt = gt.copy()
    ts = gt["ts"].values
    tn = (ts - ts.min()) / max(ts.max() - ts.min(), 1e-9)
    ax.plot(gt["x"], gt["y"], "-", color="gray", linewidth=1.0, alpha=0.45, zorder=2)
    sc = ax.scatter(gt["x"], gt["y"], c=tn, cmap=cmap, s=22,
                    alpha=0.85, edgecolors="white", linewidths=0.3, zorder=3,
                    vmin=0, vmax=1)
    # start = green, end = red
    ax.scatter([gt["x"].iloc[0]], [gt["y"].iloc[0]], c="lime", s=180,
               marker="o", edgecolors="black", linewidths=1.5, zorder=6)
    ax.scatter([gt["x"].iloc[-1]], [gt["y"].iloc[-1]], c="red", s=180,
               marker="X", edgecolors="black", linewidths=1.5, zorder=6)
    ax.annotate("START", (gt["x"].iloc[0], gt["y"].iloc[0]),
                textcoords="offset points", xytext=(7, 5), fontsize=8,
                fontweight="bold", color="green")
    ax.annotate("END", (gt["x"].iloc[-1], gt["y"].iloc[-1]),
                textcoords="offset points", xytext=(7, -5), fontsize=8,
                fontweight="bold", color="red")
    return sc


def add_colorbar(scatter, ax, label):
    cbar = plt.colorbar(scatter, ax=ax, fraction=0.04, pad=0.02, shrink=0.75)
    cbar.set_label(label, fontsize=8)


def draw_tracks(ax, data, primary_only=False, candidates=None, lw=1.6):
    """Draw all tracks on `ax`. Returns dict of per-track handles for legend."""
    ev = data["eval"]
    handles = {}

    track_ids = sorted(ev["track_id"].unique(),
                     key=lambda t: 0 if t == "PRIMARY_DIVER" else 1)
    if primary_only:
        track_ids = [t for t in track_ids if t == "PRIMARY_DIVER"]
    if candidates is not None:
        track_ids = [t for t in track_ids if t in candidates]

    for t in track_ids:
        sub = ev[ev["track_id"] == t].sort_values("frame_id")
        if sub.empty:
            continue
        is_primary = (t == "PRIMARY_DIVER")
        # draw line: solid for primary, dashed for candidates
        ls = "-" if not is_primary else "-"
        alpha_line = 0.95 if is_primary else 0.85
        ax.plot(sub["track_x"], sub["track_y"],
                color=SET_COLORS[hash(t) % len(SET_COLORS)] if False else None,
                linestyle=ls, linewidth=lw + (0.8 if is_primary else 0),
                alpha=alpha_line, zorder=5)
        # overlay status markers
        for status, style in STATUS_STYLE.items():
            ss = sub[sub["status"] == status]
            if ss.empty:
                continue
            ax.scatter(ss["track_x"], ss["track_y"],
                       c=style["c"], s=42 if is_primary else 28,
                       marker=style["m"], alpha=0.9,
                       edgecolors="black", linewidths=0.3, zorder=6)
        handles[t] = (is_primary, len(sub))
    return handles


def legend_for_tracks(ax, handles):
    """Build a legend showing each track + status colours."""
    elems = []
    for t, (is_primary, n) in handles.items():
        elems.append(Line2D([0], [0],
                            color="black" if is_primary else "dimgray",
                            linewidth=2.2 if is_primary else 1.4,
                            linestyle="-" if is_primary else "--",
                            label=f"{t} (n={n})"))
    for status, style in STATUS_STYLE.items():
        elems.append(Line2D([0], [0], marker=style["m"], color="w",
                            markerfacecolor=style["c"], markeredgecolor="black",
                            markersize=8, label=style["label"]))
    elems.append(Line2D([0], [0], color="gray", linewidth=1.5,
                        alpha=0.6, label="GT path"))
    ax.legend(handles=elems, loc="upper right", fontsize=7.5,
              ncol=2, framealpha=0.92)


# ────────────────────────────────────────────────────────────────────────────
# 1) SINGLE PANEL: every track + GT, per-set
# ────────────────────────────────────────────────────────────────────────────
def plot_single_per_set(data, s, xlim=None, ylim=None):
    gt = data["gt"]
    ev = data["eval"]
    summ = data["summary"]

    if xlim is None:
        xlim = (gt["x"].min() - 30, gt["x"].max() + 30)
    if ylim is None:
        ylim = (gt["y"].min() - 30, gt["y"].max() + 30)

    fig, ax = plt.subplots(figsize=(10, 9))
    polar_background(ax, xlim, ylim)
    sc = gt_path(ax, gt)

    handles = draw_tracks(ax, data)

    add_colorbar(sc, ax, "Time progression")

    # summary box
    status_counts = ev["status"].value_counts().to_dict()
    track_counts = ev["track_id"].value_counts().to_dict()
    info = (
        f"GT frames : {summ['n_gt_frames']}\n"
        f"Recall     : {summ['detection_recall']*100:.1f}%\n"
        f"Mean err   : {summ['mean_error_px']:.2f} px\n"
        f"Median err : {summ['median_error_px']:.2f} px\n"
        f"P90 / Max  : {summ['p90_error_px']:.1f} / {summ['max_error_px']:.1f} px\n"
        f"Tracks     : {len(track_counts)}\n"
        f"Status     : " +
        " | ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
    )
    ax.text(0.015, 0.985, info, transform=ax.transAxes,
            va="top", ha="left", fontsize=8.5, family="monospace",
            bbox=dict(boxstyle="round,pad=0.45", facecolor="white",
                      edgecolor="gray", alpha=0.92))

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.invert_yaxis()
    ax.set_aspect("equal")
    ax.set_xlabel("xpix (right)")
    ax.set_ylabel("ypix (forward / up)")
    ax.set_title(f"SET {s} — All v15 tracks vs GT  ({len(track_counts)} tracks)",
                 fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3)
    legend_for_tracks(ax, handles)

    out = OUT / f"set_{s}_all_tracks_v15.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out}")


# ────────────────────────────────────────────────────────────────────────────
# 2) 2×2 PANEL: GT only | primary | candidates | error timeline
# ────────────────────────────────────────────────────────────────────────────
def plot_split_per_set(data, s, xlim=None, ylim=None):
    gt = data["gt"]
    ev = data["eval"]
    summ = data["summary"]

    if xlim is None:
        xlim = (gt["x"].min() - 30, gt["x"].max() + 30)
    if ylim is None:
        ylim = (gt["y"].min() - 30, gt["y"].max() + 30)

    fig, axes = plt.subplots(2, 2, figsize=(16, 13))

    # ── top-left : GT path only (time coloured) ──────────────────────────────
    ax = axes[0, 0]
    polar_background(ax, xlim, ylim)
    sc = gt_path(ax, gt)
    add_colorbar(sc, ax, "Time")
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.invert_yaxis(); ax.set_aspect("equal")
    ax.set_title(f"set_{s} — GT 真实航迹 ({len(gt)} frames)", fontsize=11, fontweight="bold")
    ax.set_xlabel("xpix"); ax.set_ylabel("ypix")
    ax.grid(True, alpha=0.3)

    # ── top-right : PRIMARY_DIVER vs GT ──────────────────────────────────────
    ax = axes[0, 1]
    polar_background(ax, xlim, ylim)
    gt_path(ax, gt)
    draw_tracks(ax, data, primary_only=True, lw=2.4)
    # annotate GT-diver error
    prim = ev[ev["track_id"] == "PRIMARY_DIVER"].sort_values("frame_id")
    if not prim.empty:
        # draw error vectors
        for _, r in prim.iterrows():
            if pd.notna(r["error_px"]):
                ax.plot([r["gt_x"], r["track_x"]],
                        [r["gt_y"], r["track_y"]],
                        color="red", alpha=0.10, linewidth=0.6, zorder=4)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.invert_yaxis(); ax.set_aspect("equal")
    ax.set_title(f"PRIMARY_DIVER  hits={len(prim)}  mean={summ['mean_error_px']:.2f}px",
                 fontsize=11, fontweight="bold", color="#b91c1c")
    ax.set_xlabel("xpix"); ax.set_ylabel("ypix")
    ax.grid(True, alpha=0.3)

    # legend
    elems = []
    for status, style in STATUS_STYLE.items():
        elems.append(Line2D([0], [0], marker=style["m"], color="w",
                            markerfacecolor=style["c"], markeredgecolor="black",
                            markersize=8, label=style["label"]))
    elems.append(Line2D([0], [0], color="black", linewidth=2.5,
                        label=f"PRIMARY_DIVER (n={len(prim)})"))
    elems.append(Line2D([0], [0], color="gray", linewidth=1, alpha=0.6, label="GT"))
    ax.legend(handles=elems, loc="upper right", fontsize=8, ncol=2)

    # ── bottom-left : CANDIDATE_* tracks ─────────────────────────────────────
    ax = axes[1, 0]
    polar_background(ax, xlim, ylim)
    gt_path(ax, gt)
    cand_ids = [t for t in ev["track_id"].unique() if t != "PRIMARY_DIVER"]
    cand_palette = ["#a855f7", "#ec4899", "#14b8a6", "#f97316", "#84cc16",
                    "#6366f1", "#06b6d4", "#eab308"]
    for idx, t in enumerate(sorted(cand_ids)):
        sub = ev[ev["track_id"] == t].sort_values("frame_id")
        c = cand_palette[idx % len(cand_palette)]
        ax.plot(sub["track_x"], sub["track_y"], "-", color=c,
                linewidth=1.8, alpha=0.85, label=f"{t} (n={len(sub)})", zorder=5)
        for status, style in STATUS_STYLE.items():
            ss = sub[sub["status"] == status]
            if not ss.empty:
                ax.scatter(ss["track_x"], ss["track_y"], c=style["c"], s=30,
                           marker=style["m"], edgecolors="black", linewidths=0.3,
                           alpha=0.95, zorder=6)
    ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.invert_yaxis(); ax.set_aspect("equal")
    ax.set_title(f"CANDIDATE 轨迹 ({len(cand_ids)} tracks)", fontsize=11, fontweight="bold")
    ax.set_xlabel("xpix"); ax.set_ylabel("ypix")
    ax.grid(True, alpha=0.3)
    elems2 = [Line2D([0], [0], color=cand_palette[i % len(cand_palette)],
                     linewidth=1.8, label=sorted(cand_ids)[i])
              for i in range(len(cand_ids))]
    for status, style in STATUS_STYLE.items():
        elems2.append(Line2D([0], [0], marker=style["m"], color="w",
                             markerfacecolor=style["c"], markeredgecolor="black",
                             markersize=7, label=style["label"]))
    ax.legend(handles=elems2, loc="upper right", fontsize=7.5, ncol=2)

    # ── bottom-right : per-frame error bars per track ───────────────────────
    ax = axes[1, 1]
    palette = ["#b91c1c", "#a855f7", "#ec4899", "#14b8a6", "#f97316",
               "#84cc16", "#6366f1", "#06b6d4", "#eab308"]
    all_ids = sorted(ev["track_id"].unique(),
                     key=lambda t: 0 if t == "PRIMARY_DIVER" else 1)
    for i, t in enumerate(all_ids):
        sub = ev[ev["track_id"] == t].sort_values("frame_id")
        c = "#b91c1c" if t == "PRIMARY_DIVER" else palette[i % len(palette)]
        valid = sub[sub["error_px"].notna()]
        ax.plot(valid["frame_id"], valid["error_px"], "-o",
                color=c, linewidth=1.4, markersize=3, alpha=0.85, label=t)
    ax.axhline(20, color="green", linestyle="--", alpha=0.4, label="20px")
    ax.axhline(50, color="orange", linestyle="--", alpha=0.4, label="50px")
    ax.set_xlabel("Frame ID")
    ax.set_ylabel("Tracking error (px)")
    ax.set_title("Per-frame error — each track", fontsize=11, fontweight="bold")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8, ncol=2)

    fig.suptitle(f"SET {s} — v15 轨迹全貌 (GT | PRIMARY | CANDIDATES | error timeline)",
                 fontsize=14, fontweight="bold", y=1.005)
    out = OUT / f"set_{s}_tracks_split_v15.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out}")


# ────────────────────────────────────────────────────────────────────────────
# 3) OVERVIEW: 2×2 with all four sets
# ────────────────────────────────────────────────────────────────────────────
def plot_overview(sets_data):
    fig, axes = plt.subplots(2, 2, figsize=(18, 16))
    axes = axes.ravel()

    for idx, s in enumerate(SETS):
        data = sets_data[s]
        ax = axes[idx]
        gt = data["gt"]
        xlim = (gt["x"].min() - 30, gt["x"].max() + 30)
        ylim = (gt["y"].min() - 30, gt["y"].max() + 30)

        polar_background(ax, xlim, ylim)
        gt_path(ax, gt)
        handles = draw_tracks(ax, data)

        # per-set summary
        sm = data["summary"]
        ev = data["eval"]
        track_count = ev["track_id"].nunique()
        info = (f"Mean err : {sm['mean_error_px']:.2f} px\n"
                f"Median   : {sm['median_error_px']:.2f} px\n"
                f"P90 / Max: {sm['p90_error_px']:.1f} / {sm['max_error_px']:.1f} px\n"
                f"Recall   : {sm['detection_recall']*100:.1f}%\n"
                f"Tracks   : {track_count}")
        ax.text(0.015, 0.985, info, transform=ax.transAxes,
                va="top", ha="left", fontsize=8, family="monospace",
                bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                          edgecolor="gray", alpha=0.92))

        ax.set_xlim(*xlim); ax.set_ylim(*ylim); ax.invert_yaxis(); ax.set_aspect("equal")
        ax.set_title(f"SET {s} — all tracks", fontsize=12, fontweight="bold")
        ax.set_xlabel("xpix"); ax.set_ylabel("ypix")
        ax.grid(True, alpha=0.3)

        # legend (compact)
        elems = []
        for t, (is_primary, n) in handles.items():
            elems.append(Line2D([0], [0],
                                color="black" if is_primary else "dimgray",
                                linewidth=2.2 if is_primary else 1.4,
                                linestyle="-" if is_primary else "--",
                                label=f"{t} (n={n})"))
        for status, style in STATUS_STYLE.items():
            elems.append(Line2D([0], [0], marker=style["m"], color="w",
                                markerfacecolor=style["c"], markeredgecolor="black",
                                markersize=7, label=style["label"]))
        elems.append(Line2D([0], [0], color="gray", linewidth=1, alpha=0.6, label="GT"))
        ax.legend(handles=elems, loc="upper right", fontsize=7, ncol=2)

    fig.suptitle("DFKI 4 Sets 全视野跟踪航迹 — All tracks v15",
                 fontsize=16, fontweight="bold", y=1.005)
    out = OUT / "all_sets_tracks_overview.png"
    fig.tight_layout()
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  → {out}")


# ────────────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────────────
def main():
    print("Loading v15 results …")
    sets_data = {s: load_set(s) for s in SETS}

    print("\nGenerating per-set single-panel visualizations …")
    for s in SETS:
        plot_single_per_set(sets_data[s], s)

    print("\nGenerating per-set split visualizations …")
    for s in SETS:
        plot_split_per_set(sets_data[s], s)

    print("\nGenerating 2×2 overview …")
    plot_overview(sets_data)

    print(f"\nAll visualizations saved to {OUT}/")


if __name__ == "__main__":
    main()