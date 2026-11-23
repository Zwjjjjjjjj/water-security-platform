<script setup lang="ts">
import { onMounted, onBeforeUnmount, shallowRef, watch, computed, ref, nextTick } from 'vue'
import { loadSceneAssetsV2, getSceneAssetsV2, type SceneAssetV2 } from './scene-assets.v2'
import SceneBackgroundV2 from './SceneBackgroundV2.vue'
import SceneObliqueV2 from './SceneObliqueV2.vue'
import SceneUnderwaterV2 from './SceneUnderwaterV2.vue'
import { OBLIQUE_SUPPRESS, OBLIQUE_SCENE_LABELS, enuToImage, imageToEnu } from './scenes.config.v2'
import { computeObliqueFit, obliqueCoverRange, obliqueFramedRange, obliqueImageToContainer, OBLIQUE_NAT_W, OBLIQUE_NAT_H, panelOcclusion } from './oblique-fit'
import { groundSpriteMatrix } from './scene-placement'
import { shipImageMatrixV2, svgMatrix, type ShipSpriteInfo } from './scene-placement.v2'
import { NPP_PLACEMENTS, HARBOR_VECTOR_PLACEMENTS, BACKGROUND_SHIPS } from './scenes.config.v2'
import { makeNavigable, blockedKind, isMoored, navigableDisplayPosition, makeNavigableOblique } from './navigation'
import { clampToWater, clampToOpenWater, clampToOpenWaterNearest, isNavigableEnu, clearanceCells } from './harbor-water'
import { buildWake, type WakeSegment, type WakeSample } from './water-effects'
import SceneWaterFxV2 from './SceneWaterFxV2.vue'
import SceneSensorsV2 from './SceneSensorsV2.vue'
import {
  STRUCTURES,
  CHANNEL_WALLS,
  PIERS,
  OFFSHORE_STRUCTURES,
  VERTICAL_PROPS,
  SUN_DIR,
  type StructureSegment,
  type VerticalProp,
} from './structures.v2'
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

/**
 * 视图范围（米制），由用户缩放/平移直接控制。
 *
 * 【为什么声明在这里，而不是靠近使用处】
 * 下方大量 computed（bgLabelPositions / nppSprites / shipSprites /
 * bgShipSprites / stationEdgeHint …）都会读取它。
 * 虽然 computed 是惰性求值、运行时不会触发 TDZ，
 * 但把响应式源集中声明在文件前部有两个实益：
 *   1. 依赖关系一目了然，不会漏掉某个 computed 的触发源；
 *   2. 避免「某个 computed 恰好没把 viewRange 读进依赖图」，
 *      导致视口变化后贴图不跟随的隐蔽问题。
 */
const viewRange = ref({ xMin: -2600, xMax: 1000, yMin: 3000, yMax: 5000 })
/** 用户是否已操作视图（缩放/平移），用于区分自动跟随与手动视图 */
const userViewAdjusted = ref(false)
/**
 * 坐标轴绘图区边距（像素）。
 *
 * 【斜视水面模式 → 归零】
 * 斜视背景要**铺满整个中央显示区**，而 ECharts 的 grid 必须与背景
 * 共用同一个原点，否则两者会错位。
 * 因此斜视**水面**模式下 grid 归零，背景与航迹都从容器 (0,0) 起算。
 *
 * 代价：坐标轴刻度标签会被面板边缘裁掉。
 * 这是可接受的 —— 斜视背景是一张**概念场景图**（未与实测 ENU 配准），
 * 素材包的目标布局里本来就没有坐标轴；
 * 读数由右侧「目标详情 / 量测信息」面板提供。
 * 用户若需要坐标轴，切到「俯视」模式即可。
 *
 * 【水下 / 正俯视模式保持原边距】
 * 正俯视底图（`SceneBackgroundV2`）按 GRID 边距排布，
 * 水下场景也沿用同一套留白；两者行为保持不变。
 */
const GRID_OBLIQUE = { left: 0, right: 0, top: 0, bottom: 0 }
/** 2.5D 模式下坐标层使用零边距，使底图与轨迹层完全重合（避免透视错位） */
const gridFor = () => {
  if (view25d.value) return { left: 8, right: 8, top: 8, bottom: 8 }
  if (isObliqueSurface.value) return GRID_OBLIQUE
  return GRID
}
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
/**
 * V2 场景标签（写实航拍风格）。
 *
 * 参考图的标注非常克制 —— 只有 2 个深色胶囊
 * （「核电厂区（示意）」「取水口」），
 * 其余都是低透明度的纯文字区域名。
 * 与 V1 相比少了大量建筑名标签，因为写实底图里建筑本身就是清晰的。
 */
const BG_LABELS: BgLabel[] = [
  // —— 厂区主标注（参考图左上角深色胶囊）——
  { dx: 372, dy: 56, text: '核电厂区（示意）', fill: '#eef6fa', size: 12, group: 'factory', chip: true },
  // —— 取排水口（参考图右侧深色胶囊）——
  { dx: 566, dy: 158, text: '取水口', fill: '#eef6fa', size: 12, group: 'intake', chip: true },
  { dx: 706, dy: 172, text: '排放口', fill: '#eef6fa', size: 12, group: 'intake', chip: true },
  // —— 区域名称（最弱层，低透明度纯文字）——
  { dx: 186, dy: 300, text: '西侧堤', fill: '#9fc4cf', size: 11, opacity: 0.55 },
  { dx: 96, dy: 396, text: '警戒浮标', fill: '#8fb4c2', size: 11, opacity: 0.45 },
  { dx: 452, dy: 470, text: '禁航区连线', fill: '#8fb4c2', size: 11, opacity: 0.45 },
  { dx: 760, dy: 250, text: '外海域', fill: '#7fa8b8', size: 11, opacity: 0.4 },
  { dx: 420, dy: 320, text: '核电厂厂前水域', fill: '#7fa8b8', size: 12, opacity: 0.42 },
  // —— 关键构筑物（写实底图里可见，仅作定位锚点）——
  { dx: 300, dy: 118, text: '核岛', fill: '#d8f4fc', size: 11, group: 'factory' },
  { dx: 660, dy: 110, text: '冷却塔', fill: '#c8e8f6', size: 11, group: 'factory' },
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
 * 固定斜视模式下的场景标签位置。
 *
 * 【为什么不能用 bgLabelPositions】
 * `bgLabelPositions` 走的是「设计坐标 → ENU 米制 → 容器像素」，
 * 而斜视背景是一张**已烘焙透视**的概念图，
 * 图中厂区/取水口/灯桩的实际像素位置与 ENU 平面没有对应关系。
 * 沿用 ENU 中转，文字必然与背景里的建筑错位。
 *
 * 【本函数的做法】
 * 直接用**图片原生像素坐标**（1536×1024）定位，
 * 再经 `oblique-fit.ts`（与背景图共用的同一份映射）换算到容器像素。
 * 于是：
 *   - 文字锚点 = 背景图里的对应物（像素级一致）
 *   - 缩放/平移时与背景同步变换
 *   - 字号保持屏幕像素，不随缩放变得过大/过小
 */
const obliqueLabelPositions = computed(() => {
  if (!isOblique.value) return []

  const g = gridFor()
  const { w: W, h: H } = plotSize()
  const v = viewRange.value

  /* 与 SceneObliqueV2 共用同一份映射参数
     （锚点同为容器原点 0,0，背景与文字才能严格对齐） */
  const fit = computeObliqueFit({
    xMin: v.xMin,
    xMax: v.xMax,
    yMin: v.yMin,
    yMax: v.yMax,
    gridLeft: g.left,
    gridTop: g.top,
    plotW: Math.max(50, W - g.left - g.right),
    plotH: Math.max(50, H - g.top - g.bottom),
    anchorX: 0,
    anchorY: 0,
  })

  const items = OBLIQUE_SCENE_LABELS.map((lb) => {
    const [cx, cy] = obliqueImageToContainer(fit, lb.px, lb.py)
    /* 文字相对锚点的偏移（图片像素 → 容器像素） */
    const x = cx + (lb.offsetX || 0) * fit.r
    const y = cy + (lb.offsetY || 0) * fit.r

    return {
      text: lb.text,
      x,
      y,
      /* 统一字段：与正俯视标签保持同构，便于模板共用 */
      fill: lb.style === 'plain' ? '#bfe6f2' : '#eef6fa',
      weight: 'normal' as const,
      size: lb.size ?? 12,
      opacity: lb.opacity ?? 0.9,
      chip: lb.style !== 'plain',
      priority: lb.priority ?? 3,
      visible: true,
      inPlot: x > -80 && x < W + 80 && y > -40 && y < H + 40,
    }
  })

  /* —— 避让（与正俯视标签同一套规则）——
     优先级高的先占位，低优先级的沿垂直方向上下微移；
     实在挪不开则隐藏，避免文字糊成一片。 */
  const padX = 10
  const padY = 6
  const boxes: { x1: number; x2: number; y1: number; y2: number }[] = []
  const hit = (x1: number, y1: number, x2: number, y2: number) =>
    boxes.some((b) => Math.min(x2, b.x2) - Math.max(x1, b.x1) > 1 && Math.min(y2, b.y2) - Math.max(y1, b.y1) > 1)

  items.sort((a, b) => a.priority - b.priority)
  for (const it of items) {
    if (!it.inPlot) {
      it.visible = false
      continue
    }
    const w = it.text.length * it.size + padX
    const h = it.size + padY
    let x1 = it.x - w / 2
    let x2 = it.x + w / 2
    let y1 = it.y - h / 2
    let y2 = it.y + h / 2
    if (hit(x1, y1, x2, y2)) {
      let placed = false
      for (const d of [1, -1, 2, -2, 3, -3]) {
        const ny1 = y1 + d * (h + 4)
        const ny2 = ny1 + h
        if (!hit(x1, ny1, x2, ny2)) {
          y1 = ny1
          y2 = ny2
          placed = true
          break
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
 * 当前应渲染的场景标签集合。
 *
 * 水面斜视 → 用图片像素锚定的 `obliqueLabelPositions`（与背景严格对应）
 * 水面正俯视 → 用 ENU 中转的 `bgLabelPositions`
 * 水下     → 不渲染任何场景标注（水下场景独立，不复用水面标签）
 */
const activeSceneLabels = computed(() => {
  if (isUnder.value) return []
  if (isOblique.value) return obliqueLabelPositions.value
  return bgLabelPositions.value
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

/**
 * 斜视场景的船体贴图放大系数（只作用于 `shipSprites` 这一层）。
 *
 * 用户要求「水上模式，按比例稍微增大目标船只」。
 *
 * 【与上面 `SHIP_DISPLAY_SCALE` 的区别】
 * 上面那个（1.45）作用于 **2.5D 立牌式船体**（正俯视 2.5D 模式）；
 * 这里这个只作用于**斜视模式的船体贴图**（`shipSprites`）。
 * 两者是不同渲染路径，系数各自独立，避免互相影响。
 *
 * 【为什么乘在 length/beam（米）上】
 * `shipImageMatrixV2()` 用「真实船长/船宽（米）」反推屏幕尺寸，
 * 这样缩放视图时船体比例天然正确。
 * 若改成直接放大矩阵，船与船之间的相对比例会失真
 * （160m 集装箱船与 24m 渔船会被拉成同一个量级）。
 *
 * 乘在米制尺寸上：160m → 217.5m、90m → 121.5m、24m → 32.4m，
 * 三者比例完全不变，只是整体放大一档。
 *
 * 【为什么不是 2 倍】
 * 底图是**概念场景图**，没有真实地理配准，
 * 船体只是「位置参照」。放太大就会与底图码头、岸线的尺度脱节，
 * 看起来像玩具船漂在真港里。
 *
 * 【当前取值 1.55】
 * 用户要求「水上模式中船只需要稍微放大一点」。
 * 1.35 → 1.55 是**再放大一档**（约 +15%）：
 * 160m → 248m、90m → 139.5m、24m → 37.2m，
 * 三者比例完全不变，只是整体再大一点，
 * 让船体在 1920 宽的大屏上更醒目（实测集装箱船长约 110px → 127px）。
 *
 * 仍远小于 2 倍 —— 再大就会与底图码头尺度脱节。
 */
const OBLIQUE_SHIP_SCALE = 1.55

/* ===== 场景素材（图层化接入） ===== */
/** 已加载的场景素材清单 */
const sceneAssets = shallowRef<SceneAssetV2[]>([])
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
 * 船体航向平滑。
 *
 * 【为什么要平滑 —— 实测数据的问题】
 * 瞬时 `course_deg` 在相邻样本间会剧烈跳变：
 * 实测 T002 的 24 个样本里有 **13 次**相邻航向跳变超过 45°
 * （例如 259.6° → 0°，这是跨 0°/360° 的正常回绕，但也存在真正的急转）。
 * 直接用它驱动船体贴图，船只就会「抽搐式」转向 —— 完全不像在航行。
 *
 * 【平滑方法】
 * 不用低通滤波（会把船头转向的延迟做得很难看），
 * 而是取**最近若干个采样点的位置**，用**首尾连线**求切线方向：
 *
 *   θ = atan2(Δe, Δn)   （0° = 正北，顺时针为正，与 course_deg 同约定）
 *
 * 这等价于「弦切法」—— 用一段弧的弦代替切线，
 * 天然把单点噪声抹平，且**没有延迟**（只用当前位置之前的点）。
 *
 * @param history 历史点（ENU）
 * @param fallback 无有效历史时回退的航向（度）
 * @param window 取最近多少个点求弦
 */
function smoothedCourseDeg(
  history: { east_m: number; north_m: number }[],
  fallback: number | null,
  window = 6,
): number | null {
  if (fallback == null || !Number.isFinite(fallback)) return null
  /* 取最近 window 个有效点 */
  const pts: [number, number][] = []
  for (let i = history.length - 1; i >= 0 && pts.length < window; i--) {
    const p = history[i]
    if (!p) continue
    if (!Number.isFinite(p.east_m) || !Number.isFinite(p.north_m)) continue
    pts.push([p.east_m, p.north_m])
  }
  if (pts.length < 2) return fallback
  /* pts 是逆序（最近的在前），取最老与最新 */
  const newest = pts[0]
  const oldest = pts[pts.length - 1]
  const de = newest[0] - oldest[0]
  const dn = newest[1] - oldest[1]
  /* 位移过小（几乎没动）→ 用回退值，避免除零/随机抖动 */
  if (Math.hypot(de, dn) < 0.5) return fallback
  /* 0° = 正北、顺时针为正 */
  const deg = (Math.atan2(de, dn) * 180) / Math.PI
  return (deg + 360) % 360
}

/**
 * 在两个航向之间做**最短弧插值**。
 *
 * 【为什么不能直接线性插值】
 * 359° → 1° 的线性插值会走 358°（几乎转了一整圈），
 * 而实际只转了 2°。必须把差值归一化到 (-180, 180]。
 */
function lerpCourseDeg(a: number, b: number, t: number): number {
  let d = ((b - a + 540) % 360) - 180
  return (a + d * t + 360) % 360
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

/**
 * 航迹折线的物理可行化入口（按当前底图模式自动选规则）。
 *
 * 【两种底图，两套水陆判据】
 * - **斜视水面模式**（`isOblique`）：底图是 `campus_oblique_v3.png`，
 *   岸线是斜线、厂区从左下延伸到右上。必须用底图自身的水域掩码，
 *   否则 T002 这类航迹会径直穿过厂房。
 * - **正俯视 / 水下模式**：沿用 2D 海图的岸线多边形与构筑物线段。
 *
 * 两种模式都只修正「两点之间的连线」，
 * 实测 ENU 坐标本身始终不变（散点、标签、距离仍用真实值）。
 */
function routeTrack(raw: [number, number][]): [number, number][] {
  if (raw.length < 2) return raw.slice()
  if (isOblique.value) return makeNavigableOblique(raw)
  return makeNavigable(raw)
}

/**
 * 目标在地图上的**显示位置**（与航迹折线用同一套水陆判据）。
 *
 * 落在陆地上的实测点，显示时移到最近的可航水域，
 * 避免出现「航迹点在厂房里、船在海上」的视觉分裂。
 *
 * 【为什么要推到「离岸有余量」的水域】
 * 仅保证「在水里」还不够 —— 实测点常落在堤坝旁一格宽的缝隙里，
 * 船就会「骑」在岸边。用户明确要求「在水域游动、不要贴着岸边」，
 * 因此这里用 `clampToOpenWater()`：要求离岸 ≥ 3 个掩码格（≈24 米）。
 */
function displayPosition(eastM: number, northM: number): { east: number; north: number; adjusted: boolean } {
  if (isOblique.value) {
    if (isNavigableEnu(eastM, northM) && clearanceCells(eastM, northM) >= 3) {
      return { east: eastM, north: northM, adjusted: false }
    }
    /**
     * 【用「最近达标格」而不是「余量最大格」】
     * `clampToOpenWater()` 挑的是搜索半径内离岸余量**最大**的格子，
     * 这个选择与点的位置无关 → 同一条航迹上相邻的点会被推到
     * 完全不同的水域，画面上就是「船在陆区附近来回瞬移」。
     *
     * 实测 T002 末段（整段在厂区陆域内）：
     *   (−725, 4667) → (−190.6, 4100.8)   ← 挑到了最靠海中央的格子
     *   (−574, 4768) → ( 109.4, 4339.2)
     *   (−875, 4543) → (−828.1, 4082.5)
     *
     * 改成「最近的达标格」后，落点离实测点尽可能近，
     * 相邻点的落点也就尽可能接近 —— 瞬移消失。
     */
    const [e, n] = clampToOpenWaterNearest([eastM, northM])
    return { east: e, north: n, adjusted: true }
  }
  return navigableDisplayPosition(eastM, northM)
}

/* ------------------------------------------------------------------ *
 * 沿航迹连续的显示位置（消除「穿模瞬移」）
 * ------------------------------------------------------------------ */

/**
 * 每个目标的「可航航线 + 时间→弧长映射」缓存。
 *
 * 【要解决的问题 —— 三个叠加的病根】
 *
 * 1. **单点 clamp 互相独立**
 *    `clampToOpenWater()` 对每个点独立求解，且在半径内挑「离岸余量最大」的格子，
 *    于是相邻采样点被推到**毫不相干**的水域：
 *      实测 T002 末段（整段落在厂区陆域内）：
 *        (−725, 4667) → (−190.6, 4100.8)
 *        (−574, 4768) → ( 109.4, 4339.2)
 *      两个落点相距数百米 —— 画面上就是「船在陆区附近来回瞬移」。
 *
 * 2. **航迹线随回放不断重算**
 *    航迹折线用的是 `history`（随时间增长），每新增一个采样点就要重跑一次 A*，
 *    路径一变，已经画在线上的船就跟着「跳」一下。
 *
 * 3. **实测末段落在陆域内**
 *    T002 末段 `navigable=false`，物理上根本不可能在航；
 *    A* 会绕行数百米，船若按实测点落位就会脱离航迹线。
 *
 * 【统一的解决思路】
 * 把「可航航线」作为唯一事实来源：
 *   · 航线 = A* 作用在**整条**实测轨迹上（`full_track`）→ 一次算好，全程稳定；
 *   · 航迹线画这条航线；
 *   · 船的位置 = 该航线上「当前回放时刻」对应的弧长位置。
 *
 * 于是天然满足：
 *   · 船永远骑在自己画出的那条线上 → 不会脱离轨迹；
 *   · 相邻时刻的弧长连续 → 不会瞬移；
 *   · 航线全程不变 → 不会因回放推进而重算跳动。
 */
const routeProgressCache = new Map<
  string,
  {
    key: string
    /** 航线（容器无关的 ENU 坐标） */
    pts: [number, number][]
    /** 每个原始采样点对应的航线弧长比例（0~1） */
    atSample: number[]
    /** 航线总弧长（米） */
    totalM: number
  }
>()

/** 历史点序列的指纹（点数 + 首尾坐标即可区分不同历史） */
function historyKey(hist: any[]): string {
  const n = hist.length
  if (!n) return '0'
  const a = hist[0]
  const b = hist[n - 1]
  return `${n}:${a?.east_m},${a?.north_m}:${b?.east_m},${b?.north_m}`
}

/**
 * 取得某目标的「可航航线」及其「采样点 → 弧长比例」映射。
 *
 * @param t 目标快照项
 */
function routeProgressFor(t: any): {
  pts: [number, number][]
  atSample: number[]
  totalM: number
} {
  const empty = { pts: [] as [number, number][], atSample: [] as number[], totalM: 0 }

  /**
   * 【必须用整条轨迹，而不是 `history`】
   * `history` 随回放推进而增长，用它算 A* 会导致航线反复重算、
   * 路径一变船就跟着跳。用 `full_track`（整条实测轨迹）算一次，全程稳定。
   */
  const src = (t.full_track?.length ? t.full_track : t.history || []) as any[]
  const pts = src
    .filter((p: any) => p?.east_m != null && p?.north_m != null)
    .map((p: any) => [p.east_m, p.north_m] as [number, number])
  if (pts.length < 2) return empty

  const key = historyKey(src)
  const cached = routeProgressCache.get(t.global_track_id)
  if (cached && cached.key === key) return cached

  /* A* 寻路：得到可航航线 */
  const route = routeTrack(pts)

  /* 航线累计弧长 */
  const cum: number[] = [0]
  for (let i = 1; i < route.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(route[i][0] - route[i - 1][0], route[i][1] - route[i - 1][1]))
  }
  const totalM = cum[cum.length - 1] || 0

  /**
   * 每个原始采样点对应的**航线弧长比例**。
   *
   * 【为什么不能直接用「最近的航线点」】
   * 实测 T002 的 A* 航线会绕行 7563m（实测航迹只有 2158m），
   * 因为航迹中段横穿厂区，A* 必须绕到南边去。
   * 于是「最近的航线点」在很多采样点上会落在同一个位置，
   * 映射退化（#6~#23 全映射到 idx=96/128），
   * 船就会在航线中途卡住不动，或突然跳一大段。
   *
   * 【正确的映射 —— 按「实测航迹的累计弧长比例」等分航线】
   *   航线比例 = 实测累计弧长 / 实测总弧长
   *
   * 理由：船的「航行进度」由实测航迹表达（这是真实意图），
   * 航线只负责「这段进度应该画在哪个可航位置」。
   * 这样：
   *   · 进度严格单调 → 船只会一直向前，不会卡住；
   *   · 相邻采样点的进度差 = 实测位移占比 → 连续，不会瞬移；
   *   · 船始终落在自己画出的航迹线上。
   */
  /**
   * 每个原始采样点对应的**航线弧长比例**。
   *
   * 【映射方式 —— 「最近的航线点」】
   * 实测 T002 的 A* 航线长 7563m，而实测航迹只有 2104m
   * （实测航迹横穿厂区，A* 必须绕行到南边去）。
   * 因此**不能**按「等分航线」映射 —— 那会让船远远落后：
   *   等分映射：平均偏离实测 1008m
   *   最近映射：平均偏离实测  408m
   * 而 T001 / T004 的航线与实测等长，最近映射的偏离是 **0m**。
   *
   * 「最近航线点」既保住了船与实测位置的接近，
   * 又保证船落在线上（不会脱离航迹）。
   *
   * 【退化问题与处理】
   * T002 中段多个采样点会映射到**同一个**航线点（因为航迹在陆里，
   * 那一带航线只有一条）。这会导致船在那一段停住不动。
   * 处理：把相同映射的采样点在航线那一小段上**均匀展开**，
   * 让船继续沿着线缓慢前进，而不是卡住。
   */
  const atSample: number[] = new Array(pts.length).fill(0)
  {
    /* 先求每个采样点的最近航线点下标 */
    const nearestIdx: number[] = new Array(pts.length).fill(0)
    for (let i = 0; i < pts.length; i++) {
      let bi = 0
      let bd = Infinity
      for (let j = 0; j < route.length; j++) {
        const d = (route[j][0] - pts[i][0]) ** 2 + (route[j][1] - pts[i][1]) ** 2
        if (d < bd) {
          bd = d
          bi = j
        }
      }
      nearestIdx[i] = bi
    }

    /* 分组：连续映射到同一航线点的采样点属于一组 */
    let groupStart = 0
    for (let i = 1; i <= pts.length; i++) {
      const sameGroup = i < pts.length && nearestIdx[i] === nearestIdx[groupStart]
      if (sameGroup) continue

      /* 处理 [groupStart, i) 这一组 */
      const gi = nearestIdx[groupStart]
      const gj = nearestIdx[i - 1]
      if (gi === gj) {
        /* 全组映射到同一个点 → 在该点附近按时间比例微展开，
         * 让船不会完全卡死（展开范围限制在前后一个航线点之间）。 */
        const lo = Math.max(0, gi - 1)
        const hi = Math.min(route.length - 1, gi + 1)
        const span = cum[hi] - cum[lo]
        const n = i - groupStart
        for (let k = groupStart; k < i; k++) {
          const f = n > 1 ? (k - groupStart) / (n - 1) : 0
          atSample[k] = totalM > 0 ? (cum[lo] + span * f) / totalM : 0
        }
      } else {
        /* 组内单调地从 gi 展开到 gj */
        const n = i - groupStart
        for (let k = groupStart; k < i; k++) {
          const f = n > 1 ? (k - groupStart) / (n - 1) : 0
          const arc = cum[gi] + (cum[gj] - cum[gi]) * f
          atSample[k] = totalM > 0 ? arc / totalM : 0
        }
      }
      groupStart = i
    }
  }

  const entry = { key, pts: route, atSample, totalM }
  routeProgressCache.set(t.global_track_id, entry)
  return entry
}

/**
 * 当前回放时刻在航线上的弧长比例。
 *
 * 【与船的位置必须用同一个算法】
 * 航迹线的末端与船的位置若各算各的，
 * 就会出现「线头在某处、船在另一处」的错位。
 * 因此两者都调用本函数，保证严丝合缝。
 */
function arcRatioAtTime(t: any, timeMs: number): number {
  const info = routeProgressFor(t)
  if (info.pts.length < 2 || !info.atSample.length) return 0

  const src = (t.full_track?.length ? t.full_track : t.history || []) as any[]
  const times = src.map((p: any) => p?.time_ms ?? 0)
  if (!times.length) return 0

  const t0 = times[0]
  const t1 = times[times.length - 1]
  const timeRatio = t1 > t0 ? Math.max(0, Math.min(1, (timeMs - t0) / (t1 - t0))) : 0

  /* 时间比例 → 采样点下标 → 该采样点的弧长比例 */
  const si = timeRatio * (times.length - 1)
  const i0 = Math.floor(si)
  const i1 = Math.min(times.length - 1, i0 + 1)
  const f = si - i0
  const r0 = info.atSample[Math.min(i0, info.atSample.length - 1)] ?? 0
  const r1 = info.atSample[Math.min(i1, info.atSample.length - 1)] ?? r0
  return r0 + (r1 - r0) * f
}

/**
 * 取航线上按弧长比例的位置。
 */
function pointOnRouteByRatio(
  route: [number, number][],
  ratio: number,
): [number, number] {
  if (!route.length) return [0, 0]
  if (route.length === 1) return route[0]

  const cum: number[] = [0]
  for (let i = 1; i < route.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(route[i][0] - route[i - 1][0], route[i][1] - route[i - 1][1]))
  }
  const total = cum[cum.length - 1] || 1
  const s = Math.max(0, Math.min(1, ratio)) * total

  let i = 0
  while (i < cum.length - 2 && cum[i + 1] < s) i++
  const segLen = cum[i + 1] - cum[i]
  const u = segLen > 0 ? (s - cum[i]) / segLen : 0
  const a = route[i]
  const b = route[i + 1]
  return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u]
}

/**
 * 目标在地图上的**显示位置** —— 落在自己那条可航航线上。
 *
 * 【做法】
 * 1. 若实测点本身就在可航水域且离岸有余量 → 直接用真实坐标（最常见的情况）；
 * 2. 否则取该点在**可航航线**上的对应位置（按回放时刻的弧长比例）。
 *
 * 【为什么这是「不瞬移」的关键】
 * 实测 T002 的末段（t=245000~302000）整段落在厂区陆域内，
 * 最近的合法水域在 300m 开外。此时任何「单点独立求解」的落位方式
 * （`clampToOpenWater()`）都会把相邻采样点推到不同的水域格：
 *
 *   (−725, 4667) → (−190.6, 4100.8)
 *   (−574, 4768) → ( 109.4, 4339.2)
 *   (−875, 4543) → (−828.1, 4082.5)
 *
 * 三个落点彼此相距数百米 —— 画面上就是「船在陆区附近来回瞬移 + 穿模」。
 *
 * 改用「航线弧长」定位后：
 *   · 船永远骑在自己画出的那条航迹线上，不会脱离轨迹；
 *   · 位置由「航线 + 时间」唯一决定，不存在独立求解 → 不会瞬移；
 *   · 航线一次算好（作用在整条实测轨迹上），全程不变 → 不会因回放推进而跳动。
 */
function displayPositionAlongTrack(
  t: any,
  eastM: number,
  northM: number,
): { east: number; north: number; adjusted: boolean } {
  /* 正俯视模式沿用 2D 岸线判据 */
  if (!isOblique.value) {
    return navigableDisplayPosition(eastM, northM)
  }

  /* 点本身就在可航水域且离岸有余量 → 直接用真实坐标 */
  if (isNavigableEnu(eastM, northM) && clearanceCells(eastM, northM) >= 3) {
    return { east: eastM, north: northM, adjusted: false }
  }

  /**
   * 【落在自己那条可航航线上】
   *
   * 实测末段（T002 的 t=245000~302000）整段落在厂区陆域内，
   * 最近的合法水域在 300m 开外 —— 无论怎么单点 clamp，
   * 相邻采样点都会落到不同的水域格，必然瞬移。
   *
   * 唯一自洽的做法：让船骑在自己画出的那条航迹线上。
   *   · 航迹线 = A* 作用在整条实测轨迹上得到的可航航线；
   *   · 船的位置 = 该航线上「按实测累计弧长比例」对应的位置。
   *
   * 这样：
   *   · 船与航迹线严丝合缝（不会脱离自己那条线）；
   *   · 相邻采样点的进度差 = 实测位移占比 → 连续，不瞬移；
   *   · 航线一次算好，全程不变 → 不会因回放推进而重算跳动。
   */
  const info = routeProgressFor(t)
  if (info.pts.length < 2 || !info.atSample.length) {
    const [e, n] = clampToOpenWaterNearest([eastM, northM])
    return { east: e, north: n, adjusted: true }
  }

  /**
   * 【用「当前回放时刻」在航线上的位置】
   * 而不是「离实测点最近的采样点」——
   * 前者与航迹线末端的算法完全一致，
   * 船就永远钉在航迹线的末端，不会超前也不会滞后。
   */
  const nowMs = (props.snapshot as any)?.time_ms ?? 0
  const ratio = arcRatioAtTime(t, nowMs)
  const [e, n] = pointOnRouteByRatio(info.pts, ratio)
  return { east: e, north: n, adjusted: true }
}

/**
 * 目标标签的摆放位置（避开左右侧栏遮挡）。
 *
 * 【问题】
 * 目标标签默认画在目标**正上方**。若目标本身就在右侧栏附近，
 * 标签会整块落在侧栏底下 —— 目标还在，但编号看不见，
 * 用户会以为「右侧目标丢了」。
 *
 * 【做法】
 * 先把目标的显示坐标换算成容器像素，判断它离哪一侧侧栏更近：
 *   · 靠右 → 标签改画到目标的**左边**（position: 'left'）
 *   · 靠左 → 标签改画到目标的**右边**（position: 'right'）
 *   · 居中 → 保持默认的 'top'
 *
 * 只在斜视模式生效（正俯视模式的绘图区有 GRID 边距，
 * 本来就不会被侧栏压住）。
 */
function labelPositionFor(eastM: number, northM: number): 'top' | 'left' | 'right' {
  if (!isOblique.value) return 'top'
  const W = el.value?.clientWidth || 1920
  const occ = panelOcclusion(W)
  const [px] = projectEnu(eastM, northM)
  /* 标签宽度约 130px，半宽 65px；留出余量避免贴边 */
  const margin = 90
  if (px > occ.right - margin) return 'left'
  if (px < occ.left + margin) return 'right'
  return 'top'
}

/**
 * 取素材的船体贴图信息（含轮廓范围与示意尺寸）
 *
 * 【轮廓回退】
 * manifest 里只有部分船型标注了 `opaque_bbox_px`（alpha 轮廓范围）。
 * 缺失时回退到整图尺寸 —— 代价是透明留白会让船看起来略小、略偏，
 * 但优于「整个目标不显示」。真实尺寸换算仍以 `length_m / beam_m` 为准。
 */
function shipSpriteOf(assetId: string): ShipSpriteInfo | null {
  const a = getSceneAssetsV2().find((x) => x.id === assetId)
  if (!a || !a.length_m || !a.beam_m) return null
  const bbox: [number, number, number, number] =
    a.opaque_bbox_px && a.opaque_bbox_px[2] > a.opaque_bbox_px[0] && a.opaque_bbox_px[3] > a.opaque_bbox_px[1]
      ? a.opaque_bbox_px
      : [0, 0, a.width_px, a.height_px]
  return {
    id: a.id,
    url: a.url,
    width_px: a.width_px,
    height_px: a.height_px,
    opaque_bbox_px: bbox,
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
    const a = getSceneAssetsV2().find((x) => x.id === pl.id)
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
      const m = shipImageMatrixV2(
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
      const m = shipImageMatrixV2(
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
      /* 立体船体按「沿航迹连续的位置」落位：
       * 实测坐标落在厂区陆域内时，投影到该目标自己的可航航线上，
       * 避免船体悬浮在厂房之间，**同时避免船在陆区附近瞬移**
       * （单点 clamp 会把相邻采样点推到不同水域，见 displayPositionAlongTrack）。 */
      const disp = displayPositionAlongTrack(t, t.east_m, t.north_m)
      const m = shipImageMatrixV2(
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
 * 水面目标的**屏幕锚点**（容器像素）—— 供传感器层画链路用。
 *
 * 【为什么必须从这一层导出，而不是让传感器层自己算】
 * 船体贴图的位置来自 `shipImageMatrixV2(asset, …, projectEnu)`：
 *   · 投影走 `projectEnu`（与 ECharts 坐标系严格一致）；
 *   · 落位走 `displayPositionAlongTrack()`（实测点若落在陆域，
 *     投影到该目标自己的可航航线上，保证位置连续、不瞬移）。
 *
 * 而 `SceneSensorsV2` 自带的 `project()` 走的是**另一条链路**：
 *   ENU → `enuToImage()` → `obliqueImageToContainer()`
 * 其中 `computeObliqueFit` 的缩放 `r = max(rW, rH)`（取两轴较大者，
 * 保证底图不被拉伸），与 `projectEnu` 的 `pxPerM = plotW/(xMax-xMin)`
 * **并不相等**。
 *
 * 结果就是：传感器层算出的目标点与船体贴图差出几十像素 ——
 * AIS 链路末端落在船旁边的空地上，而不是船上。
 *
 * 这里直接复用**同一条** `projectEnu` + `displayPosition` 链路，
 * 把最终屏幕坐标交给传感器层，从根上消除两套投影不一致的问题。
 *
 * 【返回结构】
 *   id / east / north —— 目标标识与 ENU 落位（供距离、方位角计算）
 *   x / y             —— 船体轮廓中心的容器像素（链路终点）
 */
const targetAnchors = computed(() => {
  const targets = (props.snapshot as any)?.targets || []
  const out: any[] = []
  for (const t of targets) {
    if ((t.domain || 'surface') === 'underwater') continue
    if (t.east_m == null || t.north_m == null) continue
    /* 与船体贴图完全一致的落位逻辑（沿航迹连续，杜绝瞬移） */
    const disp = displayPositionAlongTrack(t, t.east_m, t.north_m)
    const [x, y] = projectEnu(disp.east, disp.north)
    out.push({
      id: t.global_track_id,
      name: t.name,
      mmsi: t.mmsi,
      speed: t.speed_mps,
      sources: t.sources || [],
      east: disp.east,
      north: disp.north,
      x,
      y,
    })
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
    const a = getSceneAssetsV2().find((x) => x.id === assetId)
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
  const asset = getSceneAssetsV2().find((a) => a.id === 'steam_plume')
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
    const a = getSceneAssetsV2().find((x) => x.id === assetId)
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
  /* 【显式依赖视口】
   * 船体贴图的位置由 `projectEnu()` 依据 `viewRange` 换算，
   * 而 `projectEnu` 是普通函数 —— Vue 虽能追踪它内部读到的 ref，
   * 但这里显式读一次，可确保视口变化时本 computed 必定失效重算，
   * 不会出现「航迹动了、船贴图没动」的错位。 */
  void viewRange.value
  void chartSize.value
  const out: any[] = []
  const targets = (props.snapshot as any)?.targets || []
  for (const t of targets) {
    const assetId = TARGET_SHIP_ASSET[t.global_track_id]
    if (!assetId) continue
    const asset = shipSpriteOf(assetId)
    if (!asset) continue
    if (t.east_m == null || t.north_m == null || t.course_deg == null) continue
    try {
      /* 【与 ECharts 目标点共用同一份显示坐标】
       * ECharts 的目标散点用的是 `displayPosition()` 的结果：
       * 实测坐标若落在陆域/构筑物上，显示时移到最近的水侧泊位，
       * 避免「航迹点在厂房里、船在海上」这种视觉分裂。
       *
       * 船体贴图必须用**同一份**坐标，否则会出现
       * 「航迹点在 (1514,495)、船却画在 (1555,198)」的错位。
       * 实测坐标本身仍完整保留在数据面板与量测信息中。
       *
       * 【为什么要沿航迹连续】
       * 单点 clamp 会把相邻采样点推到不同水域，船就会在陆区附近
       * 出现「穿模瞬移」（实测 T002 末段显示位移一度达 272px，
       * 而实测位移只有 175m）。改用 `displayPositionAlongTrack()`
       * 把实测点投影到自己的可航航线上，位置连续且与轨迹线严丝合缝。 */
      const disp = displayPositionAlongTrack(t, t.east_m, t.north_m)
      /* 【船体显示放大系数】
       * 用户要求「按比例稍微增大目标船只」。
       *
       * 【为什么乘在 length/beam 上而不是直接缩放矩阵】
       * `shipImageMatrixV2()` 用「真实船长/船宽（米）」反推屏幕尺寸，
       * 这样缩放视图时船体比例天然正确。
       * 若改成直接放大矩阵，船与船之间的相对比例会失真
       * （160m 集装箱船与 24m 渔船会被拉成同一个量级）。
       *
       * 所以这里把长、宽**同时**乘同一个系数：
       *   · 船体之间的大小关系保持不变（仍是 160m : 90m : 24m）；
       *   · 只是整体比「纯真实尺寸」看起来大一档。
       *
       * 1.35 倍是视觉上的「稍微增大」——
       * 太大就会与底图的港区尺度脱节（船比码头还长）。
       */
      const lengthM = (t.length_m ?? SHIP_LEN_M[t.ship_type] ?? asset.length_m) * OBLIQUE_SHIP_SCALE
      const beamM = (t.beam_m ?? asset.beam_m) * OBLIQUE_SHIP_SCALE

      /**
       * 【航向平滑】
       * 瞬时 course_deg 在样本间会剧烈跳变（实测 T002 有 13 次 >45°），
       * 直接用会让船体贴图「抽搐式」转向。
       * 这里用最近 6 个点的**首尾连线**求切线方向（弦切法），
       * 既抹平噪声又没有滤波延迟。
       */
      const rawCourse = t.course_deg
      const hist = (t.history || []) as any[]
      const smoothCourse = smoothedCourseDeg(hist, rawCourse, 6)
      /* 平滑值与瞬时值差异过大时（>25°）优先信任平滑值 */
      const course = smoothCourse ?? rawCourse

      const m = shipImageMatrixV2(
        asset,
        { east_m: disp.east, north_m: disp.north, course_deg: course },
        projectEnu,
        { lengthM, beamM },
      )
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

/**
 * 船舶尾迹（按速度关联）。
 *
 * 【规则 —— 用户明确要求】
 *   · 快速运动目标 → 明显的渐细尾流；
 *   · 约 0.4 米/秒的目标 → 只保留很弱的扰动；
 *   · 静止目标 → 完全没有尾迹。
 *
 * 判据在 `buildWake()` 里（速度门槛 0.8 m/s），
 * 这里只负责把每个目标的历史点与当前速度喂进去。
 *
 * 尾迹画在**图片像素坐标系**（与背景对齐），
 * 因此这里用 `enuToImage()` 换算，而不是 `projectEnu()`。
 */
const shipWakes = computed(() => {
  void assetsReady.value
  const out: WakeSegment[] = []
  const targets = (props.snapshot as any)?.targets || []
  const nowMs = (props.snapshot as any)?.time_ms ?? 0
  for (const t of targets) {
    /* 水下目标不画水面尾迹 */
    if ((t.domain || 'surface') !== 'surface') continue
    const speed = t.speed_mps ?? 0
    const hist: WakeSample[] = (t.history || [])
      .filter((p: any) => p.east_m != null && p.north_m != null && p.time_ms != null)
      .map((p: any) => ({ e: p.east_m, n: p.north_m, t: p.time_ms }))
    if (hist.length < 2) continue
    const asset = shipSpriteOf(TARGET_SHIP_ASSET[t.global_track_id])
    const lengthM = t.length_m ?? SHIP_LEN_M[t.ship_type] ?? asset?.length_m ?? 60
    const beamM = t.beam_m ?? asset?.beam_m ?? 12
    out.push(
      ...buildWake(hist, enuToImage, {
        speedMps: speed,
        lengthM,
        beamM,
        nowMs,
      }),
    )
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
    /* 【水下目标不参与取景】
     * 水下目标走的是声纳量测的 view_u/view_v 定位体系，
     * 与水面实测 ENU 混算会让水面视野被拉偏。
     * 水下模式有自己的取景逻辑（底图铺满 + 声纳覆盖区）。 */
    if ((t.domain || 'surface') === 'underwater') continue
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

/**
 * 水下场景取景 —— 按「水下实际可见内容」框定视口。
 *
 * 【内容是什么】
 * 水下画面上真正能看见的只有三样东西：
 *   · 声纳站（底图归一化坐标 view_u / view_v）
 *   · 声纳波（以声纳站为圆心）
 *   · 水下目标及其航迹
 * 它们在底图上只占中间一小块，
 * 所以视口必须**只框这一块**，而不是整张底图。
 *
 * 【怎么换算】
 * 水下坐标是**归一化**（0~1）→ 底图原生像素 → ENU 米制，
 * 与底图用的是同一套仿射（`imageToEnu`），因此换算后与底图严格对齐。
 *
 * 【为什么效果是「和水下模块一样的比例」】
 * 取景用的填充率、可见区宽度、边距留白，
 * 与水面 `fitPointsRange` 完全一致 ——
 * 同样的内容用同样的规则装进同样的画布，
 * 两边复位后的**视觉比例**就一致了。
 */
function fitUnderwaterRange(): {
  xMin: number
  xMax: number
  yMin: number
  yMax: number
} | null {
  const W = el.value?.clientWidth || 960
  const H = el.value?.clientHeight || 500
  const g = gridFor()
  const plotW = Math.max(50, W - g.left - g.right)
  const plotH = Math.max(50, H - g.top - g.bottom)

  /* 收集水下内容点（归一化坐标） */
  const normPts: [number, number][] = []

  /* 声纳站（含演示用的多个显示节点） */
  const devices = (props.snapshot as any)?.devices || []
  for (const d of devices) {
    if (d.kind !== 'sonar') continue
    const nodes = (d.display_nodes || []).length ? d.display_nodes : [d]
    for (const n of nodes) {
      const u = n.view_u ?? d.view_u
      const v = n.view_v ?? d.view_v
      if (u == null || v == null) continue
      normPts.push([u, v])
    }
  }

  /* 水下目标（含已走过的航迹） */
  for (const t of (props.snapshot as any)?.targets || []) {
    if ((t.domain || 'surface') !== 'underwater') continue
    const src: any[] = t.full_track?.length ? t.full_track : t.history || []
    for (const p of src) {
      if (p.view_u == null || p.view_v == null) continue
      normPts.push([p.view_u, p.view_v])
    }
    if (t.view_u != null && t.view_v != null) normPts.push([t.view_u, t.view_v])
  }

  if (!normPts.length) return null

  /* 归一化 → 底图像素 → ENU 米制 */
  const enuPts: [number, number][] = normPts.map(([u, v]) =>
    imageToEnu(u * OBLIQUE_NAT_W, v * OBLIQUE_NAT_H),
  )

  /* 内容包围盒 */
  let eMin = Infinity, eMax = -Infinity, nMin = Infinity, nMax = -Infinity
  for (const [e, n] of enuPts) {
    eMin = Math.min(eMin, e); eMax = Math.max(eMax, e)
    nMin = Math.min(nMin, n); nMax = Math.max(nMax, n)
  }

  /**
   * 填充率与水面一致。
   *
   * 声纳波是画面里最醒目的元素（以站为心的椭圆），
   * 它的半径不计入包围盒，所以要给内容留出余量，
   * 否则复位后波纹会被切在画面外。
   */
  const FILL = 0.62
  const LABEL_PAD_PX = 70
  const occ = panelOcclusion(W)
  const visibleW = Math.max(200, occ.right - occ.left)
  const visibleCenter = (occ.left + occ.right) / 2

  const availW = Math.max(80, visibleW * FILL - LABEL_PAD_PX * 2)
  const availH = Math.max(80, plotH * FILL - LABEL_PAD_PX * 2)

  const cE = (eMin + eMax) / 2
  const cN = (nMin + nMax) / 2

  /* 与水面同样的换算：内容铺满可见区 → 反推视口跨度 */
  const pxPerM = Math.max(
    Math.max(1e-6, (eMax - eMin) / availW),
    Math.max(1e-6, (nMax - nMin) / availH),
  )
  const spanX = pxPerM * plotW
  const spanY = pxPerM * plotH

  /* 内容中心对齐到可见区中心（与水面同规则） */
  const shiftE = (plotW / 2 - visibleCenter) / pxPerM

  return {
    xMin: cE - spanX / 2 + shiftE, xMax: cE + spanX / 2 + shiftE,
    yMin: cN - spanY / 2, yMax: cN + spanY / 2,
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
  const mode: ViewMode = effectiveViewMode.value

  /* ================================================================
   * 【水下模式 · 独立取景】
   *
   * 为什么不能沿用水面的取景：
   *   水面取景的对象是「厂区建筑 + 背景船只 + 岸线 + 水面目标」，
   *   它们几乎铺满整张底图 —— 于是视口跨度很大、底图被压得很小。
   *   而水下场景里这些东西**一个都不显示**：
   *   底图上真正可见的只有「声纳站 + 声纳波 + 水下目标」，
   *   它们只占底图中间很小一块（归一化 u≈0.40~0.68、v≈0.52~0.81）。
   *
   * 后果就是「复位后还是太大」：
   *   视口按整张底图取景 → 声纳波与水下目标被缩成几个小点，
   *   画面里大片是空的底图。
   *
   * 正确做法：按**水下实际内容**取景 ——
   *   把声纳站与水下目标的位置（归一化坐标）换算成 ENU 米制，
   *   再用与水面相同的「铺满可见区」逻辑取景。
   *   这样水面/水下复位后的**视觉比例一致**。
   * ================================================================ */
  if (isUnder.value && mode !== 'follow') {
    const r = fitUnderwaterRange()
    if (r) {
      viewRange.value = r
      const s = Math.max((r.xMax - r.xMin) / plotW, (r.yMax - r.yMin) / plotH)
      scalePx.value = Math.round(pickScale(s) / s)
      scaleLabel.value = scaleText(pickScale(s))
      recordAutoFitSpan(r)
      return r
    }
  }

  /* 取景模式决定视口：
   *   subject  —— 主体适配（默认）：厂区 + 港池 + 目标铺满视窗
   *   follow   —— 目标跟随：以选中目标为中心
   *   overview —— 全域总览：完整展示 R01 与全部目标的相对位置
   *
   * props.follow 是外部「目标跟随」开关，它等价于 follow 模式；
   * 若两者冲突，以用户显式选择的取景模式为准。 */
  /* 【斜视模式 · 独立取景】
   *
   * 斜视背景是一张**固定的概念场景图**，厂区（画面主体）位于图的上部。
   * 若沿用正俯视的「按实测目标取景」，
   * 目标都在海里 → 视口只框住海面 → 厂区被裁到画面外。
   *
   * 所以斜视模式改为：让**图片的有效内容区**恰好铺满绘图区
   * （厂区在上、海面在下，与素材包 preview 的目标布局一致）。
   *
   * 该视口仍由 ENU 表达，与 `enuToImage` 严格双向绑定，
   * 因此缩放/平移后背景与航迹依然同步。
   *
   * 「目标跟随」模式下仍以选中目标为中心 —— 用户明确要求跟随时，
   * 应当优先满足跟随意图。 */
  if (isOblique.value && mode !== 'follow') {
    /* 斜视模式的取景目标有两个，且互相冲突：
     *   1. 背景要**铺满整个显示区**（视觉要求，不能改）；
     *   2. 目标要落在**两侧栏之间的可见区**（可用性要求）。
     *
     * 单纯把视口居中到可见区是没用的 ——
     * 可见区中心（960px）恰好就是整个显示区中心（1920/2），
     * 位移量为 0，目标照样躲在侧栏底下。
     *
     * 正确做法：**按目标取景，但把取景框限制在可见区宽度内**。
     *   · 目标集合（实测 ENU 点）→ 算出米制包围盒；
     *   · 可见区宽度只有 972px，是全屏的 50.6%；
     *     所以为了容下这些目标，视野必须比「铺满」时更小（放大）；
     *   · 放大后背景仍然盖满全屏（背景是无限可拉伸的贴图），
     *     不会出现「图外空白」；
     *   · 若目标全部落在可见区内，就按可见区居中。
     *
     * 这样既满足「目标看得见」，又满足「背景铺满」。
     */
    const occ = panelOcclusion(W)
    const visibleW = Math.max(200, occ.right - occ.left)
    const visibleCenter = (occ.left + occ.right) / 2

    /* 目标集合：只取**当前位置 + 最近一段历史**。
     *
     * 【为什么不取全部历史】
     * T002 的完整历史横跨 E -2289..637（近 3000 米），
     * 若按全部历史取景，视野会被拉得极大，
     * 结果是「船小得看不见 + 目标仍然分散在两侧」。
     *
     * 用户关心的是**当前这一刻**船在哪、在往哪走。
     * 因此只用当前位置与最近 40 秒（≈8 个测点）的历史，
     * 既能表达运动趋势，又不会把视野撑爆。 */
    const tgtPts: [number, number][] = []
    for (const t of targets) {
      /* 【水下目标不参与水面取景】
       * 水下目标的定位体系是声纳量测的 view_u/view_v，
       * 与水面实测 ENU 不是同一套配准，直接混在一起取景
       * 会把水面视野拉偏（水下目标在总览模式里是可见的）。
       * 水下模式有自己的取景逻辑（底图铺满 + 声纳覆盖区）。 */
      if ((t.domain || 'surface') === 'underwater') continue
      if (t.east_m != null && t.north_m != null) tgtPts.push([t.east_m, t.north_m])
      const hist: any[] = t.history || []
      for (const p of hist.slice(-8)) {
        if (p.east_m != null && p.north_m != null) tgtPts.push([p.east_m, p.north_m])
      }
    }

    let r: { xMin: number; xMax: number; yMin: number; yMax: number }
    if (tgtPts.length >= 2) {
      /* 目标包围盒 */
      let eMin = Infinity, eMax = -Infinity, nMin = Infinity, nMax = -Infinity
      for (const [e, n] of tgtPts) {
        if (e < eMin) eMin = e
        if (e > eMax) eMax = e
        if (n < nMin) nMin = n
        if (n > nMax) nMax = n
      }
      /* 视野跨度：由背景「铺满」决定 */
      const cover = obliqueCoverRange({ panelW: W, panelH: H })
      const coverSpanX = cover.xMax - cover.xMin
      const coverSpanY = cover.yMax - cover.yMin

      /* 【关键：跨度由「目标 + 可见区」决定，位置由「目标」决定】
       *
       * 背景要铺满整个显示区（1920px），但**只有两侧栏之间的 972px 是可见的**。
       * 若按「铺满」的跨度（3312m）取景，实测目标横跨 2650m，
       * 换算成像素是 1536px —— 必然有一部分躲在侧栏底下。
       *
       * 正确的做法：**让目标群刚好铺满可见区**。
       *   · 目标横跨 (eMax - eMin) 米；
       *   · 要让它落在 visibleW 像素内 → 每米像素数
       *       pxPerM = (eMax - eMin) / visibleW；
       *   · 视野跨度 = pxPerM × plotW（整个显示区对应的米数）。
       *
       * 背景是**贴图**，放大后只是像素变粗，不会出现「图外空白」
       *（背景锚在容器原点，任何缩放下都盖满全屏）。
       * 代价是画面更聚焦于海面、厂区被推到边缘 ——
       * 这正是「目标必须看得见」优先于「厂区完整」的取舍。
       *
       * 下限保护：跨度不小于「铺满」跨度的 55%，
       * 避免目标很集中时把画面拉到失真的放大倍率。
       */
      const LABEL_PAD_PX = 70
      const usableW = Math.max(120, visibleW - LABEL_PAD_PX * 2)
      const usableH = Math.max(120, plotH - LABEL_PAD_PX * 2)
      const pxPerMTarget = Math.max(
        (eMax - eMin) / usableW,
        (nMax - nMin) / usableH,
      )
      const spanX = Math.max(pxPerMTarget * plotW, coverSpanX * 0.55)
      const spanY = Math.max(pxPerMTarget * plotH, coverSpanY * 0.55)
      const pxPerM = plotW / spanX

      /* 目标群中心 → 可见区中心 */
      const cE = (eMin + eMax) / 2
      const cN = (nMin + nMax) / 2
      const shiftE = (plotW / 2 - visibleCenter) / pxPerM

      r = {
        xMin: cE - spanX / 2 + shiftE, xMax: cE + spanX / 2 + shiftE,
        yMin: cN - spanY / 2, yMax: cN + spanY / 2,
      }
    } else {
      r = obliqueCoverRange({ panelW: W, panelH: H })
    }

    /* 把视野夹回合理范围（允许比背景图更宽 —— 两侧露出的是平铺海面） */
    const clamped = clampAutoFrameRange(r)

    const s = Math.max((clamped.xMax - clamped.xMin) / plotW, (clamped.yMax - clamped.yMin) / plotH)
    scalePx.value = Math.round(pickScale(s) / s)
    scaleLabel.value = scaleText(pickScale(s))
    viewRange.value = clamped
    recordAutoFitSpan(clamped)
    return clamped
  }

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

/**
 * 历史尾迹分段淡入:越接近当前越亮,共 6 段。
 *
 * 【航迹减细 —— 只突出选中目标】
 * 选中目标：2.4px + 辉光，是画面的主角；
 * 未选中：1.1px、无辉光、透明度整体压低。
 * 之前所有尾迹都是 2~3px，多条船的航迹叠在一起，
 * 整个海面被粗线糊住，船体贴图与厂区细节反而看不清。
 */
function trailSeries(name: string, color: string, pts: number[][], isSel: boolean, z: number) {
  if (pts.length < 2) return []
  const K = 6
  const out: any[] = []
  const n = pts.length
  for (let i = 0; i < K; i++) {
    const a = Math.floor((i * (n - 1)) / K)
    const b = Math.floor(((i + 1) * (n - 1)) / K)
    if (b <= a) continue
    /* 淡入曲线：选中目标保持完整的 0.18→1.0；
     * 未选中目标压到 0.10→0.45，避免抢戏。 */
    const base = isSel ? 0.18 : 0.1
    const span = isSel ? 0.82 : 0.35
    out.push({
      name: `${name}-seg${i}`,
      type: 'line',
      data: pts.slice(a, b + 1),
      showSymbol: false,
      silent: true,
      lineStyle: {
        color,
        width: isSel ? 2.4 : 1.1,
        opacity: base + (span * (i + 1)) / K,
        shadowColor: color,
        /* 辉光只给选中目标 */
        shadowBlur: isSel && i === K - 1 ? 9 : 0,
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
  /* 【水下目标不走 ECharts】
   * 水下航迹由 `SceneUnderwaterV2` 独立渲染，原因有三：
   *   1. 水下目标带**深度**，需要「深度 → 屏幕纵向偏移」的第三维表达，
   *      ECharts 的二维散点/折线做不到；
   *   2. 水下目标的位置来自声纳量测的 `view_u/view_v`（底图配准坐标），
   *      与水面目标的 ENU 实测坐标不是同一套定位体系；
   *   3. 水下目标需要**沿航迹连续行进**的动画 + 声纳探测联动，
   *      这是 SVG 层特有的表现。
   *
   * 若这里也画一遍，就会出现「ECharts 的实线航迹」压在
   * 「SVG 的深度分层航迹」之上，两套坐标语义互相打架。
   */
  const surfaceTargets = targets.filter((t: any) => (t.domain || 'surface') !== 'underwater')
  for (const t of surfaceTargets) {
    const c = targetColor(t.global_track_id)
    const isSel = t.global_track_id === props.selectedTargetId
    const isMooredTarget = mooredIds.has(t.global_track_id)
    if (props.layers.source_tracks && !isMooredTarget) {
      for (const s of t.source_histories || []) {
        const raw = (s.history || []).map((p: any) => [p.east_m, p.north_m] as [number, number])
        if (raw.length < 2) continue
        /* 物理可行化：源点迹是实测真值，坐标不能改，
         * 但显示用的折线要绕开陆域与构筑物，否则会「穿模」。
         * 斜视模式按底图水域掩码绕行，正俯视按岸线多边形绕行。 */
        const pts = routeTrack(raw)
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
            /* 【航迹减细，只突出选中目标】
             * 未选中的源点迹压到 0.8px 并降低透明度；
             * 选中目标才给到 1.4px。
             * 之前所有航迹都是 1.1~1.8px，多条叠加后
             * 画面被线糊住，船只与厂区反而看不清。 */
            width: isSel ? 1.4 : 0.8,
            opacity: isSel ? 0.85 : 0.5,
            type: s.source_id === 'R01' ? 'dashed' : 'dotted',
          },
          z: 4,
        })
      }
    }
    if (props.layers.fused_tracks && !isMooredTarget) {
      const hist = t.history || []
      /**
       * 【航迹线用「整条轨迹」算出的稳定航线】
       *
       * 旧实现用 `history`（随回放增长）算 A*，
       * 每新增一个采样点就重算一次航线 —— 路径一变，
       * 已经画在线上的船就跟着跳一下（T002 末段实测跳了 114px）。
       *
       * 现在航线作用在 `full_track`（整条实测轨迹）上，
       * 一次算好、全程不变，船与航迹线也就不会再分家。
       *
       * 画面上只画「当前时刻之前已经走过」的那一段航线，
       * 呈现「航迹随回放生长」的效果。
       */
      const routeInfo = routeProgressFor(t)
      const allPts = routeInfo.pts.length >= 2 ? routeInfo.pts : routeTrack(hist.map((p: any) => [p.east_m, p.north_m] as [number, number]))

      /* 当前时刻在航线上的弧长比例（截断到「已走过」的部分） */
      let drawn = allPts
      if (routeInfo.pts.length >= 2 && hist.length) {
        const nowMs = (props.snapshot as any)?.time_ms ?? 0
        /* 与船的位置用完全相同的算法 */
        const arcRatio = arcRatioAtTime(t, nowMs)
        /* 按弧长比例裁剪航线 */
        drawn = []
        const cum: number[] = [0]
        for (let i = 1; i < allPts.length; i++) {
          cum.push(cum[i - 1] + Math.hypot(allPts[i][0] - allPts[i - 1][0], allPts[i][1] - allPts[i - 1][1]))
        }
        const total = cum[cum.length - 1] || 1
        const limit = arcRatio * total
        for (let i = 0; i < allPts.length; i++) {
          if (cum[i] > limit) break
          drawn.push(allPts[i])
        }
        /* 末端补上当前时刻的精确位置，保证线头与船重合 */
        if (drawn.length >= 2) {
          const [ex, ey] = pointOnRouteByRatio(allPts, arcRatio)
          drawn.push([ex, ey])
        }
      }

      const pts = drawn.length >= 2 ? drawn : allPts
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
     * 符号改画在该目标自己的可航航线上 —— 船不会浮在厂房之间，
     * 也不会在陆区附近瞬移（相邻采样点必须连续）。
     * 实测坐标本身不改，仍用于量测信息与数据面板。 */
    const disp = displayPositionAlongTrack(t, t.east_m, t.north_m)
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
      /* 【符号形态：斜视模式交给船体贴图】
       *
       * 用户要求「用统一视角的船型替换三角符号」。
       * 斜视水面模式下，船体贴图层（`.ws-ship-layer`）已经画了
       * 与背景视角一致的 2.5D 船体（8 种船型，俯视统一视角，航向随数据旋转）。
       *
       * 若这里再画一个三角船符，就会出现「贴图上叠一个三角形」——
       * 正是要避免的「三角符号 + 船体」双份船形。
       *
       * 所以斜视模式把 ECharts 符号降级为**不可见的交互锚点**：
       *   · 不画形状（opacity 0），但保留 hover / 点击 / tooltip 能力；
       *   · 标签仍画在这里（标签要在最上层，且要能避让侧栏）。
       *
       * 正俯视 / 水下模式没有贴图层，保留原来的船符（带驾驶室分层）。
       */
      symbol: isObliqueSurface.value ? 'circle' : shipPath,
      symbolRotate: rot,
      // 尺寸按真实船长换算（米→像素），保证缩放时船体比例正确
      symbolSize: shipPixelSize(t, isSel),
      itemStyle: {
        color: c,
        /* 斜视模式：符号完全透明，只作为交互锚点 */
        opacity: isObliqueSurface.value ? 0 : 1,
        borderColor: isMooredTarget ? '#f0c98a' : '#02182a',
        borderWidth: isMooredTarget ? 1.8 : 1.2,
        shadowColor: c,
        shadowBlur: isSel ? 12 : 5,
        // 2.5D 下的落影：向右下偏移，形成悬浮高度感
        shadowOffsetX: view25d.value ? (isSel ? 3 : 2) : 0,
        shadowOffsetY: view25d.value ? (isSel ? 4 : 3) : 0,
      },
      /* 目标标签层级：编号最突出（16px 粗体 + 目标色），
       * 次要信息（速度/航向）退到 11px 浅灰蓝，避免与设施文字抢注意力。
       *
       * 【MMSI 不再画在地图上】
       * MMSI 是 9 位数字，画在图上既占地方又干扰阅读，
       * 需要查证时到右侧「目标详情」面板看即可。
       * 地图只保留「编号 + 速度/航向」这类现场感信息。
       *
       * 【标签避让】
       * 目标靠近侧栏时，标签翻转到另一侧，避免被面板盖住。 */
      label: {
        show: true,
        formatter: isMooredTarget
          ? `{b|${t.global_track_id}}\n{S|锚泊待命}`
          : `{b|${t.global_track_id}}\n{S|${t.speed_mps?.toFixed?.(1) ?? '-'} 米/秒${t.course_deg != null ? '  ' + t.course_deg.toFixed(0) + '°' : ''}}`,
        color: '#e6fbff',
        fontSize: 12,
        position: labelPositionFor(disp.east, disp.north),
        distance: 18,
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
      /* 艏向箭头跟随目标符号的显示位置（陆域内则投影到航迹上） */
      const arrowDisp = displayPositionAlongTrack(t, last.east_m, last.north_m)
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
    /* 【时间标签收敛为少量关键节点】
     * 之前每 30s 一个点、全部常驻显示，
     * 一条 5 分钟的航迹就有 10 个标签，多目标叠加后画面全是小字。
     *
     * 现在改为：
     *   · 只给**选中目标**的航迹打常驻标签；
     *   · 未选中目标只保留测点小圆点（不显示文字）；
     *   · 任何测点悬停都能在 tooltip 里看到完整时刻。
     *
     * 选中目标的标签再按 60s 一档取点，保证「一眼能读出时间轴」，
     * 但不至于密密麻麻。 */
    const marked = timeMarks.slice(0, 120)
    /* 选中目标的时间标签（常驻） */
    const selMarks = marked.filter((d: any) => d.tid === props.selectedTargetId)
    /* 其他目标只留标记点 */
    const otherMarks = marked.filter((d: any) => d.tid !== props.selectedTargetId)

    /* 选中目标的标签按 60s 分档取点 */
    const shownSel = selMarks.filter((d: any, i: number) => {
      if (i === 0) return true
      const prev = selMarks[i - 1]
      return Math.floor(d.t / 60000) !== Math.floor(prev.t / 60000)
    })

    const mk = (d: any, showLabel: boolean) => ({
      value: d.value,
      t: d.t,
      tid: d.tid,
      label: {
        show: showLabel,
        formatter: `${String(Math.floor(d.t / 60000)).padStart(2, '0')}:${String(Math.floor((d.t % 60000) / 1000)).padStart(2, '0')}`,
        color: showLabel ? '#7ba3b8' : '#4d6d80',
        fontSize: 10,
        position: 'right',
        distance: 6,
      },
    })

    series.push({
      name: 'time-marks',
      type: 'scatter',
      data: [...shownSel.map((d: any) => mk(d, true)), ...otherMarks.map((d: any) => mk(d, false))],
      symbolSize: 4,
      /* 不再 silent —— 悬停要能读出完整时刻 */
      silent: false,
      itemStyle: { color: '#3d7ea6', opacity: 0.75 },
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
        /* 【时间标记优先于目标】
         * 时间标记现在也带 `tid`（未选中目标的时间点只显示圆点、不显示文字，
         * 悬停时读出完整时刻）。
         * 所以必须**先**判 `d.t`，否则会误显示成船舶详情。 */
        if (d.t != null) {
          const ts = `${String(Math.floor(d.t / 60000)).padStart(2, '0')}:${String(Math.floor((d.t % 60000) / 1000)).padStart(2, '0')}`
          return d.tid ? `${d.tid} · 历史时刻 ${ts}` : `历史时刻 ${ts}`
        }
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
      /* 斜视水面模式下 grid 归零，轴名与刻度会被面板边缘裁掉，直接隐藏 */
      name: isObliqueSurface.value ? '' : '东向（米）',
      min: xMin, max: xMax,
      nameTextStyle: { color: '#8fb9cc', fontSize: 13 },
      axisLine: { show: !isObliqueSurface.value, lineStyle: { color: '#1875a1' } },
      axisLabel: { show: !isObliqueSurface.value, color: '#8fb9cc', fontSize: 12 },
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
      name: isObliqueSurface.value ? '' : '北向（米）',
      min: yMin, max: yMax,
      nameTextStyle: { color: '#8fb9cc' },
      axisLine: { show: !isObliqueSurface.value, lineStyle: { color: '#1875a1' } },
      axisLabel: { show: !isObliqueSurface.value, color: '#8fb9cc', fontSize: 12 },
      splitLine: { show: false },
    },
    series,
  }
}


/** 是否启用 2.5D 轴测视图 */
/**
 * V2 默认使用**纯 2D 正俯视**。
 *
 * 参考图是标准的航拍俯视图，没有透视倾斜；
 * V2 的目标就是复刻这种「真实航拍底图 + 正俯视实体」的效果，
 * 所以默认关闭 2.5D 倾斜。
 * 用户仍可通过右下角的「2.5D」按钮临时切换查看立体效果。
 */
const view25d = ref(false)

/**
 * V2 背景模式。
 *
 * - `top`     —— 正俯视（`campus_aerial.png`），ENU 平面逻辑，航迹可地理配准
 * - `oblique` —— 固定斜俯视（`campus_oblique.png`），贴近最终参考图
 *
 * 【为什么斜视模式要单独一套坐标】
 * 斜视背景**已经烘焙了投影**，不能再套 2.5D 倾斜变换，
 * 也不能用 ENU 平面直接对齐（没有核验过的控制点）。
 * 因此斜视模式启用「示意局部坐标系」：
 *   - 背景：图片原生像素
 *   - ENU 点位：经 `enuToImage()` 映射到该坐标系
 * 界面上标注「场景示意」，不宣称已地理配准。
 */
const sceneMode = ref<'top' | 'oblique'>('oblique')

/** 当前是否处于固定斜俯视模式 */
const isOblique = computed(() => sceneMode.value === 'oblique')

/**
 * 是否处于「斜俯视 + 水面」模式。
 *
 * 这是**铺满全屏**的那一套渲染路径：
 *   · 背景 = `campus_oblique.png`（斜视概念图）
 *   · 背景锚在容器原点，ECharts grid 归零
 *   · 坐标轴刻度隐藏（概念图没有地理配准，刻度无意义）
 *   · 平移受 `clampObliqueRange()` 约束，不会拖出画外
 *
 * 水下模式与正俯视模式不走这套逻辑，保持原有留白与坐标轴。
 */
const isObliqueSurface = computed(() => isOblique.value && !isUnder.value)

/** 斜视模式下应关闭的旧内容（背景已含山体/厂区/岸线/防波堤） */
const suppressOld = computed(() => (isOblique.value ? OBLIQUE_SUPPRESS : null))

/**
 * 切换背景模式（斜视 ⇄ 正俯视）。
 *
 * 切到斜视时：
 *   - 强制关闭 2.5D（斜视背景已烘焙投影，再倾斜会二次压缩）；
 *   - 清除用户手动调整标记，让新的取景逻辑重新计算视野。
 */
function toggleSceneMode() {
  sceneMode.value = isOblique.value ? 'top' : 'oblique'
  if (isOblique.value) {
    view25d.value = false
    userViewAdjusted.value = false
  }
  nextTick(() => {
    render()
  })
}
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
  /* 【基准必须是 .ws-chart 的左上角，不是 .ws-chart-wrap 的】
   *
   * 视口的米制范围（viewRange）是相对 **绘图区** 定义的，
   * 而 `plotW = chart.clientWidth - grid.left - grid.right`
   * 也基于 chart 的尺寸。
   *
   * 若用 host（.ws-chart-wrap）的左上角做基准，
   * 在「host 与 chart 不重合」时（内边距、居中偏移、尺寸差异）
   * 指针位置会整体偏移固定量 ——
   * 表现为「滚轮缩放时画面往一边平移」。
   *
   * 所以这里反解出的是**相对 chart 左上角**的像素，
   * 与 plotW/plotH 的基准严格一致。
   */
  return {
    px: (e.clientX - cr.left) / (scaleX || 1),
    py: (e.clientY - cr.top) / (scaleY || 1),
  }
}

let dragging = false
let dragStartX = 0
let dragStartY = 0
let dragOrigin = { xMin: 0, xMax: 0, yMin: 0, yMax: 0 }

/**
 * 【铺满全屏】平移边界约束。
 *
 * 斜视背景是一张**有限**的概念图（1536×1024）。
 * 若允许视口无限平移，背景会被拖出画面，
 * 于是画面里出现「图外」的空白 —— 正是用户反馈的「拖动会出现画面以外」。
 *
 * 这里把视口限制在**背景图能覆盖的范围**内：
 *   视口宽度 ≤ 图片宽度对应的 ENU 跨度
 *   视口高度 ≤ 图片高度对应的 ENU 跨度
 * 并且视口必须完整落在图片的 ENU 范围内。
 *
 * 超出部分无法平移到的地方由**底层海面平铺**兜底，
 * 因此即便在边界处，也只会看到海面，不会看到刺眼的空白底色。
 *
 * 【为什么只在斜视模式生效】
 * 正俯视模式的底图是按 ENU 无限铺开的设计坐标底图，
 * 不存在「图外」的问题，保持原有行为不变。
 */
function clampObliqueRange(range: { xMin: number; xMax: number; yMin: number; yMax: number }) {
  if (!isObliqueSurface.value) return range

  /* 图片（整幅，不只是内容区）的 ENU 覆盖范围 */
  const imgE = imageToEnu(0, 0)
  const imgRight = imageToEnu(OBLIQUE_NAT_W, 0)
  const imgTop = imageToEnu(0, 0)
  const imgBottom = imageToEnu(0, OBLIQUE_NAT_H)
  const eMin = Math.min(imgE[0], imgRight[0])
  const eMax = Math.max(imgE[0], imgRight[0])
  const nMin = Math.min(imgTop[1], imgBottom[1])
  const nMax = Math.max(imgTop[1], imgBottom[1])

  const xSpan = range.xMax - range.xMin
  const ySpan = range.yMax - range.yMin

  /* 视口跨度不得超过图片跨度（否则图片必然盖不满） */
  const maxXSpan = eMax - eMin
  const maxYSpan = nMax - nMin
  const nxSpan = Math.min(xSpan, maxXSpan)
  const nySpan = Math.min(ySpan, maxYSpan)

  /* 视口中心夹在图片范围内 */
  const cx = (range.xMin + range.xMax) / 2
  const cy = (range.yMin + range.yMax) / 2
  const halfX = nxSpan / 2
  const halfY = nySpan / 2
  const clampedCx = Math.min(Math.max(cx, eMin + halfX), eMax - halfX)
  const clampedCy = Math.min(Math.max(cy, nMin + halfY), nMax - halfY)

  return {
    xMin: clampedCx - halfX,
    xMax: clampedCx + halfX,
    yMin: clampedCy - halfY,
    yMax: clampedCy + halfY,
  }
}

/**
 * 「按目标取景」的视口（不受图片跨度限制）。
 *
 * 【与 `clampObliqueRange()` 的区别】
 * 后者服务于**用户手动缩放/平移**：把视口限制在背景图范围内，
 * 保证图不会被拖出画面。
 *
 * 但「自动取景」的目标是让实测目标落在可见区，
 * 这可能需要一个**比背景图更宽**的视野
 * （背景 3600m 的跨度若要装下横跨 2650m 的目标并留出边距，
 *  需要的视野约 6100m —— 此时背景只覆盖屏幕中间 1130px，
 *  两侧露出的是**平铺的海面纹理**，而不是刺眼的空白）。
 *
 * 这正是 `SceneObliqueV2` 三层结构（海面底纹平铺 + 斜视概念图）
 * 设计的用意：海面层永远铺满整个容器。
 *
 * 所以自动取景**不经过** `clampObliqueRange()`，
 * 只保证视口中心不跑得太偏。
 */
function clampAutoFrameRange(range: { xMin: number; xMax: number; yMin: number; yMax: number }) {
  if (!isObliqueSurface.value) return range

  const imgE = imageToEnu(0, 0)
  const imgRight = imageToEnu(OBLIQUE_NAT_W, 0)
  const imgTop = imageToEnu(0, 0)
  const imgBottom = imageToEnu(0, OBLIQUE_NAT_H)
  const eMin = Math.min(imgE[0], imgRight[0])
  const eMax = Math.max(imgE[0], imgRight[0])
  const nMin = Math.min(imgTop[1], imgBottom[1])
  const nMax = Math.max(imgTop[1], imgBottom[1])

  /* 允许的额外余量：背景图外再多露出 2.5 倍图宽的海面。
   * 用户要能自由拖拽查看 —— 限制太紧会让人觉得「拖到边界就卡住了」。 */
  const marginX = (eMax - eMin) * 2.5
  const marginY = (nMax - nMin) * 2.5

  const cx = (range.xMin + range.xMax) / 2
  const cy = (range.yMin + range.yMax) / 2
  const halfX = (range.xMax - range.xMin) / 2
  const halfY = (range.yMax - range.yMin) / 2
  const clampedCx = Math.min(Math.max(cx, eMin - marginX + halfX), eMax + marginX - halfX)
  const clampedCy = Math.min(Math.max(cy, nMin - marginY + halfY), nMax + marginY - halfY)

  return {
    xMin: clampedCx - halfX,
    xMax: clampedCx + halfX,
    yMin: clampedCy - halfY,
    yMax: clampedCy + halfY,
  }
}

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
  /* 斜视模式用 clampAutoFrameRange（允许视野超出背景图范围），
   * 若用 clampObliqueRange，拖拽会被强行拉回背景图内 —— 表现为「拖不动」。 */
  viewRange.value = isObliqueSurface.value
    ? clampAutoFrameRange({
        xMin: v.xMin - dx, xMax: v.xMax - dx,
        yMin: v.yMin + dy, yMax: v.yMax + dy,
      })
    : {
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
  viewRange.value = isObliqueSurface.value
    ? clampAutoFrameRange({
        xMin: v.xMin - dx, xMax: v.xMax - dx,
        yMin: v.yMin + dy, yMax: v.yMax + dy,
      })
    : {
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
    /* 【重要】这里**不能**使用 `replaceMerge: ['series', ...]`。
     *
     * `replaceMerge` 的语义是「用本次传入的同名组件**替换**已有组件」。
     * 本次只传了 xAxis / yAxis / axisPointer，**没有传 series**，
     * 于是 ECharts 会把已有的全部 series 删除 —— 表现为
     * 「一缩放，航迹、雷达覆盖、目标散点全部消失，只剩 SVG 船和标签」。
     *
     * 正确做法：坐标轴用 replaceMerge 替换（保证 min/max 与主题一致，
     * 不残留旧轴样式），series 保持不动 —— 它由 `render()` 负责整体重建。
     */
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
      { replaceMerge: ['xAxis', 'yAxis'] } as any,
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
 *   - 上限 = 自动取景跨度的 1/6：
 *     最多放大 6 倍，用于看清单个目标或设施的细节。
 */
const ZOOM_IN_MAX_FACTOR = 6

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
 * 【必须保持两个不变量，否则用户会看到「滚轮在平移画面」】
 *
 * 1. **等比例不变量**
 *    视口的「米/像素」比例在 E 方向和 N 方向必须相同
 *    （`s = max(xSpan/plotW, ySpan/plotH)`，且 xSpan/plotW === ySpan/plotH）。
 *    若对 `halfW`/`halfH` 分别乘 factor，就会破坏这个不变量；
 *    随后的 `clampZoomRange()` 会按 `sClamped * plotW/plotH` 重新推导跨度，
 *    于是跨度被改写、中心被重算 —— 结果就是画面在缩小的同时**整体漂移**。
 *
 * 2. **锚点不变量**
 *    指针指向的那个点，在缩放前后必须是**同一个米制坐标**。
 *    做法：先按等比例求出新跨度，再反解出「保持锚点不动」的新中心。
 *
 * 正确顺序：
 *   a. 用当前比例 s 把指针像素换算成米制锚点（anchE/anchN）；
 *   b. 新比例 s' = s * factor；
 *   c. 新跨度 = s' * plotW / plotH（保持等比例）；
 *   d. 由「锚点像素位置固定 + 锚点米制坐标固定」反解出新的范围边界；
 *   e. 这样锚点严格不动，且 E/N 比例一致。
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
  /* 最小可视跨度，防止过度放大 */
  const minSpan = MIN_SPAN
  const spanForS = Math.min(sNew * plotW, sNew * plotH)
  if (spanForS < minSpan) {
    sNew = minSpan / Math.min(plotW, plotH)
  }

  const xSpan = sNew * plotW
  const ySpan = sNew * plotH

  /* ---- 3) 新范围：无锚点时保持原中心；有锚点时让锚点严格不动 ---- */
  let xMin: number
  let yMax: number
  if (anchorE != null && anchorN != null) {
    /* 【关键：锚点给的是「范围边界」，不是「中心」】
     *
     * 设锚点的屏幕像素为 (px, py)（相对绘图区左上角，缩放前后不变），
     * 锚点的米制坐标为 (anchorE, anchorN)（缩放前后不变）。
     *
     * 不变量（E 方向）：
     *   anchorE = xMin + px * s
     * 缩放后 s 变成 sNew，同一个像素位置、同一个米制锚点：
     *   anchorE = xMin' + px * sNew
     * 解出：
     *   xMin' = anchorE - px * sNew
     *
     * 同理（N 方向，y 向下增长）：
     *   anchorN = yMax - py * s
     *   yMax'  = anchorN + py * sNew
     *
     * 【注意】
     * xMin'/yMax' 是**范围的边界**，不能直接当中心用。
     * 早先的写法 `cx = anchorE - px * sNew` 把它当成了中心，
     * 于是范围整体又偏移了半个跨度 —— 表现为「滚轮缩放时画面平移」。
     */
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
  // 允许区间：base（不缩小） ~ base / 6（最多放大 6 倍）
  const sMax = base / ZOOM_IN_MAX_FACTOR
  const sMin = base
  let sClamped = s
  if (s > sMin) sClamped = sMin
  else if (s < sMax) sClamped = sMax
  if (sClamped === s) return

  /* 保持中心不变地缩放。
   *
   * 【关键：跨度必须按等比例重建】
   * 不能只改 xSpan 或只改 ySpan，也不能让 xSpan/plotW ≠ ySpan/plotH ——
   * 那会破坏「1m(E) = 1m(N)」的不变量，
   * 导致后续投影、量测、航迹绘制全部错位。
   *
   * 这里统一用 `sClamped * plotW` 与 `sClamped * plotH` 重建跨度，
   * 两者共享同一个 sClamped，等比例天然成立。
   */
  const cx = (v.xMin + v.xMax) / 2
  const cy = (v.yMin + v.yMax) / 2
  const xSpan = sClamped * plotW
  const ySpan = sClamped * plotH
  /* 斜视模式不再用 clampObliqueRange 限制跨度 ——
   * 那会把视野强行钉在背景图范围内，导致「缩放没反应 / 只能缩到图大小」。
   * 这里只保留中心夹取，跨度由上面的 sClamped 决定。 */
  viewRange.value = clampAutoFrameRange({
    xMin: cx - xSpan / 2, xMax: cx + xSpan / 2,
    yMin: cy - ySpan / 2, yMax: cy + ySpan / 2,
  })
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
 * 用户按「复位比例」的意图是「我刚才放大看细节，现在看不清了，
 * 想回到正常比例」，而不是「把镜头拉到全景去」，
 * 所以这里保留当前 viewMode，只清掉 userViewAdjusted 标记。
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

/** 对外暴露给父组件的方法（resetScale 供「复位比例」按钮调用） */
defineExpose({ resetScale, setViewMode })

/** 同步图表容器尺寸（供 SVG viewBox 使用）。
 * 取 .ws-chart 的 clientWidth/Height，与底图 holder 的 CSS 尺寸一致，
 * 保证 SVG viewBox 与 CSS 尺寸严格相等，避免 preserveAspectRatio 拉伸带来的偏移。 */
/**
 * 同步图表像素尺寸。
 *
 * 【为什么要看 wrapper 而不是 .ws-chart】
 * .ws-chart 的高度依赖父链（.ws-ground → .ws-scene-3d），
 * 而这两者在 2D 模式下是 static 定位 —— 若 CSS 尚未应用，
 * .ws-chart 的 clientHeight 会是 0，导致 chartSize 永远停在 960×500。
 *
 * .ws-chart-wrap 有确定高度（由 #world 的固定画布尺寸决定），
 * 因此用它作为尺寸来源最可靠；
 * 拿不到时再退回 .ws-chart。
 */
function syncChartSize() {
  /* 尺寸来源优先级：.ws-chart > .ws-chart-wrap
   *
   * .ws-chart 与 ECharts 画布同尺寸，是 SVG 覆盖层（船/标签）坐标的基准；
   * wrapper 是容器，尺寸相同但可能因 CSS 作用域问题滞后。
   * 两个都取，取第一个非零的宽高。 */
  const c = el.value
  const host = gestureHost || c?.closest('.ws-chart-wrap')
  let w = c?.clientWidth || 0
  let h = c?.clientHeight || 0
  if (!w || !h) {
    w = host?.clientWidth || 0
    h = host?.clientHeight || 0
  }
  if (w && h && (w !== chartSize.value.w || h !== chartSize.value.h)) {
    chartSize.value = { w, h }
    return true
  }
  return false
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
  loadSceneAssetsV2().then((list) => {
    sceneAssets.value = list as any
    assetsReady.value = true
    render()
  })
  chart = echarts.init(el.value!)
  ;(window as any).__chart = chart
  /* 尺寸同步：挂载后 CSS 可能尚未生效（此时 clientHeight 为 0），
   * 因此在 nextTick 与两帧 rAF 后各同步一次，
   * 确保 chartSize 与 .ws-chart 的真实尺寸一致 ——
   * 否则 SVG 覆盖层与 ECharts 画布会按不同尺寸布局，贴图与航迹错位。 */
  const syncNow = () => {
    if (syncChartSize()) {
      chart?.resize()
      render()
    }
  }
  nextTick(() => {
    syncNow()
    requestAnimationFrame(() => {
      syncNow()
      requestAnimationFrame(syncNow)
    })
  })
  render()
  chart.on('click', (p: any) => {
    const tid = p.data?.tid || (typeof p.seriesName === 'string' ? p.seriesName.split('-')[0] : null)
    if (tid && /^T\d/.test(tid)) emit('select-target', tid)
  })
  /* 先绑定手势（它会把 gestureHost 指向 .ws-chart-wrap），
   * 下面的 ResizeObserver 才能观察到正确的宿主元素。 */
  bindViewportGestures()
  ro = new ResizeObserver(() => {
    syncChartSize()
    rerender()
  })
  /* 【关键】观察 .ws-chart-wrap 而不是 .ws-chart。
   *
   * .ws-chart 自身在 CSS 里是 `position: relative; width/height: 100%`，
   * 但它的**父链**（.ws-ground → .ws-scene-3d）在 2D 模式下是 static 定位，
   * 一旦 .ws-chart 的高度塌成 0，ResizeObserver 就再也不会触发
   * （尺寸没变化，没有通知），chartSize 便永远停在初始的 960×500。
   *
   * 后果是：航迹层按 960×500 布局，而 SVG 船层/标签层按 1920×870 布局，
   * 两者错位 —— 船贴图和航迹点对不上。
   *
   * 改为观察有确定高度的 .ws-chart-wrap，尺寸变化一定能被捕获。 */
  const resizeTarget = gestureHost || el.value
  if (resizeTarget) ro.observe(resizeTarget)
  window.addEventListener('resize', rerender)
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
      v-if="!isUnder && !view25d && !suppressOld?.nppBuildings && nppSprites.length"
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
    <!--
      【水面 / 水下 场景背景分离】
      水面与水下是两套**互斥**的场景背景，不能同时出现在同一层：

        · 水面（!isUnder）：斜俯视背景 或 正俯视背景，二选一
        · 水下（isUnder）  ：独立的水下监视区示意背景

      【此前的缺陷】
      水下示意背景写的是 `<svg v-else>`，而 `v-else` 绑定的是**上一个兄弟节点**
      `SceneBackgroundV2`。斜视模式下 `SceneBackgroundV2` 为 false，
      于是水下示意背景被一并渲染出来 ——
      表现为「水面和水下合到了同一个图层」，两层背景叠在一起。

      现在改为各自独立的 `v-if`，彻底分开。
    -->
    <!-- 水面场景 · 固定斜俯视背景（贴近最终参考图） -->
    <SceneObliqueV2
      v-if="!isUnder && isOblique"
      class="ws-v2-scene ws-v2-oblique"
      :assets-ready="assetsReady"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
    />
    <!--
      海面动效与水岸细节层（仅斜视水面模式）。

      【为什么单独一层】
      泡沫 / 波纹 / 航标 / 尾迹都必须与底图**像素级对齐**，
      它们画的是「底图上的东西」而不是「ENU 平面上的东西」。
      本层复用与 `SceneObliqueV2` 完全相同的映射参数（computeObliqueFit），
      因此缩放/平移时与背景严格同步。

      【顺序】
      底图（SceneObliqueV2） → 本层（泡沫/波纹/航标/尾迹） → 航迹与船（ECharts + SVG）
    -->
    <SceneWaterFxV2
      v-if="!isUnder && isOblique"
      class="ws-v2-scene ws-v2-waterfx"
      :assets-ready="assetsReady"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
      :wakes="shipWakes"
    />
    <!--
      传感器层（雷达 / AIS / 光电）。

      【位置说明】
      本组件挂载在 **`.ws-ground` 内的船体贴图层之后**
      （见下方「传感器层」注释），因此渲染在船与航迹**之上**。
    -->
    <!-- 水面场景 · 正俯视背景 -->
    <SceneBackgroundV2
      v-if="!isUnder && !isOblique"
      class="ws-v2-scene"
      :assets-ready="assetsReady"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
    />
    <!--
      水下场景 · 实景底图 + 声纳覆盖动画 + 水下目标。

      【与水面场景的关系】
      水下不画「正俯视 ENU 底图」，而是复用**斜俯视实景底图**：
      效果图里的水下场景就是一张航拍实景港湾图 ——
      岸线、厂区、防波堤围出的水域就是声纳作业区。
      底图与水面模式共用 `SceneObliqueV2`，因此水下也有「铺满全屏」的效果。

      【图层顺序】
        SceneObliqueV2（底图）  →  SceneUnderwaterV2（声纳波 + 目标）  →  .ws-chart（ECharts）
      声纳波在底图之上、ECharts 之下，
      这样 ECharts 的航迹/坐标仍能盖在声纳波上，不会被遮挡。
    -->
    <SceneObliqueV2
      v-if="isUnder"
      class="ws-v2-scene ws-v2-oblique"
      :assets-ready="assetsReady"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
    />
    <SceneUnderwaterV2
      v-if="isUnder"
      class="ws-under-layer"
      :snapshot="snapshot"
      :selected-target-id="selectedTargetId"
      :layers="layers"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
      :paused="paused"
      :time-ms="(snapshot as any)?.time_ms ?? 0"
    />
    </div>
    <div ref="el" class="ws-chart"></div>

    <!-- 背景船只图层：真实船型贴图，与实测目标船体同一坐标系 -->
    <svg
      v-if="!isUnder && !view25d && !suppressOld?.backgroundShips && backgroundShipSprites.length"
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

    <!--
      传感器层（雷达 / AIS / 光电）。

      【为什么挂在船体贴图层「之后」】
      新增的 AIS 报文链路与光电跟踪框都是**指向目标**的：
        · AIS 链路从岸台连到船上的报文框 —— 终点就在船身上；
        · 光电跟踪框要**套在船身上** —— 不盖住船就等于没画。

      若把传感器层放在船层之前，这两处都会被船体贴图遮掉：
      观众只看到一根从船底下钻出来的绿线，和一个完全看不见的跟踪框。

      所以传感器层必须排在船体贴图之后。
      代价是雷达扫描扇面会盖住航迹 ——
      但扇面填充只有 3~7% 透明度（senRadarScan 渐变），
      盖上去只是极淡的一层青色，不构成遮挡。

      【坐标系不变】
      传感器层用的仍是与底图、航迹、船完全相同的 ENU 投影链路，
      挂到 `.ws-ground` 内后随 2.5D 场景一同倾斜，位置依然严格对齐。
    -->
    <SceneSensorsV2
      v-if="!isUnder && isOblique"
      class="ws-v2-scene ws-v2-sensors"
      :assets-ready="assetsReady"
      :chart-w="chartSize.w"
      :chart-h="chartSize.h"
      :x-min="viewRange.xMin"
      :x-max="viewRange.xMax"
      :y-min="viewRange.yMin"
      :y-max="viewRange.yMax"
      :grid-left="gridFor().left"
      :grid-right="gridFor().right"
      :grid-top="gridFor().top"
      :grid-bottom="gridFor().bottom"
      :time-ms="(snapshot as any)?.time_ms ?? 0"
      :paused="paused"
      :targets="targetAnchors"
      :selected-target-id="selectedTargetId"
    />

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
        v-for="(lb, i) in activeSceneLabels"
        :key="i"
        class="ws-bg-label ws-scene-upright-label"
        :style="{
          left: lb.x + 'px',
          top: lb.y + 'px',
          color: lb.fill ?? '#eef6fa',
          fontSize: lb.size + 'px',
          fontWeight: lb.weight || 'normal',
          opacity: lb.opacity ?? 0.9,
        }"
        :class="{ 'is-hidden': !lb.visible, 'ws-bg-label--chip': lb.chip !== false }"
      >{{ lb.text }}</span>
    </div>
    </div>

    <div class="ws-chart-hud">
      <span class="ws-tag">{{
        isUnder
          ? '水下监视区示意 · 未标定'
          : isOblique
            ? '实测航迹回放 · 场景示意 · 未配准'
            : '实测航迹回放 · 核电厂区背景示意'
      }}</span>
      <span class="ws-chart-north">↑ 真北方向</span>
      <span class="ws-chart-clock">{{ clockNote }}</span>
    </div>
    <div class="ws-chart-legend">
      <span><i class="sw fused"></i>统一航迹</span>
      <span><i class="sw radar"></i>雷达源点迹</span>
      <span><i class="sw ais"></i>AIS源点迹</span>
      <span v-if="!isUnder"><i class="sw ring"></i>距离环（1/2/3公里）</span>
      <span v-else><i class="sw time"></i>时间标记 30秒</span>
      <!--
        传感器图例（仅斜视水面模式）。

        地图上的三类传感器用**不同主色 + 不同动效**区分：
          雷达 = 橙红，旋转扫描扇面
          AIS  = 绿色，接收脉冲圈 + 到目标的流动报文链路
          光电 = 黄色，视场锥 + 跟踪框（跟随目标）
        图例把「颜色 → 传感器」的对应关系写清楚，
        否则观众看到画面上三处不同颜色的动效却不知道是什么。
      -->
      <template v-if="!isUnder && isOblique">
        <span><i class="sw sen-radar"></i>雷达扫描</span>
        <span><i class="sw sen-ais"></i>AIS接收</span>
        <span><i class="sw sen-ais-link"></i>AIS报文链路</span>
        <span><i class="sw sen-eo"></i>光电视场</span>
        <span><i class="sw sen-eo-lock"></i>光电跟踪</span>
      </template>
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
      <!--
        背景模式切换。
        斜视（固定斜俯视）贴近最终参考图，含山体/建筑/防波堤立体细节；
        正俯视为 ENU 平面逻辑，航迹可地理配准。
      -->
      <button
        type="button"
        :title="isOblique ? '当前：固定斜俯视（场景示意）· 点击切换为正俯视' : '当前：正俯视（ENU 平面）· 点击切换为固定斜俯视'"
        class="ws-zoom-25d"
        :class="{ active: isOblique }"
        @click="toggleSceneMode"
      >{{ isOblique ? '斜视' : '俯视' }}</button>
      <button
        type="button"
        title="切换 2.5D / 2D 视图"
        class="ws-zoom-25d"
        :class="{ active: view25d }"
        :disabled="isOblique"
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
      <span v-if="isOblique">固定斜俯视场景 · 概念背景未与实测 ENU 配准 · 不代表真实岸线与水深</span>
      <span v-else>底图为核电厂区示意框架，不代表真实岸线与水深 · 设施位置仅作示意</span>
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
.ws-ground .ws-ship-layer,
.ws-ground .ws-v2-scene,
.ws-ground .ws-oblique-bg-svg {
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
/* 水下场景背景：与水面场景彻底分开。
   水面用写实/航拍素材，水下用同一张实景底图 + 独立的声纳覆盖层，
   两者互斥渲染，不会叠在一起。 */
.ws-under-bg {
  opacity: 1;
  z-index: 0;
}
/* ===== 水下声纳覆盖层 =====
 * 声纳波束与扩散波纹在底图之上、ECharts（.ws-chart）之下：
 *   底图 → 声纳波 → 航迹/坐标
 * 这样波纹覆盖水面，但不会挡住航迹与坐标轴。
 *
 * z-index 只能设 1：.ws-chart 是 position:absolute 且 z-index:auto，
 * 两者同为定位元素时，z-index:1 的声纳层会盖在 .ws-chart 之上，
 * 因此这里给 .ws-chart 显式 z-index:2（见下方 .ws-chart 规则）。 */
.ws-under-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 1;
}
/* ===== V2 写实场景层 =====
 * 一张写实航拍底图 + 海面贴图 + 护岸/防波堤，
 * 与 .ws-chart（航迹层）同坐标系，共用同一套米→像素换算，
 * 因此底图与实测航迹永远像素级对齐。
 *
 * 【层级说明】
 * 这里**不能**设置 z-index —— 一旦抬高，背景就会盖住 ECharts 航迹层，
 * 表现为「底图是一层、航迹是另一层」的两层割裂感。
 * 正确做法与 V1 的 .ws-harbor-bg 一致：
 *   背景层 z-index 为 auto（0），靠 DOM 顺序排在 .ws-chart 之前，
 *   于是航迹、船、标签自然叠在背景之上，成为同一个画面。 */
.ws-v2-scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: auto;
}

/*
 * 传感器层（雷达 / AIS / 光电）：z-index 7。
 *
 * 【为什么必须显式给 7】
 * 船体贴图层是 z-index:6、航迹层是 z-index:2 ——
 * 传感器层要**盖在船上面**，否则：
 *   · AIS 报文链路（岸台 → 船上的报文框）会被船身挡住一截；
 *   · 光电跟踪框（四角括号）根本套不到船身上，等于白画。
 *
 * 而 7 又必须小于 .ws-station-hint（14）等 HUD 层，
 * 免得传感器动画压住操作提示。
 */
.ws-v2-sensors {
  z-index: 7;
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
  /* 水下模式下声纳波纹层 z-index:1，这里抬到 2，
     保证航迹与坐标轴始终盖在声纳波之上。 */
  z-index: 2;
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
/* 传感器图例色块：与 SceneSensorsV2 的主色严格一致 */
.sw.sen-radar { background: linear-gradient(90deg, #ff8a5a, rgba(255,107,90,.15)); height: 8px; box-shadow: 0 0 6px rgba(255,138,90,.55); }
.sw.sen-ais { background: linear-gradient(90deg, #4fe07a, rgba(79,224,122,.15)); height: 8px; box-shadow: 0 0 6px rgba(79,224,122,.55); }
/* AIS 报文链路：断续绿线 + 一个亮点（对应 SceneSensorsV2 的 .ws-ais-link-*） */
.sw.sen-ais-link { background: repeating-linear-gradient(90deg, #8affc0 0 3px, transparent 3px 7px); height: 3px; box-shadow: 0 0 6px rgba(138,255,192,.5); }
.sw.sen-eo { background: linear-gradient(90deg, #ffd54a, rgba(255,213,74,.15)); height: 8px; box-shadow: 0 0 6px rgba(255,213,74,.55); }
/* 光电跟踪框：四角括号（对应 .ws-sen-eo-lock） */
.sw.sen-eo-lock { background: transparent; border: 1px solid #ffd54a; height: 10px; width: 10px; box-shadow: 0 0 6px rgba(255,213,74,.5); border-radius: 0; }
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
