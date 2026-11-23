"""用 CutDataTarget.csv 自带标注验证关联结果.

真值口径 (已核验):
  A. AT 行 目标编号=MMSI: 采集方雷达-AIS关联标签
  B. CDT 行 目标编号=413203610: 雷达检测直接标号
  C. 匹配编号=1: 雷达-光电确认配对 (AT 25+CDT 21 @03-05; AT 23+CDT 14 @03-12)
  D. Photo 文件名 AIS_0_413203610: 光电主目标真值

用法: python3 verification/verify_association_truth.py
"""
import csv, json, glob, os, collections

SCEN = [
    ("03-05", "data/mtdsp/CutDataTarget.csv",
     "output/alignment/mtdsp_yantai_optical",
     "output/association/mtdsp_yantai_optical"),
    ("03-12", "data/mtdsp2/CutDataTarget.csv",
     "output/alignment/mtdsp2_yantai_optical",
     "output/association/mtdsp2_yantai_optical"),
]

for ds, raw, al, assoc in SCEN:
    print("=" * 60 + f"\n【{ds}】")
    cut = list(csv.DictReader(open(raw, encoding="utf-8-sig")))
    at = collections.Counter(r["目标编号"] for r in cut if r["目标类型"] == "AT")
    print(f"真值A AT标签: {dict(at)}")
    c413 = [r for r in cut if r["目标类型"] == "CDT" and r["目标编号"] == "413203610"]
    m1 = [r for r in cut if r["匹配编号"] == "1"]
    print(f"真值B CDT标413: {len(c413)}条; 真值C match=1: {len(m1)}条 "
          f"(AT{sum(1 for r in m1 if r['目标类型']=='AT')}/CDT{sum(1 for r in m1 if r['目标类型']=='CDT')}, 全部MMSI=413203610)")
    # 纯测量可关联性
    for f in sorted(glob.glob(al + "/tracks/radar/*.csv")):
        tid = os.path.basename(f)[:-4]
        if "900" in tid or tid == "1":
            continue
        rr = list(csv.DictReader(open(f, encoding="utf-8")))
        pure = sum(1 for x in rr if x.get("radar_measurement_kind") == "radar_detected")
        snap = sum(1 for x in rr if x.get("radar_measurement_kind") == "ais_associated")
        print(f"  radar:{tid} 纯测量{pure} 快照{snap}")
    # 重建轨成员标签
    recon = {}
    for f in glob.glob(al + "/tracks/radar_reconstructed/*.csv"):
        tid = "radar_reconstructed:" + os.path.basename(f)[:-4]
        rr = list(csv.DictReader(open(f, encoding="utf-8")))
        s = set()
        for x in rr:
            s.add(str(x.get("source_row")))
            for p in str(x.get("member_source_rows", "")).strip("[]").split(","):
                p = p.strip().strip("'").strip('"')
                if p:
                    s.add(p)
        recon[tid] = s
    s = json.load(open(assoc + "/association_summary.json", encoding="utf-8"))
    for k in ["radar_ais", "optical_ais", "radar_optical"]:
        print(f"  {k} accepted:")
        for a in s["associations"][k]["accepted"]:
            extra = ""
            if a["track_a"].startswith("radar_reconstructed:"):
                labs = collections.Counter()
                for so in recon.get(a["track_a"], set()):
                    try:
                        r = cut[int(so) - 1]
                        labs[(r["目标类型"], r["目标编号"], r["匹配编号"])] += 1
                    except Exception:
                        labs[("?", "?", "?")] += 1
                extra = f" 成员{dict(labs)}"
            print(f"    {a['track_a']} <-> {a['track_b']} med={a['median_m']} ov={a['overlap']} {a.get('confidence','')}{extra}")
    # CDT标413检测级召回
    acc = {a["track_a"]: a["track_b"] for a in s["associations"]["radar_ais"]["accepted"]}
    tot = hit = 0
    miss = collections.Counter()
    for idx, r in enumerate(cut, start=1):
        if not (r["目标类型"] == "CDT" and r["目标编号"] == "413203610"):
            continue
        tot += 1
        owner = next((t for t, m in recon.items() if str(idx) in m), None)
        if owner is None:
            miss["重建未收录"] += 1
        elif acc.get(owner) == "ais:413203610":
            hit += 1
        else:
            miss[f"{owner}->{acc.get(owner)}"] += 1
    print(f"  CDT标413检测级召回: {hit}/{tot} = {100*hit/tot:.1f}% 未命中{dict(miss)}")
