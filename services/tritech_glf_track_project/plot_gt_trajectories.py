"""
Plot the four real diver trajectories (one per set) from GT data.

Source: /root/Diver Tracking - Sonar/set_{1-4}/tracking_sonar_coordinates_{1-4}.csv

Outputs:
  /root/dfki_output_v14/visualizations/gt_trajectory_set_{1-4}.png  (per set)
  /root/dfki_output_v14/visualizations/gt_trajectory_all_sets.png    (combined)
"""

import numpy as np
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_ROOT
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap

GT_ROOT = Path(str(DFKI_ROOT))
OUT_DIR = Path(str(DFKI_OUTPUT_V14 / "visualizations"))
OUT_DIR.mkdir(parents=True, exist_ok=True)

SETS = [1, 2, 3, 4]
COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444"]
LABELS = ["set 1", "set 2", "set 3", "set 4"]


def load_gt(s):
    """Load GT for set s; derive frame index from image filename timestamp."""
    p = GT_ROOT / f"set_{s}" / f"tracking_sonar_coordinates_{s}.csv"
    df = pd.read_csv(p)
    # Sort by timestamp parsed from filename (image_name is a timestamp filename)
    df["timestamp_s"] = pd.to_numeric(
        df["image_name"].str.replace(r"\.jpg$", "", regex=True),
        errors="coerce"
    )
    df = df.sort_values("timestamp_s").reset_index(drop=True)
    df["frame_idx"] = np.arange(len(df))
    # Velocity per frame (px/frame)
    dx = df["x"].diff()
    dy = df["y"].diff()
    df["speed_px"] = np.sqrt(dx**2 + dy**2)
    return df


def plot_single_set(df, s):
    """Plot single-set GT trajectory with polar rings + velocity colormap."""
    fig, ax = plt.subplots(figsize=(8, 8))

    # Background polar grid (sonar convention: origin = bottom-center)
    for r in [100, 200, 300, 400, 500]:
        ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                                alpha=0.3, linestyle=":", linewidth=1))
    # Cross-hairs at u=0, v=0
    ax.axhline(0, color="gray", linestyle=":", alpha=0.2, linewidth=0.5)
    ax.axvline(0, color="gray", linestyle=":", alpha=0.2, linewidth=0.5)

    # Trajectory with velocity-colored segments
    points = np.array([df["x"].values, df["y"].values]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)

    # Color by speed (blue=fast, red=slow — inverted of 'jet' for readability)
    speeds = df["speed_px"].fillna(0).values
    norm = plt.Normalize(vmin=0, vmax=max(10, np.percentile(speeds, 95)))
    lc = LineCollection(segments, cmap="plasma", norm=norm,
                         linewidths=2.5, alpha=0.85)
    lc.set_array(speeds[:-1])
    ax.add_collection(lc)

    # Frame markers
    ax.scatter(df["x"], df["y"],
               c=df["frame_idx"], cmap="viridis",
               s=15, alpha=0.6, edgecolors="white", linewidths=0.3,
               zorder=3)

    # Start (green) and end (red) markers
    ax.scatter([df["x"].iloc[0]], [df["y"].iloc[0]],
               c="lime", s=180, marker="o", edgecolors="black",
               linewidths=1.5, zorder=5, label="Start")
    ax.scatter([df["x"].iloc[-1]], [df["y"].iloc[-1]],
               c="red", s=180, marker="X", edgecolors="black",
               linewidths=1.5, zorder=5, label="End")

    # Auto-fit with sonar-origin margin
    all_x = df["x"].values
    all_y = df["y"].values
    pad = 40
    ax.set_xlim(max(0, all_x.min() - pad), all_x.max() + pad)
    ax.set_ylim(max(0, all_y.min() - pad), all_y.max() + pad)
    ax.invert_yaxis()  # sonar convention

    ax.set_xlabel("xpix (right from sonar center)", fontsize=10)
    ax.set_ylabel("ypix (forward / up)", fontsize=10)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)

    # Colorbars
    cbar1 = plt.colorbar(lc, ax=ax, fraction=0.04, pad=0.02, shrink=0.7)
    cbar1.set_label("Speed (px/frame)", fontsize=9)
    cbar2 = plt.colorbar(plt.cm.ScalarMappable(cmap="viridis",
                                                norm=plt.Normalize(
                                                    vmin=df["frame_idx"].min(),
                                                    vmax=df["frame_idx"].max())),
                          ax=ax, fraction=0.04, pad=0.06, shrink=0.7)
    cbar2.set_label("Frame index", fontsize=9)

    # Stats annotation
    total_dist = df["speed_px"].sum()
    mean_speed = df["speed_px"].mean()
    max_speed = df["speed_px"].max()
    duration = (df["timestamp_s"].max() - df["timestamp_s"].min())
    stats_txt = (f"frames: {len(df)}\n"
                 f"duration: {duration:.1f} s\n"
                 f"total distance: {total_dist:.0f} px\n"
                 f"mean speed: {mean_speed:.2f} px/frame\n"
                 f"max speed: {max_speed:.2f} px/frame")
    ax.text(0.02, 0.98, stats_txt,
            transform=ax.transAxes, va="top", ha="left",
            fontsize=9, family="monospace",
            bbox=dict(boxstyle="round,pad=0.4",
                      facecolor="white", edgecolor="gray", alpha=0.9))

    ax.set_title(f"SET {s} — Real Diver Trajectory (from GT)",
                 fontsize=13, fontweight="bold", pad=10)
    ax.legend(loc="upper right", fontsize=10)

    fig.tight_layout()
    out = OUT_DIR / f"gt_trajectory_set_{s}.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"  -> {out}")
    return stats_txt


def plot_all_sets(gt_data, stats_per_set):
    """Plot all four trajectories on one chart for comparison."""
    fig, ax = plt.subplots(figsize=(10, 10))

    # Background polar rings (for reference scale)
    for r in [100, 200, 300, 400, 500]:
        ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                                alpha=0.2, linestyle=":", linewidth=1))

    all_x = []
    all_y = []

    for s, color, label, df in zip(SETS, COLORS, LABELS, gt_data):
        # Trajectory line
        ax.plot(df["x"], df["y"], "-", color=color, linewidth=2,
                alpha=0.7, label=f"{label} ({len(df)} frames)")

        # Start (filled circle) and end (X) markers
        ax.scatter([df["x"].iloc[0]], [df["y"].iloc[0]],
                   c=color, s=120, marker="o", edgecolors="black",
                   linewidths=1.5, zorder=5)
        ax.scatter([df["x"].iloc[-1]], [df["y"].iloc[-1]],
                   c=color, s=140, marker="X", edgecolors="black",
                   linewidths=1.5, zorder=5)

        # Frame index labels at start/end
        ax.annotate(f"{label} start", (df["x"].iloc[0], df["y"].iloc[0]),
                     textcoords="offset points", xytext=(8, -8),
                     fontsize=8, color=color, fontweight="bold")
        ax.annotate(f"{label} end", (df["x"].iloc[-1], df["y"].iloc[-1]),
                     textcoords="offset points", xytext=(8, 8),
                     fontsize=8, color=color, fontweight="bold")

        all_x.extend(df["x"].values)
        all_y.extend(df["y"].values)

    ax.set_xlim(max(0, min(all_x) - 30), max(all_x) + 30)
    ax.set_ylim(max(0, min(all_y) - 30), max(all_y) + 30)
    ax.invert_yaxis()
    ax.set_xlabel("xpix (right from sonar center)", fontsize=11)
    ax.set_ylabel("ypix (forward / up)", fontsize=11)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)

    # Legend with custom markers for start/end
    from matplotlib.lines import Line2D
    legend_elements = []
    for s, color, label, df in zip(SETS, COLORS, LABELS, gt_data):
        legend_elements.append(Line2D([0], [0], color=color, linewidth=2,
                                       label=f"{label} ({len(df)} frames)"))
    legend_elements.append(Line2D([0], [0], marker="o", color="w",
                                   markerfacecolor="gray", markersize=10,
                                   markeredgecolor="black",
                                   label="Start"))
    legend_elements.append(Line2D([0], [0], marker="X", color="w",
                                   markerfacecolor="gray", markersize=10,
                                   markeredgecolor="black",
                                   label="End"))
    ax.legend(handles=legend_elements, loc="upper right", fontsize=9)

    ax.set_title("All 4 Sets — Real Diver Trajectories (from GT)",
                 fontsize=14, fontweight="bold", pad=10)

    fig.tight_layout()
    out = OUT_DIR / "gt_trajectory_all_sets.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(f"  -> {out}")


def plot_speed_profiles(gt_data):
    """Plot speed-over-time profile for each set."""
    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=False)

    for ax, s, color, df in zip(axes, SETS, COLORS, gt_data):
        ax.plot(df["frame_idx"], df["speed_px"],
                "-", color=color, linewidth=1.2, alpha=0.8)
        ax.fill_between(df["frame_idx"], 0, df["speed_px"],
                         color=color, alpha=0.2)

        mean_s = df["speed_px"].mean()
        ax.axhline(mean_s, color="black", linestyle="--", alpha=0.4,
                    label=f"mean = {mean_s:.2f} px/frame")

        ax.set_ylabel("Speed (px/frame)", fontsize=9)
        ax.set_title(f"SET {s} — Speed Profile  "
                     f"(total dist = {df['speed_px'].sum():.0f} px, "
                     f"duration = {(df['timestamp_s'].max()-df['timestamp_s'].min()):.1f}s)",
                     fontsize=10, fontweight="bold")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=9)
        ax.set_xlim(0, len(df) - 1)

    axes[-1].set_xlabel("Frame index (sorted by timestamp)", fontsize=10)
    fig.suptitle("GT Diver Speed vs Time — All Sets",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    out = OUT_DIR / "gt_speed_profile_all_sets.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"  -> {out}")


def main():
    print("Loading GT data...")
    gt_data = []
    stats_per_set = []
    for s in SETS:
        df = load_gt(s)
        gt_data.append(df)
        print(f"  set_{s}: {len(df)} points, "
              f"x={df['x'].min()}-{df['x'].max()}, "
              f"y={df['y'].min()}-{df['y'].max()}")

    print("\nGenerating visualizations:")
    for s, df in zip(SETS, gt_data):
        stats_per_set.append(plot_single_set(df, s))

    plot_all_sets(gt_data, stats_per_set)
    plot_speed_profiles(gt_data)

    print(f"\nAll GT trajectory plots saved to {OUT_DIR}/")


if __name__ == "__main__":
    main()
