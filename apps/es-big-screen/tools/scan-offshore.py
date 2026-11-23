#!/usr/bin/env python3
"""
方案：U003 整条航迹都在**堤外外海**，完全不进港池。

【为什么放弃「绕堤」】
用底图 8px 水域掩码做 BFS 绕堤实测：
  · 堤体在掩码里是一整块连续陆地，横跨 x 690~890、y 698~780，**没有堤口**；
  · 绕行路线（BFS 最短路 99 格）因 8px 栅格对角移动会擦过陆地角点，
    重采样后仍有 36/98 点落陆、38/97 段连线穿陆；
  · 绕行航程高达 1932 m，而 U003 的速度 2.6 m/s × 时间窗 282 s 只有 733 m，
    要么跑不完，要么被压慢到 0.9 m/s（物理上不像微型潜航器）。

【结论】
与其绕一个大弯（还穿水），不如让 U003 **从始至终都在外海侧**作业 ——
这也更符合真实作业逻辑：微型潜航器在防波堤外侧做水下测绘/巡检，
本来就比挤在港池里合理。

【选址约束】
  1. 全程在底图水域掩码内（落陆 = 0）；
  2. 至少部分区段落在声纳站 S01-3 的波圈内（保证探测联动）；
  3. 与 U001/U002 的 view 区域不重叠；
  4. 航程 ≈ 速度 × 时间窗（2.6 m/s × 282 s ≈ 733 m）。
"""
import base64, json, math, os, re, shutil

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

def clearance_field():
    INF = 255
    d = bytearray([INF]) * (MASK_COLS*MASK_ROWS)
    for i in range(len(d)):
        if G[i] != 1: d[i] = 0
    for y in range(MASK_ROWS):
        for x in range(MASK_COLS):
            i = y*MASK_COLS+x
            if d[i] == 0: continue
            best = d[i]
            if x>0: best=min(best,d[i-1]+1)
            if y>0: best=min(best,d[i-MASK_COLS]+1)
            if x>0 and y>0: best=min(best,d[i-MASK_COLS-1]+1.414)
            if x<MASK_COLS-1 and y>0: best=min(best,d[i-MASK_COLS+1]+1.414)
            d[i]=min(INF,int(round(best)))
    for y in range(MASK_ROWS-1,-1,-1):
        for x in range(MASK_COLS-1,-1,-1):
            i=y*MASK_COLS+x
            if d[i]==0: continue
            best=d[i]
            if x<MASK_COLS-1: best=min(best,d[i+1]+1)
            if y<MASK_ROWS-1: best=min(best,d[i+MASK_COLS]+1)
            if x<MASK_COLS-1 and y<MASK_ROWS-1: best=min(best,d[i+MASK_COLS+1]+1.414)
            if x>0 and y<MASK_ROWS-1: best=min(best,d[i+MASK_COLS-1]+1.414)
            d[i]=min(INF,int(round(best)))
    return d
CLEAR = clearance_field()
def clearance_enu(e, n):
    x,y = enu_to_px(e,n)
    cx=min(MASK_COLS-1,max(0,int(x//MASK_CELL))); cy=min(MASK_ROWS-1,max(0,int(y//MASK_CELL)))
    return CLEAR[cy*MASK_COLS+cx]

SONAR_NODES = [(0.40,0.568),(0.53,0.565),(0.66,0.557)]
SONAR_PX = [(u*MAP_W, v*MAP_H) for u,v in SONAR_NODES]
WAVE_R = MAP_W*0.17
def sonar_hit(px,py):
    return any(math.hypot(px-sx,py-sy)<=WAVE_R for sx,sy in SONAR_PX)

# ---------------------------------------------------------------- 扫描外海可用区
print("=== 外海区域（v > 0.73）水域网格图（每 20px 采样）===")
print("      ", ''.join(str((x//20)%10) for x in range(560, 1240, 20)))
for py in range(740, 900, 12):
    row = ''
    for px in range(560, 1240, 20):
        row += '#' if is_water_px(px,py) else '.'
    print(f"y={py:3d} {row}")

print()
print("=== 候选外海锚点核验 ===")
CAND = [
    # (view_u, view_v)
    (0.660, 0.740), (0.690, 0.745), (0.700, 0.760), (0.660, 0.780),
    (0.620, 0.790), (0.580, 0.800), (0.540, 0.790), (0.500, 0.780),
    (0.470, 0.770), (0.440, 0.760), (0.430, 0.780), (0.450, 0.810),
    (0.500, 0.820), (0.550, 0.830), (0.600, 0.830), (0.650, 0.820),
    (0.680, 0.790), (0.700, 0.810), (0.560, 0.760), (0.520, 0.750),
]
ok_uv = []
for u,v in CAND:
    e,n = px_to_enu_uv(u,v)
    px,py = enu_to_px(e,n)
    w = is_water_px(px,py); cl = clearance_enu(e,n); sh = sonar_hit(px,py)
    print(f"  ({u:.3f},{v:.3f}) px=({px:6.1f},{py:6.1f}) water={w!s:5} clear={cl:2d} sonar={sh!s:5}")
