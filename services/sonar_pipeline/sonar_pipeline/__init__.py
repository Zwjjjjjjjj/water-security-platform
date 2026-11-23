"""
sonar_pipeline — unified entry for raw data → tracking → association → fusion.

Real path (GLF / DFKI):      Stage 1 → 2 → 3 → 4 → 5 → 6 → 7/8
Sim path  (N virtual sonars): Reference → simulate → local Kalman → align → associate → fuse

Stage 5/6 here delegate to the official two_sonar_association engines
(TrackAssociation / NPartiteAssociator / CovarianceWeightedFusion / InformationFusionN)
instead of the inlined copies that used to live in e2e_bridge.
"""
from .common import load_pipeline_config, resolve_paths  # noqa: F401
from .association_fusion import associate_n_way, fuse_n_way  # noqa: F401

__all__ = ["load_pipeline_config", "resolve_paths", "associate_n_way", "fuse_n_way"]
