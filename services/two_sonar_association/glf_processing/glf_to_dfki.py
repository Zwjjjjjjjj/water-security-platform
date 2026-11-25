#!/usr/bin/env python3
"""
GLF (Tritech raw) -> DFKI (Cartesian fan image) converter.

Input:  GLF extracted files (frame_metadata.csv, frames_u8.bin, bearings_f64le.bin)
Output: DFKI-style set directory
        - frame_NNNN.jpg  (932x514 grayscale, fan-shaped Cartesian)
        - frame_metadata.csv  (frame_index, image_name, timestamp_s, time_millis, max_range_m, ...)

Image geometry (mirrors dfki_frame_store.DFKIFrameStore):
    W=932, H=514
    ORIGIN_X=465.5, ORIGIN_Y=513.0
    BEARING span: -26 deg ... +26 deg (256 bearing bins)
    MAX_RADIUS_PX=490.0

Conversion:
    polar_frame[n_range, n_beam]  (raw GLF, n_range=917, n_beam=256)
    => for each pixel (xpix, ypix):
        r_px  = sqrt((xpix-cx)^2 + (ypix-cy)^2)        # in image
        b_deg = atan2(xpix-cx, cy-ypix) * 180/pi       # bearing
        r_bin = r_px / MAX_RADIUS_PX * (n_range-1)     # range bin
        b_bin = (b_deg - BEAR_LEFT) / span * (n_beam-1) # bearing bin
        -> bilinear interpolation from polar_frame

Usage
-----
    python glf_to_dfki.py \
        --glf-dir glf_processing/work \
        --out-set-dir glf_processing/dfki_tanana \
        --max-frames 869
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
import time

import numpy as np
import pandas as pd

# Reuse the Tritech project's frame store (sibling project, same monorepo).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from two_sonar_paths import TRITECH_ROOT  # noqa: E402
sys.path.insert(0, str(TRITECH_ROOT / 'src'))
from frame_store import FrameStore  # noqa: E402


# Target image geometry (matches DFKI sonar images)
W = 932
H = 514
ORIGIN_X = 465.5
ORIGIN_Y = 513.0
MAX_RADIUS_PX = 490.0
BEARING_LEFT_DEG = -26.0
BEARING_RIGHT_DEG = 26.0


def bilinear_polar_to_cartesian(polar: np.ndarray, bearings_deg: np.ndarray) -> np.ndarray:
    """
    Convert one (n_range, n_beam) polar intensity frame to a Cartesian (H, W) fan image.

    Pixel convention: x right, y down. Acoustic origin at (ORIGIN_X, ORIGIN_Y) bottom-centre.
    Bearings array is in degrees, ascending across columns.

    Out-of-fan pixels are filled with 0 (matches the dark surround of a real sonar image).
    """
    n_range, n_beam = polar.shape
    assert bearings_deg.size == n_beam, f"bearings {bearings_deg.size} != n_beam {n_beam}"

    # Pre-compute pixel grid in (xpix, ypix)
    xs = np.arange(W)
    ys = np.arange(H)
    XX, YY = np.meshgrid(xs, ys)              # (H, W)

    # Polar coords of each pixel
    dx = XX - ORIGIN_X
    dy = ORIGIN_Y - YY                        # y increases downward in image; we want up = +range
    r_px = np.sqrt(dx * dx + dy * dy)         # (H, W)
    b_deg = np.degrees(np.arctan2(dx, dy))    # -180..180; will be -90..+90 inside fan

    # Mask: pixels inside the fan (radius <= MAX_RADIUS_PX, bearing in span)
    in_fan = (r_px <= MAX_RADIUS_PX) & (b_deg >= BEARING_LEFT_DEG) & (b_deg <= BEARING_RIGHT_DEG)

    # Map (r_px, b_deg) -> bin indices
    r_norm = np.clip(r_px / MAX_RADIUS_PX, 0.0, 1.0)
    r_bin_f = r_norm * (n_range - 1)
    b_span = BEARING_RIGHT_DEG - BEARING_LEFT_DEG
    b_norm = (b_deg - BEARING_LEFT_DEG) / b_span
    b_bin_f = np.clip(b_norm, 0.0, 1.0) * (n_beam - 1)

    # Bilinear interpolation
    r0 = np.floor(r_bin_f).astype(np.int32)
    r1 = np.clip(r0 + 1, 0, n_range - 1)
    b0 = np.floor(b_bin_f).astype(np.int32)
    b1 = np.clip(b0 + 1, 0, n_beam - 1)
    dr = (r_bin_f - r0).astype(np.float32)
    db = (b_bin_f - b0).astype(np.float32)

    I00 = polar[r0, b0]
    I01 = polar[r0, b1]
    I10 = polar[r1, b0]
    I11 = polar[r1, b1]
    I = ((1 - dr) * (1 - db) * I00 +
         (1 - dr) *      db  * I01 +
              dr  * (1 - db) * I10 +
              dr  *      db  * I11).astype(np.uint8)

    out = np.zeros((H, W), dtype=np.uint8)
    out[in_fan] = I[in_fan]
    return out


def time_to_unix_seconds(time_iso: str) -> float:
    """ISO 8601 with 'Z' -> Unix seconds (float)."""
    from datetime import datetime
    s = time_iso.rstrip("Z")
    if "." in s:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
    else:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    return dt.timestamp()


def main():
    ap = argparse.ArgumentParser(description="Convert GLF (Tritech raw) to DFKI (Cartesian fan images)")
    ap.add_argument("--glf-dir", type=Path, required=True,
                   help="Directory with frame_metadata.csv + frames_u8.bin + bearings_f64le.bin")
    ap.add_argument("--out-set-dir", type=Path, required=True,
                   help="Output set directory (will contain *.jpg directly)")
    ap.add_argument("--max-frames", type=int, default=None,
                   help="Limit number of frames (for testing)")
    ap.add_argument("--prefix", type=str, default="tanana",
                   help="Output image filename prefix (default 'tanana')")
    ap.add_argument("--write-jpg", action="store_true", default=True,
                   help="Write JPGs (default True)")
    args = ap.parse_args()

    import cv2

    out_set_dir = args.out_set_dir
    out_set_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading FrameStore from {args.glf_dir} ...")
    fs = FrameStore(args.glf_dir)
    n_total = len(fs.meta)
    n = min(n_total, args.max_frames) if args.max_frames else n_total
    print(f"  total frames in GLF: {n_total}")
    print(f"  will convert:       {n}")
    print(f"  output dir:         {out_set_dir}")
    rows = []
    t_start = time.time()
    for i in range(n):
        r = fs.row(i)
        polar = fs.frame(i)                  # (n_range, n_beam) uint8
        bearings = fs.bearings_deg(i)        # (n_beam,) float
        img = bilinear_polar_to_cartesian(polar, bearings)

        # Output filename uses Unix timestamp in seconds (matches DFKI)
        ts_s = time_to_unix_seconds(r["time_iso"])
        fname = f"{ts_s:.9f}.jpg"
        if args.write_jpg:
            cv2.imwrite(str(out_set_dir / fname), img,
                       [cv2.IMWRITE_JPEG_QUALITY, 92])

        rows.append({
            "frame_index": i,
            "image_name": fname,
            "timestamp_s": ts_s,
            "time_millis": ts_s * 1000.0,
            "max_range_m": None,            # GLF already has scale, but DFKI convention = None
            "width": W,
            "height": H,
            "bearing_left_deg": BEARING_LEFT_DEG,
            "bearing_right_deg": BEARING_RIGHT_DEG,
            # extra metadata for traceability
            "device_id": int(r["device_id"]),
            "device_type": str(r["device_type"]),
            "n_range": int(r["n_range"]),
            "n_beam": int(r["n_beam"]),
            "sound_speed_mps": float(r["sound_speed_mps"]),
            "gain_percent": int(r["gain_percent"]),
        })
        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.time() - t_start
            fps = (i + 1) / max(elapsed, 1e-6)
            eta = (n - i - 1) / max(fps, 1e-6)
            print(f"  [{i+1:4d}/{n}] fps={fps:.1f} eta={eta:.0f}s "
                  f"max_I={int(polar.max())} mean_I={float(polar.mean()):.1f}")

    meta = pd.DataFrame(rows)
    parent = out_set_dir.parent
    meta_path = parent / "frame_metadata.csv"
    meta.to_csv(meta_path, index=False)
    print(f"\nWritten metadata: {meta_path}")
    print(f"  rows: {len(meta)}")
    print(f"  time range: {meta['timestamp_s'].min():.3f} ... {meta['timestamp_s'].max():.3f} "
          f"({meta['timestamp_s'].max() - meta['timestamp_s'].min():.2f} s)")

    summary = {
        "source_glf": str(args.glf_dir),
        "out_set_dir": str(out_set_dir),
        "n_frames": int(len(meta)),
        "first_ts_s": float(meta['timestamp_s'].min()),
        "last_ts_s": float(meta['timestamp_s'].max()),
        "duration_s": float(meta['timestamp_s'].max() - meta['timestamp_s'].min()),
        "device_id": int(meta['device_id'].iloc[0]),
        "device_type": str(meta['device_type'].iloc[0]),
        "image_w": W,
        "image_h": H,
        "n_range": int(meta['n_range'].iloc[0]),
        "n_beam": int(meta['n_beam'].iloc[0]),
    }
    import json
    with open(parent / "extraction_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Written summary: {parent / 'extraction_summary.json'}")
    print(f"\nNext step: load with DFKIFrameStore('{out_set_dir}')")


if __name__ == "__main__":
    main()
