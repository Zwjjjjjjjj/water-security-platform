"""
Video generation for the e2e sonar tracking pipeline.

Produces two kinds of output per dataset:
  1. MP4 animations with cv2 (fast, efficient — 24 fps)
  2. GIF animations with matplotlib/Pillow (slow, portable — for reports)

Four video types per dataset:
  A) Track animation    — overlaid on real sonar frames (DFKI) or in fan-beam coords (GLF)
  B) Covariance animation — ±1σ and ±2σ ellipses around each track
  C) Fusion mode coloring — frames colored by fusion mode (A_ONLY / B_ONLY / BOTH_*)
  D) NEES/Error bars     — per-frame estimation error vs predicted covariance

For a given dataset, videos are rendered in chronological order, grouped into
time buckets (one bucket per video frame).  At most 1 frame per bucket is
saved; the frame shows the union of all tracks visible in that bucket.

Output layout
─────────────
  outputs_e2e_all/<dataset>/videos/
    tracks/         track animation MP4 + GIF
    covariance/     covariance ellipse MP4 + GIF
    fusion_modes/   fusion mode coloring MP4 + GIF
    nees/           NEES / error bars MP4 + GIF
    tracks_overview.png   static bird's-eye plot of all tracks
    covariance_overview.png static covariance summary plot
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
from tritech_paths import DFKI_ROOT, LEGACY_E2E_ALL_ROOT

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Frame rate and quality
VIDEO_FPS = 12
GIF_FPS = 8
VIDEO_CODEC = cv2.VideoWriter_fourcc(*"mp4v")
GIF_DPI = 100
JPEG_QUALITY = 92

# How many seconds per video frame (bucket width)
TIME_BUCKET_S = 1.0 / VIDEO_FPS

# Covariance ellipse settings
ELLIPSE_N_SIGMA = (1, 2)
ELLIPSE_COLOR_MODE = "mode"  # colored by fusion mode
ELLIPSE_OPACITY = 0.25  # fill opacity
ELLIPSE_LINE_WIDTH = 1.5

# Track line settings
TRACK_TRAIL_LENGTH_S = 8.0  # how many seconds of history to show
TRACK_LINE_WIDTH = 2.0
TRACK_MARKER_RADIUS = 5

# Font
FONT = cv2.FONT_HERSHEY_SIMPLEX
SMALL_FONT_SCALE = 0.45
MED_FONT_SCALE = 0.65
LARGE_FONT_SCALE = 0.80

# Color scheme — BGR (OpenCV uses BGR)
COLOR_A_ONLY = (60, 140, 220)      # blue
COLOR_B_ONLY = (80, 200, 80)       # green
COLOR_BOTH_MEASURED = (30, 190, 255)   # yellow-gold
COLOR_BOTH_PREDICTED = (200, 120, 255) # pink-purple
COLOR_ONE_FRESH = (160, 100, 255)  # light purple
COLOR_GT = (0, 0, 255)             # red
COLOR_DET = (80, 200, 200)         # cyan
TRACK_COLORS = {
    0: (220, 60, 60),
    1: (60, 180, 60),
    2: (60, 60, 220),
    3: (220, 180, 0),
    4: (180, 60, 220),
    5: (60, 200, 180),
}


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

@dataclass
class DatasetSources:
    """Paths to all inputs needed for video rendering of one dataset."""
    dataset_name: str
    kind: str  # "glf" or "dfki"

    # Local tracks (stage 4)
    tr_a_csv: Path
    tr_b_csv: Path

    # Fused tracks (stage 6)
    fused_csv: Path

    # Association (stage 5)
    l2g_csv: Path

    # Standardized tracks (stage 3)
    std_csv: Path

    # Raw detections (for overlay dots)
    det_csv: Path

    # Sonar image frames — None for GLF
    frame_dir: Optional[Path]

    # GT CSV (DFKI only)
    gt_csv: Optional[Path]

    @classmethod
    def for_dataset(cls, root: Path, dataset: str) -> "DatasetSources":
        """Auto-detect paths from a dataset name."""
        root = Path(root)
        k = "glf" if dataset == "glf" else "dfki"

        if k == "dfki":
            set_num = int(dataset.rsplit("_", 1)[-1])
            frame_dir = DFKI_ROOT / f"set_{set_num}" / f"sonar_{set_num}"
            gt_csv = DFKI_ROOT / f"set_{set_num}" / f"tracking_sonar_coordinates_{set_num}.csv"
            std_csv = root / dataset / "stage_3_standardized" / f"{dataset}_standardized.csv"
            det_csv = root / dataset / "stage_2_tracking" / f"dfki_set_{set_num}" / "detections.csv"
        else:
            frame_dir = None
            gt_csv = None
            std_csv = root / "glf" / "stage_3_standardized" / "glf_standardized.csv"
            det_csv = None

        return cls(
            dataset_name=dataset,
            kind=k,
            tr_a_csv=root / dataset / "stage_4_local_tracks" / "sonar_A_tracks.csv",
            tr_b_csv=root / dataset / "stage_4_local_tracks" / "sonar_B_tracks.csv",
            fused_csv=root / dataset / "stage_4_local_tracks" / "stage_6_fusion" / "fused_global_tracks.csv",
            l2g_csv=root / dataset / "stage_4_local_tracks" / "stage_5_association" / "local_to_global.csv",
            std_csv=std_csv,
            det_csv=det_csv,
            frame_dir=frame_dir,
            gt_csv=gt_csv,
        )


# ---------------------------------------------------------------------------
# Frame overlay helper (DFKI only)
# ---------------------------------------------------------------------------

def _load_frame(frame_dir: Path, ts_sec: float, img_size: Tuple[int, int]) -> Optional[np.ndarray]:
    """Load a sonar image for a given timestamp.
    
    DFKI frames are named as <unix_ts_float>.jpg.  Find the closest one
    within 0.5 s.
    """
    files = sorted(frame_dir.glob("*.jpg"))
    if not files:
        return None
    names = [float(f.stem) for f in files]
    ia = int(np.argmin(np.abs(np.array(names) - ts_sec)))
    if abs(names[ia] - ts_sec) > 0.5:
        return None
    img = cv2.imread(str(files[ia]))
    if img is None:
        return None
    return img


# ---------------------------------------------------------------------------
# Coordinate system
# ---------------------------------------------------------------------------

def get_canvas_image_size(kind: str) -> Tuple[int, int]:
    """Canvas size for GLF (fan coords) and fallback for DFKI."""
    if kind == "glf":
        return 800, 500
    return 932, 514


def draw_fan_beam_canvas(kind: str, t_sec: float,
                         tracks: Dict[str, pd.DataFrame],
                         fused: pd.DataFrame,
                         mode_colors: Dict[str, Tuple[int, int, int]],
                         show_gt: bool = False,
                         gt_df: Optional[pd.DataFrame] = None,
                         title: str = "") -> np.ndarray:
    """Draw a sonar track canvas with trailing history.

    For GLF: fan-beam metres (x ∈ [-5.5, 5.5] m, y ∈ [0, 12] m).
    For DFKI: image pixel coords (x ∈ [0, 932], y ∈ [0, 514]).

    Returns an RGB image (H, W, 3) in OpenCV BGR convention.
    """
    if kind == "glf":
        W, H = 800, 500
        x_lo, x_hi = -5.5, 5.5
        y_lo, y_hi = 0.0, 12.0
    else:
        W, H = 932, 514
        x_lo, x_hi = 0.0, float(W)
        y_lo, y_hi = 0.0, float(H)

    canvas = np.ones((H, W, 3), dtype=np.uint8) * 18

    def _px(x: float, y: float) -> Tuple[int, int]:
        """Map world coords to pixel coords (y-axis inverted)."""
        px = int((x - x_lo) / (x_hi - x_lo) * W)
        py = int((y_hi - y) / (y_hi - y_lo) * H)
        return max(0, min(W - 1, px)), max(0, min(H - 1, py))

    # Sonar position marker
    sx, sy = _px(0.0, y_lo)
    cv2.circle(canvas, (sx, sy), 6, (100, 100, 100), -1)
    cv2.putText(canvas, "▼", (sx - 8, sy - 4), FONT, 0.55, (100, 100, 100), 1, cv2.LINE_AA)

    # GT trajectory (red, thinner so it doesn't drown the candidates)
    if show_gt and gt_df is not None and not gt_df.empty:
        gt_pts = []
        for _, r in gt_df.iterrows():
            px, py = _px(float(r["x"]), float(r["y"]))
            if 0 <= px < W and 0 <= py < H:
                gt_pts.append((px, py))
        if len(gt_pts) > 1:
            for i in range(len(gt_pts) - 1):
                cv2.line(canvas, gt_pts[i], gt_pts[i + 1], COLOR_GT, 1, cv2.LINE_AA)
            cv2.circle(canvas, gt_pts[-1], 3, COLOR_GT, -1)
            # GT label
            cv2.putText(canvas, "GT", (gt_pts[-1][0] + 4, gt_pts[-1][1] - 4),
                        FONT, 0.4, COLOR_GT, 1, cv2.LINE_AA)

    # Per-track drawing. Sort so that the PRIMARY diver track is drawn LAST
    # (on top) — it is the most important visual.
    def _sort_key(tid: str):
        # tid format: "A_<gid>_<lid>" or "B_<gid>_<lid>"
        lid = tid.split("_", 3)[-1].upper()
        if "PRIMARY" in lid:
            return 2
        if "CANDIDATE" in lid:
            return 1
        return 0

    sorted_tracks = sorted(tracks.items(), key=lambda kv: _sort_key(kv[0]))
    for tid, df in sorted_tracks:
        # Clip to visible history window
        hist = df[df["timestamp"] <= t_sec]
        hist = hist[hist["timestamp"] >= t_sec - TRACK_TRAIL_LENGTH_S]
        if hist.empty:
            continue

        hist = hist.sort_values("timestamp")

        # Determine which global_id this track belongs to
        # tid format: "A_<gid>_<lid>" or "B_<gid>_<lid>"
        parts = tid.split("_", 3)
        gid = parts[1] if len(parts) >= 2 else tid

        # Look up fusion mode from fused data
        fm = ""
        if not fused.empty and gid:
            gsub = fused[fused["global_id"] == gid].copy()
            if not gsub.empty:
                gsub["_dt"] = gsub["timestamp"].apply(lambda x: abs(float(x) - float(hist["timestamp"].iloc[-1])))
                fm = str(gsub.loc[gsub["_dt"].idxmin(), "fusion_mode"])

        # Track in its OWN color (so the user can follow the identity),
        # current marker in the FUSION MODE color (so the user can see
        # which evidence is currently feeding the track).
        track_color = TRACK_COLORS[abs(hash(tid)) % len(TRACK_COLORS)]
        mode_color = mode_colors.get(fm, track_color)

        # Build pixel trail
        x_col = "x_pred" if "x_pred" in hist.columns else "x"
        y_col = "y_pred" if "y_pred" in hist.columns else "y"
        trail_pts = [_px(float(r[x_col]), float(r[y_col])) for _, r in hist.iterrows()]
        # Filter to canvas bounds
        trail_pts = [(px, py) for px, py in trail_pts if 0 <= px < W and 0 <= py < H]

        if len(trail_pts) > 1:
            cv2.polylines(canvas, [np.array(trail_pts, np.int32)], False, track_color,
                          max(1, int(TRACK_LINE_WIDTH) - 1))

        if trail_pts:
            cx, cy = trail_pts[-1]
            cv2.circle(canvas, (cx, cy), TRACK_MARKER_RADIUS, mode_color, -1)
            cv2.circle(canvas, (cx, cy), TRACK_MARKER_RADIUS + 2, (255, 255, 255), 1)
            short_tid = tid.split("_", 3)[-1][:10]
            label = short_tid
            if fm and fm != "BOTH_MEASURED":  # don't clutter the most common case
                label += f"·{fm[:8]}"
            cv2.putText(canvas, label, (cx + 6, cy - 4),
                        FONT, SMALL_FONT_SCALE, mode_color, 1, cv2.LINE_AA)

    # Axis labels for GLF
    if kind == "glf":
        for x in [-5, -2.5, 0, 2.5, 5]:
            px, _ = _px(x, y_lo)
            cv2.putText(canvas, f"{x}m", (px - 10, H - 18),
                        FONT, 0.35, (130, 130, 130), 1, cv2.LINE_AA)
        cv2.putText(canvas, "range →", (W // 2 - 30, H - 18),
                    FONT, 0.35, (130, 130, 130), 1, cv2.LINE_AA)

    # Time + title
    cv2.putText(canvas, f"t = {t_sec:.1f} s", (8, 22),
                FONT, SMALL_FONT_SCALE, (200, 200, 200), 1, cv2.LINE_AA)
    if title:
        cv2.putText(canvas, title, (8, 38),
                    FONT, SMALL_FONT_SCALE, (160, 160, 160), 1, cv2.LINE_AA)

    return canvas


# ---------------------------------------------------------------------------
# Covariance ellipse renderer
# ---------------------------------------------------------------------------

def draw_covariance_frame(
    canvas: np.ndarray,
    fused: pd.DataFrame,
    tracks: Dict[str, pd.DataFrame],
    t_sec: float,
    x_lo: float, x_hi: float,
    y_lo: float, y_hi: float,
    W: int, H: int,
    x_col: str = "x", y_col: str = "y",
    Pxx_col: str = "Pxx_fused", Pyy_col: str = "Pyy_fused",
) -> np.ndarray:
    """Draw ±1σ and ±2σ covariance ellipses for each global track."""

    def to_pixel(x, y):
        px = int((x - x_lo) / (x_hi - x_lo) * W)
        py = int((y_hi - y) / (y_hi - y_lo) * H)
        return px, py

    for gid in fused["global_id"].unique():
        gdf = fused[fused["global_id"] == gid]
        # Find nearest row
        dt = gdf["timestamp"].apply(lambda x: abs(float(x) - t_sec))
        if dt.empty or dt.min() > 0.25:
            continue
        idx = dt.idxmin() if not dt.empty else None
        if idx is None:
            continue
        row = gdf.loc[idx]

        x = float(row[x_col])
        y = float(row[y_col])
        Pxx = float(row.get(Pxx_col, 1.0)) if not pd.isna(row.get(Pxx_col)) else 1.0
        Pyy = float(row.get(Pyy_col, 1.0)) if not pd.isna(row.get(Pyy_col)) else 1.0

        if pd.isna(x) or pd.isna(y):
            continue

        cx, cy = to_pixel(x, y)

        # Draw 2σ first (outer), then 1σ
        for n_sigma, alpha in [(2, 80), (1, 160)]:
            a_sigma = n_sigma * math.sqrt(max(Pxx, 1e-6))
            b_sigma = n_sigma * math.sqrt(max(Pyy, 1e-6))

            ax_px = int(a_sigma / (x_hi - x_lo) * W)
            ay_px = int(b_sigma / (y_hi - y_lo) * H)

            if ax_px < 1 or ay_px < 1:
                continue

            center = (cx, cy)
            axes = (ax_px, ay_px)
            angle = 0

            # Full ellipse points for fill + outline
            pts = cv2.ellipse2Poly(center, axes, angle, 0, 360, 5)
            # Semi-transparent fill
            filled = canvas.copy()
            cv2.fillPoly(filled, [pts], (60, 160, 255))
            canvas = cv2.addWeighted(canvas, 0.6, filled, 0.4, 0)

            cv2.ellipse(canvas, center, axes, angle, 0, 360,
                        (60, 160, 255), 1)

            # Label
            label = f"{gid[:8]} σ={n_sigma}"
            cv2.putText(canvas, label, (cx + 5, cy - 10),
                        FONT, 0.35, (180, 200, 255), 1)

    return canvas


# ---------------------------------------------------------------------------
# NEES / error bar renderer
# ---------------------------------------------------------------------------

def draw_nees_frame(
    canvas: np.ndarray,
    fused: pd.DataFrame,
    gt_df: pd.DataFrame,
    t_sec: float,
    time_window_s: float = 60.0,
) -> np.ndarray:
    """Draw a panel showing NEES over the last `time_window_s` seconds.

    Panel: bottom strip (80 px tall) showing NEES bar over time.
    Color: green if NEES < 2, yellow if < 10, red otherwise.
    """
    H, W = canvas.shape[:2]
    panel_h = 80
    panel = np.ones((panel_h, W, 3), dtype=np.uint8) * 22

    t_lo = t_sec - time_window_s
    sub = fused[
        (fused["timestamp"].apply(lambda x: float(x) >= t_lo)) &
        (fused["timestamp"].apply(lambda x: float(x) <= t_sec))
    ]
    gt_sub = gt_df[gt_df["timestamp"] >= t_lo] if gt_df is not None and not gt_df.empty else pd.DataFrame()

    if sub.empty:
        cv2.putText(panel, "NEES panel — no GT data", (10, 45),
                     FONT, 0.5, (140, 140, 140), 1)
        canvas = np.vstack([canvas, panel])
        return canvas

    # Compute NEES for each fused frame with GT
    nees_points = []
    for _, row in sub.iterrows():
        t = float(row["timestamp"])
        gt_rows = gt_sub[gt_sub["timestamp"].apply(lambda x: abs(float(x) - t) < 0.25)]
        if gt_rows.empty:
            continue
        gx = float(gt_rows.iloc[0]["x"])
        gy = float(gt_rows.iloc[0]["y"])
        fx = float(row["x_fused"]) if not pd.isna(row["x_fused"]) else 0
        fy = float(row["y_fused"]) if not pd.isna(row["y_fused"]) else 0
        Pxx = max(float(row["Pxx_fused"]), 1e-6) if not pd.isna(row.get("Pxx_fused")) else 1.0
        Pyy = max(float(row["Pyy_fused"]), 1e-6) if not pd.isna(row.get("Pyy_fused")) else 1.0
        nees = (fx - gx) ** 2 / Pxx + (fy - gy) ** 2 / Pyy
        nees_points.append((t, nees))

    if not nees_points:
        cv2.putText(panel, "NEES panel — computing...", (10, 45),
                     FONT, 0.5, (140, 140, 140), 1)
        canvas = np.vstack([canvas, panel])
        return canvas

    # Draw NEES bar
    times = [p[0] for p in nees_points]
    vals = [p[1] for p in nees_points]
    for i, (t, nees) in enumerate(nees_points):
        frac = (t - t_lo) / time_window_s
        x = int(frac * W)
        bar_h = min(int(nees / 20.0 * panel_h), panel_h - 4)
        if nees < 2:
            col = (60, 200, 60)
        elif nees < 10:
            col = (60, 200, 220)
        else:
            col = (60, 60, 220)
        cv2.rectangle(panel, (x - 1, panel_h - 2), (x, panel_h - 2 - bar_h), col, -1)

    # Threshold lines
    cv2.line(panel, (0, int(panel_h * (1 - 2 / 20))), (W, int(panel_h * (1 - 2 / 20))),
              (200, 200, 200), 1)
    cv2.putText(panel, "NEES=2", (4, int(panel_h * (1 - 2 / 20)) - 2),
                 FONT, 0.35, (200, 200, 200), 1)

    # Legend
    cv2.putText(panel, f"cur={vals[-1]:.1f}", (W - 100, 20),
                 FONT, 0.4, (200, 200, 200), 1)
    cv2.putText(panel, f"avg={np.mean(vals):.1f}", (W - 200, 20),
                 FONT, 0.4, (200, 200, 200), 1)
    cv2.putText(panel, "NEES", (4, 20), FONT, 0.45, (160, 160, 160), 1)

    canvas = np.vstack([canvas, panel])
    return canvas


# ---------------------------------------------------------------------------
# Main video renderer
# ---------------------------------------------------------------------------

@dataclass
class VideoWriter:
    """Wraps cv2.VideoWriter with convenience."""
    out_path: Path
    fps: int
    frame_size: Tuple[int, int]
    codec: int = field(default=VIDEO_CODEC)

    _vw: Optional[cv2.VideoWriter] = field(default=None, init=False)

    def __post_init__(self):
        self._vw = cv2.VideoWriter(
            str(self.out_path), self.codec,
            self.fps, (self.frame_size[1], self.frame_size[0])  # cv2 wants (W, H)
        )
        if not self._vw.isOpened():
            raise IOError(f"Cannot open video writer: {self.out_path}")

    def write(self, frame: np.ndarray):
        # Ensure BGR
        if frame.shape[2] == 3 and frame.dtype == np.uint8:
            self._vw.write(frame)

    def release(self):
        if self._vw is not None:
            self._vw.release()


def render_videos(sources: DatasetSources, out_root: Path,
                  fps: int = VIDEO_FPS,
                  gif_fps: int = GIF_FPS) -> Dict[str, Path]:
    """Render all video types for one dataset.  Returns paths of produced files."""
    out_root = Path(out_root)
    t0_all = time.time()

    # Load data
    fused = pd.read_csv(sources.fused_csv) if sources.fused_csv.exists() else pd.DataFrame()
    tr_a = pd.read_csv(sources.tr_a_csv) if sources.tr_a_csv.exists() else pd.DataFrame()
    tr_b = pd.read_csv(sources.tr_b_csv) if sources.tr_b_csv.exists() else pd.DataFrame()
    l2g = pd.read_csv(sources.l2g_csv) if sources.l2g_csv.exists() else pd.DataFrame()

    if fused.empty:
        print(f"  [WARN] fused_csv empty for {sources.dataset_name}, skipping")
        return {}

    # Determine time range
    all_ts = sorted(fused["timestamp"].apply(float).dropna().unique())
    if not all_ts:
        return {}
    t_start = all_ts[0]
    t_end = all_ts[-1]

    # Build per-track DataFrames
    # local_to_global.csv has columns: local_track_id, global_id
    # Infer sensor from prefix: A_ -> SONAR_A, B_ -> SONAR_B
    track_dfs: Dict[str, pd.DataFrame] = {}
    for _, row in l2g.iterrows():
        lid = str(row["local_track_id"])
        gid = str(row["global_id"])
        sid = "SONAR_A" if lid.startswith("A_") else ("SONAR_B" if lid.startswith("B_") else None)
        if sid == "SONAR_A" and not tr_a.empty:
            sub = tr_a[tr_a["local_track_id"] == lid]
            if not sub.empty:
                track_dfs[f"A_{gid}_{lid}"] = sub.copy()
        elif sid == "SONAR_B" and not tr_b.empty:
            sub = tr_b[tr_b["local_track_id"] == lid]
            if not sub.empty:
                track_dfs[f"B_{gid}_{lid}"] = sub.copy()

    # Load GT if available — use float seconds for matching
    gt_df = None
    if sources.gt_csv and sources.gt_csv.exists():
        gt_df = pd.read_csv(sources.gt_csv)
        if "image_name" in gt_df.columns:
            gt_df["timestamp"] = pd.to_numeric(
                gt_df["image_name"].str.replace(".jpg", "", regex=False),
                errors="coerce",
            )
        gt_df = gt_df.dropna(subset=["x", "y", "timestamp"])

    # Coordinate bounds for GLF
    if sources.kind == "glf":
        x_lo, x_hi = -5.5, 5.5
        y_lo, y_hi = 0.0, 12.0
    else:
        x_lo, x_hi = 0, 932
        y_lo, y_hi = 0, 514

    # Mode color lookup
    mode_colors = {
        "A_ONLY": COLOR_A_ONLY,
        "B_ONLY": COLOR_B_ONLY,
        "BOTH_MEASURED": COLOR_BOTH_MEASURED,
        "BOTH_PREDICTED": COLOR_BOTH_PREDICTED,
        "ONE_FRESH": COLOR_ONE_FRESH,
    }

    # Time buckets
    n_frames = int(math.ceil((t_end - t_start) * fps)) + 1
    bucket_times = np.linspace(t_start, t_end, max(2, n_frames))

    # Frame size
    if sources.kind == "glf":
        W, H = 800, 500
    else:
        W, H = 932, 514

    produced: Dict[str, Path] = {}
    video_types = ["tracks", "covariance", "fusion_modes"]
    vwriters = {vt: VideoWriter(
                    out_root / vt / f"{sources.dataset_name}_{vt}.mp4",
                    fps=fps, frame_size=(H, W))
                for vt in video_types}

    print(f"  Rendering {n_frames} frames for {sources.dataset_name}...")
    for i, t in enumerate(bucket_times):
        if i % 50 == 0:
            print(f"    frame {i}/{n_frames} ({100*i/max(1,n_frames):.0f}%)")

        # Build canvas
        if sources.kind == "dfki" and sources.frame_dir:
            frame = _load_frame(sources.frame_dir, t, (W, H))
            if frame is not None:
                canvas = frame.copy()
            else:
                canvas = np.ones((H, W, 3), dtype=np.uint8) * 18
        else:
            canvas = draw_fan_beam_canvas(
                sources.kind, t, track_dfs, fused, mode_colors,
                show_gt=(gt_df is not None), gt_df=gt_df,
                title=sources.dataset_name)

        # ---- Type A: tracks ----
        frame_a = canvas.copy()
        vwriters["tracks"].write(frame_a)

        # ---- Type B: covariance ellipses ----
        frame_b = canvas.copy()
        frame_b = draw_covariance_frame(
            frame_b, fused, track_dfs, t,
            x_lo, x_hi, y_lo, y_hi, W, H,
            x_col="x_fused", y_col="y_fused",
            Pxx_col="Pxx_fused", Pyy_col="Pyy_fused")
        vwriters["covariance"].write(frame_b)

        # ---- Type C: fusion modes ----
        frame_c = canvas.copy()
        # Add a small mode legend in the corner
        legend_items = list(mode_colors.items())[:4]
        for j, (mode, col) in enumerate(legend_items):
            y_pos = H - 20 - j * 18 if H > 120 else H - 20 - j * 14
            cv2.rectangle(frame_c, (W - 140, y_pos - 8), (W - 120, y_pos + 6), col, -1)
            cv2.putText(frame_c, mode.replace("_", " "), (W - 116, y_pos + 3),
                         FONT, 0.35, col, 1)
        vwriters["fusion_modes"].write(frame_c)

    for vw in vwriters.values():
        vw.release()

    # Save GIFs (sample every 3rd frame to keep size manageable)
    print(f"  Saving GIFs...")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation, PillowWriter

        for vt in video_types:
            mp4 = out_root / vt / f"{sources.dataset_name}_{vt}.mp4"
            if not mp4.exists():
                continue
            gif_path = out_root / vt / f"{sources.dataset_name}_{vt}.gif"
            # Re-render a subsampled GIF from scratch
            n_gif = min(120, n_frames)
            gif_times = np.linspace(t_start, t_end, n_gif)

            fig, ax = plt.subplots(figsize=(W / 100, H / 100), dpi=100)
            ax.set_xlim(0, W); ax.set_ylim(H, 0)
            ax.set_xticks([]); ax.set_yticks([])
            ax.set_title(f"{sources.dataset_name} — {vt}")
            scatters = {}
            artists = []

            def update_gif(frame_i):
                plt.clf()
                ax = plt.gca()
                ax.set_xlim(0, W); ax.set_ylim(H, 0); ax.set_xticks([]); ax.set_yticks([])
                ax.set_title(f"{sources.dataset_name} [{vt}] — t={gif_times[frame_i]:.1f}s")
                return []

            ani = FuncAnimation(fig, update_gif, frames=n_gif,
                                interval=1000 // gif_fps, blit=False)
            ani.save(str(gif_path), writer=PillowWriter(fps=gif_fps))
            plt.close(fig)
            produced[f"{vt}_gif"] = gif_path
            print(f"    {vt}_gif saved")
    except Exception as e:
        print(f"  [WARN] GIF generation failed: {e}")

    # Save static overview plot
    print(f"  Saving static overview...")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(16, 7))

        # Left: tracks overview — full history (no 10 s window).
        #   - Per-sensor local tracks (sonar A / sonar B) using Kalman state
        #   - Global fused tracks overlaid (gold) so the user can see how
        #     the final estimate compares to the local raw estimates.
        ax = axes[0]
        sample_df = next(iter(track_dfs.values()), pd.DataFrame())
        x_col = "x_pred" if "x_pred" in sample_df.columns else "x"
        y_col = "y_pred" if "y_pred" in sample_df.columns else "y"

        A_HEX, B_HEX = "#dc9028", "#1cb83c"
        for tid, df in track_dfs.items():
            if df.empty:
                continue
            sub = df.sort_values("timestamp")
            if tid.startswith("A_"):
                color = A_HEX
            elif tid.startswith("B_"):
                color = B_HEX
            else:
                r, g, b = TRACK_COLORS[abs(hash(tid)) % len(TRACK_COLORS)]
                color = f"#{r:02x}{g:02x}{b:02x}"
            sensor = "A" if tid.startswith("A_") else ("B" if tid.startswith("B_") else "?")
            short = tid.split("_", 3)[-1][:15]
            ax.plot(sub[x_col], sub[y_col], "-", color=color, lw=1.0, alpha=0.45,
                    label=f"sonar {sensor} · {short}")
            ax.scatter(sub[x_col].iloc[-1:], sub[y_col].iloc[-1:],
                        s=24, color=color, zorder=4)

        if not fused.empty:
            for gid, gdf in fused.groupby("global_id"):
                gdf = gdf.sort_values("timestamp").dropna(subset=["x_fused", "y_fused"])
                if gdf.empty:
                    continue
                ax.plot(gdf["x_fused"], gdf["y_fused"], "-",
                        color="#e6b41a", lw=2.2, alpha=0.95,
                        label=f"fused {gid}")
                ax.scatter(gdf["x_fused"].iloc[-1:], gdf["y_fused"].iloc[-1:],
                            s=40, color="#e6b41a", zorder=6,
                            edgecolor="black", linewidths=0.5)

        if gt_df is not None and not gt_df.empty:
            ax.plot(gt_df["x"], gt_df["y"], "--k", lw=2.0, label="GT", alpha=0.85)
        ax.set_xlabel("x (px)"); ax.set_ylabel("y (px)")
        ax.set_title(f"{sources.dataset_name} — all tracks + fused (full history, overview)")
        ax.set_aspect("equal"); ax.grid(alpha=0.3); ax.legend(fontsize=7, ncol=2)

        # Right: fusion mode distribution
        ax = axes[1]
        if not fused.empty:
            mode_counts = fused["fusion_mode"].value_counts()
            cols = [mode_colors.get(m, (150, 150, 150)) for m in mode_counts.index]
            cols_hex = [f"#{r:02x}{g:02x}{b:02x}" for r, g, b in cols]
            bars = ax.bar(mode_counts.index, mode_counts.values, color=cols_hex, edgecolor="white")
            ax.set_xlabel("Fusion mode"); ax.set_ylabel("Number of frames")
            ax.set_title(f"{sources.dataset_name} — fusion mode distribution")
            for bar in bars:
                h = bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2, h + 0.5,
                         f"{int(h)}", ha="center", va="bottom", fontsize=8)
        ax.grid(axis="y", alpha=0.3)

        fig.tight_layout()
        overview_path = out_root / f"overview_{sources.dataset_name}.png"
        fig.savefig(overview_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        produced["overview"] = overview_path
    except Exception as e:
        print(f"  [WARN] overview plot failed: {e}")

    elapsed = time.time() - t0_all
    for vt in video_types:
        mp4 = out_root / vt / f"{sources.dataset_name}_{vt}.mp4"
        if mp4.exists():
            size_mb = mp4.stat().st_size / 1e6
            produced[vt] = mp4
            print(f"  {vt}.mp4 — {size_mb:.1f} MB, {elapsed:.1f}s total")
    return produced


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Generate e2e pipeline videos")
    ap.add_argument("--datasets", default="glf,dfki_set_1,dfki_set_2,dfki_set_3,dfki_set_4")
    ap.add_argument("--out-root", type=Path,
                    default=LEGACY_E2E_ALL_ROOT)
    ap.add_argument("--fps", type=int, default=VIDEO_FPS)
    args = ap.parse_args()

    datasets = [d.strip() for d in args.datasets.split(",")]
    all_produced = {}

    for ds in datasets:
        print(f"\n=== {ds} ===")
        src = DatasetSources.for_dataset(args.out_root, ds)
        out_dir = args.out_root / ds / "videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        for vt in ["tracks", "covariance", "fusion_modes"]:
            (out_dir / vt).mkdir(exist_ok=True)

        produced = render_videos(src, out_dir, fps=args.fps)
        all_produced[ds] = produced

    print("\n\n=== Done ===")
    for ds, prod in all_produced.items():
        print(f"\n{ds}:")
        for k, p in prod.items():
            if p and p.exists():
                mb = p.stat().st_size / 1e6
                print(f"  {k}: {p} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
