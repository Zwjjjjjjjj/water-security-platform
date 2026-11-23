"""
重建港区水域掩码（campus_oblique_v3.png）。

【为什么需要重建】
旧掩码是用连通域提取法从底图里抠出「主水域」，
结果把**核电站厂区内部的一片凹地**误判成了水域，
于是 T002 这类航迹会直接从厂房里穿过去，船只也会「游进」厂区。

【新做法】
1. 按颜色特征提取海面：
   海 = 蓝色主导 (B>G>=R) 且亮度适中，并排除高亮建筑；
2. 取**最大连通域**（真正的大海），丢掉天空/山影等小碎块；
3. `binary_fill_holes` 填掉被堤坝包围的港池内孔洞；
4. `binary_closing` 补上被细堤/栈桥切断的连通；
5. **腐蚀 49px** —— 让船中心离岸留出约 6 个掩码格（≈48 米）的余量，
   避免船只「贴着岸边」；
6. 降采样到 192×128（每格 8×8 原图像素），packbits(big) + base64。

【输出】
- stdout: base64 字符串（可直接粘进 harbor-water.ts）
- /tmp/water_raw.npy: 腐蚀前的二值图（可视化用）
- /tmp/mask_new.npy: 192×128 的 0/1 网格

用法：
    python3 tools/gen-water-mask.py
"""

import base64
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = os.path.join(
    ROOT, "public/water-security/assets/scenes-v2/terrain/campus_oblique_v3.png"
)

CELL = 8  # 每个掩码格对应的底图像素边长
ERODE_PX = 49  # 腐蚀半径（奇数），49/8 ≈ 6.1 个掩码格 ≈ 48 m 离岸余量


def build_water_mask(bg_path: str) -> np.ndarray:
    """返回腐蚀+降采样后的 192×128 掩码网格（1=可航水域）。"""
    im = Image.open(bg_path).convert("RGB")
    a = np.array(im).astype(np.float32)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]

    # --- 1) 海面颜色特征 ---
    # 海水：蓝色主导、亮度适中
    sea = (b > g) & (g >= r) & (b > 40) & (r < 90)
    # 排除高亮建筑（白/浅灰）
    sea &= ~((r > 140) & (g > 140) & (b > 140))

    # --- 2) 只保留最大的连通域 ---
    lab, n = ndimage.label(sea)
    if n == 0:
        raise RuntimeError("未找到海面区域")
    sizes = ndimage.sum(sea, lab, range(1, n + 1))
    main = int(np.argmax(sizes)) + 1
    water = lab == main

    # --- 3) 填孔：堤坝围出的港池内部不应被当成陆地 ---
    water = ndimage.binary_fill_holes(water)

    # --- 4) 闭运算：补回被细堤/栈桥切断的连通 ---
    water = ndimage.binary_closing(water, structure=np.ones((9, 9)))

    # --- 5) 腐蚀：保证船只离岸有余量 ---
    eroded = ndimage.binary_erosion(water, structure=np.ones((ERODE_PX, ERODE_PX)))
    np.save("/tmp/water_raw.npy", eroded)

    # --- 6) 降采样 ---
    h, w = eroded.shape
    rows, cols = h // CELL, w // CELL
    grid = eroded[: rows * CELL, : cols * CELL].reshape(rows, CELL, cols, CELL).mean(axis=(1, 3))
    return (grid > 0.5).astype(np.uint8)


def encode(mask: np.ndarray) -> str:
    """0/1 网格 → packbits(big) → base64。"""
    packed = np.packbits(mask.flatten(), bitorder="big")
    return base64.b64encode(packed.tobytes()).decode()


def verify(mask: np.ndarray, b64: str) -> bool:
    """回读校验：base64 解码后必须与原网格完全一致。"""
    raw = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
    back = np.unpackbits(raw, bitorder="big")[: mask.size].reshape(mask.shape)
    return bool(np.array_equal(mask, back))


def main() -> int:
    if not os.path.exists(BG):
        print(f"找不到底图：{BG}", file=sys.stderr)
        return 1

    mask = build_water_mask(BG)
    b64 = encode(mask)
    np.save("/tmp/mask_new.npy", mask)

    ok = verify(mask, b64)
    print(f"网格 {mask.shape}  水格 {int(mask.sum())}  base64 长度 {len(b64)}  回读一致 {ok}")
    if not ok:
        return 1
    print(b64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
