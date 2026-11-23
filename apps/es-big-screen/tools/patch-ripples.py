#!/usr/bin/env python3
"""升级 buildRipples：让波纹真实推进（自远及近），并携带传播参数。"""
path = 'src/water-security/integration/water-effects.ts'
src = open(path, encoding='utf-8').read()

old = """/** 一条细波纹（沿水平方向的低幅正弦） */
export interface Ripple {
  points: PxPolyline
  opacity: number
  phase: number
  /** 动画周期（秒） */
  period: number
}

/**
 * 生成海面细波纹。
 *
 * 【为什么只用正弦，不用噪声】
 * 正弦波的「缓慢起伏」读起来就是水面的涌浪；
 * 而高频噪声在低透明度叠加下只会变成脏点。
 *
 * 【为什么要避开陆地区域】
 * 波纹画到岸上会变成「岸上也有水纹」的穿帮。
 * 因此逐段采样，遇到陆地就断开该段。
 *
 * @param sampler 水域采样器
 * @param seed    随机种子
 * @param count   波纹条数
 */
export function buildRipples(
  sampler: WaterSampler,
  seed = 7,
  count = 26,
): Ripple[] {
  const out: Ripple[] = []
  let s = seed * 9301 + 49297
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280
    return s / 233280
  }
  for (let i = 0; i < count; i++) {
    const y = 80 + rnd() * (sampler.mapH - 140)
    const amp = 2 + rnd() * 5
    const wl = 90 + rnd() * 190
    const phase = rnd() * Math.PI * 2
    const period = 9 + rnd() * 13
    const x0 = rnd() * sampler.mapW
    const x1 = x0 + wl * (1.4 + rnd() * 2.4)
    const points: PxPolyline = []
    let run: PxPolyline = []
    for (let x = x0; x <= Math.min(x1, sampler.mapW); x += 6) {
      const yv = y + Math.sin((x - x0) / wl * Math.PI * 2 + phase) * amp
      if (sampler.isWater(x, yv)) {
        run.push([x, yv])
      } else {
        if (run.length > 4) points.push(...run)
        run = []
      }
    }
    if (run.length > 4) points.push(...run)
    if (points.length < 5) continue
    out.push({
      points,
      opacity: 0.05 + rnd() * 0.07,
      phase,
      period,
    })
  }
  return out
}"""

new = '''/** 一条细波纹（沿水平方向的低幅正弦） */
export interface Ripple {
  points: PxPolyline
  opacity: number
  phase: number
  /** 动画周期（秒） */
  period: number
  /**
   * 传播方向（单位向量，图片像素坐标）。
   *
   * 【物理含义】
   * 涌浪**从外海向岸边推进**，即大致沿「上方 → 下方」方向
   * （斜视图里岸线在上、海在下）。
   * 加上一点横向分量，波纹看起来才不会像整排同步平移的栅栏。
   */
  dx: number
  dy: number
  /**
   * 传播速度（像素/秒）。
   *
   * 浅水波速 $c=\\sqrt{gh}$；取 $g=9.8$、水深 $h\\approx6$ m 得 $c\\approx7.7$ m/s。
   * 换算到底图像素（0.4267 px/m）约 3.3 px/s，
   * 但斜视图有透视压缩，纵向看起来更快，故取 6~11 px/s。
   */
  speed: number
  /** 波纹在画面内的循环半径（像素）—— 走出画面后从另一侧回来 */
  span: number
}

/**
 * 生成海面细波纹。
 *
 * 【为什么只用正弦，不用噪声】
 * 正弦波的「缓慢起伏」读起来就是水面的涌浪；
 * 而高频噪声在低透明度叠加下只会变成脏点。
 *
 * 【为什么要避开陆地区域】
 * 波纹画到岸上会变成「岸上也有水纹」的穿帮。
 * 因此逐段采样，遇到陆地就断开该段。
 *
 * 【本次改动的核心 —— 波纹会「走」】
 * 旧版波纹只有透明度起伏，位置完全不动 ——
 * 看起来像「贴在地上的花纹」而不是水面。
 * 真实涌浪是**传播**的：波峰自外海向岸边推进。
 * 因此每条波纹携带传播方向与速度，渲染层按时间整体平移。
 *
 * @param sampler 水域采样器
 * @param seed    随机种子
 * @param count   波纹条数
 */
export function buildRipples(
  sampler: WaterSampler,
  seed = 7,
  count = 26,
): Ripple[] {
  const out: Ripple[] = []
  let s = seed * 9301 + 49297
  const rnd = () => {
    s = (s * 9301 + 49297) % 233280
    return s / 233280
  }
  for (let i = 0; i < count; i++) {
    const y = 80 + rnd() * (sampler.mapH - 140)
    const amp = 2 + rnd() * 5
    const wl = 90 + rnd() * 190
    const phase = rnd() * Math.PI * 2
    const period = 9 + rnd() * 13
    const x0 = rnd() * sampler.mapW
    const x1 = x0 + wl * (1.4 + rnd() * 2.4)
    const points: PxPolyline = []
    let run: PxPolyline = []
    for (let x = x0; x <= Math.min(x1, sampler.mapW); x += 6) {
      const yv = y + Math.sin((x - x0) / wl * Math.PI * 2 + phase) * amp
      if (sampler.isWater(x, yv)) {
        run.push([x, yv])
      } else {
        if (run.length > 4) points.push(...run)
        run = []
      }
    }
    if (run.length > 4) points.push(...run)
    if (points.length < 5) continue

    /**
     * 【传播方向】
     * 主方向朝下（向岸边推进），横向加 ±0.35 的分量。
     * 归一化后作为单位方向。
     */
    const rawDx = (rnd() - 0.5) * 0.7
    const rawDy = 1
    const rawLen = Math.hypot(rawDx, rawDy)
    const dx = rawDx / rawLen
    const dy = rawDy / rawLen

    out.push({
      points,
      opacity: 0.05 + rnd() * 0.07,
      phase,
      period,
      dx,
      dy,
      speed: 6 + rnd() * 5,
      span: sampler.mapH + 80,
    })
  }
  return out
}'''

assert old in src, "buildRipples block not found"
src = src.replace(old, new, 1)
open(path, 'w', encoding='utf-8').write(src)
print("buildRipples upgraded")
