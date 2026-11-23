#!/usr/bin/env python3
"""
最终确定 U003 的堤外航迹。

【地形事实 —— 用底图水域掩码核验过】
主防波堤堤体 (883,698)→(793,716)→(731,729) 连同堤头延伸到 px≈690,780，
在掩码里是**一整块连续陆地**，横跨 x 690~890、y 698~780。
因此港池（堤内侧）与外海（堤外侧）在这张底图上是**完全隔开的**，
不存在可以「直线穿过」的堤口。

结论：U003 要到堤外，必须**绕行堤头西南端**（px≈690,780）。
已用 8px 栅格 BFS 求出一条全程在水的最短水路。

【路线】
港池东南 → 向西（堤内近岸水道）→ 西南（绕向堤头）→
绕过堤头端点 → 沿外海南侧水道向东 → 外海东南角巡弋

【声纳覆盖】
堤头点 (690,780) 距声纳站 S01-1 (614,582) = 210px，
距 S01-3 (1014,570) = 381px。
波圈半径 261px，所以堤头仍被 S01-1 覆盖 —— 探测联动照常。
"""
import base64, json, math, os, re, shutil
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "public", "water-security", "data")
MASK_COLS, MASK_ROWS, MASK_CELL = 192, 128, 8
MAP_W, MAP_H = 1536, 1024
E_MIN, E_MAX = -2600.0, 1000.0
N_MIN, N_MAX = 2900.0, 5100.0
A = MAP_W / (E_MAX - E_MIN)
D = -(1000.0 - 40.0) / (N_MAX - N_MIN)
E_OFF = 0.0 - A * E_MIN
F = 40.0 - D * N_MAX

def enu_to_px(e, n): return (A*e + E_OFF, D*n + F)
def px_to_enu(x, y):
    det = A*D; dx = x-E_OFF; dy = y-F
    return ((D*dx)/det, (A*dy)/det)
def px_to_enu_uv(u, v): return px_to_enu(u*MAP_W, v*MAP_H)
def u_from_enu(e, n):
    x, y = enu_to_px(e, n); return (x/MAP_W, y/MAP_H)

src = open(os.path.join(ROOT, "src/water-security/integration/harbor-water.ts"), encoding="utf-8").read()
i = src.find("const MASK_B64"); j = src.find(";", i)
raw = base64.b64decode("".join(re.findall(r"'([^']*)'", src[i:j])))
G = bytearray(MASK_COLS*MASK_ROWS)
for k in range(len(G)):
    G[k] = (raw[k >> 3] >> (7 - (k & 7))) & 1

def is_water_px(x, y):
    cx, cy = int(x//MASK_CELL), int(py//MASK_CELL)
    if cx<0 or cy<0 or cx>=MASK_COLS or cy>=MASK_ROWS: return False
    return G[cy*MASK_COLS+cx] == 1
def is_water_enu(e, n): return is_water_px(*enu_to_px(e, n))

SONAR_NODES = [(0.40,0.568),(0.53,0.565),(0.66,0.557)]
SONAR_PX = [(u*MAP_W, v*MAP_H) for u,v in SONAR_NODES]
WAVE_R = MAP_W * 0.17
def sonar_hit(px, py):
    return any(math.hypot(px-sx, py-sy) <= WAVE_R for sx,sy in SONAR_PX)

# ---------------------------------------------------------------- BFS 水路
def bfs_route(start_px, goal_px):
    s = (int(start_px[0]//8), int(start_px[1]//8))
    g = (int(goal_px[0]//8), int(goal_px[1]//8))
    prev = {s: None}; q = deque([s])
    while q:
        cur = q.popleft()
        if cur == g: break
        cx, cy = cur
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx,ny = cx+dx, cy+dy
            if nx<0 or ny<0 or nx>=MASK_COLS or ny>=MASK_ROWS: continue
            if (nx,ny) in prev or G[ny*MASK_COLS+nx] != 1: continue
            prev[(nx,ny)] = cur; q.append((nx,ny))
    if g not in prev: return None
    p = []; cur = g
    while cur is not None:
        p.append(cur); cur = prev[cur]
    p.reverse()
    return [((x+0.5)*8, (y+0.5)*8) for x,y in p]

# BFS 路径 → 平滑（在 8px 栅格上做简单的视界裁剪）
def smooth_px_route(pts):
    """Douglas-Peucker 简化，保留弯折点。"""
    if len(pts) < 3: return list(pts)
    def d(p, a, b):
        ax, ay = a; bx, by = b; px, py = p
        dx, dy = bx-ax, by-ay
        if dx==0 and dy==0: return math.hypot(px-ax, py-ay)
        t = max(0, min(1, ((px-ax)*dx+(py-ay)*dy)/(dx*dx+dy*dy)))
        return math.hypot(px-(ax+t*dx), py-(ay+t*dy))
    out = [pts[0]]
    stack = [(0, len(pts)-1)]
    while stack:
        s, e = stack.pop()
        if e <= s+1: continue
        mi, md = s, 0
        for k in range(s+1, e):
            dd = d(pts[k], pts[s], pts[e])
            if dd > md: mi, md = k, dd
        if md > 14:  # 14px 容差
            out.append(pts[mi]); stack.append((s, mi)); stack.append((mi, e))
    out.append(pts[-1])
    # 去重
    res = [out[0]]
    for p in out[1:]:
        if math.hypot(p[0]-res[-1][0], p[1]-res[-1][1]) > 6:
            res.append(p)
    return res

print("=== 地形核验 ===")
for (px,py,name) in [(883,698,'堤内端'),(793,716,'堤中'),(731,729,'堤头灯桩'),
                     (690,780,'堤头端点'),(596,708,'西侧水道'),(596,772,'堤西南端')]:
    print(f"  {name:8s} ({px:4d},{py:4d}) view=({px/MAP_W:.4f},{py/MAP_H:.4f}) water={is_water_px(px,py)!s:5} sonar={sonar_hit(px,py)!s:5}")

print()
print("=== BFS 求绕堤水路 ===")
# 起点：港池中部偏东南（与 U001/U002 不重叠），终点：外海东南角
route = bfs_route((830, 630), (1080, 800))
if route is None:
    raise SystemExit("BFS 未找到水路")
print(f"  栅格路径 {len(route)} 点")
smooth = smooth_px_route(route)
print(f"  平滑后 {len(smooth)} 个锚点：")
for p in smooth:
    print(f"    px=({p[0]:6.1f},{p[1]:6.1f}) view=({p[0]/MAP_W:.4f},{p[1]/MAP_H:.4f}) "
          f"water={is_water_px(*p)!s:5} sonar={sonar_hit(*p)!s:5}")

# 平滑后的折线是否全程在水？逐段检测
print()
print("=== 平滑折线逐段水域检测 ===")
for k in range(len(smooth)-1):
    a, b = smooth[k], smooth[k+1]
    steps = max(2, int(math.hypot(b[0]-a[0], b[1]-a[1])/3))
    bad = sum(1 for s in range(steps+1)
              if not is_water_px(a[0]+(b[0]-a[0])*s/steps, a[1]+(b[1]-a[1])*s/steps))
    if bad:
        print(f"  段 {k} ({a[0]:.0f},{a[1]:.0f})→({b[0]:.0f},{b[1]:.0f}): 落陆 {bad}/{steps+1} 个采样")

print()
print("=== 直接用 BFS 栅格路径（不简化），按弧长重采样 ===")
# BFS 路径在 8px 栅格上，本身全程在水；
# 但相邻栅格中心连线可能擦过陆地角点，所以每点再做一次「小范围修正」。
def nudge(px, py, radius=3):
    """若落陆，挪到 3 格内最近的水格中心。"""
    if is_water_px(px, py):
        return (px, py)
    cx, cy = int(px//8), int(py//8)
    best, bd = None, None
    for dy in range(-radius, radius+1):
        for dx in range(-radius, radius+1):
            x, y = cx+dx, cy+dy
            if x<0 or y<0 or x>=MASK_COLS or y>=MASK_ROWS: continue
            if G[y*MASK_COLS+x] != 1: continue
            d = math.hypot(dx,dy)
            if bd is None or d < bd:
                bd, best = d, ((x+0.5)*8, (y+0.5)*8)
    return best if best else (px, py)

pts = []
for p in route:
    q = nudge(*p)
    if not pts or math.hypot(q[0]-pts[-1][0], q[1]-pts[-1][1]) > 1:
        pts.append(q)

# 弧长重采样到 ~20m（1 格 ≈ 4.27m ENU，8px ≈ 37m ENU... 实测换算）
# ENU 1m = 1536/3600 px = 0.4267 px，所以 20m = 8.5px
def resample_px(pts, step_px=8.5):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.hypot(pts[i][0]-pts[i-1][0], pts[i][1]-pts[i-1][1]))
    total = cum[-1]
    n = max(2, int(total/step_px)+1)
    out, j = [], 0
    for k in range(n):
        s = total*k/(n-1)
        while j < len(cum)-2 and cum[j+1] < s: j += 1
        seg = cum[j+1]-cum[j]
        t = 0 if seg<=0 else (s-cum[j])/seg
        out.append((pts[j][0]+(pts[j+1][0]-pts[j][0])*t, pts[j][1]+(pts[j+1][1]-pts[j][1])*t))
    return out

res = resample_px(pts, 8.5)
print(f"  重采样后 {len(res)} 点")
bad = sum(1 for p in res if not is_water_px(*p))
print(f"  落陆点 = {bad}")
son = sum(1 for p in res if sonar_hit(*p))
print(f"  声纳圈内 = {son}/{len(res)} ({son/len(res)*100:.0f}%)")
# 折线段检测
segbad = 0
for k in range(len(res)-1):
    a,b = res[k], res[k+1]
    steps = max(2, int(math.hypot(b[0]-a[0], b[1]-a[1])/3))
    for s in range(steps+1):
        if not is_water_px(a[0]+(b[0]-a[0])*s/steps, a[1]+(b[1]-a[1])*s/steps):
            segbad += 1
            break
print(f"  穿陆的连线段 = {segbad}/{len(res)-1}")

# view 范围
us = [p[0]/MAP_W for p in res]; vs = [p[1]/MAP_H for p in res]
print(f"  view u {min(us):.4f}~{max(us):.4f}  v {min(vs):.4f}~{max(vs):.4f}")

# ENU 航程
enu_pts = [px_to_enu(*p) for p in res]
L = sum(math.hypot(enu_pts[i][0]-enu_pts[i-1][0], enu_pts[i][1]-enu_pts[i-1][1]) for i in range(1,len(enu_pts)))
print(f"  航程 = {L:.0f} m → 2.6 m/s 需 {L/2.6:.0f} 秒")

print()
print("=== 生成的 view 锚点（供脚本使用）===")
print("U003_UV = [")
for p in res:
    print(f"    ({p[0]/MAP_W:.4f}, {p[1]/MAP_H:.4f}),")
print("]")
