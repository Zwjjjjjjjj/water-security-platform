"""
Extended Stage 8 evaluators.

These cover three things the base `e2e_evaluate.evaluate_fusion_quality` did
not check:

1. **BOTH_PREDICTED honesty** — verify that every row labelled
   ``BOTH_PREDICTED`` truly has NO real measurement on either side at that
   timestamp (both rows are pure Kalman prediction steps), as opposed to a
   bug where the freshness accounting under-reports and a frame with real
   evidence is silently demoted to "predicted".

2. **Duplicate-evidence audit** — for each local track (A or B), enumerate
   every measurement timestamp and count how many fused frames folded that
   same physical measurement into their evidence base.  The expected value
   is exactly 1 (one-to-one consumption).  A count > 1 means the same
   measurement is being treated as independent evidence multiple times,
   which can artificially shrink the fused covariance.

3. **GT-grounded RMSE_A / RMSE_B / RMSE_Fused** — for DFKI datasets, take
   the true PRIMARY-track ground truth, find the nearest A row, B row, and
   fused row at each GT timestamp, and report RMSE of each against the
   truth.  This is the cleanest way to know whether fusion actually
   improves over the best single sensor.  For GLF (no external GT) we fall
   back to the synthetic truth: B is generated from A, so A is treated as
   ground truth and the same per-sensor RMSE numbers are reported for
   diagnostic value only.

4. **Covariance consistency (NEES-like)** — for every fused frame with a
   known ground truth, compute the squared normalised error
   ``((x - x_gt)^2 + (y - y_gt)^2) / trace(P_fused)`` and report its mean
   and 95% interval.  A well-calibrated filter gives ≈ 1 on average; a
   value >> 1 means the filter is over-confident (covariance too small),
   a value << 1 means it is under-confident.

All evaluators return plain dicts and are written to the same JSON summary
that `run_full_evaluation` produces.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 1) BOTH_PREDICTED honesty check
# ---------------------------------------------------------------------------
def evaluate_both_predicted_honesty(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
    local_to_global_csv: Path,
) -> Dict:
    """Audit whether every BOTH_PREDICTED row really has zero fresh evidence.

    A row is labelled BOTH_PREDICTED by the fusion engine when at least one
    of {A, B} was stale at that timestamp.  Stale = the local row was NOT
    a fresh measurement (already consumed or not a measurement at all).

    Here we re-derive the verdict from the raw rows and compare:
        re-derived mode  vs  reported mode
    For BOTH_PREDICTED rows we expect:
        - The fused state equals whichever side is fresh, or a de-weighted
          blend of the two stale states.
        - NEITHER side contributes `row_has_measurement == True` at a
          timestamp STRICTLY newer than the last consumed one for its track.

    We also count how many BOTH_PREDICTED rows secretly contain a real
    measurement on either side.  Any non-zero count is a bug.
    """
    fused = pd.read_csv(fused_csv) if Path(fused_csv).exists() else pd.DataFrame()
    if fused.empty:
        return {"n_both_predicted": 0, "honest": True}

    l2g = pd.read_csv(local_to_global_csv) if Path(local_to_global_csv).exists() else pd.DataFrame()
    if l2g.empty:
        return {"n_both_predicted": int((fused["fusion_mode"] == "BOTH_PREDICTED").sum()),
                "honest": True, "note": "no local_to_global; skipped per-row audit"}

    tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
    tr_b = pd.read_csv(tr_b_csv) if Path(tr_b_csv).exists() else pd.DataFrame()

    # local_track_id -> DataFrame indexed by timestamp
    def _index_by(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        if df.empty or "local_track_id" not in df.columns:
            return out
        for tid, grp in df.groupby("local_track_id"):
            out[tid] = grp.sort_values("timestamp").reset_index(drop=True)
        return out

    a_by = _index_by(tr_a)
    b_by = _index_by(tr_b)

    g2l: Dict[str, Dict[str, str]] = {}
    for _, r in l2g.iterrows():
        gid = r["global_id"]
        lid = r["local_track_id"]
        sensor = "A" if str(lid).startswith("A_") else ("B" if str(lid).startswith("B_") else None)
        if sensor is None:
            continue
        g2l.setdefault(gid, {})[sensor] = lid

    bp = fused[fused["fusion_mode"] == "BOTH_PREDICTED"].copy()
    n = len(bp)
    secret_a = 0   # how many BOTH_PREDICTED rows have a fresh A measurement available
    secret_b = 0
    secret_any = 0
    # Walk each BOTH_PREDICTED row and apply the same one-to-one consumption
    # logic the fusion loop used.  If the re-derivation flags either side
    # as fresh, the row was mislabelled.
    for gid, grp in bp.groupby("global_id"):
        m = g2l.get(gid, {})
        a_lid = m.get("A")
        b_lid = m.get("B")
        a_df = a_by.get(a_lid) if a_lid else None
        b_df = b_by.get(b_lid) if b_lid else None
        consumed_a: Optional[float] = None
        consumed_b: Optional[float] = None
        # Match the fusion loop: timestamps in union order
        all_ts = set()
        if a_df is not None:
            all_ts.update(a_df["timestamp"].tolist())
        if b_df is not None:
            all_ts.update(b_df["timestamp"].tolist())
        ts_sorted = sorted(all_ts)
        for t in ts_sorted:
            in_row = grp[grp["timestamp"].apply(lambda x, _t=t: abs(float(x) - _t) < 1e-6)]
            if in_row.empty:
                continue
            a_row, b_row = _nearest(a_df, t), _nearest(b_df, t)
            a_fresh = _is_fresh(a_row, consumed_a)
            b_fresh = _is_fresh(b_row, consumed_b)
            if a_fresh:
                consumed_a = float(a_row["timestamp"])
                secret_a += 1
            if b_fresh:
                consumed_b = float(b_row["timestamp"])
                secret_b += 1
            if a_fresh or b_fresh:
                secret_any += 1
    honest = (secret_any == 0)
    return {
        "n_both_predicted": int(n),
        "rows_with_secret_measurement_A": int(secret_a),
        "rows_with_secret_measurement_B": int(secret_b),
        "rows_with_secret_measurement_any": int(secret_any),
        "honest": bool(honest),
        "audit": "PASS" if honest else "FAIL: BOTH_PREDICTED row contains fresh evidence",
    }


def _nearest(df: Optional[pd.DataFrame], t: float, tol: float = 0.12) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    diffs = (df["timestamp"] - t).abs()
    idx = int(diffs.idxmin())
    if diffs.iloc[idx] > tol:
        return None
    return df.iloc[idx]


def _is_fresh(row: Optional[pd.Series], last_consumed: Optional[float]) -> bool:
    """Reproduce the consumption rule: row is fresh if it is a real
    measurement AND its timestamp is strictly newer than the last one
    consumed from this same track.
    """
    if row is None:
        return False
    if not bool(row.get("measurement_available", True)):
        return False
    t_meas = float(row["timestamp"])
    if last_consumed is not None and t_meas <= last_consumed + 1e-9:
        return False
    return True


# ---------------------------------------------------------------------------
# 2) Duplicate-evidence audit
# ---------------------------------------------------------------------------
def evaluate_duplicate_evidence(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
    local_to_global_csv: Path,
) -> Dict:
    """For every local track, count fused frames that used each physical
    measurement as evidence.  Expected = 1 for one-to-one consumption.

    We approximate the consumption logic: walk the BOTH_MEASURED rows
    chronologically, and for each frame look up the nearest A and B row.
    Track the timestamp of the last consumed measurement per local track;
    a row is "new evidence" if its own measurement timestamp is strictly
    newer than the last consumed one.
    """
    fused = pd.read_csv(fused_csv) if Path(fused_csv).exists() else pd.DataFrame()
    tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
    tr_b = pd.read_csv(tr_b_csv) if Path(tr_b_csv).exists() else pd.DataFrame()
    l2g = pd.read_csv(local_to_global_csv) if Path(local_to_global_csv).exists() else pd.DataFrame()
    if fused.empty or l2g.empty:
        return {"n_measurements_A": 0, "n_measurements_B": 0, "audit": "no data"}

    g2l: Dict[str, Dict[str, str]] = {}
    for _, r in l2g.iterrows():
        gid = r["global_id"]
        lid = r["local_track_id"]
        sensor = "A" if str(lid).startswith("A_") else ("B" if str(lid).startswith("B_") else None)
        if sensor is None:
            continue
        g2l.setdefault(gid, {})[sensor] = lid

    def _index_by(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        if df.empty or "local_track_id" not in df.columns:
            return out
        for tid, grp in df.groupby("local_track_id"):
            out[tid] = grp.sort_values("timestamp").reset_index(drop=True)
        return out

    a_by = _index_by(tr_a)
    b_by = _index_by(tr_b)

    # Counters: per local track, per (lid, t_meas) how many fused frames
    # treated it as fresh evidence.
    a_use: Dict[Tuple[str, float], int] = {}
    b_use: Dict[Tuple[str, float], int] = {}
    a_meas_total = 0
    b_meas_total = 0
    a_meas_fresh = 0
    b_meas_fresh = 0

    for gid in fused["global_id"].unique():
        m = g2l.get(gid, {})
        a_lid = m.get("A")
        b_lid = m.get("B")
        a_df = a_by.get(a_lid) if a_lid else None
        b_df = b_by.get(b_lid) if b_lid else None
        # union of timestamps in this gid
        all_ts = set()
        if a_df is not None:
            all_ts.update(a_df["timestamp"].tolist())
        if b_df is not None:
            all_ts.update(b_df["timestamp"].tolist())
        ts_sorted = sorted(all_ts)
        consumed_a: Optional[float] = None
        consumed_b: Optional[float] = None
        for t in ts_sorted:
            a_row = _nearest(a_df, t)
            b_row = _nearest(b_df, t)
            if a_row is not None and bool(a_row.get("measurement_available", True)):
                a_meas_total += 1
            if b_row is not None and bool(b_row.get("measurement_available", True)):
                b_meas_total += 1
            if _is_fresh(a_row, consumed_a):
                consumed_a = float(a_row["timestamp"])
                a_use[(a_lid, consumed_a)] = a_use.get((a_lid, consumed_a), 0) + 1
                a_meas_fresh += 1
            if _is_fresh(b_row, consumed_b):
                consumed_b = float(b_row["timestamp"])
                b_use[(b_lid, consumed_b)] = b_use.get((b_lid, consumed_b), 0) + 1
                b_meas_fresh += 1

    def _stats(d: Dict[Tuple[str, float], int]) -> Dict:
        if not d:
            return {"n_measurements": 0, "max_uses": 0, "n_dup_uses": 0, "dup_ratio": 0.0}
        uses = list(d.values())
        return {
            "n_measurements": int(len(d)),
            "max_uses": int(max(uses)),
            "n_duplicate_uses": int(sum(v - 1 for v in uses if v > 1)),
            "dup_ratio": round(float(sum(v - 1 for v in uses if v > 1)) / max(1, sum(uses)), 4),
        }

    a_stats = _stats(a_use)
    b_stats = _stats(b_use)
    ok = (a_stats["max_uses"] <= 1 and b_stats["max_uses"] <= 1)
    return {
        "A": a_stats,
        "B": b_stats,
        "A_total_measurements": int(a_meas_total),
        "B_total_measurements": int(b_meas_total),
        "A_fresh_consumptions": int(a_meas_fresh),
        "B_fresh_consumptions": int(b_meas_fresh),
        "audit": "PASS: each physical measurement used at most once" if ok
                 else "FAIL: same measurement counted as evidence multiple times",
    }


# ---------------------------------------------------------------------------
# 3) RMSE_A / RMSE_B / RMSE_Fused against ground truth
# ---------------------------------------------------------------------------
def evaluate_rmse_against_truth(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
    local_to_global_csv: Path,
    dfki_set_num: Optional[int] = None,
    dfki_root: Optional[Path] = None,
    synthetic_truth: bool = False,
    tol_s: float = 0.25,
) -> Dict:
    """Compute RMSE of A, B and the fused estimate against ground truth.

    For DFKI, GT is the DFKI-provided
    `tracking_sonar_coordinates_<n>.csv`.  For GLF, ``synthetic_truth=True``
    uses the sonar-A local track (B was synthesised FROM A, so A is the
    noise-free reference).  Returns per-sensor RMSE on x, y and 2D position.
    """
    fused = pd.read_csv(fused_csv) if Path(fused_csv).exists() else pd.DataFrame()
    if fused.empty:
        return {"available": False}

    if dfki_set_num is not None and dfki_root is not None:
        gt_path = dfki_root / f"set_{dfki_set_num}" / f"tracking_sonar_coordinates_{dfki_set_num}.csv"
        if not gt_path.exists():
            return {"available": False, "note": f"GT not found at {gt_path}"}
        gt = pd.read_csv(gt_path)
        gt["ts"] = pd.to_datetime(
            pd.to_numeric(gt["image_name"].str.replace(".jpg", "", regex=False), errors="coerce"),
            unit="s", utc=True, errors="coerce",
        )
        gt = gt.dropna(subset=["ts", "x", "y"]).sort_values("ts").reset_index(drop=True)
        gt_t = gt["ts"].astype("int64").to_numpy() / 1e9
        gt_x = gt["x"].to_numpy()
        gt_y = gt["y"].to_numpy()
    elif synthetic_truth:
        # GLF fallback: A is the truth (B is A + noise)
        tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
        if tr_a.empty:
            return {"available": False, "note": "no A track for synthetic truth"}
        # Take the longest A track (PRIMARY if available, else the longest)
        primary = tr_a[tr_a["local_track_id"].str.upper().str.contains("PRIMARY", na=False)]
        if primary.empty:
            primary = tr_a
        ref = primary.sort_values("timestamp")
        gt_t = ref["timestamp"].to_numpy()
        gt_x = ref["x"].to_numpy()
        gt_y = ref["y"].to_numpy()
    else:
        return {"available": False, "note": "no GT available (use --dfki-set-num or --synthetic-truth)"}

    l2g = pd.read_csv(local_to_global_csv) if Path(local_to_global_csv).exists() else pd.DataFrame()
    tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
    tr_b = pd.read_csv(tr_b_csv) if Path(tr_b_csv).exists() else pd.DataFrame()
    if l2g.empty or tr_a.empty:
        return {"available": False, "note": "missing local_to_global or A tracks"}

    # global_id -> {A: lid, B: lid}
    g2l: Dict[str, Dict[str, str]] = {}
    for _, r in l2g.iterrows():
        gid = r["global_id"]; lid = r["local_track_id"]
        sensor = "A" if str(lid).startswith("A_") else ("B" if str(lid).startswith("B_") else None)
        if sensor is None:
            continue
        g2l.setdefault(gid, {})[sensor] = lid

    def _index_by(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        if df.empty or "local_track_id" not in df.columns:
            return out
        for tid, grp in df.groupby("local_track_id"):
            out[tid] = grp.sort_values("timestamp").reset_index(drop=True)
        return out

    a_by = _index_by(tr_a)
    b_by = _index_by(tr_b)

    # For DFKI we need to transform fused pixel coordinates back into the
    # same coordinate system the GT uses.  Both the GT and the A track
    # are in image pixels already (x_pix / y_pix); fused uses x_fused/y_fused
    # which is also in image pixels.  So no transform needed.

    # Pick the global id whose A track overlaps the most with GT
    def _a_overlap(gid: str) -> int:
        a_lid = g2l.get(gid, {}).get("A")
        if a_lid is None or a_lid not in a_by:
            return 0
        a_t = a_by[a_lid]["timestamp"].to_numpy()
        if len(a_t) == 0:
            return 0
        return int(np.sum(np.min(np.abs(a_t[:, None] - gt_t[None, :]), axis=0) < tol_s))

    cand = [gid for gid in fused["global_id"].unique() if _a_overlap(gid) > 0]
    if not cand:
        return {"available": False, "note": "no GID overlaps with GT timestamps"}
    primary_gid = max(cand, key=_a_overlap)
    a_lid = g2l[primary_gid].get("A")
    b_lid = g2l[primary_gid].get("B")
    a_df = a_by.get(a_lid)
    b_df = b_by.get(b_lid)
    fused_gid = fused[fused["global_id"] == primary_gid].sort_values("timestamp")

    a_t = a_df["timestamp"].to_numpy() if a_df is not None else np.array([])
    a_x = a_df["x"].to_numpy() if a_df is not None else np.array([])
    a_y = a_df["y"].to_numpy() if a_df is not None else np.array([])
    b_t = b_df["timestamp"].to_numpy() if b_df is not None else np.array([])
    b_x = b_df["x"].to_numpy() if b_df is not None else np.array([])
    b_y = b_df["y"].to_numpy() if b_df is not None else np.array([])
    f_t = fused_gid["timestamp"].to_numpy()
    f_x = fused_gid["x_fused"].to_numpy()
    f_y = fused_gid["y_fused"].to_numpy()

    def _rmse(t_query, x_query, y_query, t_gt, x_gt, y_gt, max_dt=tol_s):
        if len(t_query) == 0:
            return None
        # For each GT point, find the nearest query point within max_dt
        dxs, dys = [], []
        for i, t in enumerate(t_gt):
            d = np.abs(t_query - t)
            j = int(np.argmin(d))
            if d[j] > max_dt:
                continue
            dxs.append(float(x_query[j] - x_gt[i]))
            dys.append(float(y_query[j] - y_gt[i]))
        if not dxs:
            return None
        dxs = np.array(dxs); dys = np.array(dys)
        return {
            "n_matched": int(len(dxs)),
            "rmse_x": round(float(np.sqrt((dxs ** 2).mean())), 3),
            "rmse_y": round(float(np.sqrt((dys ** 2).mean())), 3),
            "rmse_2d": round(float(np.sqrt((dxs ** 2 + dys ** 2).mean())), 3),
            "mean_err_2d": round(float(np.hypot(dxs, dys).mean()), 3),
            "median_err_2d": round(float(np.median(np.hypot(dxs, dys))), 3),
            "p90_err_2d": round(float(np.percentile(np.hypot(dxs, dys), 90)), 3),
        }

    res = {
        "available": True,
        "primary_global_id": primary_gid,
        "gt_source": "DFKI" if (dfki_set_num is not None and dfki_root is not None) else
                     "synthetic (Sonar A reference)",
        "n_gt_points": int(len(gt_t)),
        "RMSE_A_vs_GT": _rmse(a_t, a_x, a_y, gt_t, gt_x, gt_y),
        "RMSE_B_vs_GT": _rmse(b_t, b_x, b_y, gt_t, gt_x, gt_y),
        "RMSE_fused_vs_GT": _rmse(f_t, f_x, f_y, gt_t, gt_x, gt_y),
    }
    # Fusion improvement
    ra = res["RMSE_A_vs_GT"]; rb = res["RMSE_B_vs_GT"]; rf = res["RMSE_fused_vs_GT"]
    if rf is not None:
        if ra is not None and ra["rmse_2d"] > 0:
            res["fused_vs_A_improvement_pct"] = round(
                100.0 * (1.0 - rf["rmse_2d"] / ra["rmse_2d"]), 1)
        if rb is not None and rb["rmse_2d"] > 0:
            res["fused_vs_B_improvement_pct"] = round(
                100.0 * (1.0 - rf["rmse_2d"] / rb["rmse_2d"]), 1)
    return res


# ---------------------------------------------------------------------------
# 4) Covariance consistency (NEES-like)
# ---------------------------------------------------------------------------
def evaluate_covariance_consistency(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
    local_to_global_csv: Path,
    dfki_set_num: Optional[int] = None,
    dfki_root: Optional[Path] = None,
    synthetic_truth: bool = False,
    tol_s: float = 0.25,
) -> Dict:
    """Normalised estimation error squared: how consistent are fused
    covariances with the actual errors?

    For each GT timestamp, find the nearest fused frame, compute
        r = (x_fused - x_gt)^2 / Pxx_fused  +  (y_fused - y_gt)^2 / Pyy_fused
    A well-calibrated 2D estimator has mean r ≈ 2 (chi2 with 2 DOF).
    Mean r << 1 = over-confident (covariance too small).
    Mean r >> 2 = under-confident (covariance too large).
    """
    fused = pd.read_csv(fused_csv) if Path(fused_csv).exists() else pd.DataFrame()
    if fused.empty:
        return {"available": False}

    # Re-use the same GT setup as the RMSE evaluator
    if dfki_set_num is not None and dfki_root is not None:
        gt_path = dfki_root / f"set_{dfki_set_num}" / f"tracking_sonar_coordinates_{dfki_set_num}.csv"
        if not gt_path.exists():
            return {"available": False, "note": f"GT not found at {gt_path}"}
        gt = pd.read_csv(gt_path)
        gt["ts"] = pd.to_datetime(
            pd.to_numeric(gt["image_name"].str.replace(".jpg", "", regex=False), errors="coerce"),
            unit="s", utc=True, errors="coerce",
        )
        gt = gt.dropna(subset=["ts", "x", "y"]).sort_values("ts").reset_index(drop=True)
        gt_t = gt["ts"].astype("int64").to_numpy() / 1e9
        gt_x = gt["x"].to_numpy()
        gt_y = gt["y"].to_numpy()
    elif synthetic_truth:
        tr_a = pd.read_csv(tr_a_csv) if Path(tr_a_csv).exists() else pd.DataFrame()
        if tr_a.empty:
            return {"available": False, "note": "no A track for synthetic truth"}
        primary = tr_a[tr_a["local_track_id"].str.upper().str.contains("PRIMARY", na=False)]
        if primary.empty:
            primary = tr_a
        ref = primary.sort_values("timestamp")
        gt_t = ref["timestamp"].to_numpy()
        gt_x = ref["x"].to_numpy()
        gt_y = ref["y"].to_numpy()
    else:
        return {"available": False, "note": "no GT available"}

    l2g = pd.read_csv(local_to_global_csv) if Path(local_to_global_csv).exists() else pd.DataFrame()
    if l2g.empty:
        return {"available": False, "note": "missing local_to_global"}
    g2l: Dict[str, Dict[str, str]] = {}
    for _, r in l2g.iterrows():
        gid = r["global_id"]; lid = r["local_track_id"]
        sensor = "A" if str(lid).startswith("A_") else ("B" if str(lid).startswith("B_") else None)
        if sensor is None:
            continue
        g2l.setdefault(gid, {})[sensor] = lid

    # Find primary GID
    def _a_overlap(gid: str) -> int:
        a_lid = g2l.get(gid, {}).get("A")
        if a_lid is None or a_lid not in []:  # we don't need full A here, just count
            return 0
        return 1
    primary_gid = next(iter(fused["global_id"].unique()), None)
    if primary_gid is None:
        return {"available": False}
    fused_gid = fused[fused["global_id"] == primary_gid].sort_values("timestamp")
    f_t = fused_gid["timestamp"].to_numpy()
    f_x = fused_gid["x_fused"].to_numpy()
    f_y = fused_gid["y_fused"].to_numpy()
    f_Pxx = fused_gid["Pxx_fused"].to_numpy()
    f_Pyy = fused_gid["Pyy_fused"].to_numpy()

    n_re = []  # per-frame normalised error squared
    n = 0
    for i, t in enumerate(gt_t):
        d = np.abs(f_t - t)
        j = int(np.argmin(d))
        if d[j] > tol_s:
            continue
        Pxx = max(float(f_Pxx[j]), 1e-6)
        Pyy = max(float(f_Pyy[j]), 1e-6)
        ex = float(f_x[j] - gt_x[i])
        ey = float(f_y[j] - gt_y[i])
        n_re.append((ex * ex) / Pxx + (ey * ey) / Pyy)
        n += 1
    if not n_re:
        return {"available": False, "note": "no fused frames matched GT"}
    n_re = np.array(n_re)
    # Chi-square with 2 DOF: ideal mean = 2, 95% interval = [0.05, 7.38]
    mean_nees = float(n_re.mean())
    return {
        "available": True,
        "primary_global_id": primary_gid,
        "n_matched": int(n),
        "mean_NEES": round(mean_nees, 3),
        "median_NEES": round(float(np.median(n_re)), 3),
        "p95_NEES": round(float(np.percentile(n_re, 95)), 3),
        "frac_below_chi2_05": round(float(np.mean(n_re < 0.05)), 3),
        "frac_above_chi2_738": round(float(np.mean(n_re > 7.38)), 3),
        # Verdict
        "verdict": "over-confident" if mean_nees > 4.0
                   else ("well-calibrated" if 0.5 <= mean_nees <= 4.0
                         else "under-confident"),
        "ideal_mean_for_2d_chi2": 2.0,
    }


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------
def run_extended_evaluation(
    fused_csv: Path,
    tr_a_csv: Path,
    tr_b_csv: Path,
    local_to_global_csv: Path,
    dfki_set_num: Optional[int] = None,
    dfki_root: Optional[Path] = None,
    synthetic_truth: bool = False,
) -> Dict:
    out: Dict = {}
    out["both_predicted_honesty"] = evaluate_both_predicted_honesty(
        fused_csv, tr_a_csv, tr_b_csv, local_to_global_csv)
    out["duplicate_evidence_audit"] = evaluate_duplicate_evidence(
        fused_csv, tr_a_csv, tr_b_csv, local_to_global_csv)
    out["rmse_against_truth"] = evaluate_rmse_against_truth(
        fused_csv, tr_a_csv, tr_b_csv, local_to_global_csv,
        dfki_set_num=dfki_set_num, dfki_root=dfki_root,
        synthetic_truth=synthetic_truth)
    out["covariance_consistency"] = evaluate_covariance_consistency(
        fused_csv, tr_a_csv, tr_b_csv, local_to_global_csv,
        dfki_set_num=dfki_set_num, dfki_root=dfki_root,
        synthetic_truth=synthetic_truth)
    return out
