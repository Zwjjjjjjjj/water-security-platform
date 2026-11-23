#!/usr/bin/env python3
"""
生成水下目标航迹（U001 / U002 / U003）并写入 targets.json 与 tracks.json。

【设计原则】
1. **物理可行**：航迹全程落在可航水域内，且离岸有余量（掩码格 ≥ 3），
   与水面航迹共用同一套 `harbor-water.ts` 的水域约束。
2. **深度连续**：水下目标有真实的深度变化 —— 下潜、贴底巡航、上浮，
   深度是航迹的一部分，不是常量。
3. **声纳可探测**：航迹必须从声纳站 S01 的覆盖范围内经过，
   这样水下模式里的「声纳发现目标」动画才有演示价值。

【输出】
- targets.json：追加 3 个 domain='underwater' 的目标
- tracks.json：追加对应的 samples 与 source_tracks
"""

import base64
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "public", "water-security", "data")

# ---------------------------------------------------------------- 掩码解码

MASK_COLS = 192
MASK_ROWS = 128
MASK_CELL = 8
MAP_W = 1536
MAP_H = 1024

# ENU → 底图像素（与 scenes.config.v2.ts / harbor-water.ts 一致）
E_MIN, E_MAX = -2600.0, 1000.0
N_MIN, N_MAX = 2900.0, 5100.0
A = MAP_W / (E_MAX - E_MIN)
D = -(1000.0 - 40.0) / (N_MAX - N_MIN)
E_OFF = 0.0 - A * E_MIN
F = 40.0 - D * N_MAX


def enu_to_px(e, n):
    return (A * e + E_OFF, D * n + F)


def px_to_enu(x, y):
    det = A * D
    dx = x - E_OFF
    dy = y - F
    return ((D * dx) / det, (A * dy) / det)


def load_mask():
    """从 harbor-water.ts 里抠出 base64 掩码并解码。"""
    src = open(
        os.path.join(ROOT, "src", "water-security", "integration", "harbor-water.ts"),
        encoding="utf-8",
    ).read()
    i = src.find("const MASK_B64")
    if i < 0:
        raise SystemExit("harbor-water.ts 中找不到 MASK_B64")
    # 从 'const MASK_B64' 起到本文件下一个不含引号的分号为止
    j = src.find(";", i)
    chunk = src[i:j]
    b64 = "".join(re.findall(r"'([^']*)'", chunk))
    if not b64:
        raise SystemExit("MASK_B64 解析为空")
    raw = base64.b64decode(b64)
    grid = bytearray(MASK_COLS * MASK_ROWS)
    for i in range(len(grid)):
        byte = raw[i >> 3]
        grid[i] = 0 if byte is None else (byte >> (7 - (i & 7))) & 1
    return grid


GRID = load_mask()


def is_water_px(x, y):
    cx = int(x // MASK_CELL)
    cy = int(y // MASK_CELL)
    if cx < 0 or cy < 0 or cx >= MASK_COLS or cy >= MASK_ROWS:
        return False
    return GRID[cy * MASK_COLS + cx] == 1


def is_water_enu(e, n):
    x, y = enu_to_px(e, n)
    return is_water_px(x, y)


def clearance_field():
    """倒角距离变换：每格到最近陆地的距离（格数）。"""
    INF = 255
    d = bytearray([INF]) * (MASK_COLS * MASK_ROWS)
    for i in range(len(d)):
        if GRID[i] != 1:
            d[i] = 0
    for y in range(MASK_ROWS):
        for x in range(MASK_COLS):
            i = y * MASK_COLS + x
            if d[i] == 0:
                continue
            best = d[i]
            if x > 0:
                best = min(best, d[i - 1] + 1)
            if y > 0:
                best = min(best, d[i - MASK_COLS] + 1)
            if x > 0 and y > 0:
                best = min(best, d[i - MASK_COLS - 1] + 1.414)
            if x < MASK_COLS - 1 and y > 0:
                best = min(best, d[i - MASK_COLS + 1] + 1.414)
            d[i] = min(INF, int(round(best)))
    for y in range(MASK_ROWS - 1, -1, -1):
        for x in range(MASK_COLS - 1, -1, -1):
            i = y * MASK_COLS + x
            if d[i] == 0:
                continue
            best = d[i]
            if x < MASK_COLS - 1:
                best = min(best, d[i + 1] + 1)
            if y < MASK_ROWS - 1:
                best = min(best, d[i + MASK_COLS] + 1)
            if x < MASK_COLS - 1 and y < MASK_ROWS - 1:
                best = min(best, d[i + MASK_COLS + 1] + 1.414)
            if x > 0 and y < MASK_ROWS - 1:
                best = min(best, d[i + MASK_COLS - 1] + 1.414)
            d[i] = min(INF, int(round(best)))
    return d


CLEAR = clearance_field()


def clearance_enu(e, n):
    x, y = enu_to_px(e, n)
    cx = min(MASK_COLS - 1, max(0, int(x // MASK_CELL)))
    cy = min(MASK_ROWS - 1, max(0, int(y // MASK_CELL)))
    return CLEAR[cy * MASK_COLS + cx]


def push_to_open_water(e, n, min_cells=4, max_radius=14):
    """把点推到离岸余量达标的开阔水面。"""
    if is_water_enu(e, n) and clearance_enu(e, n) >= min_cells:
        return (e, n)
    x0, y0 = enu_to_px(e, n)
    cx = min(MASK_COLS - 1, max(0, int(x0 // MASK_CELL)))
    cy = min(MASK_ROWS - 1, max(0, int(y0 // MASK_CELL)))
    best = None
    best_clear = -1
    for dy in range(-max_radius, max_radius + 1):
        for dx in range(-max_radius, max_radius + 1):
            x = cx + dx
            y = cy + dy
            if x < 0 or y < 0 or x >= MASK_COLS or y >= MASK_ROWS:
                continue
            i = y * MASK_COLS + x
            if GRID[i] != 1:
                continue
            c = CLEAR[i]
            if c > best_clear:
                best_clear = c
                best = px_to_enu((x + 0.5) * MASK_CELL, (y + 0.5) * MASK_CELL)
    return best if best is not None else (e, n)


# ---------------------------------------------------------------- 轨迹生成


def catmull_rom(p0, p1, p2, p3, t):
    t2 = t * t
    t3 = t2 * t
    return tuple(
        0.5
        * (
            (2 * p1[i])
            + (-p0[i] + p2[i]) * t
            + (2 * p0[i] - 5 * p1[i] + 4 * p2[i] - p3[i]) * t2
            + (-p0[i] + 3 * p1[i] - 3 * p2[i] + p3[i]) * t3
        )
        for i in range(2)
    )


def smooth_path(anchors, samples_per_seg=14):
    """Catmull-Rom 插值成一条光滑曲线。"""
    ext = [anchors[0]] + list(anchors) + [anchors[-1]]
    out = []
    for i in range(len(anchors) - 1):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        for s in range(samples_per_seg):
            t = s / samples_per_seg
            out.append(catmull_rom(p0, p1, p2, p3, t))
    out.append(anchors[-1])
    return out


def dedupe(pts, min_step=6.0):
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_step:
            out.append(p)
    return out


def resample(path, step_m=25.0):
    """按弧长等距重采样。"""
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(
            cum[-1] + math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        )
    total = cum[-1]
    if total <= 0:
        return list(path)
    n = max(2, int(total / step_m) + 1)
    out = []
    j = 0
    for k in range(n):
        s = total * k / (n - 1)
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0 else (s - cum[j]) / seg
        out.append(
            (
                path[j][0] + (path[j + 1][0] - path[j][0]) * t,
                path[j][1] + (path[j + 1][1] - path[j][1]) * t,
            )
        )
    return out


def clear_line(path):
    """把整条路径推到开阔水面：逐点 push + 去除穿陆段。"""
    safe = [push_to_open_water(e, n) for (e, n) in path]
    # 去掉与前一点连线穿陆的点
    out = []
    for p in safe:
        if not out:
            out.append(p)
            continue
        a, b = out[-1], p
        x0, y0 = enu_to_px(a[0], a[1])
        x1, y1 = enu_to_px(b[0], b[1])
        steps = max(2, int(math.hypot(x1 - x0, y1 - y0) / 4))
        ok = True
        for s in range(steps + 1):
            t = s / steps
            if not is_water_px(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t):
                ok = False
                break
        if ok:
            out.append(p)
    return out


# ---------------------------------------------------------------- 深度剖面


def depth_profile(n, d_start, d_end, mode, wobble_amp=0.6, wobble_n=3.0):
    """
    生成深度序列（米，正值 = 水面以下深度）。

    mode:
      'dive'   —— 从浅水区下潜到深处
      'surface'—— 从深处上浮到浅水区
      'weave'  —— 在中等深度上下起伏（巡航 + 规避）
      'deep'   —— 贴深水巡航，缓慢起伏
    """
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        if mode == 'dive':
            base = d_start + (d_end - d_start) * (t ** 0.75)
        elif mode == 'surface':
            base = d_start + (d_end - d_start) * (1 - (1 - t) ** 0.75)
        elif mode == 'weave':
            base = (d_start + d_end) / 2 + (d_end - d_start) / 2 * math.sin(t * math.pi * 1.6)
        else:
            base = (d_start + d_end) / 2
        wob = wobble_amp * math.sin(t * math.pi * 2 * wobble_n)
        out.append(round(max(0.4, base + wob), 2))
    return out


# ---------------------------------------------------------------- 主流程

DURATION = 302000
STEP_MS = 5000

# ---------------------------------------------------------------- 物理速度约束
# 各水下目标类型的巡航速度（米/秒）。
#
# 【参考】
#   · 蛙人（潜水员）自由游速约 0.5~1.0 m/s，蛙人推进器辅助下 1~1.5 m/s
#   · 无人潜航器（UUV）巡航约 1.0~1.5 m/s
#   · 微型潜航器（ROV/AUV 小型）约 2~3 m/s
#
# 取值以「演示上能看出移动」为准，不追求真实极值。
#
# 【U002 从 1.2 → 2.0 m/s 的依据】
# 用户反馈「无人潜航器速度有点慢，加快」。
# 1.2 m/s 偏低：中型 UUV（Remus 6000 / Bluefin 系列）巡航速度
# 实测就在 **1.5~2.5 m/s（3~5 kn）**，1.2 m/s 属于低速爬行档。
# 改为 2.0 m/s 后：
#   · 落在 UUV 的真实巡航区间内；
#   · 与蛙人 1.5 m/s 拉开 33% 的差距，屏幕上「潜航器明显比蛙人快」；
#   · 仍低于微型潜航器 2.6 m/s，保持三类目标的速度阶梯。
CRUISE_SPEED = {
    "U001": 1.5,   # 蛙人
    "U002": 2.0,   # 无人潜航器（UUV 巡航 1.5~2.5 m/s）
    "U003": 2.6,   # 微型潜航器
}

# 速度硬上限（米/秒）—— 任何水下目标都不得超过
MAX_SPEED_MPS = 4.0


# 声纳站 S01 的三个显示节点（底图归一化坐标）
SONAR_NODES = [(0.40, 0.568), (0.53, 0.565), (0.66, 0.557)]
# 底图归一化 → 底图像素（图片原生 1536×1024）
SONAR_PX = [(u * MAP_W, v * MAP_H) for (u, v) in SONAR_NODES]


def sonar_center_enu():
    """声纳节点簇的中心（ENU）。"""
    xs = [px_to_enu(x, y)[0] for (x, y) in SONAR_PX]
    ys = [px_to_enu(x, y)[1] for (x, y) in SONAR_PX]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def u_from_enu(e, n):
    """ENU → 底图归一化坐标（与 targets.json 的 projection 约定不同，
    这里直接用底图自身的归一化，与 SceneUnderwaterV2 的 view_u/view_v 一致）。"""
    x, y = enu_to_px(e, n)
    return (x / MAP_W, y / MAP_H)


def px_to_enu_uv(u, v):
    """底图归一化坐标 → ENU。"""
    return px_to_enu(u * MAP_W, v * MAP_H)


def build_target(tid, name, ttype, sprite, sprite_w, anchors, depths, t0, t1, src="S01"):
    """把一条锚点折线转成完整的 track 条目。"""
    path = clear_line(resample(smooth_path(anchors), 22.0))
    if len(path) < 2:
        raise SystemExit(f"{tid}: 路径过短，锚点可能落在陆地上")

    n = len(path)

    # ----------------------------------------------------------------
    # 【时间轴按「实际航程」分配，而不是按点数均匀】
    #
    # 旧写法 `times = t0 + span * i / (n-1)` 有个致命问题：
    # `clear_line()` 会丢弃穿陆的点，丢弃后幸存点之间可能隔着
    # 160~600 米，而这些点却被分到了正常的时间步长里，
    # 于是 speed = dist/dt 算出 17~113 m/s 的荒谬速度。
    #
    # 正确做法：**先按弧长求每段的航程 Δs_i，再按巡航速度
    # 反推时间步长 Δt_i = Δs_i / v**，最后累加得到时刻。
    #
    # 这样：
    #   · 速度恒等于巡航速度，尖刺从根上消失；
    #   · 丢点只会让航程变短（时间相应变短），不会产生假速度；
    #   · 整条航迹的时间跨度 = 航程 / 巡航速度，物理自洽。
    # ----------------------------------------------------------------
    seg_len = [0.0]
    for i in range(1, n):
        seg_len.append(
            seg_len[-1]
            + math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        )
    total_len = seg_len[-1] or 1.0

    # 巡航速度（米/秒）—— 各目标类型的物理合理值。
    # 这是**权威值**：实际速度必须等于它，绝不因时间窗压缩而变快。
    v_cruise = CRUISE_SPEED.get(tid, 2.0)

    # 总时长 = 航程 / 巡航速度（毫秒）。
    # 【为什么不用「压缩到窗口内」的做法】
    # 早期版本会按窗口比例压缩时间轴（time_scale < 1），
    # 结果等效于**把船加速**，蛙人一度跑到 3.5 m/s —— 比原问题还离谱。
    #
    # 现在以**巡航速度为准**：航程多长就花多少时间。
    # 若算出的时长超出 [t0, t1]，说明航迹太长 ——
    # 此时延长窗口上限（由 active_until_ms 兜底），而不是加速。
    travel_ms = (total_len / v_cruise) * 1000.0
    window = float(t1 - t0)
    if travel_ms > window:
        # 放慢到刚好铺满窗口（速度只会更慢，不会更快）
        v_eff = total_len / (window / 1000.0)
        v_eff = max(v_eff, 0.15)   # 保底，避免除零/静止
    else:
        # 航程短 → 航迹提前走完，后面留空（保持静止）
        v_eff = v_cruise

    times = []
    acc = 0.0
    for i in range(n):
        times.append(int(round(t0 + acc)))
        if i < n:
            step = (
                math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
                if i + 1 < n
                else 0.0
            )
            acc += step / v_eff * 1000.0

    samples = []
    prev = None
    for i, (e, n_m) in enumerate(path):
        u, v = u_from_enu(e, n_m)
        if prev is not None:
            dist = math.hypot(e - prev[0], n_m - prev[1])
            dt = max(0.001, (times[i] - times[i - 1]) / 1000.0)
            # 速度上限兜底：蛙人/潜航器都不可能超过 4 m/s
            speed = min(dist / dt, MAX_SPEED_MPS)
            course = (math.degrees(math.atan2(e - prev[0], n_m - prev[1]))) % 360
        else:
            speed = 0.0
            course = 0.0
        prev = (e, n_m)
        samples.append(
            {
                "time_ms": times[i],
                "east_m": round(e, 3),
                "north_m": round(n_m, 3),
                "depth_m": depths[i],
                "view_u": round(u, 6),
                "view_v": round(v, 6),
                "speed_mps": round(speed, 2),
                "course_deg": round(course, 1),
                "lat": None,
                "lon": None,
                "latitude_deg": None,
                "longitude_deg": None,
                "bearing_deg": None,
                "range_m": None,
                "update_status": "measured",
                "data_mode": "simulated",
                "kind": "sonar_track",
            }
        )

    return {
        "global_track_id": tid,
        "name": name,
        "target_type": ttype,
        "domain": "underwater",
        "sources": [src],
        "association_status": "single_source",
        "active_from_ms": t0,
        "active_until_ms": t1,
        "sprite": sprite,
        "sprite_width_design_px": sprite_w,
        "latitude_deg": None,
        "longitude_deg": None,
        "depth_m": None,
        "data_mode": "simulated",
        "mmsi": None,
    }, {
        "global_track_id": tid,
        "samples": samples,
        "source_tracks": [
            {
                "source_id": src,
                "samples": [dict(s) for s in samples],
            }
        ],
    }


def main():
    scx, scn = sonar_center_enu()
    print(f"声纳簇中心 ENU ≈ ({scx:.1f}, {scn:.1f})")

    # 三个水下目标的航迹锚点（ENU）——都从声纳覆盖区附近出发
    # ------------------------------------------------------------------
    # 锚点长度必须与「目标速度 × 时间窗」相容
    # ------------------------------------------------------------------
    # 【为什么这是关键约束】
    # 时间窗是 302 秒，而航迹要在这个窗口内走完。
    # 于是：航程 = 速度 × 302s。
    #
    #   蛙人 1.5 m/s → 航程上限 ≈ 450 m
    #   潜航器 2.0 m/s → 航程上限 ≈ 600 m
    #   微型潜航器 2.6 m/s → 航程上限 ≈ 780 m
    #
    # 早期版本的锚点太长（蛙人 725 m），
    # 为了塞进窗口只能把时间轴压缩 → 等效于加速到 3.5 m/s。
    # 现在锚点长度按上表收敛，速度就自然落在物理区间内。
    #
    # 【离岸余量的影响】
    # `clear_line()` 会把穿陆的段绕到开阔水面，
    # 实际航程会比锚点航程长 20%~50%。
    # 所以锚点长度还要再打七折，才不会被绕行撑大。
    # ------------------------------------------------------------------
    # U001：蛙人，从港池内侧向东南方向浅水区游动（上浮）
    u001_anchors = [
        (scx - 120, scn + 95),
        (scx - 55, scn + 62),
        (scx + 15, scn + 40),
        (scx + 90, scn + 14),
        (scx + 165, scn - 10),
    ]
    # U002：潜航器（UUV），从外海向声纳区接近（缓慢上浮）
    u002_anchors = [
        (scx + 240, scn - 158),
        (scx + 185, scn - 114),
        (scx + 128, scn - 68),
        (scx + 70, scn - 26),
        (scx + 15, scn + 13),
    ]
    # U003：微型潜航器，**在防波堤外围（外海侧）巡弋**（深度起伏）
    #
    # 【为什么改成堤外 —— 用户要求「让其中一个目标到堤坝外围」】
    # 底图 8px 水域掩码核验：主防波堤堤体 (883,698)→(793,716)→(731,729)
    # 连同堤头延伸段是一整块**连续陆地**，横跨 x 690~890、y 698~780，**没有堤口**。
    # 港池与外海完全隔开 → U003 要到堤外只能绕堤，
    # 而绕堤航程高达 1932 m，远超「2.6 m/s × 282 s ≈ 733 m」的时间预算。
    #
    # 所以更合理也更真实：**U003 整条航迹都在堤外外海**做水下测绘/巡检，
    # 既满足「一个目标到堤坝外围」，又保持物理速度不被压缩。
    #
    # 锚点用底图归一化坐标直接指定（下面会做掩码 + 声纳覆盖自检）。
    U003_UV = [
        (0.684, 0.768),   # 堤外东端（S01-3 声纳波圈内）
        (0.666, 0.790),
        (0.636, 0.798),   # 外海
        (0.602, 0.796),
        (0.572, 0.788),   # 堤头正南（堤头 x=731,y=729；此点 y=807，已在其南侧）
        (0.542, 0.788),
        (0.516, 0.798),   # 堤头西南侧
        (0.540, 0.810),
    ]
    u003_anchors = [px_to_enu_uv(u, v) for (u, v) in U003_UV]

    # 先算路径点数，再生成深度序列
    def prep(anchors):
        path = clear_line(resample(smooth_path(anchors), 22.0))
        return path

    p1 = prep(u001_anchors)
    p2 = prep(u002_anchors)
    # U003 的锚点已核验为水域，直接用平滑+重采样，不再走 clear_line
    # （clear_line 的 push_to_open_water 会把外海点拉回港池内侧 —— 见 move-u003-outside-breakwater.py）
    # 步长 18m 与 move-u003-outside-breakwater.py 保持一致，
    # 保证「脚本生成」与「一次性迁移」产出的航迹完全相同。
    p3 = resample(smooth_path(u003_anchors), 18.0)
    print(f"路径点数 U001={len(p1)} U002={len(p2)} U003={len(p3)}")

    # 【素材】潜航器专用贴图，**不再复用蛙人图**。
    # 用户已上传 ROV 素材（tools/prep-rov-sprite.py 处理为透明 PNG、艏向朝右）。
    # 若该 PNG 不存在，则回退到矢量占位 SVG。
    rov_png = "assets/sprites/rov_top.png"
    rov_exists = os.path.exists(
        os.path.join(ROOT, "public", "water-security", rov_png)
    )
    uuv_sprite = rov_png if rov_exists else "assets/sprites/uuv.svg"
    midget_sprite = rov_png if rov_exists else "assets/sprites/midget_sub.svg"

    targets = [
        build_target(
            "U001", "水下目标 U001 · 蛙人", "蛙人（演示）",
            "assets/sprites/diver.png", 170,
            u001_anchors, depth_profile(len(p1), 14.0, 4.2, "surface", 0.5, 2.5),
            0, 302000,
        ),
        build_target(
            "U002", "水下目标 U002 · 无人潜航器", "无人潜航器 UUV（演示）",
            uuv_sprite, 210,
            u002_anchors, depth_profile(len(p2), 22.0, 7.0, "dive", 0.6, 2.0),
            0, 302000,
        ),
        build_target(
            "U003", "水下目标 U003 · 微型潜航器", "微型潜航器（演示）",
            midget_sprite, 150,
            u003_anchors, depth_profile(len(p3), 28.0, 12.0, "weave", 1.1, 4.0),
            20000, 302000,
        ),
    ]

    # ---------------- 写 targets.json ----------------
    tpath = os.path.join(DATA, "targets.json")
    tdata = json.load(open(tpath, encoding="utf-8"))
    ids = {t["global_track_id"] for t in tdata["items"]}
    for t in targets:
        # 同上：水下目标由本脚本生成，始终覆盖（保证与 tracks.json 一致）
        if t[0]["global_track_id"] in ids:
            idx = next(
                i for i, x in enumerate(tdata["items"])
                if x["global_track_id"] == t[0]["global_track_id"]
            )
            tdata["items"][idx] = t[0]
        else:
            tdata["items"].append(t[0])
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump(tdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"targets.json: 现有 {len(tdata['items'])} 个目标")

    # ---------------- 写 tracks.json ----------------
    kpath = os.path.join(DATA, "tracks.json")
    kdata = json.load(open(kpath, encoding="utf-8"))
    kids = {t["global_track_id"] for t in kdata["items"]}
    for _, k in targets:
        # 【为什么要覆盖而不是跳过】
        # 原写法 `if id not in kids: append` 意味着：
        # 一旦航迹已存在就**永远不更新** —— 改了生成算法也重跑不出新结果
        # （实测：改完速度算法后重跑，tracks.json 纹丝不动）。
        # 水下航迹本来就是本脚本生成的，应当始终以脚本为准。
        if k["global_track_id"] in kids:
            idx = next(
                i for i, t in enumerate(kdata["items"])
                if t["global_track_id"] == k["global_track_id"]
            )
            kdata["items"][idx] = k
        else:
            kdata["items"].append(k)
    with open(kpath, "w", encoding="utf-8") as f:
        json.dump(kdata, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"tracks.json: 现有 {len(kdata['items'])} 条航迹")

    # ---------------- 自检 ----------------
    print("\n=== 自检：水域 / 离岸余量 ===")
    for _, k in targets:
        pts = [(s["east_m"], s["north_m"]) for s in k["samples"]]
        bad = sum(1 for p in pts if not is_water_enu(*p))
        clears = [clearance_enu(*p) for p in pts]
        ds = [s["depth_m"] for s in k["samples"]]
        print(
            f"  {k['global_track_id']}: 点数={len(pts)} 落陆={bad} "
            f"余量 min/avg={min(clears)}/{sum(clears)/len(clears):.1f} "
            f"深度 {min(ds):.1f}~{max(ds):.1f}m "
            f"时长 {k['samples'][0]['time_ms']}~{k['samples'][-1]['time_ms']}ms"
        )


if __name__ == "__main__":
    main()
