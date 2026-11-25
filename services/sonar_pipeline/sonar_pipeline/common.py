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

# Legacy (pre-monorepo) absolute roots that may still appear inside
# pipeline.json.  When such a path is still present on disk it is honoured
# as-is; otherwise it is remapped onto the in-repo location so the config
# stays valid after the projects were moved into this monorepo.
_LEGACY_ROOTS = {
    "/root/Tritech_GLF_Track_Project": TRITECH_ROOT,
    "/root/two_sonar_association": TWO_SONAR_ROOT,
    "/root/sonar_pipeline": PIPELINE_ROOT,
}


def remap_legacy_path(value: str | Path) -> Path:
    """Map a legacy absolute project root onto its monorepo location.

    Existing paths win: if the legacy location still exists on disk it is
    returned unchanged, so a deployment that keeps the original layout is
    unaffected.  Only when the legacy root is absent do we fall back to the
    in-repo equivalent.
    """
    p = Path(value)
    s = str(p)
    for legacy, current in _LEGACY_ROOTS.items():
        if s == legacy or s.startswith(legacy + "/"):
            if Path(legacy).exists():
                return p
            return current / p.relative_to(legacy)
    return p


def load_pipeline_config(path: str | Path | None = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("glf_path", "glf_profile", "dfki_root", "output_root"):
        if isinstance(cfg.get(key), str):
            cfg[key] = str(remap_legacy_path(cfg[key]))
    return cfg


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
