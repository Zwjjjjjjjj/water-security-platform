"""Final v14 vs v15 comparison visualization."""
import json, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

OUT = Path(str(DFKI_OUTPUT_V15 / "visualizations"))
OUT.mkdir(parents=True, exist_ok=True)

sets = [1, 2, 3, 4]
v14, v15 = {}, {}
for s in sets:
    with open(fstr(DFKI_OUTPUT_V14 / f"dfki_set_{s}" / "gt_evaluation_summary.json")) as f:
        v14[s] = json.load(f)
    with open(fstr(DFKI_OUTPUT_V15 / f"dfki_set_{s}" / "gt_evaluation_summary.json")) as f:
        v15[s] = json.load(f)

# ----- Figure 1: bar chart of error metrics --------------------------------
fig, axes = plt.subplots(2, 2, figsize=(14, 9))

metrics = [
    ("mean_error_px",   "Mean error (px)"),
    ("rmse_px",         "RMSE (px)"),
    ("p90_error_px",    "P90 error (px)"),
    ("max_error_px",    "Max error (px)"),
]

for ax, (m, label) in zip(axes.flat, metrics):
    a = [v14[s].get(m, 0) or 0 for s in sets]
    b = [v15[s].get(m, 0) or 0 for s in sets]
    x = np.arange(len(sets))
    w = 0.35
    ax.bar(x - w/2, a, w, label="v14 (前)", color="#94a3b8", edgecolor="black")
    ax.bar(x + w/2, b, w, label="v15 (后)", color="#3b82f6", edgecolor="black")
    ax.set_xticks(x)
    ax.set_xticklabels([f"set_{s}" for s in sets])
    ax.set_ylabel(label)
    ax.set_title(f"{label} — v14 vs v15")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    for i, (av, bv) in enumerate(zip(a, b)):
        ax.text(i - w/2, av + 0.5, f"{av:.1f}", ha="center", fontsize=8)
        ax.text(i + w/2, bv + 0.5, f"{bv:.1f}", ha="center", fontsize=8,
                fontweight="bold", color="#1e40af")

fig.suptitle("All-Sets Error Metrics: v14 (修复前) vs v15 (修复后)",
             fontsize=14, fontweight="bold")
fig.tight_layout()
fig.savefig(OUT / "v14_vs_v15_metrics.png", dpi=140, bbox_inches="tight")
plt.close(fig)
print(f"Saved: {OUT}/v14_vs_v15_metrics.png")

# ----- Figure 2: trajectory comparison for set_2 ----------------------------
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_OUTPUT_V15, DFKI_ROOT
gt = pd.read_csv(str(DFKI_ROOT / "set_2" / "tracking_sonar_coordinates_2.csv"))
gt["ts"] = gt["image_name"].str.replace(".jpg","",regex=False).astype(float)
gt = gt.sort_values("ts").reset_index(drop=True)
ev14 = pd.read_csv(str(DFKI_OUTPUT_V14 / "dfki_set_2" / "gt_evaluation.csv"))
ev15 = pd.read_csv(str(DFKI_OUTPUT_V15 / "dfki_set_2" / "gt_evaluation.csv"))

# Status colour map
status_colors = {
    "CONFIRMED":      "#3b82f6",
    "TENTATIVE":      "#f59e0b",
    "DETECTOR_ORACLE":"#8b5cf6",
    "LOST_RECOVERED": "#10b981",  # v15 new
    "TRACK_LOST":     "#ef4444",
}

fig, axes = plt.subplots(1, 2, figsize=(16, 8))

for ax, ev, title in zip(axes, [ev14, ev15], ["v14 (修复前)", "v15 (修复后)"]):
    ax.set_facecolor("#f8f9fa")
    for r in [100, 200, 300, 400]:
        ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                                alpha=0.2, linestyle=":", linewidth=1))
    # GT
    ax.plot(gt["x"], gt["y"], "-", color="gray", linewidth=1.5, alpha=0.5,
            label=f"GT 真实 ({len(gt)} 帧)", zorder=2)
    # ev segments
    for status, color in status_colors.items():
        sub = ev[ev["status"] == status]
        if len(sub) == 0:
            continue
        marker = "x" if status == "TRACK_LOST" else ("*" if status == "TENTATIVE" else "o")
        ax.scatter(sub["track_x"], sub["track_y"], c=color, s=35, alpha=0.85,
                   edgecolors="black", linewidths=0.3, marker=marker,
                   label=f"{status} ({len(sub)})", zorder=4)
        # connect with thin line
        if len(sub) > 1:
            sub_sorted = sub.sort_values("frame_id")
            ax.plot(sub_sorted["track_x"], sub_sorted["track_y"], "-",
                    color=color, alpha=0.5, linewidth=1.2, zorder=3)
    # Start/end
    ax.scatter([gt["x"].iloc[0]], [gt["y"].iloc[0]], c="lime", s=200,
               marker="o", edgecolors="black", linewidths=2, zorder=6)
    ax.scatter([gt["x"].iloc[-1]], [gt["y"].iloc[-1]], c="red", s=200,
               marker="X", edgecolors="black", linewidths=2, zorder=6)

    # stats
    n_conf  = (ev["status"] == "CONFIRMED").sum()
    n_rec   = (ev["status"] == "LOST_RECOVERED").sum()
    n_lost  = (ev["status"] == "TRACK_LOST").sum()
    mean_err = ev["error_px"].mean()
    max_err  = ev["error_px"].max()
    stats = (f"CONFIRMED     : {n_conf}\n"
             f"LOST_RECOVERED: {n_rec}\n"
             f"TRACK_LOST    : {n_lost}\n"
             f"Mean err      : {mean_err:.2f} px\n"
             f"Max err       : {max_err:.2f} px")
    ax.text(0.02, 0.98, stats, transform=ax.transAxes,
            va="top", ha="left", fontsize=9, family="monospace",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                      edgecolor="gray", alpha=0.92))
    ax.set_title(f"{title} — set_2 航迹", fontsize=12, fontweight="bold")
    ax.set_xlabel("xpix (right from sonar center)")
    ax.set_ylabel("ypix (forward / up)")
    ax.set_aspect("equal")
    ax.invert_yaxis()
    ax.grid(True, alpha=0.3)
    ax.set_xlim(310, 580)
    ax.set_ylim(380, 180)
    ax.legend(loc="lower right", fontsize=8)

fig.suptitle("set_2 真实航迹 vs tracker 评估结果（v14 → v15 修复对比）",
             fontsize=14, fontweight="bold", y=1.01)
fig.tight_layout()
fig.savefig(OUT / "set2_v14_vs_v15_trajectory.png", dpi=140, bbox_inches="tight")
plt.close(fig)
print(f"Saved: {OUT}/set2_v14_vs_v15_trajectory.png")

# ----- Figure 3: delta summary table ---------------------------------------
fig, ax = plt.subplots(figsize=(11, 4))
ax.axis("off")
table_data = [["Set", "Metric", "v14", "v15", "Δ"]]
metrics_summary = [
    ("Track Lost",      "n_track_lost"),
    ("Lost Recovered",  "n_lost_recovered"),
    ("Mean err (px)",   "mean_error_px"),
    ("Median err (px)", "median_error_px"),
    ("RMSE (px)",       "rmse_px"),
    ("P90 (px)",        "p90_error_px"),
    ("Max (px)",        "max_error_px"),
]
for s in sets:
    for label, m in metrics_summary:
        a = v14[s].get(m, 0) or 0
        b = v15[s].get(m, 0) or 0
        a_str = f"{a:.2f}" if isinstance(a, float) else str(a)
        b_str = f"{b:.2f}" if isinstance(b, float) else str(b)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
            d = (b - a) / a * 100
            d_str = f"{d:+.1f}%"
        else:
            d_str = "—"
        table_data.append([f"set_{s}", label, a_str, b_str, d_str])

table = ax.table(cellText=table_data, loc="center", cellLoc="center",
                 colWidths=[0.08, 0.25, 0.12, 0.12, 0.12])
table.auto_set_font_size(False)
table.set_fontsize(10)
table.scale(1, 1.4)
# Header styling
for j in range(5):
    table[0, j].set_facecolor("#1f2937")
    table[0, j].set_text_props(color="white", fontweight="bold")

ax.set_title("Final v14 → v15 Comparison",
             fontsize=14, fontweight="bold", pad=20)
fig.savefig(OUT / "v14_vs_v15_table.png", dpi=140, bbox_inches="tight")
plt.close(fig)
print(f"Saved: {OUT}/v14_vs_v15_table.png")
print("\nDone!")
