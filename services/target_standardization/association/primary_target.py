"""主目标提取 + 碎片过滤（基于已有关联输出，不改关联算法）.

规则（无真值可独立运行，真值只用于验证）:
  主目标 = 与 AIS/光电主目标高置信关联的重建碎片 + radar:<MMSI> 整轨
  碎片/杂波 = radar-optical 低置信（median > 300m）或成员全为 900xxx 的重建轨
  距离 = 关联对的重叠段平面残差 median/mean（米），偏差修正后

用法: python3 association/primary_target.py
输出: output/association/<scen>/primary_targets.json
"""
import json, csv, glob, os, collections

SCEN = [
    ("03-05", "data/mtdsp/CutDataTarget.csv",
     "output/alignment/mtdsp_yantai_optical",
     "output/association/mtdsp_yantai_optical"),
    ("03-12", "data/mtdsp2/CutDataTarget.csv",
     "output/alignment/mtdsp2_yantai_optical",
     "output/association/mtdsp2_yantai_optical"),
]

for ds, raw, al, assoc in SCEN:
    cut = list(csv.DictReader(open(raw, encoding="utf-8-sig")))
    s = json.load(open(os.path.join(assoc, "association_summary.json"), encoding="utf-8"))
    # 重建轨成员标签
    recon = {}
    for f in glob.glob(os.path.join(al, "tracks/radar_reconstructed/*.csv")):
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
    # 高置信雷达-光电对 => 主目标碎片
    ro = s["associations"]["radar_optical"]["accepted"]
    ra = s["associations"]["radar_ais"]["accepted"]
    main_frags = set()
    for a in ro:
        if a.get("confidence") == "high" and a["track_a"].startswith("radar_reconstructed:"):
            main_frags.add(a["track_a"])
    for a in ra:
        if a["track_a"].startswith("radar_reconstructed:"):
            main_frags.add(a["track_a"])
    # radar:<MMSI> 整轨 + AIS 主目标
    main_radar_full = [a["track_a"] for a in ra if a["track_a"].startswith("radar:")]
    # 碎片：所有候选重建轨 - 主目标碎片
    all_recon = set("radar_reconstructed:" + os.path.basename(f)[:-4]
                    for f in glob.glob(os.path.join(al, "tracks/radar_reconstructed/*.csv")))
    # 只看进入候选的（points>=3）；从 summary candidates 取
    cand_recon = set(c["track_id"] for c in s["candidates"]["radar"] if c["track_id"].startswith("radar_reconstructed:"))
    filtered = sorted(cand_recon - main_frags)
    out = {
        "scenario": ds,
        "main_target_mmsi": "413203610",
        "main_radar_full": main_radar_full,
        "main_fragments": sorted(main_frags),
        "filtered_fragments": filtered,
        "n_filtered": len(filtered),
        "distances_m": {
            "radar_ais": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"]) for a in ra],
            "optical_ais": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"]) for a in s["associations"]["optical_ais"]["accepted"]],
            "radar_optical_main": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"]) for a in ro if a.get("confidence") == "high"],
            "radar_optical_filtered": [(a["track_a"], a["track_b"], a["median_m"], a.get("mean_m"), a["overlap"]) for a in ro if a.get("confidence") == "low"],
        },
    }
    # 真值验证：主碎片是否含 CDT 413 / 被滤掉的是否全 900xxx
    def has413(tid):
        return sum(v for k, v in recon.get(tid, {}).get("labels", {}).items() if k[1] == "413203610")
    def all900(tid):
        labs = recon.get(tid, {}).get("labels", {})
        return len(labs) > 0 and all(k[1].startswith("900") or k[0] == "?" for k in labs)
    out["truth_check"] = {
        "main_frags_with_413": sum(1 for t in main_frags if has413(t) > 0),
        "main_frags_total": len(main_frags),
        "filtered_all900": sum(1 for t in filtered if t in recon and all900(t)),
        "filtered_total": len(filtered),
    }
    with open(os.path.join(assoc, "primary_targets.json"), "w", encoding="utf-8") as h:
        json.dump(out, h, ensure_ascii=False, indent=2)
        h.write("\n")
    print("=" * 60 + f"\n【{ds}】主目标 MMSI=413203610")
    print(f" 主雷达整轨: {main_radar_full}")
    print(f" 主碎片({len(main_frags)}): {sorted(main_frags)}")
    print(f" 含CDT413: {out['truth_check']['main_frags_with_413']}/{len(main_frags)}")
    print(f" 被滤碎片({len(filtered)}): {filtered[:12]}{'...' if len(filtered)>12 else ''}")
    print(" 测量距离(米, median/mean/重叠点数):")
    for k, v in out["distances_m"].items():
        print(f"  [{k}]")
        for row in v:
            print(f"    {row[0]} <-> {row[1]}: median {row[2]}m / mean {row[3]}m / ov {row[4]}")
