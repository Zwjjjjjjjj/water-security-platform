"""
Tracking Accuracy vs Ground Truth — 追踪准确率评估

本模块专门回答"追踪过程准确率有多高"，针对两类数据集各用一套 GT 口径：

=====================================================================
A) DFKI  (像素级 GT)  — set_1..set_4/tracking_sonar_coordinates_{n}.csv
=====================================================================
GT 给出每帧的 (x, y) 像素坐标。按标准 MOT 指标评估：

  * 检测/匹配率 (Detection Rate)
      = 匹配上的 GT 帧数 / GT 总帧数
  * 位置误差 (Position Error)
      逐帧欧氏误差 → median / RMSE / p90 / max
  * 准确率阈值下的命中率 (Accuracy @ threshold)
      误差 ≤ τ 的帧比例，τ = {5, 10, 20, 30} px
      → 这是"准确率"最直观的数字
  * 漏检率 (Miss Rate) = 1 - Detection Rate
  * 轨迹召回 / 覆盖 (Track Recall)
  * 误检 (False Positive) 数量 —— 未匹配到任何 GT 的 A 轨道帧
  * 归一化误差 (Normalized Error) = RMSE / GT 轨迹总路程
      → 消除数据集尺度差异的"相对准确率"

=====================================================================
B) GLF  (事件级 GT)  — config/tanana_203913_profile.json
=====================================================================
GLF 没有逐帧像素 GT，只有 4 个采集事件的
(event_time_utc, label, expected_min/max_range_m, expected_min/max_bearing_deg)。
因此按"事件检出"评估：

  * 事件检出率 (Event Detection Rate)
      = 被正确标注的 GT 事件数 / GT 事件总数
  * 包络命中率 (Envelope Compliance)
      被标注轨道的 range/bearing 是否落在 GT 包络内
  * 距离首现误差 (First-Appearance Error)
      实际首次可见距离 vs GT first_visible_range_m
  * 目标计数误差 (Count Error)
      同一 GT 事件标注出的 track 数 vs GT count

=====================================================================
输出
=====================================================================
  accuracy_vs_gt.json / accuracy_vs_gt.md  （单数据集）
  tracking_accuracy_report.md             （全数据集汇总）
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
# tritech_paths 位于 src/ 下；包内导入与顶层导入两种方式都要支持。
try:  # 包内导入（src.tritech_paths）
    from .tritech_paths import DFKI_ROOT, GLF_PROFILE, LEGACY_E2E_ALL_ROOT
except ImportError:  # 顶层导入（tritech_paths）
    from tritech_paths import DFKI_ROOT, GLF_PROFILE, LEGACY_E2E_ALL_ROOT


# =========================================================================
# A) DFKI — pixel-level GT
# =========================================================================
def evaluate_dfki_vs_gt(
    standardized_csv: Path,
    gt_root: Path,
    set_num: int,
    accuracy_thresholds_px: Optional[List[float]] = None,
    frame_tol_s: float = 0.06,
) -> Dict:
    """
    Compare the tracked PRIMARY track against the pixel-level GT.

    Parameters
    ----------
    standardized_csv : stage 3 output (has x_pix, y_pix, timestamp_utc)
    gt_root : directory containing set_{n}/tracking_sonar_coordinates_{n}.csv
    set_num : 1..4
    accuracy_thresholds_px : error thresholds for the "accuracy @ τ" metric
    frame_tol_s : max timestamp difference for a GT/track frame match

    Returns a dict of accuracy metrics.
    """
    if accuracy_thresholds_px is None:
        accuracy_thresholds_px = [5.0, 10.0, 20.0, 30.0]

    gt_path = Path(gt_root) / f"set_{set_num}" / f"tracking_sonar_coordinates_{set_num}.csv"
    if not gt_path.exists():
        return {"error": f"GT not found: {gt_path}"}
    if not Path(standardized_csv).exists():
        return {"error": f"standardized CSV not found: {standardized_csv}"}

    tr = pd.read_csv(standardized_csv)
    gt = pd.read_csv(gt_path)

    # --- timestamps ---
    tr = tr[tr["track_id"].astype(str).str.upper().str.startswith("PRIMARY")].copy()
    if tr.empty:
        return {"error": "no PRIMARY track in standardized CSV"}
    tr["ts"] = pd.to_datetime(tr["timestamp_utc"], utc=True, errors="coerce")
    tr = tr.dropna(subset=["ts", "x_pix", "y_pix"])

    gt["ts"] = pd.to_datetime(
        pd.to_numeric(gt["image_name"].astype(str).str.replace(
            r"\.jpg$", "", regex=True), errors="coerce"),
        unit="s", utc=True, errors="coerce",
    )
    gt = gt.dropna(subset=["ts", "x", "y"]).sort_values("ts").reset_index(drop=True)

    n_gt = int(len(gt))
    n_track = int(len(tr))

    if n_gt == 0 or n_track == 0:
        return {"error": "empty GT or track", "n_gt": n_gt, "n_track": n_track}

    # --- nearest-timestamp matching (monotonic, no duplicates) ---
    tr_ts = tr["ts"].values
    gt_ts = gt["ts"].values
    tr_idx_by_ts = tr.reset_index(drop=True)

    pairs_t = []
    pairs_i = []
    used = set()
    for gi, t in enumerate(gt_ts):
        d = np.abs((tr_ts - t) / np.timedelta64(1, "s"))
        j = int(np.argmin(d))
        if d[j] <= frame_tol_s and j not in used:
            used.add(j)
            pairs_i.append(gi)
            pairs_t.append(j)

    m = len(pairs_i)
    if m == 0:
        return {"error": "no GT/track timestamp pairs within tolerance",
                "n_gt": n_gt, "n_track": n_track}

    gi = np.array(pairs_i)
    ti = np.array(pairs_t)
    dx = tr_ts[ti] - gt_ts[gi]
    err_x = tr["x_pix"].to_numpy()[ti] - gt["x"].to_numpy()[gi]
    err_y = tr["y_pix"].to_numpy()[ti] - gt["y"].to_numpy()[gi]
    err = np.hypot(err_x, err_y)

    # --- GT trajectory geometry (for normalization) ---
    gx = gt["x"].to_numpy()
    gy = gt["y"].to_numpy()
    seg = np.hypot(np.diff(gx), np.diff(gy))
    path_len = float(seg.sum())
    gspan = float(math.hypot(gx.max() - gx.min(), gy.max() - gy.min()))

    # --- velocity accuracy (on matched frames) ---
    if m > 2:
        dt = np.diff(gt_ts[gi]).astype("timedelta64[ns]").astype(float) / 1e9
        dt[dt <= 0] = np.nan
        vx_true = np.diff(gx[gi]) / dt
        vy_true = np.diff(gy[gi]) / dt
        vx_est = np.diff(tr["x_pix"].to_numpy()[ti]) / dt
        vy_est = np.diff(tr["y_pix"].to_numpy()[ti]) / dt
        ok = np.isfinite(dt) & ~np.isnan(dt)
        if ok.sum() > 1:
            sp_true = np.hypot(vx_true[ok], vy_true[ok])
            sp_est = np.hypot(vx_est[ok], vy_est[ok])
            sp_mae = float(np.mean(np.abs(sp_est - sp_true)))
            sp_rmse = float(np.sqrt(np.mean((sp_est - sp_true) ** 2)))
            ang_err = None
            if sp_true.max() > 1e-6:
                cos = (vx_true[ok] * vx_est[ok] + vy_true[ok] * vy_est[ok]) / np.maximum(
                    sp_true * sp_est, 1e-9)
                ang_err = float(np.degrees(np.mean(np.arccos(np.clip(cos, -1, 1)))))
        else:
            sp_mae = sp_rmse = ang_err = None
    else:
        sp_mae = sp_rmse = ang_err = None

    # --- accuracy @ threshold ---
    acc_at = {f"acc_at_{int(t)}px": round(float((err <= t).mean()), 4)
              for t in accuracy_thresholds_px}

    # --- false positives: track frames with no GT within tol ---
    fp_mask = np.ones(n_track, dtype=bool)
    fp_mask[ti] = False
    n_fp = int(fp_mask.sum())

    # --- IoU-like spatial hit rate using a GT-centred box ---
    out: Dict = {
        "dataset": f"dfki_set_{set_num}",
        "gt_source": str(gt_path),
        "n_gt_frames": n_gt,
        "n_track_frames": n_track,
        "n_matched_frames": m,
        "detection_rate": round(m / n_gt, 4),
        "miss_rate": round(1.0 - m / n_gt, 4),
        "false_positive_frames": n_fp,
        "precision_frames": round(m / max(1, n_track), 4),
        "recall_frames": round(m / n_gt, 4),
        "f1_frames": round(2 * m / max(1, n_track + n_gt), 4),
        "err_median_px": round(float(np.median(err)), 2),
        "err_mean_px": round(float(err.mean()), 2),
        "err_rmse_px": round(float(np.sqrt((err ** 2).mean())), 2),
        "err_p75_px": round(float(np.percentile(err, 75)), 2),
        "err_p90_px": round(float(np.percentile(err, 90)), 2),
        "err_p95_px": round(float(np.percentile(err, 95)), 2),
        "err_max_px": round(float(err.max()), 2),
        "bias_x_px": round(float(err_x.mean()), 2),
        "bias_y_px": round(float(err_y.mean()), 2),
        "gt_path_length_px": round(path_len, 1),
        "gt_diagonal_px": round(gspan, 1),
        "norm_err_vs_path": round(float(np.sqrt((err ** 2).mean()) / max(1.0, path_len)), 5),
        "norm_err_vs_diagonal": round(float(np.sqrt((err ** 2).mean()) / max(1.0, gspan)), 5),
        "speed_mae_px_s": round(sp_mae, 3) if sp_mae is not None else None,
        "speed_rmse_px_s": round(sp_rmse, 3) if sp_rmse is not None else None,
        "heading_err_deg": round(ang_err, 2) if ang_err is not None else None,
    }
    out.update(acc_at)
    return out


def plot_dfki_accuracy(standardized_csv: Path, gt_root: Path, set_num: int,
                       out_png: Path, frame_tol_s: float = 0.06) -> Optional[Path]:
    """Overlay track vs GT + error-vs-time, for visual verification."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    gt_path = Path(gt_root) / f"set_{set_num}" / f"tracking_sonar_coordinates_{set_num}.csv"
    tr = pd.read_csv(standardized_csv)
    tr = tr[tr["track_id"].astype(str).str.upper().str.startswith("PRIMARY")].copy()
    gt = pd.read_csv(gt_path)
    if tr.empty or gt.empty:
        return None
    tr["ts"] = pd.to_datetime(tr["timestamp_utc"], utc=True, errors="coerce")
    gt["ts"] = pd.to_datetime(
        pd.to_numeric(gt["image_name"].astype(str).str.replace(r"\.jpg$", "", regex=True),
                      errors="coerce"), unit="s", utc=True, errors="coerce")
    tr = tr.dropna(subset=["ts", "x_pix", "y_pix"]).sort_values("ts").reset_index(drop=True)
    gt = gt.dropna(subset=["ts", "x", "y"]).sort_values("ts").reset_index(drop=True)

    tr_ts = tr["ts"].values
    gt_ts = gt["ts"].values
    gi_l, ti_l, err_l = [], [], []
    used = set()
    for gi, t in enumerate(gt_ts):
        d = np.abs((tr_ts - t) / np.timedelta64(1, "s"))
        j = int(np.argmin(d))
        if d[j] <= frame_tol_s and j not in used:
            used.add(j)
            gi_l.append(gi); ti_l.append(j)
            err_l.append(float(np.hypot(tr["x_pix"].to_numpy()[j] - gt["x"].to_numpy()[gi],
                                        tr["y_pix"].to_numpy()[j] - gt["y"].to_numpy()[gi])))
    if not err_l:
        return None

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    ax = axes[0]
    ax.plot(gt["x"], gt["y"], "-", color="#111", lw=2.2, label="GT (real diver)", alpha=0.85)
    ax.plot(tr["x_pix"], tr["y_pix"], "-", color="#e11d48", lw=1.6,
            label="Tracker PRIMARY", alpha=0.9)
    ax.scatter(gt["x"].to_numpy()[gi_l], gt["y"].to_numpy()[gi_l],
               s=34, facecolors="none", edgecolors="#2563eb", linewidths=1.4,
               label=f"matched frames (n={len(err_l)})")
    ax.set_xlabel("x (px)"); ax.set_ylabel("y (px)")
    ax.set_title(f"DFKI set {set_num}: track vs GT")
    ax.set_aspect("equal", adjustable="datalim")
    ax.invert_yaxis(); ax.grid(alpha=0.3); ax.legend(fontsize=8)

    ax = axes[1]
    t0 = gt_ts[gi_l]
    tsec = (t0 - t0[0]).astype("timedelta64[ns]").astype(float) / 1e9
    ax.plot(tsec, err_l, "-o", ms=3, lw=1.2, color="#7c3aed")
    med = float(np.median(err_l))
    for thr, col in ((5, "#16a34a"), (10, "#0891b2"), (20, "#ea580c"), (30, "#dc2626")):
        ax.axhline(thr, ls="--", lw=0.9, color=col, alpha=0.6)
    ax.axhline(med, ls="-", lw=1.4, color="black", alpha=0.7)
    ax.set_xlabel("time since first match (s)")
    ax.set_ylabel("position error (px)")
    ax.set_title(f"error vs GT — median {med:.1f} px, RMSE {np.sqrt(np.mean(np.square(err_l))):.1f} px")
    ax.grid(alpha=0.3)

    fig.tight_layout()
    Path(out_png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=130)
    plt.close(fig)
    return Path(out_png)


# =========================================================================
# B) GLF — event-level GT
# =========================================================================
def evaluate_glf_vs_gt(
    standardized_csv: Path,
    profile_json: Path,
) -> Dict:
    """
    Compare tracked target labels against the collection-log event GT.

    The GLF GT is event-level: for each event we know
      - when it happened (event_time_utc)
      - how many targets (count)
      - the expected range / bearing envelope
      - the expected first-visible range
    A track is a correct detection if it carries the event's label AND its
    measurement stays inside the GT envelope.
    """
    tr = pd.read_csv(standardized_csv)
    if tr.empty:
        return {"error": f"empty standardized CSV: {standardized_csv}"}
    if "target_label" not in tr.columns:
        return {"error": "standardized CSV has no target_label column"}

    profile = json.loads(Path(profile_json).read_text())
    events = profile.get("events", [])
    if not events:
        return {"error": "profile has no events"}

    tr["ts"] = pd.to_datetime(tr["timestamp_utc"], utc=True, errors="coerce")

    # Normalize GT labels to the naming convention used in target_label.
    # Both sides go through the same normaliser, so a GT label of
    # "5/8in_plywood" matches a track label of "5/8in_plywood" or
    # "5_8IN_PLYWOOD" etc.
    def _norm(s: str) -> str:
        s = str(s).strip().lower()
        s = s.replace("/", "_").replace("-", "_").replace(" ", "_")
        s = s.replace(".", "_")
        while "__" in s:
            s = s.replace("__", "_")
        return s.strip("_")

    # Normalize the track labels (drop the "_1" / "_2" multiplicity suffix)
    tr["_norm_label"] = (
        tr["target_label"].astype(str).str.lower()
        .str.replace(r"_\d+$", "", regex=True)
        .map(_norm)
    )

    gt_base = {_norm(ev["label"]) for ev in events}

    per_event = []
    detected = 0
    for ev in events:
        label = _norm(ev["label"])
        count = int(ev.get("count", 1))
        et = pd.to_datetime(ev["event_time_utc"], utc=True)
        max_delay = float(ev.get("max_start_delay_s", 30))

        # Tracks labelled with this event (multiplicity suffix already stripped)
        sub = tr[tr["_norm_label"] == label].copy()

        rec = {
            "gt_label": ev["label"],
            "gt_time_utc": ev["event_time_utc"],
            "gt_count": count,
            "gt_first_visible_range_m": ev.get("first_visible_range_m"),
            "gt_range_envelope_m": [ev.get("expected_min_range_m"), ev.get("expected_max_range_m")],
            "gt_bearing_envelope_deg": [ev.get("expected_min_bearing_deg"),
                                        ev.get("expected_max_bearing_deg")],
            "n_tracks_detected": int(sub["track_id"].nunique()) if not sub.empty else 0,
            "n_frames": int(len(sub)),
        }

        if not sub.empty:
            r_lo = ev.get("expected_min_range_m", -1e9)
            r_hi = ev.get("expected_max_range_m", 1e9)
            b_lo = ev.get("expected_min_bearing_deg", -1e9)
            b_hi = ev.get("expected_max_bearing_deg", 1e9)
            m_r = (sub["range_m"] >= r_lo) & (sub["range_m"] <= r_hi)
            m_b = (sub["bearing_deg"] >= b_lo) & (sub["bearing_deg"] <= b_hi)
            m_both = m_r & m_b
            rec["n_frames_in_range"] = int(m_r.sum())
            rec["n_frames_in_bearing"] = int(m_b.sum())
            rec["n_frames_in_both"] = int(m_both.sum())
            rec["envelope_compliance"] = round(rec["n_frames_in_both"] / max(1, len(sub)), 4)
            rec["observed_range_m"] = [round(float(sub["range_m"].min()), 2),
                                       round(float(sub["range_m"].max()), 2)]
            rec["observed_bearing_deg"] = [round(float(sub["bearing_deg"].min()), 2),
                                           round(float(sub["bearing_deg"].max()), 2)]
            # time delay from GT event to first detected frame
            if sub["ts"].notna().any():
                first_ts = sub["ts"].min()
                delay = (first_ts - et).total_seconds()
                rec["first_detect_delay_s"] = round(float(delay), 2)
                rec["on_time"] = bool(-5.0 <= delay <= max_delay)
            # first-appearance range error
            gfv = ev.get("first_visible_range_m")
            if gfv is not None:
                obs_first = float(sub.sort_values("ts")["range_m"].iloc[0])
                rec["first_appearance_range_m"] = round(obs_first, 2)
                rec["first_appearance_error_m"] = round(obs_first - float(gfv), 2)
            # per-track count list
            rec["track_ids"] = sorted(sub["track_id"].unique().tolist())
            hit = rec["n_tracks_detected"] >= 1 and rec["n_frames_in_both"] > 0
            rec["event_detected"] = bool(hit)
            rec["count_correct"] = bool(rec["n_tracks_detected"] == count)
        else:
            rec.update({"n_frames_in_range": 0, "n_frames_in_bearing": 0,
                        "n_frames_in_both": 0, "envelope_compliance": 0.0,
                        "event_detected": False, "count_correct": False,
                        "track_ids": []})
        detected += 1 if rec["event_detected"] else 0
        per_event.append(rec)

    n_ev = len(events)
    counts_ok = sum(1 for r in per_event if r["count_correct"])

    # Spurious (non-GT) labelled tracks: tracks that were given a *target*
    # label but whose label doesn't correspond to any GT event.
    spurious_tracks = sorted(
        set(tr.loc[~tr["_norm_label"].isin(gt_base), "track_id"].unique().tolist())
    )

    return {
        "dataset": "glf",
        "gt_source": str(profile_json),
        "gt_basis": profile.get("label_basis", ""),
        "n_gt_events": n_ev,
        "n_events_detected": detected,
        "event_detection_rate": round(detected / max(1, n_ev), 4),
        "n_events_count_correct": counts_ok,
        "event_count_accuracy": round(counts_ok / max(1, n_ev), 4),
        "n_spurious_labelled_tracks": len(spurious_tracks),
        "spurious_track_ids": spurious_tracks,
        "mean_envelope_compliance": round(
            float(np.mean([r["envelope_compliance"] for r in per_event])), 4),
        "per_event": per_event,
    }


# =========================================================================
# Reporting
# =========================================================================
def _fmt(v, nd=2, dash="-"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return dash
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def write_accuracy_report(results: Dict[str, Dict], out_md: Path, out_json: Path) -> Path:
    """Write a combined tracking-accuracy report."""
    lines = ["# Tracking Accuracy vs Ground Truth", "",
             "GT sources:",
             "- DFKI — `Diver Tracking - Sonar/set_{1..4}/tracking_sonar_coordinates_{n}.csv` (per-frame pixel GT)",
             "- GLF  — `config/tanana_203913_profile.json` (event-level GT from the field collection log)", ""]

    # ---- DFKI ----
    dfki_keys = [k for k in results if k.startswith("dfki")]
    if dfki_keys:
        lines += ["## A. DFKI — per-frame pixel GT", ""]
        lines += ["| Dataset | detect rate | miss rate | RMSE (px) | median (px) | p90 (px) | ≤5px | ≤10px | ≤20px | ≤30px | speed RMSE (px/s) | norm err / GT path |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k in dfki_keys:
            r = results[k]
            if "error" in r:
                lines.append(f"| `{k}` | error: {r['error']} | | | | | | | | | | |")
                continue
            lines.append(
                f"| `{k}` | **{r['detection_rate']:.1%}** | {r['miss_rate']:.1%} | "
                f"**{r['err_rmse_px']}** | {r['err_median_px']} | {r['err_p90_px']} | "
                f"{r.get('acc_at_5px', 0):.1%} | {r.get('acc_at_10px', 0):.1%} | "
                f"{r.get('acc_at_20px', 0):.1%} | {r.get('acc_at_30px', 0):.1%} | "
                f"{_fmt(r.get('speed_rmse_px_s'), 2)} | {r.get('norm_err_vs_path', 0):.4f} |")

        # aggregate
        ok = [results[k] for k in dfki_keys if "error" not in results[k]]
        if ok:
            lines += ["", "### Aggregate over DFKI sets", ""]
            mean_det = float(np.mean([r["detection_rate"] for r in ok]))
            mean_rmse = float(np.mean([r["err_rmse_px"] for r in ok]))
            mean_med = float(np.mean([r["err_median_px"] for r in ok]))
            mean_a10 = float(np.mean([r.get("acc_at_10px", 0) for r in ok]))
            mean_a20 = float(np.mean([r.get("acc_at_20px", 0) for r in ok]))
            mean_norm = float(np.mean([r.get("norm_err_vs_path", 0) for r in ok]))
            lines += [f"- mean detection rate: **{mean_det:.1%}**",
                      f"- mean RMSE: **{mean_rmse:.2f} px**  (median-of-errors {mean_med:.2f} px)",
                      f"- mean accuracy @10 px: **{mean_a10:.1%}**",
                      f"- mean accuracy @20 px: **{mean_a20:.1%}**",
                      f"- mean normalized error (RMSE / GT path length): **{mean_norm:.4f}**"]

        # per-set detail
        lines += ["", "### Per-set detail", ""]
        for k in dfki_keys:
            r = results[k]
            if "error" in r:
                continue
            lines += [f"**`{k}`** — matched {r['n_matched_frames']}/{r['n_gt_frames']} GT frames "
                      f"({r['detection_rate']:.1%}), track has {r['n_track_frames']} frames "
                      f"(precision {r['precision_frames']:.1%}, F1 {r['f1_frames']:.3f})",
                      f"- bias (Δx, Δy) = ({r['bias_x_px']:+.2f}, {r['bias_y_px']:+.2f}) px "
                      f"(near-zero → no systematic bias)",
                      f"- GT path length {r['gt_path_length_px']} px, GT diagonal {r['gt_diagonal_px']} px",
                      f"- error spread: p50 {r['err_median_px']}, p75 {r['err_p75_px']}, "
                      f"p90 {r['err_p90_px']}, p95 {r['err_p95_px']}, max {r['err_max_px']} px",
                      ""]

    # ---- GLF ----
    glf = results.get("glf")
    if glf and "error" not in glf:
        lines += ["", "## B. GLF — event-level GT", ""]
        lines += [f"- **event detection rate: {glf['event_detection_rate']:.1%}** "
                  f"({glf['n_events_detected']}/{glf['n_gt_events']} events)",
                  f"- **event count accuracy: {glf['event_count_accuracy']:.1%}** "
                  f"({glf['n_events_count_correct']}/{glf['n_gt_events']} events with the right number of tracks)",
                  f"- mean envelope compliance: {glf['mean_envelope_compliance']:.1%} "
                  f"(fraction of frames whose range & bearing are inside the GT envelope)",
                  f"- spurious labelled tracks: {glf['n_spurious_labelled_tracks']} "
                  f"{glf['spurious_track_ids'] or ''}",
                  "", "| GT event | GT count | tracks detected | count OK | frames | in range+bearing | compliance | first-detect delay (s) | first-range obs/GT (m) | err (m) |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for ev in glf["per_event"]:
            lines.append(
                f"| `{ev['gt_label']}` | {ev['gt_count']} | {ev['n_tracks_detected']} | "
                f"{'✅' if ev['count_correct'] else '❌'} | {ev['n_frames']} | "
                f"{ev['n_frames_in_both']} | {ev['envelope_compliance']:.0%} | "
                f"{_fmt(ev.get('first_detect_delay_s'), 1)} | "
                f"{_fmt(ev.get('first_appearance_range_m'), 1)} / {_fmt(ev.get('gt_first_visible_range_m'), 1)} | "
                f"{_fmt(ev.get('first_appearance_error_m'), 2)} |")

    lines += ["", "## How to read these numbers", "",
              "- **detection rate** = matched GT frames / all GT frames — the tracker simply did not"
              " produce output for the rest (漏检).",
              "- **RMSE / median / p90** = position error in pixels against the real diver.",
              "- **acc@τ** = fraction of matched frames within τ px — this is the headline"
              " \"准确率\": how often the tracker was essentially on top of the diver.",
              "- **norm err / GT path** = RMSE ÷ total GT path length — a scale-free measure, so"
              " sets with a longer trajectory are not unfairly favoured.",
              "- **speed RMSE** = velocity error (px/s) on consecutive matched frames.",
              "- GLF has no per-frame GT, so it is scored on **event detection + range/bearing"
              " envelope compliance** instead of pixel RMSE.",
              "",
              "> Note: DFKI GT is the real diver; the extra `CANDIDATE_*` tracks are platform"
              " clutter and are intentionally excluded from this comparison.", ""]

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines))
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(results, indent=2, default=str))
    return out_md


# =========================================================================
# CLI
# =========================================================================
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Tracking accuracy vs GT")
    ap.add_argument("--e2e-root", type=Path, default=LEGACY_E2E_ALL_ROOT)
    ap.add_argument("--gt-root", type=Path, default=DFKI_ROOT)
    ap.add_argument("--glf-profile", type=Path,
                    default=GLF_PROFILE)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--plots", action="store_true", help="also render overlay plots")
    args = ap.parse_args()

    out_dir = args.out_dir or args.e2e_root
    results: Dict[str, Dict] = {}

    for s in (1, 2, 3, 4):
        ds = f"dfki_set_{s}"
        std = args.e2e_root / ds / "stage_3_standardized" / f"{ds}_standardized.csv"
        r = evaluate_dfki_vs_gt(std, args.gt_root, s)
        results[ds] = r
        if "error" not in r:
            print(f"{ds}: det={r['detection_rate']:.1%} rmse={r['err_rmse_px']}px "
                  f"med={r['err_median_px']}px acc@10={r.get('acc_at_10px'):.1%}")
        else:
            print(f"{ds}: ERROR {r['error']}")
        if args.plots:
            p = plot_dfki_accuracy(
                std, args.gt_root, s,
                out_dir / "tracking_accuracy" / "plots" / f"accuracy_set_{s}.png")
            if p:
                print(f"   plot -> {p}")

    glf_std = args.e2e_root / "glf" / "stage_3_standardized" / "glf_standardized.csv"
    r = evaluate_glf_vs_gt(glf_std, args.glf_profile)
    results["glf"] = r
    if "error" not in r:
        print(f"glf: events detected {r['n_events_detected']}/{r['n_gt_events']} "
              f"({r['event_detection_rate']:.1%})  count-acc={r['event_count_accuracy']:.1%}")
    else:
        print(f"glf: ERROR {r['error']}")

    md = write_accuracy_report(
        results, out_dir / "tracking_accuracy" / "tracking_accuracy_report.md",
        out_dir / "tracking_accuracy" / "tracking_accuracy.json")
    print(f"\nreport -> {md}")


if __name__ == "__main__":
    main()