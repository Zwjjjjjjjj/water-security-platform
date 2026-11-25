"""Shared path resolution for two_sonar_association.

The project is designed to run from any location inside the monorepo
(``services/two_sonar_association``).  Every default path is therefore derived
from this file's location instead of being hard-coded to an absolute
``/root/...`` prefix.

External locations (reference tracks, sibling projects) are resolved with the
following precedence:

    environment variable  >  built-in default

Supported environment variables
-------------------------------
``TWO_SONAR_ROOT``
    Root of this project (auto-detected; override only if moved).
``TRITECH_ROOT``
    Sibling single-sonar tracking project (default: ``../../tritech_glf_track_project``).
``DFKI_OUTPUT_V15``
    Legacy reference-track directory used by the regression/benchmark runners.
``TWO_SONAR_OUT``
    Default output root for experiment/benchmark runs.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Root of this project (``services/two_sonar_association``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Directory holding the importable flat modules (``align_tracks`` etc.).
SRC_DIR = PROJECT_ROOT / "src"

#: Configuration directory.
CONFIG_DIR = PROJECT_ROOT / "config"

#: Default output root for experiment / benchmark runs.
OUTPUT_ROOT = Path(os.environ.get("TWO_SONAR_OUT", str(PROJECT_ROOT / "outputs")))

#: Sibling single-sonar tracking project.
TRITECH_ROOT = Path(
    os.environ.get("TRITECH_ROOT", str(PROJECT_ROOT.parent / "tritech_glf_track_project"))
)

#: Legacy reference-track directory (DFKI set_1 single-sonar output).
DFKI_REFERENCE_ROOT = Path(os.environ.get("DFKI_OUTPUT_V15", "/root/dfki_output_v15"))

#: Default reference track used by the association / benchmark runners.
DEFAULT_REFERENCE_TRACK = str(DFKI_REFERENCE_ROOT / "dfki_set_1" / "tracks_raw.csv")

#: Default configuration file.
DEFAULT_CONFIG = str(CONFIG_DIR / "two_sonar_association.json")

#: Default fusion configuration file.
DEFAULT_FUSION_CONFIG = str(CONFIG_DIR / "fusion.json")


def add_src_to_path() -> None:
    """Make the flat ``src/`` modules importable (``align_tracks`` etc.)."""
    import sys

    p = str(SRC_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def add_tritech_src_to_path() -> None:
    """Make the sibling Tritech project's ``src/`` importable."""
    import sys

    p = str(TRITECH_ROOT / "src")
    if p not in sys.path:
        sys.path.insert(0, p)
