"""统一标准化包。

把 RadarPWN 雷达 TTM 与 MTDSP AIS CSV 两类输入适配为同一公共标准记录，
供下一阶段时空对齐模块使用。

典型用法：

    from standardization import standardize
    standardize(input_path, source_config, output_dir)
"""

from .pipeline import standardize, standardize_many
from . import schema, units, time_fields, validation

__all__ = [
    "standardize",
    "standardize_many",
    "schema",
    "units",
    "time_fields",
    "validation",
]

__version__ = schema.SCHEMA_VERSION
