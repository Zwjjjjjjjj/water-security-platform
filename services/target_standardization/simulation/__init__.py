"""模拟场景生成包。

模块：
  geo         局部 ENU 坐标与方位/距离换算
  trajectory  从实测 AIS 轨迹提取共同参考轨迹
  nmea_encode TTM / ZDA / RMC / AIS !AIVDM 编码
  scenario   场景配置、观测生成与落盘
"""

from .scenario import ScenarioConfig, ScenarioGenerator, SensorConfig  # noqa: F401
