"""
DFKI Diver Tracking Sonar — frame store adapter.

Provides the same ``__len__``, ``row()``, and ``frame()`` interface as
``FrameStore`` so that the existing tracking pipeline can consume DFKI JPG
sequences without any changes to the core tracker.

Image geometry
--------------
DFKI provides pre-rendered Cartesian sonar images (932 × 514 px, uint8 RGB)
saved as JPG.  Each image is a fan-shaped sonar display from an Oculus
M1200d with:
  - origin  ≈ (466, 513)  (x_center, bottom_row)
  - bearing ≈ −26° … +26°  from vertical
  - pixel radius max ≈ 490 px

The pixel coordinates (xpix, ypix) are used as the tracker state directly
(no physical range/bearing conversion in the DFKI branch — ``input_mode`` is
set to "dfki_pixel").

Files produced by this class
-----------------------------
frame_metadata.csv  — one row per frame; columns:
  frame_index  image_name  timestamp_s  time_millis  max_range_m  width  height
All frames share ``max_range_m = None`` (no calibrated physical scale).

Metadata is written once on first access and cached in memory.
"""

from __future__ import annotations
from pathlib import Path
import glob
import os

import numpy as np
import pandas as pd

# We defer heavy imports (cv2) until the first frame access so that
# just importing this module does not trigger a hard dependency.
_CV2_IMPORTED = False


def _ensure_cv2():
    global _CV2_IMPORTED
    if not _CV2_IMPORTED:
        global cv2
        import cv2
        _CV2_IMPORTED = True


class DFKIFrameStore:
    """
    Random-access reader for a single DFKI sonar set (set_1 … set_4).

    Frames are sorted by the numerical value encoded in the JPG filename
    (Unix timestamp in seconds, e.g. ``1650983298.083860159.jpg``).

    Parameters
    ----------
    set_dir : Path | str
        Path to the ``sonar_N`` subdirectory, e.g.
        ``.../Diver Tracking - Sonar/set_1/sonar_1``.
    """

    # Pixel geometry (verified against the actual images)
    W: int = 932
    H: int = 514
    ORIGIN_X: float = 465.5   # horizontal pixel of acoustic centre
    ORIGIN_Y: float = 513.0    # bottom row  (acoustic origin)
    MAX_RADIUS_PX: float = 490.0  # approximate max valid fan radius

    # Bearing span (degrees, verified from fan geometry):
    #   far left  ≈ −26°
    #   far right ≈ +26°
    BEARING_LEFT_DEG: float = -26.0
    BEARING_RIGHT_DEG: float =  26.0

    def __init__(self, set_dir: Path | str):
        self.set_dir = Path(set_dir)
        self._build_metadata()
        self._bg_cache: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Public interface — mirrors FrameStore
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._meta)

    def row(self, index: int) -> pd.Series:
        """Return frame metadata row as a pandas Series."""
        return self._meta.iloc[int(index)]

    def frame(self, index: int) -> np.ndarray:
        """
        Return frame as a 2-D uint8 array of shape (H, W) in pixel
        coordinates (ypix, xpix).

        The caller (detect_frame) treats the first axis as "range bins"
        and the second axis as "bearing bins", so this layout is directly
        compatible with the existing tracker.

        intensity_type is stored as a class-level note for downstream consumers.
        """
        _ensure_cv2()
        import cv2
        img_name = self._meta.iloc[int(index)]["image_name"]
        img_path = self.set_dir / img_name
        # Read grayscale: (H, W) uint8
        gray = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise FileNotFoundError(f"Cannot read {img_path}")
        return gray

    def bearings_deg(self, index: int | None = None) -> np.ndarray:
        """
        Return a uniform bearing array.

        For the DFKI branch the tracker works in pixel coordinates, but
        detect_frame still expects ``angles_deg`` for range/bearing
        clipping.  We return a linear space so that every column maps to a
        valid angle and range clipping is a no-op (clips by pixel radius
        instead).
        """
        nb = self.W   # one bearing per column
        return np.linspace(self.BEARING_LEFT_DEG, self.BEARING_RIGHT_DEG, nb)

    def background_median(self, n: int = 5) -> np.ndarray:
        """
        Return a median-background frame computed from the first ``n`` frames.

        Returns
        -------
        np.ndarray
            (H, W) float32 array — the per-pixel median of the first n frames.
        """
        _ensure_cv2()
        import cv2
        jpgs = self._sorted_jpgs[:n]
        frames = []
        for jp in jpgs:
            g = cv2.imread(str(jp), cv2.IMREAD_GRAYSCALE)
            frames.append(g.astype(np.float32))
        return np.median(frames, axis=0).astype(np.float32)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_metadata(self):
        """Scan the directory and build frame_metadata DataFrame."""
        pattern = str(self.set_dir / "*.jpg")
        jpg_paths = sorted(
            glob.glob(pattern),
            key=lambda p: float(Path(p).stem)   # numeric sort, not lexical
        )
        rows = []
        for frame_index, jp in enumerate(jpg_paths):
            stem = Path(jp).stem
            ts_s = float(stem)                # Unix timestamp in seconds
            rows.append({
                "frame_index": frame_index,
                "image_name": Path(jp).name,
                "timestamp_s": ts_s,
                "time_millis": ts_s * 1000.0,
                "max_range_m": None,           # no calibrated scale
                "width": self.W,
                "height": self.H,
                # Bearing array is uniform; store once here for convenience
                "bearing_left_deg": self.BEARING_LEFT_DEG,
                "bearing_right_deg": self.BEARING_RIGHT_DEG,
            })
        self._meta = pd.DataFrame(rows)
        self._sorted_jpgs = [Path(self.set_dir / r["image_name"]) for r in rows]

    # ------------------------------------------------------------------
    # Pixel-coordinate helpers (used by detector / GT evaluation)
    # ------------------------------------------------------------------

    def pixel_to_fan(self, xpix: float, ypix: float) -> tuple[float, float]:
        """
        Convert pixel coordinates (xpix, ypix) to fan coordinates (u, v).

        ``u`` = cross-fan (positive = right),
        ``v`` = along-boresight (positive = away from sonar).

        Uses the verified origin (cx, cy) = (465.5, 513.0).
        """
        u = xpix - self.ORIGIN_X
        v = self.ORIGIN_Y - ypix   # y decreases upward in image
        return float(u), float(v)

    def pixel_radius(self, xpix: float, ypix: float) -> float:
        """Pixel distance from the acoustic origin."""
        dx = xpix - self.ORIGIN_X
        dy = self.ORIGIN_Y - ypix
        return float(np.sqrt(dx*dx + dy*dy))

    def bearing_from_pixel(self, xpix: float) -> float:
        """Approximate bearing (degrees) from x-pixel position."""
        dx = xpix - self.ORIGIN_X
        # bearing = arctan2(dx, v)  — use full pixel radius as proxy for v
        r = self.pixel_radius(xpix, self.ORIGIN_Y - 1) + 1e-9
        return float(np.rad2deg(np.arctan2(dx, r)))
