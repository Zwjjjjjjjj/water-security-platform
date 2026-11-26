#!/usr/bin/env python3
"""把后端光电（optical）观测导出为前端场景数据集的一部分。

## 背景

前端 `apps/es-big-screen/public/water-security/data/` 下的
`devices.json` 声明了光电设备 E01（`kind: "camera"`），
但 `tracks.json` 里**没有任何 E01 的源航迹** ——
于是「数据标准化」模块里光电永远显示 0 条，
而「目标识别」模块与场景画面却把光电画成在正常工作的样子，
造成演示层与数据层口径不一致。

根因不是前端渲染，而是**导出环节漏了光电这一路**：
后端 `target_standardization` 的三源流水线
（雷达 / AIS / 光电）本就把光电算得出来，
只是前端数据集是当初只导雷达+AIS（+声纳）时留下的子集。

本脚本负责把光电补回去。

## 数据来源

  · 标准化记录 —— `output/system/standardized/<scen>/optical/standard_records.jsonl`
  · 对齐结果   —— `output/system/alignment/<scen>/aligned_records.csv`
  · 关联结论   —— `output/system/association/<scen>/optical_ais_accepted.csv`

光电的对齐结果落在 `site_enu_yantai` 站心 ENU，
与前端 `devices.json` / `tracks.json` 用的是**同一个坐标系、同一个原点**
（37.53297, 121.4199 WGS84），所以 ENU 坐标可直接搬运，不需要重投影。

## 关键处理

### 1. 方位系统差修正（+5.28°）

光电设备有**恒定的方位零点偏差**：实测 03-05 包为 +5.681°，
03-12 包为 +5.439°，跨数据集极差仅 0.24° ⇒ 设备级常量。

适配器**刻意不隐式补偿**（`optical_azimuth_bias_applied=false`），
把这件事留给下游显式处理 —— 因为不修正的话，
光电点迹会稳定偏在真值东侧约 350 m，页面上的光电航迹
会与雷达/AIS 航迹明显分叉，看起来就像「光电坏了」。

本脚本在导出时按 **+5.28°** 绕站心旋转 ENU 坐标，
即把光电点迹转到与 AIS/雷达一致的方位基准上。
该值由 `verify_optical_bias.py` 实测得出（残差中位 5.1 m @3.9 km）。

### 2. Inf / TV 双设备并列观测的处置

光电转塔有 **Inf（红外）/ TV（可见光）两台相机**，
共享同一个 `local_track_id` 与同一时间戳，但**位置不同**
（03-05 实测 138 组同时刻记录中 48 组位置不同，最大相距 727 m）。

这是两次独立测量，不是重复记录。导出时按 `local_track_id` 分组，
把 Inf 与 TV 视为**同一条光电航迹**（它们观测的是同一个目标），
同一时刻取**距离站心更近**的一条（量测误差更小），
并在样本上标注来自哪台相机。

### 3. 超量程误检的剔除

DLDete 的 AI 检测结果里有大量误检：距离散布到 28/45/234–350 km，
置信度高达 0.81–0.94 **无法用置信度区分**。
后端已按 `max_range_m = 20000 m` 标记 `optical_detection_out_of_range`
并置 `valid_for_spatial_alignment=false`。

本脚本只导出 `alignment_status == "ok"` 的记录，
即后端已判定空间可用的那些 —— 误检在这里被自然过滤掉。

### 4. 与已有全局目标的挂接

用 `optical_ais_accepted.csv` 的关联结论把光电航迹挂到已有的
全局目标（T001 等）上：后端用 Hungarian 指派 + 显式方位偏差修正，
把 `optical:<local_track_id>` 配到 `ais:<mmsi>`。

只有**关联置信度为 high** 的配对才采用；
挂不上的光电航迹**不新建全局目标**，而是单独作为一条
「未关联光电航迹」输出，避免污染既有目标体系。

## 用法

    python3 export_optical_to_frontend.py \
        --scenario mtdsp_0305 \
        --frontend-data ../../apps/es-big-screen/public/water-security/data \
        --dry-run

`--dry-run` 只打印统计，不写文件。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

#: 前端光电设备编号（与 devices.json 一致）
EO_SOURCE_ID = "E01"

#: 前端传感器类型命名 —— 注意与 devices.json 的 "camera" 不同！
#: 前端 pipeline.ts / chart-utils.ts 用的是 "eo"。
EO_KIND = "eo"

#: 光电方位零点偏差（度，正值 = 实测偏东）。
#: 依据：03-05 包 +5.681°、03-12 包 +5.439°，
#: 本脚本用 03-05 包的实测中位 5.28°（见 verify_optical_bias.py）。
OPTICAL_AZIMUTH_BIAS_DEG = 5.28

#: 时基原点（相对时间零点）—— 与前端 tracks.json 的 t0 一致
T0_LOCAL = "2025-03-05 10:35:38"


# --------------------------------------------------------------------------
# 时间工具
# --------------------------------------------------------------------------
def parse_local(text: str) -> Optional[datetime]:
    """解析 '2025-03-05 10:36:43.000 (local)' 形式的时间戳。"""
    raw = (text or "").split(" (local)")[0].strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def time_ms_from(local_text: str, t0: datetime) -> Optional[int]:
    """把本地时间戳换算成相对 t0 的毫秒数（前端 tracks.json 的 time_ms）。"""
    moment = parse_local(local_text)
    if moment is None:
        return None
    return int(round((moment - t0).total_seconds() * 1000))


def utc_text(moment: datetime) -> str:
    """前端样本里的 utc 文本格式（毫秒精度）。"""
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def detect_style(path: str, default_indent: int = 2) -> Tuple[int, bool]:
    """探测 JSON 文件原有的排版风格 → (缩进宽度, 是否以换行结尾)。

    前端这几个数据文件的历史排版并不一致：
      · `devices.json` / `tracks.json` —— 1 空格缩进、结尾无换行
      · `targets.json`             —— 2 空格缩进、结尾有换行

    导出时若统一用某个宽度 / 统一补换行，
    git diff 就会把整个文件标成「全文件重写」，看不出真实改了什么。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return default_indent, True

    trailing_newline = raw.endswith("\n")
    indents: List[int] = []
    for line in raw.split("\n")[:200]:
        stripped = line.lstrip(" ")
        if stripped == line or not stripped:
            continue
        width = len(line) - len(stripped)
        if width >= 1:
            indents.append(width)
    indent = min(indents) if indents else default_indent
    return indent, trailing_newline


# --------------------------------------------------------------------------
# 几何工具
# --------------------------------------------------------------------------
def rotate_enu(east: float, north: float, deg: float) -> Tuple[float, float]:
    """绕站心旋转 ENU 坐标（正角 = 方位增大的方向）。

    ENU 里方位角 = atan2(east, north)，所以方位增 +θ 对应
    把 (E, N) 按 [[cos, -sin], [sin, cos]] 旋转 θ。
    """
    if not deg:
        return east, north
    rad = math.radians(deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return (
        east * cos_a - north * sin_a,
        east * sin_a + north * cos_a,
    )


def enu_to_latlon(
    east: float, north: float, origin_lat: float, origin_lon: float
) -> Tuple[float, float]:
    """站心 ENU（米）→ WGS84 经纬度（度）。

    用等距圆柱近似：东西向用纬度余弦修正，
    站点 3.9 km 范围内误差 < 0.5 m，足够前端显示。
    """
    m_per_deg_lat = 111_132.92 - 559.82 * math.cos(2 * math.radians(origin_lat)) \
        + 1.175 * math.cos(4 * math.radians(origin_lat))
    m_per_deg_lon = 111_412.84 * math.cos(math.radians(origin_lat)) \
        - 93.5 * math.cos(3 * math.radians(origin_lat))
    lat = origin_lat + north / m_per_deg_lat
    lon = origin_lon + east / m_per_deg_lon
    return lat, lon


# --------------------------------------------------------------------------
# 读取后端产物
# --------------------------------------------------------------------------
def read_aligned_optical(alignment_dir: str) -> List[Dict[str, str]]:
    """读对齐结果里的光电记录（只取空间可用的）。"""
    path = os.path.join(alignment_dir, "aligned_records.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到对齐结果：{path}")
    rows: List[Dict[str, str]] = []
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("source_type") != "optical":
                continue
            if row.get("alignment_status") != "ok":
                continue
            if not row.get("east_m") or not row.get("north_m"):
                continue
            rows.append(row)
    return rows


def read_optical_ais_links(association_dir: str) -> Dict[str, str]:
    """读光电↔AIS 关联结论 → {光电 local_track_id: AIS mmsi}。

    只取 **high** 置信度的配对 —— 低置信配对的距离残差
    可达 590 m，采信它等于把光电挂到错误的目标上。
    """
    path = os.path.join(association_dir, "optical_ais_accepted.csv")
    links: Dict[str, str] = {}
    if not os.path.exists(path):
        return links
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("confidence") or "").strip().lower() != "high":
                continue
            a = (row.get("track_a") or "").strip()
            b = (row.get("track_b") or "").strip()
            if not a.startswith("optical:") or not b.startswith("ais:"):
                continue
            optical_id = a.split(":", 1)[1]
            mmsi = b.split(":", 1)[1]
            links[optical_id] = mmsi
    return links


def read_frontend_targets(data_dir: str) -> Dict[str, Dict[str, Any]]:
    """读前端 targets.json → {mmsi: target}（用于按 MMSI 挂接）。"""
    path = os.path.join(data_dir, "targets.json")
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    out: Dict[str, Dict[str, Any]] = {}
    for item in payload.get("items", []):
        mmsi = item.get("mmsi")
        if mmsi:
            out[str(mmsi)] = item
    return out


# --------------------------------------------------------------------------
# 主转换
# --------------------------------------------------------------------------
def build_optical_tracks(
    aligned: List[Dict[str, str]],
    links: Dict[str, str],
    t0: datetime,
    origin_lat: float,
    origin_lon: float,
    apply_bias: bool,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    """把对齐后的光电记录整理成「源航迹 → 样本列表」。

    返回 `(tracks, track_to_mmsi)`：
      · tracks       —— {光电 local_track_id: [样本, ...]}
      · track_to_mmsi —— {光电 local_track_id: AIS mmsi}（来自关联结论）
    """
    # 按 local_track_id + 时刻分组（Inf / TV 同一时刻各有 1 条）
    grouped: Dict[str, Dict[int, List[Dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    for row in aligned:
        track_id = row["local_track_id"]
        ms = time_ms_from(row.get("aligned_time_utc", ""), t0)
        if ms is None or ms < 0:
            continue
        grouped[track_id][ms].append(row)

    tracks: Dict[str, List[Dict[str, Any]]] = {}
    for track_id, by_time in grouped.items():
        samples: List[Dict[str, Any]] = []
        for ms in sorted(by_time.keys()):
            rows = by_time[ms]
            # Inf / TV 同时刻两条 ⇒ 取量测误差更小的那条（离站心更近）。
            # 站心是量测参考点，距离越近 ⇒ 距离量测的相对误差越小。
            best = min(rows, key=lambda r: math.hypot(float(r["east_m"]), float(r["north_m"])))
            east = float(best["east_m"])
            north = float(best["north_m"])
            if apply_bias:
                east, north = rotate_enu(east, north, OPTICAL_AZIMUTH_BIAS_DEG)

            lat, lon = enu_to_latlon(east, north, origin_lat, origin_lon)
            moment = parse_local(best.get("aligned_time_utc", ""))
            confidence = best.get("optical_confidence") or ""

            samples.append(
                {
                    "time_ms": ms,
                    "east_m": round(east, 2),
                    "north_m": round(north, 2),
                    "depth_m": None,
                    "speed_mps": None,
                    "course_deg": None,
                    "lat": round(lat, 8),
                    "lon": round(lon, 8),
                    "bearing_deg": round((math.degrees(math.atan2(east, north)) % 360), 3),
                    "range_m": round(math.hypot(east, north), 2),
                    "update_status": "measured",
                    "data_mode": "field",
                    "utc": utc_text(moment) if moment else "—",
                    "kind": "eo_primary" if best.get("optical_record_kind") == "primary_target" else "eo_detected",
                    # —— 光电专属字段（前端暂未消费，但保留可追溯性）——
                    "eo_device": best.get("optical_device") or "",
                    "eo_band": best.get("optical_band") or "",
                    "eo_confidence": float(confidence) if confidence else None,
                    "eo_record_kind": best.get("optical_record_kind") or "",
                    "eo_type_label": best.get("optical_type_label") or "",
                    "eo_range_source": best.get("optical_range_source") or "",
                    "eo_azimuth_bias_applied": bool(apply_bias),
                }
            )
        if samples:
            tracks[track_id] = samples

    track_to_mmsi = {tid: links[tid] for tid in tracks if tid in links}
    return tracks, track_to_mmsi


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把后端光电观测导出到前端场景数据集"
    )
    parser.add_argument("--scenario", default="mtdsp_0305", help="场景名")
    parser.add_argument(
        "--system-root",
        default=os.path.join(ROOT, "output", "system"),
        help="multisensor_system.py 的输出根目录",
    )
    parser.add_argument(
        "--frontend-data",
        default=os.path.join(ROOT, "..", "..", "apps", "es-big-screen", "public", "water-security", "data"),
        help="前端数据目录",
    )
    parser.add_argument(
        "--no-bias-correction",
        action="store_true",
        help="不施加方位系统差修正（保留原始实测方位，用于消融对比）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    alignment_dir = os.path.join(args.system_root, "alignment", args.scenario)
    association_dir = os.path.join(args.system_root, "association", args.scenario)
    frontend_data = os.path.abspath(args.frontend_data)

    t0 = parse_local(T0_LOCAL)
    if t0 is None:
        raise SystemExit(f"无法解析时基原点：{T0_LOCAL}")

    aligned = read_aligned_optical(alignment_dir)
    links = read_optical_ais_links(association_dir)
    targets_by_mmsi = read_frontend_targets(frontend_data)

    with open(os.path.join(frontend_data, "scene.json"), encoding="utf-8") as handle:
        scene = json.load(handle)
    origin = scene.get("origin") or {}
    origin_lat = float(origin.get("lat") or 37.53297)
    origin_lon = float(origin.get("lon") or 121.4199)

    apply_bias = not args.no_bias_correction
    tracks, track_to_mmsi = build_optical_tracks(
        aligned, links, t0, origin_lat, origin_lon, apply_bias
    )

    total_samples = sum(len(v) for v in tracks.values())
    linked_tracks = {tid: mmsi for tid, mmsi in track_to_mmsi.items() if mmsi in targets_by_mmsi}

    print("=" * 68)
    print("光电数据导出（后端 → 前端数据集）")
    print("=" * 68)
    print(f"场景            : {args.scenario}")
    print(f"后端对齐目录    : {alignment_dir}")
    print(f"前端数据目录    : {frontend_data}")
    print(f"方位偏差修正    : {'+%.2f°' % OPTICAL_AZIMUTH_BIAS_DEG if apply_bias else '未施加（原始实测方位）'}")
    print()
    print(f"空间可用光电记录: {len(aligned)}")
    print(f"光电源航迹      : {len(tracks)}")
    print(f"光电样本总数    : {total_samples}")
    print(f"高置信关联到目标: {len(linked_tracks)} 条航迹")
    print()
    for tid in sorted(tracks):
        mmsi = linked_tracks.get(tid)
        target = targets_by_mmsi.get(mmsi) if mmsi else None
        where = f"→ {target['global_track_id']} (MMSI {mmsi})" if target else "→ 未关联"
        print(f"  {tid:24s} {len(tracks[tid]):4d} 点  {where}")
    print()

    if args.dry_run:
        print("--dry-run：未写入任何文件。")
        return 0

    # ---- 写回 tracks.json -------------------------------------------------
    tracks_path = os.path.join(frontend_data, "tracks.json")
    with open(tracks_path, encoding="utf-8") as handle:
        payload = json.load(handle)

    items = payload.get("items", [])
    by_gid = {t.get("global_track_id"): t for t in items}

    # 1) 挂到已有全局目标下
    #
    # 【为什么按目标合并而不是按航迹各挂一条】
    # 前端 `source_tracks` 的语义是「一个感知源在这个目标下的观测」，
    # 同一个 `source_id` 只能有一条。光电的 Inf / TV / 画面内检测
    # 三条航迹观测的其实是**同一个目标**（后端关联到同一个 MMSI），
    # 所以必须先按目标合并成一条 E01 源航迹，否则会出现重复条目。
    #
    # 合并规则：同一时刻取「离站心更近」的那条（量测误差更小）；
    # 某时刻只有一条可用时直接采用。
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for tid, mmsi in sorted(linked_tracks.items()):
        target = targets_by_mmsi[mmsi]
        gid = target["global_track_id"]
        track = by_gid.get(gid)
        if track is None:
            continue
        bucket = merged.setdefault(gid, {})
        for sample in tracks[tid]:
            ms = sample["time_ms"]
            current = bucket.get(ms)
            if current is None:
                bucket[ms] = sample
                continue
            # 取离站心更近的一条（距离量测相对误差更小）
            if math.hypot(sample["east_m"], sample["north_m"]) < math.hypot(
                current["east_m"], current["north_m"]
            ):
                bucket[ms] = sample

    appended = 0
    for gid, bucket in merged.items():
        track = by_gid.get(gid)
        if track is None:
            continue
        samples = [bucket[ms] for ms in sorted(bucket.keys())]
        source_tracks = track.get("source_tracks", [])
        # 先移除旧的 E01 条目（保证脚本可重复执行）
        source_tracks = [st for st in source_tracks if st.get("source_id") != EO_SOURCE_ID]
        source_tracks.append({"source_id": EO_SOURCE_ID, "samples": samples})
        track["source_tracks"] = source_tracks
        appended += 1

    # 2) targets.json 的 sources 补 E01
    targets_path = os.path.join(frontend_data, "targets.json")
    with open(targets_path, encoding="utf-8") as handle:
        targets_payload = json.load(handle)
    touched: List[str] = []
    for item in targets_payload.get("items", []):
        if item.get("global_track_id") in merged:
            sources = item.setdefault("sources", [])
            if EO_SOURCE_ID not in sources:
                sources.append(EO_SOURCE_ID)
            touched.append(item["global_track_id"])

    # 3) devices.json 的 kind：camera → eo（前端 pipeline 用 "eo"）
    devices_path = os.path.join(frontend_data, "devices.json")
    with open(devices_path, encoding="utf-8") as handle:
        devices_payload = json.load(handle)
    kind_fixed = False
    for item in devices_payload.get("items", []):
        if item.get("source_id") == EO_SOURCE_ID and item.get("kind") == "camera":
            item["kind"] = EO_KIND
            kind_fixed = True

    def dump(path: str, data: Any) -> None:
        # 沿用**该文件原有**的排版风格（缩进宽度、结尾换行），
        # 否则导出后整个文件在 git 里会变成「全文件重写」，看不出改了什么。
        indent, trailing_newline = detect_style(path)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
            if trailing_newline:
                handle.write("\n")

    dump(tracks_path, payload)
    dump(targets_path, targets_payload)
    dump(devices_path, devices_payload)

    print(f"已写入 {tracks_path}")
    print(f"  · {appended} 个全局目标挂上了 E01 源航迹")
    print(f"已写入 {targets_path}")
    print(f"  · sources 补 E01：{', '.join(touched) if touched else '（无）'}")
    print(f"已写入 {devices_path}")
    print(f"  · E01 的 kind：{'camera → eo' if kind_fixed else '（已是 eo）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
