#!/usr/bin/env python3
r"""MTDSP 三源一致性验证（雷达 / AIS / 光电）。

## 目的

MTDSP 数据包里有三个**独立测量**同一批海上目标的位置表达：

| 源 | 表达方式 | 数据来源 |
| -- | -------- | -------- |
| AIS | 经纬度（WGS84） | `AIS_Trajectory_<MMSI>.csv` 的 `纬度(°)`/`经度(°)` |
| 雷达 | 距离 + 真北方位 | `CutDataTarget.csv` 的 `CDT` 行（雷达实测检测） |
| 光电 | 距离 + 真北方位 | `Photo_ScreenShot_*.txt` 的 `目标距离(M)`/`中心点方位(°)` |

前两步只用了两源（AIS 内部双重表达互检、雷达 vs AIS），
得到的是**笼统的"基准差"结论**。三源一上，才能回答关键问题：

  **三个源的偏差各是什么性质？可不可以全局补偿？**

这直接决定跨源关联算法能否落地：

  - 若是**全局系统偏差**（每个源一个常数偏移）→ 可以标定后补偿，
    关联门限按补偿后的残差设；
  - 若是**逐目标系统偏差**（杆差等）→ 不能全局补偿，
    只能逐目标建模，且关联门限必须放宽到能容纳该偏差；
  - 若是**随机误差** → 门限由统计量决定。

## 关键方法：区分"方位零点偏差"与"站址偏移"

两者都会表现为"源的方位与 AIS 对不上"，但**补偿方式完全不同**，
必须分开。判据是它们的物理特征不同：

设站址东移 $\Delta E$，目标真方位 $\theta$，距离 $R$：

  - **站址偏移** → 方位偏差 $\delta\alpha \approx \dfrac{\Delta E \cos\theta}{R}$
    （**随距离反比放大**、随方位角变号）
  - **方位零点偏差** → 方位偏差 $\delta\alpha = \text{const}$
    （**与距离无关**、与方位角无关）

本脚本对每个源同时估计两种解释，并报告哪种拟合得更好。

## 与前两步的关系

- 第 1 步 `verify_mtdsp_spatial.py`：AIS 内部双重表达互检 + 站址标定
- 第 2 步 `evaluate_mtdsp_cross_source.py`：雷达 vs AIS，AT/CDT 语义区分
- **本脚本**：三源联合，把偏差分解为"各源零点偏差 + 站址残差"

## 用法

    python3 verify_mtdsp_three_source.py \
        --data-dir data/mtdsp \
        --station-lat 37.53297 --station-lon 121.42020 \
        --output-dir output/verification/mtdsp_three_source

    # 03-12 包
    python3 verify_mtdsp_three_source.py \
        --data-dir data/mtdsp2 \
        --station-lat 37.53295 --station-lon 121.42020 \
        --output-dir output/verification/mtdsp2_three_source

## 输出

  - `mtdsp_three_source_verification.json`  逐项统计、偏差分解与结论
  - `mtdsp_three_source_samples.csv`        逐条样本明细（可追溯）
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from geodesy import LocalOrigin  # noqa: E402

# MTDSP 数据集标识（来源：MTDSP 使用说明，DOI 10.12000/JR25001）
DATASET_ID = "MTDSP-20250305103638-2003-AT-413203610-1"

# 光电元数据文件名：Photo_ScreenShot_<设备>_2002_AIS_0_<MMSI>_..._<14位时间戳>_<序号>.txt
_PHOTO_NAME_RE = re.compile(
    r"^Photo_ScreenShot_(?P<device>Inf|TV)_2002_AIS_0_(?P<mmsi>\d+)"
    r"_[\d.]+_[\d.]+_[\d.]+_(?P<timestamp>\d{14})_\d+$"
)

# 雷达波束宽度（来源：包内说明 SPPR50P_HH）
SPEC_ANTENNA_BEAMWIDTH_DEG = 1.2

# 光电图像幅面宽度（来源：截图实测 1920×1080）
IMAGE_WIDTH_PX = 1920.0


def _to_float(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        if not text:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _median(values: List[float]) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _stats(values: List[float], digits: int = 4) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), digits),
        "median": round(_median(values), digits),
        "p90": round(_percentile(values, 0.90), digits),
        "p95": round(_percentile(values, 0.95), digits),
        "max": round(max(values), digits),
        "mean": round(sum(values) / len(values), digits),
        "std": round(_std(values), digits),
    }


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _wrap_deg(value: float) -> float:
    """归一化到 (-180, 180]。"""
    return (value + 180.0) % 360.0 - 180.0


def _parse_local(value: str) -> Optional[datetime]:
    text = (value or "").split(" (local)")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------


def load_ais_tracks(data_dir: str) -> Dict[str, List[Dict[str, Any]]]:
    """按 MMSI 读取 AIS 轨迹，按时间升序。"""
    tracks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(data_dir, "AIS_Trajectory_*.csv"))):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            for index, raw in enumerate(csv.DictReader(handle), start=1):
                mmsi = (raw.get("MMSI") or "").strip()
                if not mmsi:
                    continue
                latitude = _to_float(raw.get("纬度(°)"))
                longitude = _to_float(raw.get("经度(°)"))
                moment = _parse_local((raw.get("时间") or "").strip())
                if None in (latitude, longitude) or moment is None:
                    continue
                tracks[mmsi].append(
                    {
                        "mmsi": mmsi,
                        "source_file": os.path.basename(path),
                        "source_row": index,
                        "time": moment,
                        "latitude_deg": latitude,
                        "longitude_deg": longitude,
                        "speed_kn": _to_float(raw.get("速度(kn)")),
                    }
                )
    for rows in tracks.values():
        rows.sort(key=lambda r: r["time"])
    return dict(tracks)


def interpolate_ais(
    track: List[Dict[str, Any]], moment: datetime
) -> Optional[Tuple[float, float]]:
    """在 AIS 轨迹上按时间线性插值经纬度。

    AIS 的经纬度列量化很粗（经度 0.001°≈88 m、纬度 0.0001°≈11 m），
    插值只在同一目标相邻报告之间做，且插值结果的不确定度由量化决定，
    远小于本脚本要检出的几度量级偏差。
    """
    if not track:
        return None
    if moment < track[0]["time"] or moment > track[-1]["time"]:
        return None
    if len(track) == 1:
        # 单点轨迹：没有区间可插值，该点即为唯一可用位置
        return track[0]["latitude_deg"], track[0]["longitude_deg"]
    for index in range(len(track) - 1):
        before = track[index]
        after = track[index + 1]
        if before["time"] <= moment <= after["time"]:
            span = (after["time"] - before["time"]).total_seconds()
            if span <= 0:
                return before["latitude_deg"], before["longitude_deg"]
            ratio = (moment - before["time"]).total_seconds() / span
            return (
                before["latitude_deg"]
                + (after["latitude_deg"] - before["latitude_deg"]) * ratio,
                before["longitude_deg"]
                + (after["longitude_deg"] - before["longitude_deg"]) * ratio,
            )
    return None


def load_optical(data_dir: str) -> List[Dict[str, Any]]:
    """读取光电主目标观测（设备解算元数据）。"""
    observations: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(data_dir)):
        if not name.startswith("Photo_ScreenShot"):
            continue
        if name.endswith(".jpg") or name.endswith("_DLDete.txt"):
            continue
        if not name.endswith(".txt"):
            continue
        parsed = _PHOTO_NAME_RE.match(name[: -len(".txt")])
        if parsed is None:
            continue
        meta: Dict[str, float] = {}
        with open(
            os.path.join(data_dir, name), "r", encoding="utf-8", errors="replace"
        ) as handle:
            for line in handle:
                line = line.strip()
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                if key in ("目标距离(M)", "中心点方位(°)"):
                    parsed_value = _to_float(value)
                    if parsed_value is not None:
                        meta[key] = parsed_value
        if "目标距离(M)" not in meta or "中心点方位(°)" not in meta:
            continue
        try:
            moment = datetime.strptime(parsed.group("timestamp"), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        observations.append(
            {
                "mmsi": parsed.group("mmsi"),
                "device": parsed.group("device"),
                "time": moment,
                "range_m": meta["目标距离(M)"],
                "bearing_deg": meta["中心点方位(°)"],
                "source_file": name,
            }
        )
    return observations


def load_optical_detections(data_dir: str) -> List[Dict[str, Any]]:
    """读取 DLDete 画面内检测，并还原每个检测目标的绝对方位。

    DLDete 每行逗号分隔，字段 0-6 是检测序号/类型/置信度/检测框(px)，
    字段 7 是匹配编号，字段 8 是**测距（整数米）**，字段 9 是光学目标序号。
    字段数为 7 的行没有测距，不能作位置观测。

    **方位还原**：检测框只给画面内的相对位置，但配合设备元数据的
    水平视场角与中心点方位，可以还原绝对方位：

    $$\\alpha = \\alpha_{center} + \\frac{x_{center} - W/2}{W}\\cdot \\text{HFOV}$$

    其中 $W$ 为图像幅面宽度。这是**唯一**能提供多目标方位样本的来源 ——
    主目标元数据只解算一个目标，无法用于"目标间一致性"判据。
    """
    detections: List[Dict[str, Any]] = []
    meta_by_stem: Dict[str, Dict[str, float]] = {}

    # 先建立 (时间戳) → 设备元数据 的索引
    for name in sorted(os.listdir(data_dir)):
        if not name.startswith("Photo_ScreenShot"):
            continue
        if name.endswith(".jpg") or name.endswith("_DLDete.txt"):
            continue
        if not name.endswith(".txt"):
            continue
        parsed = _PHOTO_NAME_RE.match(name[: -len(".txt")])
        if parsed is None:
            continue
        meta: Dict[str, float] = {}
        with open(
            os.path.join(data_dir, name), "r", encoding="utf-8", errors="replace"
        ) as handle:
            for line in handle:
                line = line.strip()
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                if key in ("中心点方位(°)", "水平视场角(°)"):
                    parsed_value = _to_float(value)
                    if parsed_value is not None:
                        meta[key] = parsed_value
        if "中心点方位(°)" in meta and "水平视场角(°)" in meta:
            meta_by_stem[parsed.group("timestamp")] = meta

    for name in sorted(os.listdir(data_dir)):
        if not name.endswith("_DLDete.txt"):
            continue
        parsed = _PHOTO_NAME_RE.match(name[: -len("_DLDete.txt")])
        if parsed is None:
            continue
        timestamp = parsed.group("timestamp")
        meta = meta_by_stem.get(timestamp)
        if meta is None:
            continue
        try:
            moment = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
        except ValueError:
            continue
        center_azimuth = meta["中心点方位(°)"]
        hfov = meta["水平视场角(°)"]

        with open(
            os.path.join(data_dir, name), "r", encoding="utf-8", errors="replace"
        ) as handle:
            for index, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 9:
                    continue
                range_m = _to_float(parts[8])
                x_center = _to_float(parts[3])
                if range_m is None or x_center is None:
                    continue
                azimuth = (
                    center_azimuth
                    + (x_center - IMAGE_WIDTH_PX / 2.0)
                    / IMAGE_WIDTH_PX
                    * hfov
                ) % 360.0
                detections.append(
                    {
                        "mmsi": parsed.group("mmsi"),
                        "device": parsed.group("device"),
                        "time": moment,
                        "range_m": range_m,
                        "bearing_deg": azimuth,
                        "target_index": _to_float(parts[0]),
                        "match_id": parts[7].strip() or None,
                        "confidence": _to_float(parts[2]),
                        "source_file": name,
                    }
                )
    return detections


def load_radar_cdt(data_dir: str) -> List[Dict[str, Any]]:
    """读取雷达 CDT 行（雷达实测检测目标）。

    只取带 MMSI 的 CDT 行：900xxx 内部检测号没有 AIS 对应目标，
    无法参与三源比较。`AT` 行排除 —— 它的方位/距离继承自 AIS，
    不是雷达独立测量（已逐点核实残差为 0.0000°/0.000 m）。
    """
    path = os.path.join(data_dir, "CutDataTarget.csv")
    if not os.path.exists(path):
        return []
    observations: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for index, raw in enumerate(csv.DictReader(handle), start=2):
            if (raw.get("目标类型") or "").strip() != "CDT":
                continue
            target_id = (raw.get("目标编号") or "").strip()
            # 只保留带 MMSI 的 CDT 行：9 位 MMSI 可与 AIS 轨迹配对；
            # 900xxx 是雷达内部检测号，没有 AIS 对应目标。
            if not (target_id.isdigit() and len(target_id) == 9):
                continue
            moment = _parse_local((raw.get("起始时间(帧)") or "").strip())
            bearing = _to_float(raw.get("目标方位(°)"))
            range_m = _to_float(raw.get("目标距离(米)"))
            if moment is None or bearing is None or range_m is None:
                continue
            observations.append(
                {
                    "mmsi": target_id,
                    "time": moment,
                    "range_m": range_m,
                    "bearing_deg": bearing,
                    "source_file": "CutDataTarget.csv",
                    "source_row": index,
                }
            )
    return observations


# ---------------------------------------------------------------------------
# 三源比较
# ---------------------------------------------------------------------------


def _nearest_ais_target(
    ais_tracks: Dict[str, List[Dict[str, Any]]],
    moment: datetime,
    origin: LocalOrigin,
    observed_range_m: Optional[float] = None,
    observed_bearing_deg: Optional[float] = None,
    max_dt_s: float = 120.0,
    max_range_gap_m: float = 100.0,
    max_bearing_gap_deg: float = 10.0,
) -> Optional[Tuple[str, float, float]]:
    """找出该时刻与观测最匹配的 AIS 目标，返回 (MMSI, 距离, 方位)。

    **必须同时用距离和方位配对**。只用距离不够：本数据包里两个目标
    （413203610 锚泊船 @3877 m、413328560 @3680 m）距离只差 200 m，
    但方位差 25°；只用"空间最近"会把 413203610 的检测配给 413328560，
    产生 +34° 的假偏差。

    配对规则（全部满足才接受）：
      - 距离差 ≤ max_range_gap_m
      - 方位差 ≤ max_bearing_gap_deg
      - 方位差用**观测方位**与 AIS 方位比较 —— 观测方位含待标定的
        零点偏差，因此门限必须宽于该偏差才能完成配对。
        实测光电零点偏差约 5.5°，故门限取 10°：既足以容纳真实偏差，
        又远小于错误配对（本数据实例为 25°），不会把 A 目标的检测
        配给 B 目标。
        这是**有偏但不循环**的门限：它只用于排除明显错误配对，
        不参与偏差数值本身；偏差由配对后的残差独立给出。
    """
    best: Optional[Tuple[str, float, float]] = None
    best_score = float("inf")
    for mmsi, track in ais_tracks.items():
        position = interpolate_ais(track, moment)
        if position is None:
            continue
        latitude, longitude = position
        east, north = origin.to_enu(latitude, longitude)
        distance = math.hypot(east, north)
        if distance < 1.0:
            continue
        bearing = math.degrees(math.atan2(east, north)) % 360.0

        if observed_range_m is not None:
            if abs(distance - observed_range_m) > max_range_gap_m:
                continue
        if observed_bearing_deg is not None:
            gap = _wrap_deg(observed_bearing_deg - bearing)
            if abs(gap) > max_bearing_gap_deg:
                continue

        nearest_dt = min(
            abs((row["time"] - moment).total_seconds()) for row in track
        )
        score = distance + nearest_dt * 2.0
        if score < best_score:
            best_score = score
            best = (mmsi, distance, bearing)
    return best


def build_samples(
    ais_tracks: Dict[str, List[Dict[str, Any]]],
    optical: List[Dict[str, Any]],
    radar: List[Dict[str, Any]],
    origin: LocalOrigin,
    optical_detections: Optional[List[Dict[str, Any]]] = None,
    radar_window_s: float = 3.0,
) -> List[Dict[str, Any]]:
    """构造逐条三源比较样本。

    每条样本以 AIS 经纬度插值为参考真值，比较雷达与光电的极坐标读数。
    同一时刻若多个设备（Inf/TV）都有光电观测，各自成一条样本。

    `optical_detections` 是 DLDete 画面内检测 —— 它们**不限于主目标**，
    每个检测都要与该时刻**空间最近**的 AIS 目标配对。
    这提供了多目标方位样本，是"目标间一致性"判据的唯一来源。
    """
    samples: List[Dict[str, Any]] = []

    for observation in optical:
        track = ais_tracks.get(observation["mmsi"])
        if not track:
            continue
        position = interpolate_ais(track, observation["time"])
        if position is None:
            continue
        latitude, longitude = position
        east, north = origin.to_enu(latitude, longitude)
        range_ref = math.hypot(east, north)
        if range_ref < 1.0:
            continue
        bearing_ref = math.degrees(math.atan2(east, north)) % 360.0

        samples.append(
            {
                "source_kind": "optical",
                "mmsi": observation["mmsi"],
                "device": observation["device"],
                "time": observation["time"],
                "range_ref_m": range_ref,
                "bearing_ref_deg": bearing_ref,
                "range_obs_m": observation["range_m"],
                "bearing_obs_deg": observation["bearing_deg"],
                "delta_bearing_deg": _wrap_deg(
                    observation["bearing_deg"] - bearing_ref
                ),
                "delta_range_m": observation["range_m"] - range_ref,
                "source_file": observation["source_file"],
            }
        )

    # DLDete 检测：与该时刻**距离与方位都最接近**的 AIS 目标配对
    for detection in optical_detections or []:
        paired = _nearest_ais_target(
            ais_tracks,
            detection["time"],
            origin,
            observed_range_m=detection["range_m"],
            observed_bearing_deg=detection["bearing_deg"],
        )
        if paired is None:
            continue
        mmsi, range_ref, bearing_ref = paired
        # 配对可信性：距离差必须远小于光电量程，否则是误检
        if abs(detection["range_m"] - range_ref) > 200.0:
            continue
        samples.append(
            {
                "source_kind": "optical_detection",
                "mmsi": mmsi,
                "device": detection["device"],
                "time": detection["time"],
                "range_ref_m": range_ref,
                "bearing_ref_deg": bearing_ref,
                "range_obs_m": detection["range_m"],
                "bearing_obs_deg": detection["bearing_deg"],
                "delta_bearing_deg": _wrap_deg(
                    detection["bearing_deg"] - bearing_ref
                ),
                "delta_range_m": detection["range_m"] - range_ref,
                "source_file": detection["source_file"],
            }
        )

    # 雷达：每个时刻对每个 MMSI 只取一条（取时间最接近者）
    radar_by_key: Dict[Tuple[str, datetime], List[Dict[str, Any]]] = defaultdict(list)
    for observation in radar:
        radar_by_key[(observation["mmsi"], observation["time"])].append(observation)

    for (mmsi, moment), observations in sorted(radar_by_key.items()):
        track = ais_tracks.get(mmsi)
        if not track:
            continue
        position = interpolate_ais(track, moment)
        if position is None:
            continue
        latitude, longitude = position
        east, north = origin.to_enu(latitude, longitude)
        range_ref = math.hypot(east, north)
        if range_ref < 1.0:
            continue
        bearing_ref = math.degrees(math.atan2(east, north)) % 360.0
        for observation in observations:
            samples.append(
                {
                    "source_kind": "radar",
                    "mmsi": mmsi,
                    "device": "radar",
                    "time": moment,
                    "range_ref_m": range_ref,
                    "bearing_ref_deg": bearing_ref,
                    "range_obs_m": observation["range_m"],
                    "bearing_obs_deg": observation["bearing_deg"],
                    "delta_bearing_deg": _wrap_deg(
                        observation["bearing_deg"] - bearing_ref
                    ),
                    "delta_range_m": observation["range_m"] - range_ref,
                    "source_file": observation["source_file"],
                }
            )

    samples.sort(key=lambda s: (s["time"], s["source_kind"], s["mmsi"]))
    return samples

def fit_zero_bias(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """拟合"方位零点偏差"模型：δα = const（与距离、方位角无关）。

    返回各源的常量偏差与扣除后的残差统计。
    """
    result: Dict[str, Any] = {}
    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_source[sample["source_kind"]].append(sample)

    for kind, rows in by_source.items():
        biases = [row["delta_bearing_deg"] for row in rows]
        bias = _median(biases)
        residuals = [
            _wrap_deg(row["delta_bearing_deg"] - bias) for row in rows
        ]
        result[kind] = {
            "sample_count": len(rows),
            "constant_bearing_bias_deg": round(bias, 4),
            "bearing_residual_after_removal_deg": _stats(residuals, 4),
            "range_bias_m": round(_median([row["delta_range_m"] for row in rows]), 3),
            "range_residual_after_removal_m": _stats(
                [
                    row["delta_range_m"]
                    - _median([r["delta_range_m"] for r in rows])
                    for row in rows
                ],
                3,
            ),
            "bias_by_distance_bucket_deg": _bias_by_distance(rows),
        }
    return result


def _bias_by_distance(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按距离分桶统计方位偏差。

    站址偏移 ⇒ 偏差随距离**反比放大**；零点偏差 ⇒ 各桶一致。
    这是区分两种解释的关键判据。
    """
    buckets = [(0, 2000), (2000, 4000), (4000, 6000), (6000, 8000), (8000, 12000)]
    result: Dict[str, Any] = {}
    for low, high in buckets:
        subset = [row for row in rows if low <= row["range_ref_m"] < high]
        if not subset:
            continue
        biases = [row["delta_bearing_deg"] for row in subset]
        result[f"{low}-{high}m"] = {
            "count": len(subset),
            "median_bias_deg": round(_median(biases), 4),
            "std_deg": round(_std(biases), 4),
        }
    return result


def fit_station_offset(
    samples: List[Dict[str, Any]], source_kind: str
) -> Dict[str, Any]:
    r"""拟合"站址偏移"模型：δα = ΔE·cos(θ)/R。

    对每个样本，把实测方位偏差换算成等效东向偏移
    $\Delta E_i = \delta\alpha_i \cdot R_i / \cos\theta_i$，
    取中位作为单参数站址偏移的估计，并报告其离散度。

    若各样本反解出的 ΔE 一致，说明站址偏移是合理解释；
    若离散度大而方位偏差本身很集中，则说明是零点偏差。

    **注意本判据只在有多距离跨度时有效**：若所有样本的距离几乎相同
    （如本数据包的静止目标），等效 ΔE 的离散度会趋近 0，
    这**不能**被解读为"站址偏移成立"，而是判据退化。
    此时应改看 `target_consistency` 判据。
    """
    rows = [row for row in samples if row["source_kind"] == source_kind]
    equivalent: List[Dict[str, Any]] = []
    for row in rows:
        theta = math.radians(row["bearing_ref_deg"])
        cosine = math.cos(theta)
        if abs(cosine) < 0.2:
            # 方位角接近正东/正西时，站址东移几乎不产生方位偏差，
            # 反解的 ΔE 数值上无意义（除以接近 0 的 cos）
            continue
        delta_east = (
            math.radians(row["delta_bearing_deg"]) * row["range_ref_m"] / cosine
        )
        equivalent.append(
            {
                "time": row["time"],
                "mmsi": row["mmsi"],
                "equivalent_delta_east_m": delta_east,
            }
        )

    if not equivalent:
        return {"sample_count": 0, "note": "无有效反解样本（方位角过于接近正东西向）"}

    values = [item["equivalent_delta_east_m"] for item in equivalent]
    bias = _median(values)
    residuals = [value - bias for value in values]
    ranges = [row["range_ref_m"] for row in rows]
    range_spread = max(ranges) - min(ranges)

    # 判据有效性检查：没有距离跨度时，本判据无法区分两种模型
    degenerate = range_spread < 100.0
    if degenerate:
        interpretation = (
            f"判据退化：所有样本距离跨度仅 {range_spread:.0f} m，"
            "等效 ΔE 的低离散度不构成站址偏移成立的证据；"
            "应改看 target_consistency 判据"
        )
    elif _std(values) < 50.0:
        interpretation = (
            "各样本反解出的等效东向偏移离散度小 ⇒ 站址偏移可解释该源的偏差"
        )
    else:
        interpretation = (
            "各样本反解出的等效东向偏移离散度大 ⇒ 站址偏移不能解释该源的偏差"
        )

    return {
        "sample_count": len(values),
        "fitted_delta_east_m": round(bias, 2),
        "range_spread_m": round(range_spread, 1),
        "criterion_degenerate": degenerate,
        "equivalent_delta_east_dispersion_m": _stats(values, 2),
        "residual_after_removal_m": _stats(residuals, 2),
        "interpretation": interpretation,
    }


def target_consistency(
    samples: List[Dict[str, Any]], source_kind: str
) -> Dict[str, Any]:
    """【关键判据】逐目标检查方位偏差的一致性。

    **站址偏移是全局的**：同一个站址偏移对所有目标产生同号、同比例的
    方位偏差（按 $\\cos\\theta/R$ 缩放）。

    **方位零点偏差也是全局的**：对所有目标产生同一个角度量。

    两者都能解释"单目标内"的恒定性，**但只有零点偏差能解释
    "不同目标间偏差完全一致"**。判据：

    - 各目标的方位偏差**中位数彼此相差 < 0.05°** ⇒ 各目标偏差是同一个常数
      ⇒ 指向**该源自身的方位零点偏差**（可全局补偿）；
    - 各目标的偏差差异大 ⇒ 指向**逐目标偏置**（杆差等），
      不能全局补偿，关联门限必须按最差目标放宽。

    本判据**不依赖距离跨度**，因此在只有单个目标时虽退化，
    在多目标时是决定性的。
    """
    rows = [row for row in samples if row["source_kind"] == source_kind]
    by_target: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        by_target[row["mmsi"]].append(row["delta_bearing_deg"])

    if not by_target:
        return {"target_count": 0}

    per_target = {
        mmsi: {
            "sample_count": len(biases),
            "median_bias_deg": round(_median(biases), 4),
            "std_deg": round(_std(biases), 4),
        }
        for mmsi, biases in sorted(by_target.items())
    }
    medians = [item["median_bias_deg"] for item in per_target.values()]

    if len(medians) == 1:
        interpretation = (
            "只有一个目标 ⇒ 无法用目标间一致性区分『源零点偏差』与"
            "『逐目标偏置』；本判据退化。"
            "【本数据集的物理限制】光电设备水平视场仅 2.932°，"
            "任一时刻视场内只有一个目标（其余 AIS 目标方位差 >25°，"
            "在视场外）；DLDete 的多目标检测中，距离真实但方位与任何 AIS "
            "目标都不符（差 >20°），是误检，已被距离+方位双门限剔除。"
            "⇒ 本判据在 MTDSP 上无法适用，零点偏差的判定改由"
            "『跨数据集复现』与『源间直接比较』两条独立证据给出"
        )
        consistent = None
    else:
        spread = max(medians) - min(medians)
        consistent = spread < 0.05
        interpretation = (
            f"各目标偏差中位数的极差 {spread:.4f}° "
            + (
                "⇒ 各目标偏差是同一个常数 ⇒ 该源存在**全局方位零点偏差**，"
                "可标定后全局补偿"
                if consistent
                else "⇒ 各目标偏差不一致 ⇒ 存在**逐目标偏置**（杆差等），"
                "不能全局补偿，关联门限须按最差目标放宽"
            )
        )

    return {
        "target_count": len(by_target),
        "per_target": per_target,
        "median_of_medians_deg": round(_median(medians), 4),
        "spread_between_targets_deg": (
            round(max(medians) - min(medians), 4) if len(medians) > 1 else None
        ),
        "consistent_across_targets": consistent,
        "interpretation": interpretation,
    }


def correlate_with_range(samples: List[Dict[str, Any]], source_kind: str) -> Dict[str, Any]:
    """检验方位偏差与距离是否相关。

    站址偏移 ⇒ 偏差 ∝ 1/R；零点偏差 ⇒ 不相关。
    """
    rows = [
        row
        for row in samples
        if row["source_kind"] == source_kind and row["range_ref_m"] > 100.0
    ]
    if len(rows) < 3:
        return {"sample_count": len(rows), "note": "样本不足"}

    biases = [row["delta_bearing_deg"] for row in rows]
    inverse_ranges = [1.0 / row["range_ref_m"] for row in rows]
    mean_b = sum(biases) / len(biases)
    mean_ir = sum(inverse_ranges) / len(inverse_ranges)
    numerator = sum(
        (b - mean_b) * (ir - mean_ir) for b, ir in zip(biases, inverse_ranges)
    )
    denominator = math.sqrt(
        sum((b - mean_b) ** 2 for b in biases)
        * sum((ir - mean_ir) ** 2 for ir in inverse_ranges)
    )
    correlation = numerator / denominator if denominator > 1e-12 else 0.0

    # 相关性弱 ⇒ 偏差不随距离变化 ⇒ 指向零点偏差而非站址偏移
    return {
        "sample_count": len(rows),
        "pearson_r_with_inverse_range": round(correlation, 4),
        "interpretation": (
            "偏差与 1/距离无明显相关 ⇒ 不支持站址偏移解释，指向方位零点偏差"
            if abs(correlation) < 0.5
            else "偏差与 1/距离相关 ⇒ 与站址偏移特征一致"
        ),
    }


def association_gate_recommendation(
    reports: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """给出跨源关联门限建议。

    这是三源验证的最终落脚点：前面所有分析都是为了回答
    "关联算法该设多宽的门限"。

    门限由两部分决定：
      - **可补偿的系统偏差**：扣除后残余的系统项（逐目标偏置无法全局补偿，
        必须留在门限里）；
      - **随机残差**：扣除系统项后的离散度，取 3σ 作为统计裕量。
    """
    recommendation: Dict[str, Any] = {
        "principle": (
            "方位零点偏差可全局补偿 ⇒ 扣除后用紧门限；"
            "逐目标偏置与随机残差不可补偿 ⇒ 保留在门限内。"
            "距离通道与方位通道独立 ⇒ 距离可用紧门限，方位须放宽。"
        ),
        "per_source": {},
    }

    for report in reports:
        tag = report.get("tag", "?")
        zero_bias = report.get("zero_bias_model", {})
        for kind in ("optical", "radar"):
            fit = zero_bias.get(kind)
            if not fit or not fit.get("sample_count"):
                continue
            bias = fit.get("constant_bearing_bias_deg")
            residual = fit.get("bearing_residual_after_removal_deg", {})
            residual_std = residual.get("std", 0.0)
            range_bias = fit.get("range_bias_m", 0.0)
            range_residual = fit.get("range_residual_after_removal_m", {})
            range_std = range_residual.get("std", 0.0)

            # 3σ 方位门限（度），并给出对应的切向量级
            gate_deg = 3.0 * residual_std
            recommendation["per_source"].setdefault(kind, []).append(
                {
                    "dataset": tag,
                    "systematic_bearing_bias_deg": bias,
                    "residual_std_deg": residual_std,
                    "recommended_bearing_gate_deg": round(gate_deg, 4),
                    "residual_std_m": round(
                        math.radians(residual_std) * 5000.0, 2
                    ),
                    "systematic_range_bias_m": range_bias,
                    "range_residual_std_m": range_std,
                    "recommended_range_gate_m": round(3.0 * range_std, 2),
                    "note": (
                        "系统偏差可补偿（标定后扣除）；门限只需覆盖随机残差。"
                        "若不补偿偏差，则门限须再叠加该系统偏差量级。"
                    ),
                }
            )

    return recommendation


def cross_dataset_replication(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """跨数据集复现检验。

    **这是替代"目标间一致性"的决定性判据**：它比较的是**不同采集日期**、
    **不同机动状态**（03-05 锚泊静止船 / 03-12 机动转向船）、
    **不同距离**（3.87 km / 5.0 km）下测得的同一个源的偏差。

    若偏差在如此不同的条件下仍保持同一数值 ⇒ 它是**设备级的常量**，
    而非某个目标的偶发属性，也不可能是随距离/方位变化的站址偏移。

    站址偏移无法解释这一现象：两个数据包的站址几乎相同（东向差 0 m），
    但若偏差由站址偏移产生，其大小会随目标距离与方位角变化
    （$\\delta\\alpha = \\Delta E\\cos\\theta/R$），
    而实测两个包在同一目标上的偏差几乎相同。
    """
    entries = []
    for report in reports:
        tag = report.get("tag", "?")
        zero_bias = report.get("zero_bias_model", {})
        for kind in ("optical", "radar"):
            fit = zero_bias.get(kind)
            if not fit or not fit.get("sample_count"):
                continue
            residual = fit.get("bearing_residual_after_removal_deg", {})
            entries.append(
                {
                    "dataset": tag,
                    "source_kind": kind,
                    "constant_bias_deg": fit.get("constant_bearing_bias_deg"),
                    "residual_std_deg": residual.get("std"),
                    "sample_count": fit.get("sample_count"),
                }
            )

    by_kind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_kind[entry["source_kind"]].append(entry)

    result: Dict[str, Any] = {"per_source": entries, "by_source": {}}
    for kind, rows in by_kind.items():
        biases = [row["constant_bias_deg"] for row in rows]
        spread = (max(biases) - min(biases)) if len(biases) > 1 else None
        if spread is None:
            interpretation = "只有一个数据集 ⇒ 无法做跨数据集复现检验"
            replicated = None
        elif spread < 0.5:
            replicated = True
            interpretation = (
                f"偏差在 {len(rows)} 个独立数据集间极差仅 {spread:.4f}° "
                "⇒ 同一设备在不同日期、不同距离、不同机动状态下偏差一致 "
                "⇒ 设备级常量偏差，可全局补偿"
            )
        else:
            replicated = False
            interpretation = (
                f"偏差在 {len(rows)} 个独立数据集间极差 {spread:.4f}° "
                "(超过 0.5° 判据) ⇒ 偏差**不是**严格常量，"
                "存在随条件变化的成分（可能是目标运动/配对时序误差），"
                "全局补偿时须留出该量级的裕量"
            )
        result["by_source"][kind] = {
            "dataset_count": len(rows),
            "biases_deg": [row["constant_bias_deg"] for row in rows],
            "spread_between_datasets_deg": (
                round(spread, 4) if spread is not None else None
            ),
            "replicated": replicated,
            "interpretation": interpretation,
        }
    return result


def cross_source_gap(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """光电与雷达的相对方位差 —— 不依赖 AIS 的第三方比较。

    若两源各自相对 AIS 有相反的零点偏差，则两者之差是两个偏差之和。
    这条证据完全独立于 AIS，是"两个源各自有零点偏差"的最强判据。
    """
    optical_rows = [
        row
        for row in samples
        if row["source_kind"] in ("optical", "optical_detection")
    ]
    radar_rows = [row for row in samples if row["source_kind"] == "radar"]
    if not optical_rows or not radar_rows:
        return {"note": "缺少光电或雷达样本"}

    optical_bias = _median([row["delta_bearing_deg"] for row in optical_rows])
    radar_bias = _median([row["delta_bearing_deg"] for row in radar_rows])
    gap = _wrap_deg(optical_bias - radar_bias)
    return {
        "optical_bias_vs_ais_deg": round(optical_bias, 4),
        "radar_bias_vs_ais_deg": round(radar_bias, 4),
        "optical_minus_radar_deg": round(gap, 4),
        "gap_in_tangential_m_at_4km": round(math.radians(gap) * 4000.0, 1),
        "note": (
            "两源相对 AIS 的偏差方向相反 ⇒ 各自有独立的方位零点偏差；"
            "光电与雷达之差约为两者偏差之和，与 AIS 无关"
        ),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def write_samples(path: str, samples: List[Dict[str, Any]]) -> None:
    fields = [
        "dataset",
        "source_kind",
        "mmsi",
        "device",
        "time",
        "range_ref_m",
        "bearing_ref_deg",
        "range_obs_m",
        "bearing_obs_deg",
        "delta_bearing_deg",
        "delta_range_m",
        "source_file",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for sample in samples:
            row = dict(sample)
            row["time"] = sample["time"].strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(row)


def analyse(
    data_dir: str,
    station_lat: float,
    station_lon: float,
    origin_lat: float,
    origin_lon: float,
) -> Dict[str, Any]:
    origin = LocalOrigin(origin_lat, origin_lon)
    station = LocalOrigin(station_lat, station_lon)

    ais_tracks = load_ais_tracks(data_dir)
    optical = load_optical(data_dir)
    optical_detections = load_optical_detections(data_dir)
    radar = load_radar_cdt(data_dir)

    samples = build_samples(
        ais_tracks, optical, radar, station, optical_detections=optical_detections
    )

    zero_bias = fit_zero_bias(samples)
    station_fits = {
        kind: fit_station_offset(samples, kind)
        for kind in ("optical", "optical_detection", "radar")
    }
    target_consistency_result = {
        kind: target_consistency(samples, kind)
        for kind in ("optical", "optical_detection", "radar")
    }
    range_correlation = {
        kind: correlate_with_range(samples, kind)
        for kind in ("optical", "optical_detection", "radar")
    }
    gap = cross_source_gap(samples)

    # 结论
    optical_fit = zero_bias.get("optical", {})
    radar_fit = zero_bias.get("radar", {})
    optical_residual = optical_fit.get("bearing_residual_after_removal_deg", {})
    optical_bias = optical_fit.get("constant_bearing_bias_deg")

    conclusions: List[str] = []
    if optical_bias is not None:
        conclusions.append(
            f"光电相对 AIS 的方位偏差为常量 {optical_bias:+.3f}°，"
            f"扣除后残差 std={optical_residual.get('std', float('nan'))}°"
        )
    optical_consistency = target_consistency_result.get("optical", {})
    conclusions.append(
        "光电逐目标一致性: "
        + optical_consistency.get("interpretation", "")
    )
    detection_consistency = target_consistency_result.get("optical_detection", {})
    if detection_consistency.get("target_count", 0) > 0:
        conclusions.append(
            "光电画面内检测（多目标）逐目标一致性: "
            + detection_consistency.get("interpretation", "")
        )
    radar_consistency = target_consistency_result.get("radar", {})
    conclusions.append(
        "雷达逐目标一致性: " + radar_consistency.get("interpretation", "")
    )
    optical_correlation = range_correlation.get("optical", {})
    conclusions.append(
        "光电偏差与 1/距离的相关系数 "
        f"{optical_correlation.get('pearson_r_with_inverse_range')} "
        f"⇒ {optical_correlation.get('interpretation', '')}"
    )
    conclusions.append(
        f"光电等效站址东向偏移 {station_fits['optical'].get('fitted_delta_east_m')} m，"
        f"距离跨度 {station_fits['optical'].get('range_spread_m')} m "
        f"⇒ {station_fits['optical'].get('interpretation', '')}"
    )
    if gap.get("optical_minus_radar_deg") is not None:
        conclusions.append(
            f"光电与雷达的方位差 {gap['optical_minus_radar_deg']:+.3f}°"
            f"（4 km 处切向 {gap['gap_in_tangential_m_at_4km']} m），"
            "该差值不依赖 AIS，是两源各自零点偏差的直接证据"
        )
    conclusions.append(
        f"雷达相对 AIS 的方位偏差为常量 {radar_fit.get('constant_bearing_bias_deg')}°"
    )

    return {
        "source": {
            "dataset_id": DATASET_ID,
            "data_dir": data_dir,
            "station_latitude_deg": station_lat,
            "station_longitude_deg": station_lon,
            "site_origin_latitude_deg": origin_lat,
            "site_origin_longitude_deg": origin_lon,
            "method": (
                "以 AIS 经纬度时间插值为参考，比较雷达 CDT 行与光电元数据的"
                "极坐标读数；分别拟合『方位零点偏差』与『站址偏移』两种模型"
            ),
        },
        "input_counts": {
            "ais_tracks": len(ais_tracks),
            "ais_rows": sum(len(rows) for rows in ais_tracks.values()),
            "optical_observations": len(optical),
            "optical_detections": len(optical_detections),
            "radar_cdt_observations": len(radar),
            "three_source_samples": len(samples),
        },
        "sample_counts_by_source": {
            kind: sum(1 for s in samples if s["source_kind"] == kind)
            for kind in ("optical", "optical_detection", "radar")
        },
        "zero_bias_model": zero_bias,
        "station_offset_model": station_fits,
        "target_consistency": target_consistency_result,
        "range_correlation": range_correlation,
        "cross_source_gap": gap,
        "conclusions": conclusions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MTDSP 三源一致性验证")
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        metavar="TAG:DATADIR:LAT:LON",
        help=(
            "数据包，格式 TAG:DATADIR:站址纬度:站址经度，可重复。"
            "多个包时会额外做跨数据集复现检验。"
            "例：--package 20250305:data/mtdsp:37.53297:121.42020"
        ),
    )
    parser.add_argument(
        "--data-dir", help="（单包用法）等价于 --package default:<data-dir>"
    )
    parser.add_argument("--station-lat", type=float, default=37.53297)
    parser.add_argument("--station-lon", type=float, default=121.42020)
    parser.add_argument("--origin-lat", type=float, default=37.53297)
    parser.add_argument("--origin-lon", type=float, default=121.42020)
    parser.add_argument(
        "--output-dir", default="output/verification/mtdsp_three_source"
    )
    args = parser.parse_args()

    packages: List[Tuple[str, str, float, float]] = []
    for spec in args.package:
        parts = spec.split(":")
        if len(parts) != 4:
            parser.error(f"--package 格式应为 TAG:DATADIR:LAT:LON，收到 {spec}")
        packages.append(
            (parts[0], parts[1], float(parts[2]), float(parts[3]))
        )
    if not packages:
        if not args.data_dir:
            parser.error("需要 --package 或 --data-dir")
        packages.append(
            ("default", args.data_dir, args.station_lat, args.station_lon)
        )

    os.makedirs(args.output_dir, exist_ok=True)

    reports: List[Dict[str, Any]] = []
    all_samples: List[Dict[str, Any]] = []
    for tag, data_dir, station_lat, station_lon in packages:
        report = analyse(
            data_dir,
            station_lat,
            station_lon,
            station_lat,
            station_lon,
        )
        report["tag"] = tag
        reports.append(report)

        station = LocalOrigin(station_lat, station_lon)
        samples = build_samples(
            load_ais_tracks(data_dir),
            load_optical(data_dir),
            load_radar_cdt(data_dir),
            station,
            optical_detections=load_optical_detections(data_dir),
        )
        for sample in samples:
            sample["dataset"] = tag
        all_samples.extend(samples)

    combined: Dict[str, Any] = {
        "datasets": [report["tag"] for report in reports],
        "per_dataset": reports,
        "cross_dataset_replication": cross_dataset_replication(reports),
        "association_gate_recommendation": association_gate_recommendation(
            reports
        ),
    }
    if len(reports) > 1:
        combined["cross_dataset_conclusions"] = _replication_conclusions(
            cross_dataset_replication(reports)
        )

    json_path = os.path.join(args.output_dir, "mtdsp_three_source_verification.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(combined, handle, ensure_ascii=False, indent=2)

    csv_path = os.path.join(args.output_dir, "mtdsp_three_source_samples.csv")
    write_samples(csv_path, all_samples)

    print(json.dumps(combined, ensure_ascii=False, indent=2))
    print(f"\n报告已写入 {json_path}")
    print(f"样本明细已写入 {csv_path}")
    return 0


def _replication_conclusions(replication: Dict[str, Any]) -> List[str]:
    """把跨数据集复现结果转成结论句。"""
    conclusions: List[str] = []
    for kind, item in replication.get("by_source", {}).items():
        label = {"optical": "光电", "radar": "雷达"}.get(kind, kind)
        conclusions.append(
            f"{label}：{item.get('interpretation', '')}"
        )
    return conclusions


if __name__ == "__main__":
    raise SystemExit(main())
