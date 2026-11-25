"""Shared config + path helpers for the unified pipeline.

Single source of truth: /root/sonar_pipeline/config/pipeline.json
It merges:
  - Tritech_GLF_Track_Project/config/default_config.json  (GLF tracking/sensor/sim/assoc)
  - Tritech_GLF_Track_Project/config/dfki_config.json     (DFKI tracking)
  - two_sonar_association/config/two_sonar_association.json (2-sonar assoc/fusion)
  - two_sonar_association/config/n_sonar_association.json  (N-sonar assoc/fusion)
  - bridge B-synthesis params (previously hard-coded in run_e2e_pipeline.py)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Any

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PIPELINE_ROOT / "config" / "pipeline.json"

# Sibling project roots.  Both are part of this monorepo, so the defaults are
# derived from this file's location; environment variables allow relocating
# them when the projects are deployed separately.
SERVICES_ROOT = PIPELINE_ROOT.parent
TRITECH_ROOT = Path(
    os.environ.get("TRITECH_ROOT", str(SERVICES_ROOT / "tritech_glf_track_project"))
)
TWO_SONAR_ROOT = Path(
    os.environ.get("TWO_SONAR_ROOT", str(SERVICES_ROOT / "two_sonar_association"))
)
TWO_SONAR_SRC = TWO_SONAR_ROOT / "src"

DEFAULT_OUT = Path(os.environ.get("PIPELINE_OUT", str(SERVICES_ROOT.parent / "outputs")))
DEFAULT_GLF = Path(os.environ.get("GLF_PATH", "/root/data/log_2025-06-19-203913.glf"))
DEFAULT_GLF_PROFILE = TRITECH_ROOT / "config" / "tanana_203913_profile.json"
DEFAULT_DFKI_ROOT = Path(os.environ.get("DFKI_ROOT", "/root/Diver Tracking - Sonar"))


def load_pipeline_config(path: str | Path | None = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_paths(cfg: Dict[str, Any] | None = None) -> Dict[str, Path]:
    """Resolve every external dependency to an absolute Path.

    Environment variables take precedence over the values in pipeline.json so
    the pipeline can be pointed at relocated datasets without editing config.
    """
    cfg = cfg or {}
    return {
        "tritech_root": TRITECH_ROOT,
        "two_sonar_root": TWO_SONAR_ROOT,
        "two_sonar_src": TWO_SONAR_SRC,
        "default_out": Path(os.environ.get("PIPELINE_OUT", cfg.get("output_root", str(DEFAULT_OUT)))),
        "default_glf": Path(os.environ.get("GLF_PATH", cfg.get("glf_path", str(DEFAULT_GLF)))),
        "default_glf_profile": Path(
            os.environ.get("GLF_PROFILE", cfg.get("glf_profile", str(DEFAULT_GLF_PROFILE)))
        ),
        "default_dfki_root": Path(os.environ.get("DFKI_ROOT", cfg.get("dfki_root", str(DEFAULT_DFKI_ROOT)))),
    }


DATASETS = ("glf", "dfki_set_1", "dfki_set_2", "dfki_set_3", "dfki_set_4")


def dataset_kind(name: str) -> str:
    return "glf" if name == "glf" else "dfki"


def dfki_set_num(name: str) -> int | None:
    if name.startswith("dfki_set_"):
        try:
            return int(name.rsplit("_", 1)[-1])
        except ValueError:
            return None
    return None
