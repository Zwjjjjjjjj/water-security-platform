"""跨源坐标系统差核查：先核查，必要时修正。

## 结论（verdict）

  - 03-05 包：11 对有效配对（AIS 原始轨迹插值到 CDT 时刻），
    Δ方位 **−2.404° ± 0.200°**，Δ距离 **+17.7 ± 4.0 m** ——
    稳定、可重复。
  - 03-12 包：6 对有效配对（AIS 仅 6 点，大量 CDT 超出 AIS 时间范围），
    其中 4 对 Δ方位 −1.4° ~ −2.0°（与 03-05 一致），
    2 对（帧 28/30）Δ方位 −5.4° ~ −5.7° ——
    疑似误标或该时段的独立扰动，证据不足以定为全局常数。

## 因此

  - **不做自动估计、不默认应用任何修正**。自动估计在当前证据下
    会把误标/扰动吃进全局参数，污染关联门限。
  - 本模块只提供：证据复现函数 + 可选的手动修正函数。
    是否修正、修正多少，由调用方（关联阶段）显式决定。

## 偏差来源（已核验）

CDT 极坐标的参考点比 AIS 极坐标的参考点偏东约 159 m
（详见 `standardization/radar_cutdata_adapter.py` 第 8 条）。
在 3.9 km 处对应约 2.4° 方位差，与 03-05 实测吻合。
**这是两套坐标基准的固定差异，不是随机误差**；但在拿到跨目标、
跨时段的充分证据前，不自动扣除。

## 用法

    from association.bias import summarize_cdt_bias, apply_cdt_correction

    # 只看证据，不修正
    report = summarize_cdt_bias(cutdata_rows, ais_rows_by_mmsi)

    # 显式修正（调用方决定参数）
    corrected = apply_cdt_correction(
        detections, delta_bearing_deg=-2.404, delta_range_m=+17.7
    )
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 1° 方位在典型作用距离对应的横向米数
METERS_PER_DEG = 52.0


def _parse_time(text: str) -> Optional[datetime]:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def interpolate_ais_polar(
    ais_sequence: Sequence[Tuple[float, float, float]],
    epoch: float,
) -> Optional[Tuple[float, float]]:
    """把 AIS 极坐标序列线性插值到目标时刻。

    `ais_sequence` 为 `[(epoch_s, 方位角_deg, 距离_m)]`（需按时间排序）。
    方位插值处理 0°/360° 绕回。超出序列范围返回 None（不外推）。
    """
    ordered = sorted(ais_sequence, key=lambda item: item[0])
    before: Optional[Tuple[float, float, float]] = None
    after: Optional[Tuple[float, float, float]] = None
    for item in ordered:
        if item[0] <= epoch:
            before = item
        if item[0] >= epoch and after is None:
            after = item
    if before is None or after is None:
        return None
    if after[0] == before[0]:
        return before[1] % 360.0, before[2]
    ratio = (epoch - before[0]) / (after[0] - before[0])
    delta = after[1] - before[1]
    if delta > 180.0:
        delta -= 360.0
    elif delta < -180.0:
        delta += 360.0
    bearing = (before[1] + ratio * delta) % 360.0
    rng = before[2] + ratio * (after[2] - before[2])
    return bearing, rng


def bearing_residual_deg(measured: float, expected: float) -> float:
    """方位残差（度），绕回到 (−180, 180]。"""
    residual = (measured - expected) % 360.0
    if residual > 180.0:
        residual -= 360.0
    return residual


def summarize_cdt_bias(
    cutdata_rows: Sequence[Dict[str, Any]],
    ais_polar_by_mmsi: Dict[str, Sequence[Tuple[float, float, float]]],
    match_id: str = "1",
) -> Dict[str, Any]:
    """复现 CDT vs AIS 系统差证据。

    用光电确认关联（`匹配编号=1`）的 CDT 行，与 AIS 原始轨迹
    （经插值到 CDT 时刻）逐对比较，输出每对残差与汇总统计。

    返回 `{"pairs": [...], "count": n, "mean_dbrg": ..., "std_dbrg": ...,
    "mean_drng": ..., "std_drng": ..., "verdict": ...}`。
    `verdict` 为 `"stable"`（std 小、可重复）或 `"inconclusive"`
    （样本少或散布大，不建议自动修正）。
    """
    pairs: List[Dict[str, Any]] = []
    for row in cutdata_rows:
        if str(row.get("目标类型")) != "CDT":
            continue
        if str(row.get("匹配编号")) != match_id:
            continue
        mmsi = str(row.get("目标编号"))
        sequence = ais_polar_by_mmsi.get(mmsi)
        if not sequence:
            continue
        moment = _parse_time(str(row.get("起始时间(帧)") or ""))
        if moment is None:
            continue
        expected = interpolate_ais_polar(sequence, moment.timestamp())
        if expected is None:
            pairs.append(
                {
                    "frame": row.get("帧序号"),
                    "status": "out_of_ais_range",
                }
            )
            continue
        measured_brg = float(row.get("目标方位(°)"))
        measured_rng = float(row.get("目标距离(米)"))
        pairs.append(
            {
                "frame": row.get("帧序号"),
                "status": "paired",
                "measured_brg": measured_brg,
                "measured_rng": measured_rng,
                "expected_brg": round(expected[0], 3),
                "expected_rng": round(expected[1], 1),
                "dBrg": round(
                    bearing_residual_deg(measured_brg, expected[0]), 3
                ),
                "dRng": round(measured_rng - expected[1], 1),
            }
        )

    valid = [p for p in pairs if p.get("status") == "paired"]
    report: Dict[str, Any] = {
        "pair_count": len(valid),
        "out_of_range_count": sum(
            1 for p in pairs if p.get("status") == "out_of_ais_range"
        ),
        "pairs": pairs,
    }
    if len(valid) >= 2:
        import statistics as stats

        dbrg = [p["dBrg"] for p in valid]
        drng = [p["dRng"] for p in valid]
        report["mean_dbrg"] = round(stats.mean(dbrg), 3)
        report["std_dbrg"] = round(stats.stdev(dbrg), 3)
        report["mean_drng"] = round(stats.mean(drng), 1)
        report["std_drng"] = round(stats.stdev(drng), 1)
        #  verdict：样本 ≥8 且方位 std ≤0.5° 才算稳定可重复
        if len(valid) >= 8 and stats.stdev(dbrg) <= 0.5:
            report["verdict"] = "stable"
        else:
            report["verdict"] = "inconclusive"
            report["verdict_note"] = (
                "样本少或散布大，不建议自动修正；"
                "如需修正请由调用方显式指定参数。"
            )
    else:
        report["verdict"] = "inconclusive"
        report["verdict_note"] = "有效配对不足 2 对，无法判定。"
    return report


def apply_cdt_correction(
    detections: Sequence[Dict[str, Any]],
    delta_bearing_deg: float,
    delta_range_m: float = 0.0,
) -> List[Dict[str, Any]]:
    """对 CDT 检测点应用显式的系统差修正（调用方决定参数）。

    从实测极坐标中扣除 `delta_*`（即加上其相反数），返回新列表，
    原输入不改动。修正后的行 marked `bias_corrected=True` 并记录参数。
    """
    corrected: List[Dict[str, Any]] = []
    for row in detections:
        new_row = dict(row)
        brg = row.get("bearing_deg")
        rng = row.get("range_m")
        try:
            new_brg = (
                (float(brg) - delta_bearing_deg) % 360.0
                if brg is not None and str(brg).strip() != ""
                else brg
            )
            new_rng = (
                float(rng) - delta_range_m
                if rng is not None and str(rng).strip() != ""
                else rng
            )
        except (TypeError, ValueError):
            new_brg, new_rng = brg, rng
        new_row["bearing_deg"] = new_brg
        new_row["range_m"] = new_rng
        new_row["bias_corrected"] = True
        new_row["bias_delta_bearing_deg"] = delta_bearing_deg
        new_row["bias_delta_range_m"] = delta_range_m
        corrected.append(new_row)
    return corrected
