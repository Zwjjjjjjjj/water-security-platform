from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd


class FrameStore:
    """Random-access reader for TritechFiles-exported frame/bearing binaries."""

    def __init__(self, export_dir: Path):
        self.export_dir = Path(export_dir)
        self.meta = pd.read_csv(self.export_dir / "frame_metadata.csv")
        self.frames_path = self.export_dir / "frames_u8.bin"
        self.bearings_path = self.export_dir / "bearings_f64le.bin"
        self._frames = np.memmap(self.frames_path, mode="r", dtype=np.uint8)
        self._bearings = np.memmap(self.bearings_path, mode="r", dtype="<f8")

    def __len__(self):
        return len(self.meta)

    def row(self, index: int):
        return self.meta.iloc[int(index)]

    def frame(self, index: int) -> np.ndarray:
        r = self.row(index)
        offset = int(r.frame_offset_bytes)
        n = int(r.frame_length_bytes)
        nr, nb = int(r.n_range), int(r.n_beam)
        arr = np.asarray(self._frames[offset:offset+n], dtype=np.uint8)
        if arr.size != nr * nb:
            raise ValueError(f"Frame {index} size mismatch: {arr.size} vs {nr*nb}")
        return arr.reshape(nr, nb)

    def bearings_rad(self, index: int) -> np.ndarray:
        r = self.row(index)
        byte_offset = int(r.bearing_offset_bytes)
        byte_len = int(r.bearing_length_bytes)
        start = byte_offset // 8
        n = byte_len // 8
        return np.asarray(self._bearings[start:start+n], dtype=float)

    def bearings_deg(self, index: int) -> np.ndarray:
        return np.degrees(self.bearings_rad(index))
