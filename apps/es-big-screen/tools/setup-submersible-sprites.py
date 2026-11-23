#!/usr/bin/env python3
"""
为水下潜航器建立**独立的素材槽位**（不再复用蛙人贴图）。

【背景】
三个水下目标原本全部用 `assets/sprites/diver.png`（蛙人），
导致「无人潜航器 UUV」和「微型潜航器」也是一个人形 —— 明显不对。

【本脚本做什么】
1. 在 `public/water-security/assets/sprites/` 下生成两个**矢量占位素材**：
   · `uuv.svg`      —— 无人潜航器（鱼雷形，尾推 + 导流罩）
   · `midget_sub.svg` —— 微型潜航器（小胖鱼形，十字尾 + 探照灯）
   占位素材是纯 SVG，**任何尺寸/主题都能正常渲染**，等用户上传 PNG 后直接替换。
2. 更新 `targets.json`：
   · U001（蛙人）→ 继续用 `assets/sprites/diver.png`
   · U002（无人潜航器）→ `assets/sprites/uuv.svg`
   · U003（微型潜航器）→ `assets/sprites/midget_sub.svg`
3. 打印一份「素材替换说明」，告诉用户把 PNG 放到哪个路径即可无缝替换。

【为什么占位素材做成 SVG 而不是直接留空】
留空的话，SceneUnderwaterV2 的 `<image v-if="t.sprite">` 会整块不渲染，
目标只剩一个色块箭头 —— 演示效果直接崩。
SVG 占位保证任何时候都有可辨认的「潜航器」外形，
用户上传 PNG 后只需改 targets.json 里的路径（或直接覆盖同名文件）。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "public", "water-security", "data")
SPRITES = os.path.join(ROOT, "public", "water-security", "assets", "sprites")

os.makedirs(SPRITES, exist_ok=True)

# ------------------------------------------------------------------ #
# 矢量占位素材
# ------------------------------------------------------------------ #
UUV_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<!--
  无人潜航器（UUV）占位素材 · 鱼雷/鱼形回体
  ------------------------------------------------------------------
  【用途】水下目标 U002 的贴图。
  【替换方式】把同名 PNG（建议 1536×1024 透明底）放到
              public/water-security/assets/sprites/uuv.png，
              然后把 targets.json 里 U002 的 sprite 改成
              "assets/sprites/uuv.png" 即可无缝替换。
  【朝向】默认朝右（+x），SceneUnderwaterV2 会按艏向旋转它。
-->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 150" width="300" height="150">
  <defs>
    <linearGradient id="uuvBody" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#8fd8ef"/>
      <stop offset="45%" stop-color="#2e9dc4"/>
      <stop offset="100%" stop-color="#12556f"/>
    </linearGradient>
    <radialGradient id="uuvNose" cx="0.75" cy="0.5" r="0.5">
      <stop offset="0%" stop-color="#d8f6ff" stop-opacity="0.85"/>
      <stop offset="100%" stop-color="#d8f6ff" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- 尾推舵（十字翼） -->
  <g fill="#1b7ea3" stroke="#9fe3f7" stroke-width="2.5">
    <path d="M 46 58 L 16 40 L 16 78 L 46 84 Z"/>
    <path d="M 46 92 L 16 110 L 16 72 L 46 66 Z"/>
    <path d="M 46 66 L 24 30 L 24 66 L 46 78 Z" opacity="0.75"/>
  </g>

  <!-- 主体（鱼雷形） -->
  <path d="M 44 75
           C 44 50, 96 36, 158 36
           C 222 36, 272 54, 282 75
           C 272 96, 222 114, 158 114
           C 96 114, 44 100, 44 75 Z"
        fill="url(#uuvBody)" stroke="#a9e9fb" stroke-width="3"/>

  <!-- 艏部导流罩高光 -->
  <ellipse cx="252" cy="75" rx="34" ry="26" fill="url(#uuvNose)"/>

  <!-- 观察窗 / 传感器 -->
  <ellipse cx="216" cy="70" rx="17" ry="13" fill="#04283a" stroke="#7fe3ff" stroke-width="2.5"/>
  <ellipse cx="212" cy="67" rx="6" ry="5" fill="#7fe3ff" opacity="0.8"/>

  <!-- 侧向声呐天线 -->
  <path d="M 150 40 L 150 24 L 168 24" fill="none" stroke="#9fe3f7" stroke-width="3" stroke-linecap="round"/>

  <!-- 尾部螺旋桨 -->
  <g stroke="#c9f2ff" stroke-width="3" fill="none" opacity="0.9">
    <path d="M 44 62 A 16 16 0 0 1 44 88"/>
    <path d="M 38 75 L 52 75"/>
  </g>
</svg>
"""

MIDGET_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<!--
  微型潜航器（Midget Submarine）占位素材 · 胖鱼形
  ------------------------------------------------------------------
  【用途】水下目标 U003（现驻防波堤外围外海）的贴图。
  【替换方式】把同名 PNG（建议 1536×1024 透明底）放到
              public/water-security/assets/sprites/midget_sub.png，
              然后把 targets.json 里 U003 的 sprite 改成
              "assets/sprites/midget_sub.png" 即可无缝替换。
  【朝向】默认朝右（+x），SceneUnderwaterV2 会按艏向旋转它。
-->
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 150" width="300" height="150">
  <defs>
    <linearGradient id="msBody" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#ffd97a"/>
      <stop offset="45%" stop-color="#d98f2b"/>
      <stop offset="100%" stop-color="#7a4a10"/>
    </linearGradient>
    <radialGradient id="msGlass" cx="0.5" cy="0.4" r="0.6">
      <stop offset="0%" stop-color="#bff3ff" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="#1b6f8c" stop-opacity="0.9"/>
    </radialGradient>
  </defs>

  <!-- 尾部十字舵 -->
  <g fill="#b06f1c" stroke="#ffdca0" stroke-width="2.5">
    <path d="M 56 58 L 20 36 L 20 80 L 56 86 Z"/>
    <path d="M 56 92 L 20 114 L 20 70 L 56 64 Z"/>
  </g>

  <!-- 主体 -->
  <path d="M 52 75
           C 52 46, 104 30, 168 30
           C 232 30, 278 50, 286 75
           C 278 100, 232 120, 168 120
           C 104 120, 52 104, 52 75 Z"
        fill="url(#msBody)" stroke="#ffdfa6" stroke-width="3"/>

  <!-- 观察窗 -->
  <ellipse cx="236" cy="70" rx="26" ry="21" fill="url(#msGlass)" stroke="#ffe0ab" stroke-width="3"/>
  <ellipse cx="230" cy="64" rx="9" ry="7" fill="#eafcff" opacity="0.85"/>

  <!-- 艏部探照灯 -->
  <circle cx="278" cy="75" r="9" fill="#fff6d8" stroke="#e8b551" stroke-width="2.5"/>
  <path d="M 288 66 L 300 52 L 300 98 L 288 84 Z" fill="#fff3c4" opacity="0.32"/>

  <!-- 顶部潜望镜 -->
  <path d="M 150 32 L 150 14 L 176 14" fill="none" stroke="#ffdfa6" stroke-width="3.5" stroke-linecap="round"/>

  <!-- 侧向推进器舱 -->
  <ellipse cx="106" cy="104" rx="26" ry="11" fill="#8a5413" stroke="#ffdca0" stroke-width="2.5"/>
  <circle cx="106" cy="104" r="5" fill="#ffdfa6" opacity="0.85"/>
</svg>
"""

for name, content in (("uuv.svg", UUV_SVG), ("midget_sub.svg", MIDGET_SVG)):
    path = os.path.join(SPRITES, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"写入占位素材：assets/sprites/{name}  ({len(content)} 字节)")

# ------------------------------------------------------------------ #
# 更新 targets.json
# ------------------------------------------------------------------ #
# 【素材优先级】
# 实拍/实绘的 PNG 优先；没有 PNG 时回退到矢量占位。
# 这样用户上传 PNG 后无需再改代码 —— 重跑本脚本即可生效。
ROV_PNG = "assets/sprites/rov_top.png"
has_rov = os.path.exists(os.path.join(ROOT, "public", "water-security", ROV_PNG))

if has_rov:
    U002_SPRITE = ROV_PNG
    U003_SPRITE = ROV_PNG
    print(f"\n检测到用户上传的潜航器素材 → {ROV_PNG}，U002/U003 均使用该素材")
else:
    U002_SPRITE = "assets/sprites/uuv.svg"
    U003_SPRITE = "assets/sprites/midget_sub.svg"
    print("\n未检测到潜航器 PNG，回退到矢量占位素材")

SPRITE_MAP = {
    "U001": ("assets/sprites/diver.png", 170),          # 蛙人 —— 保留原素材
    "U002": (U002_SPRITE, 210),                        # 无人潜航器
    "U003": (U003_SPRITE, 150),                        # 微型潜航器
}

tpath = os.path.join(DATA, "targets.json")
tdata = json.load(open(tpath, encoding="utf-8"))
changed = []
for item in tdata["items"]:
    tid = item.get("global_track_id")
    if tid in SPRITE_MAP:
        sprite, w = SPRITE_MAP[tid]
        if item.get("sprite") != sprite or item.get("sprite_width_design_px") != w:
            item["sprite"] = sprite
            item["sprite_width_design_px"] = w
            changed.append(tid)

with open(tpath, "w", encoding="utf-8") as f:
    json.dump(tdata, f, ensure_ascii=False, indent=2)
    f.write("\n")

print()
print("=== targets.json 贴图分配 ===")
for item in tdata["items"]:
    tid = item.get("global_track_id", "")
    if tid.startswith("U"):
        print(f"  {tid}  {item.get('target_type',''):22s} → {item.get('sprite')}")
print(f"  （本次改动：{changed or '无'}）")

# ------------------------------------------------------------------ #
# 素材替换说明
# ------------------------------------------------------------------ #
print()
print("=" * 68)
print("素材替换说明")
print("=" * 68)
print("""
【素材库现状】已核查 public/water-security/assets/ 全部子目录，
  只有蛙人素材（sprites/diver.png、icons/diver.svg、icons/diver.png），
  **没有任何潜航器/潜航艇素材**。

【当前处理】
用户已上传潜航器素材（黄色框式 ROV 俯视图，黑底 1024×1024），
经 tools/prep-rov-sprite.py 处理为透明 PNG 并旋转到艏向朝右：
  · assets/sprites/rov_top.png  → U002 无人潜航器 + U003 微型潜航器
同时保留两个矢量占位素材作为兜底：
  · assets/sprites/uuv.svg
  · assets/sprites/midget_sub.svg

【如需替换为其他 PNG 素材】
  1. 把 PNG 放到  public/water-security/assets/sprites/
  2. 文件名建议：rov_top.png（或任意名字，改 targets.json 里的 sprite 字段）
  3. 修改 public/water-security/data/targets.json：
       U002 → "assets/sprites/<你的文件>.png"
       U003 → "assets/sprites/<你的文件>.png"

【贴图规格要求】
  · 透明背景 PNG
  · 分辨率 1024×1024 左右（正方形，便于等比缩放）
  · 主体**朝向画面右方（+x）** —— SceneUnderwaterV2 会按实时艏向旋转它
  · 若素材是「艏朝上」，需先顺时针旋转 90°
    （tools/prep-rov-sprite.py 已自动处理）
  · U002（UUV）：细长鱼雷形，长径比约 3:1
  · U003（微型潜航器）：胖鱼形，长径比约 2:1，带尾舵与探照灯

【如需自动切换】
  把 PNG 放进 sprites/ 目录后运行：
      python3 tools/setup-submersible-sprites.py
  该脚本会重新分配 targets.json 里的贴图。
""")
