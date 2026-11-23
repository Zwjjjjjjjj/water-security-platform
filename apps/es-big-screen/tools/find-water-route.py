#!/usr/bin/env python3
"""精确找出主防波堤堤头（西南端）端点，以及绕行路径上全程在水面的廊道。"""
import base64, math, os, re
from collections import deque

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASK_COLS, MASK_ROWS, MASK_CELL = 192, 128, 8
MAP_W, MAP_H = 1536, 1024
src = open(os.path.join(ROOT, "src/water-security/integration/harbor-water.ts"), encoding="utf-8").read()
i = src.find("const MASK_B64"); j = src.find(";", i)
raw = base64.b64decode("".join(re.findall(r"'([^']*)'", src[i:j])))
G = bytearray(MASK_COLS*MASK_ROWS)
for k in range(len(G)):
    G[k] = (raw[k >> 3] >> (7 - (k & 7))) & 1

def w(px, py):
    cx, cy = int(px//MASK_CELL), int(py//MASK_CELL)
    if cx<0 or cy<0 or cx>=MASK_COLS or cy>=MASK_ROWS: return False
    return G[cy*MASK_COLS+cx] == 1

print("=== 堤头端点精确化：沿堤轴线西南延伸，5px 步进 ===")
for d in range(60, 130, 5):
    px = 731 - d*0.62
    py = 729 + d*0.78
    print(f"  +{d:3d}px ({px:6.1f},{py:6.1f}) water={w(px,py)!s:5} "
          f"格=({int(px//8)},{int(py//8)})")

print()
print("=== BFS 水路：港池(798,614) → 外海(1060,819) ===")
start = (int(798//8), int(614//8))
goal  = (int(1060//8), int(819//8))
prev = {start: None}
q = deque([start])
found = False
while q:
    cur = q.popleft()
    if cur == goal:
        found = True; break
    cx, cy = cur
    for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
        nx, ny = cx+dx, cy+dy
        if nx<0 or ny<0 or nx>=MASK_COLS or ny>=MASK_ROWS: continue
        if (nx,ny) in prev: continue
        if G[ny*MASK_COLS+nx] != 1: continue
        prev[(nx,ny)] = cur
        q.append((nx,ny))
print("BFS 找到路径:", found)
if found:
    path = []
    cur = goal
    while cur is not None:
        path.append(cur); cur = prev[cur]
    path.reverse()
    pts = [((x+0.5)*8, (y+0.5)*8) for x,y in path]
    print(f"路径长度 {len(pts)} 格")
    key = [pts[0]]
    for k in range(1, len(pts)-1):
        ax, ay = pts[k][0]-pts[k-1][0], pts[k][1]-pts[k-1][1]
        bx, by = pts[k+1][0]-pts[k][0], pts[k+1][1]-pts[k][1]
        if abs(ax*by-ay*bx) > 1e-6:
            key.append(pts[k])
    key.append(pts[-1])
    print("转折点:")
    for p in key:
        print(f"   px=({p[0]:6.1f},{p[1]:6.1f})  view=({p[0]/MAP_W:.4f},{p[1]/MAP_H:.4f})")
    print()
    print("=== 抽稀后的 view 锚点（每 10 格取一个）===")
    for p in pts[::10] + [pts[-1]]:
        print(f"   ({p[0]/MAP_W:.4f}, {p[1]/MAP_H:.4f}),")
