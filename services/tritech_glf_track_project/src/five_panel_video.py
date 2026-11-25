"""
Five-element comparison video:
  GT + Sonar A + Sonar B + Fusion + 95% covariance ellipse

Layout — 5 vertical panels, all on the same time axis, rendered at 12 fps:

  ┌────────────────────────────┐  ┌────────────────────────────┐
  │  1. Ground Truth           │  │  2. Sonar A (real)         │
  │  red ●●                    │  │  cyan ●●                   │
  └────────────────────────────┘  └────────────────────────────┘
  ┌────────────────────────────┐  ┌────────────────────────────┐
  │  3. Sonar B (synthetic)    │  │  4. Fusion (G001..G_N)     │
  │  green ●●                  │  │  gold ●● + mode tag        │
  └────────────────────────────┘  └────────────────────────────┘
  ┌────────────────────────────────────────────────────────────┐
  │  5. 95% covariance ellipse on the fused state              │
  │     χ²(2, 0.95) ≈ 6.0; ellipse = √(6)·√Pxx, √(6)·√Pyy     │
  └────────────────────────────────────────────────────────────┘

Coordinate frame: a single shared `(x, y)` plane (image pixels for DFKI,
metres for GLF).  Each panel uses the same `(x_lo, x_hi) × (y_lo, y_hi)`
extents so the user can directly compare the five estimates.

The fusion panel also draws a translucent fan (α=0.3) that fades the
ellipse out beyond the current covariance radius — that way it is obvious
when the filter is highly confident vs when it is hallucinating.

For the GLF dataset there is no per-frame GT, so the top-left panel is
replaced with an "event labels" panel (target_label vs event timestamp).

CLI:
  python src/five_panel_video.py --datasets dfki_set_2
  python src/five_panel_video.py --datasets glf
  python src/five_panel_video.py --datasets dfki_set_1,dfki_set_2,glf
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
import pandas as pd
from tritech_paths import DFKI_ROOT, GLF_PROFILE, LEGACY_E2E_ALL_ROOT

# =========================================================================
# Constants
# =========================================================================
FPS = 12
VIDEO_CODEC = cv2.VideoWriter_fourcc(*"mp4v")
PANEL_W = 480           # per-panel pixel width
PANEL_H = 360           # per-panel pixel height
TITLE_BAR = 28
PAD = 8                 # outer padding
LINE = 18               # panel spacing
BG = (18, 18, 24)       # dark canvas
PANEL_BG = (245, 246, 250)
GRID = (210, 215, 225)
AXIS = (90, 95, 105)
TEXT = (40, 42, 50)
SUBTEXT = (110, 115, 125)

GT_COLOR = (40, 40, 220)         # red (BGR)
A_COLOR = (220, 170, 40)         # cyan-blue (BGR) — sonar A
B_COLOR = (40, 200, 60)          # green — sonar B
FUSION_COLOR = (20, 180, 220)    # gold — fused
ELLIPSE_FILL = (40, 180, 220)    # 95% ellipse fill
ELLIPSE_EDGE = (20, 130, 200)    # ellipse outline

FONT = cv2.FONT_HERSHEY_SIMPLEX

# χ²(2, 0.95) = 5.991  → scale = sqrt(5.991) ≈ 2.447
CHI2_95 = 5.991
SCALE_95 = math.sqrt(CHI2_95)

# How many seconds of history to show in each panel
TRAIL_S = 10.0

# Track history color, mode-dependent
FUSION_MODE_COLOR = {
    "A_ONLY":          (220, 170, 40),
    "B_ONLY":          (40, 200, 60),
    "BOTH_MEASURED":   (20, 180, 220),
    "BOTH_PREDICTED":  (200, 130, 220),
    "ONE_FRESH":       (200, 160, 60),
}


# =========================================================================
# Data loading
# =========================================================================
@dataclass
class DatasetSources:
    dataset_name: str
    kind: str               # "dfki" or "glf"
    tr_a_csv: Path
    tr_b_csv: Path
    meas_a_csv: Path
    meas_b_csv: Path
    fused_csv: Path
    gt_csv: Optional[Path]  # only DFKI
    frame_dir: Optional[Path]
    glf_profile: Optional[Path]

    @classmethod
    def for_dataset(cls, root: Path, dataset: str) -> "DatasetSources":
        root = Path(root)
        if dataset == "glf":
            return cls(
                dataset_name="glf", kind="glf",
                tr_a_csv=root / "glf" / "stage_4_local_tracks" / "sonar_A_tracks.csv",
                tr_b_csv=root / "glf" / "stage_4_local_tracks" / "sonar_B_tracks.csv",
                meas_a_csv=root / "glf" / "stage_4_local_tracks" / "sonar_A_measurements.csv",
                meas_b_csv=root / "glf" / "stage_4_local_tracks" / "sonar_B_measurements.csv",
                fused_csv=root / "glf" / "stage_4_local_tracks" / "stage_6_fusion" / "fused_global_tracks.csv",
                gt_csv=None,
                frame_dir=None,
                glf_profile=GLF_PROFILE,
            )
        set_num = int(dataset.rsplit("_", 1)[-1])
        return cls(
            dataset_name=dataset, kind="dfki",
            tr_a_csv=root / dataset / "stage_4_local_tracks" / "sonar_A_tracks.csv",
            tr_b_csv=root / dataset / "stage_4_local_tracks" / "sonar_B_tracks.csv",
            meas_a_csv=root / dataset / "stage_4_local_tracks" / "sonar_A_measurements.csv",
            meas_b_csv=root / dataset / "stage_4_local_tracks" / "sonar_B_measurements.csv",
            fused_csv=root / dataset / "stage_4_local_tracks" / "stage_6_fusion" / "fused_global_tracks.csv",
            gt_csv=DFKI_ROOT / f"set_{set_num}" / f"tracking_sonar_coordinates_{set_num}.csv",
            frame_dir=DFKI_ROOT / f"set_{set_num}" / f"sonar_{set_num}",
            glf_profile=None,
        )


def _load_gt(gt_csv: Path) -> pd.DataFrame:
    """DFKI: image_name → unix seconds."""
    gt = pd.read_csv(gt_csv)
    if "image_name" in gt.columns:
        gt["t"] = pd.to_numeric(
            gt["image_name"].str.replace(".jpg", "", regex=False),
            errors="coerce",
        )
    elif "timestamp" in gt.columns:
        gt["t"] = pd.to_numeric(gt["timestamp"], errors="coerce")
    else:
        gt["t"] = np.arange(len(gt), dtype=float)
    return gt.dropna(subset=["t", "x", "y"]).sort_values("t").reset_index(drop=True)


def _load_glf_events(profile: Path) -> pd.DataFrame:
    """GLF: events with (label, event_time_utc, expected ranges)."""
    if not profile.exists():
        return pd.DataFrame()
    p = json.loads(profile.read_text())
    rows = []
    for ev in p.get("events", []):
        rows.append({
            "label": ev.get("label", "?"),
            "t": pd.to_datetime(ev.get("event_time_utc"), utc=True,
                                errors="coerce").timestamp()
                if pd.to_datetime(ev.get("event_time_utc"), utc=True,
                                  errors="coerce") is not pd.NaT
                else np.nan,
            "range_lo": ev.get("expected_min_range_m", np.nan),
            "range_hi": ev.get("expected_max_range_m", np.nan),
            "bearing_lo": ev.get("expected_min_bearing_deg", np.nan),
            "bearing_hi": ev.get("expected_max_bearing_deg", np.nan),
        })
    return pd.DataFrame(rows)


def _xy_cols(df: pd.DataFrame) -> Tuple[str, str]:
    """Resolve the (x, y) column pair for a given CSV flavour.

    Priority: x_pred / y_pred (Kalman state) over x / y (raw measurement).
    The Panel 2/3 trails are documented as "Kalman-filtered state (smooth
    line)" — the x/y columns in the stage_4 tracks CSV are *measurement*
    positions, not the filtered state, so they preserve outlier 30+ px
    jumps.  The Kalman state (x_pred, y_pred) is what the tracker actually
    used for downstream fusion; using it here eliminates the jagged
    polyline artefacts.
    """
    if "x_common" in df.columns and "y_common" in df.columns:
        return "x_common", "y_common"
    if "x_fused" in df.columns and "y_fused" in df.columns:
        return "x_fused", "y_fused"
    if "x_pred" in df.columns and "y_pred" in df.columns:
        return "x_pred", "y_pred"
    if "x" in df.columns and "y" in df.columns:
        return "x", "y"
    raise KeyError("no (x, y) columns found")


def _P_cols(df: pd.DataFrame) -> Tuple[str, str]:
    if "Pxx_fused" in df.columns and "Pyy_fused" in df.columns:
        return "Pxx_fused", "Pyy_fused"
    if "Pxx" in df.columns and "Pyy" in df.columns:
        return "Pxx", "Pyy"
    return "Pxx_fused", "Pyy_fused"


# =========================================================================
# Coordinate extents
# =========================================================================
def _shared_extent(
    tr_a: pd.DataFrame, tr_b: pd.DataFrame, fused: pd.DataFrame, gt: Optional[pd.DataFrame]
) -> Tuple[float, float, float, float]:
    """Shared (x_lo, x_hi, y_lo, y_hi) for all five panels.

    Image y axis is *downward*, so we deliberately invert y so that the
    first GT point (top of image) appears at the TOP of the panel.
    """
    xs, ys = [], []
    for d in (tr_a, tr_b, fused, gt if gt is not None else pd.DataFrame()):
        if d.empty:
            continue
        xc, yc = _xy_cols(d)
        xs.append(d[xc].to_numpy()); ys.append(d[yc].to_numpy())
    if not xs:
        return 0, 1, 0, 1
    x_all = np.concatenate(xs); y_all = np.concatenate(ys)
    x_lo, x_hi = float(np.nanmin(x_all)) - 20, float(np.nanmax(x_all)) + 20
    y_lo, y_hi = float(np.nanmax(y_all)) + 20, float(np.nanmin(y_all)) - 20  # inverted
    span = max(x_hi - x_lo, y_hi - y_lo, 50)
    cx = (x_lo + x_hi) / 2
    cy = (y_lo + y_hi) / 2
    return cx - span / 2, cx + span / 2, cy - span / 2, cy + span / 2


def _world_to_panel(x: float, y: float, ext: Tuple[float, float, float, float],
                    W: int, H: int) -> Tuple[int, int]:
    x_lo, x_hi, y_lo, y_hi = ext
    px = int((x - x_lo) / (x_hi - x_lo) * W)
    # y axis is inverted in our world frame
    py = int((y - y_lo) / (y_hi - y_lo) * H)
    return px, py


# =========================================================================
# Panel drawing
# =========================================================================
def _new_panel(title: str, width: int = PANEL_W) -> np.ndarray:
    """Create a fresh (W, H, 3) panel with a title bar and a grid."""
    panel = np.full((PANEL_H, width, 3), 255, dtype=np.uint8)
    # Title bar
    cv2.rectangle(panel, (0, 0), (width, TITLE_BAR), (28, 32, 50), -1)
    cv2.putText(panel, title, (8, 19), FONT, 0.5, (240, 240, 245), 1, cv2.LINE_AA)
    # Grid (8 vertical, 6 horizontal divisions)
    for i in range(1, 9):
        gx = int(i * width / 8)
        cv2.line(panel, (gx, TITLE_BAR), (gx, PANEL_H - 1), GRID, 1)
    for j in range(1, 7):
        gy = TITLE_BAR + int(j * (PANEL_H - TITLE_BAR) / 6)
        cv2.line(panel, (0, gy), (width - 1, gy), GRID, 1)
    # Axis frame
    cv2.rectangle(panel, (0, TITLE_BAR), (width - 1, PANEL_H - 1), AXIS, 1)
    return panel


def _draw_trail(panel: np.ndarray, df: pd.DataFrame, t_now: float,
                ext: Tuple[float, float, float, float],
                color: Tuple[int, int, int], col_x: str, col_y: str,
                radius: int = 3, thickness: int = 2, fade: bool = True,
                max_age_s: Optional[float] = TRAIL_S) -> None:
    """Draw a fading trail of (x, y) points where (t_now - max_age_s) <= timestamp <= t_now.

    Pass `max_age_s=None` to draw the full history (no time window).
    """
    H, W = panel.shape[:2]
    if df.empty:
        return
    sub = df[df["timestamp"].astype(float) <= t_now]
    if sub.empty:
        return
    # ── time-window truncation (max_age_s seconds of history) ──────────────
    if max_age_s is not None:
        sub = sub[sub["timestamp"].astype(float) >= t_now - max_age_s]
    if sub.empty:
        return
    sub = sub.sort_values("timestamp")
    # Break drawing when the time gap between consecutive points exceeds
    # 2× the nominal update interval (0.2 s → 0.4 s).  Gaps this large
    # indicate a missed-detection / re-acquisition event.
    MAX_GAP = 0.40  # seconds
    pts = [_world_to_panel(float(r[col_x]), float(r[col_y]), ext, W, H)
           for _, r in sub.iterrows()]
    prev_ts = None
    for i in range(1, len(pts)):
        curr_ts = float(sub.iloc[i]["timestamp"])
        gap = (curr_ts - prev_ts) if prev_ts is not None else 0.0
        if gap > MAX_GAP:
            # Just start a fresh segment — skip the line for this gap
            pass
        else:
            if fade:
                alpha = 0.4 + 0.6 * (i / max(1, len(pts) - 1))
                col = tuple(int(c * alpha) for c in color)
            else:
                col = color
            cv2.line(panel, pts[i - 1], pts[i], col, thickness, cv2.LINE_AA)
        prev_ts = curr_ts
    if pts:
        cv2.circle(panel, pts[-1], radius + 2, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.circle(panel, pts[-1], radius, color, -1, cv2.LINE_AA)


def _draw_ellipse(panel: np.ndarray, x: float, y: float, Pxx: float, Pyy: float,
                  ext: Tuple[float, float, float, float], label: str = "",
                  color_fill: Tuple[int, int, int] = ELLIPSE_FILL,
                  color_edge: Tuple[int, int, int] = ELLIPSE_EDGE) -> None:
    """95% covariance ellipse centered at (x, y)."""
    H, W = panel.shape[:2]
    if Pxx <= 0 or Pyy <= 0 or math.isnan(Pxx) or math.isnan(Pyy):
        return
    a = SCALE_95 * math.sqrt(Pxx)
    b = SCALE_95 * math.sqrt(Pyy)
    if a < 0.5 or b < 0.5:
        return
    cx, cy = _world_to_panel(x, y, ext, W, H)
    a_px = max(1, int(a / (ext[1] - ext[0]) * W))
    b_px = max(1, int(b / (ext[3] - ext[2]) * H))
    overlay = panel.copy()
    cv2.ellipse(overlay, (cx, cy), (a_px, b_px), 0, 0, 360, color_fill, -1, cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.30, panel, 0.70, 0, panel)
    cv2.ellipse(panel, (cx, cy), (a_px, b_px), 0, 0, 360, color_edge, 2, cv2.LINE_AA)
    cv2.circle(panel, (cx, cy), 3, color_edge, -1, cv2.LINE_AA)
    if label:
        cv2.putText(panel, label, (cx + 6, cy - 6), FONT, 0.4, color_edge, 1, cv2.LINE_AA)


def _draw_axis_labels(panel: np.ndarray, ext: Tuple[float, float, float, float],
                      unit: str = "px") -> None:
    H, W = panel.shape[:2]
    x_lo, x_hi, y_lo, y_hi = ext
    for i in range(0, 5):
        gx = int(i * W / 4)
        x_val = x_lo + i * (x_hi - x_lo) / 4
        cv2.putText(panel, f"{x_val:.0f}{unit}", (gx + 2, H - 4),
                    FONT, 0.32, SUBTEXT, 1, cv2.LINE_AA)
    for j in range(0, 4):
        gy = TITLE_BAR + 10 + j * (H - TITLE_BAR - 10) / 3
        y_val = y_lo + j * (y_hi - y_lo) / 3
        cv2.putText(panel, f"{y_val:.0f}", (4, int(gy)),
                    FONT, 0.32, SUBTEXT, 1, cv2.LINE_AA)


# =========================================================================
# Per-frame composition
# =========================================================================
def _panel_gt(gt: pd.DataFrame, t_now: float, ext, unit: str) -> np.ndarray:
    p = _new_panel("1. Ground Truth")
    _draw_axis_labels(p, ext, unit)
    if gt.empty:
        cv2.putText(p, "(no GT for this dataset)", (PANEL_W // 2 - 90, PANEL_H // 2),
                    FONT, 0.5, SUBTEXT, 1, cv2.LINE_AA)
        return p
    sub = gt[gt["t"].astype(float) <= t_now]
    if not sub.empty:
        pts = [_world_to_panel(float(r["x"]), float(r["y"]), ext, PANEL_W, PANEL_H)
               for _, r in sub.iterrows()]
        for i in range(1, len(pts)):
            cv2.line(p, pts[i - 1], pts[i], GT_COLOR, 2, cv2.LINE_AA)
        cx, cy = pts[-1]
        cv2.circle(p, (cx, cy), 5, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.circle(p, (cx, cy), 4, GT_COLOR, -1, cv2.LINE_AA)
    return p


def _panel_sonar(tracks: pd.DataFrame, meas: pd.DataFrame, t_now: float,
                 ext, unit: str, color: Tuple[int, int, int], title: str,
                 trail_s: Optional[float] = None) -> np.ndarray:
    p = _new_panel(title)
    _draw_axis_labels(p, ext, unit)
    cx_col, cy_col = _xy_cols(tracks if not tracks.empty else meas)
    # ── Kalman-filtered state (smooth line), drawn per track_id ─────────────
    if not tracks.empty:
        id_col = next((c for c in ("track_id", "local_track_id") if c in tracks.columns), None)
        if id_col is None:
            raise ValueError(
                "Sonar tracks CSV is missing a track-ID column (track_id / local_track_id); "
                "drawing without grouping would create cross-track artefacts."
            )
        for _, grp in tracks.groupby(id_col):
            _draw_trail(p, grp, t_now, ext, color, cx_col, cy_col,
                        radius=3, thickness=2,
                        max_age_s=trail_s)  # None = full history
    # Raw measurements (sparser dots)
    if not meas.empty:
        sub = meas[meas["timestamp"].astype(float) <= t_now]
        sub = sub[sub["measurement_available"].astype(str).str.lower() == "true"]
        sub = sub.tail(50)  # last 50 dots max
        for _, r in sub.iterrows():
            try:
                px, py = _world_to_panel(float(r[cx_col]), float(r[cy_col]),
                                          ext, PANEL_W, PANEL_H)
            except (KeyError, ValueError):
                continue
            if 0 <= px < PANEL_W and TITLE_BAR <= py < PANEL_H:
                cv2.circle(p, (px, py), 1, color, -1, cv2.LINE_AA)
    return p


def _panel_fusion(fused: pd.DataFrame, t_now: float, ext, unit: str) -> np.ndarray:
    p = _new_panel("4. Fusion (G001..)")
    _draw_axis_labels(p, ext, unit)
    if fused.empty:
        return p
    sub = fused[fused["timestamp"].astype(float) <= t_now]
    sub = sub.sort_values("timestamp")
    if sub.empty:
        return p
    # Draw trail per GID, color by mode of the *most recent* row of that GID
    for gid, gdf in sub.groupby("global_id"):
        if gdf.empty:
            continue
        recent = gdf.iloc[-1]
        fm = str(recent.get("fusion_mode", ""))
        color = FUSION_MODE_COLOR.get(fm, FUSION_COLOR)
        # Trail
        pts = [_world_to_panel(float(r["x_fused"]), float(r["y_fused"]),
                                ext, PANEL_W, PANEL_H)
               for _, r in gdf.iterrows()
               if not (math.isnan(r["x_fused"]) or math.isnan(r["y_fused"]))]
        for i in range(1, len(pts)):
            cv2.line(p, pts[i - 1], pts[i], color, 2, cv2.LINE_AA)
        # Most recent dot
        if pts:
            cx, cy = pts[-1]
            cv2.circle(p, (cx, cy), 5, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(p, (cx, cy), 4, color, -1, cv2.LINE_AA)
            cv2.putText(p, f"{gid}", (cx + 6, cy - 6), FONT, 0.4, color, 1, cv2.LINE_AA)
            cv2.putText(p, fm[:8], (cx + 6, cy + 8), FONT, 0.32, color, 1, cv2.LINE_AA)
    return p


def _panel_ellipse(fused: pd.DataFrame, t_now: float, ext, unit: str,
                   width: int = PANEL_W) -> np.ndarray:
    p = _new_panel("5. 95% Covariance Ellipse (chi2(2)=5.99)", width=width)
    _draw_axis_labels(p, ext, unit)
    if fused.empty:
        return p
    Pxx_c, Pyy_c = _P_cols(fused)
    sub = fused[fused["timestamp"].astype(float) <= t_now].sort_values("timestamp")
    if sub.empty:
        return p
    for gid, gdf in sub.groupby("global_id"):
        if gdf.empty:
            continue
        recent = gdf.iloc[-1]
        try:
            x = float(recent["x_fused"]); y = float(recent["y_fused"])
            Pxx = float(recent[Pxx_c]); Pyy = float(recent[Pyy_c])
        except (KeyError, ValueError):
            continue
        if math.isnan(x) or math.isnan(y) or Pxx <= 0 or Pyy <= 0:
            continue
        _draw_ellipse(p, x, y, Pxx, Pyy, ext, label=str(gid))
    # Footer: chi² scale
    cv2.putText(p, f"scale = sqrt(5.991) = {SCALE_95:.3f}", (8, PANEL_H - 8),
                FONT, 0.35, SUBTEXT, 1, cv2.LINE_AA)
    return p


def _compose_frame(
    src: DatasetSources,
    t_now: float,
    ext,
    tr_a: pd.DataFrame,
    tr_b: pd.DataFrame,
    meas_a: pd.DataFrame,
    meas_b: pd.DataFrame,
    fused: pd.DataFrame,
    gt: pd.DataFrame,
) -> np.ndarray:
    """Compose the 5-panel layout for a single time instant.

    Layout:
      ┌─────────────┬─────────────┐
      │ GT          │ Sonar A     │
      ├─────────────┼─────────────┤
      │ Sonar B     │ Fusion      │
      └─────────────┴─────────────┘
      ┌───────────────────────────┐
      │ 95% Covariance Ellipse    │
      └───────────────────────────┘

    Below the panels: a timeline showing t_now, with a small strip that
    highlights which panels have a measurement at t_now.
    """
    unit = "px" if src.kind == "dfki" else "m"
    top_w = PANEL_W * 2 + LINE
    top_h = PANEL_H * 2 + LINE

    panel_gt_or_events = _panel_gt(gt, t_now, ext, unit) if src.kind == "dfki" \
        else _panel_glf_events(src, t_now, ext, unit)
    panel_a = _panel_sonar(tr_a, meas_a, t_now, ext, unit, A_COLOR, "2. Sonar A (real)")
    panel_b = _panel_sonar(tr_b, meas_b, t_now, ext, unit, B_COLOR, "3. Sonar B (synthetic)")
    panel_f = _panel_fusion(fused, t_now, ext, unit)
    panel_e = _panel_ellipse(fused, t_now, ext, unit, width=top_w)
    top = np.full((top_h, top_w, 3), 30, dtype=np.uint8)
    top[0:PANEL_H, 0:PANEL_W] = panel_gt_or_events
    top[0:PANEL_H, PANEL_W + LINE:PANEL_W * 2 + LINE] = panel_a
    top[PANEL_H + LINE:PANEL_H * 2 + LINE, 0:PANEL_W] = panel_b
    top[PANEL_H + LINE:PANEL_H * 2 + LINE, PANEL_W + LINE:PANEL_W * 2 + LINE] = panel_f
    # Bottom ellipse panel — full width
    full_h = top_h + PANEL_H
    full = np.full((full_h, top_w, 3), 30, dtype=np.uint8)
    full[0:top_h, 0:top_w] = top
    full[top_h:top_h + PANEL_H, 0:top_w] = panel_e

    # Footer banner with t and stats
    footer = np.full((40, full.shape[1], 3), 20, dtype=np.uint8)
    cv2.putText(footer, f"dataset: {src.dataset_name}   t = {t_now:.2f} s   unit = {unit}",
                (10, 26), FONT, 0.55, (220, 220, 230), 1, cv2.LINE_AA)
    # Mode legend on the right side
    x_legend = full.shape[1] - 720
    cv2.putText(footer, "fusion mode:", (x_legend, 14), FONT, 0.42, SUBTEXT, 1, cv2.LINE_AA)
    for i, (mode, col) in enumerate(FUSION_MODE_COLOR.items()):
        cx = x_legend + 110 + i * 120
        cv2.rectangle(footer, (cx, 24), (cx + 14, 32), col, -1, cv2.LINE_AA)
        cv2.putText(footer, mode, (cx + 18, 32), FONT, 0.4, (220, 220, 230), 1, cv2.LINE_AA)
    full = np.vstack([full, footer])
    return full


def _panel_glf_events(src: DatasetSources, t_now: float, ext, unit: str) -> np.ndarray:
    p = _new_panel("1. GLF events (no per-frame GT)")
    _draw_axis_labels(p, ext, unit)
    events = _load_glf_events(src.glf_profile)
    if events.empty:
        cv2.putText(p, "(no GLF events profile)", (PANEL_W // 2 - 90, PANEL_H // 2),
                    FONT, 0.5, SUBTEXT, 1, cv2.LINE_AA)
        return p
    for _, ev in events.iterrows():
        if math.isnan(ev["t"]):
            continue
        if ev["t"] > t_now:
            break
        # Plot the expected range/bearing envelope as a small box
        if not (math.isnan(ev["range_lo"]) or math.isnan(ev["range_hi"])):
            # In SONAR_FAN metres, bearing is small.  We treat (range,bearing)
            # as (x, y) bounds roughly.
            x0 = float(ev["range_lo"]); x1 = float(ev["range_hi"])
            bx0 = -ev["range_hi"] * 0.2; bx1 = ev["range_hi"] * 0.2
            y0 = -ev["range_hi"] * 0.2; y1 = ev["range_hi"] * 0.2
            try:
                p0 = _world_to_panel(bx0, x0, ext, PANEL_W, PANEL_H)
                p1 = _world_to_panel(bx1, x1, ext, PANEL_W, PANEL_H)
            except (ZeroDivisionError, ValueError):
                continue
            cv2.rectangle(p, p0, p1, GT_COLOR, 1)
            cv2.putText(p, str(ev["label"])[:18], (p0[0] + 4, p0[1] - 4),
                        FONT, 0.36, GT_COLOR, 1, cv2.LINE_AA)
    return p


# =========================================================================
# Main
# =========================================================================
def render_five_panel_video(src: DatasetSources, out_path: Path,
                             fps: int = FPS) -> Optional[Path]:
    """Render the 5-panel comparison video for one dataset."""
    # Load inputs
    tr_a = pd.read_csv(src.tr_a_csv) if src.tr_a_csv.exists() else pd.DataFrame()
    tr_b = pd.read_csv(src.tr_b_csv) if src.tr_b_csv.exists() else pd.DataFrame()
    meas_a = pd.read_csv(src.meas_a_csv) if src.meas_a_csv.exists() else pd.DataFrame()
    meas_b = pd.read_csv(src.meas_b_csv) if src.meas_b_csv.exists() else pd.DataFrame()
    fused = pd.read_csv(src.fused_csv) if src.fused_csv.exists() else pd.DataFrame()
    gt = _load_gt(src.gt_csv) if (src.kind == "dfki" and src.gt_csv and src.gt_csv.exists()) \
        else pd.DataFrame()

    if fused.empty and tr_a.empty and tr_b.empty:
        print(f"  [SKIP] {src.dataset_name}: no data")
        return None

    # Normalize time column to float seconds
    for d in (tr_a, tr_b, meas_a, meas_b, fused):
        if "timestamp" in d.columns:
            d["timestamp"] = pd.to_numeric(d["timestamp"], errors="coerce")
        elif "timestamp_utc" in d.columns:
            d["timestamp"] = pd.to_datetime(d["timestamp_utc"], utc=True,
                                            errors="coerce").astype("int64") / 1e9
        d.dropna(subset=["timestamp"], inplace=True)

    # Determine time range
    all_ts = []
    for d in (tr_a, tr_b, fused, gt):
        if not d.empty and "timestamp" in d.columns:
            all_ts.extend(d["timestamp"].astype(float).tolist())
    if not all_ts:
        print(f"  [SKIP] {src.dataset_name}: no timestamps")
        return None
    t_start, t_end = float(min(all_ts)), float(max(all_ts))
    if t_end <= t_start:
        t_end = t_start + 1.0

    # Shared coordinate extent
    ext = _shared_extent(tr_a, tr_b, fused, gt if not gt.empty else None)

    n_frames = max(2, int((t_end - t_start) * fps))
    times = np.linspace(t_start, t_end, n_frames)
    print(f"  Rendering {n_frames} frames ({n_frames/fps:.1f}s) for {src.dataset_name} ...")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # We need a sample frame to know the actual canvas size
    sample = _compose_frame(src, t_start, ext, tr_a, tr_b, meas_a, meas_b, fused, gt)
    H, W = sample.shape[:2]
    vw = cv2.VideoWriter(str(out_path), VIDEO_CODEC, fps, (W, H))
    if not vw.isOpened():
        print(f"  [ERROR] cannot open video writer: {out_path}")
        return None

    t0 = time.time()
    for i, t in enumerate(times):
        if i % 50 == 0:
            print(f"    frame {i}/{n_frames}")
        frame = _compose_frame(src, float(t), ext, tr_a, tr_b, meas_a, meas_b, fused, gt)
        vw.write(frame)
    vw.release()
    elapsed = time.time() - t0
    size_mb = out_path.stat().st_size / 1e6
    print(f"  done in {elapsed:.1f}s — {size_mb:.1f} MB → {out_path}")
    return out_path


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datasets", default="dfki_set_2",
                    help="Comma-separated list of dataset names, e.g. 'dfki_set_2,glf'")
    ap.add_argument("--out-root", type=Path, default=LEGACY_E2E_ALL_ROOT)
    ap.add_argument("--fps", type=int, default=FPS)
    args = ap.parse_args()

    datasets = [d.strip() for d in args.datasets.split(",")]
    for ds in datasets:
        print(f"\n=== {ds} ===")
        src = DatasetSources.for_dataset(args.out_root, ds)
        out_dir = args.out_root / ds / "videos" / "five_panel"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{ds}_five_panel.mp4"
        render_five_panel_video(src, out, fps=args.fps)


if __name__ == "__main__":
    main()
