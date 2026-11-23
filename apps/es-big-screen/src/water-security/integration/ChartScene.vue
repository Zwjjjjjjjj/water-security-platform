<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, watch, computed, ref, nextTick } from 'vue'
import { loadSceneAssets, getSceneAssets, type SceneAsset } from './scene-assets'
import { shipImageMatrix, svgMatrix, groundSpriteMatrix, type ShipSpriteInfo } from './scene-placement'
import { NPP_PLACEMENTS, HARBOR_VECTOR_PLACEMENTS, BACKGROUND_SHIPS } from './scenes.config'
import { makeNavigable, blockedKind, isMoored, navigableDisplayPosition } from './navigation'
import {
  STRUCTURES,
  CHANNEL_WALLS,
  PIERS,
  OFFSHORE_STRUCTURES,
  VERTICAL_PROPS,
  SUN_DIR,
  type StructureSegment,
  type VerticalProp,
} from './structures'
import * as echarts from 'echarts'

const props = defineProps({
  snapshot: { type: Object, required: true },
  selectedTargetId: { type: String, default: 'T001' },
  layers: { type: Object, default: () => ({ coverage: true, source_tracks: true, fused_tracks: true }) },
  follow: { type: Boolean, default: false },
  paused: { type: Boolean, default: false },
  mode: { type: String, default: 'overview' },
})
const emit = defineEmits(['select-target'])

const el = shallowRef<HTMLElement | null>(null)
/** .ws-scene-3d：承载 perspective 的 3D 上下文 */
const sceneEl = shallowRef<HTMLElement | null>(null)
/** .ws-ground：承载 rotateX/scale 的倾斜地面 */
const groundEl = shallowRef<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let ro: ResizeObserver | null = null

// 站心 ENU,单位 m。全景包络 E -2600~1000 / N 3000~5000,兼容 T002 远端
const BASE_X: [number, number] = [-2600, 1000]
const BASE_Y: [number, number] = [3000, 5000]
const GRID = { left: 56, right: 18, top: 44, bottom: 40 }
/** 2.5D 模式下坐标层使用零边距，使底图与轨迹层完全重合（避免透视错位） */
const gridFor = () => (view25d.value ? { left: 8, right: 8, top: 8, bottom: 8 } : GRID)
const isUnder = computed(() => props.mode === 'underwater')

const scaleLabel = ref('0　0.5公里　1.0公里')
const scalePx = ref(120)


/**
 * 底图锚定参数（米制）：
 * 设计坐标 dx∈[0,1000]、dy∈[0,500]（y 向下）映射到米制
 *   mE = ANCHOR_E + dx * UNIT_E
 *   mN = ANCH_N  - dy * UNIT_N
 * 底图内容因此锚定在固定的真实米制位置，缩放/平移时不会漂移出视口。
 */
const BG_ANCHOR = { e: -2200, n: 4900 }
/** 每个设计单位对应的米数：1000 设计单位 = 3400 米（东向），500 设计单位 = 1900 米（北向） */
const BG_UNIT = { e: 3.4, n: 3.8 }
/**
 * 底图 <g> 的 transform：设计坐标 → 图表容器像素。
 *
 * 【重要修复】此前底图用「米制 viewBox + scale(kE,-kN)」自建了一套坐标系，
 * 而 SVG 的 y 轴向下、ECharts 的 y 轴向上，两者方向相反，
 * 导致底图相对航迹层上下翻转（实测核岛处偏差 573px、底图原点处 776px）。
 * 视图一变（缩放/平移/切 2.5D），两套内容就会错开，看起来像两张独立的地图。
 *
 * 现改为与 projectEnu() 完全一致的设计坐标 → 像素映射：
 *   px = g.left + (mE - xMin) * pxPerM
 *   py = g.top + (yMax - mN) * pxPerM
 * 其中 mE / mN 由 designToMeter() 给出，
 * 于是底图、岸线、建筑、目标、航迹全部落在同一套 ENU 框架内。
 */
const bgTransform = computed(() => {
  const g = gridFor()
  const W = el.value?.clientWidth || chartSize.value.w || 960
  const v = viewRange.value
  const plotW = Math.max(50, W - g.left - g.right)
  const pxPerM = plotW / (v.xMax - v.xMin)
  // 设计坐标原点（dx=0, dy=0）在容器中的像素位置
  const tx = g.left + (BG_ANCHOR.e - v.xMin) * pxPerM
  const ty = g.top + (v.yMax - BG_ANCHOR.n) * pxPerM
  // 每个设计单位对应的像素（东向 / 北向，均为正值 → 与图表同向）
  const kx = BG_UNIT.e * pxPerM
  const ky = BG_UNIT.n * pxPerM
  return `translate(${tx.toFixed(2)},${ty.toFixed(2)}) scale(${kx.toFixed(6)},${ky.toFixed(6)})`
})
/**
 * 底图标签定位：把设计坐标换算为米制，再换算为图表容器内的像素位置。
 * 标签层不参与 3D 倾斜，始终正立清晰；位置随视图缩放/平移实时同步。
 */
interface BgLabel {
  dx: number
  dy: number
  text: string
  fill: string
  size: number
  weight?: string
  anchor?: 'start' | 'middle' | 'end'
  /** 不透明度（用于区分文字层级：区域名最弱、设施名次之） */
  opacity?: number
  /** 是否使用胶囊底衬（默认 true；区域名等大段文字可关掉） */
  chip?: boolean
  /** 设计坐标所属分组（用于继承 translate 偏移） */
  group?: 'factory' | 'intake'
}
const BG_LABELS: BgLabel[] = [
  // —— 区域名称（最弱层，低透明度）——
  { dx: 120, dy: 20, text: '西侧山体', fill: '#6f9d92', size: 13, opacity: 0.7 },
  { dx: 660, dy: 20, text: '北侧山体', fill: '#6f9d92', size: 13, opacity: 0.7 },
  { dx: 470, dy: 352, text: '核电厂厂前水域', fill: '#5a93a8', size: 14, opacity: 0.55 },
  { dx: 830, dy: 292, text: '外海域', fill: '#4d7f95', size: 13, opacity: 0.5 },
  // —— 设施名称（次之层）——
  { dx: 300, dy: 132, text: '核岛', fill: '#d8f4fc', size: 13, group: 'factory' },
  { dx: 490, dy: 126, text: '常规岛厂房', fill: '#c8e8f6', size: 13, group: 'factory' },
  { dx: 668, dy: 122, text: '冷却塔', fill: '#a8d8ea', size: 12, group: 'factory' },
  { dx: 790, dy: 118, text: '开关站', fill: '#a8d8ea', size: 12, group: 'factory' },
  { dx: 900, dy: 108, text: '输电铁塔', fill: '#a8d8ea', size: 11, group: 'factory' },
  { dx: 336, dy: 190, text: '取水口', fill: '#8fe0b8', size: 12, group: 'intake' },
  { dx: 596, dy: 196, text: '排水口', fill: '#f0c08a', size: 12, group: 'intake' },
  { dx: 296, dy: 158, text: '取水泵房', fill: '#a8d8ea', size: 11, group: 'intake' },
  // —— 构筑物 / 区域（弱化）——
  { dx: 848, dy: 238, text: '东长堤', fill: '#7fb6c9', size: 12, opacity: 0.8 },
  { dx: 242, dy: 244, text: '西短堤', fill: '#7fb6c9', size: 12, opacity: 0.8 },
  { dx: 656, dy: 404, text: '警戒巡逻航线', fill: '#5fc6dd', size: 13, opacity: 0.7 },
  { dx: 424, dy: 302, text: '前池湾', fill: '#5fc6dd', size: 13, opacity: 0.65 },
  { dx: 300, dy: 396, text: '警戒巡逻区', fill: '#5fb6cf', size: 13, opacity: 0.6 },
  { dx: 720, dy: 378, text: '温排水混合区', fill: '#c9a86a', size: 13, opacity: 0.65 },
  { dx: 210, dy: 300, text: '警戒浮标', fill: '#c9ac72', size: 11, opacity: 0.8 },
  { dx: 880, dy: 440, text: '等深线', fill: '#3f7d9b', size: 12, opacity: 0.6 },
]

/** 设计坐标 → 米制坐标 */
function designToMeter(dx: number, dy: number): [number, number] {
  return [BG_ANCHOR.e + dx * BG_UNIT.e, BG_ANCHOR.n - dy * BG_UNIT.n]
}

/** 当前视图下，米制坐标 → 图表容器像素位置 */
function meterToPixel(e: number, n: number): [number, number] {
  const v = viewRange.value
  const g = gridFor()
  const { w } = plotSize()
  const plotW = Math.max(50, w - g.left - g.right)
  const pxPerM = plotW / (v.xMax - v.xMin)
  return [g.left + (e - v.xMin) * pxPerM, g.top + (v.yMax - n) * pxPerM]
}

/** 图表容器尺寸（未变换像素），供船体贴图 / 底图 SVG 视口使用。
 * el 为 shallowRef，clientWidth 变化不会触发重算，故用独立响应式状态并在 resize 时更新。 */
const chartSize = ref({ w: 960, h: 500 })

/**
 * 按文本查找 BG_LABELS 中的标签定义（取其配色与字号）。
 * 2.5D 下厂区标签嵌套在建筑立牌内部，需要立牌的局部坐标来定位，
 * 但样式数据仍与 2D 共用同一份来源，避免两套样式漂移。
 */
function factoryLabelOf(text: string): BgLabel | null {
  if (!text) return null
  const norm = (t: string) => t.replace(/[（(].*?[)）]/g, '').trim()
  const key = norm(text)
  return BG_LABELS.find((l) => norm(l.text) === key) ?? null
}

/**
 * 标签层：设计坐标 → 屏幕像素位置。
 *
 * 2D 模式下：直接用图表容器坐标（与航迹一致）。
 * 2.5D 模式下：底图保持 2D 正俯视，仅建筑与船只等物件做立体抬升；
 *              标签层在场景之外，必须经过同一套 3D 投影，
 *              才能与场景内的建筑/航迹落在同一位置（否则文字会「浮」在图形上方）。
 */
const bgLabelPositions = computed(() => {
  const g = gridFor()
  const { w: W, h: H } = plotSize()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value
  // 像素/米（两轴一致，等比例）
  const pxPerM = plotW / (v.xMax - v.xMin)

  /* 先算出每个标签的理想位置，再做避让。
   *
   * 避让规则（按优先级从高到低）：
   *   1. 设施名称（group=factory/intake）优先占位，位置不允许挪动
   *      （它们挂在建筑立牌内部，物理上不能脱开）；
   *   2. 区域名称优先级低，若被遮挡则沿垂直方向上下微移；
   *   3. 实在挪不开则隐藏。
   */
  const items = BG_LABELS.map((lb) => {
    /* 标签坐标直接就是它在场景中的位置：
     * 厂区标签已与建筑锚点对齐（dx/dy 相同），不再叠加历史偏移。 */
    const [mE, mN] = designToMeter(lb.dx, lb.dy)
    // 地面平面坐标（与航迹层同一坐标系）
    const gx = g.left + (mE - v.xMin) * pxPerM
    const gy = g.top + (v.yMax - mN) * pxPerM

    /* 标签放在屏幕空间，位置由 projectScene 把地面点投影得到，
     * 因此标签与倾斜后的地面要素严格重合，且始终正立可读。 */
    const [sx, sy] = projectScene(gx, gy)

    // 仅 2.5D 下厂区标签由建筑立牌内部承载，此处排除避免重复；
    // 2D 模式下立牌层不渲染，厂区标签必须照常显示。
    const dupInBillboard = view25d.value && factoryLabelOf(lb.text)?.group === 'factory'
    return {
      ...lb,
      x: sx,
      y: sy,
      priority: lb.group ? 0 : 1,
      inPlot: sx > g.left - 60 && sx < W - g.right + 60 && sy > g.top - 20 && sy < H - g.bottom + 20,
      visible: !dupInBillboard,
    }
  })

  /* —— 避让 —— */
  // 估算标签占位（胶囊内边距 3px×2 + 边框 1px×2）
  const padX = 10
  const padY = 6
  const boxes: { x1: number; x2: number; y1: number; y2: number }[] = []

  const hit = (x1: number, y1: number, x2: number, y2: number) =>
    boxes.some((b) => Math.min(x2, b.x2) - Math.max(x1, b.x1) > 1 && Math.min(y2, b.y2) - Math.max(y1, b.y1) > 1)

  // 高优先级先占位
  items.sort((a, b) => a.priority - b.priority)
  for (const it of items) {
    if (!it.visible || !it.inPlot) continue
    const w = it.text.length * (it.size || 13) + padX
    const h = (it.size || 13) + padY
    let x1 = it.x - w / 2
    let x2 = it.x + w / 2
    let y1 = it.y - h / 2
    let y2 = it.y + h / 2
    if (hit(x1, y1, x2, y2)) {
      // 沿垂直方向找空位（上下各试 4 档）
      let placed = false
      for (const d of [1, -1, 2, -2, 3, -3, 4, -4]) {
        const ny1 = y1 + d * (h + 4)
        const ny2 = ny1 + h
        if (!hit(x1, ny1, x2, ny2)) {
          y1 = ny1; y2 = ny2; placed = true; break
        }
      }
      if (!placed) {
        it.visible = false
        continue
      }
      it.y = (y1 + y2) / 2
    }
    boxes.push({ x1, x2, y1, y2 })
  }
  return items
})

/**
 * 船舶像素尺寸：由真实船长（米）按当前米/像素比例换算。
 * 保持最小/最大边界，避免极小或极端缩放时符号失真。
 */
function shipPixelSize(t: any, isSel: boolean) {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH) // 米/像素
  const lenM = t.length_m ?? SHIP_LEN_M[t.ship_type] ?? 60
  const px = lenM / s
  const base = isSel ? 26 : 18
  // 以真实尺寸为主，并限制在 [12, 90] 像素区间
  return Math.max(12, Math.min(90, Math.max(px, base)))
}

function circlePts(r: number, n = 128) {
  const pts: number[][] = []
  for (let i = 0; i <= n; i++) {
    const a = (i / n) * Math.PI * 2
    pts.push([Math.cos(a) * r, Math.sin(a) * r])
  }
  return pts
}
// bearing line from station outward. bearing: 0=北(+N), 90=东(+E)
function bearingLine(b: number, r = 6000) {
  const a = (b * Math.PI) / 180
  return [[0, 0], [Math.sin(a) * r, Math.cos(a) * r]]
}

/**
 * 俯视船符（艏向朝上/北），随 course 旋转。
 * 分层绘制：船体轮廓 + 驾驶室 + 艏楼，比单一三角更易辨识。
 */
const shipPath = 'path://M0,-15 L5.5,2 L6.5,10 L3,13 L0,11 L-3,13 L-6.5,10 L-5.5,2 Z'
/** 船体内部结构（驾驶室等），使用浅色描边叠加 */
const shipDetailPath = 'path://M0,-7 L2.6,1 L2.6,6 L-2.6,6 L-2.6,1 Z'
/** 船舶类型对应的船体长度（米），用于计算像素尺寸 */
const SHIP_LEN_M: Record<string, number> = { cargo: 90, tanker: 140, container: 160, fishing: 24, service: 30 }

/* ===== 场景素材（图层化接入） ===== */
/** 已加载的场景素材清单 */
const sceneAssets = shallowRef<SceneAsset[]>([])
/** 素材加载状态：ready 表示清单已就绪 */
const assetsReady = ref(false)

/**
 * 目标 → 船型素材 id 映射。
 * 素材按「示意模板尺寸」提供，不代表数据集目标真实船型/船长；
 * 未经核验的船型仅作示意显示，界面上以「示意」标注。
 */
const TARGET_SHIP_ASSET: Record<string, string> = {
  T001: 'container_160m_top',
  T002: 'cargo_90m_top',
  T003: 'bulk_180m_top',
  T004: 'fishing_24m_top',
  T005: 'engineering_60m_top',
}

/**
 * 米制 ENU → 图表容器像素 的投影函数。
 * 与 ECharts 坐标系严格一致：viewRange 已按等比例扩展过，
 * 故轴 min/max 直接对应绘图区左右/上下边缘，两轴共用同一 pxPerM。
 */
function projectEnu(eastM: number, northM: number): [number, number] {
  const g = gridFor()
  const { w } = plotSize()
  const plotW = Math.max(50, w - g.left - g.right)
  const v = viewRange.value
  const pxPerM = plotW / (v.xMax - v.xMin)
  return [g.left + (eastM - v.xMin) * pxPerM, g.top + (v.yMax - northM) * pxPerM]
}

/** 取素材的船体贴图信息（含轮廓范围与示意尺寸） */
function shipSpriteOf(assetId: string): ShipSpriteInfo | null {
  const a = getSceneAssets().find((x) => x.id === assetId)
  if (!a || !a.opaque_bbox_px || !a.length_m || !a.beam_m) return null
  return {
    id: a.id,
    url: a.url,
    width_px: a.width_px,
    height_px: a.height_px,
    opaque_bbox_px: a.opaque_bbox_px,
    length_m: a.length_m,
    beam_m: a.beam_m,
  }
}

/**
 * 地面建筑贴图列表。
 * 锚点为设计坐标，经 designToMeter 换算为米制后，与航迹共用同一投影函数定位，
 * 保证建筑与实测航迹在同一 ENU 框架内严格对齐。
 */
const nppSprites = computed(() => {
  if (!assetsReady.value) return [] as any[]
  const out: any[] = []
  for (const pl of NPP_PLACEMENTS) {
    if (pl.enabled === false) continue
    const a = getSceneAssets().find((x) => x.id === pl.id)
    if (!a) continue
    const [mE, mN] = designToMeter(pl.dx, pl.dy)
    // 贴图锚点像素位置（与航迹共用同一投影）
    const anchor = projectEnu(mE, mN)
    const pxPerM = pixelPerMeter()
    // 设计单位 → 像素
    const kx = pxPerM * BG_UNIT.e
    const ky = pxPerM * BG_UNIT.n
    // 目标显示尺寸（按 alpha 轮廓的占地，换算为像素）
    const targetW = pl.w * kx
    const targetH = pl.h * ky

    // 2D：贴图平铺在地面平面，与航迹共用同一 ENU 投影
    // （2.5D 时建筑改由立牌层承载，见 nppBillboards）
    const m = groundSpriteMatrix(a, anchor, targetW, targetH)
    out.push({
      key: pl.id,
      href: a.url,
      width: a.width_px,
      height: a.height_px,
      transform: `translate(${m[4].toFixed(2)},${m[5].toFixed(2)}) scale(${m[0].toFixed(6)},${m[3].toFixed(6)})`,
    })
  }
  return out
})

/**
 * 当前绘图区的尺寸（像素）。
 * 统一从这里取宽高，保证底图 transform、projectEnu、标签层、船体贴图
 * 用的是同一份数值，任何时候都不会各算各的而错位。
 */
function plotSize(): { w: number; h: number } {
  return { w: chartSize.value.w || el.value?.clientWidth || 960, h: chartSize.value.h || el.value?.clientHeight || 500 }
}

/** 当前视图的像素/米比例（两轴一致） */
function pixelPerMeter(): number {
  const g = gridFor()
  const { w } = plotSize()
  const plotW = Math.max(50, w - g.left - g.right)
  const v = viewRange.value
  return plotW / (v.xMax - v.xMin)
}

/**
 * 背景船只贴图（氛围元素，不参与交互与量测）。
 * 与实测目标船体共用同一套矩阵算法与同一 ENU 投影，仅尺寸单位换算不同：
 * 实测目标按米制长度显示，背景船只按设计单位指定长度。
 */
const backgroundShipSprites = computed(() => {
  if (!assetsReady.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  for (const bs of BACKGROUND_SHIPS) {
    const asset = shipSpriteOf(bs.id)
    if (!asset) continue
    const [mE, mN] = designToMeter(bs.dx, bs.dy)
    // 显示船长（米）：优先用配置值，否则用素材示意模板尺寸
    const lengthM = bs.lengthM && bs.lengthM > 0 ? bs.lengthM : asset.length_m
    const beamM = lengthM * (asset.beam_m / asset.length_m)
    try {
      const m = shipImageMatrix(
        asset,
        { east_m: mE, north_m: mN, course_deg: bs.course },
        projectEnu,
        { lengthM, beamM },
      )
      out.push({
        key: `${bs.id}-${bs.dx}-${bs.dy}`,
        href: asset.url,
        width: asset.width_px,
        height: asset.height_px,
        transform: svgMatrix(m),
        opacity: bs.opacity ?? 0.92,
      })
    } catch (err) {
      console.warn('[scenes] 背景船只矩阵计算失败', bs.id, err)
    }
  }
  return out
})

/**
 * 立体船体（2.5D）。
 *
 * 船体贴图原本是 SVG <image> 平铺在地面平面上，2.5D 下无法参与三维抬升
 * （SVG 内部没有 3D 上下文）。这里改为 HTML <img>：
 * 从原矩阵中解出「中心点 + 旋转角 + 显示尺寸」，
 * 再叠加 translateZ 抬升，船只便能像 RTS 游戏里一样立在 2D 底图的水面上。
 *
 * 矩阵形式为 [a, b, c, d, e, f]（SVG 2D 仿射）：
 *   缩放/旋转：x' = a·x + c·y + e，y' = b·x + d·y + f
 *   旋转角 = atan2(b, a)
 *   轮廓中心在素材内的局部坐标 cx, cy（opaque_bbox 中心）
 */
const shipBillboards = computed(() => {
  if (!assetsReady.value || !view25d.value) return [] as any[]
  const out: any[] = []
  const src = props.snapshot as any
  const targets = src?.targets || []

  const push = (key: string, asset: any, m: number[], opacity: number, selected: boolean) => {
    const [a, b, c, d, e, f] = m
    const [x0, y0, x1, y1] = asset.opaque_bbox_px
    const cx = (x0 + x1) / 2
    const cy = (y0 + y1) / 2
    // 轮廓中心经矩阵变换后的屏幕位置
    const cxp = a * cx + c * cy + e
    const cyp = b * cx + d * cy + f

    /* 素材的 x 轴为船长方向、y 轴为船宽方向；矩阵 M = [[a,c],[b,d]] 把它映射到屏幕。
     * 等价写法：整张素材按 |(a,b)| 与 |(c,d)| 缩放，再整体旋转 atan2(b,a)，
     * 中心落在轮廓中心。这样 HTML 元素即可 1:1 复现原 SVG 变换。 */
    const ang = (Math.atan2(b, a) * 180) / Math.PI
    const scaleX = Math.hypot(a, b)
    const scaleY = Math.hypot(c, d)

    /* 元素承载的是整张素材（含透明边距），其中心并不等于船体轮廓中心。
     * 必须把「轮廓中心」换算到「整图中心」的局部偏移，再定位元素，
     * 否则船体中心会偏离航迹点（实测可达 10+ px）。 */
    /* 显示放大：整体按 SHIP_DISPLAY_SCALE 放大，
     * 使船在大屏远景中仍有足够的辨识度。 */
    const fullW = scaleX * asset.width_px * SHIP_DISPLAY_SCALE
    const fullH = scaleY * asset.height_px * SHIP_DISPLAY_SCALE
    // 轮廓中心在整图局部坐标中的位置（未旋转）
    const localCx = cx - asset.width_px / 2
    const localCy = cy - asset.height_px / 2
    // 轮廓中心相对整图中心的偏移，经旋转后加到整图中心上
    const rad = (ang * Math.PI) / 180
    const offX = (localCx * scaleX) * Math.cos(rad) - (localCy * scaleY) * Math.sin(rad)
    const offY = (localCx * scaleX) * Math.sin(rad) + (localCy * scaleY) * Math.cos(rad)
    /* 地面点投影到屏幕（与地面层渲染完全一致的变换链），
     * 船体轮廓中心对齐投影后的航迹点，于是船「浮」在斜视水面上。 */
    const [sx, sy, sf] = projectScene(cxp, cyp)
    // 屏幕空间中的旋转：原矩阵把素材 x 轴映射到屏幕方向，
    // 地面倾斜后需重算（平面旋转 + 俯视压扁）。
    const dispAng = ang - SCENE_TILT_DEG * 0
    out.push({
      key,
      href: asset.url,
      left: sx - fullW * sf / 2,
      top: sy - fullH * sf / 2,
      w: fullW * sf,
      h: fullH * sf,
      angle: dispAng,
      opacity,
      selected,
      // 接地阴影：贴在地面平面坐标（航迹点正下方）
      shadow: { x: cxp, y: cyp, w: fullW * 0.9, h: Math.max(4, fullH * 0.22) },
      /* 船体侧板厚度：按船宽比例给出。
       * 船是漂浮物，侧板太薄会失去「浮在水面上」的体积感，
       * 这里按宽度的 16% 计算，并设下限保证小船也有可见厚度。 */
      hullH: Math.max(3, Math.min(26, fullH * sf * SHIP_HULL_RATIO)),
      depth: cyp,
    })
  }

  // 背景船（直接读配置，与 backgroundShipSprites 用同一套矩阵算法）
  for (const bs of BACKGROUND_SHIPS) {
    const asset = shipSpriteOf(bs.id)
    if (!asset) continue
    const [mE, mN] = designToMeter(bs.dx, bs.dy)
    const lengthM = bs.lengthM ?? asset.length_m
    const beamM = lengthM * (asset.beam_m / asset.length_m)
    try {
      const m = shipImageMatrix(
        asset,
        { east_m: mE, north_m: mN, course_deg: bs.course },
        projectEnu,
        { lengthM, beamM },
      )
      push(`${bs.id}-${bs.dx}-${bs.dy}`, asset, m, bs.opacity ?? 0.92, false)
    } catch (err) {
      console.warn('[scenes] 背景船立体化失败', bs.id, err)
    }
  }

  // 实测目标船
  for (const t of targets) {
    const assetId = TARGET_SHIP_ASSET[t.global_track_id]
    if (!assetId) continue
    const asset = shipSpriteOf(assetId)
    if (!asset) continue
    if (t.east_m == null || t.north_m == null || t.course_deg == null) continue
    try {
      /* 立体船体同样按「水侧合法位置」落位：
       * 实测坐标落在厂区陆域内时，移到最近的水侧泊位，
       * 避免船体悬浮在厂房之间。 */
      const disp = navigableDisplayPosition(t.east_m, t.north_m)
      const m = shipImageMatrix(
        asset,
        { east_m: disp.east, north_m: disp.north, course_deg: t.course_deg },
        projectEnu,
      )
      push(t.global_track_id, asset, m, t.global_track_id === props.selectedTargetId ? 1 : 0.88,
        t.global_track_id === props.selectedTargetId)
    } catch (err) {
      console.warn('[scenes] 目标船立体化失败', t.global_track_id, err)
    }
  }
  return out
})

/**
 * 建筑基座（场坪）。
 *
 * 建筑立牌虽然锚点精确，但深色建筑贴在深色陆地上仍然缺少「落在地上」的依托。
 * 这里在地面层绘制一块浅色场坪（设计坐标 → 与底图同一坐标系），
 * 场坪与建筑锚点重合，于是建筑自然「坐」在自己的场坪上。
 */
const nppPads = computed(() => {
  const out: any[] = []
  /* 必须依赖 assetsReady：素材清单是异步加载的，
   * 若不在 computed 里读它，首次渲染时清单还是空的，
   * 场坪就永远不会出现（且不会随素材加载完成而重算）。 */
  if (!assetsReady.value) return out
  for (const pl of NPP_PLACEMENTS) {
    if (pl.enabled === false) continue
    const assetId = pl.id25d || pl.id
    const a = getSceneAssets().find((x) => x.id === assetId)
    if (!a) continue
    /* 场坪画成菱形，与 45° 素材自带的菱形地坪同构，
     * 于是建筑正好「坐」在自己的场坪上，接缝完全吻合。 */
    const w = pl.w * 1.06
    const h = w * 0.5 // 45° 俯视菱形的高宽比约 1:2
    out.push({
      key: pl.id,
      x: pl.dx - w / 2,
      y: pl.dy - h / 2,
      w,
      h,
    })
  }
  return out
})

/**
 * 冷却塔水雾（蒸汽羽流）。
 *
 * 【作用】
 * 水雾是冷却塔最具辨识度的特征 —— 有了向上扩散的羽流，
 * 观众立刻能读出「这是一座冷却塔」，
 * 同时羽流的垂直延展把塔的高度「顶」了出来，
 * 让厂区在 2.5D 下有明确的垂直尺度。
 *
 * 位置：锚定在冷却塔顶部（与立牌顶边对齐），
 * 向上飘散；带轻微的横向漂移与呼吸动画，避免静止的呆板。
 */
const steamPlumes = computed(() => {
  if (!assetsReady.value || !view25d.value) return [] as any[]
  const asset = getSceneAssets().find((a) => a.id === 'steam_plume')
  if (!asset) return [] as any[]
  const tower = NPP_PLACEMENTS.find((p) => p.id === 'cooling_tower_top')
  if (!tower) return [] as any[]
  const [mE, mN] = designToMeter(tower.dx, tower.dy)
  const [gx, gy] = projectEnu(mE, mN)
  const [sx, sy, sf] = projectScene(gx, gy)
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n
  /* 羽流从塔顶（建筑立牌**可见轮廓**的顶端）向上升起。
   *
   * 【对齐方式】
   * 直接复用 nppBillboards 里已经算好的立牌几何 ——
   * 那里已经处理了素材透明留白、透视缩放、落地锚点补偿。
   * 在这里重复推导一遍只会引入偏差，
   * 所以改为从立牌数据里取「可见轮廓顶部」。
   */
  const bb = nppBillboards.value.find((b) => b.key === 'cooling_tower_top')
  if (!bb) return [] as any[]
  /* 立牌的可见轮廓上沿（nppBillboards 中 labelY 即轮廓顶部附近）。
   * 这里用「可见高度」反推：立牌底边是落地锚点，
   * 顶部按素材 opaque_bbox 的比例裁掉透明留白。 */
  /* 立牌矩形顶端为 bb.y；可见轮廓顶端 = bb.y + topInset × bb.h
   * （topInset 是 PNG 顶部透明留白占显示高度的比例）。 */
  const towerTopY = bb.y + bb.topInset * bb.h
  /* 塔的可见高度（轮廓顶 → 落地锚点） */
  const visibleH = bb.h * (1 - bb.topInset)
  const plumeW = bb.w * 1.12
  /* 羽流高度约为塔可见高度的 1.6 倍 ——
   * 真实核电站的水雾可高出塔顶几十米到上百米，
   * 是厂区最醒目的垂直特征。 */
  const plumeH = Math.max(46, visibleH * 1.6)
  return [
    {
      key: 'plume-cooling-tower',
      href: asset.url,
      left: bb.x + bb.w / 2 - plumeW / 2,
      /* 羽流底边落在塔的可见顶端 */
      top: towerTopY - plumeH,
      w: plumeW,
      h: plumeH,
      opacity: 0.34,
    },
  ]
})

/**
 * 构筑物基脚浪花。
 *
 * 【作用】
 * 堤坝的立面画完之后，堤体与水面之间是「硬碰硬」的交界 ——
 * 看起来像一块贴纸插进蓝色背景。
 *
 * 在堤脚外侧加一圈破碎的白浪，
 * 就有了「堤体挡浪、浪花碎在堤脚」的关系，
 * 水陆的交界立刻变得自然。
 *
 * 实现：沿构筑物中心线的外侧（南侧）生成短促的白色弧段，
 * 宽度与浪高随构筑物类型变化。
 */
const structureFoam = computed(() => {
  if (!view25d.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n
  for (const seg of [...STRUCTURES, ...OFFSHORE_STRUCTURES]) {
    if (seg.kind === 'beacon' || seg.kind === 'wall') continue
    const pts = resamplePolyline(seg.pts, 22)
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i]
      const b = pts[i + 1]
      const dx = (b[0] - a[0]) * kx
      const dy = (b[1] - a[1]) * ky
      const len = Math.hypot(dx, dy)
      if (len < 6) continue
      /* 浪花位于堤体外侧：沿中心线法线的南向分量偏移。
       * 法线取 (-ty, tx)，其 y 分量为正时即为南侧。 */
      let tx = dx / len
      let ty = dy / len
      const nx = -ty
      const ny = tx
      const sign = ny >= 0 ? 1 : -1
      const off = (seg.width / 2 + 2.2) * ky
      const a2: [number, number] = [a[0] + nx * sign * 2.2, a[1] + ny * sign * 2.2]
      const b2: [number, number] = [b[0] + nx * sign * 2.2, b[1] + ny * sign * 2.2]
      const [e1, n1] = designToMeter(a2[0], a2[1])
      const [e2, n2] = designToMeter(b2[0], b2[1])
      const [g1x, g1y] = projectEnu(e1, n1)
      const [g2x, g2y] = projectEnu(e2, n2)
      const ang = (Math.atan2(g2y - g1y, g2x - g1x) * 180) / Math.PI
      /* 浪花高度：主堤高、渠壁矮 */
      const foamH = seg.height * ky * 0.42
      out.push({
        key: `fm-${seg.kind}-${i}-${a[0]}`,
        x: (g1x + g2x) / 2,
        y: (g1y + g2y) / 2 + off * 0.35,
        w: Math.hypot(g2x - g1x, g2y - g1y) * 0.92,
        h: Math.max(2, foamH),
        angle: ang,
        opacity: seg.kind === 'breakwater' ? 0.55 : 0.32,
      })
    }
  }
  return out
})

/**
 * 颜色明暗调整。
 * amount > 0 提亮，< 0 压暗（范围 -1 ~ 1）。
 * 用于给竖向构件生成「受光面 / 背光面」的圆柱渐变。
 */
function shade(hex: string, amount: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return hex
  const n = parseInt(m[1], 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  const f = (v: number) => {
    if (amount >= 0) return Math.round(v + (255 - v) * amount)
    return Math.round(v * (1 + amount))
  }
  return `rgb(${f(r)}, ${f(g)}, ${f(b)})`
}

/**
 * 竖向构筑物（灯桩 / 桅杆）的屏幕位置。
 *
 * 【为什么放在屏幕空间】
 * 灯桩是「站着的」—— 高度方向垂直于地面。
 * 若放在倾斜的地面空间里，它会随地面一起被压扁，
 * 变成一根斜趴趴的短线，完全失去「立起来」的观感。
 *
 * 因此：底部锚点用 projectScene() 投影到屏幕（与地面严丝合缝），
 * 柱体本身则竖直向上画在屏幕空间 —— 这才是真正的竖杆。
 */
const verticalProps = computed(() => {
  if (!view25d.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n
  for (const vp of VERTICAL_PROPS) {
    const [mE, mN] = designToMeter(vp.dx, vp.dy)
    const [gx, gy] = projectEnu(mE, mN)
    const [sx, sy, sf] = projectScene(gx, gy)
    /* 柱体高度：设计单位 → 米 → 屏幕像素。
     * 竖直构件在透视下不会随地面压缩，
     * 所以这里只乘地面缩放，不乘 cos(倾角)。 */
    const hPx = vp.height * ky * sf
    const wPx = Math.max(2, vp.width * kx * sf)
    out.push({
      ...vp,
      key: vp.id,
      left: sx,
      top: sy,
      hPx,
      wPx,
      /* 接地阴影：柱脚在地面上的小椭圆 */
      shadow: { x: gx, y: gy, w: wPx * 1.5, h: Math.max(3, wPx * 0.55) },
    })
  }
  // 景深：靠南（gy 大）的先画（在后），靠北的压在上面
  out.sort((a, b) => b.top - a.top)
  return out
})

/**
 * 立体构筑物的接地阴影（2.5D）。
 *
 * 阴影投在西南侧（与 SUN_DIR 一致），
 * 让堤坝 / 渠壁 / 栈桥在地面上留下体积的「重量感」——
 * 没有阴影的立体物会显得贴在图上而不是坐在地上。
 */
const structureShadows = computed(() => {
  if (!view25d.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n
  for (const seg of [...STRUCTURES, ...CHANNEL_WALLS, ...PIERS, ...OFFSHORE_STRUCTURES]) {
    const pts = seg.pts
    // 阴影沿中心线生成，宽度比堤体略宽，高度按堤体宽度压缩
    const offX = SUN_DIR[0] * seg.height * 0.55
    const offY = SUN_DIR[1] * seg.height * 0.55
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i]
      const b = pts[i + 1]
      const dx = (b[0] - a[0]) * kx
      const dy = (b[1] - a[1]) * ky
      const len = Math.hypot(dx, dy)
      if (len < 0.5) continue
      const [e1, n1] = designToMeter(a[0] + offX, a[1] + offY)
      const [e2, n2] = designToMeter(b[0] + offX, b[1] + offY)
      const [g1x, g1y] = projectEnu(e1, n1)
      const [g2x, g2y] = projectEnu(e2, n2)
      out.push({
        key: `st-${seg.kind}-${i}-${a[0]}`,
        x: g1x,
        y: g1y,
        w: Math.hypot(g2x - g1x, g2y - g1y),
        h: Math.max(3, seg.width * ky * 0.5),
        angle: (Math.atan2(g2y - g1y, g2x - g1x) * 180) / Math.PI,
      })
    }
  }
  return out
})

/**
 * 接地阴影集合（2.5D）。
 * 汇总建筑与船只的阴影锚点，统一在贴地的阴影层里绘制，
 * 避免阴影随物体一起被抬离地面而失去「接触感」。
 */
const groundShadows = computed(() => {
  const out: any[] = []
  for (const nb of nppBillboards.value) {
    if (!nb.shadow) continue
    out.push({ key: 'npp-' + nb.key, ...nb.shadow })
  }
  for (const sb of shipBillboards.value) {
    if (!sb.shadow) continue
    out.push({ key: 'ship-' + sb.key, ...sb.shadow })
  }
  // 构筑物（堤坝 / 渠壁 / 栈桥）的落地阴影
  for (const ss of structureShadows.value) {
    out.push({ key: ss.key, ...ss })
  }
  // 竖向构筑物（灯桩 / 桅杆）的柱脚阴影
  for (const vp of verticalProps.value) {
    if (!vp.shadow) continue
    out.push({ key: 'vp-' + vp.key, ...vp.shadow })
  }
  return out
})

/**
 * 港区矢量结构图层（防波堤 / 航道 / 泊位）。
 * 这些是素材包原生 SVG（正投影），按设计坐标锚点摆放，与手绘底图同一坐标系。
 */
const harborVectorSprites = computed(() => {
  if (!assetsReady.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n
  for (const pl of HARBOR_VECTOR_PLACEMENTS) {
    if (pl.enabled === false) continue
    const url = `/water-security/assets/scenes/terrain/${pl.id}.svg`
    const [mE, mN] = designToMeter(pl.dx, pl.dy)
    const anchor = projectEnu(mE, mN)
    const w = pl.w * kx
    const h = pl.h * ky
    out.push({
      key: pl.id,
      href: url,
      width: w,
      height: h,
      transform: `translate(${(anchor[0] - w / 2).toFixed(2)},${(anchor[1] - h / 2).toFixed(2)})`,
      opacity: pl.opacity,
    })
  }
  return out
})

/**
 * 立体构筑物体量（防波堤 / 渠壁 / 栈桥）。
 *
 * 【设计思路】
 * 构筑物在底图 SVG 里是以设计坐标（1000×500）画的，
 * 而底图整体被 rotateX 倾斜。
 *
 * 若直接在 SVG 里画一个「有宽度的矩形条」，它仍然是**平贴**的 ——
 * 无论怎么画都只是二次元。
 *
 * 真正的立体需要三样东西：
 *   1. 顶面（堤顶 / 渠顶）：宽度 = w，高度在地面上；
 *   2. 侧面（迎光面 / 背光面）：从顶面边缘垂下来的立面；
 *   3. 立面与顶面的明暗差异 —— 这是体积感的关键。
 *
 * 实现方式：
 *   - 在「地面平面」里算出顶面多边形与侧面多边形（设计坐标）；
 *   - 侧面用 `translateZ(-h)` 压低到地面以下？不 —— 那样会陷进地面。
 *   - 正确做法：侧面多边形的顶边 = 顶面多边形的边，
 *     底边 = 顶面边沿「视线在地面上的投影方向」外扩 height 个单位。
 *   - 这样在 rotateX 之后，立面自然朝向观察者，形成真正的厚度。
 *
 * 这里的「外扩方向」取 (0, +1)（设计坐标 y 向下 = 南），
 * 即立面朝向南侧（观察者一侧）展开 —— 与俯视倾斜的透视方向一致。
 */

/** 构筑物顶面多边形（设计坐标）：沿中心线按宽度向两侧偏移 */
function structureTopFace(seg: StructureSegment): [number, number][] {
  const pts = resamplePolyline(seg.pts, 6)
  if (pts.length < 2) return []
  const half = seg.width / 2
  const left: [number, number][] = []
  const right: [number, number][] = []
  for (let i = 0; i < pts.length; i++) {
    const prev = pts[Math.max(0, i - 1)]
    const next = pts[Math.min(pts.length - 1, i + 1)]
    let tx = next[0] - prev[0]
    let ty = next[1] - prev[1]
    const len = Math.hypot(tx, ty) || 1
    tx /= len
    ty /= len
    // 法线（左侧 = (-ty, tx)）
    const nx = -ty * half
    const ny = tx * half
    left.push([pts[i][0] + nx, pts[i][1] + ny])
    right.push([pts[i][0] - nx, pts[i][1] - ny])
  }
  // 闭合：左半边 + 右半边反向
  return [...left, ...right.reverse()]
}

/**
 * 构筑物立面（侧面）多边形列表（设计坐标）。
 *
 * 每个面是 4 个点：顶边两点（顶面边缘）+ 底边两点（沿南向外扩 height）。
 * 返回的多边形用 path 的 M/L/Z 拼接。
 *
 * 【光照】
 * 光源来自西南（见 structures.ts 的 SUN_DIR）。
 * 立面若朝向西南 → 受光，用较亮的色；
 * 朝向东北 → 背光，用较暗的色。
 * 这样整个场景的明暗方向一致，立体感才成立。
 */
function structureSideFaces(seg: StructureSegment): { d: string; lit: boolean }[] {
  const pts = resamplePolyline(seg.pts, 6)
  if (pts.length < 2) return []
  const half = seg.width / 2
  const faces: { d: string; lit: boolean }[] = []
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i]
    const p1 = pts[i + 1]
    let tx = p1[0] - p0[0]
    let ty = p1[1] - p0[1]
    const len = Math.hypot(tx, ty) || 1
    tx /= len
    ty /= len
    const nx = -ty * half
    const ny = tx * half
    const aL = [p0[0] + nx, p0[1] + ny]
    const bL = [p1[0] + nx, p1[1] + ny]
    const aR = [p0[0] - nx, p0[1] - ny]
    const bR = [p1[0] - nx, p1[1] - ny]
    // 选出位于南侧（y 较大）的边作为朝观察者的立面
    const useLeft = (aL[1] + bL[1]) >= (aR[1] + bR[1])
    const t0 = useLeft ? aL : aR
    const t1 = useLeft ? bL : bR
    // 立面向南（+y）外扩 height
    const b0 = [t0[0], t0[1] + seg.height]
    const b1 = [t1[0], t1[1] + seg.height]
    /* 受光判定：立面的外法线（指向南侧）与光源方向点积为正 → 受光。
     * 光源来自西南（x 为负、y 为正）。 */
    const faceNx = 0
    const faceNy = useLeft ? 1 : -1
    const lit = faceNx * SUN_DIR[0] + faceNy * SUN_DIR[1] > 0
    faces.push({
      d:
        `M${t0[0].toFixed(1)},${t0[1].toFixed(1)} L${t1[0].toFixed(1)},${t1[1].toFixed(1)} ` +
        `L${b1[0].toFixed(1)},${b1[1].toFixed(1)} L${b0[0].toFixed(1)},${b0[1].toFixed(1)} Z`,
      lit,
    })
  }
  return faces
}

/** 折线重采样（按最大步长插入中间点） */
function resamplePolyline(pts: [number, number][], maxStep: number): [number, number][] {
  if (pts.length < 2) return pts.slice()
  const out: [number, number][] = [pts[0]]
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i]
    const b = pts[i + 1]
    const d = Math.hypot(b[0] - a[0], b[1] - a[1])
    const n = Math.max(1, Math.ceil(d / maxStep))
    for (let k = 1; k <= n; k++) {
      const t = k / n
      out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
    }
  }
  return out
}

/**
 * 立体构筑物图层数据。
 *
 * 只在 2.5D 下生成（2D 保持平面描边，避免破坏原有平面图的可读性）。
 */
const structureVolumes = computed(() => {
  if (!view25d.value) return [] as any[]
  const all: StructureSegment[] = [
    ...STRUCTURES,
    ...CHANNEL_WALLS,
    ...PIERS,
    ...OFFSHORE_STRUCTURES,
  ]
  const out: any[] = []
  for (const seg of all) {
    const top = structureTopFace(seg)
    if (!top.length) continue
    const sides = structureSideFaces(seg)
    out.push({
      key: `${seg.kind}-${seg.pts[0][0]}-${seg.pts[0][1]}`,
      kind: seg.kind,
      topPath: `M${top.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L')} Z`,
      sides,
      top: seg.top,
      side: seg.side,
      /** 受光面：比顶面暗、比背光面亮 */
      litSide: seg.litFill ?? '#4a6070',
      edge: seg.edge,
      height: seg.height,
      /* 绘制顺序：北 → 南。观察者从南向北看，
       * 南侧的体量应压在北侧之上，遮挡关系才正确。 */
      depth: seg.pts.reduce((acc, p) => acc + p[1], 0) / seg.pts.length,
    })
  }
  out.sort((a, b) => a.depth - b.depth)
  return out
})

/**
 * 2.5D 建筑立牌层。
 *
 * 底图是 2D 正俯视平面，正俯视贴图平铺其上没有立体感。
 * 这里把建筑改为「立牌」：
 * 底部锚定在地面点，向上立起并正对屏幕。
 *
 * 立牌置于 3D 场景内部，叠加与场景变换互逆的反向旋转，
 * 于是锚点随地面透视一起移动（旋转时不脱开），而立牌本身保持正立不压扁。
 * 厂区标签嵌套在立牌内部，物理上不可能与建筑分离。
 */
const nppBillboards = computed(() => {
  if (!assetsReady.value || !view25d.value) return [] as any[]
  const out: any[] = []
  const pxPerM = pixelPerMeter()
  const kx = pxPerM * BG_UNIT.e
  const ky = pxPerM * BG_UNIT.n

  for (const pl of NPP_PLACEMENTS) {
    if (pl.enabled === false) continue
    // 2.5D 使用 45° 固定视角素材（README 指定用于竖直展示牌）
    const assetId = pl.id25d || pl.id
    const a = getSceneAssets().find((x) => x.id === assetId)
    if (!a) continue
    const [mE, mN] = designToMeter(pl.dx, pl.dy)
    const ground = projectEnu(mE, mN)

    /* 素材盒 = 整张 PNG；可见轮廓 = alpha 非透明区域（opaque_bbox）。
     * 两者不等：PNG 四周有透明留白，直接按整图摆放会让比例失真。
     *
     * 【落地锚点】
     * 45° 素材的结构是「建筑 + 一块菱形地坪」，整图底边尖角只是地坪的一个角。
     * 以底角为锚点，建筑会整体浮在地坪之上（悬空）。
     * 因此：
     *   - 缩放基准：地坪水平对角线（plateW）→ 换算成设计单位的占地 w，
     *     这样「平面占地尺寸」与「屏幕上的宽度」一致；
     *   - 落地锚点：地坪中心（base）→ 建筑恰好坐在自己的地坪上。 */
    const bb = a.opaque_bbox_px ?? [0, 0, a.width_px, a.height_px]
    const anchorPx = pl.base ?? [((bb[0] + bb[2]) / 2), bb[3]]
    const plateW = pl.plateW ?? Math.max(1, bb[2] - bb[0])
    const sc = (pl.w * kx) / plateW        // 素材缩放系数（像素/素材像素）
    const boxW = a.width_px * sc           // 整图显示尺寸
    const boxH = a.height_px * sc
    // 落地锚点在盒内的局部坐标
    const bx = anchorPx[0] * sc
    const by = anchorPx[1] * sc

    /* 地面锚点投影到屏幕（与地面层渲染完全一致的变换链）。
     * 立牌放在屏幕空间，落地锚点直接对齐投影后的地面点，
     * 于是「立」在斜视地面上，落点却与地面严丝合缝。 */
    const [sx, sy, sf] = projectScene(ground[0], ground[1])
    const dispW = boxW * sf
    const dispH = boxH * sf
    const x = sx - bx * sf
    const y = sy - by * sf

    /* 建筑体量参数。
     *
     * 【为什么要加体量】
     * 45° 素材本身已经是「有立面的透视图」，
     * 单看它是一张图；但当它「立」在倾斜地面上时，
     * 底部会与地面之间缺少过渡 —— 看上去像贴纸浮在纸上。
     *
     * 解决办法：在素材下方补一段**基座体**（棱台），
     * 顶端与素材底边重合、底端贴在地面上，
     * 于是建筑就有了「从地里长出来」的实体感。 */
    const massH = (pl.massH ?? 0.34) * dispH

    out.push({
      key: pl.id,
      href: a.url,
      x,
      y,
      w: dispW,
      h: dispH,
      /* 基座体：顶边宽 = 素材底边宽，底边略宽（外扩 8%），
       * 高 massH。向下延伸，用于与地面衔接。 */
      massH,
      massInset: dispW * 0.04,
      // 基座配色：偏暗的混凝土色，与地面阴影自然衔接
      massFill: pl.massFill ?? '#2c3d49',
      massSide: pl.massSide ?? '#1b2833',
      // 景深排序：靠南（dy 大）的建筑画在前面，遮挡靠北的建筑
      depth: pl.dy,
      /* 接地阴影：与建筑的菱形地坪同宽、同中心，
       * 只是略小一圈（像投影有扩散），贴在地面上，不与建筑底座脱开。 */
      shadow: { x: ground[0], y: ground[1], w: pl.w * kx * 0.9, h: Math.max(5, pl.w * kx * 0.13) },
      /* 可见轮廓顶部相对立牌顶边的比例（0 = 顶部即轮廓顶，1 = 顶部全是留白）。
       * 用于把需要「贴到建筑顶端」的元素（标签、水雾、塔顶灯）
       * 精确对齐到轮廓而不是 PNG 的透明边缘。 */
      topInset: bb[1] * sc * sf / Math.max(1, boxH * sf),
      // 标签锚点（立牌局部坐标）：贴在建筑顶端正上方，随立牌一起走，绝不脱开
      labelX: bx * sf,
      labelY: bb[1] * sc * sf - 6,
      // 标签文案与样式取自 BG_LABELS（与 2D 模式同一份数据源）
      label: factoryLabelOf(pl.label ?? '')?.text ?? pl.label ?? '',
      labelFill: factoryLabelOf(pl.label ?? '')?.fill ?? '#d8f4fc',
      labelSize: factoryLabelOf(pl.label ?? '')?.size ?? 13,
    })
  }
  // 由北向南绘制，保证南侧建筑压住北侧建筑
  out.sort((a, b) => a.depth - b.depth)
  return out
})

/**
 * 当前回放时刻的船体贴图列表。
 * 缺少航向的目标不绘制贴图（按规范不伪造朝向），仅保留交点标记。
 */
const shipSprites = computed(() => {
  if (!assetsReady.value) return [] as any[]
  const out: any[] = []
  const targets = (props.snapshot as any)?.targets || []
  for (const t of targets) {
    const assetId = TARGET_SHIP_ASSET[t.global_track_id]
    if (!assetId) continue
    const asset = shipSpriteOf(assetId)
    if (!asset) continue
    if (t.east_m == null || t.north_m == null || t.course_deg == null) continue
    try {
      const m = shipImageMatrix(asset, { east_m: t.east_m, north_m: t.north_m, course_deg: t.course_deg }, projectEnu)
      out.push({
        key: t.global_track_id,
        href: asset.url,
        width: asset.width_px,
        height: asset.height_px,
        transform: svgMatrix(m),
        selected: t.global_track_id === props.selectedTargetId,
      })
    } catch (err) {
      // 投影失败时跳过该目标，不影响其余渲染
      console.warn('[scenes] 船体矩阵计算失败', t.global_track_id, err)
    }
  }
  return out
})
// 轨迹末端运动箭头
const arrowPath = 'path://M0,-10 L6,6 L0,2 L-6,6 Z'

function targetColor(id: string) {
  if (id === props.selectedTargetId) return '#ffe14d'
  if (id === 'T001') return '#22e4ee'
  if (id === 'T002') return '#4da6ff'
  return '#7ef0c1'
}

function isVisible(t: any) {
  if (props.mode === 'overview' || props.mode === 'replay') return true
  return (t.domain || 'surface') === props.mode
}

/** 比例尺档位选择：目标约 120px */
function pickScale(s: number) {
  const raw = 120 * s
  const cands = [50, 100, 200, 500, 1000, 2000]
  let pick = cands[0]
  for (const c of cands) if (c <= raw) pick = c
  return pick
}
/** 比例尺文案 */
function scaleText(pick: number) {
  return pick >= 1000
    ? `0　${(pick / 2000).toFixed(1)}公里　${(pick / 1000).toFixed(pick >= 2000 ? 1 : 0)}公里`
    : `0　${pick / 2}米　${pick}米`
}

/**
 * 雷达站 R01 的屏幕位置与「是否在画面外」。
 *
 * R01 在 ENU 原点，主体适配时通常落在画面外。
 * 这时在视窗边缘画一个方向箭头，提示它的相对方位；
 * 点击「总览」即可看到完整位置关系。
 */
const stationEdgeHint = computed(() => {
  const g = gridFor()
  const { w: W, h: H } = plotSize()
  if (!W || !H) return null
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value
  const pxPerM = plotW / (v.xMax - v.xMin)
  // R01 = ENU (0,0)
  const gx = g.left + (0 - v.xMin) * pxPerM
  const gy = g.top + (v.yMax - 0) * pxPerM
  const [sx, sy] = projectScene(gx, gy)
  const inside = sx >= g.left && sx <= W - g.right && sy >= g.top && sy <= H - g.bottom
  if (inside) return null

  // 投到视窗边缘：沿中心→站点的方向
  const cx = g.left + plotW / 2
  const cy = g.top + plotH / 2
  const dx = sx - cx
  const dy = sy - cy
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len
  const uy = dy / len
  // 与矩形边界求交
  const tx = ux !== 0 ? (plotW / 2) / Math.abs(ux) : Infinity
  const ty = uy !== 0 ? (plotH / 2) / Math.abs(uy) : Infinity
  const t = Math.min(tx, ty)
  const ex = cx + ux * t
  const ey = cy + uy * t
  return {
    x: ex,
    y: ey,
    // 指向站点的方位角（度，0 = 正北，顺时针）
    bearing: (Math.atan2(ux, -uy) * 180) / Math.PI,
  }
})

/**
 * 取景模式。
 *
 * - subject（主体适配，默认）：厂区 + 港池 + 当前目标，自动铺满视窗；
 * - follow（目标跟随）：以选中目标为中心；
 * - overview（全域总览）：完整展示雷达站 R01 与全部目标的相对位置。
 *
 * 关键：自动取景只依据「有效内容」（厂区岸线、当前目标、实测航迹），
 * 雷达站原点、距离环、方位线、网格等参考元素不参与计算，
 * 否则会把视野撑大、让主体缩成中间一小块。
 */
export type ViewMode = 'subject' | 'follow' | 'overview'

/** 当前取景模式 */
const viewMode = ref<ViewMode>('subject')

/**
 * 实际生效的取景模式。
 *
 * 外部的「目标跟随」开关（props.follow）会把模式切到 follow，
 * 但如果用户已经在界面上显式选过某个模式，则以用户选择为准。
 */
const effectiveViewMode = computed<ViewMode>(() =>
  props.follow && viewMode.value === 'subject' ? 'follow' : viewMode.value,
)

/**
 * 有效内容包围盒（米制 ENU）。
 *
 * 包含：
 *   - 厂区/港池范围（底图覆盖区，设计坐标 → 米制）
 *   - 当前可见目标的当前位置
 *   - 实测航迹点（目标历史轨迹 + 源点迹）
 *
 * 不包含：雷达站原点、距离环、方位线、网格、装饰背景。
 */
function collectContentPoints(targets: any[]): [number, number][] {
  /* 收集「有效内容」的点集（米制 ENU）。
   *
   * 用离散点而不是矩形，是为了得到真实的内容范围：
   * 矩形四角往往是空白海域（例如西南角），
   * 把它们算进去会凭空多出一大片空海，把主体压小。
   *
   * 纳入的点：
   *   - 厂区建筑群锚点（NPP_PLACEMENTS，设计坐标 → 米制）
   *   - 背景船只锚点（BACKGROUND_SHIPS）
   *   - 当前目标的当前位置
   *   - 实测航迹点（目标历史轨迹 + 源点迹）
   *
   * 不纳入：雷达站原点、距离环、方位线、网格、装饰背景。 */
  const pts: [number, number][] = []

  // 厂区建筑群
  for (const p of NPP_PLACEMENTS) {
    if (p.enabled === false) continue
    const [e, n] = designToMeter(p.dx, p.dy)
    pts.push([e, n])
  }
  // 背景船只
  for (const s of BACKGROUND_SHIPS) {
    const [e, n] = designToMeter(s.dx, s.dy)
    pts.push([e, n])
  }
  // 岸线两端（设计 x ∈ [0, 1000]、y ≈ 165）
  {
    const [e1, n1] = designToMeter(0, 165)
    const [e2, n2] = designToMeter(1000, 165)
    pts.push([e1, n1], [e2, n2])
  }
  // 目标当前位置
  for (const t of targets) {
    if (t.east_m != null && t.north_m != null) pts.push([t.east_m, t.north_m])
    // 实测航迹点
    for (const h of t.history || []) {
      if (h.east_m == null || h.north_m == null) continue
      pts.push([h.east_m, h.north_m])
    }
    for (const sh of t.source_histories || []) {
      for (const h of sh.history || []) {
        if (h.east_m == null || h.north_m == null) continue
        pts.push([h.east_m, h.north_m])
      }
    }
  }
  return pts
}

/**
 * 安全留边（像素）。
 * 四周留出余量，并为目标标签预留位置，避免内容贴边或标签被裁掉。
 */
const FRAME_PAD_PX = 56
/** 目标标签需要的额外留边（像素）：标签画在目标上方 */
const LABEL_PAD_PX = 42

/**
 * 「主体适配」计算视口。
 *
 * 目标：有效内容铺满可用视窗，保持等比例（1m E = 1m N）。
 *
 * 算法：
 *   1. 取有效内容点集（厂区岸线 + 目标 + 航迹）；
 *   2. 2.5D 下先投影到屏幕空间再求包围盒
 *      （地面被 rotateX 压缩，ENU 矩形在屏幕上不是矩形，
 *       直接用 ENU 范围适配会导致旋转后主体偏小或偏到一侧）；
 *   3. 按目标填充率反推所需的米/像素比例；
 *   4. 把内容中心对准视窗中心。
 */
function fitSubjectRange(targets: any[]) {
  const pts = collectContentPoints(targets)
  if (!pts.length) return null
  /* 填充率：内容铺满可用视窗。
   *
   * 这里用 100%（而不是 85%~90%），因为需求是「画面占满画面」：
   * 剩余留白只保留标签避让所需的最小空间。
   * 之所以仍然留边，是因为目标标签画在目标上方，
   * 完全贴边会把标签裁掉。 */
  return fitPointsRange(pts, 1.0)
}

/**
 * 按点集拟合视口（通用取景内核）。
 *
 * @param pts     有效内容的 ENU 点集（米）
 * @param fill    目标填充率（内容占可用视窗的比例）
 *
 * 2D 与 2.5D 都走这里；2.5D 时先投影再算范围。
 */
function fitPointsRange(pts: [number, number][], fill: number) {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  if (!pts.length) return null

  /* 有效内容的目标占用尺寸（像素）。
   *
   * 需求是「画面占满画面」，所以 fill = 1.0：内容铺满绘图区。
   *
   * 高度方向仍扣掉目标标签所需的空间（LABEL_PAD_PX）：
   * 标签画在目标上方，若内容顶到视窗上沿，标签会被裁掉。
   * 宽度方向不再扣减 —— 留白由高度方向的限制自然产生，
   * 不会被重复计算而让内容偏小。 */
  const FILL = fill
  const availW = Math.max(80, plotW * FILL)
  const availH = Math.max(80, plotH - LABEL_PAD_PX * 2)

  // 内容中心的米制坐标
  let eMin = Infinity, eMax = -Infinity, nMin = Infinity, nMax = -Infinity
  for (const [e, n] of pts) {
    eMin = Math.min(eMin, e); eMax = Math.max(eMax, e)
    nMin = Math.min(nMin, n); nMax = Math.max(nMax, n)
  }
  const cE = (eMin + eMax) / 2
  const cN = (nMin + nMax) / 2

  const spanE = eMax - eMin
  const spanN = nMax - nMin

  if (!view25d.value) {
    /* 2D：ENU 包围盒直接等比例换算 */
    const s = Math.max(spanE / availW, spanN / availH)
    const xSpan = s * plotW
    const ySpan = s * plotH
    scalePx.value = Math.round(pickScale(s) / s)
    scaleLabel.value = scaleText(pickScale(s))
    return {
      xMin: cE - xSpan / 2, xMax: cE + xSpan / 2,
      yMin: cN - ySpan / 2, yMax: cN + ySpan / 2,
    }
  }

  /* 2.5D：先投影再算范围。
   *
   * 地面被 rotateX 压缩并带透视，同一个 ENU 矩形在屏幕上不是矩形：
   * 远端（北）被压扁、近端（南）被放大。
   * 因此不能直接用 ENU 范围去适配，而要：
   *   1. 给定「米/像素 s」与「ENU 中心」，把内容四角投影到屏幕；
   *   2. 看投影后的屏幕包围盒离目标（占满可用区并居中）差多少；
   *   3. 迭代修正 s（缩放）与中心（平移）。
   * 投影非线性，但迭代 5~6 次即可收敛到 1px 以内。
   */
  let s = Math.max(spanE / availW, spanN / availH)
  let cEc = cE
  let cNc = cN
  /* 用真实要素点（而非矩形四角）来求屏幕包围盒：
   * 矩形四角常落在空白海域，会让计算出的范围偏大。 */
  const corners = pts
  /* 迭代求 (s, 中心)：
   *   缩放修正 —— 内容应占满可用区（取较小者，保证不被裁切）；
   *   居中修正 —— 把屏幕包围盒中心移到视窗中心。
   *
   * 中心修正用 0.8 的阻尼系数：改变中心会改变透视（非线性），
   * 全量修正容易来回振荡，阻尼后 12 次迭代即可稳定在 1px 内。 */
  for (let i = 0; i < 12; i++) {
    const xMin = cEc - (s * plotW) / 2
    const yMax = cNc + (s * plotH) / 2
    let sxMin = Infinity, sxMax = -Infinity, syMin = Infinity, syMax = -Infinity
    for (const [e, n] of corners) {
      /* ENU → 地面平面容器像素（与 projectEnu / bgTransform 同一映射） */
      const gx = g.left + (e - xMin) / s
      const gy = g.top + (yMax - n) / s
      const [sx, sy] = projectScene(gx, gy)
      sxMin = Math.min(sxMin, sx); sxMax = Math.max(sxMax, sx)
      syMin = Math.min(syMin, sy); syMax = Math.max(syMax, sy)
    }
    const spanX = Math.max(1, sxMax - sxMin)
    const spanY = Math.max(1, syMax - syMin)
    // —— 缩放修正
    const k = Math.min(availW / spanX, availH / spanY)
    if (Math.abs(k - 1) > 0.002) s = s / k
    // —— 居中修正（带阻尼）
    //    屏幕 +x = 东，屏幕 +y = 南，故 ΔE = dpx·s、ΔN = -dpy·s
    const dpx = (sxMin + sxMax) / 2 - W / 2
    const dpy = (syMin + syMax) / 2 - H / 2
    cEc += dpx * s * 0.8
    cNc += -dpy * s * 0.8
    if (Math.abs(k - 1) <= 0.002 && Math.abs(dpx) < 0.5 && Math.abs(dpy) < 0.5) break
  }

  const xSpan = s * plotW
  const ySpan = s * plotH
  scalePx.value = Math.round(pickScale(s) / s)
  scaleLabel.value = scaleText(pickScale(s))
  return {
    xMin: cEc - xSpan / 2, xMax: cEc + xSpan / 2,
    yMin: cNc - ySpan / 2, yMax: cNc + ySpan / 2,
  }
}

/** 等比例视口:保证 1m(E) 与 1m(N) 在屏幕上等长,跟随/缩放均成立 */
function fitRanges(targets: any[]) {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const sel = targets.find((t) => t.global_track_id === props.selectedTargetId)
  // 用户已缩放/平移时：直接沿用当前米制视口，保证缩放不被重渲染覆盖
  if (userViewAdjusted.value) {
    const v = viewRange.value
    // 以用户视口跨度为准，重新按绘图区等比例展开，保证 1m(E)=1m(N)
    const s2 = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)
    const cx2 = (v.xMin + v.xMax) / 2
    const cy2 = (v.yMin + v.yMax) / 2
    const range2 = {
      xMin: cx2 - (s2 * plotW) / 2, xMax: cx2 + (s2 * plotW) / 2,
      yMin: cy2 - (s2 * plotH) / 2, yMax: cy2 + (s2 * plotH) / 2,
    }
    viewRange.value = range2
    scalePx.value = Math.round(pickScale(s2) / s2)
    scaleLabel.value = scaleText(pickScale(s2))
    return range2
  }
  /* 取景模式决定视口：
   *   subject  —— 主体适配（默认）：厂区 + 港池 + 目标铺满视窗
   *   follow   —— 目标跟随：以选中目标为中心
   *   overview —— 全域总览：完整展示 R01 与全部目标的相对位置
   *
   * props.follow 是外部「目标跟随」开关，它等价于 follow 模式；
   * 若两者冲突，以用户显式选择的取景模式为准。 */
  const mode: ViewMode = effectiveViewMode.value

  if (mode === 'subject') {
    const r = fitSubjectRange(targets)
    if (r) {
      viewRange.value = r
      recordAutoFitSpan(r)
      return r
    }
  }

  let cx: number, cy: number, xSpan: number, ySpan: number
  if (mode === 'follow' && sel && sel.east_m != null) {
    cx = sel.east_m; cy = sel.north_m
    xSpan = 1800; ySpan = 1300
  } else {
    /* 全域总览：把雷达站 R01（ENU 原点）、厂区与全部目标一起纳入画面，
     * 这样能看清三者的相对位置关系（R01 在厂区以南约 4.5 公里处）。
     * 注意：这会明显拉远视野，是「总览」模式的固有代价，
     * 默认的主体适配模式不包含 R01。
     *
     * 总览同样走投影感知的取景内核（2.5D 下 R01 与厂区在屏幕上
     * 的相对位置会被透视压缩，只按 ENU 范围算会漏掉 R01）。 */
    const pts = collectContentPoints(targets)
    pts.push([0, 0]) // 雷达站 R01
    const r = fitPointsRange(pts, 0.9)
    if (r) {
      viewRange.value = r
      recordAutoFitSpan(r)
      return r
    }
    cx = (BASE_X[0] + BASE_X[1]) / 2
    cy = (BASE_Y[0] + BASE_Y[1]) / 2
    xSpan = BASE_X[1] - BASE_X[0]
    ySpan = BASE_Y[1] - BASE_Y[0]
  }
  const s = Math.max(xSpan / plotW, ySpan / plotH) // 米/像素,双向一致
  xSpan = s * plotW
  ySpan = s * plotH
  // 动态比例尺:目标约 120px,取整档
  const pick = pickScale(s)
  scalePx.value = Math.round(pick / s)
  scaleLabel.value = scaleText(pick)
  const range = { xMin: cx - xSpan / 2, xMax: cx + xSpan / 2, yMin: cy - ySpan / 2, yMax: cy + ySpan / 2 }
  viewRange.value = range
  recordAutoFitSpan(range)
  return range
}

/**
 * 找到折线上距离给定测点最近的点（用于把时间标记吸附到航迹线上）。
 */
function nearestOnPolyline(
  pts: [number, number][],
  eastM: number,
  northM: number,
): [number, number] {
  let best: [number, number] = [eastM, northM]
  let bestD = Infinity
  for (const p of pts) {
    const d = (p[0] - eastM) ** 2 + (p[1] - northM) ** 2
    if (d < bestD) {
      bestD = d
      best = p
    }
  }
  return [best[0], best[1]]
}

/** 历史尾迹分段淡入:越接近当前越亮,共 6 段 */
function trailSeries(name: string, color: string, pts: number[][], isSel: boolean, z: number) {
  if (pts.length < 2) return []
  const K = 6
  const out: any[] = []
  const n = pts.length
  for (let i = 0; i < K; i++) {
    const a = Math.floor((i * (n - 1)) / K)
    const b = Math.floor(((i + 1) * (n - 1)) / K)
    if (b <= a) continue
    out.push({
      name: `${name}-seg${i}`,
      type: 'line',
      data: pts.slice(a, b + 1),
      showSymbol: false,
      silent: true,
      lineStyle: {
        color,
        width: isSel ? 3 : 2,
        opacity: 0.18 + (0.82 * (i + 1)) / K,
        shadowColor: color,
        shadowBlur: i === K - 1 ? (isSel ? 10 : 6) : 0,
      },
      z,
    })
  }
  return out
}

function buildOption() {
  const snap: any = props.snapshot
  if (!snap) return {}
  const targets: any[] = (snap.targets || []).filter(isVisible)
  const { xMin, xMax, yMin, yMax } = fitRanges(targets)
  const under = isUnder.value

  const series: any[] = []
  // —— 坐标参考层:严格 ENU。距离环 + 方位参考线(未确认的 95°±24° 覆盖扇区已关闭,不绘制)
  if (props.layers.coverage && !under) {
    /* 距离环：只保留主要档位（1/2/3 公里），透明度压低，
     * 避免五层同心圆把画面切成「公路图」，与航迹争夺注意力。 */
    ;[1000, 2000, 3000].forEach((r) => {
      series.push({
        name: `range-${r}`,
        type: 'line',
        data: circlePts(r),
        showSymbol: false,
        silent: true,
        lineStyle: { color: '#155a7a', width: 1, type: 'dashed', opacity: 0.2 },
        z: 1,
      })
    })
    /* 方位参考线：只画正交的主方位（0/45/90…），其余隐去 */
    for (let b = 0; b < 360; b += 45) {
      series.push({
        name: `brg-${b}`,
        type: 'line',
        data: bearingLine(b),
        showSymbol: false,
        silent: true,
        lineStyle: { color: '#0f3d55', width: 1, opacity: b % 90 === 0 ? 0.22 : 0.1 },
        z: 1,
      })
    }
  }
  // 站址 R01(水面页)
  if (!under) {
    series.push({
      name: 'station',
      type: 'scatter',
      data: [{ value: [0, 0], station: true }],
      symbolSize: 14,
      itemStyle: { color: '#ff5a5a', borderColor: '#fff', borderWidth: 2 },
      label: {
        show: true,
        formatter: '雷达站 R01\n北纬37.53297°，东经121.4199°',
        color: '#c98c8c',
        fontSize: 11,
        position: 'top',
        distance: 12,
        backgroundColor: 'rgba(3,26,46,0.9)',
        borderColor: '#ff5a5a',
        borderWidth: 1,
        borderRadius: 3,
        padding: [4, 8],
      },
      z: 10,
    })
  }

  // —— 航迹层:全部直接取 [E_m, N_m],不经过 view_u/view_v
  const timeMarks: any[] = []
  /* 系泊目标判定：长期停在陆域 / 堤体上的目标（如厂区内锚泊小船）。
   * 它们不是「在航行的船」，因此不画航行尾迹，改为静态目标标记，
   * 位置仍用真实 ENU 坐标。 */
  const mooredIds = new Set<string>()
  for (const t of targets) {
    const rawAll = (t.history || []).map((p: any) => [p.east_m, p.north_m] as [number, number])
    if (rawAll.length >= 2 && isMoored(rawAll)) mooredIds.add(t.global_track_id)
  }
  for (const t of targets) {
    const c = targetColor(t.global_track_id)
    const isSel = t.global_track_id === props.selectedTargetId
    const isMooredTarget = mooredIds.has(t.global_track_id)
    if (props.layers.source_tracks && !isMooredTarget) {
      for (const s of t.source_histories || []) {
        const raw = (s.history || []).map((p: any) => [p.east_m, p.north_m] as [number, number])
        if (raw.length < 2) continue
        /* 物理可行化：源点迹是实测真值，坐标不能改，
         * 但显示用的折线要绕开陆域与构筑物，否则会「穿模」。 */
        const pts = makeNavigable(raw)
        /* 若该源点迹整段都在陆域上（雷达在厂区内跟踪到的静止目标），
         * 它不是「航行中的船」，不画尾迹 ——
         * 否则会出现一条在厂房里穿行的假航迹。 */
        if (isMoored(raw)) continue
        series.push({
          name: `${t.global_track_id}-${s.source_id}`,
          type: 'line',
          data: pts,
          showSymbol: false,
          silent: true,
          lineStyle: {
            color: s.source_id === 'R01' ? 'rgba(77,166,255,0.7)' : 'rgba(62,243,163,0.7)',
            width: isSel ? 1.8 : 1.1,
            type: s.source_id === 'R01' ? 'dashed' : 'dotted',
          },
          z: 4,
        })
      }
    }
    if (props.layers.fused_tracks && !isMooredTarget) {
      const hist = t.history || []
      const raw = hist.map((p: any) => [p.east_m, p.north_m] as [number, number])
      /* 物理可行化：融合航迹同样按地图修正显示折线，
       * 避免压陆、穿堤；测点（时间标记、目标符号）仍用真实 ENU 坐标。 */
      const pts = makeNavigable(raw)
      series.push(...trailSeries(`${t.global_track_id}-fused`, c, pts, isSel, 5))
      /* 时间标记:每 30s 取一个,重叠隐藏。
       *
       * 标记位置取自「修正后的航迹折线」上最接近该时刻的测点的点，
       * 这样时间标记会落在实际绘制的航迹线上，
       * 而不是浮在厂区陆地上（与航迹线脱节）。 */
      let lastBin = -1
      for (const p of hist) {
        const bin = Math.floor(p.time_ms / 30000)
        if (bin === lastBin) continue
        lastBin = bin
        const anchor = nearestOnPolyline(pts, p.east_m, p.north_m)
        timeMarks.push({ value: anchor, t: p.time_ms, tid: t.global_track_id })
      }
    }
    // —— 目标交互层:小船符 + 末端箭头 + 选中框,位置取当前回放时刻插值
    if (t.east_m == null || t.north_m == null) continue
    const rot = t.course_deg != null ? t.course_deg : 0
    /* 目标符号的显示位置：
     * 若实测位置落在厂区陆域内（实测数据确实如此），
     * 符号改画在最近的水侧泊位 —— 船不会浮在厂房之间。
     * 实测坐标本身不改，仍用于量测信息与数据面板。 */
    const disp = navigableDisplayPosition(t.east_m, t.north_m)
    series.push({
      name: t.global_track_id,
      type: 'scatter',
      data: [
        {
          value: [disp.east, disp.north],
          tid: t.global_track_id,
          mmsi: t.mmsi,
          speed: t.speed_mps,
          course: t.course_deg,
          lat: t.lat ?? t.latitude_deg,
          lon: t.lon ?? t.longitude_deg,
          bearing: t.bearing_deg,
          range: t.range_m,
          utc: t.utc,
          /* 系泊目标：长期待在陆域 / 堤体上，不在航行。
           * 标记为静态目标，显示「锚泊」字样，避免被误读为航行中的船。 */
          moored: isMooredTarget,
        },
      ],
      symbol: shipPath,
      symbolRotate: rot,
      // 尺寸按真实船长换算（米→像素），保证缩放时船体比例正确
      symbolSize: shipPixelSize(t, isSel),
      itemStyle: {
        color: c,
        borderColor: isMooredTarget ? '#f0c98a' : '#02182a',
        borderWidth: isMooredTarget ? 1.8 : 1.2,
        shadowColor: c,
        shadowBlur: isSel ? 12 : 5,
        // 2.5D 下的落影：向右下偏移，形成悬浮高度感
        shadowOffsetX: view25d.value ? (isSel ? 3 : 2) : 0,
        shadowOffsetY: view25d.value ? (isSel ? 4 : 3) : 0,
      },
      /* 目标标签层级：编号最突出（16px 粗体 + 目标色），
       * 次要信息（速度/航向）退到 11px 浅灰蓝，避免与设施文字抢注意力。 */
      label: {
        show: true,
        formatter: isMooredTarget
          ? `{b|${t.global_track_id}} · {m|${t.mmsi || ''}}\n{S|锚泊待命  航迹已按陆域约束处理}`
          : `{b|${t.global_track_id}} · {m|${t.mmsi || ''}}\n{S|${t.speed_mps?.toFixed?.(1) ?? '-'} 米/秒  ${t.course_deg != null ? t.course_deg.toFixed(0) + '°' : '—'}}`,
        color: '#e6fbff',
        fontSize: 12,
        position: 'top',
        distance: 20,
        backgroundColor: 'rgba(3,26,46,0.88)',
        borderColor: c,
        borderWidth: 1,
        borderRadius: 4,
        padding: [4, 8],
        rich: {
          b: { fontSize: 16, fontWeight: 700, color: c },
          m: { fontSize: 11, color: '#7fa8bd' },
          S: { fontSize: 11, color: '#7fa8bd' },
        },
      },
      emphasis: { scale: 1.25 },
      z: 8,
    })
    // 轨迹末端运动箭头(艏向)
    const last = (t.history || []).at(-1)
    if (last && last.course_deg == null && t.course_deg == null) {
      // 无艏向则不画
    } else if (last) {
      /* 艏向箭头跟随目标符号的显示位置（陆域内则移到泊位） */
      const arrowDisp = navigableDisplayPosition(last.east_m, last.north_m)
      series.push({
        name: `${t.global_track_id}-arrow`,
        type: 'scatter',
        data: [{ value: [arrowDisp.east, arrowDisp.north] }],
        symbol: arrowPath,
        symbolRotate: t.course_deg ?? last.course_deg ?? 0,
        symbolSize: 12,
        silent: true,
        itemStyle: { color: c, opacity: 0.95 },
        label: { show: false },
        z: 9,
      })
    }
    // 选中框
    if (isSel) {
      series.push({
        name: `${t.global_track_id}-box`,
        type: 'scatter',
        data: [{ value: [disp.east, disp.north] }],
        symbol: 'rect',
        symbolSize: [44, 44],
        silent: true,
        itemStyle: { color: 'transparent', borderColor: c, borderWidth: 1.5, opacity: 0.9 },
        label: { show: false },
        z: 7,
      })
    }
  }
  // 时间标记统一一个 series,hideOverlap 防重叠
  if (timeMarks.length) {
    series.push({
      name: 'time-marks',
      type: 'scatter',
      data: timeMarks.slice(0, 60).map((d: any) => ({
        value: d.value,
        t: d.t,
        label: {
          show: true,
          formatter: `${String(Math.floor(d.t / 60000)).padStart(2, '0')}:${String(Math.floor((d.t % 60000) / 1000)).padStart(2, '0')}`,
          color: '#5f8296',
          fontSize: 10,
          position: 'right',
          distance: 6,
        },
      })),
      symbolSize: 4,
      silent: true,
      itemStyle: { color: '#3d7ea6', opacity: 0.9 },
      labelLayout: { hideOverlap: true },
      z: 6,
    })
  }

  return {
    backgroundColor: 'transparent',
    animation: !props.paused,
    grid: gridFor(),
    /* 关闭 axisPointer 的联动十字线。
     * 默认样式是浅灰白（#B9BEC9）的虚线，拖动时会被激活，
     * 在深色海图上表现为突然出现的白色十字，与网格线一起干扰观感。
     * 坐标读数改由下方的量测面板提供。 */
    axisPointer: { show: false, link: [] },
    tooltip: {
      trigger: 'item',
      backgroundColor: 'rgba(3,26,46,0.95)',
      borderColor: '#22e4ee',
      textStyle: { color: '#e1f7ff', fontSize: 14 },
      formatter: (p: any) => {
        if (p.data?.station) return `雷达站 R01<br/>北纬37.53297°，东经121.4199°<br/>站心坐标原点`
        const d = p.data || {}
        if (d.t != null && !d.tid) return `历史时刻 ${String(Math.floor(d.t / 60000)).padStart(2, '0')}:${String(Math.floor((d.t % 60000) / 1000)).padStart(2, '0')}`
        if (!d.tid) return p.seriesName
        return `${d.tid} · MMSI ${d.mmsi || '—'}<br/>` +
          `经纬度 ${d.lat?.toFixed?.(5) ?? '—'}，${d.lon?.toFixed?.(5) ?? '—'}<br/>` +
          `站心坐标 东${p.value[0].toFixed(1)}米 / 北${p.value[1].toFixed(1)}米<br/>` +
          `速度 ${d.speed?.toFixed?.(2) ?? '—'}米/秒 · 艏向 ${d.course != null ? d.course.toFixed(1) + '°' : '—'}<br/>` +
          `距离 ${d.range?.toFixed?.(1) ?? '—'}米 · 方位 ${d.bearing?.toFixed?.(2) ?? '—'}°<br/>` +
          `<span style="color:#8fb9cc">${d.utc || ''}</span>`
      },
    },
    xAxis: {
      type: 'value',
      name: '东向（米）',
      min: xMin, max: xMax,
      nameTextStyle: { color: '#8fb9cc', fontSize: 13 },
      axisLine: { lineStyle: { color: '#1875a1' } },
      axisLabel: { color: '#8fb9cc', fontSize: 12 },
      /* 米制网格线：彻底关闭。
       *
       * 【为什么必须关】
       * 1) 深色海图上，网格与底图的海岸线/等深线/航迹叠加会很杂乱；
       * 2) 更关键 —— ECharts 在只传 { min, max } 的 setOption 调用中，
       *    会把坐标轴的 splitLine 重置为**主题默认值**（近白 #E0E6F1），
       *    于是「拖动一下」网格就变成刺眼的白线（用户反馈的现象）。
       *    显式 show:false 才能保证任何重绘路径下都不出现网格线。 */
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      name: '北向（米）',
      min: yMin, max: yMax,
      nameTextStyle: { color: '#8fb9cc' },
      axisLine: { lineStyle: { color: '#1875a1' } },
      axisLabel: { color: '#8fb9cc', fontSize: 12 },
      splitLine: { show: false },
    },
    series,
  }
}

/** 视图范围（米制），由用户缩放/平移直接控制 */
const viewRange = ref({ xMin: -2600, xMax: 1000, yMin: 3000, yMax: 5000 })
/** 用户是否已操作视图（缩放/平移），用于区分自动跟随与手动视图 */
const userViewAdjusted = ref(false)

/** 是否启用 2.5D 轴测视图 */
const view25d = ref(true)
/** 2.5D 倾斜角（度）：俯视角，越大越接近水平 */
/** 2.5D 旋转角（度）：绕竖轴方位 */
/** 全景基准范围 */
const FULL_RANGE = { xMin: -2600, xMax: 1000, yMin: 3000, yMax: 5000 }
/** 最小可视跨度（米），防止过度放大 */
const MIN_SPAN = 300

/**
 * 绑定滚轮缩放与拖拽平移手势。
 *
 * 【监听对象的选择】
 * 监听挂在 **.ws-chart-wrap**（场景容器）而不是 ECharts 容器上。
 *
 * 原因：2.5D 模式下 .ws-chart 位于被 rotateX 倾斜的 .ws-ground 内，
 * 它的 getBoundingClientRect() 返回的是**变换后**的包围盒
 * （实测 1920×870 → 784×211），尺寸与位置都对不上实际绘图区，
 * 导致「按下点是否在绘图区内」的判断全部失效，拖拽被静默丢弃。
 *
 * 而 .ws-chart-wrap 始终保持原始矩形（不参与 3D 变换），
 * 用它换算坐标最可靠。
 *
 * 另外 .ws-scene-3d 已设 pointer-events:none，
 * 所以覆盖在图表上方的底图 / 立牌 / 船只 / 标注都不会吃掉鼠标事件。
 */
function bindViewportGestures() {
  const node = el.value?.closest('.ws-chart-wrap') as HTMLElement | null
  if (!node) return
  gestureHost = node
  node.addEventListener('wheel', onWheel, { passive: false })
  node.addEventListener('mousedown', onMouseDown)
  node.addEventListener('touchstart', onTouchStart, { passive: true })
  node.addEventListener('touchmove', onTouchMove, { passive: false })
  node.addEventListener('touchend', onTouchEnd)
  node.addEventListener('touchcancel', onTouchEnd)
  /* mousemove / mouseup 挂在 window 上：
   * 拖拽时指针可能移出图表区域（甚至移出浏览器窗口），
   * 若只在图表上监听，一旦指针离开就会「拖不动」或拖一半卡住。 */
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
}

/** 手势监听宿主（.ws-chart-wrap），用于把屏幕坐标换算成图表坐标 */
let gestureHost: HTMLElement | null = null

function onWheel(e: WheelEvent) {
  e.preventDefault()
  const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12
  zoomAt(factor, e.clientX, e.clientY)
}

/**
 * 屏幕坐标 → 图表容器像素坐标（未变换坐标系）。
 *
 * 【为什么需要】
 * 2.5D 下 .ws-chart 被 3D 变换包裹，
 * 其 getBoundingClientRect() 与「容器左上角 + 内部像素」不再是一一对应。
 *
 * 但缩放/平移的数学只需要「指针相对绘图区的位置与位移」，
 * 用未变换的绘图区尺寸做换算即可。
 *
 * 这里以 .ws-chart-wrap（不参与 3D 变换）的屏幕矩形为基准，
 * 再按「未变换尺寸 / 变换后尺寸」的比例反解出容器像素坐标。
 */
function pointerToPlot(e: { clientX: number; clientY: number }): { px: number; py: number } {
  const host = gestureHost ?? el.value?.closest('.ws-chart-wrap')
  const chart = el.value
  if (!host || !chart) return { px: 0, py: 0 }
  const hr = host.getBoundingClientRect()
  const cr = chart.getBoundingClientRect()
  const cw = chart.clientWidth || cr.width || 1
  const ch = chart.clientHeight || cr.height || 1
  /* 变换后的包围盒 / 未变换尺寸 = 缩放比 */
  const scaleX = (cr.width || cw) / cw
  const scaleY = (cr.height || ch) / ch
  return {
    px: (e.clientX - hr.left) / (scaleX || 1),
    py: (e.clientY - hr.top) / (scaleY || 1),
  }
}

let dragging = false
let dragStartX = 0
let dragStartY = 0
let dragOrigin = { xMin: 0, xMax: 0, yMin: 0, yMax: 0 }

function onMouseDown(e: MouseEvent) {
  // 仅在绘图区左键按下时开始平移
  if (e.button !== 0 || !el.value) return
  const g = gridFor()
  const W = el.value.clientWidth
  const H = el.value.clientHeight
  if (!W || !H) return
  const { px, py } = pointerToPlot(e)
  if (px < g.left || px > W - g.right) return
  if (py < g.top || py > H - g.bottom) return
  dragging = true
  dragStartX = px
  dragStartY = py
  dragOrigin = { ...viewRange.value }
  // 拖拽时禁用文本选中，避免鼠标移动时选中文字
  document.body.style.userSelect = 'none'
  gestureHost?.classList.add('is-dragging')
}

function onMouseMove(e: MouseEvent) {
  if (!dragging || !el.value) return
  const g = gridFor()
  const W = el.value.clientWidth
  const H = el.value.clientHeight
  if (!W || !H) return
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = dragOrigin
  /* 米/像素：用未变换的绘图区尺寸，
   * 保证 1m(E) = 1m(N) 在屏幕上等长（平移不破坏比例）。 */
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)
  const { px, py } = pointerToPlot(e)
  const dx = (px - dragStartX) * s
  const dy = (py - dragStartY) * s
  viewRange.value = {
    xMin: v.xMin - dx, xMax: v.xMax - dx,
    yMin: v.yMin + dy, yMax: v.yMax + dy,
  }
  userViewAdjusted.value = true
  applyView()
}

function onMouseUp() {
  dragging = false
  document.body.style.userSelect = ''
  gestureHost?.classList.remove('is-dragging')
}

/**
 * 触摸手势（触摸屏大屏 / 平板）。
 *
 * 单指拖动 = 平移，双指捏合 = 缩放。
 * 与鼠标手势共用同一套视口数学，因此行为完全一致。
 */
function onTouchStart(e: TouchEvent) {
  if (!el.value) return
  if (e.touches.length === 1) {
    const t = e.touches[0]
    const g = gridFor()
    const W = el.value.clientWidth
    const H = el.value.clientHeight
    if (!W || !H) return
    const { px, py } = pointerToPlot({ clientX: t.clientX, clientY: t.clientY })
    if (px < g.left || px > W - g.right) return
    if (py < g.top || py > H - g.bottom) return
    dragging = true
    dragStartX = px
    dragStartY = py
    dragOrigin = { ...viewRange.value }
    gestureHost?.classList.add('is-dragging')
  } else if (e.touches.length === 2) {
    /* 双指捏合：记录初始距离与视口跨度，move 时按比例缩放。 */
    const [a, b] = [e.touches[0], e.touches[1]]
    pinchStartDist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)
    pinchStartRange = { ...viewRange.value }
    dragging = false
  }
}

/** 双指捏合距离 */
let pinchStartDist = 0
/** 双指捏合起始视口 */
let pinchStartRange = { xMin: 0, xMax: 0, yMin: 0, yMax: 0 }

function onTouchMove(e: TouchEvent) {
  if (!el.value) return
  if (e.touches.length === 2 && pinchStartDist > 0) {
    e.preventDefault()
    const [a, b] = [e.touches[0], e.touches[1]]
    const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)
    if (dist < 1) return
    /* 手指间距变大 → 放大（factor < 1），反之缩小。 */
    const factor = pinchStartDist / dist
    const cx = (pinchStartRange.xMin + pinchStartRange.xMax) / 2
    const cy = (pinchStartRange.yMin + pinchStartRange.yMax) / 2
    const halfW = ((pinchStartRange.xMax - pinchStartRange.xMin) / 2) * factor
    const halfH = ((pinchStartRange.yMax - pinchStartRange.yMin) / 2) * factor
    viewRange.value = {
      xMin: cx - halfW, xMax: cx + halfW,
      yMin: cy - halfH, yMax: cy + halfH,
    }
    userViewAdjusted.value = true
    clampZoomRange()
    applyView()
    return
  }
  if (e.touches.length !== 1 || !dragging) return
  e.preventDefault()
  const g = gridFor()
  const W = el.value.clientWidth
  const H = el.value.clientHeight
  if (!W || !H) return
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = dragOrigin
  const sc = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)
  const t = e.touches[0]
  const { px, py } = pointerToPlot({ clientX: t.clientX, clientY: t.clientY })
  const dx = (px - dragStartX) * sc
  const dy = (py - dragStartY) * sc
  viewRange.value = {
    xMin: v.xMin - dx, xMax: v.xMax - dx,
    yMin: v.yMin + dy, yMax: v.yMax + dy,
  }
  userViewAdjusted.value = true
  applyView()
}

function onTouchEnd(e: TouchEvent) {
  if (e.touches.length === 0) {
    dragging = false
    pinchStartDist = 0
    document.body.style.userSelect = ''
    gestureHost?.classList.remove('is-dragging')
  }
}

/**
 * 应用当前 viewRange 到坐标轴与底图。
 *
 * 【关键：必须带 replaceMerge】
 * 直接 setOption({ xAxis: { min, max } }) 会把坐标轴模型**整体替换**成
 * 只含 min/max 的残缺对象，丢失 type:'value' 等内部方法，
 * ECharts 随后在 layout 阶段抛
 * 「axis.getAxesOnZeroOf is not a function」，
 * 视口更新因此中断 —— 表现就是「拖不动」。
 *
 * 这里与 render() 保持一致地带上 replaceMerge，
 * 让 min/max 合并进完整的坐标轴模型，而不是替换它。
 */
function applyView() {
  const v = viewRange.value
  if (chart) {
    chart.setOption(
      {
        /* 这里必须**显式带上 splitLine / axisPointer 的关闭状态**。
         *
         * ECharts 的 setOption 对坐标轴是「按需合并」，
         * 只传 { min, max } 时，其余子属性可能被重置为主题默认值
         * —— splitLine 就会变回近白色 #E0E6F1，
         * 表现为「拖一下动就冒出白色网格线」。
         *
         * 显式声明 show:false 后，任何重绘路径下都不会再出现网格线。 */
        xAxis: { min: v.xMin, max: v.xMax, splitLine: { show: false } },
        yAxis: { min: v.yMin, max: v.yMax, splitLine: { show: false } },
        axisPointer: { show: false, link: [] },
      },
      { replaceMerge: ['series', 'xAxis', 'yAxis'] } as any,
    )
  }
  updateScaleBar()
  // 等待 setOption 合并后回读实际范围并校正比例尺
  requestAnimationFrame(() => {
    syncBgFromChart()
    updateScaleBar()
  })
}

/** 从 ECharts 实际坐标范围回读底图视口（保证底图与坐标层像素级一致） */
function syncBgFromChart() {
  if (!chart) return
  const opt: any = chart.getOption()
  const xa = opt?.xAxis?.[0]
  const ya = opt?.yAxis?.[0]
  if (!xa || !ya) return
  const xMin = Number(xa.min), xMax = Number(xa.max)
  const yMin = Number(ya.min), yMax = Number(ya.max)
  if (![xMin, xMax, yMin, yMax].every((v) => isFinite(v))) return
  const v = { xMin, xMax, yMin, yMax }
  viewRange.value = v
  updateScaleBar()
}

/** 按当前视口更新比例尺 */
function updateScaleBar() {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH) // 米/像素
  const pick = pickScale(s)
  scalePx.value = Math.round(pick / s)
  scaleLabel.value = scaleText(pick)
}

/**
 * 缩放约束。
 *
 * 需求：「画面占满画面，不需要缩小，仅放大一小部分」。
 * 因此把用户缩放限制在一个窄带内：
 *   - 下限 = 自动取景（主体适配）算出的跨度：
 *     不允许再缩小，否则画面又会退回到「主体挤在中间、四周大片空海」；
 *   - 上限 = 自动取景跨度的 1/1.5：
 *     最多放大 1.5 倍，用于看清单个目标或设施的细节。
 */
const ZOOM_IN_MAX_FACTOR = 1.5

/** 自动取景跨度（米/像素），作为缩放的下限基准 */
const autoFitSpan = ref(0)

/**
 * 当前是否还能继续放大。
 *
 * 放大上限是自动取景跨度的 1 / ZOOM_IN_MAX_FACTOR，
 * 到达上限后「＋」按钮置灰，避免无效点击。
 */
const canZoomIn = computed(() => {
  const base = autoFitSpan.value
  if (!base) return true
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)
  // s 越小表示放得越大；s 到达 base / MAX 时即为上限
  return s > base / ZOOM_IN_MAX_FACTOR + 1e-6
})

/**
 * 记录自动取景得到的米/像素比例，作为用户缩放的下限基准。
 *
 * 用户不能把画面缩得比自动取景更小（否则又回到「主体挤在中间、
 * 四周大片空海」）；放大则最多到 1.5 倍（ZOOM_IN_MAX_FACTOR）。
 *
 * 基准取自「实际算出来的视口」，因此对主体适配 / 目标跟随 /
 * 全域总览三个模式都成立 —— 每个模式各自有自己的缩放下限。
 */
function recordAutoFitSpan(range: { xMin: number; xMax: number; yMin: number; yMax: number }) {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  autoFitSpan.value = Math.max(
    (range.xMax - range.xMin) / plotW,
    (range.yMax - range.yMin) / plotH,
  )
}

/**
 * 以指定屏幕点为锚，缩放视口（factor<1 放大）。
 *
 * 缩放范围受 clampZoomRange 约束：
 *   不能缩得比自动取景更小，也不能放得超过 1.5 倍。
 */
function zoomAt(factor: number, anchorClientX?: number, anchorClientY?: number) {
  const v = viewRange.value
  const g = gridFor()
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)

  /* 当前米/像素比例（E、N 两向一致） */
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)

  /* ---- 1) 指针 → 容器像素 → 米制锚点 ---- */
  let anchorE: number | null = null
  let anchorN: number | null = null
  let anchorPx = 0
  let anchorPy = 0
  if (anchorClientX != null && anchorClientY != null && el.value) {
    const { px: rawX, py: rawY } = pointerToPlot({ clientX: anchorClientX, clientY: anchorClientY })
    anchorPx = rawX - g.left
    anchorPy = rawY - g.top
    anchorE = v.xMin + anchorPx * s
    anchorN = v.yMax - anchorPy * s
  }

  /* ---- 2) 新比例与新跨度（保持等比例） ---- */
  let sNew = s * factor
  const spanForS = Math.min(sNew * plotW, sNew * plotH)
  if (spanForS < MIN_SPAN) {
    sNew = MIN_SPAN / Math.min(plotW, plotH)
  }
  const xSpan = sNew * plotW
  const ySpan = sNew * plotH

  /* ---- 3) 新范围：无锚点时保持原中心；有锚点时让锚点严格不动 ---- */
  let xMin: number
  let yMax: number
  if (anchorE != null && anchorN != null) {
    /* 锚点给的是「范围边界」：
     *   anchorE = xMin + px * s  →  xMin' = anchorE - px * sNew
     *   anchorN = yMax - py * s  →  yMax' = anchorN + py * sNew
     * 注意 xMin'/yMax' 是边界，不能当中心用。 */
    xMin = anchorE - anchorPx * sNew
    yMax = anchorN + anchorPy * sNew
  } else {
    xMin = (v.xMin + v.xMax) / 2 - xSpan / 2
    yMax = (v.yMin + v.yMax) / 2 + ySpan / 2
  }

  viewRange.value = {
    xMin, xMax: xMin + xSpan,
    yMin: yMax - ySpan, yMax,
  }
  userViewAdjusted.value = true
  clampZoomRange()
  applyView()
}

/**
 * 把当前视口钳制到允许的缩放范围内。
 *
 * 缩放下限 = 自动取景跨度（画面占满视窗）。
 * 缩放上限 = 自动取景跨度 ÷ ZOOM_IN_MAX_FACTOR（最多放大 1.5 倍）。
 */
function clampZoomRange() {
  const base = autoFitSpan.value
  if (!base) return
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)
  const v = viewRange.value

  // 当前米/像素比例
  const s = Math.max((v.xMax - v.xMin) / plotW, (v.yMax - v.yMin) / plotH)
  // 允许区间：base（不缩小） ~ base / 1.5（最多放大 1.5 倍）
  const sMax = base / ZOOM_IN_MAX_FACTOR
  const sMin = base
  let sClamped = s
  if (s > sMin) sClamped = sMin
  else if (s < sMax) sClamped = sMax
  if (sClamped === s) return

  // 保持中心不变地缩放
  const cx = (v.xMin + v.xMax) / 2
  const cy = (v.yMin + v.yMax) / 2
  const xSpan = sClamped * plotW
  const ySpan = sClamped * plotH
  viewRange.value = {
    xMin: cx - xSpan / 2, xMax: cx + xSpan / 2,
    yMin: cy - ySpan / 2, yMax: cy + ySpan / 2,
  }
}

/** 以视图中心按倍数缩放（factor<1 放大） */
function zoomBy(factor: number) {
  zoomAt(factor)
}

/**
 * 切换取景模式并立即重新取景。
 *
 * 「主体适配」是默认模式：有效内容铺满视窗。
 * 「目标跟随」以选中目标为中心。
 * 「全域总览」把雷达站 R01 与全部目标一起纳入画面。
 */
function setViewMode(mode: ViewMode) {
  viewMode.value = mode
  userViewAdjusted.value = false
  const targets = (props.snapshot as any)?.targets || []
  const range = fitRanges(targets)
  if (range) {
    viewRange.value = range
    applyView()
  }
  // 等坐标轴合并完成后以实际范围重算比例尺
  setTimeout(() => {
    syncBgFromChart()
    updateScaleBar()
  }, 60)
}

/**
 * 复位缩放比例 —— 恢复初始比例尺。
 *
 * 只把**缩放比例**恢复到初始自适应值，取景模式保持不变
 * （还是主体 / 还是跟随）。
 *
 * 【为什么不能直接复用 setViewMode('subject')】
 * 场景若正处在「跟随」模式，用户按「复位比例」的意图是
 * 「我刚才放大看细节，现在看不清了，想回到正常比例」，
 * 而不是「把镜头拉到全景去」。
 * 所以这里保留当前 viewMode，只清掉 userViewAdjusted 标记，
 * 让下次 fitRanges 按当前模式重新自适应。
 *
 * 需要「全域总览」视野时，用场景缩放面板里的「总览」按钮。
 */
function resetScale() {
  userViewAdjusted.value = false
  const targets = (props.snapshot as any)?.targets || []
  const range = fitRanges(targets)
  if (range) {
    viewRange.value = range
    applyView()
  }
  setTimeout(() => {
    syncBgFromChart()
    updateScaleBar()
  }, 60)
}

/**
 * 对外暴露的方法。
 *
 * `resetScale()` 供父级的「复位比例」按钮调用。
 */
defineExpose({ resetScale, setViewMode })

/** 同步图表容器尺寸（供 SVG viewBox 使用）。
 * 取 .ws-chart 的 clientWidth/Height，与底图 holder 的 CSS 尺寸一致，
 * 保证 SVG viewBox 与 CSS 尺寸严格相等，避免 preserveAspectRatio 拉伸带来的偏移。 */
function syncChartSize() {
  const w = el.value?.clientWidth || 0
  const h = el.value?.clientHeight || 0
  if (w && h && (w !== chartSize.value.w || h !== chartSize.value.h)) {
    chartSize.value = { w, h }
  }
}

function render() {
  if (!chart || !el.value) return
  chart.setOption(buildOption(), { replaceMerge: ['series', 'xAxis', 'yAxis'] } as any)
}

function rerender() {
  if (!chart) return
  chart.resize()
  render()
}

onMounted(() => {
  // 加载场景素材清单（失败时回退内置 SVG 示意图）
  loadSceneAssets().then((list) => {
    sceneAssets.value = list
    assetsReady.value = true
    render()
  })
  chart = echarts.init(el.value!)
  syncChartSize()
  ;(window as any).__chart = chart
  render()
  chart.on('click', (p: any) => {
    const tid = p.data?.tid || (typeof p.seriesName === 'string' ? p.seriesName.split('-')[0] : null)
    if (tid && /^T\d/.test(tid)) emit('select-target', tid)
  })
  ro = new ResizeObserver(() => {
    syncChartSize()
    rerender()
    })
  if (el.value) ro.observe(el.value)
  window.addEventListener('resize', rerender)
  bindViewportGestures()
})
onBeforeUnmount(() => {
  ro?.disconnect()
  window.removeEventListener('resize', rerender)
  window.removeEventListener('mouseup', onMouseUp)
  gestureHost?.removeEventListener('wheel', onWheel)
  gestureHost?.removeEventListener('mousedown', onMouseDown)
  gestureHost?.removeEventListener('touchstart', onTouchStart)
  gestureHost?.removeEventListener('touchmove', onTouchMove)
  gestureHost?.removeEventListener('touchend', onTouchEnd)
  gestureHost?.removeEventListener('touchcancel', onTouchEnd)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
  gestureHost = null
  document.body.style.userSelect = ''
  chart?.dispose()
  chart = null
})

watch(() => [props.snapshot, props.selectedTargetId, props.layers, props.follow, props.paused, props.mode], render, { deep: true })

/* 播放过程中：视野保持稳定，仅在目标逼近边缘时微调平移（不缩放）。 */
watch(
  () => (props.snapshot as any)?.time_ms,
  () => {
    nudgeFrameIfNeeded()
  },
)

/* 切换 2D / 2.5D 时坐标边距与投影变化，需重新渲染并重算比例尺。
 * 2.5D 的投影是非线性的，取景范围也必须跟着重算，
 * 否则同一取景在两种模式下的主体占比会不一样。 */
watch(view25d, () => {
  nextTick(() => {
    userViewAdjusted.value = false
    const targets = (props.snapshot as any)?.targets || []
    const range = fitRanges(targets)
    if (range) {
      viewRange.value = range
      applyView()
    }
    render()
    updateScaleBar()
  })
})

/**
 * 自动取景的触发时机。
 *
 * 需求：初始化、切换场景、点击按钮时触发；
 * 播放过程中保持视野稳定（不随目标移动而跳动），
 * 只在目标接近边缘时才适当调整。
 *
 * 实现：
 *   - 初始化 / 场景切换（snapshot 的 session 变化）→ 立即重新取景；
 *   - 播放过程中 → 仅当目标接近视窗边缘时微调，且只平移不缩放。
 */
const lastAutoFrameSession = ref('')

watch(
  () => (props.snapshot as any)?.session_id,
  (sid) => {
    if (!sid || sid === lastAutoFrameSession.value) return
    lastAutoFrameSession.value = sid
    userViewAdjusted.value = false
    nextTick(() => {
      const targets = (props.snapshot as any)?.targets || []
      const range = fitRanges(targets)
      if (range) {
        viewRange.value = range
        applyView()
      }
    })
  },
  { immediate: true },
)

/** 目标接近视窗边缘时的微调阈值（米） */
const EDGE_MARGIN_M = 120

/**
 * 播放中的边缘微调：目标靠近边缘时把视窗平移一点，
 * 但不改变缩放比例，避免画面持续缩放抖动。
 */
function nudgeFrameIfNeeded() {
  if (userViewAdjusted.value) return
  if (effectiveViewMode.value === 'overview') return
  const targets = (props.snapshot as any)?.targets || []
  if (!targets.length) return
  const sel = targets.find((t) => t.global_track_id === props.selectedTargetId) || targets[0]
  if (!sel || sel.east_m == null || sel.north_m == null) return
  const v = viewRange.value
  const m = EDGE_MARGIN_M
  let dx = 0
  let dy = 0
  if (sel.east_m < v.xMin + m) dx = v.xMin + m - sel.east_m
  else if (sel.east_m > v.xMax - m) dx = v.xMax - m - sel.east_m
  if (sel.north_m < v.yMin + m) dy = v.yMin + m - sel.north_m
  else if (sel.north_m > v.yMax - m) dy = v.yMax - m - sel.north_m
  if (!dx && !dy) return
  viewRange.value = {
    xMin: v.xMin + dx, xMax: v.xMax + dx,
    yMin: v.yMin + dy, yMax: v.yMax + dy,
  }
  applyView()
}
/**
 * 2.5D 场景样式：CSS 3D 轴测投影。
 *
 * 分两个空间：
 *  - .ws-ground：地面空间。整体 rotateX(倾角) + scale，再由父级 perspective 投影，
 *    形成「从南向北俯视」的斜视地面。底图、航迹、坐标层、接地阴影都在这里，
 *    它们同属一个平面，因此天然像素级对齐。
 *  - .ws-scene-upright：屏幕空间。建筑立牌、船只、标注在这里，
 *    位置由 projectScene() 把地面点投影到屏幕得到，
 *    于是立牌虽然「立」在斜视地面上，落点却与地面完全重合。
 */
const scene25dStyle = computed(() => {
  if (!view25d.value) {
    return { transform: 'none', transformStyle: 'flat' } as any
  }
  return {
    perspective: `${SCENE_PERSPECTIVE_PX}px`,
    perspectiveOrigin: '50% 50%',
    transformStyle: 'preserve-3d',
  } as any
})

/** 地面层的倾斜与缩放（2D 模式下为空） */
const sceneGroundStyle = computed(() => {
  if (!view25d.value) {
    return { transform: 'none', transformStyle: 'flat' } as any
  }
  return {
    transform: `rotateX(${SCENE_TILT_DEG}deg) scale(${SCENE_SCALE})`,
    transformStyle: 'preserve-3d',
  } as any
})

/**
 * 场景平面（容器像素）→ 屏幕像素。
 *
 * 【为什么不手写变换公式】
 * CSS 的 transform 链（rotateX + scale + perspective）矩阵乘法顺序、
 * transform-origin 的作用点、perspective-origin 的偏移，
 * 任何一处手推都可能与浏览器实际渲染差几十像素（实测确实差过 100px），
 * 表现为「立牌浮在地面上、不落地」。
 *
 * 因此这里直接读取浏览器算好的矩阵来复算：
 *   1. 取 .ws-ground 的 computed transform（matrix3d）与 transform-origin；
 *   2. 取 .ws-scene-3d 的 perspective 与 perspective-origin；
 *   3. 按 CSS 的规范顺序依次施加，得到与渲染完全一致的屏幕坐标。
 *
 * 返回 [屏幕x, 屏幕y, 透视缩放因子]；
 * 缩放因子用于让远处的立牌略小、近处略大，与地面纵深保持一致。
 */
function projectScene(x: number, y: number): [number, number, number] {
  if (!view25d.value) return [x, y, 1]
  const ground = groundEl.value
  const scene = sceneEl.value
  if (!ground || !scene) return [x, y, 1]

  const gcs = getComputedStyle(ground)
  const gmRaw = gcs.transform
  if (!gmRaw || gmRaw === 'none') return [x, y, 1]

  // 1) 元素自身的 transform（含 transform-origin 的平移补偿）
  //    CSS 语义：p' = T(O) · M · T(-O) · p
  const m = new DOMMatrix(gmRaw)
  const org = gcs.transformOrigin.split(' ').map((v) => parseFloat(v) || 0)
  const ox0 = org[0] || 0
  const oy0 = org[1] || 0
  const m2 = new DOMMatrix()
    .translate(ox0, oy0)
    .multiply(m)
    .translate(-ox0, -oy0)
  const pt = m2.transformPoint(new DOMPoint(x, y))
  const px = pt.x
  const py = pt.y
  const pz = pt.z

  // 2) 父级的 perspective（CSS：先按 perspective-origin 平移，再除以 (1 - z/d)，最后平移回来）
  const scs = getComputedStyle(scene)
  const persp = parseFloat(scs.perspective)
  if (!isFinite(persp) || persp <= 0) return [px, py, 1]
  const porg = scs.perspectiveOrigin.split(' ').map((v) => parseFloat(v) || 0)
  const pOx = porg[0] || 0
  const pOy = porg[1] || 0
  const qx = px - pOx
  const qy = py - pOy
  const w = 1 - pz / persp
  if (!isFinite(w) || Math.abs(w) < 1e-6) return [px, py, 1]
  const f = 1 / w
  return [pOx + qx * f, pOy + qy * f, f]
}

/**
 * 建筑立牌的抬升量（屏幕空间）。
 *
 * 立牌已由 projectScene() 精确对齐到倾斜后的地面点，
 * 这里再整体向上抬一点，形成「站在地面上」的高度感。
 */
const uprightTransform = computed(() => {
  if (!view25d.value) return 'none'
  /* 立牌的可见轮廓底边中点已精确对齐投影后的地面点，
   * 此处不能再做垂直平移，否则建筑会整体离开地面（悬空感）。 */
  return 'none'
})

/**
 * 地面倾斜角（度）：0 = 正俯视，值越大越接近水平。
 *
 * 【为什么要加大】
 * 30° 是「2.5D 地图」的经典俯角，但在核电站这种「大面积平面 + 少量高体量」的场景里，
 * 倾角不够时建筑的立面几乎看不到 —— 侧板被压得极薄，
 * 看起来就是二维插画。
 *
 * 42° 让建筑立面、堤坝侧面、船只侧板都获得足够高度，
 * 立体感才成立；同时地面纵向压缩到 cos42° ≈ 0.74，
 * 仍然保留足够的水面纵深用于铺满视窗。
 */
const SCENE_TILT_DEG = 42

/**
 * 地面缩放：倾斜后地面在屏幕上的投影会变小，
 * 用一个略大于 1 的缩放把它重新铺满绘图区。
 */
const SCENE_SCALE = 1.3

/**
 * 建筑抬升高度（像素）：立牌在屏幕空间中整体向上抬起的距离，
 * 形成「站在地面上」的高度感。数值过大建筑会明显脱离地面。
 */
const BUILDING_LIFT_PX = 16

/** 船体抬升高度（像素）：船体较矮，抬升量也相应减小 */
const SHIP_LIFT_PX = 10

/**
 * 船只显示放大系数（仅作用于 2.5D 立体视图）。
 *
 * 【为什么需要】
 * 船体按真实米制尺寸绘制，一艘 160m 的集装箱船在
 * 1920px 宽的大屏上只有几十像素 —— 从远景看几乎看不见，
 * 主体感被建筑和堤坝完全压掉。
 *
 * 这里统一放大 1.45 倍，让船在大屏上有足够的辨识度，
 * 同时保持「船比建筑矮得多」的相对关系（不变），
 * 因此不会破坏空间层次的正确性。
 *
 * 注意：该系数只影响显示尺寸，不改变航迹点位置，
 * 也不参与任何距离/方位量测。
 */
const SHIP_DISPLAY_SCALE = 1.45

/**
 * 船体侧板厚度系数（相对船体宽度）。
 * 船是唯一完全漂浮在水上的物体，必须有明确的吃水厚度，
 * 否则会像贴纸浮在水面。
 */
const SHIP_HULL_RATIO = 0.16

/**
 * 场景透视参数（须与 CSS .ws-scene-3d 的 perspective 一致）。
 *
 * 数值越小，透视越强（近大远小越明显），立体感越足；
 * 但过小会让远端厂区严重缩小、标签挤在一起。
 * 1500px 是在「纵深明显」与「厂区仍可读」之间的平衡点。
 */
const SCENE_PERSPECTIVE_PX = 1500

/**
 * 抬升后的透视补偿：求出元素在「场景平面坐标系」中的摆放位置。
 *
 * 背景：物件用 translateZ(Z) 抬离地面后，透视缩放 k = P/(P-Z) 会让它朝消失点收缩，
 * 从而偏离目标地面点（实测偏差可达 60 px）。故需反投影补偿。
 *
 * 坐标关系（po = perspective-origin）：
 *   最终屏幕位置 = po + (平面坐标 - po) · k
 * 要求抬升后轮廓中心仍落在目标屏幕位置 target：
 *   轮廓中心平面坐标 F_c = po + (target - po) / k
 *   元素中心平面坐标 F_e = F_c - off      （off：元素中心 → 轮廓中心 的平面内偏移）
 *   元素 left/top = F_e - 尺寸/2          （left/top 属于平面坐标系）
 *
 * 注意：返回的是「平面坐标」，不能直接当屏幕坐标使用。
 *
 * @param x,y       目标屏幕位置（未抬升时该点所在的位置）
 * @param offX/offY 元素中心 → 轮廓中心 的偏移（平面像素）
 * @param liftPx    抬升高度
 * @param originX/Y 场景的 perspective-origin（布局像素）
 */
function compensateLift(
  x: number,
  y: number,
  offX: number,
  offY: number,
  liftPx: number,
  originX: number,
  originY: number,
): [number, number] {
  const k = SCENE_PERSPECTIVE_PX / (SCENE_PERSPECTIVE_PX - liftPx)
  if (!isFinite(k) || k <= 0) return [x, y]
  return [
    originX + (x - originX) / k - offX,
    originY + (y - originY) / k - offY,
  ]
}

/**
 * 场景变换的实时取值（供外部读取，避免与 uprightTransform 不同步）。
 * 场景若因交互（如拖拽旋转）改变倾斜/旋转角，立牌与文字的反向旋转
 * 必须同步更新，否则立牌会随地面倾斜而歪斜、脱开锚点。
 */

const clockNote = computed(() => {
  const s: any = props.snapshot
  if (!s) return ''
  return `回放 ${(s.time_ms / 1000).toFixed(0)}秒`
})
</script>

<template>
  <div class="ws-chart-wrap" :class="{ 'is-under': isUnder, 'is-25d': view25d }">
    <!-- 示意背景层:核电厂区俯视示意框架(水面) / 水下监视区示意(水下),仅作示意,不参与距离方位计算 -->
    <!-- 2.5D 场景容器：承载地面层与立体物层的轴测投影 -->
    <div ref="sceneEl" :key="view25d ? '25d' : '2d'" class="ws-scene-3d" :style="scene25dStyle">
    <!-- 地面空间：底图 + 航迹 + 坐标层 + 接地阴影，同属一个倾斜平面，
         故它们彼此像素级对齐；2D 模式下不加倾斜。 -->
    <div ref="groundEl" class="ws-ground" :style="sceneGroundStyle">
    <!-- 底图定位到坐标层绘图区，保证与轨迹像素级对齐 -->
    <div class="ws-bg-holder">
    <!-- 港区矢量图层：防波堤 / 航道 / 泊位（素材包原生 SVG，正投影，可直接作地面平面） -->
    <svg
      v-if="!isUnder && harborVectorSprites.length"
      class="ws-harbor-vector"
      :viewBox="`0 0 ${chartSize.w} ${chartSize.h}`"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <image
        v-for="hv in harborVectorSprites"
        :key="hv.key"
        :href="hv.href"
        :x="0"
        :y="0"
        :width="hv.width"
        :height="hv.height"
        :transform="hv.transform"
        :opacity="hv.opacity"
      />
    </svg>
    <!-- 核电站建筑贴图：与航迹共用同一 ENU 投影 -->
    <svg
      v-if="!isUnder && !view25d && nppSprites.length"
      class="ws-npp-layer"
      :viewBox="`0 0 ${chartSize.w} ${chartSize.h}`"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <image
        v-for="np in nppSprites"
        :key="np.key"
        :href="np.href"
        :x="0"
        :y="0"
        :width="np.width"
        :height="np.height"
        :transform="np.transform"
      />
    </svg>
    <svg
      v-if="!isUnder"
      class="ws-harbor-bg"
      :viewBox="`0 0 ${chartSize.w} ${chartSize.h}`"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <!-- 海面：近岸（北，靠厂区）浅、远海（南）深（三段渐变，避免大面积纯色） -->
        <linearGradient id="wsWater" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#124658" />
          <stop offset="0.45" stop-color="#0B3348" />
          <stop offset="1" stop-color="#061E30" />
        </linearGradient>
        <!-- 陆地：基色 + 顶部受光边 -->
        <linearGradient id="wsLand" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#1b3038" />
          <stop offset="0.6" stop-color="#14252d" />
          <stop offset="1" stop-color="#0e1c24" />
        </linearGradient>
        <linearGradient id="wsLandEdge" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#3d6f80" />
          <stop offset="1" stop-color="#20505f" />
        </linearGradient>
        <!-- 近岸浅水带（贴岸） -->
        <linearGradient id="wsShore" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#2d7f8e" stop-opacity="0.5" />
          <stop offset="1" stop-color="#2d7f8e" stop-opacity="0" />
        </linearGradient>
        <radialGradient id="wsGlow" cx="0.5" cy="0.45" r="0.7">
          <stop offset="0" stop-color="#0e5f86" stop-opacity="0.22" />
          <stop offset="1" stop-color="#0e5f86" stop-opacity="0" />
        </radialGradient>
        <!-- 波纹：低透明度细线，重复平铺 -->
        <pattern id="wsRipple" width="26" height="14" widthUnits="userSpaceOnUse" heightUnits="userSpaceOnUse" patternTransform="rotate(-8)">
          <path d="M0,7 C6,3 12,3 18,7 C21,9 24,9 26,7" fill="none" stroke="#8fd6e8" stroke-width="0.8" stroke-opacity="0.5" />
        </pattern>
      </defs>
      <!-- 纯海水底：整幅铺满绘图区（不再是「一块深蓝矩形底板」）。
           米制网格由 ECharts 坐标层绘制，与底图共用同一视口，天然对齐。 -->
      <rect x="0" y="0" :width="chartSize.w" :height="chartSize.h" fill="url(#wsWater)" />
      <rect x="0" y="0" :width="chartSize.w" :height="chartSize.h" fill="url(#wsGlow)" />
      <!-- 海面波纹：极低透明度平铺，给大面积纯蓝一点材质层次（不与航迹争注意力） -->
      <rect x="0" y="0" :width="chartSize.w" :height="chartSize.h" fill="url(#wsRipple)" opacity="0.06" />
      <!-- 厂区示意要素：设计坐标 → 容器像素，与航迹共用同一 ENU 投影与视口 -->
      <g :transform="bgTransform">
      <!--
        陆域（核电厂区，示意）：
        厂区位于北侧，陆域是「完整地块」而非狭长色带；
        建筑、道路、绿地全部落在地块内，视觉上建筑才有「站在地上」的依托。
        设计坐标 y 向下（0 = 最北）：
          y 0..30     后方山体（高地）
          y 30..150   厂区陆域（核岛 / 常规岛 / 开关站 / 输电铁塔所在）
          y ≈150..170 岸线（含厂前小湾）
          y >170      海域
      -->
      <!-- 主体陆域：北高南低，南缘随海岸线起伏（厂前湾向南凸） -->
      <path
        d="M0,0 H1000 L1000,158 L960,166 L900,156 L840,168 L780,150 L720,164 L660,180 L600,170 L540,152 L480,166 L420,178 L360,162 L300,172 L240,158 L180,168 L120,154 L60,160 L0,152 Z"
        fill="url(#wsLand)" />
      <!-- 陆域边缘受光层：让地块有厚度，而不是一张平贴的色块 -->
      <path
        d="M0,0 H1000 L1000,158 L960,166 L900,156 L840,168 L780,150 L720,164 L660,180 L600,170 L540,152 L480,166 L420,178 L360,162 L300,172 L240,158 L180,168 L120,154 L60,160 L0,152 Z"
        fill="none" stroke="url(#wsLandEdge)" stroke-width="2.6" />
      <!-- 后方山体：厂址北侧高地，抬高画面纵深 -->
      <path d="M0,0 H1000 V22 L900,32 L812,22 L724,34 L636,22 L548,34 L460,24 L372,34 L284,22 L196,34 L108,22 L0,32 Z" fill="#0b1a21" opacity="0.92" />
      <path d="M0,0 H1000 V13 L900,23 L812,13 L724,24 L636,13 L548,24 L460,14 L372,24 L284,14 L196,24 L108,14 L0,23 Z" fill="#16272f" opacity="0.7" />
      <!-- 岸线：主线 + 外侧浅水带 + 内侧湿润带 -->
      <path d="M0,152 L60,160 L120,154 L180,168 L240,158 L300,172 L360,162 L420,178 L480,166 L540,152 L600,170 L660,180 L720,164 L780,150 L840,168 L900,156 L960,166 L1000,158"
            fill="none" stroke="#3fd2c0" stroke-width="2.2" opacity="0.8" />
      <path d="M0,160 L60,168 L120,162 L180,176 L240,166 L300,180 L360,170 L420,186 L480,174 L540,160 L600,178 L660,188 L720,172 L780,158 L840,176 L900,164 L960,174 L1000,166"
            fill="none" stroke="#3fd2c0" stroke-width="1" opacity="0.24" />
      <!-- 近岸浅水带（岸线外侧 12 设计单位） -->
      <path d="M0,152 L60,160 L120,154 L180,168 L240,158 L300,172 L360,162 L420,178 L480,166 L540,152 L600,170 L660,180 L720,164 L780,150 L840,168 L900,156 L960,166 L1000,158 L1000,170 L960,178 L900,168 L840,180 L780,162 L720,176 L660,192 L600,182 L540,164 L480,178 L420,190 L360,174 L300,184 L240,170 L180,180 L120,166 L60,172 L0,164 Z"
            fill="url(#wsShore)" />
      <!--
        厂区道路：只出现在陆地上（环形主干道 + 支路），
        不再把路画到海面上，避免与实测航迹混淆。
      -->
      <g fill="none" stroke-linecap="round">
        <!-- 环形主干道（沿厂区中轴） -->
        <path d="M210,132 L300,118 L400,124 L520,114 L640,122 L760,112 L860,120 L950,110" stroke="#2a3d47" stroke-width="5" opacity="0.95" />
        <path d="M210,132 L300,118 L400,124 L520,114 L640,122 L760,112 L860,120 L950,110" stroke="#456470" stroke-width="0.9" stroke-dasharray="7 6" opacity="0.5" />
        <!-- 支路：核岛 → 常规岛 / 冷却塔 / 开关站 -->
        <path d="M306,122 L312,86 M312,86 L470,94 M470,94 L470,120 M520,116 L540,82 M660,120 L690,84 M760,114 L790,80"
              stroke="#2a3d47" stroke-width="2.8" opacity="0.85" />
        <!-- 厂前区道路（沿岸内侧） -->
        <path d="M230,142 L340,138 L450,142 L560,136 L680,132 L790,126 L900,120" stroke="#243640" stroke-width="2.4" opacity="0.7" />
      </g>
      <!-- 绿地 / 厂前区绿化带（低饱和，不抢建筑） -->
      <g opacity="0.45">
        <path d="M244,74 L348,66 L348,84 L244,92 Z" fill="#1c3a30" />
        <path d="M604,66 L712,60 L712,80 L604,86 Z" fill="#1c3a30" />
        <path d="M800,112 L890,102 L896,120 L806,130 Z" fill="#1c3a30" />
        <path d="M150,120 L216,114 L216,132 L150,138 Z" fill="#1c3a30" />
      </g>
      <!-- 厂区围墙 / 场地边界（贴近陆域南缘，表示厂区范围） -->
      <path d="M206,136 L300,122 L400,128 L520,118 L640,126 L762,116 L858,124 L952,114"
            fill="none" stroke="#3c6b7d" stroke-width="1" opacity="0.36" stroke-dasharray="4 5" />
      <!--
        建筑场坪：每座建筑下方一块浅色场坪（设计坐标 → 容器像素）。
        建筑立牌正落在自己的场坪上，于是「坐」在地上而不是浮在地面之上。
      -->
      <g>
        <path
          v-for="pd in nppPads"
          :key="'pad-' + pd.key"
          :d="`M${pd.x},${pd.y + pd.h / 2} L${pd.x + pd.w / 2},${pd.y} L${pd.x + pd.w},${pd.y + pd.h / 2} L${pd.x + pd.w / 2},${pd.y + pd.h} Z`"
          fill="#2b4a58"
          fill-opacity="0.5"
          stroke="#63a3b8"
          stroke-opacity="0.26"
          stroke-width="0.8"
        />
      </g>
      <!-- 取排水系统（核电厂形态，示意）：
           取水为明渠，自取水口（设计 x=340，岸线 y≈170）向东南伸入海域；
           排水为暗渠，自排水口（设计 x=600，岸线 y≈170）先沿岸外推再向东南入海。 -->
      <g>
        <!-- 取水明渠：水道水体 + 两侧渠壁 + 中心线 -->
        <path d="M340,168 C360,210 390,256 430,300 C468,342 512,376 562,404" fill="none" stroke="#4dcddb" stroke-opacity=".14" stroke-width="28" stroke-linecap="round" />
        <path d="M326,166 C346,209 376,255 416,299 C454,341 498,375 548,403" fill="none" stroke="#8fc4d4" stroke-opacity=".5" stroke-width="1.4" stroke-dasharray="10 7" />
        <path d="M354,170 C374,211 404,257 444,301 C482,343 526,377 576,405" fill="none" stroke="#8fc4d4" stroke-opacity=".5" stroke-width="1.4" stroke-dasharray="10 7" />
        <path d="M340,168 C360,210 390,256 430,300 C468,342 512,376 562,404" fill="none" stroke="#6ce5e8" stroke-opacity=".55" stroke-width="1.1" stroke-dasharray="16 8 3 8" />
        <!-- 取水口喇叭口（贴岸） -->
        <path d="M324,166 L356,170 L352,182 L328,178 Z" fill="#8fe0b8" fill-opacity=".5" stroke="#8fe0b8" stroke-width="1.2" />
        <circle cx="340" cy="174" r="3.4" fill="#8fe0b8" />
        <!-- 取水末端滤网区 -->
        <g opacity=".45" stroke="#6ce5e8" stroke-width="1.2" stroke-linecap="round">
          <path d="M554,396 L572,404 M558,390 L576,398 M564,402 L582,410" />
        </g>
        <!-- 排水暗渠：暗色管体 + 中心虚线 -->
        <path d="M600,172 C626,202 660,234 704,264 C748,294 796,320 846,344" fill="none" stroke="#4d6170" stroke-opacity=".65" stroke-width="5" stroke-linecap="round" />
        <path d="M600,172 C626,202 660,234 704,264 C748,294 796,320 846,344" fill="none" stroke="#c2a06a" stroke-opacity=".5" stroke-width="1.3" stroke-dasharray="9 6" />
        <!-- 排水扩散区 -->
        <g opacity=".38" fill="#c2a06a">
          <ellipse cx="852" cy="348" rx="20" ry="8" transform="rotate(28 852 348)" />
          <ellipse cx="868" cy="356" rx="13" ry="6" transform="rotate(28 868 356)" />
        </g>
        <!-- 排水口（贴岸） -->
        <path d="M588,168 L614,176 L610,186 L590,178 Z" fill="#f0c08a" fill-opacity=".45" stroke="#f0c08a" stroke-width="1.2" />
        <circle cx="600" cy="177" r="3.4" fill="#f0c08a" />
      </g>
      <!--
        防波堤 / 长堤（有厚度的石堤，而非画在海上的粗环线）：
        用「堤体 + 堤顶高光 + 外侧浪花」三层表达厚度，线条宽度收敛，不再抢视觉中心。
      -->
      <g fill="none" stroke-linecap="round" :opacity="view25d ? 0.28 : 1">
        <!-- 主堤（东南环抱，绕开厂区陆域）
             2.5D 下由立体构筑物层绘制顶面+立面，
             这里只保留一条极弱的底线作为「堤在地面上的接触痕」。 -->
        <path d="M600,196 C720,214 850,250 906,320 C948,372 890,430 764,466 C630,504 460,494 340,456 C250,428 216,378 246,322"
              stroke="#2b3a44" stroke-width="9" opacity="0.88" />
        <path d="M600,192 C720,210 850,246 906,316 C948,368 890,426 764,462 C630,500 460,490 340,452 C250,424 216,374 246,318"
              stroke="#47606d" stroke-width="3.4" opacity="0.6" />
        <!-- 外侧浪花（细虚线，弱） -->
        <path d="M600,202 C720,220 850,256 906,326 C948,378 890,436 764,472 C630,510 460,500 340,462 C250,434 216,384 246,328"
              stroke="#7fc4d6" stroke-width="1" stroke-dasharray="6 9" opacity="0.22" />
        <!-- 西短堤 -->
        <path d="M232,182 C286,212 320,256 336,308" stroke="#2b3a44" stroke-width="7.5" opacity="0.85" />
        <path d="M232,178 C286,208 320,252 336,304" stroke="#47606d" stroke-width="3" opacity="0.58" />
        <path d="M232,187 C286,217 320,261 336,313" stroke="#7fc4d6" stroke-width="0.9" stroke-dasharray="5 8" opacity="0.2" />
      </g>
      <!--
        构筑物基脚浪花（仅 2.5D）：
        贴在堤体外侧的破碎白浪，让水陆交界自然。
        位于地面空间内，随地面一同倾斜。
      -->
      <g v-if="view25d" class="ws-foam-layer">
        <line
          v-for="fm in structureFoam"
          :key="'fm-' + fm.key"
          :x1="0"
          :y1="0"
          :x2="0"
          :y2="0"
          :style="{
            stroke: 'rgba(196, 236, 250, 1)',
            strokeWidth: fm.h + 'px',
            strokeLinecap: 'round',
            opacity: fm.opacity,
          }"
          :transform="`translate(${fm.x}, ${fm.y}) rotate(${fm.angle})`"
        />
      </g>
      <!--
        立体构筑物层（仅 2.5D）：
        防波堤 / 渠壁 / 栈桥在这里以「顶面 + 立面」的多边形绘制，
        立面向观察者（南侧）展开，形成真实的厚度与体量，
        不再是平贴在地面的描边 —— 这是「立体感」的关键。
        绘制顺序由北向南，南侧体量压住北侧，遮挡关系正确。
      -->
      <g v-if="view25d" class="ws-volume-layer">
        <g v-for="sv in structureVolumes" :key="'sv-' + sv.key">
          <!-- 立面：先画，位于顶面之下。受光面亮、背光面暗，形成体积 -->
          <path
            v-for="(sf, si) in sv.sides"
            :key="'sd-' + sv.key + '-' + si"
            :d="sf.d"
            :fill="sf.lit ? sv.litSide : sv.side"
            fill-opacity="0.96"
          />
          <!-- 顶面：受光，明显亮于立面 -->
          <path
            :d="sv.topPath"
            :fill="sv.top"
            fill-opacity="0.95"
            :stroke="sv.edge || sv.top"
            stroke-opacity="0.5"
            stroke-width="0.7"
          />
        </g>
      </g>
      <!-- 堤头灯桩（替代大面积粗环，给出方位锚点） -->
      <g>
        <circle cx="906" cy="320" r="2.6" fill="#ffd54a" opacity="0.85" />
        <circle cx="906" cy="320" r="6" fill="none" stroke="#ffd54a" stroke-width="0.8" opacity="0.3" />
        <circle cx="336" cy="308" r="2.4" fill="#ffd54a" opacity="0.8" />
        <circle cx="336" cy="308" r="5.6" fill="none" stroke="#ffd54a" stroke-width="0.8" opacity="0.28" />
      </g>
      <!-- 警戒巡逻航线：改为细虚线（不再是粗环），仅在陆侧湾内一圈 -->
      <g opacity="0.28">
        <path d="M350,190 C470,214 630,220 776,258 C874,286 876,354 768,388 C634,426 442,412 352,356 C296,320 300,232 350,190"
              stroke="#2fd6e8" stroke-width="1" stroke-dasharray="9 8" fill="none" />
        <circle cx="350" cy="196" r="2.4" fill="#ffd54a" opacity="0.65" />
      </g>
      <!-- 前池湾（弱化）：只保留一层淡轮廓 -->
      <ellipse cx="450" cy="280" rx="130" ry="46" fill="rgba(47,214,232,0.03)" stroke="#2aa5c8" stroke-width="0.9" stroke-dasharray="10 8" opacity="0.36" />
      </g>
    </svg>
    <svg v-else class="ws-harbor-bg" viewBox="0 0 1000 500" preserveAspectRatio="none" aria-hidden="true" style="inset: 0; width: 100%; height: 100%">
      <rect x="0" y="0" width="1000" height="500" fill="#020f1e" />
      <rect x="0" y="60" width="1000" height="130" fill="#062033" opacity="0.8" />
      <rect x="0" y="190" width="1000" height="130" fill="#041827" opacity="0.8" />
      <rect x="0" y="320" width="1000" height="180" fill="#020d18" opacity="0.9" />
      <text x="18" y="40" fill="#5f93ad" font-size="17">水下监视区示意（未标定，不显示距离）</text>
      <text x="18" y="130" fill="#33566b" font-size="15">浅层（示意）</text>
      <text x="18" y="260" fill="#33566b" font-size="15">中层（示意）</text>
      <text x="18" y="400" fill="#33566b" font-size="15">深层（示意）</text>
      <g opacity="0.85">
        <path d="M500,250 L380,420 L620,420 Z" fill="rgba(255,213,74,0.08)" stroke="#ffd54a" stroke-dasharray="6 5" />
        <circle cx="500" cy="250" r="7" fill="none" stroke="#ffd54a" stroke-width="2" />
        <text x="516" y="255" fill="#c9a94f" font-size="15">声纳站 S01（示意位置）</text>
      </g>
    </svg>
    </div>
    <div ref="el" class="ws-chart"></div>

    <!-- 背景船只图层：真实船型贴图，与实测目标船体同一坐标系 -->
    <svg
      v-if="!isUnder && !view25d && backgroundShipSprites.length"
      class="ws-bg-ship-layer"
      :viewBox="`0 0 ${chartSize.w} ${chartSize.h}`"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <image
        v-for="bs in backgroundShipSprites"
        :key="bs.key"
        :href="bs.href"
        :x="0"
        :y="0"
        :width="bs.width"
        :height="bs.height"
        :transform="bs.transform"
        :opacity="bs.opacity"
      />
    </svg>
    <!-- 船体贴图层：与轨迹共用同一 ENU 投影，随 2.5D 场景一同倾斜 -->
    <svg
      v-if="!isUnder && !view25d"
      class="ws-ship-layer"
      :viewBox="`0 0 ${chartSize.w} ${chartSize.h}`"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <image
        v-for="sp in shipSprites"
        :key="sp.key"
        :href="sp.href"
        :x="0"
        :y="0"
        :width="sp.width"
        :height="sp.height"
        :transform="sp.transform"
        :opacity="sp.selected ? 1 : 0.88"
      />
    </svg>

    <!-- 接地阴影层：位于地面空间，贴在倾斜后的地面/水面上，
         为建筑与船只提供「站在地面上」的接触线索。
         阴影坐标用的是「地面平面坐标」，故与倾斜后的地面严丝合缝。 -->
    <div v-if="!isUnder && view25d" class="ws-shadow-layer" aria-hidden="true">
      <i
        v-for="sd in groundShadows"
        :key="'sh-' + sd.key"
        class="ws-ground-shadow"
        :class="{ 'ws-ground-shadow--bar': sd.angle != null }"
        :style="{
          left: sd.x + 'px',
          top: sd.y + 'px',
          width: sd.w + 'px',
          height: sd.h + 'px',
          transform: sd.angle != null
            ? `translate(-50%, -50%) rotate(${sd.angle}deg)`
            : 'translate(-50%, -50%)',
        }"
      />
    </div>
    <!-- 地面空间结束 -->
    </div>

    <!-- 屏幕空间层：建筑立牌、立体船体、场景标注。
         位置由 projectScene() 把地面点投影到屏幕得到，
         因此立牌「立」在斜视地面上，落点与地面严丝合缝，且始终正立可读。 -->
    <div v-if="!isUnder" class="ws-scene-upright" aria-hidden="true">
      <!--
        冷却塔水雾：
        锚定在塔顶向上飘散，是厂区最强的垂直线索之一。
        置于最底层，避免盖住塔体与标签。
      -->
      <img
        v-for="sp in steamPlumes"
        :key="'pl-' + sp.key"
        :src="sp.href"
        class="ws-steam-plume"
        :style="{
          left: sp.left + 'px',
          top: sp.top + 'px',
          width: sp.w + 'px',
          height: sp.h + 'px',
          opacity: sp.opacity,
          transform: uprightTransform,
        }"
        alt=""
      />

      <!--
        竖向构筑物（灯桩 / 桅杆）：
        底部锚点由 projectScene() 投影到地面点，柱体竖直向上。
        这是场景里最强的三维线索 —— 俯视 2D 底图永远画不出「一根竖杆」。
      -->
      <div
        v-for="vp in verticalProps"
        :key="'vp-' + vp.key"
        class="ws-vertical-prop"
        :style="{
          left: vp.left + 'px',
          top: vp.top + 'px',
          width: vp.wPx + 'px',
          height: vp.hPx + 'px',
          transform: uprightTransform,
        }"
      >
        <!-- 柱体：左侧受光、右侧背光的双渐变，形成圆柱体积 -->
        <i class="ws-vertical-prop-body" :style="{ background: `linear-gradient(90deg, ${vp.color} 0%, ${vp.color} 34%, ${shade(vp.color, -0.45)} 100%)` }" />
        <!-- 灯室 / 塔顶 -->
        <i class="ws-vertical-prop-cap" :style="{ background: vp.capColor }" />
        <!-- 顶端灯光 -->
        <i
          v-if="vp.glow"
          class="ws-vertical-prop-glow"
          :style="{ background: vp.glowColor, boxShadow: `0 0 10px 3px ${vp.glowColor}` }"
        />
      </div>

      <!-- 建筑立牌：厂区标签嵌套在立牌内部，随立牌一同变换 -->
      <div
        v-for="nb in nppBillboards"
        :key="nb.key"
        class="ws-npp-billboard"
        :style="{
          left: nb.x + 'px',
          top: nb.y + 'px',
          width: nb.w + 'px',
          height: nb.h + 'px',
          transform: uprightTransform,
        }"
      >
        <!-- 基座体：衔接素材底部与地面，让建筑「长」在地面上而非浮着 -->
        <i
          class="ws-npp-mass"
          :style="{
            height: nb.massH + 'px',
            inset: '-1px 0 auto 0',
            background: `linear-gradient(180deg, ${nb.massFill} 0%, ${nb.massSide} 100%)`,
            borderLeft: nb.massInset + 'px solid transparent',
            borderRight: nb.massInset + 'px solid transparent',
          }"
        />
        <img :src="nb.href" class="ws-npp-billboard-img" alt="" />
        <span
          v-if="nb.label"
          class="ws-bg-label ws-bg-label--attached"
          :style="{ left: nb.labelX + 'px', top: nb.labelY + 'px', color: nb.labelFill, fontSize: nb.labelSize + 'px' }"
        >{{ nb.label }}</span>
      </div>

      <!-- 立体船体：水面上的立体物件，底边锚在水面点，抬升形成纵深 -->
      <div
        v-for="sb in shipBillboards"
        :key="'bb-' + sb.key"
        class="ws-ship-billboard"
        :style="{
          left: sb.left + 'px',
          top: sb.top + 'px',
          width: sb.w + 'px',
          height: sb.h + 'px',
          opacity: sb.opacity,
          transform: `translateY(${-SHIP_LIFT_PX}px)`,
        }"
      >
        <!-- 旋转容器：侧板与贴图一起按航向转动，保持船体厚度方向正确 -->
        <div class="ws-ship-rotor" :style="{ transform: `rotate(${sb.angle.toFixed(3)}deg)` }">
          <!-- 吃水泡沫：在船体水线处一圈淡亮边，强化「浮在水上」的体积感 -->
          <i class="ws-ship-wake" :style="{ bottom: -(sb.hullH * 0.55) + 'px' }" />
          <!-- 船体立体侧板：俯视船体贴图下方叠一层深调侧板，
               形成「有厚度的船体」观感（等价于 RTS 中的低多边形船体）。 -->
          <i class="ws-ship-hull" :style="{ height: sb.hullH + 'px' }" />
          <img :src="sb.href" class="ws-ship-billboard-img" alt="" />
        </div>
      </div>

      <!-- 场景标注（长堤/航道等）：直接立在地面点上，同样反向旋转保持正立 -->
      <span
        v-for="(lb, i) in bgLabelPositions"
        :key="i"
        class="ws-bg-label ws-scene-upright-label"
        :style="{
          left: lb.x + 'px',
          top: lb.y + 'px',
          color: lb.fill,
          fontSize: lb.size + 'px',
          fontWeight: lb.weight || 'normal',
          opacity: lb.opacity ?? 0.9,
        }"
        :class="{ 'is-hidden': !lb.visible, 'ws-bg-label--chip': lb.chip !== false }"
      >{{ lb.text }}</span>
    </div>
    </div>

    <div class="ws-chart-hud">
      <span class="ws-tag">{{ isUnder ? '水下监视区示意 · 未标定' : '实测航迹回放 · 核电厂区背景示意' }}</span>
      <span class="ws-chart-north">↑ 真北方向</span>
      <span class="ws-chart-clock">{{ clockNote }}</span>
    </div>
    <div class="ws-chart-legend">
      <span><i class="sw fused"></i>统一航迹</span>
      <span><i class="sw radar"></i>雷达源点迹</span>
      <span><i class="sw ais"></i>AIS源点迹</span>
      <span v-if="!isUnder"><i class="sw ring"></i>距离环（1/2/3公里）</span>
      <span v-else><i class="sw time"></i>时间标记 30秒</span>
    </div>
    <div class="ws-chart-scale"><div class="scale-line" :style="{ width: scalePx + 'px' }"></div><span>{{ scaleLabel }}</span></div>
    <div class="ws-chart-zoom">
      <!--
        缩放：只保留放大，不提供缩小。
        画面已由「自动取景」铺满视窗，再缩小就会出现大片空海；
        放大最多到 1.5 倍（见 ZOOM_IN_MAX_FACTOR），用于看细节。
      -->
      <button
        type="button"
        :title="`放大（最多 ${ZOOM_IN_MAX_FACTOR} 倍）`"
        :disabled="!canZoomIn"
        @click="zoomBy(0.7)"
      >＋</button>
      <!--
        复位比例：把缩放恢复到初始自适应比例。
        与「主体 / 跟随 / 总览」的区别是 —— 那三个切取景模式，
        这个只复位比例尺，取景模式不变。
      -->
      <button
        type="button"
        title="复位比例：恢复初始比例尺"
        class="ws-zoom-reset"
        @click="resetScale()"
      >复位</button>
      <button
        type="button"
        title="主体适配：厂区与目标铺满视窗"
        class="ws-zoom-reset"
        :class="{ active: effectiveViewMode === 'subject' }"
        @click="setViewMode('subject')"
      >主体</button>
      <button
        type="button"
        title="目标跟随：跟随当前选中目标"
        class="ws-zoom-reset"
        :class="{ active: effectiveViewMode === 'follow' }"
        @click="setViewMode('follow')"
      >跟随</button>
      <button
        type="button"
        title="全域总览：显示雷达站与全部目标的相对位置"
        class="ws-zoom-reset"
        :class="{ active: effectiveViewMode === 'overview' }"
        @click="setViewMode('overview')"
      >总览</button>
      <button
        type="button"
        title="切换 2.5D / 2D 视图"
        class="ws-zoom-25d"
        :class="{ active: view25d }"
        @click="view25d = !view25d"
      >2.5D</button>
    </div>
    <div class="ws-chart-hint">滚轮缩放 · 拖拽平移</div>

    <!--
      雷达站 R01 在画面外时的边缘方向提示。
      R01 不参与「主体适配」的计算（否则视野会被撑大），
      这里仅用一个小箭头提示它的相对方位，点击「总览」可看完整位置关系。
    -->
    <div
      v-if="stationEdgeHint"
      class="ws-station-hint"
      :style="{ left: stationEdgeHint.x + 'px', top: stationEdgeHint.y + 'px' }"
      title="雷达站 R01 在此方位（点击「总览」查看完整位置关系）"
    >
      <span class="ws-station-hint-arrow" :style="{ transform: `rotate(${stationEdgeHint.bearing}deg)` }">▲</span>
      <span class="ws-station-hint-text">R01</span>
    </div>
    <div v-if="!(snapshot?.targets || []).length" class="ws-chart-empty">
      {{ isUnder ? '当前无水下实测目标（声纳未标定，不显示距离）' : '当前视图无目标' }}
    </div>
    <!-- 角标说明：所有「示意」类说明集中在一处，画面内不再逐条重复 -->
    <div class="ws-chart-note">
      <span>底图为核电厂区示意框架，不代表真实岸线与水深 · 设施位置仅作示意</span>
      <span>距离方位以雷达站实测站址（北纬37.53297°，东经121.4199°）为准</span>
    </div>
  </div>
</template>

<style scoped>
.ws-chart-wrap { position: absolute; inset: 0; background: #02101f; overflow: hidden; }
/* 拖拽时切换为「抓取」光标，给出可交互的视觉反馈 */
.ws-chart-wrap { cursor: grab; }
.ws-chart-wrap.is-dragging { cursor: grabbing; }
/* 触摸设备上不显示抓取光标（无意义），但保留拖拽能力 */
@media (hover: none) {
  .ws-chart-wrap { cursor: default; }
}
/* ===== 2.5D 场景（底图 2D + 物件立体）=====
 * 设计：底图保持正俯视 2D 平面（不倾斜），建筑与船只作为立体物件「立」在底图上，
 * 做法同 RTS 游戏：地面是 2D，物件是 3D billboard。
 *
 * 关键点：
 * 1. 场景容器不施加 rotateX/rotateZ，底图与航迹完全保持 2D 正俯视；
 * 2. 立体物件用 translateZ 沿视线方向抬升（相机正对屏幕时即「向上立起」）；
 * 3. 物件底部锚点通过 transform-origin 固定在地面点，不会随抬升漂移；
 * 4. 立牌位于「屏幕空间」，其落点由 projectScene() 从地面点投影得到，
 *    故与倾斜后的地面严格重合（实测误差 < 1px）。
 */
.ws-scene-3d {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  transform-origin: 50% 50%;
  will-change: transform;
  /* 场景层（底图 / 立牌 / 船只 / 标注）覆盖在 ECharts 容器之上，
   * 若保持 pointer-events:auto 会把鼠标事件全部吃掉，
   * 导致「拖不动、缩不了」。这里让事件穿透到下层的图表容器。 */
  pointer-events: none;
}
/* 仅在 2.5D 态下启用过渡，切回 2D 立即生效 */
.ws-chart-wrap.is-25d .ws-scene-3d {
  transition: transform 0.45s cubic-bezier(0.4, 0, 0.2, 1);
}
/* ===== 地面空间 =====
 * 底图、航迹、坐标层、接地阴影全部位于此容器内，
 * 整体绕 X 轴倾斜形成斜视地面。
 * 四者同属一个平面 ⇒ 彼此像素级对齐，永远不会分层错位。 */
.ws-ground {
  position: absolute;
  inset: 0;
  /* 明确高度：父级 .ws-scene-3d 若为 static，子元素的 height:100%
   * 会退化为 auto（高度按内容算），导致 ECharts 容器高度为 0。
   * 这里让地面层绝对定位并撑满场景容器，为百分比高度提供确定基准。 */
  width: 100%;
  height: 100%;
  transform-origin: 50% 50%;
  transform-style: preserve-3d;
}
/* 地面各层都在同一 z=0 平面，杜绝层级缝隙 */
.ws-ground .ws-harbor-bg,
.ws-ground .ws-chart,
.ws-ground .ws-harbor-vector,
.ws-ground .ws-npp-layer,
.ws-ground .ws-bg-ship-layer,
.ws-ground .ws-ship-layer {
  transform: translateZ(0);
}
/* 2.5D 下：HUD/图例/比例尺保持正对屏幕 */
.ws-chart-wrap.is-25d .ws-chart-hud,
.ws-chart-wrap.is-25d .ws-chart-legend,
.ws-chart-wrap.is-25d .ws-chart-scale,
.ws-chart-wrap.is-25d .ws-chart-note,
.ws-chart-wrap.is-25d .ws-chart-zoom,
.ws-chart-wrap.is-25d .ws-chart-hint,
.ws-chart-wrap.is-25d .ws-chart-empty {
  position: absolute;
  z-index: 30;
}
/* 2.5D 模式下的氛围层：暗角 + 大气透视。
 *
 * 【为什么要大气透视】
 * 真实的三维场景里，远处会因为空气散射而**变淡、变蓝**。
 * 这层渐变让画面上半部（远端厂区）偏淡偏冷，
 * 下半部（近端海域）保持清晰饱和 ——
 * 于是深度不只靠透视缩放来表达，还靠**清晰度**来表达，
 * 这是让 2.5D 摆脱「贴纸感」的关键一笔。 */
.ws-chart-wrap.is-25d::after {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 20;
  background:
    /* 远端大气：上方偏冷偏淡 */
    linear-gradient(
      180deg,
      rgba(88, 142, 176, 0.2) 0%,
      rgba(60, 110, 148, 0.1) 22%,
      rgba(20, 60, 92, 0) 46%
    ),
    /* 暗角：把注意力收进画面中心 */
    radial-gradient(ellipse at 50% 52%, rgba(0, 0, 0, 0) 38%, rgba(1, 8, 16, 0.58) 100%);
}
/* ===== 立体物件层 =====
 * 立牌（建筑）、船体与厂区标签都位于 .ws-scene-3d 内部，
 * 与蓝色底图共享同一 3D 空间，于是：
 *   - 物件按 translateZ 抬升，与底图共享同一透视，形成真实纵深；
 *   - 底图、航迹、地面注记始终保持 2D 正俯视，不受抬升影响；
 *   - 厂区标签嵌套在立牌内部，随建筑一同变换，绝不脱开。
 */
/* ===== 屏幕空间 =====
 * 建筑立牌、立体船体、场景标注位于此层：
 * 位置由 projectScene() 从地面点投影得到，故与倾斜后的地面严丝合缝，
 * 同时保持正立（不随地面倾斜而歪斜）。 */
.ws-scene-upright {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 11;
  overflow: visible;
}

/* 建筑立牌：底部锚在地面点上，向上立起。
 * 位置与尺寸已由 JS 精确算好（含透视反投影补偿），
 * 故 transform-origin 固定为 0 0，避免二次偏移。 */
.ws-npp-billboard {
  position: absolute;
  display: block;
  transform-origin: 0 0;
  /* 屏幕空间，无需 3D 上下文 */
  transform: translateZ(0);
}
/* 建筑基座体（棱台）。
 *
 * 45° 素材自带立面，但底部与地面之间没有实体过渡，
 * 视觉上像「贴纸浮在图上」。
 * 这里在素材下方补一段梯形基座：
 *   - 上边与素材底边同宽；
 *   - 下边略窄（用负 margin 的透明边框把两侧收进去，形成收分）；
 *   - 顶部亮、底部暗，配合材质渐变形成圆柱/棱柱的体积暗示。
 *
 * 它同时充当建筑与地面之间的「实体连接」，
 * 让整个厂区从平面剪影变成有体量的建筑群。 */
.ws-npp-mass {
  position: absolute;
  display: block;
  width: 100%;
  box-sizing: border-box;
  pointer-events: none;
  /* 底部收分：左右透明边框把主体内容区收窄，形成上宽下窄的棱台 */
  background-clip: padding-box;
  /* 顶部高光，模拟受光顶边 */
  box-shadow:
    inset 0 1px 0 rgba(150, 200, 220, 0.28),
    inset 0 -8px 14px rgba(0, 0, 0, 0.45);
  border-bottom: 1px solid rgba(0, 0, 0, 0.5);
  z-index: -1;
}

/* 建筑立牌贴图。
 *
 * 素材是深色调写实风，直接贴在深色底图上几乎看不见；
 * 但 filter 会展平 3D 上下文（抬升失效、贴图被压扁），
 * 因此改用「opacity + 混合模式 + drop-shadow 分离层」的方式：
 *   - mix-blend-mode: screen 让深色部分透出底图，亮部保留；
 *   - 单独的影子层（::after）提供落地感与轮廓分离。
 */
.ws-npp-billboard-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
  /* 不可在 3D 元素上使用 filter：会强制展平其 3D 渲染上下文。 */
  opacity: 0.98;
  mix-blend-mode: screen;
  /* 提高亮度与对比度，让建筑在深色底图上「凸」出来；
   * drop-shadow 在贴图轮廓外生成一圈暗边，
   * 配合基座形成清晰的体量边界。 */
  filter:
    brightness(1.42) saturate(1.14) contrast(1.1)
    drop-shadow(0 3px 5px rgba(0, 0, 0, 0.55));
}

/* 接地阴影层：贴在地面/水面，与立体物件层分离，
 * 通过「物体下方有影」这一线索建立物体与地面的接触关系。 */
.ws-shadow-layer {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 10;
  overflow: visible;
}
/* 接地阴影：与建筑底座同宽同位置，方向统一朝右下方（与场景光照一致），
 * 透明度压低到 0.15~0.25，避免变成画面上的黑斑。 */
.ws-ground-shadow {
  position: absolute;
  transform: translate(-50%, -50%);
  border-radius: 50%;
  background: radial-gradient(
    ellipse at 42% 42%,
    rgba(0, 0, 0, 0.5) 0%,
    rgba(0, 0, 0, 0.22) 46%,
    rgba(0, 0, 0, 0) 76%
  );
  filter: blur(1.5px);
  pointer-events: none;
}

/* 构筑物（堤坝 / 渠壁 / 栈桥）的落地阴影：
 * 条形而非椭圆，按堤段走向旋转，模拟斜射光下的投影。 */
.ws-ground-shadow--bar {
  border-radius: 40%;
  background: linear-gradient(
    90deg,
    rgba(0, 0, 0, 0) 0%,
    rgba(0, 0, 0, 0.34) 18%,
    rgba(0, 0, 0, 0.38) 50%,
    rgba(0, 0, 0, 0.34) 82%,
    rgba(0, 0, 0, 0) 100%
  );
  filter: blur(3px);
}

/* 冷却塔水雾：
 * 向上扩散的蒸汽羽流，带轻微的呼吸动画，
 * 让静止的场景有「厂区在运行」的生气。 */
.ws-steam-plume {
  position: absolute;
  transform-origin: 50% 100%;
  pointer-events: none;
  mix-blend-mode: screen;
  opacity: 0.34;
  z-index: 0;
  animation: ws-steam-breathe 7.5s ease-in-out infinite;
}
@keyframes ws-steam-breathe {
  0%,
  100% {
    opacity: 0.26;
    transform: scaleY(0.97) translateX(0);
  }
  50% {
    opacity: 0.42;
    transform: scaleY(1.04) translateX(1.5%);
  }
}

/* 构筑物基脚浪花：
 * 轻微模糊 + 不规则断口，模拟碎浪而非实线。 */
.ws-foam-layer {
  filter: blur(1.2px);
}
.ws-foam-layer line {
  stroke-dasharray: 5 3.5;
}

/* ===== 竖向构筑物（灯桩 / 桅杆）=====
 * 场景里最强的三维线索。
 * 底部锚点贴着倾斜后的地面点，柱体竖直向上，
 * 于是观众一眼就能判断出这是一个三维空间而非平面图。 */
.ws-vertical-prop {
  position: absolute;
  transform-origin: 50% 100%;
  pointer-events: none;
  z-index: 1;
}
/* 柱体：左侧受光、右侧背光，形成圆柱体积感 */
.ws-vertical-prop-body {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  display: block;
  border-radius: 40% / 8%;
  box-shadow:
    inset 0 0 0 1px rgba(0, 0, 0, 0.35),
    1px 0 3px rgba(0, 0, 0, 0.45);
}
/* 塔顶 / 灯室 */
.ws-vertical-prop-cap {
  position: absolute;
  left: -18%;
  right: -18%;
  top: -12%;
  height: 16%;
  display: block;
  border-radius: 50% / 40%;
  box-shadow: inset 0 -1px 2px rgba(0, 0, 0, 0.45);
}
/* 顶端灯光 */
.ws-vertical-prop-glow {
  position: absolute;
  left: 50%;
  top: -14%;
  width: 42%;
  height: 9%;
  margin-left: -21%;
  display: block;
  border-radius: 50%;
  opacity: 0.95;
}

/* 立体船体：外层负责定位与抬升，内层负责航向旋转。
 * 分两层是为了避免「抬升 + 旋转」写在同一个 transform 里互相干扰。 */
.ws-ship-billboard {
  position: absolute;
  transform-origin: 50% 50%;
  transform: translateZ(0);
}
/* 船体侧板：位于船体贴图下方并向下偏移，
 * 用比船体更暗的蓝灰表现船舷侧面，形成厚度感。
 * 侧板自身不做旋转，随船体一起绕中心转动。
 *
 * 【立体构造】
 * 一个真正的船体侧面不是矩形，而是：
 *   - 上缘 = 甲板线（受光，亮）
 *   - 中段 = 舷侧（背光，最暗）
 *   - 下缘 = 水线（略微回亮，形成吃水线的亮边）
 * 用三层渐变 + 上下不对称圆角（船首尖、船尾方），
 * 就能得到有船型特征的立面，而不是一个圆角矩形。 */
.ws-ship-hull {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  /* 船首（右端）收窄、船尾（左端）方钝 */
  border-radius: 14% 34% 34% 14% / 16% 24% 24% 16%;
  background:
    /* 水线亮边 */
    linear-gradient(
      to bottom,
      rgba(58, 104, 132, 0.9) 0%,
      rgba(18, 44, 66, 0.96) 18%,
      rgba(8, 24, 38, 1) 72%,
      rgba(4, 13, 22, 1) 100%
    );
  transform: translateY(42%);
  /* 甲板线高光 + 水线暗边 */
  box-shadow:
    inset 0 1px 0 rgba(140, 200, 226, 0.4),
    inset 0 -3px 5px rgba(0, 0, 0, 0.55),
    0 3px 7px rgba(0, 0, 0, 0.55);
  pointer-events: none;
}

/* 船体吃水泡沫圈：在水线处加一圈极淡的亮边，
 * 强化「船浮在水上」而不是「贴在图上」的观感。 */
.ws-ship-wake {
  position: absolute;
  left: -6%;
  right: -6%;
  bottom: 0;
  height: 42%;
  border-radius: 30% / 40%;
  background: radial-gradient(
    ellipse at 50% 60%,
    rgba(190, 235, 248, 0.22) 0%,
    rgba(140, 210, 235, 0.1) 42%,
    rgba(120, 200, 230, 0) 72%
  );
  filter: blur(3px);
  pointer-events: none;
  z-index: -2;
}

/* 船体旋转容器：承载侧板与贴图，整体按航向角旋转 */
.ws-ship-rotor {
  position: absolute;
  inset: 0;
  transform-origin: 50% 50%;
}

.ws-ship-billboard-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  /* 必须是 fill：原 SVG 矩阵对素材做了非等比缩放（船长/船宽比例不同），
   * 用 contain 会额外留边，导致船体尺寸与 2D 模式不一致。 */
  object-fit: fill;
  transform-origin: 50% 50%;
  /* 船体贴图为俯视素材，需提亮以融入深蓝底图；
   * 不使用 filter（filter 会展平 3D 上下文，使抬升失效）。 */
  opacity: 0.95;
  mix-blend-mode: screen;
}

/* 场景标注（长堤/航道等）：平贴地面，保持地图注记的读法。
 * 底图是 2D 正俯视，地面注记不应被抬升或倾斜。 */
.ws-scene-upright-label {
  transform-origin: 50% 50%;
  transform: translate(-50%, -50%);
}

/* 底图标签层：与 .ws-chart 同坐标系，不随 3D 场景倾斜 */
.ws-bg-labels {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 12;
  overflow: hidden;
}
/* 场景标注：统一胶囊样式（半透明深底 + 细描边），
 * 让文字在海面/陆地上都清晰可读，且不显杂乱。 */
.ws-bg-label {
  position: absolute;
  transform: translate(-50%, -50%);
  white-space: nowrap;
  line-height: 1;
  letter-spacing: 0.2px;
  text-shadow: 0 1px 3px rgba(0, 10, 20, 0.95), 0 0 6px rgba(0, 20, 35, 0.8);
  opacity: 0.92;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
}
/* 胶囊底衬：深色半透明 + 1px 细边 + 轻微外发光 */
.ws-bg-label--chip {
  padding: 3px 8px;
  border-radius: 10px;
  background: rgba(4, 22, 36, 0.62);
  border: 1px solid rgba(88, 160, 190, 0.28);
  backdrop-filter: blur(2px);
  box-shadow: 0 1px 6px rgba(0, 8, 16, 0.55);
}
/* 嵌套在立牌内的标签：坐标为立牌局部坐标，由立牌统一变换 */
.ws-bg-label--attached {
  transform: translate(-50%, -100%);
  z-index: 2;
}
.ws-bg-label.is-hidden { display: none; }

/* 底图容器：精确对齐 ECharts 绘图区 */
.ws-bg-holder {
  position: absolute;
  /* 与 .ws-chart 完全重合（均为 inset:0），确保贴图 SVG 与轨迹坐标系一致 */
  inset: 0;
}
/* 底图 SVG：与 .ws-chart 同坐标系。
   底图与坐标层现在共用 projectEnu 的「容器像素」空间，
   因此 SVG 覆盖整个容器，viewBox 与容器像素 1:1，
   底图内的厂区要素由 bgTransform 换算到与航迹完全相同的像素位置。 */
.ws-harbor-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0.82;
  pointer-events: none;
}
/* 港区底图贴图与建筑贴图层 */
.ws-harbor-vector,
.ws-npp-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}
.ws-harbor-vector { z-index: 1; }
.ws-npp-layer { z-index: 2; }
/* 建筑贴图素材为暗色调写实风，提亮并加轻微投影，使其在深色底图上可辨 */
.ws-npp-layer image {
  filter: brightness(1.45) saturate(1.15) contrast(1.05)
    drop-shadow(2px 3px 4px rgba(0, 0, 0, 0.55));
}
.ws-harbor-bg text { font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
/* ECharts 容器。
 *
 * 【为什么不能只写 position:absolute; inset:0】
 * ECharts 在 init() 时会往容器上写内联 `position: relative`，
 * 内联样式优先级高于类样式，于是 `inset: 0` 对一个 relative 元素失效 ——
 * 它会按内容高度撑开（这里是 0），导致：
 *   1. 图表画布高度为 0，什么都看不见；
 *   2. 拖拽 / 滚轮事件没有可命中的区域。
 *
 * 因此这里用 width/height 100% 明确撑满父容器，
 * 无论 ECharts 把 position 改成什么都能拿到正确尺寸。
 */
.ws-chart {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}
/* 背景船只层与船体贴图层：与 .ws-chart 同坐标系（均位于 .ws-scene-3d 内），保证与航迹对齐 */
.ws-bg-ship-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 5;
}
.ws-ship-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 6;
}
/* 船体贴图提亮，保证在深色底图与航迹上清晰可辨 */
.ws-ship-layer image,
.ws-bg-ship-layer image {
  filter: brightness(1.35) saturate(1.1) contrast(1.05)
    drop-shadow(1px 2px 3px rgba(0, 0, 0, 0.5));
}
.ws-chart-hud { position: absolute; top: 10px; left: 12px; right: 12px; display: flex; gap: 10px; align-items: center; pointer-events: none; }
.ws-chart-hud .ws-tag { margin-right: auto; }
.ws-chart-north { color: #9fd8e8; font-size: 14px; background: rgba(3,26,46,0.85); border: 1px solid #155171; border-radius: 5px; padding: 4px 10px; }
.ws-chart-clock { color: #8fb9cc; font-size: 14px; background: rgba(3,26,46,0.85); border: 1px solid #155171; border-radius: 5px; padding: 4px 10px; }
.ws-chart-legend { position: absolute; left: 12px; bottom: 34px; display: flex; gap: 14px; color: #b0d2e0; font-size: 13px; background: rgba(3,26,46,0.85); border: 1px solid #155171; border-radius: 5px; padding: 6px 10px; pointer-events: none; }
.sw { display: inline-block; width: 22px; height: 3px; vertical-align: middle; margin-right: 6px; border-radius: 2px; }
.sw.fused { background: #22e4ee; box-shadow: 0 0 6px #22e4ee; }
.sw.radar { background: repeating-linear-gradient(90deg, #4da6ff 0 5px, transparent 5px 9px); }
.sw.ais { background: repeating-linear-gradient(90deg, #3ef3a3 0 2px, transparent 2px 6px); }
.sw.ring { background: transparent; border: 1px dashed #155a7a; height: 10px; width: 10px; border-radius: 50%; }
.sw.time { background: #3d7ea6; height: 6px; width: 6px; border-radius: 50%; }
.ws-chart-scale { position: absolute; right: 16px; bottom: 34px; color: #b0d2e0; font-size: 12px; text-align: center; background: rgba(3,26,46,0.85); border: 1px solid #155171; border-radius: 5px; padding: 6px 12px; pointer-events: none; }
.ws-chart-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #8fb9cc; font-size: 16px; pointer-events: none; }
/* 角标说明：集中放在右下角，避免与 HUD 争位置 */
.ws-chart-note {
  position: absolute;
  right: 16px;
  bottom: 34px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  align-items: flex-end;
  color: #56788c;
  font-size: 11px;
  line-height: 1.5;
  background: rgba(3, 26, 46, 0.62);
  border: 1px solid rgba(21, 81, 113, 0.5);
  border-radius: 5px;
  padding: 5px 9px;
  pointer-events: none;
  max-width: 46%;
  text-align: right;
}
.scale-line { height: 8px; border-left: 1px solid #b3d8e6; border-right: 1px solid #b3d8e6; border-bottom: 2px solid #b3d8e6; margin: 0 auto 4px; }
/* 缩放控件 */
.ws-chart-zoom { position: absolute; right: 16px; top: 52px; display: flex; flex-direction: column; gap: 4px; }
.ws-chart-zoom button {
  width: 30px; height: 28px; line-height: 1; cursor: pointer;
  color: #bfe4f2; font-size: 15px; font-family: inherit;
  background: rgba(3,26,46,0.9); border: 1px solid #1d6a8e; border-radius: 4px;
  transition: background 0.15s, border-color 0.15s;
}
.ws-chart-zoom button:hover { background: rgba(12,58,88,0.95); border-color: #2fd6e8; color: #e6f9ff; }
.ws-chart-zoom button:active { background: rgba(20,88,124,1); }
.ws-chart-zoom .ws-zoom-reset { font-size: 12px; }
.ws-chart-zoom button:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.ws-chart-zoom .ws-zoom-reset.active {
  background: rgba(20, 92, 128, 0.95);
  border-color: #22e4ee;
  color: #eafcff;
}
.ws-chart-zoom .ws-zoom-25d { font-size: 11px; letter-spacing: 0.3px; }
.ws-chart-zoom .ws-zoom-25d.active {
  background: rgba(20, 92, 128, 0.95);
  border-color: #22e4ee;
  color: #eafcff;
  box-shadow: 0 0 8px rgba(34, 228, 238, 0.45);
}
/* 雷达站边缘方向提示：低调的小箭头 + 编号，不抢主体注意力 */
.ws-station-hint {
  position: absolute;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  pointer-events: none;
  z-index: 14;
  opacity: 0.75;
}
.ws-station-hint-arrow {
  display: block;
  font-size: 13px;
  line-height: 1;
  color: #ff8f8f;
  text-shadow: 0 0 6px rgba(0, 0, 0, 0.9);
}
.ws-station-hint-text {
  font-size: 10px;
  line-height: 1;
  color: #ffb3b3;
  background: rgba(3, 26, 46, 0.85);
  border: 1px solid rgba(255, 90, 90, 0.5);
  border-radius: 3px;
  padding: 1px 4px;
}
.ws-chart-hint {
  position: absolute; right: 56px; top: 56px; color: #5f8ba0; font-size: 11px;
  background: rgba(3,26,46,0.75); border-radius: 4px; padding: 3px 8px;
  pointer-events: none; white-space: nowrap;
}
</style>
