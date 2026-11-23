#!/usr/bin/env python3
"""
扫描主防波堤沿线的**堤口缺口**（陆→水的过渡），找出可以合法穿越的位置。
"""
import base64, math, os, re

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

# 主防波堤的堤体折线（图片像素）
BREAK = [(883,698),(793,716),(731,729)]

print("=== 沿堤轴线（883,698)->(731,729）逐点扫描，找陆→水缺口 ===")
# 参数化
for t in [k/40 for k in range(41)]:
    # 在折线上按弧长插值
    segs = list(zip(BREAK, BREAK[1:]))
    total = sum(math.hypot(b[0]-a[0], b[1]-a[1]) for a,b in segs)
    target = t*total
    acc = 0
    px,py = BREAK[0]
    for a,b in segs:
        sl = math.hypot(b[0]-a[0], b[1]-a[1])
        if acc+sl >= target:
            f = (target-acc)/sl
            px,py = a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f
            break
        acc += sl
    # 检查垂直于堤方向的法线两侧
    print(f"  t={t:.3f} 堤轴点=({px:6.1f},{py:6.1f}) 堤上water={w(px,py)!s:5} "
          f"内侧(上,dy=-16)={w(px,py-16)!s:5} 外侧(下,dy=+16)={w(px,py+16)!s:5} "
          f"外侧(dy=+40)={w(px,py+40)!s:5} 外侧(dy=+64)={w(px,py+64)!s:5}")

print()
print("=== 堤线延长方向（向西南/东北）找端点 ===")
# 堤头在 (731,729)，往西南延长
print("从堤头(731,729)向西南方向扫描：")
for d in range(0, 140, 10):
    px = 731 - d*0.62
    py = 729 + d*0.78
    print(f"  +{d:3d}px ({px:6.1f},{py:6.1f}) water={w(px,py)!s:5} 上={w(px,py-20)!s:5} 下={w(px,py+20)!s:5}")

print()
print("=== 横向扫描 y=716 一行，找水域廊道（堤口）===")
for px in range(700, 1050, 10):
    row = ''.join('#' if w(px,py) else '.' for py in range(680, 780, 10))
    print(f"  x={px:4d}  y680..780: {row}")
