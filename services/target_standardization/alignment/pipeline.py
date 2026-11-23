"""对齐流水线：读标准化结果 → 空间对齐 → 时间整理 → 独立评测。

产出：
  aligned_records.csv        两路对齐后的逐条记录（保留原始字段 + ENU + 界限）
  aligned_grid_records.csv   插值到公共时间栅格后的记录
  alignment_quality.json     成功/失败/插值统计与失败原因
  alignment_evaluation.json  与真值比较的位置误差、时间误差、覆盖率
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, List, Optional

from .bounds import BoundCalculator
from .config import INTERP_ENU_FIRST, AlignmentConfig
from .evaluation import AlignmentEvaluator
from .spatial import SpatialAligner
from .temporal import TemporalAligner, format_iso as _format_epoch
from .track_export import TrackExporter
from .transform import latlon_to_enu, radar_polar_to_enu
from .verification import CoordinateVerifier
from .transform import FrameConverter


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


ALIGNED_FIELDS = [
    "record_id",
    "source_id",
    "source_type",
    "session_id",
    "local_track_id",
    "state_time_utc",
    "aligned_time_utc",
    "aligned_clock_offset_applied_s",
    "aligned_time_quantum_bias_applied_s",
    "state_time_raw",
    "time_basis",
    "time_precision",
    "receive_time_utc",
    "position_type",
    "frame_id",
    "east_m",
    "north_m",
    "up_m",
    "alignment_frame_id",
    "alignment_status",
    "alignment_method",
    "alignment_failure_reason",
    "alignment_bearing_reference_used",
    "alignment_install_heading_applied",
    "position_latitude_deg",
    "position_longitude_deg",
    "range_m",
    "bearing_rad",
    "speed_mps",
    "course_rad",
    "error_bound_planar_m",
    "error_bound_radial_m",
    "error_bound_tangential_m",
    "error_bound_components",
    # 雷达侧扩展字段（区分测量来源与目标类型）
    "radar_source_format",
    "radar_target_type",
    "radar_measurement_kind",
    "radar_detection_box",
    "radar_frame_index",
    "optical_match_id",
    "optical_match_time_raw",
    # 光电侧扩展字段
    "optical_source_format",
    "optical_device",
    "optical_band",
    "optical_record_kind",
    "optical_primary_mmsi",
    "optical_target_index",
    "optical_detection_index",
    "optical_type_raw",
    "optical_type_category",
    "optical_type_label",
    "optical_confidence",
    "optical_range_m",
    "optical_azimuth_deg",
    "optical_range_source",
    "optical_detection_out_of_range",
    "optical_range_filter_limit_m",
    "optical_box_center_x_px",
    "optical_box_center_y_px",
    "optical_box_width_px",
    "optical_box_height_px",
    # 雷达平台自身状态（IF/IQ 帧头：平台航向角与速度）
    "platform_heading_deg",
    "platform_speed_mps",
    "platform_state_frame_index",
    "platform_state_time_raw",
    "platform_state_time_delta_s",
    "platform_state_match_basis",
    "source_file",
    "source_row",
]

GRID_FIELDS = [
    "time_utc",
    "source_id",
    "source_type",
    "local_track_id",
    "east_m",
    "north_m",
    "up_m",
    "alignment_frame_id",
    "alignment_status",
    "speed_mps",
    "course_rad",
    "interpolation_method",
    "interpolation_mode",
    "interpolation_available",
    "interpolation_gap_s",
    "interpolation_ratio",
    "interpolation_reason",
    "before_source_row",
    "after_source_row",
    "before_state_time_utc",
    "after_state_time_utc",
    "time_resolution_s",
]


def _flatten(record: Dict[str, Any]) -> Dict[str, Any]:
    """把嵌套的 position 字段摊平，便于 CSV 查看。"""
    row = dict(record)
    position = record.get("position") or {}
    row["position_latitude_deg"] = position.get("latitude_deg")
    row["position_longitude_deg"] = position.get("longitude_deg")
    row["range_m"] = position.get("range_m")
    row["bearing_rad"] = position.get("bearing_rad")
    for key in ("east_m", "north_m", "up_m"):
        if key not in row:
            row[key] = None
    bound = record.get("error_bound") or {}
    row["error_bound_planar_m"] = bound.get("planar_m")
    row["error_bound_radial_m"] = bound.get("radial_m")
    row["error_bound_tangential_m"] = bound.get("tangential_m")
    row["error_bound_components"] = (
        json.dumps(bound.get("components", {}), ensure_ascii=False)
        if bound
        else None
    )
    return row


def _group_tracks_by_sensor(
    tracks: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """按传感器类型分组航迹清单。"""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for track in tracks:
        grouped.setdefault(str(track.get("sensor_type")), []).append(track)
    return grouped


def _annotate_clock_offset(
    evaluation: Dict[str, Any], alignment_config: AlignmentConfig
) -> None:
    """把配置里的偏移/量化偏差与实测残差对照，说明残差是否已被解释。

    残差 = 配置的时钟偏移 + 配置的量化偏差 + 未解释部分。
    未解释部分应接近 0；若不为 0，说明配置缺项或数据与配置不符。
    """
    correction = evaluation.get("clock_offset_correction") or {}
    for source in alignment_config.sources:
        stats = correction.get(source.source_type)
        if stats is None:
            continue
        configured_offset = source.time.known_clock_offset_s
        configured_bias = source.time.known_quantum_bias_s
        stats["configured_clock_offset_s"] = configured_offset
        stats["configured_quantum_bias_s"] = configured_bias
        stats["configured_total_s"] = configured_offset + configured_bias
        before = stats.get("before_correction_s") or {}
        after = stats.get("after_correction_s") or {}
        stats["residual_before_correction_s"] = before.get("mean")
        stats["residual_after_correction_s"] = after.get("mean")
        stats["unexplained_after_correction_s"] = (
            None if after.get("mean") is None else after.get("mean")
        )
        stats["residual_explained"] = (
            after.get("mean") is not None and abs(after.get("mean")) <= 1e-6
        )
        stats["residual_explanation"] = (
            f"读数偏差 = 时钟偏移 {configured_offset:+.3f} s"
            + (
                f" + 时间量化偏差 {configured_bias:+.3f} s"
                f"（时间字段步长 {source.time.time_quantum_s} s）"
                if source.time.time_quantum_s
                else ""
            )
            + f"；修正后残差 {after.get('mean', 0.0):+.6f} s"
        )


def _collect_missing_requirements(
    records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """汇总各数据源缺失的对齐前提（站址、外参、时间基准等）。"""
    by_source: Dict[str, Dict[str, int]] = {}
    for record in records:
        source_id = str(record.get("source_id"))
        bucket = by_source.setdefault(source_id, {})
        for item in record.get("alignment_missing_requirements") or []:
            bucket[item] = bucket.get(item, 0) + 1
        reason = record.get("alignment_failure_reason")
        if reason and record.get("alignment_status") == "failed":
            if reason not in bucket:
                bucket[reason] = bucket.get(reason, 0) + 1
    return {
        "by_source": by_source,
        "note": (
            "缺少站址/外参/可靠时间的记录不会被静默丢弃："
            "空间转换失败时 alignment_status=failed 并记录原因；"
            "无时间基准的记录不进入插值序列"
        ),
    }


def _summarize_platform_state(
    records: List[Dict[str, Any]], aligner: SpatialAligner
) -> Dict[str, Any]:
    """汇总雷达平台自身状态（IF/IQ 帧头）的关联情况。

    该信息描述**平台自身**，与被观测目标无关；它不参与方位→ENU 换算，
    仅作记录与追溯。
    """
    index = getattr(aligner, "platform_state_index", None)
    if index is None:
        return {
            "enabled": False,
            "note": "未配置 platform_state，既有行为不变",
        }
    headings = [
        r["platform_heading_deg"]
        for r in records
        if r.get("platform_heading_deg") is not None
    ]
    speeds = [
        r["platform_speed_mps"]
        for r in records
        for _ in (0,)
        if r.get("platform_speed_mps") is not None
    ]
    by_basis: Dict[str, int] = {}
    for record in records:
        basis = record.get("platform_state_match_basis")
        if basis:
            by_basis[basis] = by_basis.get(basis, 0) + 1
    summary: Dict[str, Any] = {
        "enabled": True,
        "if_source_file": index.source_file,
        "if_frame_count": len(index),
        "records_with_platform_state": len(headings),
        "match_basis_counts": by_basis,
        "applied_to_bearing_conversion": False,
        "note": (
            "平台航向/速度描述平台自身（固定岸基站的 GNSS 状态），"
            "不参与方位→ENU 换算：实测证明方位基准为真北固定基准，"
            "与平台艏向无关（权威 AIS 反算真方位：不修正残差中位数 "
            "0.45°/0.53°，叠加平台航向恶化到 6.60°/6.43°，"
            "叠加角度修正恶化到 15.55°/15.87°）"
        ),
    }
    if headings:
        summary["heading_deg"] = {
            "min": min(headings),
            "max": max(headings),
            "mean": sum(headings) / len(headings),
        }
    if speeds:
        summary["speed_mps"] = {
            "min": min(speeds),
            "max": max(speeds),
            "mean": sum(speeds) / len(speeds),
        }
    return summary


def _grid_to_enu(
    grid_point, aligner: SpatialAligner, alignment_config: AlignmentConfig
) -> Dict[str, Any]:
    """把栅格上的插值结果整理成输出行。

    `enu_first` 模式下 ENU 已由插值直接给出；`observation_domain` 模式下
    需要在此把观测域的位置转到现场 ENU。
    """
    info = grid_point.interpolation
    row = {
        "time_utc": grid_point.time_utc,
        "source_id": grid_point.source_id,
        "source_type": grid_point.source_type,
        "local_track_id": grid_point.local_track_id,
        "alignment_frame_id": alignment_config.site_frame.frame_id,
        "speed_mps": grid_point.speed_mps,
        "course_rad": grid_point.course_rad,
        "interpolation_method": info.get("method"),
        "interpolation_mode": info.get("mode"),
        "interpolation_available": info.get("available"),
        "interpolation_gap_s": info.get("gap_s"),
        "interpolation_ratio": info.get("ratio"),
        "interpolation_reason": info.get("reason"),
        "before_source_row": info.get("before_source_row"),
        "after_source_row": info.get("after_source_row"),
        "before_state_time_utc": info.get("before_state_time_utc"),
        "after_state_time_utc": info.get("after_state_time_utc"),
        "time_resolution_s": info.get("time_resolution_s"),
        "east_m": None,
        "north_m": None,
        "up_m": None,
        "alignment_status": "failed",
    }

    if not info.get("available"):
        row["alignment_status"] = "unavailable"
        return row

    if grid_point.east_m is not None and grid_point.north_m is not None:
        # enu_first：ENU 已由插值给出
        row["east_m"] = grid_point.east_m
        row["north_m"] = grid_point.north_m
        row["up_m"] = grid_point.up_m
        row["alignment_status"] = "ok"
        return row

    position = grid_point.position
    if not position:
        row["alignment_status"] = "unavailable"
        row["interpolation_reason"] = "empty_position"
        return row

    if grid_point.source_type in ("radar", "radar_cutdata", "optical"):
        # 光电与雷达同址部署，距离/真方位的定义方式完全一致，
        # 必须走同一条极坐标转换路径；否则会被误当作经纬度源处理。
        station = alignment_config.radar_station
        enu = radar_polar_to_enu(
            aligner.frame,
            station_latitude_deg=station.station_latitude_deg,
            station_longitude_deg=station.station_longitude_deg,
            station_height_m=station.station_height_m,
            range_m=float(position["range_m"]),
            bearing_true_rad=float(position["bearing_rad"]),
        )
    else:
        enu = latlon_to_enu(
            aligner.frame,
            float(position["latitude_deg"]),
            float(position["longitude_deg"]),
        )

    row["east_m"] = enu.east_m
    row["north_m"] = enu.north_m
    row["up_m"] = enu.up_m
    row["alignment_status"] = "ok"
    return row


def run_alignment(
    *,
    radar_records_path: str,
    ais_records_path: str,
    alignment_config: AlignmentConfig,
    output_dir: str,
    truth_path: Optional[str] = None,
    mapping_path: Optional[str] = None,
    extra_records_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """执行对齐并落盘。

    truth_path / mapping_path 只传给评测器，对齐过程不读取它们。

    extra_records_paths 用于接入**第三类传感器**（如 MTDSP 光电）。这些源
    在对齐配置中各自声明 source_id 与 source_type，走与雷达相同的
    range_bearing → ENU 转换路径，但保留自己的 source_type 以便区分。
    """
    os.makedirs(output_dir, exist_ok=True)

    # -- 配置校验：缺少站址/外参/时间时给出明确的不可用原因 -----------------
    config_problems = alignment_config.validate()

    radar_records = read_jsonl(radar_records_path)
    ais_records = read_jsonl(ais_records_path)
    records = radar_records + ais_records
    for extra_path in extra_records_paths or []:
        records.extend(read_jsonl(extra_path))

    aligner = SpatialAligner(alignment_config)

    # -- 独立坐标校验（与被校验代码不同的实现）------------------------------
    verifier = CoordinateVerifier(
        origin_latitude_deg=alignment_config.site_frame.origin_latitude_deg,
        origin_longitude_deg=alignment_config.site_frame.origin_longitude_deg,
        origin_height_m=alignment_config.site_frame.origin_height_m,
        project_converter=FrameConverter(aligner.frame),
    )
    coordinate_checks = [check.to_dict() for check in verifier.run_all()]

    quality_stats = aligner.align_records(records)

    # -- 位置误差上界 -------------------------------------------------------
    bound_calculator = BoundCalculator(alignment_config)
    for record in records:
        bound = bound_calculator.bound_for_record(record)
        if bound is not None:
            record["error_bound"] = bound.to_dict()

    # -- 时间整理与插值到公共栅格 -------------------------------------------
    temporal = TemporalAligner(alignment_config, spatial_aligner=aligner)
    tracks = temporal.build_tracks(records)
    grid = temporal.grid_times(tracks)
    grid_rows = []
    temporal_stats = {
        "interpolation_mode": alignment_config.interpolation_mode,
        "track_count": len(tracks),
        "grid_point_count": len(grid),
        "grid_step_s": alignment_config.output_time_step_s,
        "max_interpolation_gap_s": alignment_config.max_interpolation_gap_s,
        "by_track": {},
    }

    # 把修正后的公共时间写回记录，供评测检验偏移修正效果
    for record in records:
        source_config = alignment_config.source_config(str(record.get("source_id")))
        corrected = temporal.corrected_state_epoch(record, source_config)
        if corrected is not None:
            record["aligned_time_utc"] = temporal._format_grid_time(corrected)
            record["aligned_clock_offset_applied_s"] = (
                source_config.time.known_clock_offset_s if source_config else 0.0
            )
            record["aligned_time_quantum_bias_applied_s"] = (
                source_config.time.known_quantum_bias_s if source_config else 0.0
            )

    # -- aligned_records.csv ------------------------------------------------
    # （写在时间修正之后，保证 CSV 里的 aligned_time_utc 已填好）
    aligned_path = os.path.join(output_dir, "aligned_records.csv")
    flattened = [_flatten(record) for record in records]
    with open(aligned_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=ALIGNED_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(flattened)

    for key, points in tracks.items():
        # 按航迹自身时间范围生成栅格：公共栅格对短航迹会产生大量
        # "超出航迹范围"的冗余不可用记录，且不改变任何可用点的插值结果。
        track_grid = temporal.grid_times_for_track(points)
        results = temporal.interpolate_track(points, track_grid)
        track_stats = {
            "source_id": points[0].source_id,
            "source_type": points[0].source_type,
            "local_track_id": points[0].local_track_id,
            "source_point_count": len(points),
            "interpolated": 0,
            "exact": 0,
            "unavailable": 0,
            "unavailable_reasons": {},
        }
        for result in results:
            row = _grid_to_enu(result, aligner, alignment_config)
            grid_rows.append(row)
            info = result.interpolation
            if not info.get("available"):
                track_stats["unavailable"] += 1
                reason = info.get("reason", "unknown")
                track_stats["unavailable_reasons"][reason] = (
                    track_stats["unavailable_reasons"].get(reason, 0) + 1
                )
            elif info.get("method") == "exact":
                track_stats["exact"] += 1
            else:
                track_stats["interpolated"] += 1
        temporal_stats["by_track"]["|".join(key)] = track_stats

    grid_path = os.path.join(output_dir, "aligned_grid_records.csv")
    with open(grid_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GRID_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(grid_rows)

    # -- tracks/ （按传感器分组的完整航迹，供航迹关联使用）------------------
    # 从**原始观测**而非插值栅格构建：栅格点是插值产物，会把插值误差
    # 引入关联门限，并丢失观测语义（测量类型、误差上界、原始行号）。
    track_exporter = TrackExporter(
        origin_latitude_deg=alignment_config.site_frame.origin_latitude_deg,
        origin_longitude_deg=alignment_config.site_frame.origin_longitude_deg,
        frame_id=alignment_config.site_frame.frame_id,
    )
    track_list, track_points, radar_detections = track_exporter.build(records)
    track_result = track_exporter.write(
        output_dir, track_list, track_points, radar_detections
    )

    # -- alignment_quality.json --------------------------------------------
    quality = {
        "alignment_config_id": alignment_config.alignment_config_id,
        "parameter_version": alignment_config.parameter_version,
        "module_version": alignment_config.module_version,
        "config_validation": {
            "problems": config_problems,
            "usable": not config_problems,
        },
        "site_frame": {
            "frame_id": alignment_config.site_frame.frame_id,
            "datum": alignment_config.site_frame.datum,
            "origin_latitude_deg": alignment_config.site_frame.origin_latitude_deg,
            "origin_longitude_deg": alignment_config.site_frame.origin_longitude_deg,
            "origin_height_m": alignment_config.site_frame.origin_height_m,
        },
        "radar_station": {
            "station_latitude_deg": alignment_config.radar_station.station_latitude_deg,
            "station_longitude_deg": alignment_config.radar_station.station_longitude_deg,
            "station_height_m": alignment_config.radar_station.station_height_m,
            "install_heading_deg": alignment_config.radar_station.install_heading_deg,
            "install_heading_applied_to_ttm_bearing": False,
            "install_heading_note": (
                "TTM 方位参考为 T（真北）时方位已是真方位，不叠加安装方向"
                "或本船艏向；该参数仅作记录。"
            ),
            "reference_point_offset_east_m": alignment_config.radar_station.reference_point_offset_east_m,
            "reference_point_offset_north_m": alignment_config.radar_station.reference_point_offset_north_m,
            "reference_point_offset_up_m": alignment_config.radar_station.reference_point_offset_up_m,
            "magnetic_variation_deg": alignment_config.radar_station.magnetic_variation_deg,
        },
        "coordinate_verification": {
            "note": (
                "独立实现校验：与 alignment.transform 使用不同的数学路径，"
                "避免共用代码掩盖同一错误"
            ),
            "all_passed": all(item["passed"] for item in coordinate_checks),
            "checks": coordinate_checks,
        },
        "spatial": quality_stats,
        "temporal": temporal_stats,
        "tracks": {
            "note": (
                "按传感器分组的完整航迹（输出在 tracks/ 下），"
                "从原始观测构建、供航迹关联使用。"
                "雷达 CDT 的 900xxx 内部号非稳定航迹号，已标记为 "
                "unstable_internal_id，不可直接作关联主键。"
            ),
            "track_count": len(track_list),
            "point_count": sum(int(item["point_count"]) for item in track_list),
            "radar_detection_count": len(radar_detections),
            "by_sensor": {
                sensor: {
                    "track_count": len(items),
                    "point_count": sum(int(i["point_count"]) for i in items),
                    "stable_track_count": sum(
                        1 for i in items if i["track_stability"] == "stable"
                    ),
                }
                for sensor, items in _group_tracks_by_sensor(track_list).items()
            },
        },
        "missing_requirements": _collect_missing_requirements(records),
        "platform_state": _summarize_platform_state(records, aligner),
    }

    # -- alignment_evaluation.json ----------------------------------------
    evaluation: Dict[str, Any] = {}
    if truth_path:
        evaluator = AlignmentEvaluator(
            truth_path,
            mapping_path,
            frame_origin=(
                alignment_config.site_frame.origin_latitude_deg,
                alignment_config.site_frame.origin_longitude_deg,
            ),
        )
        evaluation = evaluator.evaluate_records(records)
        evaluation["cross_source_consistency"] = evaluator.cross_source_consistency(
            records
        )
        evaluation["grid"] = evaluator.evaluate_grid(grid_rows)
        evaluation["clock_offset_correction"] = (
            evaluator.verify_clock_offset_correction(records)
        )
        _annotate_clock_offset(evaluation, alignment_config)
        evaluation["coverage"] = evaluator.evaluate_coverage(records)
        evaluation["bounds"] = evaluator.evaluate_bounds(records)
        quality["evaluation_summary"] = {
            "matched_count": evaluation.get("matched_count"),
            "radar_planar_error_m": evaluation.get("position_error_by_sensor", {})
            .get("radar", {})
            .get("planar_m", {}),
            "ais_planar_error_m": evaluation.get("position_error_by_sensor", {})
            .get("ais", {})
            .get("planar_m", {}),
        }

    _write_json(os.path.join(output_dir, "alignment_quality.json"), quality)
    if evaluation:
        _write_json(
            os.path.join(output_dir, "alignment_evaluation.json"), evaluation
        )

    return {
        "aligned_records": aligned_path,
        "grid_records": grid_path,
        "tracks": track_result,
        "track_list": track_list,
        "quality": quality,
        "evaluation": evaluation,
        "records": records,
        "grid_rows": grid_rows,
    }
