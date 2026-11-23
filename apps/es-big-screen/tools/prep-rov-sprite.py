#!/usr/bin/env python3
"""
把用户上传的潜航器（ROV）照片处理成水下场景可直接使用的贴图。

【输入】
用户上传的 1024×1024 JPG：黄色框式 ROV 俯视图（黑底）。
  · 艏向 = **图像上方**（摄像头 + 双探照灯 + 两侧导流灯）
  · 尾部 = **图像下方**（双推进器 + 液压接口）

【输出】
public/water-security/assets/sprites/rov_top.png
  · 透明背景 PNG（黑底抠除 + 边缘羽化）
  · 旋转 90°，使**艏向朝右（+x）**
  · 主体居中于正方形画布

【为什么必须旋转】
SceneUnderwaterV2 的水下目标贴图是
    <g transform="translate(x y) rotate(course)">
即**图像默认朝右（+x）**，再按实时艏向旋转。
若直接用朝上的图，潜航器会「倒着走」——船头指向尾部。

【实现要点】
用 NumPy 向量化处理，避开纯 Python 逐像素循环的滤波陷阱：
  1. 硬掩码 = 「不是背景」→ 高斯模糊 → 阈值，得到**干净的主体轮廓**；
  2. 用该轮廓求包围盒并裁剪（背景大片黑被裁掉）；
  3. alpha = 硬掩码（外扩 1px 抗锯齿）∪ 软掩码（羽化），
     避免 ROV 内部的黑色橡胶软管被误抠成透明。
"""
import os
import numpy as np
from PIL import Image, ImageFilter

SRC = ("/root/.cursor/projects/root/assets/"
       "c__Users_______AppData_Roaming_Cursor_User_workspaceStorage_"
       "23bd325825c8dd394cf1c5e6db0a7820_images_"
       "image-d76cae99-0f8e-4416-9f58-19f9d2067cb9.jpg")
OUT_DIR = "/root/es-big-screen/public/water-security/assets/sprites"
OUT = os.path.join(OUT_DIR, "rov_top.png")

os.makedirs(OUT_DIR, exist_ok=True)

im = Image.open(SRC).convert("RGB")
arr = np.asarray(im).astype(np.float32)
H, W = arr.shape[:2]
print(f"源图 {W}×{H}")

mx = arr.max(axis=2)
mn = arr.min(axis=2)
light = (mx + mn) / 2
sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)

# 背景判定：纯黑。
#
# 【为什么不能只按「亮度低」判】
# ROV 上有**深蓝色液压软管**（实测 RGB 约 (20,30,45)），
# 亮度同样很低，但它是主体的一部分，必须保留。
# 纯黑背景的三个通道都接近 0 且彼此相等；
# 深蓝软管的蓝通道明显大于红通道 → 用「色度中性度」区分。
chroma = mx - mn
is_bg = (mx < 34) & (chroma < 14)

# 硬掩码 = 「不是背景」→ 高斯模糊 → 阈值，得到**干净的主体轮廓**
hard = (~is_bg).astype(np.float32) * 255
hard_img = Image.fromarray(hard.astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius=2.0))
hard_arr = np.asarray(hard_img).astype(np.float32)

# 阈值化得到干净轮廓（去掉孤立噪点）
solid = (hard_arr > 110).astype(np.uint8) * 255

# 闭运算（膨胀后腐蚀）填掉主体内部的空洞（黑色软管等）
dil = Image.fromarray(solid).filter(ImageFilter.MaxFilter(size=7))
solid = np.asarray(dil.filter(ImageFilter.MinFilter(size=7))).copy()

print(f"硬掩码前景像素 {int((solid>0).sum())} / {W*H}")

# ---------------------------------------------------------------- 2. 包围盒 + 裁剪
ys, xs = np.nonzero(solid)
if len(xs) == 0:
    raise SystemExit("未检出主体")
x0, x1 = int(xs.min()), int(xs.max())
y0, y1 = int(ys.min()), int(ys.max())
print(f"主体包围盒 x[{x0},{x1}] y[{y0},{y1}]  尺寸 {x1-x0+1}×{y1-y0+1}")

pad = 6
x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
x1 = min(W, x1 + pad); y1 = min(H, y1 + pad)

rgba = Image.open(SRC).convert("RGBA").crop((x0, y0, x1 + 1, y1 + 1))
soft_local = solid[y0:y1 + 1, x0:x1 + 1].copy()

# 软掩码：硬掩码的高斯模糊版本（边缘羽化）
soft_img = Image.fromarray(soft_local).filter(ImageFilter.GaussianBlur(radius=1.5))
soft_local = np.asarray(soft_img).astype(np.float32)

# alpha = 软掩码 与 「非背景」原始信号的联合 —— 保证主体内部全不透明
not_bg = (~is_bg[y0:y1 + 1, x0:x1 + 1]).astype(np.float32) * 255
not_bg_img = Image.fromarray(not_bg.astype(np.uint8)).filter(ImageFilter.MaxFilter(size=5))
not_bg_local = np.asarray(not_bg_img).astype(np.float32)

alpha = np.maximum(soft_local, np.minimum(not_bg_local, 255))
# 外部一圈彻底透明
alpha[0, :] = alpha[-1, :] = alpha[:, 0] = alpha[:, -1] = 0

a_img = Image.fromarray(alpha.astype(np.uint8))
rgba.putalpha(a_img)
print(f"裁剪后 {rgba.size}  alpha: 透明 {int((alpha<8).sum())} / 半透明 {int(((alpha>=8)&(alpha<250)).sum())} / 不透明 {int((alpha>=250).sum())}")

# ---------------------------------------------------------------- 3. 旋转：艏向朝右
rot = rgba.rotate(-90, expand=True, resample=Image.BICUBIC)
print(f"旋转后 {rot.size}")

# ---------------------------------------------------------------- 3b. 去白边
#
# 【为什么要去】
# 源 JPG 在主体周围有一圈**浅灰/白色描边**（原图的柔光/晕影），
# 抠底后这圈白边会残留在透明区域边缘，
# 在深蓝色水下背景上就是一圈刺眼的白晕。
#
# 做法：把边缘 1~2 像素的 alpha 向内收（腐蚀），
# 让白边随主体一起被裁掉。
rot_a = np.asarray(rot).astype(np.float32)
rot_a[:, :, 3] = np.minimum(
    rot_a[:, :, 3],
    np.minimum(
        np.minimum(
            np.roll(rot_a[:, :, 3], 1, axis=0),
            np.roll(rot_a[:, :, 3], -1, axis=0),
        ),
        np.minimum(
            np.roll(rot_a[:, :, 3], 1, axis=1),
            np.roll(rot_a[:, :, 3], -1, axis=1),
        ),
    ),
)
rot = Image.fromarray(rot_a.astype(np.uint8), "RGBA")

# 白边去不掉的残余：把半透明边缘像素的 RGB 向「近黑」拉，
# 让任何残留都变成与背景同色而不是白边
ra = np.asarray(rot).astype(np.float32)
edge = (ra[:, :, 3] > 8) & (ra[:, :, 3] < 240)
if edge.any():
    for c in range(3):
        ch = ra[:, :, c]
        ch[edge] = np.minimum(ch[edge], 70)
    ra[:, :, 3][edge] = 0
rot = Image.fromarray(ra.astype(np.uint8), "RGBA")
print(f"去白边：处理半透明边缘像素 {int(edge.sum())}")

# ---------------------------------------------------------------- 4. 正方形画布居中
w, h = rot.size
side = max(w, h)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(rot, ((side - w) // 2, (side - h) // 2), rot)
print(f"画布 {canvas.size}")

canvas.save(OUT, "PNG")
print(f"\n已输出：{OUT}")

# 预览（深色底，方便肉眼确认）
prev = Image.new("RGB", canvas.size, (6, 24, 40))
prev.paste(canvas, (0, 0), canvas)
prev_path = os.path.join("/tmp", "rov_preview.png")
prev.save(prev_path)
print(f"预览：{prev_path}")
