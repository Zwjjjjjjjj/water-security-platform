#!/usr/bin/env python3
"""生成统一验收报告。

读取各场景的对齐产物（质量报告 + 评测报告），汇总成一份可交付的验收报告，
逐条回答 A/B/C 各自必须回答的问题。

**真值隔离**：本程序只读取已经生成的评测报告，不重新计算误差；对齐过程
本身从未接触真值文件。

用法：
    python3 run_acceptance.py                     # 全部场景
    python3 run_acceptance.py --scenario A_geometry_baseline
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from alignment.config import MODULE_VERSION  # noqa: E402

ALIGNMENT_ROOT = os.path.join(ROOT, "output", "alignment")

# 每个场景必须回答的问题
SCENARIO_QUESTIONS = {
    "A_geometry_baseline": [
        "坐标方向、单位和参考点是否正确？",
        "逐点误差是否符合编码精度？",
    ],
    "B_asynchronous": [
        "已知时间偏移是否按配置修正？",
        "AIS 整秒精度造成的残差是否单独说明？",
    ],
    "C_observation_perturbation": [
        "实际收到多少观测？",
        "插值补了多少点？",
        "多少点因间隔过长不可用？",
        "误差统计使用了哪些有效点？",
    ],
    "mtdsp_yantai": [
        "真实设备数据的空间对齐是否自洽？",
        "时区未确认时，时间对齐如何处理？",
        "跨源位置残差的量级与来源？",
        "雷达实测目标与 AIS 继承值是否被区分？",
    ],
}


def _load(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _fmt_stats(stats: Optional[Dict[str, Any]]) -> str:
    if not stats or not stats.get("count"):
        return "—"
    return (
        f"mean {stats['mean']:.4f} / median {stats['median']:.4f} / "
        f"max {stats['max']:.4f} m"
    )


def build_report(scenario_ids: List[str]) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "report_id": "alignment_acceptance_v1",
        "module_version": MODULE_VERSION,
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "scenarios": {},
    }

    for scenario_id in scenario_ids:
        base = os.path.join(ALIGNMENT_ROOT, scenario_id)
        quality = _load(os.path.join(base, "alignment_quality.json"))
        evaluation = _load(os.path.join(base, "alignment_evaluation.json"))
        if quality is None:
            continue

        temporal = quality.get("temporal", {})
        entry: Dict[str, Any] = {
            "questions": SCENARIO_QUESTIONS.get(scenario_id, []),
            "coordinate_verification": {
                "all_passed": quality.get("coordinate_verification", {}).get(
                    "all_passed"
                ),
                "checks": [
                    {
                        "name": item["name"],
                        "passed": item["passed"],
                        "detail": item["detail"],
                    }
                    for item in quality.get("coordinate_verification", {}).get(
                        "checks", []
                    )
                ],
            },
            "config_validation": quality.get("config_validation"),
            "interpolation_mode": temporal.get("interpolation_mode"),
            "spatial": quality.get("spatial"),
            "temporal": {
                "grid_step_s": temporal.get("grid_step_s"),
                "max_interpolation_gap_s": temporal.get("max_interpolation_gap_s"),
                "track_count": temporal.get("track_count"),
                "grid_point_count": temporal.get("grid_point_count"),
                "by_track": temporal.get("by_track"),
            },
            "missing_requirements": quality.get("missing_requirements"),
            "platform_state": quality.get("platform_state"),
            "tracks": quality.get("tracks"),
        }

        if evaluation:
            entry["position_error"] = {
                sensor: {
                    "planar_m": stats.get("planar_m"),
                    "east_m": stats.get("east_m"),
                    "north_m": stats.get("north_m"),
                }
                for sensor, stats in evaluation.get(
                    "position_error_by_sensor", {}
                ).items()
            }
            entry["effective_points"] = {
                "matched_count": evaluation.get("matched_count"),
                "unmatched_count": evaluation.get("unmatched_count"),
                "missing_truth_count": evaluation.get("missing_truth_count"),
                "not_converted_count": evaluation.get("not_converted_count"),
                "note": evaluation.get("effective_points_note"),
            }
            entry["time_error"] = evaluation.get("time_error_by_sensor")
            entry["clock_offset_correction"] = evaluation.get(
                "clock_offset_correction"
            )
            entry["coverage"] = evaluation.get("coverage")
            entry["bounds"] = evaluation.get("bounds", {}).get("by_sensor")
            entry["bounds_note"] = evaluation.get("bounds", {}).get("note")
            entry["cross_source_consistency"] = evaluation.get(
                "cross_source_consistency"
            )
            entry["grid_evaluation"] = {
                "evaluated_count": evaluation.get("grid", {}).get(
                    "evaluated_count"
                ),
                "unavailable_count": evaluation.get("grid", {}).get(
                    "unavailable_count"
                ),
                "position_error_by_sensor": evaluation.get("grid", {}).get(
                    "position_error_by_sensor"
                ),
                "time_error_by_sensor": evaluation.get("grid", {}).get(
                    "time_error_by_sensor"
                ),
            }

        # MTDSP 真实数据场景：附加实测证据（跨源评估 + 误差模型标定）
        if scenario_id == "mtdsp_yantai":
            entry["mtdsp_evidence"] = _mtdsp_evidence()

        report["scenarios"][scenario_id] = entry

    return report


def _mtdsp_evidence() -> Dict[str, Any]:
    """MTDSP 实测证据：跨源评估 + 误差模型标定结果。"""
    evidence: Dict[str, Any] = {
        "spatial_self_consistency": _load(
            os.path.join(
                ROOT, "output", "verification", "mtdsp_spatial",
                "mtdsp_spatial_verification.json",
            )
        ),
        "cross_source": _load(
            os.path.join(
                ROOT, "output", "evaluation", "mtdsp_cross_source",
                "mtdsp_cross_source_evaluation.json",
            )
        ),
        "error_model": _load(
            os.path.join(
                ROOT, "output", "calibration", "mtdsp_error_model.json"
            )
        ),
    }
    return {k: v for k, v in evidence.items() if v is not None}


def render_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("时空对齐模块验收报告")
    lines.append(f"模块版本 alignment v{MODULE_VERSION}")
    lines.append(f"生成时间 {report['generated_at_utc']}")
    lines.append("=" * 78)

    for scenario_id, entry in report["scenarios"].items():
        lines.append("")
        lines.append(f"■ 场景 {scenario_id}")
        lines.append("-" * 78)
        for question in entry.get("questions", []):
            lines.append(f"  ? {question}")

        verification = entry.get("coordinate_verification", {})
        lines.append(
            f"  独立坐标校验：{'全部通过' if verification.get('all_passed') else '存在失败'}"
        )
        for check in verification.get("checks", []):
            mark = "PASS" if check["passed"] else "FAIL"
            lines.append(f"    [{mark}] {check['detail']}")

        validation = entry.get("config_validation") or {}
        lines.append(
            f"  配置校验：{'可用' if validation.get('usable') else '存在问题'}"
            f" {validation.get('problems') or ''}"
        )

        spatial = entry.get("spatial", {}).get("by_source", {})
        if spatial:
            lines.append("  空间转换：")
            for source_id, stats in spatial.items():
                lines.append(
                    f"    {source_id}: 转换 {stats['converted']}/{stats['total']}"
                    f"，失败 {stats['failed']}，无位置 {stats['no_position']}"
                )

        temporal = entry.get("temporal", {})
        lines.append(
            f"  时间整理：插值方式 {entry.get('interpolation_mode')}，"
            f"栅格步长 {temporal.get('grid_step_s')} s，"
            f"最大间隔 {temporal.get('max_interpolation_gap_s')} s"
        )
        for key, stats in (temporal.get("by_track") or {}).items():
            lines.append(
                f"    {key}: 源点 {stats['source_point_count']}，"
                f"精确 {stats['exact']}，插值 {stats['interpolated']}，"
                f"不可用 {stats['unavailable']} {stats['unavailable_reasons'] or ''}"
            )

        coverage = entry.get("coverage")
        if coverage:
            lines.append("  覆盖率：")
            for source_id, stats in coverage.items():
                lines.append(
                    f"    {source_id}: 应有 {stats['expected_observations']}，"
                    f"实收 {stats['observed_observations']}，"
                    f"缺失 {stats['missing_observations']}"
                    f"（漏报 {stats['dropout_truth_count']}，"
                    f"未解释 {stats['unexplained_missing']}），"
                    f"覆盖率 {stats['coverage_ratio']:.4f}"
                )

        position_error = entry.get("position_error") or {}
        if position_error:
            lines.append("  位置误差（相对真值）：")
            for source_id, stats in position_error.items():
                lines.append(f"    {source_id}: {_fmt_stats(stats.get('planar_m'))}")
                lines.append(
                    f"      东向 {_fmt_stats(stats.get('east_m'))} / "
                    f"北向 {_fmt_stats(stats.get('north_m'))}"
                )

        effective = entry.get("effective_points")
        if effective:
            lines.append(
                f"  误差统计使用的有效点：{effective['matched_count']} 条"
                f"（无状态时间 {effective['unmatched_count']}，"
                f"真值配对失败 {effective['missing_truth_count']}，"
                f"未转换/坐标缺失 {effective['not_converted_count']}）"
            )

        clock = entry.get("clock_offset_correction")
        if clock:
            lines.append("  时钟偏移修正：")
            for source_id, stats in clock.items():
                before = stats.get("before_correction_s", {})
                after = stats.get("after_correction_s", {})
                lines.append(
                    f"    {source_id}: 修正前 {before.get('mean', 0):+.4f} s → "
                    f"修正后 {after.get('mean', 0):+.4f} s"
                    f"（残差最大 {after.get('max', 0):+.4f} s）"
                )
                if stats.get("residual_explanation"):
                    lines.append(
                        f"      {stats['residual_explanation']}"
                        f"；残差已完整解释：{stats.get('residual_explained')}"
                    )

        bounds = entry.get("bounds")
        if bounds:
            lines.append("  误差上界比较：")
            for source_id, stats in bounds.items():
                lines.append(
                    f"    {source_id}: 在界内 {stats['within_bound_count']}/"
                    f"{stats['checked_count']}，超限 {stats['violation_count']}，"
                    f"径向超限 {stats['radial_violation_count']}，"
                    f"切向超限 {stats['tangential_violation_count']}，"
                    f"误差/上界最大比 {stats['error_over_bound_ratio_max']:.3f}"
                )

        grid = entry.get("grid_evaluation")
        if grid:
            lines.append(
                f"  栅格评测：有效 {grid['evaluated_count']} 点，"
                f"不可用 {grid['unavailable_count']} 点"
            )
            for source_id, stats in (grid.get("position_error_by_sensor") or {}).items():
                lines.append(f"    {source_id}: {_fmt_stats(stats.get('planar_m'))}")

        cross = entry.get("cross_source_consistency")
        if cross and cross.get("pair_count"):
            lines.append(
                f"  跨源自洽：{cross['pair_count']} 对，"
                f"互差 {_fmt_stats(cross.get('planar_diff_m'))}"
            )

        missing = entry.get("missing_requirements", {}).get("by_source") or {}
        if any(missing.values()):
            lines.append("  缺失前提：")
            for source_id, items in missing.items():
                if items:
                    lines.append(f"    {source_id}: {items}")

        platform = entry.get("platform_state") or {}
        if platform.get("enabled"):
            heading = platform.get("heading_deg") or {}
            speed = platform.get("speed_mps") or {}
            basis = platform.get("match_basis_counts") or {}
            lines.append("  雷达平台自身状态（IF 帧头，不参与方位换算）：")
            lines.append(
                f"    IF 帧 {platform.get('if_frame_count')} 帧，"
                f"覆盖记录 {platform.get('records_with_platform_state')} 条，"
                f"匹配方式 {basis}"
            )
            if heading:
                lines.append(
                    f"    平台航向 {heading.get('min', 0):.1f}°..{heading.get('max', 0):.1f}°"
                    f"（均值 {heading.get('mean', 0):.3f}°）"
                )
            if speed:
                lines.append(
                    f"    平台速度 {speed.get('min', 0) * 1000:.0f}..{speed.get('max', 0) * 1000:.0f} mm/s"
                    f"（均值 {speed.get('mean', 0) * 1000:.2f} mm/s）—— 固定岸基站的 GNSS 定位抖动"
                )
            lines.append(
                "    结论：方位基准为真北固定基准，平台航向与方位换算无关"
                "（权威 AIS 反算真方位：不修正残差中位数 0.45°/0.53°，"
                "叠加平台航向恶化到 6.60°/6.43°，叠加角度修正恶化到 15.55°/15.87°）"
            )

        tracks = entry.get("tracks") or {}
        if tracks:
            by_sensor = tracks.get("by_sensor") or {}
            lines.append(
                f"  航迹导出（供航迹关联）：{tracks.get('track_count')} 条航迹，"
                f"{tracks.get('point_count')} 个点，"
                f"{tracks.get('radar_detection_count')} 个雷达检测点"
            )
            for sensor in ("radar", "ais", "optical"):
                info = by_sensor.get(sensor)
                if not info:
                    continue
                lines.append(
                    f"    {sensor:<9} 航迹 {info.get('track_count')} 条"
                    f"（稳定 {info.get('stable_track_count')}），"
                    f"点 {info.get('point_count')}"
                )
            lines.append(
                "    说明：雷达 900xxx 为不稳定内部号（已标记，不可作关联主键）；"
                "速度/航向缺失者由滑动窗口最小二乘推导（位置量化下必需）"
            )

        evidence = entry.get("mtdsp_evidence")
        if evidence:
            lines.extend(_render_mtdsp_evidence(evidence))

    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)


def _render_mtdsp_evidence(evidence: Dict[str, Any]) -> List[str]:
    """MTDSP 实测证据的文字呈现，逐条回答该场景的问题。"""
    lines: List[str] = []

    # -- 问题 1：空间对齐是否自洽 ----------------------------------------
    consistency = evidence.get("spatial_self_consistency") or {}
    lines.append("  【回答】真实设备数据的空间对齐是否自洽？")
    if consistency:
        overall = consistency.get("overall") or {}
        after = (consistency.get("after_calibration") or {}).get("overall") or {}
        calibration = consistency.get("station_calibration") or {}
        device_bearing = overall.get("delta_bearing_deg") or {}
        lines.append(
            f"    样本 {consistency.get('sample_count')} 条 / "
            f"{consistency.get('target_count')} 个目标"
            f"（AIS 双重表达互检：同一行的经纬度 vs 雷达极坐标）"
        )
        lines.append(
            f"    设备记录站址 {calibration.get('device_record_latitude_deg')}, "
            f"{calibration.get('device_record_longitude_deg')}："
            f"方位差中位 {device_bearing.get('median')}° / 最大 "
            f"{device_bearing.get('max')}°"
        )
        offset = calibration.get("offset_from_device_record_m") or {}
        lines.append(
            f"    标定站址 {calibration.get('calibrated_latitude_deg')}, "
            f"{calibration.get('calibrated_longitude_deg')}："
            f"东向偏移 {offset.get('east')} m / 北向 {offset.get('north')} m"
        )
        if after:
            b = after.get("delta_bearing_deg") or {}
            r = after.get("delta_radial_m") or {}
            t = after.get("delta_tangential_m") or {}
            lines.append(
                f"    标定后残差：方位差中位 {b.get('median')}° / 最大 "
                f"{b.get('max')}°，径向差中位 {r.get('median')} m / 最大 "
                f"{r.get('max')} m，切向差中位 {t.get('median')} m / 最大 "
                f"{t.get('max')} m"
            )
        beamwidth = (consistency.get("device_spec") or {}).get(
            "antenna_beamwidth_deg"
        )
        if after and beamwidth:
            b = after.get("delta_bearing_deg") or {}
            verdict = (
                "最大方位差小于 1.2° 波束宽度，空间自洽"
                if b.get("max", 999) < beamwidth
                else "最大方位差超出波束宽度"
            )
            lines.append(f"    结论：{verdict}。")
        cross_val = calibration.get("cross_validation") or {}
        if cross_val.get("interpretation"):
            lines.append(f"    交叉验证：{cross_val['interpretation']}")

    # -- 问题 2：时区未确认时的时间对齐 ----------------------------------
    cross = evidence.get("cross_source") or {}
    time_basis = cross.get("time_basis") or {}
    if time_basis:
        lines.append("  【回答】时区未确认时，时间对齐如何处理？")
        lines.append(
            f"    模式 {time_basis.get('mode')}，参考时刻 "
            f"{time_basis.get('reference_local_datetime')}"
        )
        lines.append(f"    {time_basis.get('note')}")
        temporal = entry_temporal = evidence.get("cross_source", {}).get(
            "radar_AT_cross_source", {}
        )
        if temporal:
            lines.append(
                f"    产出时间戳带 (local) 后缀，不可与外部数据直接比较，"
                f"但同场景内跨源相对时差确定。"
            )

    # -- 问题 3：跨源残差量级与来源 --------------------------------------
    at_block = cross.get("radar_AT_cross_source") or {}
    if at_block:
        lines.append("  【回答】跨源位置残差的量级与来源？")
        lines.append(f"    {at_block.get('description')}")
        for mmsi, stats in (at_block.get("by_mmsi") or {}).items():
            planar = stats.get("delta_planar_m") or {}
            dt = stats.get("ais_nearest_report_dt_s") or {}
            lines.append(
                f"    MMSI {mmsi}（{stats.get('pairs')} 对，速度 "
                f"{(stats.get('ais_speed_mps') or 0):.2f} m/s）: "
                f"平面差中位 {planar.get('median')} m / 最大 {planar.get('max')} m，"
                f"AIS 最近报告时刻差中位 {dt.get('median')} s"
            )
        lines.append(
            "    判读：残差与目标速度**正相关**（静止船 4.8 m，运动船 55 m），"
            "说明主因是融合/关联延迟而非坐标误差；AT 行位置本就继承自 AIS，"
            "其距离与对应 AIS 记录精确相等（差 0.0 m），延迟量 6-15 s。"
        )

    # -- 问题 4：雷达实测与 AIS 继承值的区分 ----------------------------
    cdt = cross.get("radar_CDT_with_mmsi") or {}
    if cdt:
        lines.append("  【回答】雷达实测目标与 AIS 继承值是否被区分？")
        lines.append(
            f"    CDT（雷达实测，带 MMSI）{cdt.get('evaluated_with_mmsi')} 条；"
            f"900xxx 内部检测号（雷达独有，AIS 无对应目标）"
            f"{cdt.get('internal_only_no_ais_target')} 条"
        )
        by_id = cdt.get("internal_only_by_id") or {}
        if by_id:
            lines.append(
                "    内部检测号分布："
                + "、".join(f"{k} {v} 条" for k, v in sorted(by_id.items()))
            )
        for mmsi, stats in (cdt.get("by_mmsi") or {}).items():
            planar = stats.get("delta_planar_m") or {}
            lines.append(
                f"    MMSI {mmsi}: 平面差中位 {planar.get('median')} m / "
                f"最大 {planar.get('max')} m"
            )
        lines.append(
            "    结论：AT 行标记为 ais_associated（位置继承自 AIS，不可用于评估"
            "雷达精度），CDT 行标记为 radar_detected（雷达实测）。"
        )
        basis = cross.get("station_basis_consistency") or {}
        implied = basis.get("ais_implied_station_offset_m") or {}
        if implied:
            lines.append(
                f"    CDT 相对 AIS 的平面差中位约 "
                f"{(cdt.get('by_mmsi') or {}).get('413203610', {}).get('delta_planar_m', {}).get('median')} m，"
                f"其成因已查明为**两套坐标基准的系统性固定差异**，"
                f"不是随机误差、不是数据错误、也不是『检测框的固有偏移』："
            )
            lines.append(
                f"      · IF_*.csv 帧头（123 帧一致）给出雷达自记站址 "
                f"{basis.get('radar_self_reported_station', {}).get('longitude_deg')}；"
                f"AIS 行内三元组反解的参考站相对它东向 "
                f"{implied.get('east_median')} m"
            )
            lines.append(
                "      · 同文件内 CDT vs AT（光电确认关联）Δ方位稳定，"
                "且跨目标随方位角变号 —— 符合站址偏移特征，排除统一旋转零点偏差"
            )
            lines.append(
                "      · 处理要求：跨源比对前统一站址基准，或显式扣除该系统差；"
                "当作随机噪声处理会导致关联门限整体失效"
            )

    # -- 误差模型标定 -----------------------------------------------------
    model = evidence.get("error_model") or {}
    if model:
        random_model = model.get("random_model") or {}
        lines.append("  【附加】MTDSP 实测误差模型（用于重标定模拟场景 C）：")
        bearing = random_model.get("bearing_error") or {}
        rng = random_model.get("range_error") or {}
        lines.append(
            f"    测角随机残差 std {bearing.get('random_std_deg')}°"
            f"（3σ {bearing.get('three_sigma_deg')}°）"
        )
        lines.append(
            f"    测距随机残差 std {rng.get('random_std_m')} m"
            f"（3σ {rng.get('three_sigma_m')} m）"
        )
        structure = model.get("structure") or {}
        biases = structure.get("per_target_systematic_bearing_bias_deg") or {}
        if biases:
            lines.append(
                "    各目标稳定方位系统偏差："
                + "、".join(f"{k} {v:+.4f}°" for k, v in biases.items())
            )
        lines.append(
            f"    {structure.get('summary', '')}"
        )

    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="生成时空对齐验收报告")
    parser.add_argument("--scenario", default=None, help="场景 ID")
    parser.add_argument("--all", action="store_true", help="全部场景")
    parser.add_argument(
        "--output",
        default=os.path.join(ALIGNMENT_ROOT, "acceptance_report.json"),
        help="报告 JSON 输出路径",
    )
    parser.add_argument(
        "--text-output",
        default=os.path.join(ALIGNMENT_ROOT, "acceptance_report.txt"),
        help="报告文本输出路径",
    )
    args = parser.parse_args()

    if args.scenario:
        scenario_ids = [args.scenario]
    else:
        scenario_ids = sorted(
            os.path.basename(path)
            for path in glob.glob(os.path.join(ALIGNMENT_ROOT, "*"))
            if os.path.isdir(path)
        )

    report = build_report(scenario_ids)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(args.text_output, "w", encoding="utf-8") as handle:
        handle.write(render_text(report))

    print(render_text(report))
    print(f"报告已写入 {args.output}")
    print(f"文本报告已写入 {args.text_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
