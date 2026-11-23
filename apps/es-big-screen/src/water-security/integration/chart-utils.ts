/**
 * 四模块图表的共用绘制工具。
 *
 * 【为什么要单独抽一个文件】
 * 四个模块的图表风格必须**完全一致** —— 同一套坐标轴、
 * 同一套配色、同一套网格线、同一套空态文案。
 * 把这些抽出来，模块组件里就只剩下「画什么」，
 * 不会各写各的 Eventually 长得不一样。
 */

/** 主题色板 —— 与场景页保持一致的青蓝色系 */
export const PALETTE = {
  bg: '#04192e',
  panel: '#082a44',
  grid: 'rgba(34, 228, 238, 0.14)',
  axis: 'rgba(143, 185, 204, 0.45)',
  text: '#8fb9cc',
  textStrong: '#e1f7ff',
  cyan: '#22e4ee',
  blue: '#3ea8ff',
  blueLight: '#7fd4ff',
  green: '#39d98a',
  amber: '#ffb547',
  red: '#ff6b6b',
  violet: '#a98bff',
}

/**
 * 传感器类型 → 主色
 *
 * 与效果图一致：
 *   雷达=青, AIS=蓝, 声纳=绿, 光电=紫
 */
export const KIND_COLOR: Record<string, string> = {
  radar: PALETTE.cyan,
  ais: PALETTE.blueLight,
  sonar: PALETTE.green,
  eo: PALETTE.violet,
}

/** 关联状态 → 颜色 */
export const STATE_COLOR: Record<string, string> = {
  confirmed: PALETTE.green,
  pending: PALETTE.amber,
  unlinked: PALETTE.red,
}

/** 关联状态 → 中文 */
export const STATE_LABEL: Record<string, string> = {
  confirmed: '已确认',
  pending: '待确认',
  unlinked: '未关联',
}

/**
 * 把数值映射到画布坐标。
 *
 * @param v      数值
 * @param dMin   值域下限
 * @param dMax   值域上限
 * @param pMin   画布下限
 * @param pMax   画布上限
 */
export function scale(v: number, dMin: number, dMax: number, pMin: number, pMax: number): number {
  if (dMax === dMin) return (pMin + pMax) / 2
  const t = (v - dMin) / (dMax - dMin)
  return pMin + t * (pMax - pMin)
}

/**
 * 求一组数据的值域。
 *
 * 返回 `[min, max]`，并保证 max > min（至少留 1 个单位的余量），
 * 否则退化数据（比如全是同一个值）会让除零。
 */
export function extent(values: number[]): [number, number] {
  if (!values.length) return [0, 1]
  let min = Infinity
  let max = -Infinity
  for (const v of values) {
    if (!Number.isFinite(v)) continue
    if (v < min) min = v
    if (v > max) max = v
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1]
  if (min === max) return [min - 0.5, max + 0.5]
  return [min, max]
}

/**
 * 生成折线路径。
 *
 * @param pts  点集 [{x, y}]（已是画布坐标）
 */
export function linePath(pts: { x: number; y: number }[]): string {
  if (!pts.length) return ''
  return pts.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' ')
}

/**
 * 生成面积路径（折线 + 闭合到基线）。
 */
export function areaPath(pts: { x: number; y: number }[], baseY: number): string {
  if (!pts.length) return ''
  const first = pts[0]
  const last = pts[pts.length - 1]
  return `${linePath(pts)} L${last.x.toFixed(2)},${baseY.toFixed(2)} L${first.x.toFixed(2)},${baseY.toFixed(2)} Z`
}
