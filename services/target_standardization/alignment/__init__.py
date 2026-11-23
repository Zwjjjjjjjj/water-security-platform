"""时空对齐模块 v1.0.0。

职责（与验收项一一对应）：

  1. **固定对齐输入与配置** —— `config.py`
     现场坐标原点与基准、雷达站位置与外参、各源已知时间偏移、报文编码
     精度、传感器误差模型、公共栅格参数，全部来自一个 JSON 配置。

  2. **空间对齐到同一现场 ENU** —— `transform.py` / `spatial.py`
     AIS：经纬度 → 现场 ENU；雷达：距离/真方位 → 站址局部平面 → 现场 ENU。
     方位参考为 T 时不叠加安装方向或本船艏向。

  3. **时间整理与异步匹配** —— `temporal.py`
     已知时钟偏移与时间量化偏差修正 → 按源按航迹分组 → 线性插值到公共
     栅格；间隔超限与超范围一律标记不可用，不外推。

  4. **位置误差上界** —— `bounds.py`
     按每条记录的实际距离与该源报文编码精度计算径向/切向/平面三档上界。

  5. **独立评测** —— `evaluation.py`
     只读真值与对齐结果，产出位置误差、时间误差、覆盖率、界限比较。

辅助：

  `verification.py`  独立实现的坐标校验（与 transform 不同数学路径）
  `bounds.py`       误差上界
  `pipeline.py`     编排与落盘
"""

from .config import (  # noqa: F401
    MODULE_VERSION,
    AlignmentConfig,
    EncodingPrecision,
    RadarStationConfig,
    SensorErrorModel,
    SiteFrameConfig,
    SourceConfig,
    SourceTimeConfig,
)
from .pipeline import run_alignment  # noqa: F401
from .if_platform_state import (  # noqa: F401
    IfFrameState,
    IfPlatformStateIndex,
)
from .spatial import SpatialAligner  # noqa: F401
from .temporal import TemporalAligner  # noqa: F401
from .track_export import TrackExporter, export_tracks  # noqa: F401
from .verification import CoordinateVerifier  # noqa: F401

__version__ = MODULE_VERSION
