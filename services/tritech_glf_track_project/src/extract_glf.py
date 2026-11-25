from __future__ import annotations
import os
import subprocess
from pathlib import Path

import pandas as pd


def compile_java(project_root: Path) -> Path:
    project_root = Path(project_root)
    src = project_root / "java" / "ExportGLF.java"
    jar = project_root / "libs" / "TritechFiles-local.jar"
    build = project_root / "build" / "java"
    build.mkdir(parents=True, exist_ok=True)
    class_file = build / "ExportGLF.class"

    need_compile = (
        not class_file.exists()
        or class_file.stat().st_mtime < src.stat().st_mtime
    )
    if need_compile:
        cmd = ["javac", "-encoding", "UTF-8", "-cp", str(jar), "-d", str(build), str(src)]
        subprocess.run(cmd, check=True)
    return build


def _validate_extraction(output_dir: Path) -> pd.DataFrame:
    metadata_path = output_dir / "frame_metadata.csv"
    frames_path = output_dir / "frames_u8.bin"
    bearings_path = output_dir / "bearings_f64le.bin"

    if not metadata_path.exists():
        raise RuntimeError(
            "GLF extraction failed: frame_metadata.csv was not generated."
        )
    metadata = pd.read_csv(metadata_path)
    if len(metadata) == 0:
        raise RuntimeError(
            "GLF extraction failed: 0 image frames were parsed."
        )
    if not frames_path.exists() or frames_path.stat().st_size == 0:
        raise RuntimeError(
            "GLF extraction failed: frames_u8.bin is empty."
        )
    if not bearings_path.exists() or bearings_path.stat().st_size == 0:
        raise RuntimeError(
            "GLF extraction failed: bearings_f64le.bin is empty."
        )

    expected_frame_bytes = int((metadata["n_range"] * metadata["n_beam"]).sum())
    actual_frame_bytes = frames_path.stat().st_size
    if actual_frame_bytes != expected_frame_bytes:
        raise RuntimeError(
            "frames_u8.bin size mismatch: "
            f"expected={expected_frame_bytes}, actual={actual_frame_bytes}"
        )

    expected_bearing_bytes = int(metadata["n_beam"].sum() * 8)
    actual_bearing_bytes = bearings_path.stat().st_size
    if actual_bearing_bytes != expected_bearing_bytes:
        raise RuntimeError(
            "bearings_f64le.bin size mismatch: "
            f"expected={expected_bearing_bytes}, actual={actual_bearing_bytes}"
        )

    print(f"GLF extraction validated: {len(metadata)} image frames")
    return metadata


def export_glf(glf_path: Path, output_dir: Path, project_root: Path) -> Path:
    glf_path = Path(glf_path).resolve()
    output_dir = Path(output_dir).resolve()
    project_root = Path(project_root).resolve()
    if not glf_path.exists():
        raise FileNotFoundError(glf_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    build = compile_java(project_root)
    jar = project_root / "libs" / "TritechFiles-local.jar"
    classpath = os.pathsep.join([str(build), str(jar)])
    cmd = ["java", "-cp", classpath, "ExportGLF", str(glf_path), str(output_dir)]
    subprocess.run(cmd, check=True)
    _validate_extraction(output_dir)
    return output_dir
