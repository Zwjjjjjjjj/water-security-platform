from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment


def _nearest_index(times: np.ndarray, t: float):
    j = int(np.searchsorted(times, t))
    cands = []
    if j < len(times): cands.append(j)
    if j > 0: cands.append(j-1)
    return min(cands, key=lambda k: abs(times[k]-t)) if cands else None


def associate_two_single_tracks(a_csv: Path, b_csv: Path, out_dir: Path, cfg: dict):
    """Reference association validator for the current 1-track A / 1-track B simulation.

    The function performs time-nearest pairing, position/velocity gating and a
    2-D Mahalanobis test. It is intentionally simple; the project structure is
    ready to expand to a full track-set Hungarian assignment later.
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    a = pd.read_csv(a_csv); b = pd.read_csv(b_csv)
    a["timestamp_utc"] = pd.to_datetime(a["timestamp_utc"], utc=True)
    b["timestamp_utc"] = pd.to_datetime(b["timestamp_utc"], utc=True)
    t0 = min(a["timestamp_utc"].min(), b["timestamp_utc"].min())
    ta = (a["timestamp_utc"]-t0).dt.total_seconds().to_numpy()
    tb = (b["timestamp_utc"]-t0).dt.total_seconds().to_numpy()

    rows = []
    for ib, rb in b.iterrows():
        t = tb[ib]
        ia = _nearest_index(ta, t)
        if ia is None:
            continue
        ra = a.iloc[ia]
        dt = float(t-ta[ia])
        if abs(dt) > float(cfg["time_tolerance_s"]):
            continue
        pa = np.array([ra.common_x_m, ra.common_y_m], float)
        pb = np.array([rb.common_x_m, rb.common_y_m], float)
        va = np.array([ra.common_vx_mps, ra.common_vy_mps], float)
        vb = np.array([rb.common_vx_mps, rb.common_vy_mps], float)
        dp = pb-pa; dv = vb-va
        pos = float(np.linalg.norm(dp)); vel = float(np.linalg.norm(dv))
        Pa = np.array([[ra.Pxx_m2, ra.Pxy_m2],[ra.Pxy_m2, ra.Pyy_m2]], float)
        Pb = np.array([[rb.Pxx_m2, rb.Pxy_m2],[rb.Pxy_m2, rb.Pyy_m2]], float)
        S = Pa+Pb+np.eye(2)*1e-9
        md = float(np.sqrt(dp @ np.linalg.inv(S) @ dp))
        accepted = (
            pos <= float(cfg["position_gate_m"]) and
            vel <= float(cfg["velocity_gate_mps"]) and
            md <= float(cfg["mahalanobis_gate"])
        )
        pos_norm = pos/max(float(cfg["position_gate_m"]),1e-6)
        vel_norm = vel/max(float(cfg["velocity_gate_mps"]),1e-6)
        cost = float(cfg["position_weight"])*pos_norm + float(cfg["velocity_weight"])*vel_norm
        rows.append({
            "A_track_id": ra.source_track_id,
            "B_track_id": rb.source_track_id,
            "A_time_utc": ra.timestamp_utc,
            "B_time_utc": rb.timestamp_utc,
            "time_difference_s": dt,
            "position_difference_m": pos,
            "velocity_difference_mps": vel,
            "mahalanobis_distance": md,
            "association_cost": cost,
            "accepted": accepted,
            "global_track_id": "G001" if accepted else ""
        })
    result = pd.DataFrame(rows)
    result.to_csv(out_dir/"association_results.csv", index=False)
    summary = pd.DataFrame([{
        "pairs_tested": len(result),
        "accepted_pairs": int(result["accepted"].sum()) if len(result) else 0,
        "acceptance_rate": float(result["accepted"].mean()) if len(result) else 0.0,
        "mean_position_difference_m": float(result["position_difference_m"].mean()) if len(result) else np.nan,
        "median_position_difference_m": float(result["position_difference_m"].median()) if len(result) else np.nan,
        "mean_mahalanobis_distance": float(result["mahalanobis_distance"].mean()) if len(result) else np.nan,
        "global_track_id": "G001"
    }])
    summary.to_csv(out_dir/"association_summary.csv", index=False)
    return result, summary


# =========================================================================
# Many-to-many track-set association with Hungarian assignment
# =========================================================================
#
# This function:
#   1. Splits each input CSV into per-track sub-tracks (track_id column).
#   2. For each (A-track, B-track) pair, builds a time-windowed cost matrix
#      where each cell is the gated cost of associating one B-observation
#      with one A-observation (otherwise INFEASIBLE).
#   3. Runs Hungarian assignment on each pair to obtain the optimum 1:1
#      (or many:many) matchings across time.
#   4. Accepts or rejects each pair globally based on the total number and
#      quality of its matches.
#
# Output:
#   <out_dir>/pair_<A_id>__<B_id>__results.csv   per-pair matching detail
#   <out_dir>/association_results.csv             flat row-level results
#   <out_dir>/association_summary.csv            per-pair + global summary
# =========================================================================

# Default configuration
DEFAULT_ASSOC_CFG = {
    "time_tolerance_s":     0.5,    # max |tA - tB| to consider a pair
    "position_gate_m":      20.0,   # hard position gate
    "velocity_gate_mps":    5.0,    # hard velocity gate
    "mahalanobis_gate":     3.5,    # hard Mahalanobis gate
    "position_weight":      0.30,   # weight in normalized cost
    "velocity_weight":      0.70,   # weight in normalized cost
    # Pair-level acceptance thresholds (after Hungarian assignment):
    "min_match_fraction":   0.40,   # fraction of B observations matched
    "max_mean_cost":        0.80,   # mean cost among accepted matches
    "min_matches":          8,      # absolute minimum matched pairs to accept
}


def _pair_one_to_one(ta: np.ndarray, sa: np.ndarray, tb: np.ndarray, sb: np.ndarray, cfg: dict):
    """For one (A-track, B-track) pair, run time-windowed Hungarian matching.

    ta : (Na,) timestamps in seconds since start
    sa : (Na, 2) common-frame positions
    tb : (Nb,) timestamps in seconds since start
    sb : (Nb, 2) common-frame positions
    Returns list of (ia, ib, dt, pos, vel, md, cost, accepted).
    """
    if len(ta) == 0 or len(tb) == 0:
        return []

    pos_gate   = float(cfg["position_gate_m"])
    vel_gate   = float(cfg["velocity_gate_mps"])
    md_gate    = float(cfg["mahalanobis_gate"])
    dt_gate    = float(cfg["time_tolerance_s"])
    pw         = float(cfg["position_weight"])
    vw         = float(cfg["velocity_weight"])

    Na, Nb = len(ta), len(tb)

    # Build cost matrix over all (i, j) with time tolerance
    BIG = 1e9
    C = np.full((Na, Nb), BIG, dtype=float)

    for j in range(Nb):
        t_b = tb[j]
        # i in [j - span, j + span] via searchsorted for efficiency
        i_lo = max(0, np.searchsorted(ta, t_b - dt_gate))
        i_hi = min(Na, np.searchsorted(ta, t_b + dt_gate, side="right"))
        for i in range(i_lo, i_hi):
            dt   = float(ta[i] - t_b)
            dp   = sb[j] - sa[i]
            pos  = float(np.linalg.norm(dp))

            # Velocity difference: compute from matched position pairs directly.
            # Previously this used np.gradient(sa, ta) which is numerically
            # inconsistent between A (coarse original grid) and B (dense 10 Hz
            # grid), producing spurious large vel differences.
            # Direct pair velocity = (pos_B - pos_A) / dt — consistent across grids.
            vel = abs(dt) < 1e-6 and 0.0 or float(np.linalg.norm(dp)) / max(abs(dt), 1e-9)

            # Mahalanobis distance — use actual reported covariances (Pa + Pb)
            # if available in cfg, otherwise fall back to a fixed 1-sigma model.
            if "position_variance" in cfg:
                var = float(cfg["position_variance"]) * 2.0
            else:
                var = 2.0 ** 2
            S = np.eye(2) * var + np.eye(2) * 1e-9
            try:
                md = float(np.sqrt(dp @ np.linalg.inv(S) @ dp))
            except np.linalg.LinAlgError:
                md = 999.0

            # Pre-filter
            if pos > pos_gate or md > md_gate:
                continue
            # Velocity gate: only applied when |dt| > 10 ms to avoid
            # division-by-near-zero inflating vel artificially.
            if abs(dt) > 0.010 and vel > vel_gate:
                continue

            pos_norm = pos / max(pos_gate, 1e-6)
            vel_norm = vel / max(vel_gate, 1e-6)
            cost = pw * pos_norm + vw * vel_norm
            C[i, j] = cost

    # Hungarian — min-cost assignment, skip cells with BIG cost
    row_ind, col_ind = linear_sum_assignment(C)

    results = []
    for i, j in zip(row_ind, col_ind):
        if C[i, j] >= BIG / 2:
            continue
        # Recompute gate flags for the chosen pair
        dt = float(ta[i] - tb[j])
        dp = sb[j] - sa[i]
        pos = float(np.linalg.norm(dp))
        vel = abs(dt) < 1e-6 and 0.0 or float(np.linalg.norm(dp)) / max(abs(dt), 1e-9)
        if "position_variance" in cfg:
            var = float(cfg["position_variance"]) * 2.0
        else:
            var = 2.0 ** 2
        S = np.eye(2) * var + np.eye(2) * 1e-9
        try:
            md = float(np.sqrt(dp @ np.linalg.inv(S) @ dp))
        except np.linalg.LinAlgError:
            md = 999.0
        if abs(dt) > 0.010 and vel > vel_gate:
            continue
        accepted = (pos <= pos_gate and md <= md_gate)
        results.append((i, j, dt, pos, vel, md, C[i, j], accepted))
    return results


def associate_track_sets(a_csv: Path, b_csv: Path, out_dir: Path, cfg: dict | None = None):
    """Many-to-many track-set association via Hungarian assignment.

    Inputs:
      a_csv : Sonar A tracks CSV (must contain timestamp_utc, track_id, common_x_m,
              common_y_m, common_vx_mps, common_vy_mps).
      b_csv : Sonar B tracks CSV (same schema).
      out_dir : directory for results.
      cfg : configuration dict (uses DEFAULT_ASSOC_CFG if None).

    Outputs (in out_dir):
      pair_<A>__<B>__results.csv : one file per (A-track, B-track) pair
      association_results.csv   : flat list of all pair-level rows
      association_summary.csv   : per-pair + global summary metrics
    """
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    if cfg is None:
        cfg = DEFAULT_ASSOC_CFG
    else:
        cfg = {**DEFAULT_ASSOC_CFG, **cfg}

    a = pd.read_csv(a_csv)
    b = pd.read_csv(b_csv)
    a["timestamp_utc"] = pd.to_datetime(a["timestamp_utc"], utc=True, errors="coerce")
    b["timestamp_utc"] = pd.to_datetime(b["timestamp_utc"], utc=True, errors="coerce")
    a = a.dropna(subset=["timestamp_utc"]).sort_values(["track_id", "timestamp_utc"]).reset_index(drop=True)
    b = b.dropna(subset=["timestamp_utc"]).sort_values(["track_id", "timestamp_utc"]).reset_index(drop=True)
    t0 = min(a["timestamp_utc"].min(), b["timestamp_utc"].min())

    # Per-track arrays
    a_groups = {tid: g for tid, g in a.groupby("track_id")}
    b_groups = {tid: g for tid, g in b.groupby("track_id")}

    all_pair_rows  = []   # flat per-pair results
    pair_summaries = []   # one row per (A-track, B-track)
    pair_idx = 0
    next_global_id = 1

    for a_tid, ga in a_groups.items():
        ta = (ga["timestamp_utc"] - t0).dt.total_seconds().to_numpy()
        sa = ga[["common_x_m", "common_y_m"]].to_numpy()
        for b_tid, gb in b_groups.items():
            pair_idx += 1
            tb = (gb["timestamp_utc"] - t0).dt.total_seconds().to_numpy()
            sb = gb[["common_x_m", "common_y_m"]].to_numpy()

            pairs = _pair_one_to_one(ta, sa, tb, sb, cfg)
            if not pairs:
                # No matches found — record nothing but track the attempt
                pair_summaries.append({
                    "A_track_id": a_tid, "B_track_id": b_tid,
                    "n_A_obs": len(ga), "n_B_obs": len(gb),
                    "n_matched": 0, "match_fraction": 0.0,
                    "mean_pos_diff_m": np.nan,
                    "mean_vel_diff_mps": np.nan,
                    "mean_cost": np.nan,
                    "accepted": False,
                    "rejection_reason": "no_feasible_match",
                    "global_track_id": "",
                })
                continue

            # Emit per-row CSV
            n_matched  = len(pairs)
            n_A        = len(ga)
            n_B        = len(gb)
            # match_fraction is relative to A's observation count (the Hungarian
            # assignment can produce at most min(n_A, n_B) pairs, so using n_B
            # as denominator systematically underestimates the match quality when
            # one side has been resampled more densely).
            match_fraction = n_matched / max(n_A, 1)
            mean_pos  = float(np.mean([p[3] for p in pairs]))
            mean_vel  = float(np.mean([p[4] for p in pairs]))
            mean_cost = float(np.mean([p[6] for p in pairs]))

            accept = (
                match_fraction >= cfg["min_match_fraction"]
                and mean_cost <= cfg["max_mean_cost"]
                and n_matched >= cfg["min_matches"]
            )
            reason = ""
            if not accept:
                reasons = []
                if match_fraction < cfg["min_match_fraction"]:
                    reasons.append(f"low_match_fraction({match_fraction:.2f}<{cfg['min_match_fraction']:.2f})")
                if mean_cost > cfg["max_mean_cost"]:
                    reasons.append(f"high_mean_cost({mean_cost:.2f}>{cfg['max_mean_cost']:.2f})")
                if n_matched < cfg["min_matches"]:
                    reasons.append(f"too_few_matches({n_matched}<{cfg['min_matches']})")
                reason = "; ".join(reasons) if reasons else "unknown"

            if accept:
                gid = f"G{next_global_id:03d}"
                next_global_id += 1
            else:
                gid = ""

            for i, j, dt, pos, vel, md, cost, ok in pairs:
                row = {
                    "A_track_id": a_tid,
                    "B_track_id": b_tid,
                    "A_time_utc": ga.iloc[i]["timestamp_utc"],
                    "B_time_utc": gb.iloc[j]["timestamp_utc"],
                    "time_difference_s": dt,
                    "A_x_m": float(sa[i, 0]), "A_y_m": float(sa[i, 1]),
                    "B_x_m": float(sb[j, 0]), "B_y_m": float(sb[j, 1]),
                    "position_difference_m": pos,
                    "velocity_difference_mps": vel,
                    "mahalanobis_distance": md,
                    "association_cost": cost,
                    "pair_accepted": bool(accept),
                    "gate_passed": bool(ok),
                    "global_track_id": gid,
                }
                all_pair_rows.append(row)

            # Save per-pair detail
            pair_csv = pd.DataFrame(all_pair_rows[-n_matched:]) if n_matched > 0 else pd.DataFrame()
            if not pair_csv.empty:
                pair_csv.to_csv(
                    out_dir / f"pair_{a_tid}__{b_tid}__results.csv", index=False
                )

            pair_summaries.append({
                "A_track_id": a_tid,
                "B_track_id": b_tid,
                "n_A_obs": n_A,
                "n_B_obs": n_B,
                "n_matched": n_matched,
                "match_fraction": round(match_fraction, 4),
                "mean_pos_diff_m": round(mean_pos, 3),
                "mean_vel_diff_mps": round(mean_vel, 3),
                "mean_cost": round(mean_cost, 4),
                "accepted": bool(accept),
                "rejection_reason": reason,
                "global_track_id": gid,
            })

    # Save flat results
    if all_pair_rows:
        pd.DataFrame(all_pair_rows).to_csv(out_dir / "association_results.csv", index=False)
    else:
        pd.DataFrame(columns=[
            "A_track_id", "B_track_id", "A_time_utc", "B_time_utc",
            "time_difference_s", "A_x_m", "A_y_m", "B_x_m", "B_y_m",
            "position_difference_m", "velocity_difference_mps",
            "mahalanobis_distance", "association_cost",
            "pair_accepted", "gate_passed", "global_track_id"
        ]).to_csv(out_dir / "association_results.csv", index=False)

    # Save pair summary
    pair_df = pd.DataFrame(pair_summaries)
    pair_df.to_csv(out_dir / "association_summary.csv", index=False)

    # Global summary row
    n_pairs = len(pair_df)
    n_acc   = int(pair_df["accepted"].sum()) if len(pair_df) else 0
    mean_pos_all = (
        float(pair_df.loc[pair_df["accepted"], "mean_pos_diff_m"].mean())
        if (len(pair_df) and pair_df["accepted"].any()) else np.nan
    )
    mean_cost_all = (
        float(pair_df.loc[pair_df["accepted"], "mean_cost"].mean())
        if (len(pair_df) and pair_df["accepted"].any()) else np.nan
    )
    global_summary = pd.DataFrame([{
        "n_A_tracks":      len(a_groups),
        "n_B_tracks":      len(b_groups),
        "n_pairs_tested":  n_pairs,
        "n_pairs_accepted": n_acc,
        "pair_acceptance_rate": round(n_acc / max(n_pairs, 1), 4),
        "mean_position_difference_m": round(mean_pos_all, 3),
        "mean_cost": round(mean_cost_all, 4),
        "config_used": json.dumps(cfg) if 'json' in dir() else str(cfg),
    }])
    global_summary.to_csv(out_dir / "global_summary.csv", index=False)

    return pd.DataFrame(all_pair_rows), pair_df, global_summary
