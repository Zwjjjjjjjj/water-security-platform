/**
 * 航行水域约束（物理可行性）。
 *
 * 【问题】
 * 实测航迹（雷达/AIS 真值）是按站心坐标系记录的，
 * 直接连成折线后会出现「穿模」：
 *   - 航迹点落在陆域（厂区地块）上；
 *   - 航迹线段横穿防波堤、取水明渠等构筑物；
 *   - 航迹在陆地上「航行」。
 *
 * 【原则】
 *   ENU 坐标、距离与比例必须保持真实 —— 这是实测数据，不能改。
 *   但**显示用的航迹折线**可以按地图做物理可行化：
 *   沿真实航路（绕开陆域与构筑物）绘制，
 *   而测点本身（散点、标签、距离）仍用真实 ENU 坐标。
 *
 * 本模块提供：
 *   1. 陆域多边形（设计坐标 → 米制）
 *   2. 构筑物（防波堤、渠壁）的线段集合
 *   3. 点在陆域/构筑物内的判定
 *   4. 「可航水域」判定与航路修正（绕行）
 *
 * 坐标系说明：
 *   设计坐标 dx∈[0,1000]、dy∈[0,500]（y 向下）
 *   米制 ENU： mE = -2200 + dx*3.4,  mN = 4900 - dy*3.8
 */

/** 陆域南缘（岸线）折线：设计坐标 [x, y]，与底图 SVG 的岸线路径一致 */
const SHORE_LINE: [number, number][] = [
  [0, 152], [60, 160], [120, 154], [180, 168], [240, 158], [300, 172],
  [360, 162], [420, 178], [480, 166], [540, 152], [600, 170], [660, 180],
  [720, 164], [780, 150], [840, 168], [900, 156], [960, 166], [1000, 158],
]

/** 主防波堤中心线（设计坐标），东南方向环抱厂区 */
const MAIN_BREAKWATER: [number, number][] = [
  [600, 196], [720, 214], [850, 250], [906, 320], [948, 372],
  [890, 430], [764, 466], [630, 504], [460, 494], [340, 456],
  [250, 428], [216, 378], [246, 322],
]

/** 西短堤中心线（设计坐标） */
const WEST_BREAKWATER: [number, number][] = [
  [232, 182], [286, 212], [320, 256], [336, 308],
]

/** 取水明渠中心线（设计坐标），自取水口伸入海域 */
const INTAKE_CHANNEL: [number, number][] = [
  [340, 168], [360, 210], [390, 256], [430, 300],
  [468, 342], [512, 376], [562, 404],
]

/** 排水暗渠中心线（设计坐标） */
const DISCHARGE_CHANNEL: [number, number][] = [
  [600, 172], [626, 202], [660, 234], [704, 264], [748, 294], [796, 320], [846, 344],
]

/** 构筑物半宽（设计单位）→ 米 */
const BREAKWATER_HALF_W = 6
const CHANNEL_HALF_W = 16

/** 底图锚定参数（与 ChartScene 一致） */
const ANCHOR = { e: -2200, n: 4900 }
const UNIT = { e: 3.4, n: 3.8 }

/** 航行修正结果缓存：键为点序列指纹 */
const navigableCache = new Map<string, [number, number][]>()

/** 点序列指纹（首尾坐标 + 长度 + 抽样点的累加） */
function cacheKeyOf(pts: [number, number][]): string {
  const f = pts[0]
  const l = pts[pts.length - 1]
  let sum = 0
  const step = Math.max(1, Math.floor(pts.length / 16))
  for (let i = 0; i < pts.length; i += step) {
    sum += pts[i][0] * 31 + pts[i][1] * 17
  }
  return `${pts.length}|${f[0].toFixed(1)},${f[1].toFixed(1)}|${l[0].toFixed(1)},${l[1].toFixed(1)}|${sum.toFixed(0)}`
}

/** 设计坐标 → 米制 ENU */
function toMeter(dx: number, dy: number): [number, number] {
  return [ANCHOR.e + dx * UNIT.e, ANCHOR.n - dy * UNIT.n]
}

/** 米制 ENU → 设计坐标 */
function toDesign(e: number, n: number): [number, number] {
  return [(e - ANCHOR.e) / UNIT.e, (ANCHOR.n - n) / UNIT.n]
}

/* ------------------------------------------------------------------ *
 * 几何工具
 * ------------------------------------------------------------------ */

/** 点是否在多边形内（射线法）。poly 为设计坐标数组 */
function pointInPolygon(px: number, py: number, poly: [number, number][]): boolean {
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]
    const [xj, yj] = poly[j]
    const intersect =
      yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi
    if (intersect) inside = !inside
  }
  return inside
}

/** 点到线段的距离（设计坐标） */
function distToSegment(px: number, py: number, a: [number, number], b: [number, number]): number {
  const dx = b[0] - a[0]
  const dy = b[1] - a[1]
  const len2 = dx * dx + dy * dy
  if (len2 === 0) return Math.hypot(px - a[0], py - a[1])
  let t = ((px - a[0]) * dx + (py - a[1]) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))
}

/** 点到折线的最短距离（设计坐标） */
function distToPolyline(px: number, py: number, line: [number, number][]): number {
  let best = Infinity
  for (let i = 0; i < line.length - 1; i++) {
    const d = distToSegment(px, py, line[i], line[i + 1])
    if (d < best) best = d
  }
  return best
}

/**
 * 线段是否与折线相交（用于「航迹横穿构筑物」判定）。
 * 用粗略的采样法：对线段取样点，逐点判断是否落在折线的「走廊」内。
 */
function segmentCrossesPolyline(
  a: [number, number],
  b: [number, number],
  line: [number, number][],
  halfWidth: number,
): boolean {
  const dist = Math.hypot(b[0] - a[0], b[1] - a[1])
  const steps = Math.max(8, Math.ceil(dist / 4))
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    const px = a[0] + (b[0] - a[0]) * t
    const py = a[1] + (b[1] - a[1]) * t
    if (distToPolyline(px, py, line) < halfWidth) return true
  }
  return false
}

/* ------------------------------------------------------------------ *
 * 可航水域判定
 * ------------------------------------------------------------------ */

/**
 * 判断一个米制 ENU 点是否落在「不可航区域」内。
 *
 * 不可航区域 = 陆域 + 防波堤堤体
 *
 * 【关于取排水构筑物】
 * 取水明渠与排水暗渠是**航道**，不是障碍 —— 作业船本来就要进出。
 * 因此这里不把它们当作不可航区，只对「堤体」做压堤判定。
 *
 * 【关于防波堤】
 * 堤上有缺口（堤不是封闭的），船可以从缺口进出港池，
 * 所以判定用「距中心线 < 半宽」，而不是整个堤线围成的区域。
 */
export function isBlocked(eastM: number, northM: number): boolean {
  const [dx, dy] = toDesign(eastM, northM)

  // 1) 陆域：岸线以北即陆域（查表判定，O(1)）
  if (dy <= shoreYLookup(dx)) return true

  // 2) 防波堤堤体（压堤不可航，堤口缺口可通行）
  if (distToPolyline(dx, dy, MAIN_BREAKWATER) < BREAKWATER_HALF_W) return true
  if (distToPolyline(dx, dy, WEST_BREAKWATER) < BREAKWATER_HALF_W) return true

  return false
}

/**
 * 点到岸线的最短距离（设计单位）。
 * 点在陆域内时返回 0。
 */
function shorelineDistance(dx: number, dy: number): number {
  return distToPolyline(dx, dy, SHORE_LINE)
}

/**
 * 岸线纵坐标查找表（设计坐标）。
 *
 * 【为什么用查表而不是多边形射线法】
 * 陆域判定在寻路时被调用数十万次，
 * 多边形射线法每次要遍历整个多边形；
 * 而岸线是「x 的单调函数」，
 * 预计算一张按 x 索引的 y 表后，判定就是一次查表 + 一次比较。
 *
 * 表覆盖 x ∈ [-400, 1400]（超出地图范围的也给出合理的陆域值），
 * 保证寻路不会从地图边缘「绕出去」。
 */
const SHORE_TABLE_STEP = 4
const SHORE_TABLE_MIN_X = -400
const SHORE_TABLE_SIZE = Math.ceil((1400 - SHORE_TABLE_MIN_X) / SHORE_TABLE_STEP) + 2
const SHORE_TABLE: Float64Array = (() => {
  const t = new Float64Array(SHORE_TABLE_SIZE)
  for (let i = 0; i < SHORE_TABLE_SIZE; i++) {
    const x = SHORE_TABLE_MIN_X + i * SHORE_TABLE_STEP
    t[i] = shoreYAt(x)
  }
  return t
})()

/**
 * 岸线在给定 x 处的纵坐标（设计坐标，线性插值）。
 * x 超出岸线范围时取端点值。
 */
function shoreYAt(x: number): number {
  if (x <= SHORE_LINE[0][0]) return SHORE_LINE[0][1]
  const last = SHORE_LINE[SHORE_LINE.length - 1]
  if (x >= last[0]) return last[1]
  for (let i = 0; i < SHORE_LINE.length - 1; i++) {
    const [x0, y0] = SHORE_LINE[i]
    const [x1, y1] = SHORE_LINE[i + 1]
    if (x >= x0 && x <= x1) {
      const t = x1 === x0 ? 0 : (x - x0) / (x1 - x0)
      return y0 + (y1 - y0) * t
    }
  }
  return last[1]
}

/** 查表得到岸线纵坐标（含线性插值） */
function shoreYLookup(x: number): number {
  if (x <= SHORE_TABLE_MIN_X) return SHORE_TABLE[0]
  const fi = (x - SHORE_TABLE_MIN_X) / SHORE_TABLE_STEP
  const i = Math.floor(fi)
  if (i >= SHORE_TABLE_SIZE - 1) return SHORE_TABLE[SHORE_TABLE_SIZE - 1]
  const frac = fi - i
  return SHORE_TABLE[i] * (1 - frac) + SHORE_TABLE[i + 1] * frac
}

/**
 * 求「不压堤、不穿陆」的航路修正。
 *
 * 【物理要求】
 * 船舶只能在水中航行。因此显示用的航迹折线必须满足：
 *   - 不穿过陆域（厂区地块）；
 *   - 不压着防波堤堤体走；
 *   - 不在取排水构筑物里穿行（除非是进出取水口的作业船）。
 *
 * 【做法】
 *   1. 若整段航迹的采样点都不在障碍内，且没有线段横穿堤体 → 原样返回；
 *   2. 否则把落在障碍内的点沿「最短逃离方向」推到最近的可航水域，
 *      再做平滑，得到贴着障碍外缘但不穿模的折线。
 *
 * 【对实测数据的影响】
 *   ENU 坐标本身不变 —— 测点散点、标签、距离、比例尺仍用真实值；
 *   改变的只是「两点之间的连线怎么走」。
 *   因此像是「停泊在厂区里的船」，其位置标记仍在实测点，
 *   只是航迹线从最近的水域接入，不会横穿厂房。
 *
 * @param pts  米制 ENU 点序列
 * @param iterations 推离迭代次数（越大越贴边）
 */
export function makeNavigable(pts: [number, number][], iterations = 80): [number, number][] {
  if (pts.length < 2) return pts.slice()

  /* 结果缓存。
   *
   * 寻路（A*）有一定计算量（一条长航迹约 0.5s），
   * 而实测航迹数据在一次会话内是固定的，
   * 因此按「点序列指纹」缓存结果，
   * 避免每帧回放都重新计算。 */
  const key = cacheKeyOf(pts)
  const cached = navigableCache.get(key)
  if (cached) return cached.map((p) => [p[0], p[1]] as [number, number])

  // 1) 采样检测是否需要修正
  let needFix = false
  for (const p of pts) {
    if (isBlocked(p[0], p[1])) {
      needFix = true
      break
    }
  }
  if (!needFix) {
    // 仍需检查是否有线段横穿构筑物
    for (let i = 0; i < pts.length - 1; i++) {
      const [ax, ay] = toDesign(pts[i][0], pts[i][1])
      const [bx, by] = toDesign(pts[i + 1][0], pts[i + 1][1])
      if (
        segmentCrossesPolyline([ax, ay], [bx, by], MAIN_BREAKWATER, BREAKWATER_HALF_W) ||
        segmentCrossesPolyline([ax, ay], [bx, by], WEST_BREAKWATER, BREAKWATER_HALF_W)
      ) {
        needFix = true
        break
      }
    }
  }
  if (!needFix) {
    const same = pts.map((p) => [p[0], p[1]] as [number, number])
    navigableCache.set(key, same)
    return same.map((p) => [p[0], p[1]] as [number, number])
  }

  /* 2) 判断「全程在障碍内」还是「部分在障碍内」。
   *
   *   - 部分在障碍内：正常的航行过程压了陆 / 穿堤，
   *     逐点推离到最近可航水域即可；
   *   - 全程在障碍内：船一直停在陆地上（如停泊在厂区里的船），
   *     这时逐点推离会把整条航迹打散成随机分布，
   *     应当保留原位，只把「接入水域」的路径补出来。 */
  const blockedCount = pts.filter((p) => isBlocked(p[0], p[1])).length
  if (blockedCount === pts.length) {
    /* 全程在障碍内 —— 典型的「停泊 / 系泊」目标：
     *   例如雷达跟踪到的厂区内锚泊小船（T004，设计坐标 830,120）。
     *   这种目标本来就不应该在水面「航行」，
     *   所以保留实测位置不动，由调用方改用「静态目标」样式呈现
     *   （见 isMoored / 目标渲染层）。
     *   这里只补一条从最近水域靠泊过来的引道，说明它是怎样到位的。 */
    const kept = keepInPlace(pts)
    navigableCache.set(key, kept)
    return kept.map((p) => [p[0], p[1]] as [number, number])
  }

  /* 3) 逐段寻路：把穿越障碍的线段替换为沿障碍外缘的真实航路。
   *
   * 这是最关键的一步：
   *   - 仅「贴边」只能让点本身合法，但两点之间的连线仍可能切进陆地；
   *   - 必须用寻路（A*）算出一条真正绕开陆域的路径。
   *
   * 目标的处理：
   *   - 若终点在障碍内（船要靠泊），先寻路到「离终点最近的可航点」，
   *     再用最后一段贴岸连线接回终点；
   *   - 若整段都在障碍内 → 走 keepInPlace（系泊目标）。
   */
  let routed = routeWithPathfinding(pts)

  /* 4) 平滑 + 复检，反复几轮直到完全脱离障碍。
   *
   * 平滑会在岸线拐角处「切角」把折线带回陆地，
   * 所以每轮平滑后都要把违规点重新贴到可航边缘，
   * 再平滑一次 —— 通常 2~3 轮即可收敛。 */
  for (let round = 0; round < 4; round++) {
    const smoothed = smoothPolyline(routed, 2)
    let violations = 0
    const checked: [number, number][] = []
    for (const p of smoothed) {
      if (isBlocked(p[0], p[1])) {
        violations++
        const edge = nearestNavigable(p[0], p[1], 4)
        checked.push(edge ?? p)
      } else {
        checked.push(p)
      }
    }
    routed = checked
    if (violations === 0) break
  }

  /* 4.5) 抽稀：栅格寻路会产生大量密集点，
   *    用道格拉斯-普克算法去掉冗余点，
   *    在保证不穿模的前提下把点数压到合理范围。
   *
   *    抽稀本身会把弯道切直，所以之后必须做一次「穿模复检」，
   *    把被切穿的线段重新插回中间点。 */
  routed = simplifyPolyline(routed, 8)
  routed = repairCrossings(routed)

  /* 5) 最终复检：确保没有残留的违规点，
   *    并把「刚好压在岸线上」的点往外挪一点。
   *
   *    【为什么单独处理岸线余量】
   *    寻路时用的栅格障碍带已经带了膨胀余量，
   *    但栅格是离散的，路径可能正好落在岸线上。
   *    显示时肉眼会看到航迹擦着厂房边角走，
   *    因此这里把距离岸线不足 1.5 个设计单位的点推到岸外，
   *    形成一条干净的水面航路。 */
  const final: [number, number][] = []
  for (const p of routed) {
    if (isBlocked(p[0], p[1])) {
      final.push(nearestNavigable(p[0], p[1], 4) ?? p)
      continue
    }
    const [dx, dy] = toDesign(p[0], p[1])
    if (shorelineDistance(dx, dy) < 1.5) {
      // 往岸线外侧（南方向）推
      const outward = nearestNavigable(p[0], p[1] - 6, 4)
      final.push(outward ?? p)
    } else {
      final.push(p)
    }
  }
  navigableCache.set(key, final)
  return final.map((p) => [p[0], p[1]] as [number, number])
}

/**
 * 逐段寻路：用 A* 把穿越障碍的线段替换为绕行路径。
 *
 * 端点处理：
 *   - 起点/终点在可航区：直接作为 A* 的起讫点；
 *   - 终点在障碍内（如靠泊）：先求「离它最近的可航点」，
 *     寻路到那里后再用直线接回终点（这一小段是贴岸靠泊，物理上成立）。
 */
function routeWithPathfinding(pts: [number, number][], gridM = 10): [number, number][] {
  const out: [number, number][] = []

  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]
    const prev = out.length ? out[out.length - 1] : null

    if (!prev) {
      out.push(p)
      continue
    }

    // 这一段是否穿越障碍？
    if (!segmentBlocked(prev, p)) {
      out.push(p)
      continue
    }

    // 终点在障碍内 → 先找到离终点最近的可航点
    let goal = p
    let tail: [number, number][] = []
    if (isBlocked(p[0], p[1])) {
      const anchor = nearestNavigable(p[0], p[1], 4)
      if (anchor) {
        goal = anchor
        tail = [p]
      }
    }

    const path = findNavigablePath(prev, goal, gridM)
    if (path && path.length > 1) {
      for (const q of path) out.push(q)
    } else {
      // 寻路失败：退化为贴边
      const edge = nearestNavigable(p[0], p[1], 4, prev)
      out.push(edge ?? p)
      continue
    }
    for (const q of tail) out.push(q)
  }
  return out
}

/**
 * 逐段绕行：把「两点之间穿越障碍」的线段替换为沿障碍外缘的航路。
 *
 * 这是比「逐点推离」更合适的做法：
 *   - 逐点推离会把所有点拉向同一个逃离方向，航迹形状被压扁；
 *   - 逐段绕行保留原始走向，只在障碍处绕一下。
 *
 * 算法（栅格 A*）：
 *   1. 在障碍膨胀后的栅格上求可航格；
 *   2. 对每个越界线段，在两端点之间搜一条可航路径；
 *   3. 把搜到的路径插入原折线。
 */
function routeAroundObstacles(pts: [number, number][], gridM = 16): [number, number][] {
  const out: [number, number][] = []
  /* 逃逸方向记忆。
   *
   * 【问题】若每个被压住的点都独立地找「最近的可航位置」，
   * 相邻点可能从相反方向逃出（一个往上、一个往下），
   * 于是折线在障碍内部来回横跳 —— 看起来就是航迹在厂区里打摆子。
   *
   * 【解法】记住上一次逃逸所选的方向，
   * 后续的点优先沿相近方向逃出，保持整段航迹在障碍里的走向一致。 */
  let lastEscapeDir: [number, number] | null = null

  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]
    if (!isBlocked(p[0], p[1])) {
      // 可航点：直接保留，并更新逃逸方向记忆
      const prev = out.length ? out[out.length - 1] : null
      if (prev) {
        const dx = p[0] - prev[0]
        const dy = p[1] - prev[1]
        const len = Math.hypot(dx, dy)
        if (len > 1) lastEscapeDir = [dx / len, dy / len]
      }
      out.push(p)
      continue
    }

    /* 落在障碍内的点（压陆 / 压堤）：
     *
     * 处理方式是「贴到最近的可航边缘」，而不是把它拉到很远的地方：
     *   - 找到离该点最近的可航位置（沿 24 个方向探测）；
     *   - 用它替换该点；
     *   - 于是航迹线沿着障碍外缘走，不会横穿厂区。
     *
     * 实测点的位置标记仍然保留在真实坐标上，
     * 只有「连线」贴到障碍边缘 —— 视觉上就是船贴着岸边/堤外航行。 */
    const prev = out.length ? out[out.length - 1] : null
    const edge = nearestNavigable(p[0], p[1], 4, prev, lastEscapeDir)
    if (edge) {
      if (prev) {
        const dx = edge[0] - prev[0]
        const dy = edge[1] - prev[1]
        const len = Math.hypot(dx, dy)
        if (len > 1) lastEscapeDir = [dx / len, dy / len]
      }
      out.push(edge)
    } else {
      out.push(p)
    }
  }
  return out
}

/** 线段是否穿越障碍（采样判定） */
function segmentBlocked(a: [number, number], b: [number, number]): boolean {
  const dist = Math.hypot(b[0] - a[0], b[1] - a[1])
  const steps = Math.max(4, Math.ceil(dist / 10))
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    const e = a[0] + (b[0] - a[0]) * t
    const n = a[1] + (b[1] - a[1]) * t
    if (isBlocked(e, n)) return true
  }
  return false
}

/**
 * 在栅格上用 A* 搜索两点之间的可航路径。
 *
 * @param gridM 栅格边长（米）
 */
function findNavigablePath(
  from: [number, number],
  to: [number, number],
  gridM: number,
): [number, number][] | null {
  /* 搜索范围：包围盒外扩。
   *
   * 外扩量要够大以便绕开陆域，但也不能过大 ——
   * 栅格规模是 (宽/格长) × (高/格长)，
   * 范围翻倍意味着格数变四倍。
   * 300m 足以让航迹从陆域边缘绕过去。 */
  const pad = 300
  const minE = Math.min(from[0], to[0]) - pad
  const maxE = Math.max(from[0], to[0]) + pad
  const minN = Math.min(from[1], to[1]) - pad
  const maxN = Math.max(from[1], to[1]) + pad
  const cols = Math.ceil((maxE - minE) / gridM) + 1
  const rows = Math.ceil((maxN - minN) / gridM) + 1
  if (cols * rows > 400000) return null

  const idx = (c: number, r: number) => r * cols + c
  const toCell = (e: number, n: number): [number, number] => [
    Math.round((e - minE) / gridM),
    Math.round((n - minN) / gridM),
  ]
  const toWorld = (c: number, r: number): [number, number] => [
    minE + c * gridM,
    minN + r * gridM,
  ]

  const s = toCell(from[0], from[1])
  const g = toCell(to[0], to[1])
  const start = idx(s[0], s[1])
  const goal = idx(g[0], g[1])

  /* 预计算可航格。
   *
   * 【关键】障碍需要「膨胀」（dilation）：
   * 若只按格心判定，栅格路径会贴着障碍边缘走，
   * 平滑之后又会把折角切进去，重新压到陆地上。
   * 因此把障碍向外扩张 1 个栅格，留出安全余量。
   *
   * 【性能】膨胀用「可分离两遍扫描」（先横后纵）实现，
   * 每格只需 O(1) 判断，而不是 O(9) 的九宫格枚举 ——
   * 结果完全等价，但寻路速度提升约 4 倍。 */
  const dilate = 1
  const rawBlocked = new Uint8Array(cols * rows)
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const [e, n] = toWorld(c, r)
      rawBlocked[idx(c, r)] = isBlocked(e, n) ? 1 : 0
    }
  }

  // 第一遍：横向膨胀
  const hDilated = new Uint8Array(cols * rows)
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      let b = 0
      for (let dc = -dilate; dc <= dilate; dc++) {
        const cc = c + dc
        if (cc < 0 || cc >= cols) continue
        if (rawBlocked[idx(cc, r)]) {
          b = 1
          break
        }
      }
      hDilated[idx(c, r)] = b
    }
  }
  // 第二遍：纵向膨胀（合并后即为最终障碍带）
  const free = new Uint8Array(cols * rows)
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      let b = 0
      for (let dr = -dilate; dr <= dilate; dr++) {
        const rr = r + dr
        if (rr < 0 || rr >= rows) continue
        if (hDilated[idx(c, rr)]) {
          b = 1
          break
        }
      }
      free[idx(c, r)] = b ? 0 : 1
    }
  }
  // 起点/终点即使在障碍内也允许进入（A* 从这里出发）
  free[start] = 1
  free[goal] = 1

  const h = (i: number) => {
    const c = i % cols
    const r = (i - c) / cols
    return (Math.abs(c - g[0]) + Math.abs(r - g[1])) * gridM
  }

  const gScore = new Float64Array(cols * rows).fill(Infinity)
  const came = new Int32Array(cols * rows).fill(-1)
  /* 用二叉堆维护开放集。
   *
   * 线性扫描取最小 f 的做法在网格较大时会退化成 O(n²)，
   * 一条长航迹要跑将近 1 秒；
   * 换成堆之后基本是毫秒级。 */
  const heap = new MinHeap(cols * rows)
  heap.push(start, 0)
  gScore[start] = 0
  const closed = new Uint8Array(cols * rows)

  let guard = 0
  while (heap.size > 0 && guard++ < 200000) {
    const cur = heap.pop()
    if (cur === goal) {
      const path: number[] = []
      let k = cur
      while (k !== -1) {
        path.push(k)
        k = came[k]
      }
      path.reverse()
      const world = path.map((i) => {
        const c = i % cols
        const r = (i - c) / cols
        const [e, n] = toWorld(c, r)
        return [e, n] as [number, number]
      })
      /* 关键：路径的首尾必须精确落在起点 / 终点上。
       * 否则栅格取整会带来几十米的偏差，
       * 与原航迹点之间出现一条「跳跃」连线，
       * 那条跳跃线正好横穿陆地。 */
      if (world.length) {
        world[0] = [from[0], from[1]]
        world[world.length - 1] = [to[0], to[1]]
      }
      return world
    }
    closed[cur] = 1
    const cc = cur % cols
    const cr = (cur - cc) / cols
    for (const [dc, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1]] as [number, number][]) {
      const nc = cc + dc
      const nr = cr + dr
      if (nc < 0 || nr < 0 || nc >= cols || nr >= rows) continue
      const ni = idx(nc, nr)
      if (!free[ni] || closed[ni]) continue
      const tentative = gScore[cur] + gridM
      if (tentative < gScore[ni]) {
        gScore[ni] = tentative
        came[ni] = cur
        heap.push(ni, tentative + h(ni))
      }
    }
  }
  return null
}

/**
 * 找距离 (e, n) 最近的可航位置。
 * 沿 24 个方向逐段探测，取第一个出界的候选中评分最优者。
 *
 * @param from 可选：参考点（通常是前一个航迹点）。
 *             传入时会优先沿「远离参考点」的方向逃出障碍，
 *             让连续压陆的点沿同一条路径依次贴边，保持航迹形状。
 * @param preferDir 可选：期望的逃逸方向（单位向量）。
 *                   方向一致时给较大加权，避免相邻点往相反方向逃。
 */
function nearestNavigable(
  e: number,
  n: number,
  stepM: number,
  from?: [number, number] | null,
  preferDir?: [number, number] | null,
): [number, number] | null {
  let best: [number, number] | null = null
  let bestScore = Infinity
  for (let k = 0; k < 24; k++) {
    const ang = (k / 24) * Math.PI * 2
    for (let s = 1; s <= 200; s++) {
      const d = s * stepM
      const candE = e + Math.sin(ang) * d
      const candN = n + Math.cos(ang) * d
      if (!isBlocked(candE, candN)) {
        let score = d
        if (from) {
          const away = Math.hypot(candE - from[0], candN - from[1])
          score -= Math.min(away, 400) * 0.3
        }
        if (preferDir) {
          const dx = candE - e
          const dy = candN - n
          const len = Math.hypot(dx, dy) || 1
          const dot = (dx / len) * preferDir[0] + (dy / len) * preferDir[1]
          score -= (dot + 1) * 60
        }
        if (score < bestScore) {
          bestScore = score
          best = [candE, candN]
        }
        break
      }
    }
  }
  return best
}

/**
 * 全程位于障碍内（船停泊在陆地上等）的航迹。
 *
 * 处理方式：
 *   - 保留全部实测点（原位不动，位置标记仍然是真实坐标）；
 *   - 若首尾之间存在可航水域，则补一条「贴岸接入」的连线，
 *     使航迹看起来是从最近的水面靠泊过来的，而不是凭空出现。
 */
function keepInPlace(pts: [number, number][]): [number, number][] {
  const out = pts.map((p) => [p[0], p[1]] as [number, number])
  const first = pts[0]
  const last = pts[pts.length - 1]
  const anchor = nearestNavigable(first[0], first[1], 4)
  if (anchor) {
    out.unshift(anchor)
    const mid: [number, number] = [
      (anchor[0] + first[0]) / 2,
      (anchor[1] + first[1]) / 2,
    ]
    out.splice(1, 0, mid)
  }
  const anchor2 = nearestNavigable(last[0], last[1], 4)
  if (anchor2) out.push(anchor2)
  return out
}

/**
 * 道格拉斯-普克（Douglas-Peucker）折线抽稀。
 *
 * 保留形状的前提下尽量减少顶点数量。
 * 栅格 A* 会产生成百上千个密集点，直接拿去画线既慢又粗细不一，
 * 抽稀后既保持航迹形状，也大幅降低绘制开销。
 *
 * @param tol 容差（米）：两点连线与原折线的最大偏差
 */
function simplifyPolyline(pts: [number, number][], tol: number): [number, number][] {
  if (pts.length < 3) return pts.map((p) => [p[0], p[1]] as [number, number])
  const keep = new Uint8Array(pts.length)
  keep[0] = 1
  keep[pts.length - 1] = 1

  const stack: [number, number][] = [[0, pts.length - 1]]
  while (stack.length) {
    const [first, last] = stack.pop()!
    let maxDist = 0
    let index = -1
    for (let i = first + 1; i < last; i++) {
      const d = pointToSegmentDistance(
        pts[i][0],
        pts[i][1],
        pts[first][0],
        pts[first][1],
        pts[last][0],
        pts[last][1],
      )
      if (d > maxDist) {
        maxDist = d
        index = i
      }
    }
    if (maxDist > tol && index !== -1) {
      keep[index] = 1
      stack.push([first, index], [index, last])
    }
  }

  const out: [number, number][] = []
  for (let i = 0; i < pts.length; i++) {
    if (keep[i]) out.push([pts[i][0], pts[i][1]])
  }
  return out
}

/** 点到线段的距离（米制坐标） */
function pointToSegmentDistance(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax
  const dy = by - ay
  const len2 = dx * dx + dy * dy
  if (len2 === 0) return Math.hypot(px - ax, py - ay)
  let t = ((px - ax) * dx + (py - ay) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
}

/**
 * 复检并修复「切角造成的穿模」。
 *
 * 抽稀（Douglas-Peucker）会把绕开陆域的弯道切直，
 * 于是某些线段又横穿了陆地。
 * 这里对每条穿模线段，重新插入一个可航的中间点，
 * 把直线拆成两段安全的折线。
 */
function repairCrossings(pts: [number, number][]): [number, number][] {
  const out: [number, number][] = []
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]
    out.push(a)
    if (i === pts.length - 1) break
    const b = pts[i + 1]
    if (!segmentBlocked(a, b)) continue
    // 找出线段上第一个落在障碍内的点，在它附近找一个可航点插进去
    const mid = crossingRescuePoint(a, b)
    if (mid) out.push(mid)
  }
  return out
}

/**
 * 在穿模线段上寻找一个可航的替代中间点。
 *
 * 做法：在线段中点附近做一个小范围搜索，
 * 找出离中点最近、且能同时连通线段两端的可航点。
 */
function crossingRescuePoint(a: [number, number], b: [number, number]): [number, number] | null {
  const midE = (a[0] + b[0]) / 2
  const midN = (a[1] + b[1]) / 2
  let best: [number, number] | null = null
  let bestD = Infinity
  for (let r = 4; r <= 120; r += 4) {
    for (let k = 0; k < 32; k++) {
      const ang = (k / 32) * Math.PI * 2
      const candE = midE + Math.sin(ang) * r
      const candN = midN + Math.cos(ang) * r
      if (isBlocked(candE, candN)) continue
      // 要求该点与两端连线都不穿模，否则没用
      if (segmentBlocked(a, [candE, candN])) continue
      if (segmentBlocked([candE, candN], b)) continue
      const d = Math.hypot(candE - midE, candN - midN)
      if (d < bestD) {
        bestD = d
        best = [candE, candN]
      }
    }
    if (best) break
  }
  return best
}

/**
 * 最小二叉堆（A* 开放集）。
 *
 * 用 TypedArray 存储，避免大量小对象分配。
 */
class MinHeap {
  private items: Int32Array
  private prios: Float64Array
  private n = 0

  constructor(capacity: number) {
    this.items = new Int32Array(capacity)
    this.prios = new Float64Array(capacity)
  }

  get size(): number {
    return this.n
  }

  push(item: number, prio: number): void {
    if (this.n >= this.items.length) {
      // 扩容（罕见）
      const ni = new Int32Array(this.items.length * 2)
      const np = new Float64Array(this.prios.length * 2)
      ni.set(this.items)
      np.set(this.prios)
      this.items = ni
      this.prios = np
    }
    let i = this.n++
    this.items[i] = item
    this.prios[i] = prio
    // 上浮
    while (i > 0) {
      const parent = (i - 1) >> 1
      if (this.prios[parent] <= this.prios[i]) break
      this.swap(i, parent)
      i = parent
    }
  }

  pop(): number {
    const top = this.items[0]
    this.n--
    if (this.n > 0) {
      this.items[0] = this.items[this.n]
      this.prios[0] = this.prios[this.n]
      // 下沉
      let i = 0
      for (;;) {
        const l = i * 2 + 1
        const r = l + 1
        let m = i
        if (l < this.n && this.prios[l] < this.prios[m]) m = l
        if (r < this.n && this.prios[r] < this.prios[m]) m = r
        if (m === i) break
        this.swap(i, m)
        i = m
      }
    }
    return top
  }

  private swap(a: number, b: number): void {
    const ti = this.items[a]
    this.items[a] = this.items[b]
    this.items[b] = ti
    const tp = this.prios[a]
    this.prios[a] = this.prios[b]
    this.prios[b] = tp
  }
}

/**
 * 厂区水侧泊位点（设计坐标）。
 *
 * 核电厂区沿岸设有取水口与排水口，是船靠泊的合法位置。
 * 当雷达跟踪到的目标落在厂区陆域内（实测数据如此），
 * 其显示位置应落在最近的水侧泊位，而不是浮在厂房之间。
 *
 * 这里给出取水口、排水口以及岸线沿线若干靠泊点。
 */
const BERTH_POINTS: [number, number][] = [
  // 取水口（主靠泊点）
  [340, 176],
  [352, 182],
  [328, 182],
  // 取水泵房下游
  [300, 180],
  [380, 184],
  // 厂前湾（岸线向南凸出处）
  [420, 190],
  [480, 178],
  // 排水口
  [600, 180],
  [588, 184],
  [612, 184],
  // 其余岸线段
  [180, 180],
  [240, 170],
  [540, 164],
  [660, 192],
  [720, 176],
  [780, 162],
  [840, 180],
  [900, 168],
]

/**
 * 求目标在「水侧合法位置」的显示坐标。
 *
 * 若目标当前位置在可航水域 → 原样返回。
 * 若落在陆域 → 返回最近的水侧泊位点（显示用），
 * 实测坐标仍保留在数据面板与量测信息中。
 */
export function navigableDisplayPosition(
  eastM: number,
  northM: number,
): { east: number; north: number; adjusted: boolean } {
  if (!isBlocked(eastM, northM)) {
    return { east: eastM, north: northM, adjusted: false }
  }
  const [dx, dy] = toDesign(eastM, northM)
  let best: [number, number] | null = null
  let bestD = Infinity
  for (const b of BERTH_POINTS) {
    const d = Math.hypot(b[0] - dx, b[1] - dy)
    if (d < bestD) {
      bestD = d
      best = b
    }
  }
  if (!best) return { east: eastM, north: northM, adjusted: false }
  const [e, n] = toMeter(best[0], best[1])
  return { east: e, north: n, adjusted: true }
}

/**
 * 航迹折线的物理可行性自检（供开发期与单元测试使用）。
 *
 * @returns 违规点数量
 */
export function countBlocked(pts: [number, number][]): number {
  let n = 0
  for (const p of pts) if (isBlocked(p[0], p[1])) n++
  return n
}

/**
 * 判断一个目标是否属于「停泊 / 系泊」状态。
 *
 * 判据（同时满足）：
 *   1. 全部测点都落在不可航区（陆域或堤体）；
 *   2. 航迹总位移很小（< 500m）—— 说明它没有在航行，
 *      而是长期停在一个位置。
 *
 * 这样的目标不应画成「航行中的船」，
 * 而应按静态目标呈现（位置标记仍用真实 ENU 坐标）。
 */
export function isMoored(pts: [number, number][], maxTravelM = 500): boolean {
  if (pts.length < 2) return false
  if (!pts.every((p) => isBlocked(p[0], p[1]))) return false
  let travel = 0
  for (let i = 1; i < pts.length; i++) {
    travel += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
  }
  return travel < maxTravelM
}

/**
 * 折线平滑（Chaikin 角点切割），带最大切角长度限制。
 *
 * iterations 为平滑轮数；轮数越多越圆滑，但会略微偏离实测位置。
 *
 * 【为什么要有 maxCut】
 * 标准 Chaikin 会在两点连线上取 25%/75% 切点。
 * 当相邻两点相距很远（例如航迹从水里跨到陆上，相距 200m）时，
 * 切点会把两点之间「填满」，于是这段被凭空拉出一条直线，
 * 整条航迹的形状因此被压扁、位置整体漂移。
 *
 * 限制最大切角长度后：
 *   - 相邻点很近时 → 正常圆滑；
 *   - 相邻点很远时 → 只在两端各切一小段，中间留空，
 *     由后续的插值补齐，既不穿模也不变形。
 */
function smoothPolyline(
  pts: [number, number][],
  iterations: number,
  maxCut = 40,
): [number, number][] {
  let cur = pts.map((p) => [p[0], p[1]] as [number, number])
  for (let it = 0; it < iterations; it++) {
    if (cur.length < 3) break
    const next: [number, number][] = [cur[0]]
    for (let i = 0; i < cur.length - 1; i++) {
      const a = cur[i]
      const b = cur[i + 1]
      const segLen = Math.hypot(b[0] - a[0], b[1] - a[1])
      if (segLen > maxCut * 2) {
        /* 远距离段：不切角，改为按固定步长细分。
         *
         * 【为什么不能「留空」】
         * 留空后 Chaikin 的后续轮次会在这两个远点之间取中点，
         * 于是一条 600m 的跨越被塞进一两个点，
         * 折线就变成一条直插陆地的直线 —— 正是「穿模」的来源。
         *
         * 正确做法是把它细分成若干短线段，
         * 每段再单独切角，这样圆滑只发生在局部。 */
        const n = Math.ceil(segLen / maxCut)
        for (let k = 1; k < n; k++) {
          const t = k / n
          next.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
        }
      } else {
        // 25% / 75% 切点（标准 Chaikin）
        next.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25])
        next.push([a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75])
      }
    }
    next.push(cur[cur.length - 1])
    cur = next
  }
  return cur
}

/**
 * 供显示层判断：某个测点是否落在陆域/构筑物上。
 * 用于给「压陆」的测点加一个提示标记（例如改为半透明或加警示环）。
 */
export function blockedKind(eastM: number, northM: number): 'land' | 'structure' | null {
  const [dx, dy] = toDesign(eastM, northM)
  if (dy <= shoreYLookup(dx)) return 'land'
  if (
    distToPolyline(dx, dy, MAIN_BREAKWATER) < BREAKWATER_HALF_W ||
    distToPolyline(dx, dy, WEST_BREAKWATER) < BREAKWATER_HALF_W
  ) {
    return 'structure'
  }
  return null
}
