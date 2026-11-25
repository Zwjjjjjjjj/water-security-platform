"""
Visualize GT tracking evaluation results across sets.

Reads /root/dfki_output_v10/dfki_set_*/gt_evaluation.csv and produces:
1) Per-track error boxplot per set
2) Frame-by-frame GT vs prediction overlay
3) Cumulative distribution of errors (CDF)
4) Summary bar chart of mean/median/rmse/recall across sets

Output: /root/dfki_output_v10/visualizations/
"""

import json
from pathlib import Path
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from tritech_paths import DFKI_OUTPUT_V14
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

ROOT = Path(str(DFKI_OUTPUT_V14))
OUT = ROOT / "visualizations"
OUT.mkdir(parents=True, exist_ok=True)

SETS = ["set_1", "set_2", "set_3", "set_4"]


def load_all():
    """Load GT evaluation CSVs from available sets."""
    sets_data = {}
    for s in SETS:
        d = ROOT / f"dfki_{s}"
        ge = d / "gt_evaluation.csv"
        summ = d / "gt_evaluation_summary.json"
        if ge.exists() and summ.exists():
            eval_df = pd.read_csv(ge)
            # Drop TRACK_LOST rows (error_px is NaN — no valid tracking)
            eval_df = eval_df[eval_df["error_px"].notna()].copy()
            sets_data[s] = {
                "eval": eval_df,
                "summary": json.load(open(summ)),
            }
            print(f"  Loaded {s}: {len(sets_data[s]['eval'])} evaluated rows "
                  f"(excluding TRACK_LOST), median={sets_data[s]['summary']['median_error_px']:.2f}px")
    return sets_data


# --------------------------------------------------------------------------
# 1) Summary bar chart across sets
# --------------------------------------------------------------------------
def plot_summary(sets_data):
    fig, axes = plt.subplots(1, 4, figsize=(16, 4))
    titles = ["Mean Error (px)", "Median Error (px)", "RMSE (px)", "Recall (%)"]
    keys = ["mean_error_px", "median_error_px", "rmse_px", "detection_recall"]

    sets = list(sets_data.keys())
    x = np.arange(len(sets))
    colors = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444"]

    for ax, key, title in zip(axes, keys, titles):
        vals = [sets_data[s]["summary"][key] for s in sets]
        if key == "detection_recall":
            vals = [v * 100 for v in vals]
            ax.set_ylim(0, 105)
        bars = ax.bar(x, vals, color=colors[:len(sets)])
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([s.replace("_", " ").upper() for s in sets])
        ax.grid(True, axis="y", alpha=0.3)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
                    f"{v:.1f}" + ("%" if key == "detection_recall" else ""),
                    ha="center", va="bottom", fontsize=10, fontweight="bold")
    fig.suptitle("DFKI Tracking Evaluation Summary", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT / "summary_bars.png", dpi=120)
    plt.close(fig)
    print(f"  → {OUT / 'summary_bars.png'}")


# --------------------------------------------------------------------------
# 2) Per-track error boxplot
# --------------------------------------------------------------------------
def plot_per_track_box(sets_data):
    for sname, data in sets_data.items():
        ev = data["eval"]
        tracks = sorted(ev.track_id.unique())
        fig, ax = plt.subplots(figsize=(12, 5))
        grouped = [ev[ev.track_id == t].error_px.values for t in tracks]
        bp = ax.boxplot(grouped, labels=tracks, patch_artist=True,
                        showmeans=True, meanline=True)
        for patch in bp["boxes"]:
            patch.set_facecolor("#bfdbfe")
        ax.set_ylabel("Error (px)")
        ax.set_title(f"{sname.upper()} — Per-Track Error Distribution", fontsize=13, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_ylim(0, max(50, ev.error_px.quantile(0.99) * 1.1))
        # Annotate medians
        for i, t in enumerate(tracks):
            med = np.median(grouped[i])
            ax.text(i + 1, med + 1.0, f"{med:.1f}", ha="center", fontsize=9, color="navy")
        fig.tight_layout()
        fig.savefig(OUT / f"per_track_{sname}.png", dpi=120)
        plt.close(fig)
        print(f"  → {OUT / f'per_track_{sname}.png'}")


# --------------------------------------------------------------------------
# 3) CDF of errors per set
# --------------------------------------------------------------------------
def plot_cdf(sets_data):
    fig, ax = plt.subplots(figsize=(8, 5))
    for sname, data in sets_data.items():
        ev = data["eval"]
        errs = np.sort(ev.error_px.values)
        cdf = np.arange(1, len(errs) + 1) / len(errs)
        ax.plot(errs, cdf, label=f"{sname.upper()} (n={len(errs)})", linewidth=2)
    ax.axvline(20, color="green", linestyle="--", alpha=0.5, label="20 px threshold")
    ax.axvline(50, color="orange", linestyle="--", alpha=0.5, label="50 px threshold")
    ax.set_xlabel("Error (px)")
    ax.set_ylabel("Cumulative fraction")
    ax.set_title("CDF of Tracking Error", fontsize=13, fontweight="bold")
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 100)
    ax.legend(loc="lower right", fontsize=9)
    fig.tight_layout()
    fig.savefig(OUT / "cdf_errors.png", dpi=120)
    plt.close(fig)
    print(f"  → {OUT / 'cdf_errors.png'}")


# --------------------------------------------------------------------------
# 4) GT vs predicted positions on sonar frame
# --------------------------------------------------------------------------
def plot_trajectories(sets_data):
    """Plot GT and predicted tracks on a single polar chart per set."""
    for sname, data in sets_data.items():
        ev = data["eval"]
        fig, ax = plt.subplots(figsize=(7, 7))
        # Background: greyed polar grid
        for r in [100, 200, 300]:
            ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray",
                                    alpha=0.3, linestyle=":"))
        # Note: axis labels reflect sonar frame where origin (0,0) is bottom-center
        track_colors = {
            "CONFIRMED":         "#3b82f6",
            "DETECTOR_ORACLE":   "#10b981",
            "TENTATIVE":         "#f59e0b",
        }
        for t in sorted(ev.track_id.unique()):
            sub = ev[ev.track_id == t].sort_values("frame_id")
            for status in ["CONFIRMED", "DETECTOR_ORACLE", "TENTATIVE"]:
                ss = sub[sub["status"] == status]
                if len(ss):
                    color = track_colors[status]
                    label = f"{t} [{status}]" if status == "CONFIRMED" else None
                    ax.plot(ss.track_x, ss.track_y, "-", color=color,
                            linewidth=2, alpha=0.7, label=label)
                    # GT path as thin dotted line
                    ax.plot(ss.gt_x, ss.gt_y, ":", color=color, linewidth=1, alpha=0.3)
            # Error vectors only for non-ORACLE (ORACLE already shows replacement)
            non_oracle = sub[sub["status"] != "DETECTOR_ORACLE"]
            if len(non_oracle):
                ax.plot([non_oracle.gt_x.values, non_oracle.track_x.values],
                        [non_oracle.gt_y.values, non_oracle.track_y.values],
                        color="red", alpha=0.1, linewidth=0.5)
        # Legend for track IDs and statuses
        ax.plot([], [], "-", color="#3b82f6", linewidth=2, label="CONFIRMED")
        ax.plot([], [], "-", color="#10b981", linewidth=2, label="DETECTOR_ORACLE")
        ax.plot([], [], ":", color="gray", linewidth=1, label="GT (dotted)")
        # auto-fit to data
        if len(ev):
            all_x = np.concatenate([ev.gt_x.values, ev.track_x.values])
            all_y = np.concatenate([ev.gt_y.values, ev.track_y.values])
            pad = 30
            ax.set_xlim(max(0, all_x.min() - pad), all_x.max() + pad)
            ax.set_ylim(max(0, all_y.min() - pad), all_y.max() + pad)
        ax.invert_yaxis()  # sonar convention
        ax.set_xlabel("xpix (right)")
        ax.set_ylabel("ypix (up)")
        ax.set_title(f"{sname.upper()} — Predicted vs GT Trajectories\n"
                     "(blue = CONFIRMED, green = DETECTOR_ORACLE, dotted = GT, red = error)",
                     fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8, ncol=2)
        fig.tight_layout()
        fig.savefig(OUT / f"trajectory_{sname}.png", dpi=120)
        plt.close(fig)
        print(f"  → {OUT / f'trajectory_{sname}.png'}")


# --------------------------------------------------------------------------
# 5) Per-frame error over time
# --------------------------------------------------------------------------
def plot_error_timeline(sets_data):
    for sname, data in sets_data.items():
        ev = data["eval"].sort_values("frame_id")
        fig, ax = plt.subplots(figsize=(12, 4))
        status_colors = {
            "CONFIRMED":         "#3b82f6",  # blue
            "DETECTOR_ORACLE":   "#10b981",  # green (detector was perfect)
            "TENTATIVE":         "#f59e0b",  # amber
        }
        for t in sorted(ev.track_id.unique()):
            sub = ev[ev.track_id == t]
            for status, color in status_colors.items():
                ss = sub[sub["status"] == status]
                if len(ss):
                    ax.scatter(ss.frame_id, ss.error_px,
                               color=color, s=5, alpha=0.7,
                               label=f"{t} [{status}]" if t == sorted(ev.track_id.unique())[0] else "")
            ax.plot(sub.frame_id, sub.error_px,
                    color="#3b82f6", linewidth=0.5, alpha=0.2)
        ax.axhline(20, color="green", linestyle="--", alpha=0.4, label="20px")
        ax.axhline(50, color="orange", linestyle="--", alpha=0.4, label="50px")
        # Custom legend for statuses
        legend_elements = [
            plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#3b82f6",
                        markersize=8, label="CONFIRMED"),
            plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#10b981",
                        markersize=8, label="DETECTOR_ORACLE (detector was good)"),
            plt.Line2D([0], [0], marker="o", color="w", markerfacecolor="#f59e0b",
                        markersize=8, label="TENTATIVE"),
            plt.Line2D([0], [0], color="green", linestyle="--", label="20px"),
            plt.Line2D([0], [0], color="orange", linestyle="--", label="50px"),
        ]
        ax.set_xlabel("Frame ID")
        ax.set_ylabel("Error (px)")
        ax.set_title(f"{sname.upper()} — Tracking Error vs Frame\n"
                     f"(green = DETECTOR_ORACLE: detector saw correct target but tracker drifted)",
                     fontsize=11, fontweight="bold")
        ax.grid(True, alpha=0.3)
        ax.legend(handles=legend_elements, loc="upper right", fontsize=8, ncol=3)
        fig.tight_layout()
        fig.savefig(OUT / f"timeline_{sname}.png", dpi=120)
        plt.close(fig)
        print(f"  → {OUT / f'timeline_{sname}.png'}")


# --------------------------------------------------------------------------
# 5b) Before/after comparison (fixed vs original)
# --------------------------------------------------------------------------
def plot_improvement_comparison():
    """Before/after comparison bar chart (uses hardcoded BEFORE values from conversation)."""
    import json
    BEFORE = {
        1: {"mean": 15.4,  "median": 8.13, "p90": 36.23,  "max": 102.71},
        2: {"mean": 51.76, "median": 13.73,"p90": 162.51, "max": 200.08},
        3: {"mean": 13.7,  "median": 8.86, "p90": 30.15,  "max": 83.14},
        4: {"mean": 8.69,  "median": 7.06, "p90": 15.7,   "max": 84.22},
    }
    sets_data = load_all()

    fig, axes = plt.subplots(1, 4, figsize=(20, 5))
    metrics = ["mean_error_px", "median_error_px", "p90_error_px", "max_error_px"]
    titles  = ["Mean Error", "Median Error", "P90 Error", "Max Error"]

    for ax, metric, title in zip(axes, metrics, titles):
        key = metric.replace("_error_px", "")
        before_vals = [BEFORE[s][key] for s in [1, 2, 3, 4]]
        after_vals  = [sets_data[f"set_{s}"]["summary"].get(metric, 0) for s in [1, 2, 3, 4]]
        x = np.arange(4)
        ax.bar(x - 0.2, before_vals, width=0.35,
               color="#ef4444", alpha=0.7, label="Before", linewidth=2)
        ax.bar(x + 0.2, after_vals,  width=0.35,
               color="#10b981", alpha=0.9, label="After (fixed)", linewidth=2)
        ax.set_xticks(x)
        ax.set_xticklabels([f"set {s}" for s in [1, 2, 3, 4]], fontsize=10)
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(fontsize=9)
        for i, (b, a) in enumerate(zip(before_vals, after_vals)):
            pct = (a - b) / max(b, 0.01) * 100
            sign = "↓" if a < b else "↑"
            ax.text(i + 0.2, max(a, b) + 1,
                    f"{sign}{abs(pct):.0f}%",
                    ha="center", va="bottom", fontsize=9,
                    color="#10b981" if a < b else "#ef4444", fontweight="bold")

    fig.suptitle("Tracking Error — Before vs After Fix", fontsize=15, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT / "improvement_comparison.png", dpi=120)
    plt.close(fig)
    print(f"  → {OUT / 'improvement_comparison.png'}")


# --------------------------------------------------------------------------
# 6) Sample diagnostic frames with GT overlay
# --------------------------------------------------------------------------
def plot_diagnostic_overlays(sets_data):
    """Pick 4 representative frames per set and overlay GT predictions."""
    for sname, data in sets_data.items():
        ev = data["eval"]
        # pick frames at 25/50/75/100% of timeline
        fids = sorted(ev.frame_id.unique())
        if len(fids) < 4:
            sample = fids
        else:
            q = np.linspace(0, 1, 4)
            sample = [fids[int(qi * (len(fids) - 1))] for qi in q]
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        axes = axes.ravel()
        for ax, fid in zip(axes, sample):
            diag_path = ROOT / f"dfki_{sname}/diagnostics/diag_{sname}_f{fid:04d}.png"
            if not diag_path.exists():
                ax.text(0.5, 0.5, f"(frame {fid} diag missing)",
                        ha="center", va="center")
                ax.axis("off")
                continue
            img = plt.imread(str(diag_path))
            ax.imshow(img)
            ax.set_title(f"frame {fid}", fontsize=10)
            ax.axis("off")
        fig.suptitle(f"{sname.upper()} — Sample Diagnostic Frames",
                     fontsize=14, fontweight="bold")
        fig.tight_layout()
        fig.savefig(OUT / f"diagnostic_samples_{sname}.png", dpi=110)
        plt.close(fig)
        print(f"  → {OUT / f'diagnostic_samples_{sname}.png'}")


# --------------------------------------------------------------------------
# 7) Master dashboard
# --------------------------------------------------------------------------
def plot_dashboard(sets_data):
    """Compose all key info into one dashboard PNG."""
    fig = plt.figure(figsize=(20, 14))
    gs = fig.add_gridspec(3, 4, hspace=0.4, wspace=0.35)

    # (a) Summary table (show DETECTOR_ORACLE and TRACK_LOST counts)
    ax_tab = fig.add_subplot(gs[0, :2])
    ax_tab.axis("off")
    cols = ["Set", "N_eval", "Recall", "Mean", "Median", "RMSE", "p90", "Max",
            "Oracle", "Lost"]
    rows = []
    for s, data in sets_data.items():
        sm = data["summary"]
        ev = data["eval"]
        n_oracle = int((ev["status"] == "DETECTOR_ORACLE").sum())
        # Read total from CSV (includes NaN rows)
        total_all = len(pd.read_csv(ROOT / f"dfki_{s}/gt_evaluation.csv"))
        n_lost = total_all - len(ev) - n_oracle
        rows.append([s.upper(),
                     f"{sm['n_evaluated_frames']}/{sm['n_gt_frames']}",
                     f"{sm['detection_recall']*100:.1f}%",
                     f"{sm['mean_error_px']:.2f}",
                     f"{sm['median_error_px']:.2f}",
                     f"{sm['rmse_px']:.2f}",
                     f"{sm['p90_error_px']:.2f}",
                     f"{sm['max_error_px']:.2f}",
                     str(n_oracle),
                     str(n_lost)])
    table = ax_tab.table(cellText=rows, colLabels=cols, loc="center",
                          cellLoc="center", colColours=["#dbeafe"] * len(cols))
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.1, 2.0)
    ax_tab.set_title("Per-Set GT Evaluation  (detector oracle = GT-informed upper bound)",
                      fontsize=13, fontweight="bold", pad=10)

    # (b) Median bar chart
    ax_med = fig.add_subplot(gs[0, 2:])
    sets = list(sets_data.keys())
    meds = [sets_data[s]["summary"]["median_error_px"] for s in sets]
    rmses = [sets_data[s]["summary"]["rmse_px"] for s in sets]
    recalls = [sets_data[s]["summary"]["detection_recall"] * 100 for s in sets]
    x = np.arange(len(sets))
    ax_med.bar(x - 0.25, meds, width=0.25, color="#3b82f6", label="Median")
    ax_med.bar(x, rmses, width=0.25, color="#f59e0b", label="RMSE")
    ax_med.bar(x + 0.25, recalls, width=0.25, color="#10b981", label="Recall (%)")
    ax_med.set_xticks(x)
    ax_med.set_xticklabels([s.upper() for s in sets])
    ax_med.set_title("Median / RMSE / Recall", fontsize=13, fontweight="bold")
    ax_med.legend()
    ax_med.grid(True, axis="y", alpha=0.3)

    # (c) CDF
    ax_cdf = fig.add_subplot(gs[1, :2])
    for sname, data in sets_data.items():
        ev = data["eval"]
        errs = np.sort(ev.error_px.values)
        cdf = np.arange(1, len(errs) + 1) / len(errs)
        ax_cdf.plot(errs, cdf, label=sname.upper(), linewidth=2)
    ax_cdf.axvline(20, color="green", linestyle="--", alpha=0.4)
    ax_cdf.set_xlim(0, 100)
    ax_cdf.set_xlabel("Error (px)")
    ax_cdf.set_ylabel("CDF")
    ax_cdf.set_title("Error CDF", fontsize=13, fontweight="bold")
    ax_cdf.grid(True, alpha=0.3)
    ax_cdf.legend()

    # (d) Per-track median
    ax_pt = fig.add_subplot(gs[1, 2:])
    tracks_all = []
    meds_all = []
    for s, data in sets_data.items():
        for t, g in data["eval"].groupby("track_id"):
            tracks_all.append(f"{s[-1]}.{t.split('_')[-1]}")
            meds_all.append(g.error_px.median())
    ax_pt.bar(range(len(tracks_all)), meds_all, color="steelblue")
    ax_pt.set_xticks(range(len(tracks_all)))
    ax_pt.set_xticklabels(tracks_all, rotation=45, fontsize=8)
    ax_pt.set_ylabel("Median Error (px)")
    ax_pt.set_title("Per-Track Median Error", fontsize=13, fontweight="bold")
    ax_pt.grid(True, axis="y", alpha=0.3)

    # (e) Error timeline of set_4
    ax_tl = fig.add_subplot(gs[2, :])
    if "set_4" in sets_data:
        ev = sets_data["set_4"]["eval"].sort_values("frame_id")
        for t in sorted(ev.track_id.unique()):
            sub = ev[ev.track_id == t]
            ax_tl.plot(sub.frame_id, sub.error_px, "-", linewidth=1, alpha=0.7, label=t)
        ax_tl.axhline(20, color="green", linestyle="--", alpha=0.4)
        ax_tl.set_xlabel("Frame")
        ax_tl.set_ylabel("Error (px)")
        ax_tl.set_title("set_4 — Error vs Frame", fontsize=13, fontweight="bold")
        ax_tl.legend(loc="upper right", fontsize=8, ncol=4)
        ax_tl.grid(True, alpha=0.3)

    fig.suptitle("DFKI Tracking — Results Dashboard", fontsize=18, fontweight="bold")
    fig.savefig(OUT / "dashboard.png", dpi=120)
    plt.close(fig)
    print(f"  → {OUT / 'dashboard.png'}")


def main():
    print("Loading GT evaluations...")
    sets_data = load_all()
    if not sets_data:
        print("No data found.")
        return

    print("\nGenerating visualizations:")
    plot_improvement_comparison()
    plot_summary(sets_data)
    plot_per_track_box(sets_data)
    plot_cdf(sets_data)
    plot_trajectories(sets_data)
    plot_error_timeline(sets_data)
    plot_diagnostic_overlays(sets_data)
    plot_dashboard(sets_data)
    print(f"\nAll visualizations saved to {OUT}/")


if __name__ == "__main__":
    main()
