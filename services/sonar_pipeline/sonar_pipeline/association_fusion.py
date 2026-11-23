"""Stage 5 (association) + Stage 6 (fusion) via the OFFICIAL engines.

N=2 : TrackAligner + TrackAssociation + CovarianceWeightedFusion
N>=3: NWayAligner + NPartiteAssociator + InformationFusionN

This module is the single place where association/fusion algorithms live.
The old inlined copies in Tritech src/e2e_bridge.py
(_align_tracks_nearest_neighbour / _build_simple_cost_matrix /
_hungarian_assignment / _fuse_one_frame) are NOT used here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd

from .common import TWO_SONAR_SRC

sys.path.insert(0, str(TWO_SONAR_SRC))

from align_tracks import TrackAligner  # noqa: E402
from track_association import TrackAssociation  # noqa: E402
from track_fusion import CovarianceWeightedFusion  # noqa: E402
from n_way_align import NWayAligner  # noqa: E402
from n_partite_association import NPartiteAssociator  # noqa: E402
from n_way_fusion import InformationFusionN  # noqa: E402


def _ensure_vel(df: pd.DataFrame) -> pd.DataFrame:
    """Fill NaN vx/vy by per-track gradient (needed for official aligner)."""
    df = df.copy()
    for col in ("vx", "vy", "Pxx", "Pyy"):
        if col not in df.columns:
            df[col] = 1.0 if col.startswith("P") else 0.0
    for tid, grp in df.groupby("track_id" if "track_id" in df.columns else "local_track_id"):
        idx = grp.sort_values("timestamp").index
        if df.loc[idx, "vx"].isna().all():
            x = pd.to_numeric(df.loc[idx, "x"], errors="coerce").values.astype(float)
            y = pd.to_numeric(df.loc[idx, "y"], errors="coerce").values.astype(float)
            t = pd.to_numeric(df.loc[idx, "timestamp"], errors="coerce").values.astype(float)
            if len(idx) >= 2:
                vx = np.gradient(x, t)
                vy = np.gradient(y, t)
                df.loc[idx, "vx"] = vx
                df.loc[idx, "vy"] = vy
            else:
                df.loc[idx, ["vx", "vy"]] = 0.0
    df[["vx", "vy"]] = df[["vx", "vy"]].fillna(0.0)
    df[["Pxx", "Pyy"]] = df[["Pxx", "Pyy"]].fillna(1.0)
    return df


def _norm_tracks(df: pd.DataFrame) -> pd.DataFrame:
    """Project bridge-style tracks onto the official aligner schema.

    IMPORTANT: the canonical identity is ``local_track_id`` (``A_...`` /
    ``B_...`` prefixed). The raw ``track_id`` column holds the UNDERLYING
    track name, which collides across sensors (both A and B contain
    ``PRIMARY_DIVER``). We therefore overwrite ``track_id`` with the
    prefixed local id so grouping / Hungarian / fusion can never confuse
    A_PRIMARY_DIVER with B_PRIMARY_DIVER.
    """
    df = df.copy()
    if "local_track_id" in df.columns:
        df["track_id"] = df["local_track_id"].astype(str)
    elif "track_id" not in df.columns:
        raise ValueError("tracks need track_id or local_track_id")
    keep = ["timestamp", "track_id", "x", "y", "vx", "vy", "Pxx", "Pyy",
            "measurement_available", "local_track_id", "sensor_id"]
    for c in keep:
        if c not in df.columns:
            df[c] = True if c == "measurement_available" else (1.0 if c in ("Pxx", "Pyy") else 0.0)
    return _ensure_vel(df)


def associate_n_way(
    tracks_by_sensor: Dict[str, pd.DataFrame],
    association_cfg: Dict,
    n_way_cfg: Optional[Dict] = None,
    sensor_order: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run official association.

    Returns (aligned_pairs_or_long, cost_matrix_df, associations_df, local_to_global_df).
    For N=2, aligned output uses the legacy aligned_track_pairs schema
    (track_a_id/track_b_id/Ax...); for N>=3 it is long-format aligned_long
    plus an n_clusters table folded into associations_df.
    """
    sensors = sensor_order or sorted(tracks_by_sensor.keys())
    normed = {s: _norm_tracks(tracks_by_sensor[s]) for s in sensors}
    n = len(sensors)

    if n == 2:
        sa, sb = sensors[0], sensors[1]
        aligner = TrackAligner({"max_time_gap_s": association_cfg.get("max_time_gap_s", 0.15)})
        aligned = aligner.align_all_tracks(normed[sa], normed[sb])
        if aligned.empty:
            empty_cost = pd.DataFrame(columns=["track_a_id", "track_b_id", "cost"])
            empty_assoc = pd.DataFrame(columns=["track_a_id", "track_b_id", "cost"])
            l2g = pd.DataFrame([
                {"local_track_id": t, "global_id": f"G{i+1:03d}"}
                for i, t in enumerate(
                    list(normed[sa]["track_id"].unique()) + list(normed[sb]["track_id"].unique()))
            ])
            return aligned, empty_cost, empty_assoc, l2g
        assoc = TrackAssociation(association_cfg)
        cost_matrix, ids_a, ids_b, details = assoc.build_cost_matrix(aligned)
        # Sanitize: the official matrix leaves +inf where a track pair has
        # zero aligned rows. A single all-inf row/col makes
        # linear_sum_assignment raise "cost matrix is infeasible" and kills
        # the whole pipeline. Replace +inf with a large finite cost so the
        # Hungarian stays feasible; filter_valid_associations will still
        # drop gate-failing pairs afterwards.
        import numpy as _np
        cost_matrix = _np.where(_np.isinf(cost_matrix), 1e6, cost_matrix)
        try:
            assignments = assoc.compute_hungarian_assignment(cost_matrix, ids_a, ids_b, details)
        except ValueError:
            # Last resort: greedy argmin per row on finite entries.
            assignments = []
            for i, ta in enumerate(ids_a):
                j = int(_np.argmin(cost_matrix[i]))
                if cost_matrix[i, j] < 1e6:
                    assignments.append((ta, ids_b[j], float(cost_matrix[i, j])))
        valid = assoc.filter_valid_associations(assignments, details)
        cost_rows = []
        for (ta, tb), d in details.items():
            cost_rows.append({"track_a_id": ta, "track_b_id": tb, **d})
        cost_df = pd.DataFrame(cost_rows)
        assoc_df = pd.DataFrame(valid)
        # local → global map
        rows, gid_n, used = [], 1, set()
        for r in valid:
            gid = f"G{gid_n:03d}"; gid_n += 1
            rows += [{"local_track_id": r["track_a_id"], "global_id": gid},
                     {"local_track_id": r["track_b_id"], "global_id": gid}]
            used |= {r["track_a_id"], r["track_b_id"]}
        for tid in list(normed[sa]["track_id"].unique()) + list(normed[sb]["track_id"].unique()):
            if tid not in used:
                rows.append({"local_track_id": tid, "global_id": f"G{gid_n:03d}"}); gid_n += 1
        l2g = pd.DataFrame(rows)
        return aligned, cost_df, assoc_df, l2g

    # N >= 3
    aligner = NWayAligner(
        max_time_gap_s=association_cfg.get("max_time_gap_s", 0.15),
        anchor_sensor=(n_way_cfg or {}).get("anchor_sensor_id", sensors[0]),
    )
    by_sensor_tracks = {}
    for s in sensors:
        d = normed[s].copy()
        by_sensor_tracks[s] = d
    aligned_long = aligner.align(by_sensor_tracks, anchor_sensor=sensors[0])
    associator = NPartiteAssociator(association_cfg, n_way_cfg or {})
    clusters = associator.find_clusters(aligned_long, sensors=sensors)
    crec = []
    l2g_rows = []
    for i, c in enumerate(clusters):
        gid = f"G{i+1:03d}"
        rec = {"cluster_id": i, "global_id": gid, "mean_cost": c.mean_cost,
               "gate_pass": c.gate_pass, "pair_pass_ratio": c.pair_pass_ratio}
        for s_id, t_id in c.members.items():
            rec[f"member_{s_id}"] = t_id
            l2g_rows.append({"local_track_id": t_id, "global_id": gid, "sensor_id": s_id})
        crec.append(rec)
    assoc_df = pd.DataFrame(crec) if crec else pd.DataFrame(
        columns=["cluster_id", "global_id", "mean_cost", "gate_pass", "pair_pass_ratio"])
    cost_rows = [
        {"sensor_pair": f"{k[0]}<->{k[1]}", "cost": v,
         "cluster_id": i}
        for i, c in enumerate(clusters) for k, v in c.pair_costs.items()
    ]
    cost_df = pd.DataFrame(cost_rows) if cost_rows else pd.DataFrame(
        columns=["sensor_pair", "cost", "cluster_id"])
    # unassociated tracks get own gid
    used = {r["local_track_id"] for r in l2g_rows}
    gid_n = len(clusters) + 1
    for s in sensors:
        for tid in normed[s]["track_id"].unique():
            if tid not in used:
                l2g_rows.append({"local_track_id": tid, "global_id": f"G{gid_n:03d}", "sensor_id": s})
                gid_n += 1
    l2g = pd.DataFrame(l2g_rows)
    return aligned_long, cost_df, assoc_df, l2g


def fuse_n_way(
    tracks_by_sensor: Dict[str, pd.DataFrame],
    local_to_global: pd.DataFrame,
    fusion_cfg: Dict,
    p_scale: float = 15.0,
) -> pd.DataFrame:
    """Run official fusion (2-sonar or N-sonar) and return fused_global_tracks."""
    sensors = sorted(tracks_by_sensor.keys())
    normed = {s: _norm_tracks(tracks_by_sensor[s]) for s in sensors}
    # global → {sensor: local_id}
    g2l: Dict[str, Dict[str, str]] = {}
    for _, r in local_to_global.iterrows():
        gid, lid = r["global_id"], r["local_track_id"]
        s = r.get("sensor_id") if "sensor_id" in local_to_global.columns else None
        if not s:
            s = sensors[0] if lid in set(normed[sensors[0]]["track_id"]) else sensors[-1]
            for ss in sensors:
                if lid in set(normed[ss]["track_id"].astype(str)) or lid in set(normed[ss].get("local_track_id", [])):
                    s = ss; break
        g2l.setdefault(gid, {})[s] = lid

    # index states per local id
    states: Dict[str, pd.DataFrame] = {}
    for s in sensors:
        for tid, grp in normed[s].groupby("track_id"):
            states[str(tid)] = grp.sort_values("timestamp").reset_index(drop=True)

    records: List[Dict] = []
    if len(sensors) == 2:
        fuser = CovarianceWeightedFusion(fusion_cfg)
        for gid, m in g2l.items():
            dfs = {s: states.get(m.get(s)) for s in sensors}
            all_t = sorted(set().union(*[d["timestamp"].tolist() for d in dfs.values() if d is not None]))
            for t in all_t:
                st = []
                for s in sensors:
                    d = dfs.get(s)
                    tu = _extract_one(d, t, tol=fusion_cfg.get("max_dt_for_fusion_s", 0.5) and 0.12 or 0.12)
                    st.append(tu)
                # _extract_one returns (x, P, meas, t_meas) or None
                fs = fuser.fuse(t, gid, st[0], st[1])
                records.append({
                    "timestamp": fs.timestamp, "global_id": fs.global_id,
                    "x_fused": float(fs.position[0]), "y_fused": float(fs.position[1]),
                    "vx_fused": float(fs.velocity[0]), "vy_fused": float(fs.velocity[1]),
                    "Pxx_fused": float(fs.covariance[0, 0]) * p_scale,
                    "Pyy_fused": float(fs.covariance[1, 1]) * p_scale,
                    "n_sensors_used": fs.n_sensors_used,
                    "sensors_used": ";".join(fs.sensors_used),
                    "fusion_mode": fs.fusion_mode, "is_valid": fs.is_valid,
                    "fallback_reason": fs.fallback_reason or "",
                })
    else:
        fuser = InformationFusionN(fusion_cfg)
        for gid, m in g2l.items():
            dfs = {s: states.get(m.get(s)) for s in sensors}
            all_t = sorted(set().union(*[d["timestamp"].tolist() for d in dfs.values() if d is not None]))
            for t in all_t:
                st = []
                for s in sensors:
                    tu = _extract_one(dfs.get(s), t)
                    if tu is None:
                        continue
                    x, P, meas, t_meas = tu
                    st.append((s, x, P, meas, t_meas))
                if not st:
                    continue
                fs = fuser.fuse(timestamp=t, global_id=gid, states=st)
                records.append({
                    "timestamp": fs.timestamp, "global_id": fs.global_id,
                    "x_fused": float(fs.position[0]), "y_fused": float(fs.position[1]),
                    "vx_fused": float(fs.velocity[0]), "vy_fused": float(fs.velocity[1]),
                    "Pxx_fused": float(fs.covariance[0, 0]) * p_scale,
                    "Pyy_fused": float(fs.covariance[1, 1]) * p_scale,
                    "n_sensors_used": fs.n_sensors_used,
                    "sensors_used": ";".join(fs.sensors_used),
                    "fusion_mode": fs.fusion_mode, "is_valid": fs.is_valid,
                    "fallback_reason": fs.fallback_reason or "",
                })
    return pd.DataFrame(records)


def _extract_one(df: Optional[pd.DataFrame], t: float, tol: float = 0.12):
    if df is None or df.empty:
        return None
    diffs = (df["timestamp"] - t).abs()
    idx = int(diffs.idxmin())
    if diffs.iloc[idx] > tol:
        return None
    r = df.iloc[idx]
    if pd.isna(r.get("x")) or pd.isna(r.get("Pxx")):
        return None
    x = np.array([r["x"], r["y"], r.get("vx", 0.0), r.get("vy", 0.0)], dtype=float)
    P = np.zeros((4, 4))
    P[0, 0] = r["Pxx"]; P[0, 1] = r.get("Pxy", 0.0)
    P[1, 0] = P[0, 1];  P[1, 1] = r["Pyy"]
    P[2, 2] = r.get("Pvxvx", 1.0); P[3, 3] = r.get("Pvyvy", 1.0)
    return (x, P, bool(r.get("measurement_available", True)), float(r["timestamp"]))
