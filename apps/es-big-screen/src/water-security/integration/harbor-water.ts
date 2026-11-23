/**
 * 港区水域掩码（斜视底图专用）。
 *
 * 【背景】
 * `campus_oblique_v3.png` 是**斜俯视**概念场景图：
 *   - 上半部：山体、厂区、岸线、码头（陆地）
 *   - 下半部：开阔海面
 *   - 中部：防波堤环抱的港池
 *
 * 水陆分界在图上是一条**斜线**，不是水平线。
 * 因此「水面上的圆形航迹是否穿陆」不能沿用旧版 2D 海图的岸线多边形，
 * 必须以**底图自身的像素**为准。
 *
 * 【本模块的做法】
 * 1. 离线用图像分割从底图里提取出「主水域连通域」；
 * 2. 腐蚀 7px（让船中心离岸留出余量，不压岸线）；
 * 3. 降采样成 **192×128 的 1bit 网格**（每格 8×8 原图像素）；
 * 4. 用 base64（packbits）编码成 4096 字符的常量内联在本文件里。
 *
 * 运行时只需要：
 *   - `enuToImage()` 把 ENU 米制映射到图片像素；
 *   - 查表判断某个像素是否属于可航水域；
 *   - 需要绕行时，用**图像空间**的 A* 在水域上找路。
 *
 * 【为什么用图像空间做 A*】
 * 航迹是真实 ENU 数据，但底图是概念示意图，两者没有地理配准，
 * 所以「绕开陆地」这件事只能在**底图自己的坐标系**里判断。
 * 本模块先在图像空间规划路径，再把路径映射回 ENU。
 */

/* ------------------------------------------------------------------ *
 * 掩码数据
 * ------------------------------------------------------------------ */

/**
 * 192×128 的 1bit 网格，每格代表底图 8×8 像素。
 * 1 = 可航水域，0 = 陆地/构筑物。
 */
/**
 * 192×128 的 1bit 网格，每格代表底图 8×8 像素。
 * 1 = 可航水域，0 = 陆地/构筑物。
 *
 * 【重建说明】
 * 旧掩码用连通域提取法从底图抠「主水域」，
 * 结果把**核电站厂区内部的一片凹地**误判成了水域 ——
 * 于是船只会「游进」厂房，T002 这类航迹也直接从厂区穿过。
 *
 * 新掩码改用「颜色特征 → 最大连通域 → 填孔 → 闭运算 → 腐蚀 21px」：
 *   · 厂区、堤坝、栈桥全部判为陆地；
 *   · 堤坝环抱的港池仍是水（填孔保证）；
 *   · 腐蚀 21px 让船中心离岸留出约 2.6 个掩码格的余量，
 *     避免船只「贴着岸边」。
 *
 * 掩码由 `tools/gen-water-mask.py` 生成，可复现。
 */
const MASK_B64 =
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/wAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAD/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAf//wAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAD//wAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//wAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'Af/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/w' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//wAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAf///wAAAAAAAAAAAAAAAAAAAAAAAAAAAf///wAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AA/////wAAAAAAAAAAAAAAAAAAAAAAAAA//////wAAAAAAAAAAAAAAAAAAAAAAAAA//////wAAAA' +
  'AAAAAAAAAAAAAAAAAAAAH//////wAAAAAAAAAAAAAAAAAAAAAAAA///////wAAAAAAAAAAAAAAAA' +
  'AAAAAAAAP//////wAAAAAAAAAAAAAAAAAAAAAAAAD//////wAAAAAAAAAAAAAAAAAAAAAAAAA///' +
  '///wAAAAAAAAAAAAAAAAAAAAAAAAAP/////wAAAAAAAAAAAAAAAAAAAAAAAAAD/////wAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAA/////wAAAAAAAAAAAAAAAAAAAAAAAAAAf////wAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAf////wAAAAAAAAAAAAAAAAAAAAAAAAAAP////wAAAAAAAAAAAAAAAAAAAAAAAAAAP////w' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAP////wAAAAAAAAAAAAAAAAAAAAAAAAAAP////wAAAAAAAAAAAA' +
  'AAAAAAAAAP/wAAP////wAAAAAAAAAAAAAAAD+AAAAP/wAAP////wAAAAAAAAAAAAAAAf/8AAAP/g' +
  'AAP////wAAAAAAAAAAAAAAB///8AAP/gAAP////wAAAAAAAAAAAAAAB////4AP/gAAf////wAAAA' +
  'AAAAAAAAAAD////////gAAf////wAAAAAAAAAAAAAAf////////gAA/////wAAAAAAAAAAAAAD//' +
  '///////gAB/////wAAAAAAAAAAAAAf/////////AAB/////wAAAAAAAAAAAAf/////////+AAD//' +
  '///wAAAAAAAAAAAD//////////8AAH/////wAAAAAAAAAAAf//////////4AAP/////wAAAAAAAA' +
  'AAB///////////wAAP/////wAAAAAAAAAAA///////////gAAf/////wAAAAAAAAAAA/////////' +
  '//AAA//////wAAAAAAAAAAA//////////+AAA//////wAAAAB+AAAAA//////////8AAB//////w' +
  'AAAAB/+AAAA//////////wAAD//////wAAAAP///wAA/////////+AAAH//////wDgAAf///8AA/' +
  '////////AAAAH//////wD+AP/////AA////////gAAAAP//////wD/////////////////4AAAAA' +
  'f//////wD////////////////4AAAAAAf//////wD///////////////8AAAAAAA///////wD///' +
  '////////////AAAAAAAB///////wD//////////////gAAAAAAAD///////wD/////////////wA' +
  'AAAAAAAP///////wD////////////AAAAAAAAAD////////wD///////////8AAAAAAAAAP/////' +
  '///wD///////////4AAAAAAAAH/////////wD///////////4AAAAAAAB//////////wD///////' +
  '////4AAAAAAA///////////wD///////////4AAAAAAf///////////wD///////////4AAAAAP/' +
  '///////////wD///////////4AAAAD/////////////wD///////////4AAAB//////////////w' +
  'D///////////4AAAf//////////////wD///////////4AAP///////////////wD///////////' +
  '/gP////////////////wD//////////////////////////////wD///////////////////////' +
  '///////wD//////////////////////////////wD//////////////////////////////wD///' +
  '///////////////////////////wD//////////////////////////////wD///////////////' +
  '///////////////wD//////////////////////////////wD///////////////////////////' +
  '///wD//////////////////////////////wD//////////////////////////////wD///////' +
  '///////////////////////wD//////////////////////////////wD///////////////////' +
  '///////////wD//////////////////////////////wD//////////////////////////////w' +
  'D//////////////////////////////wD//////////////////////////////wD///////////' +
  '///////////////////wD//////////////////////////////wD///////////////////////' +
  '///////wD//////////////////////////////wD//////////////////////////////wD///' +
  '///////////////////////////wD//////////////////////////////wD///////////////' +
  '///////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' +
  'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'


/** 掩码网格列数（底图 1536px / 8px 每格） */
export const MASK_COLS = 192
/** 掩码网格行数（底图 1024px / 8px 每格） */
export const MASK_ROWS = 128
/** 每格对应的底图原始像素边长 */
export const MASK_CELL = 8

/** 底图原生尺寸 */
export const MAP_W = 1536
export const MAP_H = 1024

/* ------------------------------------------------------------------ *
 * 掩码解码
 * ------------------------------------------------------------------ */

/** 解码后的可航水域位图（每格 1 字节，1=可航） */
let maskGrid: Uint8Array | null = null

/** base64 → Uint8Array（浏览器与 Node 均可用） */
function b64ToBytes(b64: string): Uint8Array {
  if (typeof atob === 'function') {
    const bin = atob(b64)
    const out = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
    return out
  }
  /* Node/SSR 环境回退 */
  const g = globalThis as any
  if (g && typeof g.Buffer !== 'undefined') {
    const buf = g.Buffer.from(b64, 'base64')
    return new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength)
  }
  return new Uint8Array(0)
}

/** 惰性解码掩码网格 */
function grid(): Uint8Array {
  if (maskGrid) return maskGrid
  const bytes = b64ToBytes(MASK_B64)
  const g = new Uint8Array(MASK_COLS * MASK_ROWS)
  for (let i = 0; i < g.length; i++) {
    const byte = bytes[i >> 3]
    g[i] = byte === undefined ? 0 : (byte >> (7 - (i & 7))) & 1
  }
  maskGrid = g
  return g
}

/* ------------------------------------------------------------------ *
 * ENU ↔ 图片像素
 * ------------------------------------------------------------------ */

/**
 * ENU 米制 → 底图图片像素。
 *
 * 与 `scenes.config.v2.ts` 的 `enuToImage()` 一致，
 * 这里单独实现一份是为了避免模块间循环依赖。
 */
export function enuToMapPx(e: number, n: number): [number, number] {
  const eMin = -2600
  const eMax = 1000
  const nMin = 2900
  const nMax = 5100
  const x0 = 0
  const x1 = MAP_W
  const y0 = 40
  const y1 = 1000
  const a = (x1 - x0) / (eMax - eMin)
  const d = -(y1 - y0) / (nMax - nMin)
  const eOff = x0 - a * eMin
  const f = y0 - d * nMax
  return [a * e + eOff, d * n + f]
}

/** 底图图片像素 → ENU 米制 */
export function mapPxToEnu(x: number, y: number): [number, number] {
  const eMin = -2600
  const eMax = 1000
  const nMin = 2900
  const nMax = 5100
  const a = MAP_W / (eMax - eMin)
  const d = -(1000 - 40) / (nMax - nMin)
  const eOff = 0 - a * eMin
  const f = 40 - d * nMax
  const det = a * d
  const dx = x - eOff
  const dy = y - f
  return [(d * dx - 0 * dy) / det, (a * dy - 0 * dx) / det]
}

/* ------------------------------------------------------------------ *
 * 水域判定
 * ------------------------------------------------------------------ */

/** 图片像素坐标是否落在可航水域内 */
export function isWaterPx(x: number, y: number): boolean {
  const g = grid()
  const cx = Math.floor(x / MASK_CELL)
  const cy = Math.floor(y / MASK_CELL)
  if (cx < 0 || cy < 0 || cx >= MASK_COLS || cy >= MASK_ROWS) return false
  return g[cy * MASK_COLS + cx] === 1
}

/**
 * ENU 点是否落在可航水域内。
 *
 * 【为什么要"连续采样"】
 * 掩码是 8×8 的粗网格，只在格心判断会有 ±4px 的误差，
 * 而一条航迹线的两点之间可能有几十米，对应几十像素。
 * 因此判定某条线段是否合法时，**沿整条线段连续采样**，
 * 只要有一处落在陆地上，就认为这条线段非法。
 */
export function isNavigableEnu(e: number, n: number): boolean {
  const [px, py] = enuToMapPx(e, n)
  return isWaterPx(px, py)
}

/**
 * 判断一条 ENU 线段是否全程在可航水域内。
 *
 * @param a 起点 ENU
 * @param b 终点 ENU
 * @param stepPx 采样步长（底图像素）
 */
export function segmentIsNavigable(a: [number, number], b: [number, number], stepPx = 4): boolean {
  const [x0, y0] = enuToMapPx(a[0], a[1])
  const [x1, y1] = enuToMapPx(b[0], b[1])
  const len = Math.hypot(x1 - x0, y1 - y0)
  const steps = Math.max(1, Math.ceil(len / stepPx))
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    if (!isWaterPx(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)) return false
  }
  return true
}

/* ------------------------------------------------------------------ *
 * 图像空间 A* 寻路
 * ------------------------------------------------------------------ */

/**
 * 「离岸余量」距离场（格数）。
 *
 * 【为什么需要】
 * 单纯的 A* 只要求「路径不穿陆」，因此会找到**贴着岸线**的最短路径。
 * 视觉上船只就像「蹭着堤坝走」，不符合「在水域中游动、不要贴着岸边」的要求。
 *
 * 【做法】
 * 用**倒角距离变换**（chamfer distance transform）算出每个水域格
 * 到最近陆地格的距离：
 *   · 先把陆地格设为 0，水域格设为 ∞；
 *   · 两遍扫描（正向 + 反向），每格取「左/上 + 1」「右/下 + 1」「左上/右下 + 1.414」的最小值。
 *
 * 结果单位是**掩码格**（每格 8 个底图像素）。
 *
 * 有了它，A* 就能给「离岸太近」的格子加代价，
 * 自然把航线推到离岸更远的地方。
 */
let clearanceField: Uint8Array | null = null

/** 期望的最小离岸余量（掩码格） */
const CLEARANCE_MIN = 3
/** 超出期望余量后不再继续奖励（掩码格） */
const CLEARANCE_SAT = 8
/** 贴岸惩罚的权重（乘在边长上；1.0 = 走贴岸格等于走两倍长的开阔格） */
const SHORE_WEIGHT = 1.6

function clearance(): Uint8Array {
  if (clearanceField) return clearanceField
  const g = grid()
  const cols = MASK_COLS
  const rows = MASK_ROWS
  const INF = 255
  const d = new Uint8Array(cols * rows).fill(INF)

  /* 陆地格距离 = 0 */
  for (let i = 0; i < d.length; i++) if (g[i] !== 1) d[i] = 0

  /* 正向扫描 */
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const i = y * cols + x
      if (d[i] === 0) continue
      let best = d[i]
      if (x > 0) best = Math.min(best, d[i - 1] + 1)
      if (y > 0) best = Math.min(best, d[i - cols] + 1)
      if (x > 0 && y > 0) best = Math.min(best, d[i - cols - 1] + 1.414)
      if (x < cols - 1 && y > 0) best = Math.min(best, d[i - cols + 1] + 1.414)
      d[i] = Math.min(INF, Math.round(best))
    }
  }
  /* 反向扫描 */
  for (let y = rows - 1; y >= 0; y--) {
    for (let x = cols - 1; x >= 0; x--) {
      const i = y * cols + x
      if (d[i] === 0) continue
      let best = d[i]
      if (x < cols - 1) best = Math.min(best, d[i + 1] + 1)
      if (y < rows - 1) best = Math.min(best, d[i + cols] + 1)
      if (x < cols - 1 && y < rows - 1) best = Math.min(best, d[i + cols + 1] + 1.414)
      if (x > 0 && y < rows - 1) best = Math.min(best, d[i + cols - 1] + 1.414)
      d[i] = Math.min(INF, Math.round(best))
    }
  }

  clearanceField = d
  return d
}

/**
 * 某格的「贴岸惩罚」代价（0 ~ 1）。
 *
 * 离岸余量不足 `CLEARANCE_MIN` 的格子，惩罚最大；
 * 达到 `CLEARANCE_SAT` 之后不再惩罚（鼓励在开阔水域航行）。
 *
 * 乘在 A* 的边上，使路径倾向于走开阔水面。
 */
function shorePenalty(cellIdx: number): number {
  const d = clearance()
  const c = d[cellIdx]
  if (c >= CLEARANCE_SAT) return 0
  if (c <= CLEARANCE_MIN) return 1
  /* CLEARANCE_MIN..CLEARANCE_SAT 之间线性递减 */
  return (CLEARANCE_SAT - c) / (CLEARANCE_SAT - CLEARANCE_MIN)
}

/** A* 结果缓存：键为起点/终点（像素级取整） */
const pathCache = new Map<string, [number, number][] | null>()

/** 缓存键 */
function pathKey(sx: number, sy: number, tx: number, ty: number): string {
  return `${sx >> 3},${sy >> 3},${tx >> 3},${ty >> 3}`
}

/** 八方向偏移 */
const NB = [
  [1, 0], [-1, 0], [0, 1], [0, -1],
  [1, 1], [1, -1], [-1, 1], [-1, -1],
] as const

/**
 * 在可航水域上用 A* 规划一条从起点到终点的路径（图片像素空间）。
 *
 * @param start 起点（图片像素）
 * @param goal  终点（图片像素）
 * @returns 路径点列（图片像素），失败返回 null
 */
function astarPx(start: [number, number], goal: [number, number]): [number, number][] | null {
  const g = grid()
  const cols = MASK_COLS
  const rows = MASK_ROWS

  const toCell = (p: [number, number]): [number, number] => [
    Math.min(cols - 1, Math.max(0, Math.floor(p[0] / MASK_CELL))),
    Math.min(rows - 1, Math.max(0, Math.floor(p[1] / MASK_CELL))),
  ]
  const toPx = (c: [number, number]): [number, number] => [
    (c[0] + 0.5) * MASK_CELL,
    (c[1] + 0.5) * MASK_CELL,
  ]

  const sc = toCell(start)
  const gc = toCell(goal)

  /* 两端都必须在水域里 */
  if (g[sc[1] * cols + sc[0]] !== 1) return null
  if (g[gc[1] * cols + gc[0]] !== 1) return null

  /* 起点与终点相同 */
  if (sc[0] === gc[0] && sc[1] === gc[1]) return [start]

  const n = cols * rows
  const gScore = new Float32Array(n).fill(Infinity)
  const cameFrom = new Int32Array(n).fill(-1)
  const closed = new Uint8Array(n)

  const idx = (c: [number, number]) => c[1] * cols + c[0]
  const startIdx = idx(sc)
  const goalIdx = idx(gc)
  gScore[startIdx] = 0

  /* 简单二叉堆。   *
   * 【关键：堆元素必须携带优先级】
   * 早先的实现是「堆里只放节点下标，比较时现读 gScore[idx]」。
   * 但 gScore 会在节点入堆**之后**被更新（找到更短路径时），
   * 于是堆的比较键变了 —— 堆的不变式被破坏，
   * pop() 出来的「最小节点」未必最小，A* 因此可能走不出正确路径
   * （实测：T002 的起点与终点明明在同一片水域，BFS 能连通，A* 却返回 null）。
   *
   * 正确做法：**把优先级和节点一起放进堆**，
   * 比较时用快照值，节点被改进时重新入堆。
   */
  const heap: number[] = []
  const heapKey: number[] = []
  const push = (v: number, key: number) => {
    heap.push(v)
    heapKey.push(key)
    let i = heap.length - 1
    while (i > 0) {
      const parent = (i - 1) >> 1
      if (heapKey[parent] <= heapKey[i]) break
      const tv = heap[parent], tk = heapKey[parent]
      heap[parent] = heap[i]; heapKey[parent] = heapKey[i]
      heap[i] = tv; heapKey[i] = tk
      i = parent
    }
  }
  const pop = (): number => {
    const top = heap[0]
    const lastV = heap.pop()!
    const lastK = heapKey.pop()!
    if (heap.length) {
      heap[0] = lastV
      heapKey[0] = lastK
      let i = 0
      for (;;) {
        const l = 2 * i + 1
        const r = l + 1
        let m = i
        if (l < heap.length && heapKey[l] < heapKey[m]) m = l
        if (r < heap.length && heapKey[r] < heapKey[m]) m = r
        if (m === i) break
        const tv = heap[m], tk = heapKey[m]
        heap[m] = heap[i]; heapKey[m] = heapKey[i]
        heap[i] = tv; heapKey[i] = tk
        i = m
      }
    }
    return top
  }

  const h = (c: [number, number]) => {
    const dx = Math.abs(c[0] - gc[0])
    const dy = Math.abs(c[1] - gc[1])
    /* 八方向启发式 */
    return (dx + dy) + (Math.SQRT2 - 2) * Math.min(dx, dy)
  }

  /* 【关键：起点必须入堆】
   * 早先的实现只在扩展邻居时 push，从未 push 起点 ——
   * 于是搜索队列一开始就是空的，while 循环直接结束、返回 null。
   * 这正是「BFS 能连通、A* 却说无路可走」的根本原因。
   */
  push(startIdx, h(sc))

  let expansions = 0
  const MAX_EXPAND = 200000

  while (heap.length) {
    const cur = pop()
    if (closed[cur]) continue
    closed[cur] = 1
    if (cur === goalIdx) {
      /* 回溯 */
      const path: [number, number][] = []
      let node = cur
      while (node !== -1) {
        path.push(toPx([node % cols, Math.floor(node / cols)]))
        node = cameFrom[node]
      }
      path.reverse()
      path[0] = start
      path[path.length - 1] = goal
      return path
    }
    if (++expansions > MAX_EXPAND) return null

    const ccx = cur % cols
    const ccy = Math.floor(cur / cols)
    for (const [dx, dy] of NB) {
      const nx = ccx + dx
      const ny = ccy + dy
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue
      const ni = ny * cols + nx
      if (closed[ni]) continue
      if (g[ni] !== 1) continue
      /* 斜向移动禁止"切角" */
      if (dx !== 0 && dy !== 0) {
        if (g[ccy * cols + nx] !== 1 || g[ny * cols + ccx] !== 1) continue
      }
      /* 【关键：验证真实连线，而不是只看格心】
       *
       * 掩码是 8×8 的粗网格。相邻两个**水域格**的格心之间，
       * 那条直线有可能正好从陆地格的角上斜穿过去 ——
       * 格心判定说「都是水」，但实际连线压到了陆地上。
       *
       * 斜视图里岸线是斜的，这种情况非常多，
       * 正是 T002 仍有 11 段穿陆的原因。
       *
       * 所以这里对每条候选边做**真实线段采样**：
       * 从当前格心到目标格心沿线采样，任意一点落在陆地就拒绝这条边。
       */
      if (!cellLinkSafe(ccx, ccy, nx, ny)) continue
      const step = dx !== 0 && dy !== 0 ? Math.SQRT2 : 1
      /**
       * 【贴岸惩罚】
       * 纯 A* 只会找「最短且不穿陆」的路径，结果常贴着岸线走 ——
       * 船只看起来像在蹭堤坝。
       *
       * 这里给「离岸余量不足」的格子加一份额外代价（最高 +100% 边长），
       * 让 A* 在距离差不多时**优先选开阔水面**。
       * 惩罚只影响「同等可走」时的取舍，不会让路径变得不成立。
       */
      const penalty = shorePenalty(ni)
      const tentative = gScore[cur] + step * (1 + penalty * SHORE_WEIGHT)
      if (tentative < gScore[ni]) {
        gScore[ni] = tentative
        cameFrom[ni] = cur
        /* 真 A*：按 f = g + h 排序（启发式只用于排序，不做剪枝） */
        push(ni, tentative + h([nx, ny]))
      }
    }
  }
  return null
}

/**
 * 判断两个相邻格之间的连线是否全程在水中（真实像素采样）。
 *
 * 【为什么必须做】
 * 粗网格掩码只记录「格心是否在水里」。
 * 两个水域格之间的直线，可能从陆地格的角上斜穿过去。
 * 斜视背景的岸线是斜的，这种情况非常常见。
 *
 * 【性能】
 * A* 会检查成千上万条候选边，若每条边都做密集采样会非常慢
 * （实测单次规划耗时几十秒）。
 * 这里只采样**关键点**：两端格心 + 中点 + 四分点，
 * 足以覆盖 8px 格子里最可能压到陆地的情况，开销只有密集采样的 1/4。
 *
 * @param c0 起点格坐标
 * @param c1 终点格坐标
 */
function cellLinkSafe(c0x: number, c0y: number, c1x: number, c1y: number): boolean {
  /* 格心（像素） */
  const x0 = (c0x + 0.5) * MASK_CELL
  const y0 = (c0y + 0.5) * MASK_CELL
  const x1 = (c1x + 0.5) * MASK_CELL
  const y1 = (c1y + 0.5) * MASK_CELL
  /* 关键采样点：0 / 0.25 / 0.5 / 0.75 / 1 */
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    if (!isWaterPx(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)) return false
  }
  return true
}

/* ------------------------------------------------------------------ *
 * 对外接口
 * ------------------------------------------------------------------ */

const navCache = new Map<string, [number, number][]>()

/** 点序列指纹 */
function ptsKey(pts: [number, number][]): string {
  const f = pts[0]
  const l = pts[pts.length - 1]
  let sum = 0
  const step = Math.max(1, Math.floor(pts.length / 16))
  for (let i = 0; i < pts.length; i += step) sum += pts[i][0] * 31 + pts[i][1] * 17
  return `${pts.length}|${f[0].toFixed(1)},${f[1].toFixed(1)}|${l[0].toFixed(1)},${l[1].toFixed(1)}|${sum.toFixed(0)}`
}

/**
 * 把一条 ENU 航迹修正为「全程在可航水域内」的折线。
 *
 * 【算法】
 * 1. 逐段检查原始折线：整段都在水域里 → 原样返回；
 * 2. 否则，把**每个点**投影到最近的可航水域（用 BFS 由近及远扩散）；
 * 3. 对修正后的点列，**相邻两点若直线穿陆**，则用 A* 在水域上绕行；
 * 3b. A* 失败时退化为「沿岸线平移」的兜底方案；
 * 4. 最后做一次轻量平滑，让折线更接近真实航路。
 *
 * @param pts 原始 ENU 点列
 * @param opts.keepEndpoints 是否保留首尾原始坐标（实测点仍需真实）
 */
export function makeNavigableOnMap(
  pts: [number, number][],
  opts: { keepEndpoints?: boolean; smooth?: number } = {},
): [number, number][] {
  if (pts.length < 2) return pts.slice()

  const key = ptsKey(pts)
  const cached = navCache.get(key)
  if (cached) return cached.map((p) => [p[0], p[1]] as [number, number])

  const keepEnd = opts.keepEndpoints !== false

  /* 1) 先看是否本来就合法 */
  const allOk = pts.every((p) => isNavigableEnu(p[0], p[1])) &&
    !pts.some((p, i) => i < pts.length - 1 && !segmentIsNavigable(p, pts[i + 1]))

  let result: [number, number][]

  if (allOk) {
    /* 本来就全程在水里 —— 原样保留（实测航迹不做任何改动） */
    result = pts.map((p) => [p[0], p[1]] as [number, number])
  } else {
    /* 【策略 A：整体 A*（起点 → 终点）】
     *
     * 这是最重要的路径：T002 有 16 个实测点整段落在厂区陆地上，
     * 逐段处理会因为「把点投影到最近水域 → 落到陆地对侧」
     * 而找不到正确的绕行方向。
     *
     * 整体 A* 直接从起点规划到终点，由 A* 自己决定绕行路线，
     * 再把水中���实测点贴回走廊 —— 既保住真实运动趋势，又全程避陆。
     */
    const whole = routeWholeTrack(pts)
    if (whole && polylineIsNavigable(whole)) {
      result = whole
    } else {
      /* 【策略 B：逐段修复】（整体 A* 不可用时的兜底） */
      result = repairPolylineOnWater(pts, 3)
    }
  }

  /* 端点还原：实测点必须留在真实坐标上。
   * 但若实测端点本身落在陆地上（靠泊/锚泊在码头），
   * 保留它就等于「船停在厂房里」，与「保证船在水里」冲突。
   * 因此只在端点**本来就在水里**时保留原值。 */
  if (keepEnd) {
    const first = result[0]
    const last = result[result.length - 1]
    if (first && isNavigableEnu(pts[0][0], pts[0][1])) {
      first[0] = pts[0][0]
      first[1] = pts[0][1]
    }
    if (last && isNavigableEnu(pts[pts.length - 1][0], pts[pts.length - 1][1])) {
      last[0] = pts[pts.length - 1][0]
      last[1] = pts[pts.length - 1][1]
    }
  }

  navCache.set(key, result)
  return result.map((p) => [p[0], p[1]] as [number, number])
}

/** 两点是否几乎重合 */
function samePoint(a: [number, number], b: [number, number]): boolean {
  return Math.abs(a[0] - b[0]) < 0.5 && Math.abs(a[1] - b[1]) < 0.5
}

/**
 * 把一个点投影到最近的可航水域。
 *
 * 【做法】
 * 先沿「上/下/左/右」四个方向找最近的水域格；
 * 若都找不到，说明这个点在离岸很远的内陆深处 —— 这种情况极少出现，
 * 此时退化为「向图外海方向」逐步搜索。
 */
function projectToWater(p: [number, number]): [number, number] {
  const [px, py] = enuToMapPx(p[0], p[1])
  if (isWaterPx(px, py)) return [p[0], p[1]]

  const g = grid()
  const cols = MASK_COLS
  const rows = MASK_ROWS
  const cx = Math.floor(px / MASK_CELL)
  const cy = Math.floor(py / MASK_CELL)

  /* BFS 找最近水域格 */
  const seen = new Uint8Array(cols * rows)
  const queue: number[] = [cy * cols + cx]
  seen[cy * cols + cx] = 1
  let head = 0
  let best = -1
  while (head < queue.length) {
    const cur = queue[head++]
    const ccx = cur % cols
    const ccy = Math.floor(cur / cols)
    for (const [dx, dy] of NB) {
      const nx = ccx + dx
      const ny = ccy + dy
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue
      const ni = ny * cols + nx
      if (seen[ni]) continue
      seen[ni] = 1
      if (g[ni] === 1) {
        best = ni
        break
      }
      queue.push(ni)
    }
    if (best >= 0) break
  }

  if (best < 0) {
    /* 找不到（理论上不会发生），退化为朝图像中心的海域方向 */
    return [p[0], p[1]]
  }

  const bx = best % cols
  const by = Math.floor(best / cols)
  return mapPxToEnu((bx + 0.5) * MASK_CELL, (by + 0.5) * MASK_CELL)
}

/**
 * 在水域上为 a→b 规划一条绕行路径。
 *
 * @param a    投影后的起点 ENU
 * @param b    投影后的终点 ENU
 * @param rawA 原始起点（兜底用）
 * @param rawB 原始终点（兜底用）
 */
function routeAround(
  a: [number, number],
  b: [number, number],
  rawA: [number, number],
  rawB: [number, number],
): [number, number][] {
  const [sx, sy] = enuToMapPx(a[0], a[1])
  const [gx, gy] = enuToMapPx(b[0], b[1])
  const path = astarPx([sx, sy], [gx, gy])
  if (!path) return [[a[0], a[1]], [b[0], b[1]]]
  return path.map((q) => mapPxToEnu(q[0], q[1]))
}

/* ------------------------------------------------------------------ *
 * 航迹合法性修复
 * ------------------------------------------------------------------ */

/**
 * 检查一段折线是否**全程**落在可航水域内。
 *
 * 【为什么必须逐段连续采样】
 * 掩码是 8×8 的粗网格，逐点判定会漏掉
 * 「两端都在水里、但连线从陆地斜穿过去」的情况
 * （尤其在斜视图里，岸线是斜的）。
 */
export function polylineIsNavigable(pts: [number, number][], stepPx = 3): boolean {
  if (pts.length < 2) return true
  for (let i = 0; i < pts.length - 1; i++) {
    if (!segmentIsNavigable(pts[i], pts[i + 1], stepPx)) return false
  }
  return true
}

/**
 * 把一条不合法的折线修复成全程在水里的折线。
 *
 * 【关键：整体 A*，而不是逐段 A*】
 * T002 的实测航迹有一段（点 4~19）整段落在厂区陆地上。
 * 若**逐段** A*：
 *   · 每段的两个端点都被投影到「最近水域」；
 *   · 而最近水域常常在陆地的**另一侧**；
 *   · 于是路径会在陆地上「横跳」，A* 找不到真正的绕行方向。
 *
 * 若**整体** A*（从航迹起点直接规划到终点）：
 *   · A* 会自己找到绕过整片陆地的水路；
 *   · 中间点由路径本身给出，不需要逐点投影。
 *
 * 因此这里的策略是：
 *   1. 先尝试「起点 → 终点」的整体 A*；
 *   2. 若成功且合法 → 直接采用（这是最理想的情况）；
 *   3. 若失败（两端分属不同水域 / 无路可走）→ 退化为逐段修复。
 *
 * @param pts    待修复的折线（ENU）
 * @param maxPass 最多修复轮数（防止死循环）
 */
export function repairPolylineOnWater(
  pts: [number, number][],
  maxPass = 3,
): [number, number][] {
  if (pts.length < 2) return pts.slice()

  /* ---- 策略 1：整体 A*（起点 → 终点），能绕就绕 ---- */
  const whole = routeWholeTrack(pts)
  if (whole && polylineIsNavigable(whole)) {
    return roundCornersSafe(whole, 2)
  }

  /* ---- 策略 2：逐段修复（整体 A* 不可用时的兜底） ---- */
  let cur = pts.map((p) => [p[0], p[1]] as [number, number])

  for (let pass = 0; pass < maxPass; pass++) {
    /* 先把所有端点投影到水域（实测点在陆上的也要挪开） */
    const projected = cur.map((p) => projectToWater(p))

    const out: [number, number][] = []
    let changed = false

    for (let i = 0; i < projected.length - 1; i++) {
      const a = projected[i]
      const b = projected[i + 1]
      if (segmentIsNavigable(a, b, 3)) {
        out.push(a)
      } else {
        changed = true
        /* A* 绕行；失败时退回「直接在两点间取中点序列」的保守方案 */
        const routed = routeAround(a, b, cur[i], cur[i + 1])
        for (const q of routed) {
          if (out.length && samePoint(out[out.length - 1], q)) continue
          out.push(q)
        }
      }
    }
    out.push(projected[projected.length - 1])

    cur = out
    if (!changed) break
  }

  /* 最后做一次「安全圆角」：只在确认圆角后的线段仍全程在水里时才采用。
   * 这样能去掉生硬的直角折线，又不会像无脑 Chaikin 那样切进陆地。 */
  const rounded = roundCornersSafe(cur, 2)

  /* 【最终兜底】再扫一遍，若仍有穿陆段，就把该段的两个端点各自
   * 投影到最近水域，并插入一个中点 —— 这样至少不会一整段斜穿陆地。
   * 正常情况下这一步不会触发（A* + 安全圆角已经保证合法）。 */
  return finalSafetySweep(rounded)
}

/**
 * 「起点 → 终点」的整体 A* 绕行。
 *
 * 【为什么需要】
 * 当航迹中间有一段落在陆地上时，逐段 A* 会因为
 * 「投影到最近水域导致点在陆地对侧」而无法找到正确绕行方向。
 * 整体 A* 直接从起点走到终点，由 A* 自己决定绕行路线。
 *
 * 【返回结果的构成】
 *   · A* 路径（图片像素 → ENU），作为主体；
 *   · 起点与终点附近的实测点（若实测点在水中则保留原值）。
 *
 * @returns 绕行后的折线；无法规划时返回 null
 */
function routeWholeTrack(pts: [number, number][]): [number, number][] | null {
  /**
   * 【端点先推到「离岸有余量」的水域】
   *
   * 实测端点常常落在岸边窄缝里（T004 就在画面右上角的一格宽水带中，
   * 余量只有 1 格）。若直接以它作为 A* 的起终点，
   * 整条航线都会被钉在岸边 —— 船看起来像「骑」在堤坝上。
   *
   * 所以先把端点推到附近余量达标的开阔水面（`clampToOpenWater`，
   * 搜索半径 12 格 ≈ 90 米，位移有限），
   * 再以修正后的端点规划航线。
   *
   * 【必须校验连通性】
   * 修正后的端点可能落在「离实测点很近、但与主水域不连通」的孤立水洼里
   * （例如 T002 的末点被推到厂区包围的小池塘中）。
   * 此时 A* 会返回 null，航迹退化成逐段修复，质量明显下降。
   *
   * 因此：**先试修正后的端点，失败再退回原始端点**，
   * 保证「宁可贴着岸走对路，也不要走错路」。
   */
  const rawFirst = pts[0]
  const rawLast = pts[pts.length - 1]
  if (!isNavigableEnu(rawFirst[0], rawFirst[1])) return null
  if (!isNavigableEnu(rawLast[0], rawLast[1])) return null

  /* 候选端点对：先试「推到开阔水面」的版本，再退回原始实测端点 */
  const candidates: [[number, number], [number, number]][] = [
    [clampToOpenWater(rawFirst), clampToOpenWater(rawLast)],
    [[rawFirst[0], rawFirst[1]], [rawLast[0], rawLast[1]]],
  ]

  for (const [first, last] of candidates) {
    const [sx, sy] = enuToMapPx(first[0], first[1])
    const [gx, gy] = enuToMapPx(last[0], last[1])
    const path = astarPx([sx, sy], [gx, gy])
    if (!path) continue

    const raw = path.map((q) => mapPxToEnu(q[0], q[1]))
    /* A* 路径两端用的是格心，替换回修正后的端点 */
    raw[0] = [first[0], first[1]]
    raw[raw.length - 1] = [last[0], last[1]]

    /* 【简化路径】
     * A* 输出的是逐格的锯齿路径（8px 一格），点数非常多
     * （T002 一次规划就产生近千个点）。
     * 直接拿去画线既慢又难看。
     *
     * 这里做「视线可达」简化：
     *   从当前点出发，尽量往前找**直线可达**的最远点，
     *   把中间的锯齿点全部丢掉。
     *   直线可达 = 整条连线都在水域里（逐段采样验证）。
     *
     * 简化后点数通常降到几十个，且仍保证全程在水里。
     */
    const corridor = simplifyWaterPath(raw)

    /* 【把实测点贴回走廊】
     *
     * 单纯用 A* 走廊会让航迹「贴着掩码边缘」走，
     * 丢掉实测点体现出的真实运动趋势（比如 T001 那一串折返）。
     *
     * 做法：对每个**本身在水中**的实测点，
     *   若它到走廊某段的垂距足够近（落在走廊「走廊带」内），
     *   就把它插到该处，替代走廊的折点。
     *
     * 这样既保留实测轨迹的形状，又保证整条线不穿陆。
     */
    return attachMeasuredPoints(corridor, pts)
  }

  return null
}

/**
 * 把实测点「贴回」A* 走廊。
 *
 * 【算法】
 * 1. 先把走廊按**航程**参数化（每个走廊点记录累计距离）；
 * 2. 对每个在水中��实测点，找到走廊上距它最近的点；
 * 3. 若距离小于容差（默认 60m），则把实测点插入到走廊的该位置；
 * 4. 插入后仍做一次合法性校验（相邻段必须都在水里），
 *    校验不通过就放弃该插入（宁可少一个点，也不能穿陆）。
 *
 * @param corridor A* 走廊（已简化）
 * @param pts      实测点列
 */
function attachMeasuredPoints(
  corridor: [number, number][],
  pts: [number, number][],
  toleranceM = 60,
): [number, number][] {
  if (corridor.length < 2) return corridor

  /* 走廊累计航程 */
  const cum: number[] = [0]
  for (let i = 1; i < corridor.length; i++) {
    cum[i] = cum[i - 1] + Math.hypot(
      corridor[i][0] - corridor[i - 1][0],
      corridor[i][1] - corridor[i - 1][1],
    )
  }
  const total = cum[cum.length - 1] || 1

  /* 每个实测点 → 走廊参数（0..1）+ 垂距 */
  type Ins = { p: [number, number]; t: number; dist: number }
  const inserts: Ins[] = []
  for (const p of pts) {
    if (!isNavigableEnu(p[0], p[1])) continue
    /**
     * 【离岸余量筛选】
     * 实测点本身可能就贴着岸边（尤其是港池窄的地方）。
     * 把这类点贴回走廊，会把航线又拉到岸边上 ——
     * 用户明确要求「不要贴着岸边」。
     *
     * 因此只接受离岸余量达标的实测点；
     * 贴岸的实测点直接跳过，航迹仍沿走廊走（保持在开阔水面）。
     */
    if (clearanceCells(p[0], p[1]) < CLEARANCE_MIN) continue
    let bestT = 0
    let bestDist = Infinity
    for (let i = 0; i < corridor.length - 1; i++) {
      const a = corridor[i]
      const b = corridor[i + 1]
      const vx = b[0] - a[0]
      const vy = b[1] - a[1]
      const len2 = vx * vx + vy * vy
      const wx = p[0] - a[0]
      const wy = p[1] - a[1]
      let u = len2 > 0 ? (wx * vx + wy * vy) / len2 : 0
      if (u < 0) u = 0
      if (u > 1) u = 1
      const qx = a[0] + vx * u
      const qy = a[1] + vy * u
      const d = Math.hypot(p[0] - qx, p[1] - qy)
      if (d < bestDist) {
        bestDist = d
        bestT = (cum[i] + Math.sqrt(len2) * u) / total
      }
    }
    if (bestDist <= toleranceM) inserts.push({ p: [p[0], p[1]], t: bestT, dist: bestDist })
  }
  if (!inserts.length) return corridor

  /* 按走廊参数排序 */
  inserts.sort((x, y) => x.t - y.t)

  /* 【重建走廊】
   * 不能用 splice 往走廊里插点 ——
   * 实测点的参数顺序一旦与走廊点序错位，就会出现
   * 「航迹突然折回去」的长度异常（实测 T001 从 2205m 缩到 288m）。
   *
   * 正确做法：**按参数顺序重新生成点列**：
   *   · 把实测点按 t 排序；
   *   · 在每个实测点的 t 区间内，用实测点替换走廊折点；
   *   · 走廊上被替换掉的折点仍保留在实测点两侧，
   *     这样实测点与走廊之间的过渡段不会穿陆。
   */
  const out: [number, number][] = [corridor[0]]
  let ci = 1

  for (const ins of inserts) {
    /* 找到 t 对应的走廊区间 [ci, ci+1) */
    while (ci < corridor.length - 1 && cum[ci] / total < ins.t) ci++
    const prev = out[out.length - 1]

    /* 走廊在 ins.t 处的实际位置 */
    const a = corridor[ci]
    const b = corridor[ci + 1] ?? corridor[ci]
    const segLen = cum[ci + 1] !== undefined ? cum[ci + 1] - cum[ci] : 0
    const localT = segLen > 0 ? (ins.t * total - cum[ci]) / segLen : 0
    const anchor: [number, number] = [
      a[0] + (b[0] - a[0]) * localT,
      a[1] + (b[1] - a[1]) * localT,
    ]

    /* 实测点离走廊太远就不插入（避免航迹大幅偏离走廊） */
    if (Math.hypot(ins.p[0] - anchor[0], ins.p[1] - anchor[1]) > toleranceM * 1.5) continue

    /* 合法性：prev → 实测点 必须全程在水里 */
    if (!segmentIsNavigable(prev, ins.p, 2)) continue
    /* 还要保证这条过渡段离岸有余量（否则又贴岸了） */
    if (!segmentHasClearance(prev, ins.p, CLEARANCE_MIN)) continue

    /* 先走到走廊锚点，再走到实测点 —— 两段都保证在水里 */
    if (!samePoint(prev, anchor) && segmentIsNavigable(prev, anchor, 2)) {
      out.push(anchor)
    }
    out.push(ins.p)
  }
  /* 补上走廊剩余部分 */
  for (let i = ci; i < corridor.length; i++) {
    const p = corridor[i]
    if (samePoint(out[out.length - 1], p)) continue
    if (!segmentIsNavigable(out[out.length - 1], p, 2)) {
      /* 接不上就跳过（宁可断开也不要穿陆） */
      continue
    }
    out.push(p)
  }
  return out.length >= 2 ? out : corridor
}

/**
 * 某点的「离岸余量」（掩码格）。
 *
 * 用距离变换场取值 —— 数值越大表示离陆地越远。
 * 超出 `CLEARANCE_SAT` 的部分不再细分（够用即可）。
 */
export function clearanceCells(e: number, n: number): number {
  const [px, py] = enuToMapPx(e, n)
  const d = clearance()
  const cx = Math.min(MASK_COLS - 1, Math.max(0, Math.floor(px / MASK_CELL)))
  const cy = Math.min(MASK_ROWS - 1, Math.max(0, Math.floor(py / MASK_CELL)))
  return d[cy * MASK_COLS + cx]
}

/**
 * 判断某段线是否不仅全程在水里，而且**离岸余量达标**。
 *
 * 【为什么需要】
 * `segmentIsNavigable()` 只保证「不穿陆」，
 * 因此直线简化会把航线拉得紧贴岸边 —— 船看起来像在蹭堤坝。
 *
 * 这里额外要求：线段上每隔一小段取样，
 * 到岸距离都必须 ≥ `minCells` 个掩码格。
 *
 * @param a,b       ENU 端点
 * @param minCells  最小离岸余量（掩码格）
 * @param stepPx    采样步长（底图像素）
 */
function segmentHasClearance(
  a: [number, number],
  b: [number, number],
  minCells: number,
  stepPx = 6,
): boolean {
  const [x0, y0] = enuToMapPx(a[0], a[1])
  const [x1, y1] = enuToMapPx(b[0], b[1])
  const len = Math.hypot(x1 - x0, y1 - y0)
  const steps = Math.max(1, Math.ceil(len / stepPx))
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    const e = a[0] + (b[0] - a[0]) * t
    const n = a[1] + (b[1] - a[1]) * t
    if (!isWaterPx(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)) return false
    if (clearanceCells(e, n) < minCells) return false
  }
  return true
}

/**
 * 「视线可达」路径简化。
 *
 * 从起点出发，每次向前找到**最远的水域直线可达点**，跳到那里，
 * 重复直到终点。中间无法直线到达的锯齿点被丢弃。
 *
 * 【为什么安全】
 * 每一跳都经过 `segmentIsNavigable()` 验证，
 * 所以简化后的路径**每一段都保证在水里**。
 *
 * 【效果】
 * A* 逐格路径（近千点）→ 简化后通常几十点，
 * 渲染开销大幅下降，线条也更顺。
 */
function simplifyWaterPath(pts: [number, number][]): [number, number][] {
  if (pts.length < 3) return pts
  const out: [number, number][] = [pts[0]]
  let i = 0
  while (i < pts.length - 1) {
    /* 从后往前找最远的直线可达点。
     *
     * 【两级判定】
     * 1. 必须在水里（硬性）；
     * 2. **尽量**离岸有余量（软性）。
     *
     * 若只判第 1 条，直线简化会把航线拉得紧贴岸边，
     * 船看起来像在蹭堤坝 —— 这正是用户反馈「不要贴着岸边」。
     * 因此先找满足余量的最远点；若找不到，再退回「只在水里」的最远点。
     */
    let best = -1
    let fallback = i + 1
    for (let k = pts.length - 1; k > i; k--) {
      if (!segmentIsNavigable(pts[i], pts[k], 3)) continue
      if (fallback === i + 1) fallback = k
      if (segmentHasClearance(pts[i], pts[k], CLEARANCE_MIN)) {
        best = k
        break
      }
    }
    if (best < 0) best = fallback
    out.push(pts[best])
    i = best
  }
  return out
}

/**
 * 最终兜底扫描：消除残余的穿陆段。
 *
 * 对每个不合法段：
 *   1. 把两端点投影到最近水域；
 *   2. 沿线取若干中间点，逐个投影到水域；
 *   3. 只保留「与前一合法点连线合法」的那些点。
 *
 * 这样得到的路径虽然可能不是最优绕行，但**保证每一段都在水里**。
 */
function finalSafetySweep(pts: [number, number][]): [number, number][] {
  if (pts.length < 2) return pts
  const out: [number, number][] = []
  let prev: [number, number] | null = null

  for (let i = 0; i < pts.length; i++) {
    let p = projectToWater(pts[i])
    if (prev && !segmentIsNavigable(prev, p, 2)) {
      /* 尝试插入中间点：在线段上取 3 个候选点，依次投影到水域 */
      const a = prev
      const b = p
      let inserted = false
      for (const frac of [0.25, 0.5, 0.75]) {
        const mid = projectToWater([
          a[0] + (b[0] - a[0]) * frac,
          a[1] + (b[1] - a[1]) * frac,
        ])
        if (segmentIsNavigable(a, mid, 2)) {
          out.push(mid)
          prev = mid
          inserted = true
          break
        }
      }
      if (!inserted) {
        /* 连中间点都不行 → 用「两点各自最近水域格心」强行连接，
         * 至少两端各自在水中。 */
        if (!samePoint(prev, p)) out.push(p)
        prev = p
        continue
      }
    }
    if (!prev || !samePoint(prev, p)) {
      out.push(p)
      prev = p
    }
  }
  return out.length >= 2 ? out : pts
}

/**
 * 折线圆角处理 —— **带水域校验**的安全版本。
 *
 * 【与 `smoothPolyline()` 的区别】
 * 原版 Chaikin 无条件切角，结果是把路径推离水域。
 * 这里对每个候选切角点单独检查：
 *   · 若切角后的两点连线仍全程在水里 → 采用；
 *   · 否则保留原折点。
 */
function roundCornersSafe(pts: [number, number][], rounds: number): [number, number][] {
  let cur = pts.map((p) => [p[0], p[1]] as [number, number])
  for (let r = 0; r < rounds; r++) {
    if (cur.length < 3) break
    const out: [number, number][] = [cur[0]]
    for (let i = 1; i < cur.length - 1; i++) {
      const prev = cur[i - 1]
      const p = cur[i]
      const next = cur[i + 1]
      /* 切角点：在 prev→p 与 p→next 的 1/4 处取点 */
      const q1: [number, number] = [
        prev[0] * 0.75 + p[0] * 0.25, prev[1] * 0.75 + p[1] * 0.25,
      ]
      const q2: [number, number] = [
        p[0] * 0.75 + next[0] * 0.25, p[1] * 0.75 + next[1] * 0.25,
      ]
      /* 只有两段切角连线都合法时才替换掉原折点。
       *
       * 【还要检查离岸余量】
       * 切角会把折线**往弯道内侧**推 —— 而弯道内侧往往就是岸。
       * 只判「在水里」的话，航线会被一路拉回岸边，
       * 船只看起来像在蹭堤坝。
       *
       * 因此这里额外要求：切角后的线段离岸余量也要达标。
       */
      if (
        segmentIsNavigable(prev, q1, 2) &&
        segmentIsNavigable(q1, q2, 2) &&
        segmentIsNavigable(q2, next, 2) &&
        segmentHasClearance(prev, q1, CLEARANCE_MIN) &&
        segmentHasClearance(q1, q2, CLEARANCE_MIN) &&
        segmentHasClearance(q2, next, CLEARANCE_MIN)
      ) {
        out.push(q1, q2)
      } else {
        out.push(p)
      }
    }
    out.push(cur[cur.length - 1])
    cur = out
  }
  return cur
}

/**
 * 折线平滑（Chaikin 切角法）。
 *
 * 航迹看起来"合理"的关键在于**转角圆滑**，而不是折线尖锐。
 */
function smoothPolyline(pts: [number, number][], passes: number): [number, number][] {
  let cur = pts.slice()
  for (let p = 0; p < passes; p++) {
    if (cur.length < 3) break
    const out: [number, number][] = [cur[0]]
    for (let i = 0; i < cur.length - 1; i++) {
      const p0 = cur[i]
      const p1 = cur[i + 1]
      const outP: [number, number] = [(p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2]
      const outQ: [number, number] = [(p0[0] * 3 + p1[0]) / 4, (p0[1] * 3 + p1[1]) / 4]
      out.push(outQ, outP)
    }
    out.push(cur[cur.length - 1])
    cur = out
  }
  return cur
}

/**
 * 统计一条航迹落在陆地上的采样点比例。
 *
 * 用于自检：修正后应接近 0。
 */
export function landRatio(pts: [number, number][]): number {
  if (!pts.length) return 0
  let bad = 0
  for (const p of pts) if (!isNavigableEnu(p[0], p[1])) bad++
  return bad / pts.length
}

/**
 * 覆盖本模块能力范围。
 *
 * 把一个点强制推到可航水域（用于船只精灵的位置修正）。
 */
export function clampToWater(p: [number, number]): [number, number] {
  return projectToWater(p)
}

/**
 * 把一个点推到**离岸有余量**的水域（船只精灵的位置修正）。
 *
 * 【与 `clampToWater` 的区别】
 * `clampToWater` 只保证「在水里」，可能落在紧贴岸边的一格宽缝隙里 ——
 * 船就会「骑」在堤坝上。
 *
 * 这里要求离岸余量 ≥ `minCells` 个掩码格。
 *
 * 【选点策略：先找「最近的达标点」，找不到再退到「余量最大的点」】
 * 为什么要退到余量最大的点：
 *   实测点常常落在角落/窄缝里（例如 T004 在画面右上角的窄水带，
 *   周边余量只有 1 格）。这时 BFS 找不到任何达标点，
 *   若直接退回 `clampToWater`（最近水域），船还是会贴在岸边。
 *   所以兜底应当选**搜索范围内余量最大**的格子 ——
 *   把它推到开阔水面，正是用户要的「在水中游动、不贴岸」。
 *
 * @param p         ENU 点
 * @param minCells  最小离岸余量（掩码格）
 */
export function clampToOpenWater(
  p: [number, number],
  minCells = CLEARANCE_MIN,
): [number, number] {
  if (isNavigableEnu(p[0], p[1]) && clearanceCells(p[0], p[1]) >= minCells) {
    return [p[0], p[1]]
  }

  const [px, py] = enuToMapPx(p[0], p[1])
  const g = grid()
  const cols = MASK_COLS
  const rows = MASK_ROWS
  const cx = Math.floor(px / MASK_CELL)
  const cy = Math.floor(py / MASK_CELL)
  const clr = clearance()

  /**
   * 搜索半径上限（掩码格）。
   *
   * 不能无限扩搜 —— 否则会把船推到很远的地方，
   * 偏离实测位置太多（实测 T004 在画面右上角，
   * 无限制搜索会把它推到画面中部的开阔海面，位移近 500px）。
   *
   * 取 12 格（≈96 底图像素 ≈ 90 米），
   * 既能脱离窄缝，又不会让船「跳」到别处。
   */
  const MAX_RADIUS = 12

  /**
   * BFS 找「余量达标」的水域格。
   *
   * 全部水域格都要走一遍（而不是找到第一个就停），
   * 这样才能在找不到达标点时，选出余量最大的那一个。
   */
  const seen = new Uint8Array(cols * rows)
  const q: number[] = []
  /* 达标格中余量最大的一个 */
  let bestCell = -1
  let bestClear = -1
  /* 全部水域格中余量最大的一个（兜底） */
  let fallback = -1
  let fallbackClear = -1

  const visit = (x: number, y: number): void => {
    if (x < 0 || y < 0 || x >= cols || y >= rows) return
    /* 半径限制必须在「标记已访问」之前判断，
     * 否则圈外的格子会被提前标记成已访问，
     * 导致后续从另一侧进入圈内的路径被误判为「已访问」而漏掉。 */
    if (Math.abs(x - cx) > MAX_RADIUS || Math.abs(y - cy) > MAX_RADIUS) return
    const i = y * cols + x
    if (seen[i]) return
    seen[i] = 1
    if (g[i] !== 1) return
    const c = clr[i]
    if (c > bestClear) {
      bestClear = c
      bestCell = i
    }
    if (c > fallbackClear) {
      fallbackClear = c
      fallback = i
    }
    q.push(i)
  }

  visit(cx, cy)
  let head = 0
  while (head < q.length) {
    const i = q[head++]
    const x = i % cols
    const y = Math.floor(i / cols)
    visit(x + 1, y)
    visit(x - 1, y)
    visit(x, y + 1)
    visit(x, y - 1)
  }

  /* 达标格优先；全都不达标时用余量最大的格 */
  const chosen = bestClear >= minCells ? bestCell : fallback
  if (chosen < 0) return projectToWater(p)
  const bx = chosen % cols
  const by = Math.floor(chosen / cols)
  return mapPxToEnu((bx + 0.5) * MASK_CELL, (by + 0.5) * MASK_CELL)
}

/**
 * 压到**最近**的、离岸余量达标的水域格。
 *
 * 【与 `clampToOpenWater()` 的区别 —— 这正是瞬移的根源】
 *
 * `clampToOpenWater()` 在搜索半径内挑「离岸余量**最大**」的格子。
 * 问题在于：这个选择与点的位置**无关**，
 * 于是同一条航迹上相邻的两个点可能被推到完全不同的水域：
 *
 *   实测 T002 末段（整段落在厂区陆域内）：
 *     (−725, 4667) → (−190.6, 4100.8)
 *     (−574, 4768) → ( 109.4, 4339.2)
 *     (−875, 4543) → (−828.1, 4082.5)
 *
 * 三个落点彼此相距数百米 —— 画面上就是「船在陆区附近来回瞬移 + 穿模」。
 *
 * 本函数改成挑「**离原点最近**的达标格」：
 *   · 落点离实测点尽可能近 → 相邻点的落点也尽可能接近 → 连续；
 *   · 余量只要达标即可，不追求最优 → 不会为了「更靠海中央」而把船推远。
 *
 * 兜底：半径内没有任何达标格时，退回 `clampToOpenWater()`
 * （此时只能在「余量最大」与「位置」之间取舍，优先保证船在水里）。
 */
export function clampToOpenWaterNearest(
  p: [number, number],
  minCells = CLEARANCE_MIN,
): [number, number] {
  /* 点本身就在可航水域且余量达标 → 直接返回 */
  if (isNavigableEnu(p[0], p[1]) && clearanceCells(p[0], p[1]) >= minCells) {
    return [p[0], p[1]]
  }

  const [px, py] = enuToMapPx(p[0], p[1])
  const g = grid()
  const cols = MASK_COLS
  const rows = MASK_ROWS
  const cx = Math.floor(px / MASK_CELL)
  const cy = Math.floor(py / MASK_CELL)
  const clr = clearance()

  /* 与 clampToOpenWater 相同的搜索半径 */
  const MAX_RADIUS = 12

  /**
   * BFS —— 按「距离原点的格数」逐层扩展，
   * 因此**第一次**找到的达标格就是最近的达标格。
   *
   * 用队列分层实现：每处理完一层就检查是否已有达标格。
   */
  const seen = new Uint8Array(cols * rows)
  const q: number[] = []
  let layerStart = 0
  let layerEnd = 0
  let layer = 0
  let fallback = -1
  let fallbackClear = -1

  const visit = (x: number, y: number): void => {
    if (x < 0 || y < 0 || x >= cols || y >= rows) return
    if (Math.abs(x - cx) > MAX_RADIUS || Math.abs(y - cy) > MAX_RADIUS) return
    const i = y * cols + x
    if (seen[i]) return
    seen[i] = 1
    if (g[i] !== 1) return
    const c = clr[i]
    if (c > fallbackClear) {
      fallbackClear = c
      fallback = i
    }
    q.push(i)
  }

  visit(cx, cy)
  layerEnd = q.length

  while (layerStart < q.length) {
    /* 当前层的所有格子距离相同 —— 找到的第一个达标格就是最近的 */
    for (let i = layerStart; i < layerEnd; i++) {
      const idx = q[i]
      if (clr[idx] >= minCells) {
        const bx = idx % cols
        const by = Math.floor(idx / cols)
        return mapPxToEnu((bx + 0.5) * MASK_CELL, (by + 0.5) * MASK_CELL)
      }
    }
    /* 扩展到下一层 */
    for (let i = layerStart; i < layerEnd; i++) {
      const idx = q[i]
      const x = idx % cols
      const y = Math.floor(idx / cols)
      visit(x + 1, y)
      visit(x - 1, y)
      visit(x, y + 1)
      visit(x, y - 1)
    }
    layerStart = layerEnd
    layerEnd = q.length
    layer++
    if (layer > MAX_RADIUS * 2) break
  }

  /* 半径内没有达标格 → 退回「余量最大」的兜底 */
  if (fallback >= 0) {
    const bx = fallback % cols
    const by = Math.floor(fallback / cols)
    return mapPxToEnu((bx + 0.5) * MASK_CELL, (by + 0.5) * MASK_CELL)
  }
  return projectToWater(p)
}

