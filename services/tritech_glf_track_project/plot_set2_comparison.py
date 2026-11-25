"""
set_2 航迹对比分析：
  - GT 原始轨迹（完整185帧）= 真实 diver 路径（一整圈）
  - 评估的 CONFIRMED+TENTATIVE 帧 = tracker 正确跟踪的片段（半圈）
  - TRACK_LOST 帧              = tracker 跟丢了的片段
  - 评估文件开头的 19 帧缺失    = set_2 第一段轨迹（gt文件有但评估未覆盖）
"""
import sys
from pathlib import Path

import pandas as pd, numpy as np, matplotlib
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14, DFKI_ROOT
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch
from matplotlib.lines import Line2D

OUT = str(DFKI_OUTPUT_V14 / "visualizations")

# ── 1. Load data ──────────────────────────────────────────────────────────────
gt = pd.read_csv(str(DFKI_ROOT / "set_2" / "tracking_sonar_coordinates_2.csv"))
gt["ts"] = gt["image_name"].str.replace(".jpg","",regex=False).astype(float)
gt = gt.sort_values("ts").reset_index(drop=True)

ev = pd.read_csv(str(DFKI_OUTPUT_V14 / "dfki_set_2" / "gt_evaluation.csv"))
ev["ts"] = ev["image_name"].str.replace(".jpg","",regex=False).astype(float)
ev = ev.sort_values("ts").reset_index(drop=True)

# ── 2. 合并：评估中每个帧对应的GT坐标 ──────────────────────────────────────────
# 评估的gt_x/gt_y 即来自GT文件（已验证0不一致）
confirmed = ev[ev["status"] == "CONFIRMED"].copy()
tentative = ev[ev["status"] == "TENTATIVE"].copy()
lost      = ev[ev["status"] == "TRACK_LOST"].copy()

# 未被评估文件覆盖的 GT 帧（开头缺失19帧）
ev_ts = set(ev["ts"].round(3))
missing_at_start = gt[~gt["ts"].round(3).isin(ev_ts)].copy()
print(f"评估缺失帧数: {len(missing_at_start)}")
print(f"缺失帧 timestamp 范围: {missing_at_start['ts'].min():.3f} - {missing_at_start['ts'].max():.3f}")
print(f"缺失帧坐标范围: x={missing_at_start['x'].min()}-{missing_at_start['x'].max()}, "
      f"y={missing_at_start['y'].min()}-{missing_at_start['y'].max()}")

# ── 3. 画图 ────────────────────────────────────────────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(16, 8))

# 左图：完整GT轨迹（真实路径）
ax = axes[0]
ax.set_facecolor("#f8f9fa")

# 背景雷达网格
for r in [100, 200, 300, 400]:
    ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                              alpha=0.25, linestyle=":", linewidth=1))
ax.axhline(0, color="gray", alpha=0.15, lw=0.5)
ax.axvline(0, color="gray", alpha=0.15, lw=0.5)

# GT完整轨迹，按时间着色
gt_ts_arr = gt["ts"].values
gt_ts_norm = (gt_ts_arr - gt_ts_arr.min()) / (gt_ts_arr.max() - gt_ts_arr.min() + 1e-9)
sc = ax.scatter(gt["x"], gt["y"], c=gt_ts_norm, cmap="plasma",
                 s=25, alpha=0.9, edgecolors="white", linewidths=0.3,
                 zorder=3, vmin=0, vmax=1)
ax.plot(gt["x"], gt["y"], "-", color="gray", linewidth=1, alpha=0.4, zorder=2)

# 标注起点终点
ax.scatter([gt["x"].iloc[0]], [gt["y"].iloc[0]], c="lime", s=200,
           marker="o", edgecolors="black", linewidths=2, zorder=6)
ax.scatter([gt["x"].iloc[-1]], [gt["y"].iloc[-1]], c="red", s=200,
           marker="X", edgecolors="black", linewidths=2, zorder=6)
ax.annotate("START", (gt["x"].iloc[0], gt["y"].iloc[0]),
             textcoords="offset points", xytext=(8, 5), fontsize=9,
             fontweight="bold", color="green")
ax.annotate("END", (gt["x"].iloc[-1], gt["y"].iloc[-1]),
             textcoords="offset points", xytext=(8, -5), fontsize=9,
             fontweight="bold", color="red")

cbar = plt.colorbar(sc, ax=ax, fraction=0.04, pad=0.02, shrink=0.75)
cbar.set_label("Time progression", fontsize=9)

ax.set_title("GT 真实航迹（完整185帧）\nset_2 — 真实 diver 运动路径",
             fontsize=12, fontweight="bold")
ax.set_xlabel("xpix (right from sonar center)")
ax.set_ylabel("ypix (forward / up)")
ax.set_aspect("equal")
ax.invert_yaxis()
ax.grid(True, alpha=0.3)
ax.set_xlim(310, 580)
ax.set_ylim(380, 180)

# 右图：分色对比
ax2 = axes[1]
ax2.set_facecolor("#f8f9fa")

for r in [100, 200, 300, 400]:
    ax2.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                              alpha=0.25, linestyle=":", linewidth=1))

# ① CONFIRMED：正确跟踪的片段（评估文件的半圈）
ax2.plot(confirmed["gt_x"], confirmed["gt_y"], "-",
         color="#3b82f6", linewidth=2.5, alpha=0.8,
         label=f"CONFIRMED ({len(confirmed)}帧)  真实误差 mean={confirmed['error_px'].mean():.1f}px")
ax2.scatter(confirmed["gt_x"], confirmed["gt_y"],
           c="#3b82f6", s=20, alpha=0.7, zorder=4)

# ② TENTATIVE：起始试探帧
ax2.plot(tentative["gt_x"], tentative["gt_y"], "-",
         color="#f59e0b", linewidth=2, alpha=0.8,
         label=f"TENTATIVE ({len(tentative)}帧)  误差 mean={tentative['error_px'].mean():.1f}px")
ax2.scatter(tentative["gt_x"], tentative["gt_y"],
           c="#f59e0b", s=30, alpha=0.9, zorder=5)

# ③ TRACK_LOST：跟丢了的帧（评估文件里这72帧被标记为丢失）
ax2.plot(lost["gt_x"], lost["gt_y"], "-",
         color="#ef4444", linewidth=2, alpha=0.8,
         label=f"TRACK_LOST ({len(lost)}帧)  ← tracker跟丢了")
ax2.scatter(lost["gt_x"], lost["gt_y"],
           c="#ef4444", s=20, alpha=0.7, zorder=4)

# ④ 评估缺失的帧（GT文件有，评估没有）
ax2.plot(missing_at_start["x"], missing_at_start["y"], "--",
         color="#6b7280", linewidth=2, alpha=0.9,
         label=f"GT有但评估缺失 ({len(missing_at_start)}帧)  ← set_2开头")
ax2.scatter(missing_at_start["x"], missing_at_start["y"],
           c="#6b7280", s=20, alpha=0.9, zorder=5,
           marker="s")

# 起点终点
ax2.scatter([gt["x"].iloc[0]], [gt["y"].iloc[0]], c="lime", s=200,
           marker="o", edgecolors="black", linewidths=2, zorder=6)
ax2.scatter([gt["x"].iloc[-1]], [gt["y"].iloc[-1]], c="red", s=200,
           marker="X", edgecolors="black", linewidths=2, zorder=6)
ax2.annotate("START", (gt["x"].iloc[0], gt["y"].iloc[0]),
             textcoords="offset points", xytext=(8, 5), fontsize=9,
             fontweight="bold", color="green")
ax2.annotate("END", (gt["x"].iloc[-1], gt["y"].iloc[-1]),
             textcoords="offset points", xytext=(8, -5), fontsize=9,
             fontweight="bold", color="red")

# 统计信息框
stats = (
    f"GT总帧: 185\n"
    f"CONFIRMED: {len(confirmed)}帧  error={confirmed['error_px'].mean():.1f}px (良好)\n"
    f"TENTATIVE: {len(tentative)}帧  error={tentative['error_px'].mean():.1f}px\n"
    f"TRACK_LOST: {len(lost)}帧  ←跟丢区域\n"
    f"评估缺失: {len(missing_at_start)}帧  ←set_2开头未覆盖"
)
ax2.text(0.02, 0.98, stats, transform=ax2.transAxes,
         va="top", ha="left", fontsize=8.5, family="monospace",
         bbox=dict(boxstyle="round,pad=0.4", facecolor="white",
                   edgecolor="gray", alpha=0.92))

ax2.set_title("tracker评估 vs GT真实航迹\nCONFIRMED半圈 + TRACK_LOST半圈 = 完整GT一圈",
              fontsize=12, fontweight="bold")
ax2.set_xlabel("xpix (right from sonar center)")
ax2.set_ylabel("ypix (forward / up)")
ax2.set_aspect("equal")
ax2.invert_yaxis()
ax2.grid(True, alpha=0.3)
ax2.set_xlim(310, 580)
ax2.set_ylim(380, 180)
ax2.legend(loc="upper right", fontsize=9)

fig.suptitle("SET 2 航迹根因分析：GT真实一圈 vs tracker半圈",
             fontsize=14, fontweight="bold", y=1.01)
fig.tight_layout()
fig.savefig(f"{OUT}/set2_trajectory_comparison.png", dpi=140, bbox_inches="tight")
plt.close(fig)
print(f"\n✓ Saved: {OUT}/set2_trajectory_comparison.png")

# ── 4. 打印 TRACK_LOST 的 tracker 输出 vs GT 的差距 ────────────────────────────
print("\n" + "="*70)
print("TRACK_LOST 帧的 tracker 输出 vs GT 真实位置（frame_id 101-112）")
print("="*70)
lost_detail = lost[["frame_id","image_name","track_x","track_y","gt_x","gt_y"]].copy()
lost_detail["err_px"] = np.sqrt((lost_detail["track_x"]-lost_detail["gt_x"])**2 +
                                  (lost_detail["track_y"]-lost_detail["gt_y"])**2)
print(lost_detail.head(15).to_string(index=False))
print(f"\nTRACK_LOST tracker误差: mean={lost_detail['err_px'].mean():.0f}px, max={lost_detail['err_px'].max():.0f}px")
print(f"GT实际轨迹段: x≈{lost['gt_x'].min():.0f}-{lost['gt_x'].max():.0f}, y≈{lost['gt_y'].min():.0f}-{lost['gt_y'].max():.0f}")
print(f"tracker错误轨迹: x≈{lost['track_x'].min():.0f}-{lost['track_x'].max():.0f}, y≈{lost['track_y'].min():.0f}-{lost['track_y'].max():.0f}")

# ── 5. 分析评估缺失的19帧（set_2开头）────────────────────────────────────────
print("\n" + "="*70)
print("评估缺失的19帧（set_2开头，GT有但评估未覆盖）")
print("="*70)
print(missing_at_start[["image_name","x","y","ts"]].to_string(index=False))
