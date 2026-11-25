"""Shared path resolution for Tritech_GLF_Track_Project.

The project lives at ``services/tritech_glf_track_project`` in the monorepo and
derives every default path from its own location, so it can be cloned and run
from any location.

External locations (raw datasets, sibling projects, legacy output caches) are
resolved with the following precedence:

    environment variable  >  built-in default

Supported environment variables
-------------------------------
``TRITECH_ROOT``
    Root of this project (auto-detected; override only if moved).
``TWO_SONAR_ROOT``
    Sibling multi-sonar association/fusion project
    (default: ``../../two_sonar_association``).
``DFKI_ROOT``
    DFKI sonar dataset root (default: ``/root/Diver Tracking - Sonar``).
``GLF_PATH``
    Gemini GLF raw log file.
``TRITECH_OUT``
    Default output root for tracking runs.
``LEGACY_E2E_ROOT``
    Legacy ``outputs_e2e`` cache root used as a read-only fallback.
``LEGACY_E2E_ALL_ROOT``
    Legacy ``outputs_e2e_all`` cache root used as a read-only fallback.
``DFKI_OUTPUT_V15`` / ``DFKI_OUTPUT_V14``
    Legacy per-set output directories (v15 / v14) used by the audit scripts.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Root of this project (``services/tritech_glf_track_project``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Importable package directory (``src/``).
SRC_DIR = PROJECT_ROOT / "src"

#: Configuration directory.
CONFIG_DIR = PROJECT_ROOT / "config"

#: Java exporter + third-party jar locations.
JAVA_DIR = PROJECT_ROOT / "java"
LIBS_DIR = PROJECT_ROOT / "libs"

#: Default output root for tracking runs.
OUTPUT_ROOT = Path(os.environ.get("TRITECH_OUT", str(PROJECT_ROOT / "outputs")))

#: Sibling multi-sonar association / fusion project.
TWO_SONAR_ROOT = Path(
    os.environ.get("TWO_SONAR_ROOT", str(PROJECT_ROOT.parent / "two_sonar_association"))
)

#: DFKI sonar dataset root.
DFKI_ROOT = Path(os.environ.get("DFKI_ROOT", "/root/Diver Tracking - Sonar"))

#: Gemini GLF raw log.
GLF_PATH = Path(os.environ.get("GLF_PATH", "/root/data/log_2025-06-19-203913.glf"))

#: Experiment-event profile for GLF track labelling.
GLF_PROFILE = Path(
    os.environ.get("GLF_PROFILE", str(CONFIG_DIR / "tanana_203913_profile.json"))
)

#: Legacy read-only cache roots.
LEGACY_E2E_ROOT = Path(os.environ.get("LEGACY_E2E_ROOT", "/root/outputs_e2e"))
LEGACY_E2E_ALL_ROOT = Path(os.environ.get("LEGACY_E2E_ALL_ROOT", "/root/outputs_e2e_all"))

#: Legacy per-set output directories.
DFKI_OUTPUT_V15 = Path(os.environ.get("DFKI_OUTPUT_V15", "/root/dfki_output_v15"))
DFKI_OUTPUT_V14 = Path(os.environ.get("DFKI_OUTPUT_V14", "/root/dfki_output_v14"))
DFKI_OUTPUT_V5 = Path(os.environ.get("DFKI_OUTPUT_V5", "/root/dfki_output_v5"))
