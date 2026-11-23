#!/usr/bin/env python3
"""剔除航迹中的「单样本位置离群点」，并平滑 course_deg。

【背景 —— 为什么必须处理】
实测航迹（尤其 T002）里存在**孤立一个样本**的位置突变：
相邻样本几乎重合，中间那个却偏出 20~30 米，且航向瞬间翻转 ~152°。

  T002 t=282000  east=-725.15  north=4666.93  course=56.3°
  T002 t=285000  east=-725.15  north=4666.93  course=56.3°
  T002 t=286000  east=-698.25  north=4664.81  course=208.3°   ← 离群
  T002 t=287000  east=-725.15  north=4666.93  course=56.3°

后果有两个：
  1. **回放瞬移**：`interpolate()` 在 285000 时会在 280000 与 286000 之间
     线性插值，于是船被拉向离群点，285000~287000 之间来回跳；
  2. **画面穿模**：离群点经 `displayPosition()` 单点 clamp 后，
     可能被推到与相邻点毫不相干的水域格，位移一度达 272px。

【处理原则 —— 只动「孤立离群」，不改变正常航迹】
判据（同时满足才算离群）：
  · 该样本落在前后样本连线的**垂直距离** > OUTLIER_DEV_M；
  · 且该样本的前后邻居**彼此很接近**（说明是一个孤立尖刺，
    而不是航迹本来就急转）。
这样真实的急转弯会被保留，只有「毛刺」被抹平。

【course_deg 的处理】
course 噪声极大（T002 24 个样本里 13 次相邻跳变 > 45°），
与位置噪声同源。这里用**平滑后的切线方向**回填 course_deg：
  θ = atan2(Δe, Δn)   （0° = 正北，顺时针为正，与 course_deg 同约定）
注意：只改 course_deg，不改 east/north —— 位置是实测真值，必须保留。

【用法】
  python3 tools/patch-track-outliers.py            # 直接改写 tracks.json
  python3 tools/patch-track-outliers.py --dry      # 只报告，不改写
"""
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = os.path.join(ROOT, 'public', 'water-security', 'data', 'tracks.json')

# ── 离群毛刺判据 ──────────────────────────────────────────────
# 中间点偏离前后邻居连线的最小距离（米）
OUTLIER_DEV_M = 8.0
# 前后两个邻居彼此重合的最大距离（米）
# 小于这个值 + 偏离超过 OUTLIER_DEV_M → 判定为孤立毛刺。
# 这是唯一采纳的判据，理由见 find_outliers() 的注释。
NEIGHBOR_IDENTICAL_M = 1.0

# ── 航向平滑 ──────────────────────────────────────────────────
# 弦切法窗口（采样点个数）
COURSE_WINDOW = 5
# 【只对在航目标平滑】
# 锚泊/静止的船（T003/T005 全程不动），
# 其位置波动是**真实的 GPS 抖动**，不是毛刺 —— 不能改。
MOVING_SPAN_M = 150.0


def dev_from_chord(p, a, b):
    """p 相对线段 a→b 的垂直距离（米）"""
    ax, ay = a['east_m'], a['north_m']
    bx, by = b['east_m'], b['north_m']
    px, py = p['east_m'], p['north_m']
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 <= 0:
        return math.hypot(px - ax, py - ay)
    k = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return math.hypot(px - (ax + vx * k), py - (ay + vy * k))


def find_outliers(samples):
    """返回需要修正的样本下标集合。

    【唯一采纳的判据 —— 铁证】
    前后两个邻居**几乎重合**（彼此距离 < 1m），中间那个点却明显偏离（> 8m）。

    这是「孤立毛刺」无可辩驳的signature：
    船在 1 秒内跳出去 27m、下一秒又回到原位 —— 这不是航行，是定位毛刺。

      T002 t=286000：邻居距离 0.0m ，中间点偏 27.0m → 毛刺 ✓
      T001 t=77000 ：邻居距离 17.4m，中间点偏 20.0m → GPS 抖动，保留 ✓
      T001 泊位切换：邻居距离很大            → 真实转场，保留 ✓

    宁可漏判也不误判：实测坐标是真实测量数据，
    只有铁证级别的毛刺才允许回填。
    """
    bad = set()
    n = len(samples)

    for i in range(1, n - 1):
        p, a, b = samples[i - 1], samples[i], samples[i + 1]
        if p['time_ms'] == b['time_ms']:
            continue

        # 前后两个邻居彼此的距离
        neighbor_gap = math.hypot(b['east_m'] - p['east_m'], b['north_m'] - p['north_m'])
        if neighbor_gap >= NEIGHBOR_IDENTICAL_M:
            continue

        # 中间点偏离邻居连线的距离
        dev = dev_from_chord(a, p, b)
        if dev > OUTLIER_DEV_M:
            bad.add(i)
    return bad


def smooth_course(samples):
    """用弦切方向回填 course_deg（不改位置）。"""
    n = len(samples)
    xs = [s['east_m'] for s in samples]
    ys = [s['north_m'] for s in samples]
    span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    if span < MOVING_SPAN_M:
        return
    for i in range(n):
        lo = max(0, i - COURSE_WINDOW)
        hi = min(n - 1, i + COURSE_WINDOW)
        if hi <= lo:
            continue
        de = samples[hi]['east_m'] - samples[lo]['east_m']
        dn = samples[hi]['north_m'] - samples[lo]['north_m']
        if abs(de) < 1e-6 and abs(dn) < 1e-6:
            continue
        # atan2(Δe, Δn)：0° = 正北，顺时针为正 —— 与 course_deg 同约定
        c = math.degrees(math.atan2(de, dn)) % 360.0
        samples[i]['course_deg'] = round(c, 4)


def main():
    dry = '--dry' in sys.argv
    with open(TRACKS, encoding='utf-8') as f:
        data = json.load(f)

    total_fixed = 0
    for tr in data['items']:
        ss = tr['samples']
        bad = find_outliers(ss)
        if bad:
            total_fixed += len(bad)
            print(f"\n=== {tr['global_track_id']} —— {len(bad)} 个离群样本 ===")
            for i in sorted(bad):
                a = ss[i]
                print(
                    f"  #{i} t={a['time_ms']:>7} "
                    f"east={a['east_m']:>9.2f} north={a['north_m']:>9.2f} "
                    f"course={a.get('course_deg')}"
                )
                if not dry:
                    # 用前后邻居的中点回填该样本位置
                    p, nxt = ss[i - 1], ss[i + 1]
                    a['east_m'] = round((p['east_m'] + nxt['east_m']) / 2.0, 3)
                    a['north_m'] = round((p['north_m'] + nxt['north_m']) / 2.0, 3)
                    print(
                        f"        → 回填 east={a['east_m']:.2f} north={a['north_m']:.2f}"
                    )
        if not dry:
            smooth_course(ss)

    if dry:
        print(f"\n[--dry] 共发现 {total_fixed} 个离群样本，未改写文件")
        return

    with open(TRACKS, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print(f"\n已改写 {TRACKS}")
    print(f"共修正 {total_fixed} 个离群样本，并对全部航迹平滑了 course_deg")


if __name__ == '__main__':
    main()
