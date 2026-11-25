#!/usr/bin/env python3
"""
核验水面传感器波束（光电视场锥 / AIS 接收扇面）是否落在**水域**上。

【为什么必须核验】
传感器在陆上（厂区/码头），波束指向水面。
如果波束方向指错了，就会出现「黄色视场锥压在厂房和山体上」——
动画与背景对不上，看起来像浮在背景之上的贴纸。

之前 `SceneSensorsV2.vue` 的 `bearingDeg` 就是靠「逐个方向检查水域覆盖率」
选出来的（见该文件 SENSORS 表的注释）。
现在光电视场锥会**动态指向被跟踪的目标**，
所以必须对**每个可能的目标**重新做这个检查。

【做法】
  1. 由底图生成水域掩码（与之前判定堤体陆地用的是同一套逻辑）；
  2. 对每个候选目标：
       · 求光电塔 → 目标的方位角；
       · 沿该方位从塔位扫到目标，统计沿线的水域占比；
       · 同时采样「视场锥的宽度范围」（±fov/2）内的水域占比。
  3. 若某目标的视场锥明显压在陆地上 → 报出来。
"""
import math
import os
import json

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBLIQUE = os.path.join(
    ROOT, "public/water-security/assets/scenes-v2/terrain/campus_oblique_v3.png"
)
DATA = os.path.join(ROOT, "public/water-security/data")

# ------------------------------------------------------------------ #
# 仿射（复刻 scenes.config.v2.ts 的 OBLIQUE_FIT）
# ------------------------------------------------------------------ #
e_min, e_max, n_min, n_max = -2600, 1000, 2900, 5100
x0, y0, x1, y1 = 0, 40, 1536, 1000
A = (x1 - x0) / (e_max - e_min)
D = -((y1 - y0) / (n_max - n_min))
E = x0 - A * e_min
F = y0 - D * n_max


def enu2px(e, n):
    return (A * e + E, D * n + F)


# ------------------------------------------------------------------ #
# 水域掩码
# ------------------------------------------------------------------ #
im = Image.open(OBLIQUE).convert("RGB")
W, H = im.size
px = im.load()

# 底图原生 1536×1024，但 OBLIQUE_FIT.img 内容区是 x 0~1536、y 40~1000
print(f"底图 {W}×{H}，仿射 a={A:.5f} d={D:.5f}")

# 水域判定：水面的蓝绿色系（低红、中绿、高蓝）
water = bytearray(W * H)
for y in range(H):
    for x in range(W):
        r, g, b = px[x, y]
        if b > r + 12 and g > r + 4 and b > 60:
            water[y * W + x] = 1


def is_water(x, y):
    if x < 0 or y < 0 or x >= W or y >= H:
        return False
    return water[int(y) * W + int(x)] == 1


print(f"水域像素占比 {sum(water) / len(water):.1%}")


def water_ratio_along(e0, n0, e1, n1, samples=120):
    """沿 (e0,n0)→(e1,n1) 采样，统计水域占比。"""
    hit = 0
    for i in range(samples + 1):
        t = i / samples
        e = e0 + (e1 - e0) * t
        n = n0 + (n1 - n0) * t
        x, y = enu2px(e, n)
        if is_water(x, y):
            hit += 1
    return hit / (samples + 1)


def cone_water_ratio(e0, n0, bearing_deg, range_m, half_fov, samples=120, rays=9):
    """
    视场锥的水域占比。

    【为什么要多射线】
    视场锥是一个扇形，只测中轴不够 ——
    锥体边缘可能压到岸上而中轴在水中。
    这里在 ±half_fov 内取 rays 条射线（含中轴），求平均。
    """
    a = math.radians(bearing_deg)
    ratios = []
    for k in range(rays):
        off = -half_fov + (2 * half_fov) * k / max(1, rays - 1)
        th = a + math.radians(off)
        e1 = e0 + range_m * math.sin(th)
        n1 = n0 + range_m * math.cos(th)
        ratios.append(water_ratio_along(e0, n0, e1, n1, samples))
    return sum(ratios) / len(ratios), ratios


# ------------------------------------------------------------------ #
# 传感器
# ------------------------------------------------------------------ #
EO_E, EO_N, EO_RANGE, EO_FOV = -57.0, 3747.9, 1000, 26
AIS_E, AIS_N, AIS_RANGE, AIS_HALF = -748.4, 3587.5, 1200, 25
RADAR_E, RADAR_N, RADAR_RANGE = -1985.9, 4034.4, 1500

print("\n" + "=" * 74)
print("水面传感器波束水域覆盖核验")
print("=" * 74)

# ---- 固定方位（雷达 / AIS） ----
print("\n【固定方位传感器】")
for name, (e, n), rng, half in [
    ("RADAR-01", (RADAR_E, RADAR_N), RADAR_RANGE, 26),
    ("AIS-01", (AIS_E, AIS_N), AIS_RANGE, AIS_HALF),
]:
    r, rays = cone_water_ratio(e, n, 175 if name == "RADAR-01" else 105, rng, half)
    print(f"  {name}: 视场水域占比 {r:.1%}  单射线 {['%.2f' % x for x in rays]}")
    print(f"         {'✅ 通过' if r >= 0.6 else '❌ 波束压在陆地上'}")

# ---- 动态光电（指向被跟踪目标） ----
print("\n【光电 EO-01 —— 视场锥动态指向被跟踪目标】")

tracks = json.load(open(os.path.join(DATA, "tracks.json"), encoding="utf-8"))
targets = json.load(open(os.path.join(DATA, "targets.json"), encoding="utf-8"))

surface = [t for t in tracks["items"] if t["global_track_id"].startswith("T")]

results = []
for tr in surface:
    tid = tr["global_track_id"]
    meta = next(x for x in targets["items"] if x["global_track_id"] == tid)
    for s in tr["samples"]:
        pass
    # 逐样本评估（光电会跟随目标移动）
    worst = (1.0, None)
    tot = 0
    cnt = 0
    for s in tr["samples"]:
        e, n = s["east_m"], s["north_m"]
        dist = math.hypot(e - EO_E, n - EO_N)
        bearing = (math.degrees(math.atan2(e - EO_E, n - EO_N)) + 360) % 360
        r, _ = cone_water_ratio(EO_E, EO_N, bearing, min(EO_RANGE, max(dist, 50)), EO_FOV / 2, 40, 5)
        tot += r
        cnt += 1
        if r < worst[0]:
            worst = (r, (s["time_ms"], bearing, dist))
    avg = tot / cnt
    results.append((tid, meta["target_type"], avg, worst))

results.sort(key=lambda r: r[2])
print(f"  {'目标':6} {'类型':16} {'视场水域占比(均)':>18} {'最差样本':>12}  判定")
for tid, ttype, avg, worst in results:
    ok = "✅" if avg >= 0.5 else "❌"
    print(
        f"  {tid:6} {ttype[:14]:16} {avg:>17.1%} {worst[0]:>11.1%}  {ok}"
    )

print("\n" + "=" * 74)
print("说明：光电默认跟踪「在 1000m 内方位最接近塔的方向」的目标；")
print("      若视场锥落水率过低，说明该目标在陆地方向，需调整跟踪策略。")
print("=" * 74)
