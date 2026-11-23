/**
 * 场景素材放置：船体 / 底图 的 SVG 矩阵计算
 *
 * 原则（来自素材包 README 约束）：
 * 1. 所有 ENU 坐标必须与已有航迹层共用同一投影，不得新建第二套坐标系；
 * 2. 船体长宽按素材的 opaque_bbox_px（alpha 轮廓）换算，不能按整张 PNG 尺寸；
 * 3. 2.5D 时船体与轨迹必须共用同一投影变换；
 * 4. length_m / beam_m 为示意模板尺寸，未经核验不得作为精度依据。
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
 * 计算船体贴图的 SVG 仿射矩阵。
 * 与素材包 integration/sprite-placement.mjs 的 shipImageMatrix 保持一致。
 */
export function shipImageMatrix(
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

  const point = project(state.east_m, state.north_m)
  const px = project(
    state.east_m + (right[0] * beamM) / (x1 - x0),
    state.north_m + (right[1] * beamM) / (x1 - x0),
  )
  const py = project(
    state.east_m - (forward[0] * lengthM) / (y1 - y0),
    state.north_m - (forward[1] * lengthM) / (y1 - y0),
  )

  const a = px[0] - point[0]
  const b = px[1] - point[1]
  const c = py[0] - point[0]
  const d = py[1] - point[1]
  return [a, b, c, d, point[0] - a * cx - c * cy, point[1] - b * cx - d * cy]
}

/** 矩阵转 SVG transform 字符串 */
export function svgMatrix(values: number[]): string {
  return `matrix(${values.map((v) => (Number.isFinite(v) ? +v.toFixed(4) : 0)).join(' ')})`
}

/**
 * 计算地面建筑贴图的矩阵。
 * 与船体同理：按 alpha 轮廓范围（opaque_bbox_px）换算实际占地的设计尺寸，
 * 并把轮廓中心对准锚点，避免透明留白造成的错位与尺寸偏差。
 */
export function groundSpriteMatrix(
  asset: { width_px: number; height_px: number; opaque_bbox_px?: [number, number, number, number] },
  /** 锚点像素位置 */
  anchorPx: [number, number],
  /** 轮廓目标显示尺寸（像素） */
  targetW: number,
  targetH: number,
): number[] {
  const bb = asset.opaque_bbox_px
  const [x0, y0, x1, y1] = bb && bb[2] > bb[0] && bb[3] > bb[1]
    ? bb
    : [0, 0, asset.width_px, asset.height_px]
  const bw = x1 - x0
  const bh = y1 - y0
  const sx = targetW / bw
  const sy = targetH / bh
  const cx = (x0 + x1) / 2
  const cy = (y0 + y1) / 2
  // translate(anchor - scale*center)
  return [sx, 0, 0, sy, anchorPx[0] - sx * cx, anchorPx[1] - sy * cy]
}

/**
 * 计算地面正俯视贴图的矩阵。
 * bounds 是场景明确设定的示意 ENU 框，不由 PNG 反推。
 */
export function groundImageMatrix(
  asset: { width_px: number; height_px: number },
  bounds: { eastMin: number; eastMax: number; northMin: number; northMax: number },
  project: ProjectFn,
): number[] {
  const { eastMin, eastMax, northMin, northMax } = bounds
  if (
    !Number.isFinite(eastMin) ||
    !Number.isFinite(eastMax) ||
    !Number.isFinite(northMin) ||
    !Number.isFinite(northMax) ||
    eastMax <= eastMin ||
    northMax <= northMin
  ) {
    throw new Error('底图 ENU 框无效')
  }
  const o = project(eastMin, northMax)
  const x = project(eastMin + (eastMax - eastMin) / asset.width_px, northMax)
  const y = project(eastMin, northMax - (northMax - northMin) / asset.height_px)
  return [x[0] - o[0], x[1] - o[1], y[0] - o[0], y[1] - o[1], o[0], o[1]]
}
