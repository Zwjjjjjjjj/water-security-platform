"""航迹关联预备模块。

组成：

  - `cdt_tracker.py`  CDT 跨帧重建：逐帧检测 → 稳定航迹。
    只有"CDT 只有逐帧检测、没有稳定目标编号"时才需要；
    已有可靠 RT 航迹的场景直接使用 RT，不走本模块。
  - `bias.py`         跨源坐标系统差核查：先核查，必要时修正。
    只给证据与可选修正函数，**不做自动估计、不默认应用**。
  - `timebase.py`     统一时间基准检查：复用对齐模块的时间，
    断言三路时间可比（共同本地时间基准即可，不强求 UTC）。
"""

from __future__ import annotations

from .bias import (  # noqa: F401
    apply_cdt_correction,
    bearing_residual_deg,
    interpolate_ais_polar,
    summarize_cdt_bias,
)
from .cdt_tracker import (  # noqa: F401
    ActiveTrack,
    CdtTracker,
    Cluster,
    cluster_frame,
    load_detections,
    polar_distance_m,
)
from .timebase import check_timebase, parse_local  # noqa: F401
