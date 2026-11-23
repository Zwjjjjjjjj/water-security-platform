#!/usr/bin/env python3
"""升级 foamAlong：生成符合物理规律的破碎浪花（涌动 + 回落 + 二次破碎）。"""
path = 'src/water-security/integration/water-effects.ts'
src = open(path, encoding='utf-8').read()

old_start = """/**
 * 沿一条折线生成破碎泡沫团。
 *
 * 【为什么是「团」而不是「线」】
 * 真实的防波堤前浪是**一段一段破碎的白沫**，
 * 不是一条均匀的白边。用大小不一的椭圆团拼出断续感，
 * 比描边真实得多。
 *
 * @param line     岸线/堤线（图片像素）
 * @param outward  泡沫朝向岸线外侧的单位法线（可为 null，自动判断）
 * @param count    泡沫团数量
 * @param seed     随机种子（保证每次渲染结果一致）
 */
export function foamAlong(
  line: PxPolyline,
  outward: [number, number] | null,
  count: number,
  seed = 1,
): FoamPatch[] {
  if (line.length < 2 || count <= 0) return []
  const out: FoamPatch[] = []
  let s = seed * 9301 + 49297
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280
    return s / 233280
  }
  for (let i = 0; i < count; i++) {
    /* 在折线上均匀取点 */
    const t = (i + rnd() * 0.8) / count
    const seg = Math.min(line.length - 2, Math.floor(t * (line.length - 1)))
    const local = t * (line.length - 1) - seg
    const [x0, y0] = line[seg]
    const [x1, y1] = line[seg + 1]
    const px = x0 + (x1 - x0) * local
    const py = y0 + (y1 - y0) * local

    /* 法线方向（外侧） */
    const dx = x1 - x0
    const dy = y1 - y0
    const len = Math.hypot(dx, dy) || 1
    let nx = -dy / len
    let ny = dx / len
    if (outward) {
      /* 让法线与给定方向同向 */
      if (nx * outward[0] + ny * outward[1] < 0) {
        nx = -nx
        ny = -ny
      }
    }
    /* 泡沫团略微离开岸线，并带一点散布 */
    const off = 2 + rnd() * 9
    const spread = (rnd() - 0.5) * 6
    out.push({
      cx: px + nx * (off + spread),
      cy: py + ny * (off + spread),
      r: 2.5 + rnd() * 6.5,
      opacity: 0.22 + rnd() * 0.4,
      phase: rnd() * Math.PI * 2,
    })
  }
  return out
}"""

new = '''/**
 * 沿一条折线生成破碎泡沫团。
 *
 * 【为什么是「团」而不是「线」】
 * 真实的防波堤前浪是**一段一段破碎的白沫**，
 * 不是一条均匀的白边。用大小不一的椭圆团拼出断续感，
 * 比描边真实得多。
 *
 * 【每一团的物理行为 —— 本次改动的核心】
 * 真实的破浪是一个**周期过程**，不是静态亮斑：
 *   1. **涌浪逼近**：波峰前进到堤前，海面抬起；
 *   2. **破碎**：波峰失稳，白沫爆发（最亮、最大）；
 *   3. **回落**：白水沿堤面回流，尺寸缩小、亮度下降；
 *   4. **二次破碎**：回流途中撞上堤体，产生二次白沫（更小、更暗）。
 *
 * 因此每团泡沫携带自己的 `phase`（在周期中的相位）、
 * `surge`（涌动幅度，沿法线方向的最大位移）、
 * `burst`（破碎强度峰值）与 `period`（周期秒数），
 * 由渲染层按时间采样出「推进 → 爆发 → 回落」的真实节奏。
 *
 * @param line     岸线/堤线（图片像素）
 * @param outward  泡沫朝向岸线外侧的单位法线（可为 null，自动判断）
 * @param count    泡沫团数量
 * @param seed     随机种子（保证每次渲染结果一致）
 */
export function foamAlong(
  line: PxPolyline,
  outward: [number, number] | null,
  count: number,
  seed = 1,
): FoamPatch[] {
  if (line.length < 2 || count <= 0) return []
  const out: FoamPatch[] = []
  let s = seed * 9301 + 49297
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280
    return s / 233280
  }
  for (let i = 0; i < count; i++) {
    /* 在折线上均匀取点 */
    const t = (i + rnd() * 0.8) / count
    const seg = Math.min(line.length - 2, Math.floor(t * (line.length - 1)))
    const local = t * (line.length - 1) - seg
    const [x0, y0] = line[seg]
    const [x1, y1] = line[seg + 1]
    const px = x0 + (x1 - x0) * local
    const py = y0 + (y1 - y0) * local

    /* 法线方向（外侧） */
    const dx = x1 - x0
    const dy = y1 - y0
    const len = Math.hypot(dx, dy) || 1
    let nx = -dy / len
    let ny = dx / len
    if (outward) {
      /* 让法线与给定方向同向 */
      if (nx * outward[0] + ny * outward[1] < 0) {
        nx = -nx
        ny = -ny
      }
    }

    /**
     * 【涌动幅度】
     * 破浪时白水向堤面推进的距离约为波高的 0.5~1.2 倍。
     * 在底图像素尺度上，取 3~14px 作为涌动幅度。
     */
    const surge = 3 + rnd() * 11

    /* 泡沫团略微离开岸线，并带一点散布 */
    const off = 1 + rnd() * 5
    const spread = (rnd() - 0.5) * 5

    /**
     * 【周期】
     * 涌浪周期通常 6~14 秒（近岸风浪）。
     * 每一团的周期略有差异 —— 完全同步会显得像机械动画。
     */
    const period = 6.5 + rnd() * 7.5

    out.push({
      cx: px + nx * (off + spread),
      cy: py + ny * (off + spread),
      r: 2.5 + rnd() * 6.5,
      opacity: 0.22 + rnd() * 0.4,
      phase: rnd() * Math.PI * 2,
      /* 沿外法线的单位向量 —— 渲染层据此做涌浪位移 */
      nx,
      ny,
      surge,
      /* 破碎强度峰值（0.3 ~ 1）：决定爆发瞬间的亮度与尺寸放大 */
      burst: 0.3 + rnd() * 0.7,
      period,
    })
  }
  return out
}'''

assert old_start in src, "foamAlong block not found"
src = src.replace(old_start, new, 1)
open(path, 'w', encoding='utf-8').write(src)
print("foamAlong upgraded")
