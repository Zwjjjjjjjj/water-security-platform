#!/usr/bin/env python3
"""水面三源（雷达 / AIS / 光电）整体处理系统 —— 统一入口.

只新增本调度文件，不改现有模块，不含可视化。
全链：标准化 -> 时空对齐 -> 航迹导出 -> CDT跨帧重建 -> 航迹关联 -> 主目标提取。

用法：
    python3 multisensor_system.py --scenario mtdsp_0305
    python3 multisensor_system.py --scenario mtdsp2_0312
    python3 multisensor_system.py --scenario all
    python3 multisensor_system.py --scenario mtdsp_0305 --output-root output --only standardize,align
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from standardization import standardize  # noqa: E402
from alignment import AlignmentConfig, run_alignment  # noqa: E402
from alignment.track_export import export_tracks  # noqa: E402
from association.associate import (  # noqa: E402
    GATE_OPTICAL_AIS_M,
    GATE_RADAR_AIS_M,
    GATE_RADAR_OPTICAL_M,
    OPTICAL_BIAS_DEG,
    RADAR_BIAS_DEG,
    associate_pair,
    load_candidates,
)
from association.cdt_tracker import CdtTracker, load_detections  # noqa: E402
from association.timebase import check_timebase  # noqa: E402

# --------------------------------------------------------------------------
# 场景注册：03-05 + 03-12 两套 MTDSP（雷达/AIS/光电全链）
# --------------------------------------------------------------------------
SCENARIOS = {
    "mtdsp_0305": {
        "data_dir": "data/mtdsp",
        "cutdata": "data/mtdsp/CutDataTarget.csv",
        "std_configs": {
            "radar": "configs/mtdsp_radar.json",
            "ais": "configs/mtdsp_ais.json",
            "optical": "configs/mtdsp_optical.json",
        },
        "align_config": "configs/alignment/alignment_site_yantai_optical.json",
        "ais_static_dir": "data/mtdsp",  # AIS静态属性目录
    },
    "mtdsp2_0312": {
        "data_dir": "data/mtdsp2",
        "cutdata": "data/mtdsp2/CutDataTarget.csv",
        "std_configs": {
            "radar": "configs/mtdsp2_radar.json",
            "ais": "configs/mtdsp2_ais.json",
            "optical": "configs/mtdsp2_optical.json",
        },
        "align_config": "configs/alignment/alignment_site_yantai_mtdsp2.json",
        "ais_static_dir": "data/mtdsp2",
    },
}

STAGES = ["standardize", "align", "tracks", "reconstruct", "associate", "primary"]


# --------------------------------------------------------------------------
# Stage 1: 标准化（三源）
# --------------------------------------------------------------------------
def run_standardize(scen: str, output_root: str) -> dict:
    cfg = SCENARIOS[scen]
    out = {}
    for source in ("radar", "ais", "optical"):
        with open(os.path.join(ROOT, cfg["std_configs"][source]), encoding="utf-8") as h:
            sc = json.load(h)
        if source == "ais" and cfg.get("ais_static_dir"):
            sc["static_info_dir"] = os.path.join(ROOT, cfg["ais_static_dir"])
        out_dir = os.path.join(output_root, "standardized", scen, source)
        summary = standardize(os.path.join(ROOT, cfg["data_dir"]), sc, out_dir)
        out[source] = os.path.join(out_dir, "standard_records.jsonl")
        print(f"[{scen}][standardize][{source}] records={summary.get('output_count')} "
              f"rejected={summary.get('rejected_count')} -> {out[source]}")
    return out


# --------------------------------------------------------------------------
# Stage 2: 时空对齐（雷达+AIS+光电extra）
# --------------------------------------------------------------------------
def run_align(scen: str, output_root: str, std_paths: dict = None) -> str:
    cfg = SCENARIOS[scen]
    if std_paths is None:
        base = os.path.join(output_root, "standardized", scen)
        std_paths = {s: os.path.join(base, s, "standard_records.jsonl")
                     for s in ("radar", "ais", "optical")}
    with open(os.path.join(ROOT, cfg["align_config"]), encoding="utf-8") as h:
        align_cfg = AlignmentConfig.from_dict(json.load(h))
    out_dir = os.path.join(output_root, "alignment", scen)
    result = run_alignment(
        radar_records_path=std_paths["radar"],
        ais_records_path=std_paths["ais"],
        alignment_config=align_cfg,
        output_dir=out_dir,
        extra_records_paths=[std_paths["optical"]],
    )
    q = result.get("quality", {})
    print(f"[{scen}][align] -> {out_dir} "
          f"coord_pass={q.get('coordinate_verification', {}).get('all_passed')} "
          f"aligned={result.get('aligned_records')} grid={result.get('grid_records')}")
    return out_dir


# --------------------------------------------------------------------------
# Stage 3: 航迹导出（按传感器分组完整航迹）
# --------------------------------------------------------------------------
def run_tracks(alignment_dir: str) -> dict:
    aligned = os.path.join(alignment_dir, "aligned_records.csv")
    if not os.path.exists(aligned):
        raise FileNotFoundError(f"找不到 {aligned}")
    quality_path = os.path.join(alignment_dir, "alignment_quality.json")
    origin_lat = origin_lon = 0.0
    frame_id = "site_enu"
    if os.path.exists(quality_path):
        with open(quality_path, encoding="utf-8") as h:
            quality = json.load(h)
        frame = quality.get("site_frame") or {}
        origin_lat = float(frame.get("origin_latitude_deg") or 0.0)
        origin_lon = float(frame.get("origin_longitude_deg") or 0.0)
        frame_id = str(frame.get("frame_id") or frame_id)
    with open(aligned, encoding="utf-8", newline="") as h:
        records = list(csv.DictReader(h))
    result = export_tracks(
        records, alignment_dir,
        origin_latitude_deg=origin_lat,
        origin_longitude_deg=origin_lon,
        frame_id=frame_id,
    )
    s = result["summary_data"]
    print(f"[tracks] -> {result['tracks_dir']} tracks={s['track_count']} "
          f"points={s['point_count']} detections={s['radar_detection_count']}")
    return result


# --------------------------------------------------------------------------
# Stage 4: CDT跨帧重建（逐帧检测 -> 稳定航迹）
# --------------------------------------------------------------------------
RECON_FIELDS = ["track_id", "sensor_type", "point_index", "time_utc", "time_epoch_s",
                "latitude_deg", "longitude_deg", "east_m", "north_m", "up_m",
                "range_m", "bearing_deg", "radar_frame_index", "measurement_kind",
                "member_count", "source_row", "member_source_rows",
                "error_bound_planar_m"]
INDEX_FIELDS = ["track_id", "sensor_type", "track_kind", "track_stability",
                "point_count", "start_time_utc", "end_time_utc", "duration_s",
                "median_dt_s", "path_length_m", "straight_line_distance_m",
                "mean_speed_mps", "max_speed_mps"]


def run_reconstruct(alignment_dir: str) -> str:
    det_path = os.path.join(alignment_dir, "tracks", "radar_detections.csv")
    if not os.path.exists(det_path):
        raise FileNotFoundError(f"找不到 {det_path}")
    frames = load_detections(det_path)
    tracks = CdtTracker().reconstruct(frames)
    tracks_dir = os.path.join(alignment_dir, "tracks")
    recon_dir = os.path.join(tracks_dir, "radar_reconstructed")
    os.makedirs(recon_dir, exist_ok=True)
    index_rows = []
    for pid in sorted(tracks):
        points = tracks[pid]
        track_id = f"radar_reconstructed:R{pid}"
        rows = []
        for idx, p in enumerate(points):
            rows.append({
                "track_id": track_id, "sensor_type": "radar", "point_index": idx,
                "time_utc": p["time_utc"], "time_epoch_s": p["time_epoch_s"],
                "latitude_deg": p.get("latitude_deg"), "longitude_deg": p.get("longitude_deg"),
                "east_m": p.get("east_m"), "north_m": p.get("north_m"), "up_m": p.get("up_m"),
                "range_m": p.get("range_m"), "bearing_deg": p.get("bearing_deg"),
                "radar_frame_index": p.get("radar_frame_index"),
                "measurement_kind": p.get("measurement_kind"),
                "member_count": p.get("member_count"), "source_row": p.get("source_row"),
                "member_source_rows": ";".join(str(x) for x in (p.get("member_source_rows") or [])),
                "error_bound_planar_m": p.get("error_bound_planar_m"),
            })
        with open(os.path.join(recon_dir, f"R{pid}.csv"), "w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=RECON_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        epochs = [float(r["time_epoch_s"]) for r in rows]
        path_len = sum(math.hypot(rows[i + 1]["east_m"] - rows[i]["east_m"],
                                  rows[i + 1]["north_m"] - rows[i]["north_m"])
                       for i in range(len(rows) - 1)) if len(rows) > 1 else 0.0
        straight = (math.hypot(rows[-1]["east_m"] - rows[0]["east_m"],
                               rows[-1]["north_m"] - rows[0]["north_m"])
                    if len(rows) > 1 else 0.0)
        duration = epochs[-1] - epochs[0] if epochs else 0.0
        dts = sorted(epochs[i + 1] - epochs[i] for i in range(len(epochs) - 1))
        med = (dts[len(dts) // 2] if len(dts) % 2 else (dts[len(dts) // 2 - 1] + dts[len(dts) // 2]) / 2.0) if dts else None
        speed = straight / duration if duration > 0 else 0.0
        index_rows.append({"track_id": track_id, "sensor_type": "radar",
                           "track_kind": "radar_reconstructed", "track_stability": "reconstructed",
                           "point_count": len(rows),
                           "start_time_utc": rows[0]["time_utc"] if rows else None,
                           "end_time_utc": rows[-1]["time_utc"] if rows else None,
                           "duration_s": round(duration, 3),
                           "median_dt_s": round(med, 3) if med is not None else None,
                           "path_length_m": round(path_len, 3),
                           "straight_line_distance_m": round(straight, 3),
                           "mean_speed_mps": round(speed, 4), "max_speed_mps": round(speed, 4)})
    with open(os.path.join(tracks_dir, "radar_reconstructed_index.csv"), "w",
              encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(index_rows)
    print(f"[reconstruct] -> {recon_dir} tracks={len(tracks)} "
          f"frames={len(frames)} detections={sum(len(m) for _, _, m in frames)}")
    return recon_dir


# --------------------------------------------------------------------------
# Stage 5: 航迹关联（radar-ais / optical-ais / radar-optical）
# --------------------------------------------------------------------------
def _mmsi_of(track_id: str) -> str:
    name = track_id.split(":", 1)[-1] if ":" in track_id else track_id
    name = name.split("@")[0]
    if name.startswith("R") or name.startswith("optical_det") or name == "1":
        return ""
    if name.isdigit() and len(name) == 9:
        return name
    return ""


def run_associate(alignment_dir: str, output_dir: str, min_points: int = 3) -> dict:
    os.makedirs(output_dir, exist_ok=True)
    aligned_path = os.path.join(alignment_dir, "aligned_records.csv")
    timebase = {"comparable": None}
    if os.path.exists(aligned_path):
        with open(aligned_path, encoding="utf-8", newline="") as h:
            records = list(csv.DictReader(h))
        present = sorted({r.get("source_type") for r in records if r.get("source_type")})
        required = [s for s in ("radar", "ais", "optical") if s in present]
        timebase = check_timebase(records, required_sensors=required or ["radar", "ais"])
        if not timebase["comparable"]:
            raise RuntimeError(f"时间不可比: {timebase['reasons']}")
    cands = load_candidates(alignment_dir, min_points=min_points, use_reconstructed=True)
    results = {}
    for label, ta, tb, gate in [
        ("radar_ais", cands["radar"], cands["ais"], GATE_RADAR_AIS_M),
        ("optical_ais", cands["optical"], cands["ais"], GATE_OPTICAL_AIS_M),
        ("radar_optical", cands["radar"], cands["optical"], GATE_RADAR_OPTICAL_M),
    ]:
        accepted, rejected = associate_pair(
            ta, tb, gate_m=gate, allow_many_to_one=True,
            optical_bias_deg=OPTICAL_BIAS_DEG, radar_bias_deg=RADAR_BIAS_DEG,
            apply_bias_correction=True)
        results[label] = {"accepted": accepted, "rejected": rejected}
        for name, rows in [(f"{label}_accepted.csv", accepted), (f"{label}_rejected.csv", rejected)]:
            path = os.path.join(output_dir, name)
            if rows:
                with open(path, "w", encoding="utf-8", newline="") as h:
                    w = csv.DictWriter(h, fieldnames=sorted(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
            else:
                open(path, "w", encoding="utf-8").close()
    evals = {}
    for k in ("radar_ais", "optical_ais", "radar_optical"):
        tp = fp = 0
        for p in results[k]["accepted"]:
            a, b = _mmsi_of(p["track_a"]), _mmsi_of(p["track_b"])
            if a and b:
                tp += (a == b)
                fp += (a != b)
        evals[k] = {"kind": k, "n_pairs": len(results[k]["accepted"]),
                    "mmsi_tp": tp, "mmsi_fp": fp}
    summary = {
        "alignment_dir": alignment_dir,
        "timebase": timebase.get("coverage"),
        "candidates": {s: [{"track_id": t.track_id, "points": t.point_count, "raw": t.raw_point_count}
                           for t in v] for s, v in cands.items()},
        "associations": {k: {"accepted": v["accepted"], "rejected_count": len(v["rejected"])}
                         for k, v in results.items()},
        "mmsi_evaluation": evals,
        "gates": {"radar_ais_m": GATE_RADAR_AIS_M, "optical_ais_m": GATE_OPTICAL_AIS_M,
                  "radar_optical_m": GATE_RADAR_OPTICAL_M,
                  "optical_bias_deg": OPTICAL_BIAS_DEG, "radar_bias_deg": RADAR_BIAS_DEG,
                  "bias_correction_applied": True},
        "note": "雷达侧只用纯测量点；评分前显式系统差修正（光电/雷达ENU绕站心旋转）；"
                "optical_ais与radar_optical允许B端多对一；重建航迹无MMSI。",
    }
    with open(os.path.join(output_dir, "association_summary.json"), "w", encoding="utf-8") as h:
        json.dump(summary, h, ensure_ascii=False, indent=2)
        h.write("\n")
    print(f"[associate] -> {output_dir} "
          f"radar_ais={len(results['radar_ais']['accepted'])} "
          f"optical_ais={len(results['optical_ais']['accepted'])} "
          f"radar_optical={len(results['radar_optical']['accepted'])}")
    return summary


# --------------------------------------------------------------------------
# Stage 6: 主目标提取 + 碎片过滤（测量距离：米）
# --------------------------------------------------------------------------
def run_primary(scen: str, alignment_dir: str, assoc_dir: str) -> dict:
    cfg = SCENARIOS[scen]
    cut_path = os.path.join(ROOT, cfg["cutdata"])
    cut = list(csv.DictReader(open(cut_path, encoding="utf-8-sig"))) if os.path.exists(cut_path) else []
    summary = json.load(open(os.path.join(assoc_dir, "association_summary.json"), encoding="utf-8"))
    recon = {}
    for f in glob.glob(os.path.join(alignment_dir, "tracks", "radar_reconstructed", "*.csv")):
        tid = "radar_reconstructed:" + os.path.basename(f)[:-4]
        rr = list(csv.DictReader(open(f, encoding="utf-8")))
        labs = collections.Counter()
        srcs = set()
        for x in rr:
            srcs.add(str(x.get("source_row")))
            for p in str(x.get("member_source_rows", "")).strip("[]").split(","):
                p = p.strip().strip("'").strip('"')
                if p:
                    srcs.add(p)
        for so in srcs:
            try:
                r = cut[int(so) - 1]
                labs[(r["目标类型"], r["目标编号"], r["匹配编号"])] += 1
            except Exception:
                labs[("?", "?", "?")] += 1
        recon[tid] = {"labels": labs, "n": len(rr)}
    ro = summary["associations"]["radar_optical"]["accepted"]
    ra = summary["associations"]["radar_ais"]["accepted"]
    main_frags = {a["track_a"] for a in ro
                  if a.get("confidence") == "high" and a["track_a"].startswith("radar_reconstructed:")}
    main_frags |= {a["track_a"] for a in ra if a["track_a"].startswith("radar_reconstructed:")}
    main_radar_full = [a["track_a"] for a in ra if a["track_a"].startswith("radar:")]
    cand_recon = {c["track_id"] for c in summary["candidates"]["radar"]
                  if c["track_id"].startswith("radar_reconstructed:")}
    filtered = sorted(cand_recon - main_frags)

    def has413(tid):
        return sum(v for k, v in recon.get(tid, {}).get("labels", {}).items() if k[1] == "413203610")

    def all900(tid):
        labs = recon.get(tid, {}).get("labels", {})
        return len(labs) > 0 and all(k[1].startswith("900") or k[0] == "?" for k in labs)

    out = {
        "scenario": scen,
        "main_target_mmsi": "413203610",
        "main_radar_full": main_radar_full,
        "main_fragments": sorted(main_frags),
        "filtered_fragments": filtered,
        "n_filtered": len(filtered),
        "distances_m": {
            "radar_ais": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"]) for a in ra],
            "optical_ais": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"])
                            for a in summary["associations"]["optical_ais"]["accepted"]],
            "radar_optical_main": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"])
                                   for a in ro if a.get("confidence") == "high"],
            "radar_optical_filtered": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"])
                                       for a in ro if a.get("confidence") == "low"],
        },
        "truth_check": {
            "main_frags_with_413": sum(1 for t in main_frags if has413(t) > 0),
            "main_frags_total": len(main_frags),
            "filtered_all900": sum(1 for t in filtered if t in recon and all900(t)),
            "filtered_total": len(filtered),
        },
    }
    with open(os.path.join(assoc_dir, "primary_targets.json"), "w", encoding="utf-8") as h:
        json.dump(out, h, ensure_ascii=False, indent=2)
        h.write("\n")
    print(f"[primary][{scen}] main={main_radar_full}+{len(main_frags)}frags "
          f"filtered={len(filtered)} truth413={out['truth_check']['main_frags_with_413']}/{len(main_frags)}")
    return out


# --------------------------------------------------------------------------
# 全链编排
# --------------------------------------------------------------------------
def run_scenario(scen: str, output_root: str, only: list = None) -> dict:
    only = only or STAGES
    result = {"scenario": scen}
    std_paths = None
    alignment_dir = os.path.join(output_root, "alignment", scen)
    assoc_dir = os.path.join(output_root, "association", scen)
    if "standardize" in only:
        std_paths = run_standardize(scen, output_root)
        result["standardized"] = std_paths
    if "align" in only:
        alignment_dir = run_align(scen, output_root, std_paths)
        result["alignment_dir"] = alignment_dir
    if "tracks" in only:
        run_tracks(alignment_dir)
        result["tracks"] = os.path.join(alignment_dir, "tracks")
    if "reconstruct" in only:
        run_reconstruct(alignment_dir)
    if "associate" in only:
        run_associate(alignment_dir, assoc_dir)
        result["association_dir"] = assoc_dir
    if "primary" in only:
        run_primary(scen, alignment_dir, assoc_dir)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="水面三源整体处理系统（雷达/AIS/光电，不含可视化）")
    ap.add_argument("--scenario", default="all", help="mtdsp_0305 | mtdsp2_0312 | all")
    ap.add_argument("--output-root", default=os.path.join(ROOT, "output", "system"),
                    help="输出根目录")
    ap.add_argument("--only", default=",".join(STAGES),
                    help="逗号分隔的子集，如 standardize,align,tracks,reconstruct,associate,primary")
    args = ap.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    for s in only:
        if s not in STAGES:
            print(f"错误：未知阶段 {s}，可选 {STAGES}", file=sys.stderr)
            return 2
    scens = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for s in scens:
        if s not in SCENARIOS:
            print(f"错误：未知场景 {s}，可选 {list(SCENARIOS)}", file=sys.stderr)
            return 2
    out = [run_scenario(s, os.path.abspath(args.output_root), only) for s in scens]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
