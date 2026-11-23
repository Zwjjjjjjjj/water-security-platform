/**
 * V2 场景素材放置（修正版）
 *
 * 【为什么需要独立于 scene-placement.ts】
 * `scene-placement.ts` 里的 `shipImageMatrix()` 存在一处单位换算错误：
 *
 *   const px = project(
 *     state.east_m + (right[0] * beamM) / (x1 - x0),   // ← 除以像素宽度
 *     ...
 *   )
 *
 * 这里的意图是「让贴图轮廓的**像素宽度**对应 `beam_m` **米**」，
 * 但 ENU 坐标本身就是米制，正确写法应当直接用米制偏移：
 *
 *   横向偏移 = right  * beam_m   （米）
 *   纵向偏移 = forward * length_m （米）
 *
 * 原式多除了一次像素宽度（cargo_90m_top 的轮廓宽 298 px），
 * 导致船体在屏幕上被缩到 2～4 像素，几乎不可见。
 *
 * V1 沿用旧行为（用户要求 V1 保持原样、仅作为备份参考），
 * 因此这里另建一份修正版，仅供 V2 使用。
 */

/** 米制 ENU → 视图像素 的投影函数类型 */
export type ProjectFn = (eastM: number, northM: number) => [number, number]

/** 船体放置所需的目标状态 */
export interface ShipState {
  east_m: number
  north_m: number
  /** 航迹方向（度）。不等同真实艏向，仅用于显示 */
  course_deg: number | null
}

/** 素材的轮廓与锚点信息 */
export interface ShipSpriteInfo {
  id: string
  url: string
  width_px: number
  height_px: number
  /** alpha 轮廓像素范围 [x0,y0,x1,y1] */
  opaque_bbox_px: [number, number, number, number]
  /** 示意模板船长（米） */
  length_m: number
  /** 示意模板船宽（米） */
  beam_m: number
}

/**
 * 计算船体贴图的 SVG 仿射矩阵（V2 修正版）。
 *
 * 【坐标链路】
 *   1. 贴图锚点（轮廓中心）落在目标的 ENU 坐标上；
 *   2. 贴图轮廓的**像素宽度**映射为 `beam_m` 米、**像素长度**映射为 `length_m` 米；
 *   3. 再用同一套 `project()` 把米制偏移投到屏幕像素。
 *
 * 因此矩阵中：
 *   - `a / b` 是「每像素横向尺寸」对应的屏幕位移向量；
 *   - `c / d` 是「每像素纵向尺寸」对应的屏幕位移向量；
 *   - 平移量把轮廓中心对准目标点。
 */
export function shipImageMatrixV2(
  asset: ShipSpriteInfo,
  state: ShipState,
  project: ProjectFn,
  opts: { lengthM?: number; beamM?: number } = {},
): number[] {
  const lengthM = opts.lengthM ?? asset.length_m
  const beamM = opts.beamM ?? asset.beam_m
  if (
    !Number.isFinite(state.east_m) ||
    !Number.isFinite(state.north_m) ||
    state.course_deg == null ||
    !Number.isFinite(state.course_deg) ||
    !Number.isFinite(lengthM) ||
    !Number.isFinite(beamM) ||
    lengthM <= 0 ||
    beamM <= 0
  ) {
    throw new Error('需要有效 ENU、航迹方向与显示模板长宽')
  }

  const course = state.course_deg
  const [x0, y0, x1, y1] = asset.opaque_bbox_px
  if (!(x1 > x0 && y1 > y0)) throw new Error('素材轮廓范围无效')
  const cx = (x0 + x1) / 2
  const cy = (y0 + y1) / 2

  const angle = (course * Math.PI) / 180
  // 航向角：0 = 正北，顺时针为正
  const forward: [number, number] = [Math.sin(angle), Math.cos(angle)]
  const right: [number, number] = [Math.cos(angle), -Math.sin(angle)]

  /* 目标点（ENU 米制 → 屏幕像素） */
  const point = project(state.east_m, state.north_m)

  /* 轮廓横向边缘（+beam 方向），ENU 直接用米制偏移 */
  const px = project(state.east_m + right[0] * beamM, state.north_m + right[1] * beamM)
  /* 轮廓纵向边缘（+length 方向） */
  const py = project(state.east_m + forward[0] * lengthM, state.north_m + forward[1] * lengthM)

  const a = (px[0] - point[0]) / (x1 - x0)
  const b = (px[1] - point[1]) / (x1 - x0)
  const c = (py[0] - point[0]) / (y1 - y0)
  const d = (py[1] - point[1]) / (y1 - y0)

  return [a, b, c, d, point[0] - a * cx - c * cy, point[1] - b * cx - d * cy]
}

/** 矩阵转 SVG transform 字符串 */
export function svgMatrix(values: number[]): string {
  return `matrix(${values.map((v) => (Number.isFinite(v) ? +v.toFixed(4) : 0)).join(' ')})`
}
