from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter


def visualize_tracks(csv_path: Path, out_dir: Path, fps: int = 12):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(csv_path)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp_utc", "fan_u_filt_m", "fan_v_filt_m", "track_id"]).sort_values("timestamp_utc")
    if df.empty:
        return None, None

    t0 = df["timestamp_utc"].min()
    df["elapsed_s"] = (df["timestamp_utc"] - t0).dt.total_seconds()
    track_dfs = {tr:g.sort_values("elapsed_s").reset_index(drop=True) for tr,g in df.groupby("track_id")}

    u_abs = max(5.0, float(np.nanmax(np.abs(df["fan_u_filt_m"]))) * 1.2)
    vmin = min(0.0, float(np.nanmin(df["fan_v_filt_m"])))
    vmax = float(np.nanmax(df["fan_v_filt_m"]))
    span = max(5.0, vmax-vmin)
    vlo, vhi = vmin-0.08*span, vmax+0.08*span

    # Static overview
    fig, ax = plt.subplots(figsize=(7,7))
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(-u_abs, u_abs); ax.set_ylim(vlo, vhi)
    ax.set_xlabel("Fan lateral coordinate u (m)")
    ax.set_ylabel("Fan axial coordinate v (m)")
    ax.set_title("Gemini sonar target tracks (SONAR_FAN)")
    ax.grid(True, alpha=0.3)
    ax.scatter([0],[0], marker="^", s=120, label="Sonar")
    for tr,g in track_dfs.items():
        ax.plot(g["fan_u_filt_m"], g["fan_v_filt_m"], linewidth=2, label=str(tr))
    ax.legend(fontsize=8)
    overview = out_dir / "tracks_overview.png"
    fig.savefig(overview, dpi=160, bbox_inches="tight")
    plt.close(fig)

    # GIF animation
    tmin, tmax = df["elapsed_s"].min(), df["elapsed_s"].max()
    nframes = min(100, max(60, int(max(1, tmax-tmin))))
    times = np.linspace(tmin, tmax, nframes)
    fig, ax = plt.subplots(figsize=(7,7))
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(-u_abs, u_abs); ax.set_ylim(vlo, vhi)
    ax.set_xlabel("Fan lateral coordinate u (m)")
    ax.set_ylabel("Fan axial coordinate v (m)")
    ax.set_title("Gemini sonar target tracks (SONAR_FAN)")
    ax.grid(True, alpha=0.3)
    ax.scatter([0],[0], marker="^", s=120, label="Sonar")
    arts = {}
    for tr in track_dfs:
        line, = ax.plot([],[], linewidth=2, label=str(tr))
        marker, = ax.plot([],[], marker="o", markersize=7, linestyle="None")
        txt = ax.text(0,0,"",fontsize=8)
        arts[tr] = (line, marker, txt)
    time_text = ax.text(0.02,0.98,"", transform=ax.transAxes, va="top")
    ax.legend(fontsize=8)

    def update(t):
        for tr,g in track_dfs.items():
            hist = g[g["elapsed_s"] <= t]
            line, marker, txt = arts[tr]
            if len(hist):
                line.set_data(hist["fan_u_filt_m"], hist["fan_v_filt_m"])
                recent = hist.iloc[-1]
                if t - float(recent["elapsed_s"]) <= 1.5:
                    u, v = float(recent["fan_u_filt_m"]), float(recent["fan_v_filt_m"])
                    marker.set_data([u],[v])
                    label = str(tr)
                    if "target_label" in recent and pd.notna(recent["target_label"]):
                        label += f" {recent['target_label']}"
                    txt.set_position((u,v)); txt.set_text(label); txt.set_visible(True)
                else:
                    marker.set_data([],[]); txt.set_visible(False)
            else:
                line.set_data([],[]); marker.set_data([],[]); txt.set_visible(False)
        time_text.set_text(f"Elapsed: {t:.1f} s")
        return []

    ani = FuncAnimation(fig, update, frames=times, interval=80, blit=False, repeat=True)
    gif = out_dir / "tracks_animation.gif"
    ani.save(gif, writer=PillowWriter(fps=min(fps, 8)), dpi=80)
    plt.close(fig)
    return overview, gif
