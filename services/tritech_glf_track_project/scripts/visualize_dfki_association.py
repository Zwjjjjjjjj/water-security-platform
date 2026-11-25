#!/usr/bin/env python3
"""Visualize DFKI track-set association results.

Per-set outputs (in /root/Tritech_GLF_Track_Project/outputs/06_association/):
  track_overlay_set_N.png       — A and B tracks in common frame, color-coded
  association_timeline_set_N.png — per-frame time difference + position error
  set_N_dashboard.png            — combined dashboard

Final output:
  association_dashboard.png    — cross-set summary
"""

from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from tritech_paths import OUTPUT_ROOT
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ASSOC_DIR = OUTPUT_ROOT / "06_association"


def _load_set(set_num):
    a = pd.read_csv(ASSOC_DIR / f"sonar_A_dfki_set_{set_num}_common_aug.csv")
    b = pd.read_csv(ASSOC_DIR / f"sonar_B_dfki_set_{set_num}_common_aug.csv")
    res = pd.read_csv(ASSOC_DIR / f"set_{set_num}/association_set_{set_num}_results.csv")
    summ = pd.read_csv(ASSOC_DIR / f"set_{set_num}/association_set_{set_num}_summary.csv")
    return a, b, res, summ


# ---------------------------------------------------------------------------
# Per-set: track overlay
# ---------------------------------------------------------------------------
def plot_track_overlay(set_num, a, b, res, summ):
    fig, ax = plt.subplots(figsize=(9, 8))

    a_tracks = a["track_id"].unique()
    b_tracks = b["track_id"].unique()

    cmap_a = plt.cm.tab10(np.linspace(0, 1, max(len(a_tracks), 1)))
    cmap_b = plt.cm.tab10(np.linspace(0, 1, max(len(b_tracks), 1)))

    # Plot A tracks (solid lines)
    for i, tid in enumerate(sorted(a_tracks)):
        sub = a[a["track_id"] == tid].sort_values("timestamp_utc")
        # Real diver: blue, Distractors: light grey
        if tid.startswith("A_DIST"):
            ax.plot(sub["common_x_m"], sub["common_y_m"], "-", color="lightgray",
                    linewidth=1, alpha=0.6, label=f"A: {tid} (distractor)")
        else:
            ax.plot(sub["common_x_m"], sub["common_y_m"], "-", color=cmap_a[i],
                    linewidth=2.5, label=f"A: {tid}")

    # Plot B tracks (dashed lines)
    for i, tid in enumerate(sorted(b_tracks)):
        sub = b[b["track_id"] == tid].sort_values("timestamp_utc")
        if tid.startswith("B_DIST"):
            ax.plot(sub["common_x_m"], sub["common_y_m"], "--", color="lightgray",
                    linewidth=1, alpha=0.6, label=f"B: {tid} (distractor)")
        else:
            ax.plot(sub["common_x_m"], sub["common_y_m"], "--", color=cmap_b[i],
                    linewidth=2.0, label=f"B: {tid}")

    # Plot accepted matches as red lines connecting matched points
    accepted = res[res["pair_accepted"] == True]  # noqa: E712
    if len(accepted):
        for _, row in accepted.iterrows():
            ax.plot([row["A_x_m"], row["B_x_m"]],
                    [row["A_y_m"], row["B_y_m"]],
                    color="red", linewidth=0.4, alpha=0.25)

    ax.set_xlabel("Common x (m)")
    ax.set_ylabel("Common y (m)")
    ax.set_title(f"set_{set_num} — Track-set association\n"
                 f"(accepted pairs: {int(summ['accepted'].sum())}/{len(summ)})",
                 fontsize=12, fontweight="bold")
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="box")
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    out = ASSOC_DIR / f"track_overlay_set_{set_num}.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Per-set: association timeline
# ---------------------------------------------------------------------------
def plot_association_timeline(set_num, res):
    if len(res) == 0:
        return
    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)

    accepted = res[res["pair_accepted"] == True]  # noqa: E712

    # 1) Position difference
    ax = axes[0]
    ax.plot(accepted["A_time_utc"], accepted["position_difference_m"], "o-",
            color="steelblue", markersize=3, linewidth=1)
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.set_ylabel("pos diff (m)")
    ax.set_title(f"set_{set_num} — Association timeline (accepted matches: {len(accepted)})",
                 fontsize=12, fontweight="bold")
    ax.grid(True, alpha=0.3)

    # 2) Velocity difference
    ax = axes[1]
    ax.plot(accepted["A_time_utc"], accepted["velocity_difference_mps"], "o-",
            color="seagreen", markersize=3, linewidth=1)
    ax.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax.set_ylabel("vel diff (m/s)")
    ax.grid(True, alpha=0.3)

    # 3) Mahalanobis distance
    ax = axes[2]
    ax.plot(accepted["A_time_utc"], accepted["mahalanobis_distance"], "o-",
            color="darkorange", markersize=3, linewidth=1)
    ax.axhline(3.5, color="red", linestyle="--", alpha=0.5, label="MD gate = 3.5")
    ax.set_ylabel("Mahalanobis d")
    ax.set_xlabel("Sonar A timestamp")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)

    for ax in axes:
        for label in ax.get_xticklabels():
            label.set_rotation(30)
            label.set_horizontalalignment("right")

    fig.tight_layout()
    out = ASSOC_DIR / f"association_timeline_set_{set_num}.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Per-set: small dashboard
# ---------------------------------------------------------------------------
def plot_set_dashboard(set_num, a, b, res, summ):
    fig = plt.figure(figsize=(15, 9))
    gs = fig.add_gridspec(2, 3, hspace=0.35, wspace=0.30)

    # (a) Track overlay
    ax_ov = fig.add_subplot(gs[0, :2])
    a_tracks = a["track_id"].unique()
    for tid in sorted(a_tracks):
        sub = a[a["track_id"] == tid].sort_values("timestamp_utc")
        color = "lightgray" if tid.startswith("A_DIST") else "tab:blue"
        ax_ov.plot(sub["common_x_m"], sub["common_y_m"], "-",
                   color=color, linewidth=2 if not color == "lightgray" else 1,
                   alpha=0.7 if color == "lightgray" else 1.0)
    b_tracks = b["track_id"].unique()
    for tid in sorted(b_tracks):
        sub = b[b["track_id"] == tid].sort_values("timestamp_utc")
        color = "lightgray" if tid.startswith("B_DIST") else "tab:orange"
        ax_ov.plot(sub["common_x_m"], sub["common_y_m"], "--",
                   color=color, linewidth=2 if not color == "lightgray" else 1,
                   alpha=0.7 if color == "lightgray" else 1.0)
    accepted = res[res["pair_accepted"] == True]  # noqa: E712
    if len(accepted):
        ax_ov.plot([accepted["A_x_m"], accepted["B_x_m"]],
                   [accepted["A_y_m"], accepted["B_y_m"]],
                   color="red", linewidth=0.3, alpha=0.25)
    ax_ov.set_xlabel("Common x (m)"); ax_ov.set_ylabel("Common y (m)")
    ax_ov.set_title("Track overlay (A=blue solid, B=orange dashed, red=matched)")
    ax_ov.grid(True, alpha=0.3)
    ax_ov.set_aspect("equal", adjustable="box")

    # (b) Summary table
    ax_tab = fig.add_subplot(gs[0, 2])
    ax_tab.axis("off")
    if len(accepted):
        sm = accepted["position_difference_m"].mean()
        sv = accepted["velocity_difference_mps"].mean()
        sd = accepted["mahalanobis_distance"].mean()
        cost = accepted["association_cost"].mean()
    else:
        sm = sv = sd = cost = float("nan")
    cell_text = [
        ["Tracks (A/B)", f"{a['track_id'].nunique()} / {b['track_id'].nunique()}"],
        ["Pairs tested", f"{len(summ)}"],
        ["Pairs accepted", f"{int(summ['accepted'].sum())}"],
        ["Matches (total)", f"{len(accepted)}"],
        ["Mean pos diff (m)", f"{sm:.2f}" if not np.isnan(sm) else "-"],
        ["Mean vel diff (m/s)", f"{sv:.2f}" if not np.isnan(sv) else "-"],
        ["Mean Mahalanobis", f"{sd:.2f}" if not np.isnan(sd) else "-"],
        ["Mean cost", f"{cost:.3f}" if not np.isnan(cost) else "-"],
    ]
    table = ax_tab.table(cellText=cell_text, loc="center", cellLoc="left",
                          colWidths=[0.55, 0.45])
    table.auto_set_font_size(False); table.set_fontsize(10); table.scale(1, 1.6)
    ax_tab.set_title("Pair metrics", fontsize=11, fontweight="bold", pad=10)

    # (c) Histogram of position differences
    ax_h = fig.add_subplot(gs[1, 0])
    if len(accepted):
        ax_h.hist(accepted["position_difference_m"], bins=20,
                  color="steelblue", edgecolor="white")
    ax_h.set_xlabel("pos diff (m)")
    ax_h.set_ylabel("count")
    ax_h.set_title("Distribution of matched position differences")
    ax_h.grid(True, alpha=0.3)

    # (d) Scatter position A vs B
    ax_s = fig.add_subplot(gs[1, 1])
    if len(accepted):
        ax_s.scatter(accepted["A_x_m"], accepted["A_y_m"], s=10, color="blue", label="A")
        ax_s.scatter(accepted["B_x_m"], accepted["B_y_m"], s=10, color="orange", label="B")
        for _, row in accepted.head(50).iterrows():
            ax_s.plot([row["A_x_m"], row["B_x_m"]],
                      [row["A_y_m"], row["B_y_m"]], color="red", linewidth=0.3, alpha=0.5)
    ax_s.set_xlabel("x (m)"); ax_s.set_ylabel("y (m)")
    ax_s.set_title("Matched observation pairs (first 50)")
    ax_s.grid(True, alpha=0.3)
    ax_s.set_aspect("equal", adjustable="box")
    ax_s.legend(fontsize=8)

    # (e) Cost vs position difference
    ax_c = fig.add_subplot(gs[1, 2])
    if len(accepted):
        ax_c.scatter(accepted["association_cost"], accepted["position_difference_m"],
                     s=12, color="purple", alpha=0.6)
    ax_c.set_xlabel("association cost")
    ax_c.set_ylabel("pos diff (m)")
    ax_c.set_title("Cost vs position difference")
    ax_c.grid(True, alpha=0.3)

    fig.suptitle(f"set_{set_num} — Association Dashboard", fontsize=14, fontweight="bold")
    out = ASSOC_DIR / f"set_{set_num}_dashboard.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------------------
# Cross-set dashboard
# ---------------------------------------------------------------------------
def plot_global_dashboard():
    summary_path = ASSOC_DIR / "all_sets_summary.csv"
    if not summary_path.exists():
        return
    df = pd.read_csv(summary_path)

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))

    # Acceptance rate per set
    ax = axes[0]
    ax.bar(df["set"].astype(str), df["pair_acceptance_rate"], color="steelblue")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("acceptance rate (accepted/tested)")
    ax.set_xlabel("Set")
    ax.set_title("Pair acceptance rate", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(df["pair_acceptance_rate"]):
        ax.text(i, v + 0.02, f"{v:.2f}", ha="center", fontweight="bold")

    # Mean position diff per set
    ax = axes[1]
    ax.bar(df["set"].astype(str), df["mean_position_difference_m"], color="seagreen")
    ax.set_ylabel("mean pos diff (m)")
    ax.set_xlabel("Set")
    ax.set_title("Mean position difference (accepted)", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(df["mean_position_difference_m"]):
        ax.text(i, v + 0.05, f"{v:.2f}", ha="center", fontweight="bold")

    # Mean cost per set
    ax = axes[2]
    ax.bar(df["set"].astype(str), df["mean_cost"], color="darkorange")
    ax.set_ylabel("mean cost")
    ax.set_xlabel("Set")
    ax.set_title("Mean association cost", fontweight="bold")
    ax.grid(True, alpha=0.3, axis="y")
    for i, v in enumerate(df["mean_cost"]):
        ax.text(i, v + 0.01, f"{v:.3f}", ha="center", fontweight="bold")

    fig.suptitle("DFKI Track-set Association — Cross-set Summary",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    out = ASSOC_DIR / "association_dashboard.png"
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"\n  -> {out}")


def main():
    for s in [1, 2, 3, 4]:
        a, b, res, summ = _load_set(s)
        print(f"\n  set_{s}: A={a['track_id'].nunique()} tracks, "
              f"B={b['track_id'].nunique()} tracks, "
              f"accepted={int(summ['accepted'].sum())}/{len(summ)} pairs")
        plot_track_overlay(s, a, b, res, summ)
        plot_association_timeline(s, res)
        plot_set_dashboard(s, a, b, res, summ)
    plot_global_dashboard()
    print(f"\nAll visualizations saved to {ASSOC_DIR}/")


if __name__ == "__main__":
    main()
