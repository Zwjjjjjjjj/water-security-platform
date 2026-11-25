from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


def _choose_track(df: pd.DataFrame, requested: str | None, target_label: str | None = None):
    if requested and requested in set(df["track_id"].astype(str)):
        return requested
    if target_label and "target_label" in df.columns:
        cand = df[df["target_label"].astype(str) == str(target_label)]
        if not cand.empty:
            stats = cand.groupby("track_id").agg(
                rows=("track_id", "size"),
                duration=("timestamp_utc", lambda x: (x.max()-x.min()).total_seconds()),
                mean_conf=("track_confidence", "mean")
            )
            stats["score"] = stats["rows"] * np.maximum(stats["duration"], 0.1) * np.maximum(stats["mean_conf"], 0.05)
            return str(stats["score"].idxmax())
    stats = df.groupby("track_id").agg(
        rows=("track_id", "size"),
        duration=("timestamp_utc", lambda x: (x.max()-x.min()).total_seconds()),
        mean_conf=("track_confidence", "mean")
    )
    stats["score"] = stats["rows"] * np.maximum(stats["duration"], 0.1) * np.maximum(stats["mean_conf"], 0.05)
    return str(stats["score"].idxmax())


def simulate_second_sonar(standardized_csv: Path, out_dir: Path, sim_cfg: dict):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(standardized_csv)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "fan_u_filt_m", "fan_v_filt_m"])
    tid = _choose_track(df, sim_cfg.get("source_track_id"), sim_cfg.get("source_target_label"))
    a = df[df["track_id"].astype(str) == tid].copy().sort_values("timestamp_utc").reset_index(drop=True)
    if len(a) < 10:
        raise RuntimeError(f"Selected source track {tid} has too few points")

    t0 = a["timestamp_utc"].iloc[0]
    t = (a["timestamp_utc"] - t0).dt.total_seconds().to_numpy()
    x = a["fan_u_filt_m"].to_numpy(float)
    y = a["fan_v_filt_m"].to_numpy(float)
    vx = np.gradient(x, t)
    vy = np.gradient(y, t)

    tx, ty = map(float, sim_cfg["translation_common_m"])
    yaw_deg = float(sim_cfg["yaw_deg"])
    yaw = np.deg2rad(yaw_deg)
    R = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
    translation = np.array([tx, ty])

    dt = 1.0 / float(sim_cfg["sample_rate_hz"])
    time_offset = float(sim_cfg["time_offset_s"])
    tb = np.arange(t.min()+time_offset, t.max()-0.02, dt)
    p_common = np.column_stack([np.interp(tb,t,x), np.interp(tb,t,y)])
    v_common = np.column_stack([np.interp(tb,t,vx), np.interp(tb,t,vy)])
    p_local = (R.T @ (p_common - translation).T).T
    v_local = (R.T @ v_common.T).T

    rng = np.random.default_rng(int(sim_cfg["random_seed"]))
    keep = rng.random(len(tb)) > float(sim_cfg["drop_rate"])
    tb = tb[keep]
    p_local = p_local[keep] + rng.normal(0, float(sim_cfg["position_std_m"]), size=(keep.sum(),2))
    v_local = v_local[keep] + rng.normal(0, float(sim_cfg["velocity_std_mps"]), size=(keep.sum(),2))
    jitter = rng.normal(0, float(sim_cfg["timestamp_jitter_std_s"]), size=keep.sum())
    p_common_obs = (R @ p_local.T).T + translation
    v_common_obs = (R @ v_local.T).T

    b = pd.DataFrame({
        "timestamp_utc": [(t0 + pd.to_timedelta(float(tt+jj), unit="s")).isoformat().replace("+00:00","Z") for tt,jj in zip(tb,jitter)],
        "elapsed_s": tb+jitter,
        "sensor_id": "SIM_SONAR_B",
        "source_track_id": "B_SIM_001",
        "simulated_from_real_track": tid,
        "coordinate_frame_local": "SIM_SONAR_B_LOCAL",
        "local_x_m": p_local[:,0], "local_y_m": p_local[:,1],
        "local_vx_mps": v_local[:,0], "local_vy_mps": v_local[:,1],
        "common_x_m": p_common_obs[:,0], "common_y_m": p_common_obs[:,1],
        "common_vx_mps": v_common_obs[:,0], "common_vy_mps": v_common_obs[:,1],
        "Pxx_m2": float(sim_cfg["position_std_m"])**2,
        "Pxy_m2": 0.0,
        "Pyy_m2": float(sim_cfg["position_std_m"])**2,
        "Pvxvx_m2ps2": float(sim_cfg["velocity_std_mps"])**2,
        "Pvyvy_m2ps2": float(sim_cfg["velocity_std_mps"])**2,
        "measurement_available": True,
        "is_simulated": True,
    })

    a_common = pd.DataFrame({
        "timestamp_utc": a["timestamp_utc"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "elapsed_s": t,
        "sensor_id": "REAL_SONAR_A",
        "source_track_id": "A_" + tid,
        "coordinate_frame_common": "ASSOCIATION_COMMON_LOCAL",
        "common_x_m": x, "common_y_m": y,
        "common_vx_mps": vx, "common_vy_mps": vy,
        "Pxx_m2": a.get("Puu_m2", pd.Series(np.full(len(a),0.10**2))).to_numpy(),
        "Pxy_m2": a.get("Puv_m2", pd.Series(np.zeros(len(a)))).to_numpy(),
        "Pyy_m2": a.get("Pvv_m2", pd.Series(np.full(len(a),0.10**2))).to_numpy(),
        "Pvxvx_m2ps2": a.get("Pdu_m2ps2", pd.Series(np.full(len(a),0.08**2))).to_numpy(),
        "Pvyvy_m2ps2": a.get("Pdv_m2ps2", pd.Series(np.full(len(a),0.08**2))).to_numpy(),
        "measurement_available": True,
        "is_simulated": False,
    })

    a_path = out_dir / "sonar_A_real_common.csv"
    b_path = out_dir / "sonar_B_simulated_observations.csv"
    a_common.to_csv(a_path, index=False)
    b.to_csv(b_path, index=False)

    params = {
        "source_real_track": tid,
        "common_frame_note": "ASSOCIATION_COMMON_LOCAL is an abstract validation frame; it is NOT real ENU.",
        "sonar_B_artificial_extrinsics": {
            "translation_common_m": [tx,ty], "yaw_deg": yaw_deg,
            "transform": "p_common = R(yaw) @ p_B_local + translation"
        },
        "simulation": sim_cfg,
    }
    with open(out_dir / "simulation_parameters.json", "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)

    fig, ax = plt.subplots(figsize=(8,7))
    ax.plot(a_common["common_x_m"], a_common["common_y_m"], linewidth=2, label="Sonar A real track")
    ax.scatter(b["common_x_m"], b["common_y_m"], s=18, label="Sonar B simulated observations")
    ax.scatter([tx],[ty], marker="^", s=100, label="Simulated Sonar B")
    ax.scatter([0],[0], marker="^", s=100, label="Reference origin")
    ax.set_xlabel("Common x (m)"); ax.set_ylabel("Common y (m)")
    ax.set_title("Real Sonar A vs simulated Sonar B")
    ax.grid(True, alpha=0.3); ax.legend(); ax.set_aspect("equal", adjustable="box")
    fig.savefig(out_dir / "sonar_A_B_simulation_overview.png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    return a_path, b_path, tid
