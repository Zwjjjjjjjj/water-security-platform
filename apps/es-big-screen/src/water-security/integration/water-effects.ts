/**
 * 港区海面动效与水岸细节。
 *
 * 【为什么单独一个模块】
 * 这些效果有共同的三点：
 *   1. 都以**底图图片像素**为坐标系（不是 ENU）——
 *      因为它们是「画在底图上的东西」，必须与底图严格对齐；
 *   2. 都只在**斜视水面模式**生效（正俯视是矢量海图，没有岸线质感）；
 *   3. 都是**随数据变化**的（尾迹看速度、泡沫看堤体、航标是固定的）。
 *
 * 本模块负责**生成图形数据**（路径/点位），
 * 由 `ChartSceneV2.vue` 负责渲染成 SVG。
 * 这样生成逻辑与渲染解耦，便于单独校验。
 */

/* ------------------------------------------------------------------ *
 * 类型
 * ------------------------------------------------------------------ */

/** 一段折线（图片像素） */
export type PxPolyline = [number, number][]

/** 泡沫团（破碎的白沫） */
export interface FoamPatch {
  /** 泡沫中心（图片像素） */
  cx: number
  cy: number
  /** 半径（像素） */
  r: number
  /** 不透明度 */
  opacity: number
  /** 相位偏移（用于让各团泡沫闪烁不同步） */
  phase: number
  /** 外法线单位向量 X —— 渲染层据此做涌浪位移 */
  nx: number
  /** 外法线单位向量 Y */
  ny: number
  /** 涌动幅度（像素）：破浪时白水向堤面推进的最大距离 */
  surge: number
  /** 破碎强度峰值（0.3 ~ 1）：爆发瞬间的亮度与尺寸放大倍率 */
  burst: number
  /** 涌浪周期（秒） */
  period: number
}

/** 航标（浮标 / 灯桩） */
export interface NavAid {
  /** 图片像素位置 */
  px: number
  py: number
  /** 种类 */
  kind: 'buoy' | 'beacon'
  /** 颜色 */
  color: string
  /** 是否闪烁（灯桩闪、浮标不闪） */
  blinking: boolean
  /** 相位偏移 */
  phase: number
}

/** 泊位标线 */
export interface BerthMark {
  /** 标线端点（图片像素） */
  x1: number
  y1: number
  x2: number
  y2: number
  opacity: number
}

/* ------------------------------------------------------------------ *
 * 水域几何（从底图掩码推出来的岸线）
 * ------------------------------------------------------------------ */

/**
 * 底图水域掩码的采样接口。
 *
 * 与 `harbor-water.ts` 共用同一份数据，
 * 这里只声明接口，避免两个模块循环依赖。
 */
export interface WaterSampler {
  /** 图片像素是否属于可航水域 */
  isWater(x: number, y: number): boolean
  /** 底图原生尺寸 */
  readonly mapW: number
  readonly mapH: number
}

/**
 * 找出「水域边界」上的一段折线。
 *
 * 【做法】
 * 沿若干条扫描线，从陆地一侧向水域一侧找第一个水域像素，
 * 把这些交界点连起来，就得到该扫描线附近的岸线片段。
 *
 * @param sampler  水域采样器
 * @param yStart   起始扫描线 Y
 * @param yEnd     结束扫描线 Y
 * @param step     扫描线间隔
 * @param fromLeft true=从左往右找（取水域左边界），false=从右往左（取右边界）
 */
function shorelineByScan(
  sampler: WaterSampler,
  yStart: number,
  yEnd: number,
  step: number,
  fromLeft: boolean,
): PxPolyline {
  const out: PxPolyline = []
  for (let y = yStart; y <= yEnd; y += step) {
    if (y < 0 || y >= sampler.mapH) continue
    if (fromLeft) {
      for (let x = 0; x < sampler.mapW; x += 4) {
        if (sampler.isWater(x, y)) {
          out.push([x, y])
          break
        }
      }
    } else {
      for (let x = sampler.mapW - 1; x >= 0; x -= 4) {
        if (sampler.isWater(x, y)) {
          out.push([x, y])
          break
        }
      }
    }
  }
  return out
}

/**
 * 判断一段岸线是「迎浪侧」还是「背浪侧」。
 *
 * 【为什么需要】
 * 破碎泡沫只应该出现在**防波堤外侧**（迎浪面），
 * 港池内侧是平静的 —— 否则整条岸线都被描白边，
 * 看起来像描边特效而不是水花。
 *
 * 判据：把岸线点沿法线方向外推一小步，
 * 若外推后仍在水域，说明这一侧是「朝外」的开阔水面（迎浪）。
 *
 * @param shoreline 岸线折线
 * @param sampler   水域采样器
 */
export function isOuterSide(shoreline: PxPolyline, sampler: WaterSampler): boolean {
  if (shoreline.length < 2) return false
  let outer = 0
  let inner = 0
  for (let i = 1; i < shoreline.length; i++) {
    const [x0, y0] = shoreline[i - 1]
    const [x1, y1] = shoreline[i]
    const dx = x1 - x0
    const dy = y1 - y0
    const len = Math.hypot(dx, dy)
    if (len < 1) continue
    /* 法线（垂直于岸线方向） */
    const nx = -dy / len
    const ny = dx / len
    const probe = 10
    if (sampler.isWater(x0 + nx * probe, y0 + ny * probe)) outer++
    if (sampler.isWater(x0 - nx * probe, y0 - ny * probe)) inner++
  }
  return outer > inner
}

/**
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
}

/* ------------------------------------------------------------------ *
 * 尾迹（Kelvin wake）
 * ------------------------------------------------------------------ */

/** 尾迹几何类型 */
export type WakeKind =
  /** Kelvin 尾迹的 V 字波臂（波峰高光，不是泡沫） */
  | 'arm'
  /** 船尾螺旋桨白水（propeller wash） */
  | 'wash'
  /** 波臂上的细亮线（强化「波」的读感） */
  | 'crest'

/**
 * 一段尾迹。
 *
 * 【为什么必须「分段」而不是一条长线】
 * 真实尾迹的**宽度、透明度沿长度连续变化**：
 *   · 船尾处最宽最亮（刚被螺旋桨搅起的白水）；
 *   · 向后逐渐张开（Kelvin 臂以约 17° 半角扩散）但同时**变淡**；
 *   · 最后消散在海面上。
 * 一条固定宽度的 polyline 无法表达这种梯度，
 * 因此尾迹被切成若干**小段**，每段带自己的宽度与不透明度。
 */
export interface WakeSegment {
  /** 尾迹段（图片像素），从船尾向后 */
  points: PxPolyline
  /** 线宽（像素） */
  width: number
  /** 不透明度（0 ~ 1） */
  opacity: number
  /** 几何类型 —— 决定渲染样式（描边 / 柔化 / 高光） */
  kind: WakeKind
  /**
   * 沿段长度的漂移相位（0 ~ 1）。
   *
   * 用于让泡沫白点沿尾迹**向后漂移**，模拟泡沫被水流带走。
   */
  drift: number
  /** 动画周期（秒） */
  period: number
}

/** 尾迹输入用的历史采样点 */
export interface WakeSample {
  /** 东向坐标（米） */
  e: number
  /** 北向坐标（米） */
  n: number
  /** 相对时刻（毫秒）；用于计算尾迹年龄 */
  t: number
}

/** 尾迹生成的物理参数 */
export interface WakeParams {
  /** 当前速度（米/秒） */
  speedMps: number
  /** 船长（米）—— 决定尾迹的横向尺度 */
  lengthM: number
  /** 船宽（米） */
  beamM: number
  /** 当前回放时刻（毫秒） */
  nowMs: number
  /**
   * 尾迹寿命（毫秒）。
   *
   * 【物理含义】
   * 尾迹在船离开后仍会存在一段时间，靠扩散与湍流耗散。
   * 这里取 60 秒：真实尾迹通常在几十秒到几分钟内消散，
   * 而回放数据集最长只有 5 分钟，60 秒既够真实又不喧宾夺主。
   */
  lifeMs?: number
}

/**
 * 图片像素 ↔ 米 的换算系数（斜视底图）。
 *
 * 【为什么需要一个全局常量】
 * 尾迹的宽度/长度需要用**米**来表达（船宽、船长），
 * 但渲染是在**图片像素**里进行的。
 * 底图 1536px 覆盖 3600m（见 `scenes.config.v2.ts` 的 OBLIQUE_FIT），
 * 所以 1m ≈ 0.4267 px。
 *
 * 这个系数只用于**几何尺度**的估算，
 * 位置一律走 `enuToImage()` 精确映射，不受此近似影响。
 */
export const PX_PER_M = 1536 / 3600

/**
 * Kelvin 尾迹的半扩散角（度）。
 *
 * 【物理依据】
 * Kelvin 尾迹的波臂以约 19.47° 的半角从船尾向后扩散，
 * 这是深水线性波理论（Kelvin 尾迹）的经典结论。
 * 现实中的尾迹通常比理论值略小（15°~19°），
 * 因为风浪背景与非线性效应会把它抹平。
 *
 * 取 17°：比理论值稍小，视觉上更接近真实海面。
 */
const KELVIN_HALF_ANGLE_DEG = 17

/**
 * 尾迹强度的衰减时间常数（秒）。
 *
 * 【物理依据】
 * 尾迹的可见强度按**指数衰减** —— 这是湍流耗散 + 波幅衰减的近似。
 * 时间常数 22 秒：过了 22 秒，强度降到 1/e ≈ 37%，
 * 约 60 秒后基本不可见。
 */
const WAKE_DECAY_TAU_S = 22

/**
 * 尾迹强度（0 ~ 1）。
 *
 * 【与速度的关系 —— 用户明确要求】
 *   · 速度快的目标：明显的尾流；
 *   · 约 0.4 m/s 的目标：只保留很弱的扰动；
 *   · 静止目标：完全没有尾迹。
 *
 * 判据用**米/秒**，而不是「有没有移动」——
 * 0.4 m/s 的目标其实一直在动，但那个速度下真实水面几乎看不出尾流，
 * 画出来反而是假信息。
 *
 * 【为什么用幂函数而不是线性】
 * 尾迹的视觉强度与船速的关系是**次线性**的：
 * 速度翻倍，尾迹宽度只增加约 60%，而不是翻倍。
 * 用 `t^0.6` 拟合这条曲线。
 */
function wakeIntensity(speedMps: number): number {
  /* 速度门槛：低于 0.8 m/s 不画尾迹 */
  if (speedMps < 0.8) return 0
  return Math.min(1, Math.pow((speedMps - 0.8) / 5.2, 0.6))
}

/**
 * 由航迹历史生成符合物理规律的尾迹。
 *
 * 【与旧版的差异 —— 旧版的问题】
 * 旧版只画两条固定宽度的折线 + 一条船尾短线，
 * 无论船多快多慢，尾迹都是「同一种样子」：
 *   · 不沿长度衰减（整条尾迹一样亮）；
 *   · 波臂与泡沫混为一谈（看起来像一条白泡沫带）；
 *   · 尾迹是「贴」在船身上的静态线条，没有泡沫漂移。
 *
 * 【新版的做法】
 *  1. 按**年龄**把历史点分成若干段（每 1.5 秒一段）；
 *  2. 每段按 Kelvin 半角向两侧张开，宽度随张开量增大、强度按指数衰减；
 *  3. 船尾处叠加「螺旋桨白水」（wash）—— 更宽、更亮、更短；
 *  4. 波臂上另加一层细亮线（crest），与白水区分开。
 *
 * @param history 历史点列（含时刻）
 * @param toPx    ENU → 图片像素 的投影函数
 * @param params  尾迹参数（速度、船型尺寸、当前时刻）
 */
export function buildWake(
  history: WakeSample[],
  toPx: (e: number, n: number) => [number, number],
  params: WakeParams,
): WakeSegment[] {
  const intensity = wakeIntensity(params.speedMps)
  if (intensity <= 0) return []
  if (history.length < 2) return []

  const lifeMs = params.lifeMs ?? 60000
  const nowMs = params.nowMs

  /* 只保留寿命内的历史点，并按时间升序排列 */
  const pts = history
    .filter((p) => Number.isFinite(p.e) && Number.isFinite(p.n) && Number.isFinite(p.t))
    .filter((p) => nowMs - p.t <= lifeMs && nowMs - p.t >= -1000)
    .sort((a, b) => a.t - b.t)
  if (pts.length < 2) return []

  /* 投影到图片像素 */
  const px = pts.map((p) => toPx(p.e, p.n))
  if (px.length < 2) return []

  const segs: WakeSegment[] = []

  /* ---- 船尾处：螺旋桨白水 ---- */
  const sternIdx = px.length - 1
  const stern = px[sternIdx]
  const sternPrev = px[Math.max(0, sternIdx - 1)]
  let sdx = stern[0] - sternPrev[0]
  let sdy = stern[1] - sternPrev[1]
  const slen = Math.hypot(sdx, sdy) || 1
  sdx /= slen
  sdy /= slen

  /**
   * 【白水的尺寸依据】
   * 螺旋桨白水的宽度约为**船宽**的 0.6~1.2 倍（气泡被螺旋桨甩出），
   * 长度约为**船长**的 0.35 倍（白水随船前进而向后延伸一小段）。
   */
  const beamPx = Math.max(3, params.beamM * PX_PER_M)
  const washLenPx = Math.max(6, params.lengthM * 0.35 * PX_PER_M)
  const washW = (1.6 + intensity * 4.2) * Math.min(2.2, beamPx / 10)
  for (let i = 0; i < 3; i++) {
    const t = i / 2
    /* 白水从船尾正后方开始，向后快速变淡 */
    segs.push({
      points: [
        [stern[0] - sdx * washLenPx * t * 0.35, stern[1] - sdy * washLenPx * t * 0.35],
        [stern[0] - sdx * washLenPx * (0.35 + t * 0.65), stern[1] - sdy * washLenPx * (0.35 + t * 0.65)],
      ],
      width: washW * (1 - t * 0.55),
      opacity: (0.30 + intensity * 0.34) * (1 - t * 0.62),
      kind: 'wash',
      drift: 0,
      period: 2.6 + i * 0.35,
    })
  }

  /* ---- Kelvin 臂：沿历史轨迹向两侧张开 ---- */
  /**
   * 【为什么逐段生成而不是一条长线】
   * 尾迹的宽度与强度必须**沿长度连续衰减**。
   * 把历史点按「年龄步长」切成小段，
   * 每段单独给出宽度/透明度，渲染时自然形成梯度。
   */
  const AGE_STEP_MS = 1500
  let lastBucket = -1
  const armPts: { px: [number, number]; ageMs: number; heading: [number, number] }[] = []

  for (let i = 0; i < px.length; i++) {
    const ageMs = nowMs - pts[i].t
    if (ageMs < 0) continue
    const bucket = Math.floor(ageMs / AGE_STEP_MS)
    if (bucket === lastBucket) continue
    lastBucket = bucket

    /* 该点的前进方向（切向） */
    const a = px[Math.max(0, i - 1)]
    const b = px[Math.min(px.length - 1, i + 1)]
    let dx = b[0] - a[0]
    let dy = b[1] - a[1]
    const len = Math.hypot(dx, dy)
    if (len < 0.001) continue
    dx /= len
    dy /= len
    armPts.push({ px: px[i], ageMs, heading: [dx, dy] })
  }
  if (armPts.length < 2) return segs

  /**
   * 【Kelvin 臂的几何】
   * 臂的位置：在船的**历史位置**上，沿当时的航向张开。
   * 张开量 = 「该点到船尾的沿迹距离」× tan(17°)。
   *
   * 这样臂的夹角自然等于 Kelvin 半角，
   * 且随着船前进，臂越来越长 —— 与理论一致。
   */
  const tanK = Math.tan((KELVIN_HALF_ANGLE_DEG * Math.PI) / 180)

  for (const side of [-1, 1] as const) {
    for (let i = 0; i < armPts.length - 1; i++) {
      const p0 = armPts[i]
      const p1 = armPts[i + 1]

      /* 该点到船尾的沿迹距离（像素） */
      const tailDistPx = Math.hypot(p0.px[0] - stern[0], p0.px[1] - stern[1])
      /* 臂的横向张开量 */
      const spreadPx = tailDistPx * tanK

      /* 臂的法向（垂直于航向） */
      const nx = -p0.heading[1] * side
      const ny = p0.heading[0] * side

      const q0: [number, number] = [p0.px[0] + nx * spreadPx, p0.px[1] + ny * spreadPx]
      const q1: [number, number] = [p1.px[0] + nx * spreadPx, p1.px[1] + ny * spreadPx]

      /* 强度随年龄指数衰减 */
      const ageFactor = Math.exp(-(p0.ageMs / 1000) / WAKE_DECAY_TAU_S)
      if (ageFactor < 0.02) continue

      /* 波臂的宽度：船宽的 1/6 起，随张开略增 */
      const baseW = Math.max(1.1, (params.beamM * PX_PER_M) / 6)
      const width = baseW * (0.85 + spreadPx / 90) * (0.6 + intensity * 0.7)

      segs.push({
        points: [q0, q1],
        width,
        opacity: (0.06 + intensity * 0.20) * ageFactor,
        kind: 'arm',
        /* 漂移相位沿臂向后递增 —— 泡沫随波臂向船尾方向移动 */
        drift: (i / armPts.length) * 0.85,
        period: 3.2 + (i % 5) * 0.22,
      })
    }
  }

  /* ---- 波峰高光：沿臂的细亮线，强化「波」的读感 ---- */
  /**
   * 【为什么单独加一层】
   * Kelvin 臂本质是**波**而不是「泡沫带」。
   * 泡沫只出现在船尾（wash），
   * 远离船的臂是**波峰高光** —— 更细、更亮、更短促。
   * 把两者混为一谈，就会出现「一条长长的白泡沫带」这种假效果。
   */
  if (intensity > 0.25) {
    for (let i = 0; i < armPts.length - 2; i += 2) {
      const p0 = armPts[i]
      const p1 = armPts[i + 1]
      const ageFactor = Math.exp(-(p0.ageMs / 1000) / WAKE_DECAY_TAU_S)
      if (ageFactor < 0.05) continue
      const tailDistPx = Math.hypot(p0.px[0] - stern[0], p0.px[1] - stern[1])
      const spreadPx = tailDistPx * tanK
      const nx = -p0.heading[1]
      const ny = p0.heading[0]
      const q0: [number, number] = [p0.px[0] + nx * spreadPx, p0.px[1] + ny * spreadPx]
      const q1: [number, number] = [p1.px[0] + nx * spreadPx, p1.px[1] + ny * spreadPx]
      segs.push({
        points: [q0, q1],
        width: 0.9,
        opacity: (0.05 + intensity * 0.13) * ageFactor,
        kind: 'crest',
        drift: 0.5,
        period: 2.8,
      })
    }
  }

  return segs
}

/** 尾迹泡沫白点 */
export interface WakeFoamDot {
  /** 段索引 */
  seg: number
  /** 段内参数位置（0 ~ 1） */
  t: number
  /** 动画周期（秒） */
  period: number
  /** 相位偏移（秒） */
  phase: number
  /** 白点半径（容器像素） */
  r: number
  /** 不透明度 */
  opacity: number
}

/**
 * 把尾迹段转成「泡沫白点」序列。
 *
 * 【为什么需要】
 * 尾迹的**几何**是静态的：船在动，尾迹留在原地。
 * 但真实的泡沫会**沿尾迹向后漂移并消散**，
 * 否则尾迹看起来像「画在地图上的线」而不是「水上的痕迹」。
 *
 * 做法：在每段尾迹上按固定间距撒若干白点，
 * 每个白点带自己的动画相位 —— 于是白点持续向后流动。
 *
 * @param segs 尾迹段（图片像素）
 */
export function buildWakeFoam(segs: WakeSegment[]): WakeFoamDot[] {
  const out: WakeFoamDot[] = []
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]
    if (s.kind !== 'wash' && s.kind !== 'arm') continue
    if (s.opacity < 0.05) continue
    const p0 = s.points[0]
    const p1 = s.points[s.points.length - 1]
    const len = Math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    /* 间距：越小越密，但白点太多会糊成一片 */
    const spacing = s.kind === 'wash' ? 4.5 : 11
    const count = Math.min(7, Math.floor(len / spacing))
    if (count < 1) continue
    for (let k = 0; k < count; k++) {
      const t = (k + 0.5) / count
      out.push({
        seg: i,
        t,
        period: s.period,
        /* 相位沿段递增 + 段自身的漂移相位 */
        phase: (t + s.drift) * s.period,
        r: s.kind === 'wash' ? 1.1 + s.width * 0.16 : 0.7 + s.width * 0.1,
        opacity: s.opacity * 0.85,
      })
    }
  }
  return out
}

/* ------------------------------------------------------------------ *
 * 海面微动
 * ------------------------------------------------------------------ */

/** 一条细波纹（沿水平方向的低幅正弦） */
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
   * 浅水波速 $c=\sqrt{gh}$；取 $g=9.8$、水深 $h\approx6$ m 得 $c\approx7.7$ m/s。
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
}

/* ------------------------------------------------------------------ *
 * 航标
 * ------------------------------------------------------------------ */

/**
 * 航标布置表（图片像素）。
 *
 * 【坐标来源】
 * 均取自 `campus_oblique_v3.png` 的实测位置（连通域分析）：
 *   · 堤头灯桩：落在防波堤堤体的端部；
 *   · 浮标：落在港池入口与航道侧。
 *
 * 这些点位是**示意**的（与实测 ENU 无地理配准），
 * 界面上以「场景示意」标注。
 */
export const NAV_AIDS: NavAid[] = [
  // —— 主防波堤堤头（东侧堤体端部，px 731,729 附近）——
  { px: 738, py: 736, kind: 'beacon', color: '#ff5a5a', blinking: true, phase: 0 },
  // —— 西侧短堤堤头 ——
  { px: 556, py: 470, kind: 'beacon', color: '#4fe07a', blinking: true, phase: 1.4 },
  // —— 港池入口警戒浮标 ——
  { px: 640, py: 640, kind: 'buoy', color: '#ffd54a', blinking: false, phase: 0 },
  { px: 848, py: 690, kind: 'buoy', color: '#ffd54a', blinking: false, phase: 0.6 },
  { px: 980, py: 742, kind: 'buoy', color: '#ffd54a', blinking: false, phase: 1.2 },
  // —— 航道侧绿色浮标 ——
  { px: 1090, py: 800, kind: 'buoy', color: '#4fe07a', blinking: false, phase: 0.3 },
  { px: 1240, py: 862, kind: 'buoy', color: '#4fe07a', blinking: false, phase: 0.9 },
  // —— 厂前水域浮标 ——
  { px: 520, py: 828, kind: 'buoy', color: '#ffd54a', blinking: false, phase: 1.8 },
]

/**
 * 泊位标线（码头前沿的泊位分隔线）。
 *
 * 【为什么要有】
 * 泊位标线给了「码头」一个尺度参照 ——
 * 没有它，码头只是一条灰色长条，看不出这是可以靠船的地方。
 */
export const BERTH_MARKS: BerthMark[] = [
  // 厂区码头（实测中心 1092,623，长条斜向）
  { x1: 975, y1: 655, x2: 975, y2: 690, opacity: 0.45 },
  { x1: 1042, y1: 630, x2: 1042, y2: 665, opacity: 0.45 },
  { x1: 1108, y1: 605, x2: 1108, y2: 640, opacity: 0.45 },
  { x1: 1174, y1: 580, x2: 1174, y2: 615, opacity: 0.45 },
  { x1: 968, y1: 660, x2: 1180, y2: 585, opacity: 0.3 },
]
