#!/usr/bin/env python3
"""水上模式：场景标注文字放大。
chip（主要设施标注）12 → 15，plain（区域名/次要标注）11 → 14。
"""
path = 'src/water-security/integration/scenes.config.v2.ts'
src = open(path, encoding='utf-8').read()

old = """export const OBLIQUE_SCENE_LABELS: ObliqueSceneLabel[] = [
  // —— 厂区主标注：贴在厂区建筑群上方 ——
  { text: '工业港湾厂区（示意）', px: 770, py: 300, style: 'chip', size: 12, priority: 0, offsetY: -18 },
  // —— 取排水口：位于厂区临海侧的岸线附近 ——
  { text: '取水口', px: 1027, py: 408, style: 'chip', size: 12, priority: 1, offsetX: 44 },
  { text: '排放口', px: 1369, py: 281, style: 'chip', size: 12, priority: 1, offsetX: 30 },
  // —— 关键构筑物（锚点即实测连通域中心）——
  { text: '罐区', px: 1113, py: 421, style: 'plain', size: 11, opacity: 0.9, priority: 2, offsetX: 52, offsetY: -6 },
  { text: '主厂房', px: 660, py: 400, style: 'plain', size: 11, opacity: 0.85, priority: 2, offsetX: -54, offsetY: 4 },
  { text: '开关站', px: 627, py: 289, style: 'plain', size: 11, opacity: 0.85, priority: 2, offsetX: -50, offsetY: -6 },
  { text: '厂区码头', px: 1092, py: 623, style: 'plain', size: 11, opacity: 0.8, priority: 2, offsetX: 40, offsetY: 10 },
  // —— 区域名（最弱层）——
  { text: '北侧林地', px: 276, py: 334, style: 'plain', size: 11, opacity: 0.5, priority: 3 },
  { text: '厂前水域', px: 700, py: 850, style: 'plain', size: 11, opacity: 0.5, priority: 3 },
  { text: '外海域', px: 1400, py: 900, style: 'plain', size: 11, opacity: 0.45, priority: 3 },
  { text: '进港航路', px: 1220, py: 760, style: 'plain', size: 11, opacity: 0.45, priority: 3 },
]"""

new = """/**
 * 【字号说明】
 * 用户要求「水上模式的那些字需要放大」。
 *   · chip（设施标注：厂区、取水口、排放口）12 → 15px
 *   · plain（构筑物与区域名）11 → 14px
 *
 * 字号放大后，标签的**占位也随之变大**，
 * 因此偏移量（offsetX/offsetY）同步放大，
 * 避免文字压在被标注的设施本体上。
 */
export const OBLIQUE_SCENE_LABELS: ObliqueSceneLabel[] = [
  // —— 厂区主标注：贴在厂区建筑群上方 ——
  { text: '工业港湾厂区（示意）', px: 770, py: 300, style: 'chip', size: 15, priority: 0, offsetY: -24 },
  // —— 取排水口：位于厂区临海侧的岸线附近 ——
  { text: '取水口', px: 1027, py: 408, style: 'chip', size: 15, priority: 1, offsetX: 52 },
  { text: '排放口', px: 1369, py: 281, style: 'chip', size: 15, priority: 1, offsetX: 36 },
  // —— 关键构筑物（锚点即实测连通域中心）——
  { text: '罐区', px: 1113, py: 421, style: 'plain', size: 14, opacity: 0.9, priority: 2, offsetX: 58, offsetY: -7 },
  { text: '主厂房', px: 660, py: 400, style: 'plain', size: 14, opacity: 0.85, priority: 2, offsetX: -62, offsetY: 5 },
  { text: '开关站', px: 627, py: 289, style: 'plain', size: 14, opacity: 0.85, priority: 2, offsetX: -58, offsetY: -7 },
  { text: '厂区码头', px: 1092, py: 623, style: 'plain', size: 14, opacity: 0.8, priority: 2, offsetX: 46, offsetY: 12 },
  // —— 区域名（最弱层）——
  { text: '北侧林地', px: 276, py: 334, style: 'plain', size: 14, opacity: 0.5, priority: 3 },
  { text: '厂前水域', px: 700, py: 850, style: 'plain', size: 14, opacity: 0.5, priority: 3 },
  { text: '外海域', px: 1400, py: 900, style: 'plain', size: 14, opacity: 0.45, priority: 3 },
  { text: '进港航路', px: 1220, py: 760, style: 'plain', size: 14, opacity: 0.45, priority: 3 },
]"""

assert old in src, "OBLIQUE_SCENE_LABELS not found"
src = src.replace(old, new, 1)
open(path, 'w', encoding='utf-8').write(src)
print("scene labels enlarged")
