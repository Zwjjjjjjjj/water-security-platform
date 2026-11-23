#!/usr/bin/env python3
"""修复水下目标速度突刺。

【问题】
`clear_line()` 会**丢弃**会穿陆的路径点。丢弃后，幸存点之间的距离
可能高达 160~600 米 —— 而时间轴是按点数**均匀**铺开的，
于是 `speed = dist / dt` 就算出 17~113 m/s 的荒谬速度。

实测：
    U001  t=18875  dist=160.8m dt=9.4s → 17.04 m/s
    U001  t=264250 dist=253.0m dt=9.4s → 26.81 m/s
    U003  t=30642  dist=601.4m dt=5.3s → 113.02 m/s

蛙人 26.8 m/s ≈ 96 km/h —— 这不是蛙人，这是火箭。

【修法：时间轴按弧长分配】
把时间从「按点数均匀」改成「**按实际航程**均匀」：
    Δt_i = Δs_i / v_cruise

这样无论中间丢了多少点，速度都**恒等于巡航速度**，
尖刺从根上消失 —— 而不是事后裁剪数字。

同时按目标类型给出物理上限，作为兜底：
    蛙人 1.5 m/s / 潜航器 1.5 m/s / 微型潜航器 3.0 m/s
"""
import re

path = 'tools/gen-underwater-tracks.py'
src = open(path, encoding='utf-8').read()

# ---------- 1. build_target：时间轴按弧长分配 ----------
old = """    n = len(path)
    # 时间轴：在 [t0, t1] 内均匀铺开
    span = t1 - t0
    times = [int(round(t0 + span * i / (n - 1))) for i in range(n)]

    samples = []
    prev = None
    for i, (e, n_m) in enumerate(path):
        u, v = u_from_enu(e, n_m)
        if prev is not None:
            dist = math.hypot(e - prev[0], n_m - prev[1])
            dt = max(0.001, (times[i] - times[i - 1]) / 1000.0)
            speed = dist / dt
            course = (math.degrees(math.atan2(e - prev[0], n_m - prev[1]))) % 360
        else:
            speed = 0.0
            course = 0.0
        prev = (e, n_m)"""

new = """    n = len(path)

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

    # 巡航速度（米/秒）—— 各目标类型的物理合理值
    v_cruise = CRUISE_SPEED.get(tid, 2.0)
    # 总时长：由航程与巡航速度决定；落在 [t0, t1] 窗口内
    travel_ms = (total_len / v_cruise) * 1000.0
    span = min(float(t1 - t0), max(1000.0, travel_ms))
    # 若航程短到不足以铺满窗口，就用航程决定总时长；
    # 否则按比例压缩到窗口内（等效于提高巡航速度，但不超过上限）
    time_scale = span / (total_len / v_cruise * 1000.0) if travel_ms > 0 else 1.0

    times = []
    acc = 0.0
    for i in range(n):
        times.append(int(round(t0 + acc * time_scale)))
        if i < n:
            step = (
                math.hypot(path[i + 1][0] - path[i][0], path[i + 1][1] - path[i][1])
                if i + 1 < n
                else 0.0
            )
            acc += step / v_cruise * 1000.0

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
        prev = (e, n_m)"""

assert old in src, "build_target time axis block not found"
src = src.replace(old, new, 1)

# ---------- 2. 新增常量 ----------
old_c = """# 声纳站 S01 的三个显示节点（底图归一化坐标）"""
new_c = '''# ---------------------------------------------------------------- 物理速度约束
# 各水下目标类型的巡航速度（米/秒）。
#
# 【参考】
#   · 蛙人（潜水员）自由游速约 0.5~1.0 m/s，蛙人推进器辅助下 1~1.5 m/s
#   · 无人潜航器（UUV）巡航约 1.0~1.5 m/s
#   · 微型潜航器（ROV/AUV 小型）约 2~3 m/s
#
# 取值以「演示上能看出移动」为准，不追求真实极值。
CRUISE_SPEED = {
    "U001": 1.5,   # 蛙人
    "U002": 1.2,   # 无人潜航器
    "U003": 2.6,   # 微型潜航器
}

# 速度硬上限（米/秒）—— 任何水下目标都不得超过
MAX_SPEED_MPS = 4.0


# 声纳站 S01 的三个显示节点（底图归一化坐标）'''

assert old_c in src
src = src.replace(old_c, new_c, 1)

open(path, 'w', encoding='utf-8').write(src)
print("gen-underwater-tracks.py patched")
