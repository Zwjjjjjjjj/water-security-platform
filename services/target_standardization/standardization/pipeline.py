"""统一标准化流程。

对外入口：standardize(input_path, source_config, output_dir)。

流程：适配器读取 → 字段映射与单位规范化 → 校验 → 输出标准记录 / 拒绝记录 /
质量汇总 / 处理日志。

路径约定：
- `source_file` 在记录中保存为**相对于数据根目录的稳定路径**（由
  source_config["data_root"] 决定，缺省为输入路径的公共父目录）。record_id
  基于该稳定路径生成，移动整个数据目录不会改变 ID。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from . import radar_ttm_adapter, schema, validation
from .ais_csv_adapter import AISCsvAdapter
from .ais_vdm_adapter import RadarPWNAisAdapter as AisVdmAdapter
from .radar_cutdata_adapter import RadarCutDataAdapter
from .optical_photo_adapter import OpticalPhotoAdapter


def _stable_path(path: str, data_root: Optional[str]) -> str:
    """把绝对路径转换为相对于数据根目录的稳定路径。"""
    if not data_root:
        return path
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(data_root))
    except ValueError:  # 不同盘符（Windows）
        return path
    return relative.replace(os.sep, "/")


def _write_jsonl(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")


def _summarize(records: List[Dict[str, Any]], rejected: List[Dict[str, Any]]) -> Dict[str, Any]:
    issue_counts: Dict[str, int] = {}
    for record in records:
        for issue in record.get("issues") or []:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    reject_counts: Dict[str, int] = {}
    for row in rejected:
        reason = row.get("reason", "unknown")
        reject_counts[reason] = reject_counts.get(reason, 0) + 1

    tracks = sorted({str(r.get("local_track_id")) for r in records})
    missing_counts = {
        field: sum(1 for r in records if r.get(field) is None)
        for field in (
            "state_time_utc",
            "state_time_raw",
            "speed_mps",
            "course_rad",
            "heading_rad",
            "course_reference",
            "motion_reference",
            "receive_time_utc",
        )
    }

    return {
        "output_count": len(records),
        "rejected_count": len(rejected),
        "total_count": len(records) + len(rejected),
        "track_count": len(tracks),
        "track_ids": tracks,
        "issue_counts": issue_counts,
        "reject_reason_counts": reject_counts,
        "missing_value_counts": missing_counts,
        "valid_for_spatial_alignment": sum(1 for r in records if r.get("valid_for_spatial_alignment")),
        "valid_for_time_alignment": sum(1 for r in records if r.get("valid_for_time_alignment")),
    }


def _default_data_root(input_path: str) -> str:
    """未显式配置 data_root 时，取输入路径的目录（单文件）或其父目录（目录）。

    这样 source_file 至少是相对于数据目录的短路径，避免把绝对路径写进
    record_id，使同一批数据在移动后仍保持 ID 稳定。
    """
    if os.path.isdir(input_path):
        return os.path.dirname(os.path.abspath(input_path)) or os.path.abspath(input_path)
    return os.path.dirname(os.path.abspath(input_path)) or os.path.abspath(input_path)


def _quantization_specs(
    source_config: Dict[str, Any],
) -> tuple:
    """从配置读取经纬度量化分辨率。

    依据必须来自数据说明或已核验的原始 CSV 数值分辨率；未配置时返回
    confident=False 的规格，此时不自动扣除裕度，只保留原始量并标记不确定。
    """
    quant = source_config.get("position_quantization") or {}

    def build(key: str, axis: str) -> validation.QuantizationSpec:
        item = quant.get(key) or {}
        return validation.QuantizationSpec(
            resolution_deg=item.get("resolution_deg"),
            basis=item.get("basis"),
            confident=bool(item.get("confident", False)),
            axis=axis,
        )

    return build("longitude", "longitude"), build("latitude", "latitude")


def standardize(
    input_path: str,
    source_config: Dict[str, Any],
    output_dir: str,
) -> Dict[str, Any]:
    """把一个数据源的输入标准化为公共记录并落盘。

    source_config 需包含：source_id、source_type、session_id，
    以及与输入类型匹配的字段映射与单位/时间设置。
    """
    source_type = source_config.get("source_type")
    os.makedirs(output_dir, exist_ok=True)
    data_root = source_config.get("data_root") or _default_data_root(input_path)

    log: List[str] = []
    log.append(f"配置版本 schema={schema.SCHEMA_VERSION}")
    log.append(f"source_id={source_config.get('source_id')} source_type={source_type}")
    log.append(f"session_id={source_config.get('session_id')}")
    log.append(f"输入 {input_path}")
    log.append(f"数据根目录 {data_root or '(未指定，source_file 使用输入路径)'}")
    log.append(f"输出目录 {output_dir}")

    sim_context = None
    if source_type == schema.SOURCE_TYPE_RADAR:
        adapter = radar_ttm_adapter.RadarTTMAdapter(source_config)
        sim_context, ctx_log = radar_ttm_adapter.build_simulation_context(
            source_config.get("all_incoming_csv")
        )
        log.extend(ctx_log)
        records, rejected, run_log = adapter.run(input_path, sim_context=sim_context)
    elif source_type == schema.SOURCE_TYPE_AIS:
        adapter = AISCsvAdapter(source_config)
        records, rejected, run_log = adapter.run(input_path)
    elif source_type == schema.SOURCE_TYPE_AIS_VDM:
        adapter = AisVdmAdapter(source_config)
        sim_context, ctx_log = radar_ttm_adapter.build_simulation_context(
            source_config.get("all_incoming_csv")
        )
        log.extend(ctx_log)
        records, rejected, run_log = adapter.run(input_path, sim_context=sim_context)
    elif source_type == schema.SOURCE_TYPE_RADAR_CUTDATA:
        adapter = RadarCutDataAdapter(source_config)
        records, rejected, run_log = adapter.run(input_path)
    elif source_type == schema.SOURCE_TYPE_OPTICAL:
        adapter = OpticalPhotoAdapter(source_config)
        records, rejected, run_log = adapter.run(input_path)
    else:
        raise ValueError(f"未知 source_type: {source_type}")

    log.extend(run_log)

    # 统一 source_file 为相对于数据根目录的稳定路径，并据此重算 record_id
    for record in records:
        record["source_file"] = _stable_path(str(record.get("source_file")), data_root)
        record["record_id"] = schema.make_record_id(
            str(record.get("source_id")),
            str(record.get("source_file")),
            record.get("source_row"),
            session_id=str(record.get("session_id") or ""),
        )
    for row in rejected:
        row["source_file"] = _stable_path(str(row.get("source_file")), data_root)
        row["record_id"] = schema.make_record_id(
            str(row.get("source_id")),
            str(row.get("source_file")),
            row.get("source_row"),
            session_id=str(row.get("session_id") or ""),
        )

    # 重复与跳变检查（只标记，不删除、不修改轨迹）
    # MTDSP 雷达侧的同一 (目标, 时间戳) 可对应两条不同帧号的记录
    # （雷达帧率高于检测输出率的正常降采样），因此改用帧号参与去重键，
    # 避免把帧率降采样误标为重复记录。
    if source_type == schema.SOURCE_TYPE_RADAR_CUTDATA:
        dup_key = ["local_track_id", "state_time_raw", "radar_frame_index"]
    elif source_type == schema.SOURCE_TYPE_OPTICAL:
        # 光电的同一时刻会有多个画面内检测目标，且主目标记录与检测记录
        # 使用不同的 local_track_id 语义，因此去重键必须带上检测序号。
        # 另外：同一时刻同时存在 Inf(红外) 与 TV(可见光) 两个设备对同一场景的
        # 观测（03-05 包 46/46 个时刻均如此），两者是**独立观测**而非重复，
        # 因此去重键必须包含设备标识。
        dup_key = [
            "optical_device",
            "local_track_id",
            "state_time_raw",
            "optical_record_kind",
            "optical_target_index",
            "optical_detection_index",
        ]
    else:
        dup_key = ["local_track_id", "state_time_raw"]
    validation.detect_duplicates(records, dup_key)
    if source_type == schema.SOURCE_TYPE_AIS:
        lon_q, lat_q = _quantization_specs(source_config)
        log.append(
            f"ais: 位置量化配置 经度={lon_q.describe()} 纬度={lat_q.describe()}"
        )
        validation.detect_position_jumps(
            records,
            max_speed_mps=source_config.get("position_jump_max_speed_mps", 15.0),
            lon_quantization=lon_q,
            lat_quantization=lat_q,
        )

    ordered = [schema.order_record(r) for r in records]

    _write_jsonl(os.path.join(output_dir, "standard_records.jsonl"), ordered)
    _write_jsonl(os.path.join(output_dir, "rejected_records.jsonl"), rejected)

    summary = {
        "source_id": source_config.get("source_id"),
        "source_type": source_type,
        "session_id": source_config.get("session_id"),
        "input_path": input_path,
        "schema_version": schema.SCHEMA_VERSION,
        "config": source_config,
    }
    summary.update(_summarize(records, rejected))

    with open(os.path.join(output_dir, "quality_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    with open(os.path.join(output_dir, "processing.log"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(log) + "\n")

    return summary


def standardize_many(
    jobs: List[Dict[str, Any]],
    output_root: str,
) -> List[Dict[str, Any]]:
    """按配置批量标准化多个数据源。

    jobs 每项：{"input_path", "source_config", "output_subdir"}
    """
    summaries = []
    for job in jobs:
        output_dir = os.path.join(output_root, job["output_subdir"])
        summaries.append(
            standardize(job["input_path"], job["source_config"], output_dir)
        )
    return summaries
