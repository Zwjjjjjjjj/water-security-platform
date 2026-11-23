<script setup lang="ts">
/**
 * V2 水上场景的「传感器层」—— 雷达 / AIS / 光电。
 *
 * 【为什么单独一层】
 * 三类传感器在画面上的表达方式差异很大：
 *   · 雷达（RADAR）—— 主动探测，有**旋转扫描线**与**测距环**；
 *   · AIS（AIS）  —— 被动接收，有**接收脉冲提示**与**目标连线**；
 *   · 光电（EO/IR）—— 有**视场锥角**、**十字分划**与**云台转动**。
 *
 * 这些都要做动画，若混进 ECharts series 会让 setOption 的频率非常高，
 * 影响回放性能。因此统一用 SVG + CSS 动画实现。
 *
 * 【坐标系 —— 全部走 ENU 平面投影（本文件的核心）】
 *
 * 之前的实现是「在屏幕上画圆 + CSS rotate(方位角)」。
 * 在斜俯视底图上有三个致命问题：
 *
 *   1. **不在同一平面**：
 *      屏幕上的正圆是「正对着观众」的，
 *      而雷达波束实际躺在**水面这个斜面上**。
 *      斜视投影后，ENU 平面上的圆会被压扁成椭圆；
 *      直接画正圆 + 旋转，就会出现「橙色光环贴在屏幕上」的感觉。
 *
 *   2. **角度含义错位**：
 *      方位角（0°=真北，顺时针增加）在斜视投影下**不是屏幕角度**。
 *      屏幕上的旋转角与地理方位角之间没有简单关系，
 *      所以 `rotate(175°)` 扫的根本不是正南偏西。
 *
 *   3. **原点会漂**：
 *      缩放/平移时若用屏幕坐标画圆，圆心与雷达站标记会分离。
 *
 * 【本文件的做法】
 * 统一走这条链路：
 *
 *   方位角 θ（ENU 平面，真北为 0°、顺时针）
 *        ↓  E = Er + r·sinθ ,  N = Nr + r·cosθ
 *   ENU 平面上的点 (E, N)
 *        ↓  enuToImage()（仿射）
 *   底图图片像素
 *        ↓  obliqueImageToContainer()（× r + 偏移）
 *   容器像素
 *
 * 扇面边缘、距离环、扫描线端点**逐个**经过这条链路，
 * 于是：
 *   · 圆自动变成贴在水面斜面上的椭圆（与底图同源投影）；
 *   · 屏幕角度自动与地理方位角对应；
 *   · 缩放平移时，扫描原点始终贴住雷达站标记。
 *
 * 【角度约定】
 * 方位角 θ：**0° = 真北，顺时针增加**（与罗盘一致）。
 *   E = E_r + r·sinθ
 *   N = N_r + r·cosθ
 *
 * 【配准状态 —— 重要】
 * 斜视底图是**概念场景图**，未与实测 ENU 做地理配准。
 * 因此这里的方位角只保证**几何自洽**（北/东/南/西四方向正确、
 * 扫描原点贴住雷达站），不代表雷达真实的安装方位。
 * 界面上标注为「扫描示意」。
 */
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { computeObliqueFit, obliqueImageToContainer, OBLIQUE_NAT_W } from './oblique-fit'
import { enuToImage } from './scenes.config.v2'
import { SENSOR_ARCHIVE } from './sensor-archive'
import { getSceneAssetsV2 } from './scene-assets.v2'

const props = defineProps({
  assetsReady: { type: Boolean, default: false },
  chartW: { type: Number, default: 1920 },
  chartH: { type: Number, default: 870 },
  xMin: { type: Number, default: -2600 },
  xMax: { type: Number, default: 1000 },
  yMin: { type: Number, default: 3000 },
  yMax: { type: Number, default: 5000 },
  gridLeft: { type: Number, default: 0 },
  gridRight: { type: Number, default: 0 },
  gridTop: { type: Number, default: 0 },
  gridBottom: { type: Number, default: 0 },
  /** 选中的传感器编号（用于高亮） */
  selectedSensor: { type: String, default: '' },
  /** 选中的目标编号（光电优先跟踪选中目标） */
  selectedTargetId: { type: String, default: '' },
  /** 回放时刻（毫秒）—— 扫描角度由「参考角度 + 经过时间 ÷ 周期」计算 */
  timeMs: { type: Number, default: 0 },
  /** 回放是否暂停 */
  paused: { type: Boolean, default: false },
  /**
   * 当前回放时刻的水面目标（用于 AIS 动态连线 / 光电跟踪）。
   *
   * 【为什么需要】
   * AIS 与光电都是**对目标起作用的**传感器：
   *   · AIS 岸台 → 目标：一条「接收链路」，随报文上报节奏闪烁；
   *   · 光电转塔 → 目标：一条「跟踪链路」，云台会**转向被跟踪的目标**。
   *
   * 只给传感器自己的位置，画不出「它在跟谁」。
   *
   * 【数据结构 —— 必须是船体贴图的屏幕锚点】
   * 每项包含：
   *   · `id` / `name` / `mmsi` / `speed` —— 目标标识
   *   · `sources`                        —— 感知来源（`['R01','A01']` 等）
   *   · `east` / `north`                 —— ENU 落位（陆域点已推到可航水域）
   *   · `x` / `y`                        —— **船体轮廓中心的容器像素**
   *
   * `x/y` 由 `ChartSceneV2` 用**与船体贴图完全相同**的
   * `projectEnu` + `displayPosition` 链路算出。
   * 若传感器层自己用 `project()` 另算一套，链路末端会偏离船体几十像素。
   */
  targets: { type: Array, default: () => [] },
})

/* ------------------------------------------------------------------ *
 * 传感器定义
 * ------------------------------------------------------------------ */

export type SensorKind = 'radar' | 'ais' | 'eo'

export interface SensorDef {
  id: string
  kind: SensorKind
  /** ENU 坐标（米）—— 由底图像素反算得到 */
  east: number
  north: number
  /** 名称 */
  name: string
  /** 探测半径（米） */
  rangeM: number
  /** 扫描周期（秒） */
  period: number
  /** 初始相位（秒），让各传感器不同步 */
  phase: number
  /** 光电：水平视场角（度） */
  fovDeg?: number
  /** 接收/扫描扇面半角（度）—— AIS 用它表示示意性的方向增益 */
  halfAngleDeg?: number
  /**
   * 扫描参考方位角（度，0 = 真北，顺时针）。
   *
   * 所有传感器的波束都必须指向**水域** —— 传感器本身在陆上，
   * 若波束扫过厂区，动画与背景就会「对不上」。
   *
   * 参考窗口的 ENU ↔ 底图像素是仿射映射，
   * 由底图像素反算得到各传感器的 ENU 位置后，
   * 逐个方向检查水域覆盖率，取水域占比最高的方向。
   */
  bearingDeg?: number
  /**
   * 设备本体贴图的素材 id（scenes-v2 manifest）。
   *
   * 【为什么用贴图而不是矢量光点】
   * 原本这一层只用「发光小圆点 + 外环 + 文字」表示设备。
   * 但真实场景里岸基雷达站、AIS 岸台、光电转塔都是**有形体的构筑物**，
   * 只用光点会让画面读不出「这里站着什么设备」。
   *
   * 贴图为固定 45° 斜俯视立牌，与本场景的固定斜视底图视角一致。
   */
  spriteId?: string
  /**
   * 贴图显示高度（容器像素）—— 覆盖素材包的建议值。
   *
   * 素材包建议：AIS 岸台 70~100px、雷达站 80~110px、光电转塔 40~60px。
   * 这里取偏上限，因为本场景底图是 1920×870 的大画面，
   * 设备若按建议下限显示会小到看不清形体。
   */
  spriteHeightPx?: number
  /**
   * 设备编号（对应 `devices.json` 的 `source_id`）。
   *
   * 用于把动画与数据源对上：面板里显示的「R01 / A01 / E01」
   * 就是真实数据里的设备号，而不是图层自造的编号。
   */
  sourceId?: string
  /**
   * 设备型号（演示配置，非核验型号）。
   *
   * 素材包说明这三张图均为「通用概念造型」，
   * 不对应已核验的真实设备型号 —— 因此界面上标注为示意型号。
   */
  model?: string
  /**
   * 安装高程（米）—— 陆域高地的示意高程。
   *
   * 用于「立牌」的高度感：设备越高，贴图底部需要额外的
   * 「离地」视觉补偿（贴在地面上但躯干更高）。
   */
  elevationM?: number
}

/**
 * 三类传感器的布置表。
 *
 * 【位置来源】
 * 取自 `campus_oblique_v3.png` 的实测像素位置，再反算成 ENU：
 *   · 雷达站：厂区西南侧高地（植被覆盖，能看到开阔海面）
 *   · AIS 岸台：厂区南侧岸边码头（灰色硬化地面）
 *   · 光电转塔：厂区东南角平台（灰色硬化地面）
 *
 * ENU 反算（依据 `scenes.config.v2.ts` 的仿射系数）：
 *   RADAR-01: px(262,505)  → ENU(-1985.9, 4034.4)
 *   AIS-01   : px(790,700)  → ENU( -748.4, 3587.5)
 *   EO-01    : px(1085,630) → ENU(  -57.0, 3747.9)
 */
const SENSORS: SensorDef[] = SENSOR_ARCHIVE.filter((a) => a.kind !== 'sonar').map((a) => ({
  id: a.id,
  kind: a.kind as SensorKind,
  east: a.east,
  north: a.north,
  name: a.name,
  rangeM: a.rangeM,
  period: a.period,
  phase: a.phase,
  fovDeg: a.fovDeg,
  halfAngleDeg: a.halfAngleDeg,
  bearingDeg: a.bearingDeg,
  spriteId: a.spriteId,
  /**
   * 贴图显示高度（容器像素）—— 覆盖素材包的建议值。
   *
   * 素材包建议：AIS 岸台 70~100px、雷达站 80~110px、光电转塔 40~60px。
   * 这里取偏上限，因为本场景底图是 1920x870 的大画面，
   * 设备若按建议下限显示会小到看不清形体。
   */
  spriteHeightPx:
    a.kind === 'radar' ? 104 : a.kind === 'ais' ? 94 : a.kind === 'eo' ? 60 : undefined,
  sourceId: a.sourceId,
  model: a.model,
  elevationM: a.elevationM,
}))

/**
 * AIS 岸台的 ENU 坐标（从 SENSORS 中取，避免两处写死同一组数字）。
 */
const AIS_STATION = {
  east: SENSORS.find((s) => s.kind === 'ais')!.east,
  north: SENSORS.find((s) => s.kind === 'ais')!.north,
}

/* ------------------------------------------------------------------ *
 * 映射
 * ------------------------------------------------------------------ */

const fit = computed(() =>
  computeObliqueFit({
    xMin: props.xMin,
    xMax: props.xMax,
    yMin: props.yMin,
    yMax: props.yMax,
    gridLeft: props.gridLeft,
    gridTop: props.gridTop,
    plotW: Math.max(50, props.chartW - props.gridLeft - props.gridRight),
    plotH: Math.max(50, props.chartH - props.gridTop - props.gridBottom),
    anchorX: 0,
    anchorY: 0,
  }),
)

/**
 * ENU 米制 → 容器像素。
 *
 * 【链路】
 * ENU ──enuToImage()──▶ 底图图片像素 ──obliqueImageToContainer()──▶ 容器像素
 *
 * 与 `SceneObliqueV2`（底图）、`SceneUnderwaterV2`（声纳）用的是**同一套** fit，
 * 因此传感器、底图、航迹、声纳严格像素级对齐。
 */
function project(east: number, north: number): [number, number] {
  const [imgX, imgY] = enuToImage(east, north)
  return obliqueImageToContainer(fit.value, imgX, imgY)
}

/**
 * 斜视场景的**视觉纵向压缩比**。
 *
 * 【为什么需要这个系数 —— 这是本次改动最关键的一点】
 *
 * `enuToImage()` 是一个**各向同性**的仿射映射
 * （a ≈ 0.4267 像素/米，|d| ≈ 0.4364 像素/米，比值 1.02 ≈ 1），
 * 所以 ENU 平面上的圆经它投影后**仍然是正圆**。
 *
 * 但底图 `campus_oblique_v3.png` 是**斜俯视的概念场景图**：
 * 画面里的水面是「躺平」的斜面，岸线是斜的，
 * 沿画面纵向被**压扁**了（与声纳波圈用同一套逻辑，ry/rx ≈ 0.46）。
 *
 * 也就是说：
 *   · **几何映射**（ENU↔图片）告诉��「这个点在哪」；
 *   · **视觉平面**告诉我们的却是「水面这个斜面长什么样」。
 *
 * 两者不是一回事。若只按几何映射画圆，
 * 得到的正圆会「立」在画面上，而不是「躺」在水面上 ——
 * 这正是用户看到的「橙色光环像贴在屏幕上的圆，与斜视港区不在同一平面上」。
 *
 * 【做法】
 * 先把 ENU 相对原点的**位移**投影到屏幕（得到未压缩的方向与长度），
 * 再把纵向分量乘以压缩比 —— 等价于「在斜面坐标系里画好，再压到屏幕上」。
 *
 * 压缩比 0.46 与声纳波圈（`SceneUnderwaterV2.surfaceEllipsePath`）保持一致，
 * 这样雷达波束圈与声纳波圈看起来在同一张「水面斜面」上。
 */
const OBLIQUE_Y_RATIO = 0.46

/**
 * 方位角 + 半径 → 容器像素（**贴合水面斜面**）。
 *
 * @param east       原点 ENU 东坐标（米）
 * @param north      原点 ENU 北坐标（米）
 * @param rangeM     距离（米）
 * @param bearingDeg 方位角（度，0=真北，顺时针）
 */
function polar(
  east: number,
  north: number,
  rangeM: number,
  bearingDeg: number,
): [number, number] {
  const a = (bearingDeg * Math.PI) / 180
  /* 真北为 0°、顺时针增加：
   *   E = Er + r·sinθ   （东向分量）
   *   N = Nr + r·cosθ   （北向分量） */
  const e = east + rangeM * Math.sin(a)
  const n = north + rangeM * Math.cos(a)

  /* 原点与目标点的屏幕位置 */
  const [ox, oy] = project(east, north)
  const [px, py] = project(e, n)

  /* 纵向位移按斜视压缩比压扁 —— 得到「躺在水面上」的椭圆 */
  return [px, oy + (py - oy) * OBLIQUE_Y_RATIO]
}

/**
 * 在 ENU 平面上采样一段圆弧，逐点投影到容器像素。
 *
 * 【为什么必须逐点投影，不能画一个椭圆】
 * 斜视投影带**纵向压缩**（见 `OBLIQUE_Y_RATIO`），
 * ENU 平面上的圆在屏幕上是**压扁的椭圆**，
 * 且：
 *   · 半轴比与中心点随视口缩放而变；
 *   · 扇面边缘不是正椭圆的一段（因为压缩只作用于纵向位移）。
 *
 * 用「采样 → 连线」的方式可以让圆环**永远精确**，
 * 且缩放时自动跟着底图变形。
 *
 * @param steps 采样点数（72 → 每 5° 一点，足够平滑）
 */
function arcPoints(
  east: number,
  north: number,
  rangeM: number,
  startDeg: number,
  endDeg: number,
  steps = 72,
): string {
  const pts: string[] = []
  for (let i = 0; i <= steps; i++) {
    const deg = startDeg + ((endDeg - startDeg) * i) / steps
    const [x, y] = polar(east, north, rangeM, deg)
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  }
  return `M ${pts.join(' L ')}`
}

/**
 * 扇面路径（ENU 平面上的扇形，逐边采样后投影）。
 *
 * 【顶点必须用同一个压缩链路】
 * 扇面的顶点是原点（雷达站），
 * 弧上的点走 `polar()`（含纵向压缩），
 * 所以顶点也必须用同一套，否则扇面会被拉歪。
 *
 * @param halfAngleDeg 半角（度）
 * @param centerDeg    中心方位角（度）
 */
function sectorPath(
  east: number,
  north: number,
  rangeM: number,
  halfAngleDeg: number,
  centerDeg: number,
): string {
  const steps = 24
  const pts: string[] = []
  /* 沿弧从 -半角 扫到 +半角 */
  for (let i = 0; i <= steps; i++) {
    const deg = centerDeg - halfAngleDeg + ((2 * halfAngleDeg) * i) / steps
    const [x, y] = polar(east, north, rangeM, deg)
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  }
  /* 回到原点闭合 */
  const [ox, oy] = project(east, north)
  return `M ${ox.toFixed(1)},${oy.toFixed(1)} L ${pts.join(' L ')} Z`
}

/* ------------------------------------------------------------------ *
 * 扫描角度 —— 跟随时间，不逐帧累加
 * ------------------------------------------------------------------ */

/**
 * 当前扫描角度（度）。
 *
 * 【为什么不用「逐帧累加角度」】
 * 逐帧累加的扫描线有三个问题：
 *   1. 与回放**脱钩**：暂停/倍速/拖时间条时，扫描角度不跟着变；
 *   2. **漂移**：页面切后台再回来，rAF 停摆，时间差被一次性补上，
 *      扫描线会「跳」一大段；
 *   3. 无法复现：同一时刻两次进入页面，扫描角度不同。
 *
 * 正确做法：**参考角度 + 经过时间 ÷ 扫描周期**。
 * 这样扫描角度是**时间的纯函数**，与帧率、暂停、后台切换都无关。
 *
 * @param period 扫描周期（秒）
 * @param phase  初始相位（秒）
 * @param bearingDeg 参考方位角（度）
 */
function scanAngle(period: number, phase: number, bearingDeg: number): number {
  /* 经过时间（秒）—— 场景时间轴 + 相位偏移 */
  const elapsed = props.timeMs / 1000 + phase
  const turns = (elapsed / period) % 1
  /* 顺时针扫描 */
  return bearingDeg + turns * 360
}

/**
 * 动画时钟 —— 只用于「扫尾渐隐」这类纯视觉的连续量。
 *
 * 扫描线**本身**的位置由 `scanAngle()` 按时间计算（与回放同步），
 * 不依赖这个时钟。
 */
const animT = ref(0)
let rafId = 0
let lastTs = 0

function tick(ts: number) {
  rafId = requestAnimationFrame(tick)
  if (!lastTs) lastTs = ts
  const dt = Math.min(120, ts - lastTs)
  lastTs = ts
  animT.value = (animT.value + dt) % 1000000
}

onMounted(() => {
  if (typeof requestAnimationFrame === 'function') {
    lastTs = 0
    rafId = requestAnimationFrame(tick)
  }
})

onBeforeUnmount(() => {
  if (rafId) cancelAnimationFrame(rafId)
  rafId = 0
})

/**
 * 设备本体贴图的放置参数（容器像素）。
 *
 * 【为什么单独算一层】
 * 这一层画的是**站在地面上的实体设备**（雷达站 / AIS 岸台 / 光电转塔），
 * 而 `sensorShapes` 画的是**水面上的波束**（扫描扇面、距离环、视场锥）。
 *
 * 二者的坐标原点含义不同：
 *   · 波束的原点 = 设备在 ENU 平面的**投影点**（贴地的那个点）；
 *   · 贴图的原点 = 设备**底座落地点**（也贴地，但立牌向上生长）。
 *
 * 所以贴图不能简单地用 `(cx, cy)` 当中心，
 * 必须用「底座锚点」把图片的落地点对准 `(cx, cy)`。
 *
 * 【锚点约定】
 * 素材包给出的 `ground_anchor_normalized_initial_guess` 是未核验的初值，
 * 本工程按每张图的 alpha 轮廓重新核验了底座行（见 tools/verify-sensor-sprites.py）：
 *   · ais_shore_station_45   底座在图片底部，锚点 ≈ (0.489, 0.985)
 *   · eo_turret_45            底座在图片底部，锚点 ≈ (0.405, 0.985)
 *   · coastal_radar_station_45 底座在图片底部，锚点 ≈ (0.495, 0.985)
 *
 * 锚点是**归一化图片坐标**（左上原点）：
 *   显示左上角 = (设备屏幕位置) − (锚点 × 显示尺寸)
 */
interface SensorSpritePlacement {
  /** 贴图 URL */
  href: string
  /** 显示宽度（容器像素） */
  w: number
  /** 显示高度（容器像素） */
  h: number
  /** 图片左上角（容器像素） */
  left: number
  /** 图片左上角（容器像素） */
  top: number
}

/**
 * 读取设备贴图的素材信息。
 *
 * 素材清单是**异步加载**的（`loadSceneAssetsV2()`），
 * 因此这里必须依赖 `props.assetsReady`：
 * 若不在 computed 里读它，首次渲染时清单还是空的，
 * 贴图就永远不会出现（也不会随加载完成而重算）。
 */
const sensorSprites = computed(() => {
  const out: Record<string, SensorSpritePlacement> = {}
  if (!props.assetsReady) return out

  const assets = getSceneAssetsV2()
  for (const s of SENSORS) {
    if (!s.spriteId) continue
    const asset = assets.find((a) => a.id === s.spriteId)
    if (!asset) continue

    const natW = asset.width_px || 1024
    const natH = asset.height_px || 1024

    /* 显示高度：配置值优先，其次素材包建议值 */
    const dispH = s.spriteHeightPx ?? asset.display_height_px ?? 100
    /* 按原始像素比例推算显示宽度（保持素材不变形） */
    const dispW = dispH * (natW / natH)

    /* 底座锚点：manifest 的 ground_anchor_uv 优先 */
    const anchor = asset.ground_anchor_uv ?? asset.anchor_uv ?? [0.5, 0.985]

    /* 设备在屏幕上的位置（贴地点） */
    const [cx, cy] = project(s.east, s.north)

    out[s.id] = {
      href: asset.url,
      w: dispW,
      h: dispH,
      /* 图片左上角 = 落地点 − 锚点 × 显示尺寸 */
      left: cx - anchor[0] * dispW,
      top: cy - anchor[1] * dispH,
    }
  }
  return out
})

/**
 * 传感器形状数据（容器像素）。
 *
 * 【为什么在 script 里算】
 * 模板表达式里写内联箭头函数会让 Vue 的模板类型检查失去局部变量推断，
 * 且每次渲染都重复计算。统一在 computed 里算，模板只取结果。
 */
const sensorShapes = computed(() =>
  SENSORS.map((s) => {
    const [cx, cy] = project(s.east, s.north)
    const bearing = s.bearingDeg ?? 0

    /* 扫描线端点（ENU 平面计算 → 投影） */
    const scanDeg = scanAngle(s.period, s.phase, bearing)
    const [sx, sy] = polar(s.east, s.north, s.rangeM, scanDeg)

    /* 扫尾：从当前角度往回拖 20°，形成渐隐拖尾 */
    const trailDeg = 20
    const trail: string[] = []
    const trailSteps = 14
    for (let i = 0; i <= trailSteps; i++) {
      const deg = scanDeg - trailDeg + (trailDeg * i) / trailSteps
      const [x, y] = polar(s.east, s.north, s.rangeM, deg)
      trail.push(`${x.toFixed(1)},${y.toFixed(1)}`)
    }

    /* --------------------------------------------------------------
     * 光电：视场锥指向**正在跟踪的目标**，而不是固定方位。
     *
     * 【为什么必须动态】
     * 固定视场锥 = 一块贴在屏幕上的黄色扇形，看不出「在跟谁」。
     * 让锥体随跟踪目标的方位角转动，
     * 画面才会读出「转塔正在把云台转向那艘船」。
     * -------------------------------------------------------------- */
    const eoBearing =
      s.kind === 'eo' && eoTarget.value ? eoTarget.value.bearingDeg : bearing

    /* 云台转动是「活」的：跟踪方位 + 一个小幅摆动。
     * 摆动幅度 1.6°，周期与转塔的伺服周期一致（9s），
     * 让它看起来像「伺服电机在微调」，而不是死盯。 */
    const eoWobble =
      s.kind === 'eo' && eoTarget.value
        ? Math.sin((props.timeMs / 1000 / s.period) * Math.PI * 2) * 1.6
        : 0
    const eoLiveBearing = eoBearing + eoWobble

    return {
      ...s,
      cx,
      cy,
      bearing,
      /* 光电实时朝向 */
      eoLiveBearing,
      /* 扫描线端点 */
      scanX: sx,
      scanY: sy,
      /* 扫尾折线 */
      trailPath: `M ${trail.join(' L ')}`,
      /* 扫尾透明度（起点最淡 → 末端最亮） */
      trailGradId: `senTrail-${s.id}`,
      /* 距离环：ENU 平面上采样的圆弧 → 投影 */
      ringPath: arcPoints(s.east, s.north, s.rangeM, 0, 360, 72),
      ringInnerPath: arcPoints(s.east, s.north, s.rangeM * 0.62, 0, 360, 60),
      /* 扇面（低透明度） */
      sectorPath: sectorPath(s.east, s.north, s.rangeM, 26, scanDeg),
      /* 光电视场锥 —— 跟随跟踪目标 */
      eoConePath: sectorPath(
        s.east,
        s.north,
        s.rangeM,
        (s.fovDeg ?? 26) / 2,
        eoLiveBearing,
      ),
      /* AIS 定向接收扇面 */
      aisBeamPath: sectorPath(
        s.east,
        s.north,
        s.rangeM,
        s.halfAngleDeg ?? 25,
        bearing,
      ),
      /* AIS 脉冲圈（ENU 圆环） */
      aisRingPath: arcPoints(s.east, s.north, s.rangeM * 0.22, 0, 360, 40),
      /* 扫尾渐变 id */
      trailGrad: `senTrailGrad-${s.id}`,
      isSel: props.selectedSensor === s.id,
    }
  }),
)

/**
 * 雷达天线的旋转指示（贴在雷达贴图顶部）。
 *
 * 【为什么需要】
 * 素材包说明得很清楚：三张图都是**整机静态图**，
 * 「不包含机械转动的天线或云台分层」。
 * 而且 README 特别提醒：**不宜把整座塔的 PNG 当船符进行平面旋转** ——
 * 那样整座塔会跟着转，看起来非常假。
 *
 * 正确做法是：
 *   · 塔身贴图保持静止（作为固定的「机座」）；
 *   · 在天线所在的**局部位置**叠加一小段旋转指示（波瓣/扫描叶），
 *     表示「天线在转」。
 *
 * 【同步】
 * 旋转角度由 `scanAngle()` 给出 —— 与水面上的扫描线**同一个角度**，
 * 于是「塔顶天线指向哪」与「水面扫描线指向哪」永远一致。
 * 用 CSS `rotate()` 表达这个角度是安全的：
 * 这里是**屏幕上的指示器**，不是地理方位角本身。
 */
const radarAntennas = computed(() =>
  sensorShapes.value
    .filter((s) => s.kind === 'radar')
    .map((s) => {
      const sp = sensorSprites.value[s.id]
      /* 天线枢轴：贴图顶部的某个高度处 */
      const pivotY = sp ? sp.top + sp.h * 0.3 : s.cy - 40
      const pivotX = sp ? sp.left + sp.w * 0.5 : s.cx
      /* 天线臂长：随贴图尺寸缩放 */
      const arm = sp ? Math.max(16, sp.w * 0.42) : 30
      return {
        id: s.id,
        pivotX,
        pivotY,
        arm,
        /* 当前扫描角（度） */
        angle: scanAngle(s.period, s.phase, s.bearing),
        /* 动画周期（秒）—— 与扫描周期一致 */
        period: s.period,
        /* 相位（秒）—— 让各雷达不同步 */
        phase: s.phase,
      }
    }),
)

/**
 * 光电转塔的云台朝向指示。
 *
 * 【与雷达天线的区别】
 * 雷达天线是**连续旋转**的（转一圈 6 秒），
 * 而光电云台是**指向目标**的 —— 它不会自己乱转，
 * 而是跟着被跟踪的船摆动，只在跟踪方位附近做小幅微调。
 *
 * 所以这里不用 CSS 旋转动画，而是**每帧按跟踪方位重算**：
 *   · 主朝向 = `eoTarget.bearingDeg`（云台正在看的方向）；
 *   · 微调摆动 = `eoWobble`（伺服电机在微调，±1.6°）。
 *
 * 屏幕角与地理方位角的关系：
 * 斜视投影下方位角不是屏幕角，但**云台指示器是屏幕上的一个箭头**，
 * 它的作用是「让观众看出云台在转向哪艘船」，
 * 因此用「目标在屏幕上的方向角」表达最直观：
 *   屏幕方向角 = atan2(目标x − 塔x, 目标y − 塔y)
 */
const eoTurret = computed(() => {
  const s = sensorShapes.value.find((x) => x.kind === 'eo')
  if (!s) return null
  const sp = sensorSprites.value[s.id]
  /* 枢轴：光电转塔的镜头位置（贴图上部） */
  const pivotX = sp ? sp.left + sp.w * 0.5 : s.cx
  const pivotY = sp ? sp.top + sp.h * 0.34 : s.cy - 24
  const arm = sp ? Math.max(12, sp.w * 0.36) : 20

  /* 云台当前指向（屏幕角） */
  let screenAngle: number
  if (eoTarget.value) {
    /* 指向目标在屏幕上的方向 */
    const dx = eoTarget.value.tx - pivotX
    const dy = eoTarget.value.ty - pivotY
    /* SVG 的 y 轴向下，rotate() 为顺时针，
     * 所以屏幕方向角 = atan2(dx, dy) */
    screenAngle = (Math.atan2(dx, dy) * 180) / Math.PI
  } else {
    /* 无跟踪目标时，用视场中心的屏幕方向 */
    screenAngle = (Math.atan2(s.scanX - pivotX, s.scanY - pivotY) * 180) / Math.PI
  }

  return {
    id: s.id,
    pivotX,
    pivotY,
    arm,
    angle: screenAngle,
    /* 是否正在跟踪（有目标才亮起） */
    tracking: !!eoTarget.value,
  }
})

/**
 * AIS 岸台的天线接收脉冲点。
 *
 * 【语义】
 * AIS 是**被动接收** —— 岸台不主动照射，只在收到报文时闪一下。
 * 素材是整机静态图，所以用「天线顶端的一点绿光」表示「正在接收」。
 *
 * 脉冲节奏由 `period`（4.5s，AIS 报文周期）给出，
 * 与 `aisTargets` 的链路闪烁同源，画面上读作「一收一发」。
 */
const aisAntennas = computed(() =>
  sensorShapes.value
    .filter((s) => s.kind === 'ais')
    .map((s) => {
      const sp = sensorSprites.value[s.id]
      /* 天线顶端：贴图上部（VHF 竖直天线在顶部） */
      const tipX = sp ? sp.left + sp.w * 0.5 : s.cx
      const tipY = sp ? sp.top + sp.h * 0.12 : s.cy - 50
      return {
        id: s.id,
        tipX,
        tipY,
        period: s.period,
        phase: s.phase,
      }
    }),
)

/* ------------------------------------------------------------------ *
 * 雷达探测动画 —— 扫描线扫过目标时的回波响应
 * ------------------------------------------------------------------ */

/**
 * 雷达当前的探测目标（只取被雷达发现的，即 `sources` 含 "R01"）。
 *
 * 【与 AIS 的区别】
 * AIS 是被动接收、持续连线；雷达是主动照射、**周期性扫过才有一拍回波**。
 * 所以雷达目标的动画必须是「随扫描线经过而闪现」，而不是常亮。
 */
const radarTargets = computed(() => {
  const list = props.targets as any[]
  return list
    .filter(
      (t) =>
        Array.isArray(t.sources) &&
        t.sources.includes('R01') &&
        Number.isFinite(t.x) &&
        Number.isFinite(t.y),
    )
    .map((t) => {
      const radar = SENSORS.find((s) => s.kind === 'radar')!
      /* 目标相对雷达站的方位角与距离 */
      const dx = t.east - radar.east
      const dz = t.north - radar.north
      const bearing = ((Math.atan2(dx, dz) * 180) / Math.PI + 360) % 360
      const rangeM = Math.hypot(dx, dz)
      const inRange = rangeM <= radar.rangeM
      return {
        id: t.id,
        name: t.name,
        x: t.x,
        y: t.y,
        bearing,
        rangeM,
        inRange,
        /* 相位错开，避免所有目标同时被扫到 */
        phase: (Number(String(t.id).replace(/\D/g, '')) || 1) * 0.41,
      }
    })
})

/**
 * 雷达扫描到某个目标时的**回波脉冲**参数。
 *
 * 【动画原理】
 * 扫描角 `scanDeg` 与目标方位角 `bearing` 的夹角越小 → 越接近被扫到。
 * 用夹角换算成「距上次扫过多少度」，再折算成动画延迟，
 * 让回波脉冲**恰好在扫描线经过目标的那一刻**亮起。
 *
 * 这样画面上就能读出「雷达正在一圈圈扫，扫到谁谁就闪一下」，
 * 而不是几个目标无缘无故地一起闪。
 */
const radarBlips = computed(() => {
  const radar = SENSORS.find((s) => s.kind === 'radar')!
  const scanDeg = scanAngle(radar.period, radar.phase, radar.bearingDeg ?? 0)
  return radarTargets.value
    .filter((t) => t.inRange)
    .map((t) => {
      /* 扫描角 → 目标方位 的顺时针夹角（0~360） */
      let delta = (scanDeg - t.bearing + 360) % 360
      /* 扫描线是顺时针转的：从扫描角「倒退」到目标方位，
       * 就是这个目标刚被扫过的时刻。换算成「扫描角落后目标」的角度差。 */
      const justScanned = (t.bearing - scanDeg + 360) % 360
      /* 扫描角落后目标的角度 → 距离下一次被扫到还有多久（度） */
      const ahead = (scanDeg - t.bearing + 360) % 360
      return {
        ...t,
        /* 归一化「扫描进度」0~1，用于驱动脉冲的透明度 */
        progress: justScanned / 360,
        /* 扫描线到目标的夹角（用于判断是否正在扫过） */
        delta: Math.min(delta, ahead),
        /* 当前是否正处在「刚扫过」的强回波段 */
        hot: justScanned < 26,
        /* 下一个回波周期（秒）—— 与雷达扫描周期一致 */
        cycle: radar.period,
      }
    })
})

/* ------------------------------------------------------------------ *
 * AIS 动态链路 —— 岸台 ↔ 被 AIS 发现的目标
 * ------------------------------------------------------------------ */

/**
 * AIS 岸台当前「收到」的目标。
 *
 * 【语义来源 —— 不是编的，是数据里本来就有的】
 * `targets.json` 里每个目标都带 `sources` 字段：
 *   · T001 → ["R01", "A01"]  雷达 + AIS **双源融合**
 *   · T002 → ["R01", "A01"]  同上
 *   · T003/T004/T005 → ["R01"]  仅雷达单源
 *
 * 也就是说：**只有 T001/T002 是 AIS 岸台真正发现并上报的船**。
 * 让 AIS 岸台连上 T003 就成了假数据。
 *
 * 【AIS 的物理特性 —— 动画节奏的依据】
 * AIS 是**被动接收**（VHF 广播被岸台收到），不是主动照射。
 * 所以它的动画语义应该是：
 *   · 岸台有一圈「报文接收脉冲」，按 AIS 报文周期节律闪；
 *   · 岸台到目标有一条**断续的连线**，表示「这条船的 AIS 报文被我收到了」；
 *   · 连线上有**流动光点**，方向是「岸台 → 目标」还是反向？
 *     → 船在**广播**，岸台在**接收**，所以能量方向是**目标 → 岸台**。
 *     （视觉上从船流向岸台 = 「信号被收到」，方向感与真实一致）
 */
const aisTargets = computed(() => {
  const list = props.targets as any[]
  return list
    .filter(
      (t) =>
        Array.isArray(t.sources) &&
        t.sources.includes('A01') &&
        Number.isFinite(t.x) &&
        Number.isFinite(t.y),
    )
    .map((t) => {
      /* 岸台位置仍用本层的 project()（传感器本体在底图上的固定位置） */
      const [ax, ay] = project(AIS_STATION.east, AIS_STATION.north)
      return {
        id: t.id,
        name: t.name,
        mmsi: t.mmsi,
        /* 目标在屏幕上的位置 —— **直接用船体贴图的锚点**，
         * 保证链路末端精确落在船上，而不是船旁边的空地。 */
        tx: t.x,
        ty: t.y,
        /* 岸台在屏幕上的位置 */
        ax,
        ay,
        /* 连线路径 */
        path: `M ${ax.toFixed(1)},${ay.toFixed(1)} L ${t.x.toFixed(1)},${t.y.toFixed(1)}`,
        /* 距岸台的直线距离（米），用于标签显示 */
        rangeM: Math.hypot(t.east - AIS_STATION.east, t.north - AIS_STATION.north),
        /* 相对岸台的方位角 */
        bearingDeg:
          ((Math.atan2(t.east - AIS_STATION.east, t.north - AIS_STATION.north) * 180) / Math.PI +
            360) %
          360,
        /* 每个目标的报文相位错开，避免所有连线同时闪 */
        phase: (Number(String(t.id).replace(/\D/g, '')) || 1) * 0.37,
      }
    })
})

/* ------------------------------------------------------------------ *
 * 光电跟踪 —— 云台对准被跟踪的目标
 * ------------------------------------------------------------------ */

/**
 * 光电转塔当前跟踪的目标。
 *
 * 【语义】
 * 光电是**主动指向**的设备：转塔会转动云台把被跟踪的船套进视场中心，
 * 画面上表现为：
 *   · 视场锥（黄）**指向目标**，而不是固定朝东南；
 *   · 目标上有**跟踪框 + 四角标记**；
 *   · 有一条细的「视线」连接塔与目标。
 *
 * 【为什么用「最近的目标」而不是「选中的目标」】
 * 真实光电转塔是自动跟踪的（配合雷达引导），
 * 默认跟踪探测距离内**方位角最接近塔的方向**的那艘船 ——
 * 这恰好也是最自然、最像真机的行为。
 * 若用户手动选中了目标，则优先跟踪选中目标。
 */
const eoTarget = computed(() => {
  const list = (props.targets as any[]).filter(
    (t) => Number.isFinite(t.x) && Number.isFinite(t.y),
  )
  if (!list.length) return null

  const EO_E = -57.0
  const EO_N = 3747.9
  const EO_RANGE = 1000

  const inRange = list.filter(
    (t) => Math.hypot(t.east - EO_E, t.north - EO_N) <= EO_RANGE,
  )
  const pool = inRange.length ? inRange : list

  /* 优先跟踪用户选中的目标 */
  const selId = props.selectedTargetId
  const selected = pool.find((t) => t.id === selId)
  const picked = selected ?? pool[0]
  if (!picked) return null

  /* 目标相对于光电塔的方位角（0 = 真北，顺时针） */
  const dx = picked.east - EO_E
  const dz = picked.north - EO_N
  const bearing = ((Math.atan2(dx, dz) * 180) / Math.PI + 360) % 360

  /* 目标屏幕坐标 —— **直接用船体贴图的锚点**，
   * 保证跟踪框精确套在船上。 */
  const tx = picked.x
  const ty = picked.y
  const [ex, ey] = project(EO_E, EO_N)

  return {
    id: picked.id,
    name: picked.name,
    mmsi: picked.mmsi,
    speed: picked.speed,
    /* 跟踪方位角 —— 视场锥据此绘制 */
    bearingDeg: bearing,
    /* 目标屏幕坐标 */
    tx,
    ty,
    /* 塔屏幕坐标 */
    ex,
    ey,
    rangeM: Math.hypot(dx, dz),
    /* 视线 */
    sightPath: `M ${ex.toFixed(1)},${ey.toFixed(1)} L ${tx.toFixed(1)},${ty.toFixed(1)}`,
  }
})
</script>

<template>
  <svg
    class="ws-sensors"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <!--
        雷达扫描扇面填充（低透明度 4%~8%）。

        【为什么这么淡】
        扇区填充的作用是「示意波束覆盖范围」，
        不是主角。太亮会盖住航迹与船体贴图 —— 那是画面的主要信息。
      -->
      <radialGradient id="senRadarScan">
        <stop offset="0" stop-color="#4fd8ff" stop-opacity="0.07" />
        <stop offset="0.55" stop-color="#4fd8ff" stop-opacity="0.035" />
        <stop offset="1" stop-color="#4fd8ff" stop-opacity="0" />
      </radialGradient>

      <!--
        雷达扫尾渐变（沿拖尾方向由淡到浓）。
        用 userSpaceOnUse 对准拖尾的首尾两点。
      -->
      <linearGradient
        v-for="s in sensorShapes"
        :id="s.trailGrad"
        :key="'tg-' + s.id"
        gradientUnits="userSpaceOnUse"
        :x1="s.cx"
        :y1="s.cy"
        :x2="s.scanX"
        :y2="s.scanY"
      >
        <stop offset="0" stop-color="#3fd0f5" stop-opacity="0.05" />
        <stop offset="1" stop-color="#7fe9ff" stop-opacity="0.42" />
      </linearGradient>

      <!-- AIS 定向接收扇面（低透明度） -->
      <radialGradient id="senAisBeam" cx="0" cy="0" r="1" gradientUnits="objectBoundingBox">
        <stop offset="0" stop-color="#4fe07a" stop-opacity="0.12" />
        <stop offset="0.6" stop-color="#4fe07a" stop-opacity="0.05" />
        <stop offset="1" stop-color="#4fe07a" stop-opacity="0" />
      </radialGradient>

      <!-- 光电视场（低透明度） -->
      <linearGradient id="senEoCone" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#ffd54a" stop-opacity="0.16" />
        <stop offset="1" stop-color="#ffd54a" stop-opacity="0.03" />
      </linearGradient>

      <!-- 传感器本体光晕（收敛，避免大面积发亮） -->
      <radialGradient id="senGlow">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.7" />
        <stop offset="0.3" stop-color="#9fe8ff" stop-opacity="0.22" />
        <stop offset="1" stop-color="#9fe8ff" stop-opacity="0" />
      </radialGradient>
    </defs>

    <!-- ===== 传感器图层 ===== -->
    <g v-for="s in sensorShapes" :key="s.id" class="ws-sensor" :class="'ws-sensor--' + s.kind">
      <!-- ============ 雷达：扫描线 + 扫尾 + 距离环 ============ -->
      <g v-if="s.kind === 'radar'">
        <!--
          扫描扇面（低透明度填充）。

          【投影方式 —— 本次改动的核心】
          扇面路径由 `sectorPath()` 在 **ENU 平面**上采样后逐点投影得到，
          因此它是「贴在水面斜面上的扇形」，
          而不是「正对着屏幕的一块饼」。
        -->
        <path :d="s.sectorPath" fill="url(#senRadarScan)" class="ws-sen-radar-sector" />

        <!--
          扫尾（拖尾）：约 20°，逐渐淡出。

          同样是 ENU 平面上采样出来的弧段，
          渐变由「起点淡 → 末端浓」。
        -->
        <path
          :d="s.trailPath"
          fill="none"
          :stroke="`url(#${s.trailGrad})`"
          stroke-width="2"
          stroke-linecap="round"
          class="ws-sen-radar-trail"
        />

        <!--
          主扫描线：青色，约 1~1.5px。

          端点是「当前扫描方位角」在 ENU 平面上的投影点 ——
          与距离环、扇面共用同一条链路，原点严格贴合雷达站标记。
        -->
        <line
          :x1="s.cx"
          :y1="s.cy"
          :x2="s.scanX"
          :y2="s.scanY"
          stroke="#8ff0ff"
          stroke-width="1.3"
          stroke-opacity="0.9"
          class="ws-sen-radar-scan"
        />

        <!--
          距离环：少量细线，低透明度。

          注意：**不再有橙色大光环**。
          之前的橙色量测环是一整个实心渐变圆，
          亮度盖住航迹，且因为是屏幕正圆，看着像「贴在屏幕上的圆」。
          现在改成 ENU 平面上采样出的细环线，天然贴合水面斜面。
        -->
        <path
          :d="s.ringPath"
          fill="none"
          stroke="#6fd8f0"
          stroke-width="0.8"
          stroke-opacity="0.24"
          stroke-dasharray="5 9"
        />
        <path
          :d="s.ringInnerPath"
          fill="none"
          stroke="#6fd8f0"
          stroke-width="0.7"
          stroke-opacity="0.15"
          stroke-dasharray="4 10"
        />

        <!--
          ===== 雷达回波脉冲：扫描线扫过目标时的响应 =====

          【画什么】
            · 目标点上一枚青色回波点（随扫描临近而变亮）；
            · 一圈从目标向外扩散、消隐的「回波环」；
            · 一条从雷达站指向目标的**回波引线**（扫描线扫过时的瞬时高亮）。

          【为什么必须与扫描线同步】
          雷达是主动照射，目标的回波只在**扫描线经过它的那一瞬间**才最强。
          如果目标只是常亮或自己匀速闪烁，画面就看不出「雷达在扫」，
          动画也失去了物理含义。

          这里把脉冲的相位锚定在「扫描角经过目标方位」的时刻，
          使整幅画面读出「雷达一圈圈扫过，每个目标被扫到时闪一下回波」。

          【锚点来源】
          目标坐标直接取船体贴图的屏幕锚点（见 props.targets 说明），
          因此回波点精确落在船上，不会出现「回波在旁边、船在别处」。
        -->
        <g v-for="b in radarBlips" :key="'rdrblip-' + b.id" class="ws-sen-radar-blip">
          <!-- 回波引线：雷达站 → 目标 -->
          <line
            :x1="s.cx"
            :y1="s.cy"
            :x2="b.x"
            :y2="b.y"
            stroke="#8ff0ff"
            stroke-width="1"
            stroke-opacity="0.4"
            class="ws-sen-radar-echo-line"
            :style="{
              animationDuration: b.cycle + 's',
              animationDelay: '-' + b.progress * b.cycle + 's',
            }"
          />
          <!-- 回波扩散环 -->
          <circle
            :cx="b.x"
            :cy="b.y"
            r="9"
            fill="none"
            stroke="#8ff0ff"
            stroke-width="1.4"
            class="ws-sen-radar-echo-ring"
            :style="{
              animationDuration: b.cycle + 's',
              animationDelay: '-' + b.progress * b.cycle + 's',
            }"
          />
          <!-- 回波点（核心亮点） -->
          <circle
            :cx="b.x"
            :cy="b.y"
            r="3.2"
            fill="#bff6ff"
            class="ws-sen-radar-echo-dot"
            :style="{
              animationDuration: b.cycle + 's',
              animationDelay: '-' + b.progress * b.cycle + 's',
            }"
          />
        </g>
      </g>

      <!-- ============ AIS：低调的接收更新提示 ============ -->
      <g v-else-if="s.kind === 'ais'">
        <!--
          AIS 岸台在陆上（厂区南侧码头），
          若是「全向大光圈」，脉冲圈会一大半压在厂区与山体上，
          看起来就像动画浮在背景之上、与底图无关。

          【本次调整】
          去掉绿色大光圈，改为：
            · 一条很淡的定向接收扇面（说明接收方向）；
            · 一个小幅的「接收更新」提示环（脉冲淡出）。
        -->
        <path :d="s.aisBeamPath" fill="url(#senAisBeam)" class="ws-sen-ais-cone" />

        <!-- 接收更新提示：小幅脉冲环（ENU 圆环） -->
        <path
          :d="s.aisRingPath"
          fill="none"
          stroke="#4fe07a"
          stroke-width="1.1"
          stroke-opacity="0.5"
          stroke-dasharray="4 5"
          class="ws-sen-ais-pulse"
          :style="{ animationDuration: s.period + 's', animationDelay: -s.phase + 's' }"
        />

        <!--
          ===== 动态 AIS 链路：岸台 ↔ 被 AIS 发现的目标 =====

          【画什么】
            · 一条断续绿线：岸台到目标，表示「这条船的 AIS 报文被岸台收到」；
            · 线上有流动光点，方向是 **目标 → 岸台**（船在广播，岸台在接收）；
            · 目标端一个绿色报文框，显示 MMSI 与距离。

          【为什么只有 T001/T002】
          见 `aisTargets` 的注释 —— 依据 `targets.json` 的 `sources` 字段，
          只有 `sources` 含 "A01" 的目标才是 AIS 岸台真正上报的船。
          给单源目标也画 AIS 线就成了假数据。
        -->
        <g v-for="t in aisTargets" :key="'aislink-' + t.id" class="ws-ais-link">
          <!-- 断续连线（底） -->
          <path
            :d="t.path"
            fill="none"
            stroke="#3ef3a3"
            stroke-width="1"
            stroke-opacity="0.34"
            stroke-dasharray="7 5"
            class="ws-ais-link-base"
          />
          <!--
            流动光点：报文从**目标流向岸台**。

            【实现】
            用 `stroke-dashoffset` 递减 + `dasharray` 极小值，
            等效于「一个亮点沿线段高速移动」。
            方向用 `animation-direction: reverse` 控制。
          -->
          <path
            :d="t.path"
            fill="none"
            stroke="#8affc0"
            stroke-width="2.1"
            stroke-linecap="round"
            stroke-dasharray="1 26"
            class="ws-ais-link-flow"
            :style="{
              animationDuration: '2.6s',
              animationDelay: '-' + t.phase + 's',
            }"
          />
          <!-- 目标端报文框 -->
          <g class="ws-ais-target">
            <!--
              报文框：MMSI 主行 + 距离/方位副行。

              【为什么框在目标的「上方」而不是正上方贴着】
              目标点就是船体贴图的**包围盒中心**（见 shipImageMatrixV2），
              船有 72~150px 长，正上方贴框会压住船艏。
              这里把框整体上移，让它悬在船艏之外的空中，
              视觉上是「这条报文属于这艘船」，又不遮挡船体。
            -->
            <rect
              :x="t.tx - 46"
              :y="t.ty - 47"
              width="92"
              height="30"
              rx="3"
              fill="#04192b"
              fill-opacity="0.88"
              stroke="#3ef3a3"
              stroke-opacity="0.85"
            />
            <text
              :x="t.tx"
              :y="t.ty - 32"
              text-anchor="middle"
              fill="#8affc0"
              font-size="11"
              class="ws-ais-target-text"
            >AIS {{ t.mmsi || t.id }}</text>
            <text
              :x="t.tx"
              :y="t.ty - 23"
              text-anchor="middle"
              fill="#5fe89a"
              fill-opacity="0.9"
              font-size="10.5"
              class="ws-ais-target-text"
            >{{ Math.round(t.rangeM) }}m · {{ Math.round(t.bearingDeg) }}°</text>
          </g>
        </g>
      </g>

      <!-- ============ 光电：视场锥 + 十字分划 ============ -->
      <g v-else>
        <!--
          视场锥（ENU 平面上采样后投影）。
          锥体的朝向 = **正在跟踪的目标方位角**（见 `eoTarget`），
          而不是固定方位 —— 这是「光电在跟踪」的关键视觉线索。
        -->
        <path :d="s.eoConePath" fill="url(#senEoCone)" class="ws-sen-eo-cone" />

        <!--
          视线：塔 → 被跟踪目标的一条细黄线。
          与雷达扫描线不同，这条线是**连续**的（光电是持续凝视，不像雷达那样扫）。
        -->
        <line
          v-if="eoTarget"
          :x1="s.cx"
          :y1="s.cy"
          :x2="eoTarget.tx"
          :y2="eoTarget.ty"
          stroke="#ffd54a"
          stroke-width="0.9"
          stroke-opacity="0.45"
          stroke-dasharray="3 4"
          class="ws-sen-eo-sight"
        />

        <!--
          跟踪框：套在目标身上的四角标记 + 中心十字。
          这是光电系统最典型的视觉语言 —— 「这台光电正锁着这个目标」。
        -->
        <g v-if="eoTarget" class="ws-sen-eo-lock">
          <g
            :transform="`translate(${eoTarget.tx} ${eoTarget.ty})`"
            :stroke-opacity="s.isSel ? 1 : 0.85"
          >
            <!-- 四角括号（长度 9px，间距 11px） -->
            <path
              d="M -16 -16 L -16 -25 M -16 -25 L -7 -25
                 M 16 -16 L 16 -25 M 16 -25 L 7 -25
                 M -16 16 L -16 25 M -16 25 L -7 25
                 M 16 16 L 16 25 M 16 25 L 7 25"
              fill="none"
              stroke="#ffd54a"
              stroke-width="1.5"
              stroke-linecap="round"
            />
            <!-- 中心十字 -->
            <line x1="-5" y1="0" x2="5" y2="0" stroke="#ffd54a" stroke-width="1" stroke-opacity="0.8" />
            <line x1="0" y1="-5" x2="0" y2="5" stroke="#ffd54a" stroke-width="1" stroke-opacity="0.8" />
          </g>
          <!--
            跟踪信息标签：挂在目标**下方**。

            【为什么放下方而 AIS 报文框放上方】
            一条船可能同时被 AIS 与光电盯着（本次数据里 T001 就是）。
            两个标签都放同一侧会叠字、互相盖住。
            AIS 报文框在上、光电跟踪框在下，天然错开。
          -->
          <g class="ws-sen-eo-lock-label">
            <rect
              :x="eoTarget.tx - 52"
              :y="eoTarget.ty + 30"
              width="104"
              height="30"
              rx="3"
              fill="#241a04"
              fill-opacity="0.88"
              stroke="#ffd54a"
              stroke-opacity="0.85"
            />
            <text
              :x="eoTarget.tx"
              :y="eoTarget.ty + 45"
              text-anchor="middle"
              fill="#ffe6a3"
              font-size="11"
              class="ws-sen-eo-label-text"
            >EO 跟踪 {{ eoTarget.id }}</text>
            <text
              :x="eoTarget.tx"
              :y="eoTarget.ty + 55"
              text-anchor="middle"
              fill="#ffcf5c"
              fill-opacity="0.9"
              font-size="10.5"
              class="ws-sen-eo-label-text"
            >{{ Math.round(eoTarget.rangeM) }}m · {{ Math.round(eoTarget.bearingDeg) }}°</text>
          </g>
        </g>

        <!-- 十字分划（固定在传感器位置，表示测角基准） -->
        <g class="ws-sen-eo-reticle" :stroke-opacity="s.isSel ? 0.95 : 0.6">
          <line :x1="s.cx - 7" :y1="s.cy" :x2="s.cx + 7" :y2="s.cy" stroke="#ffd54a" stroke-width="1.1" />
          <line :x1="s.cx" :y1="s.cy - 7" :x2="s.cx" :y2="s.cy + 7" stroke="#ffd54a" stroke-width="1.1" />
        </g>
      </g>

      <!-- ============ 设备本体（贴图立牌） ============ -->
      <!--
        【为什么用贴图而不是光点】
        原本这里只有「发光圆点 + 外环 + 文字」。
        但岸基雷达站、AIS 岸台、光电转塔都是**有形体的固定构筑物**，
        只用光点读不出「这里站着什么」。

        现在画成**斜俯视立牌**：
          · 图片底座锚点对准设备在场景中的落地点；
          · 立牌始终正立（不随水面斜面倾斜），与场景固定视角一致；
          · 贴图之上再叠原有的光点/外环/标签，
            保证动画语义（扫描、跟踪、链路）一条不少。

        【层级】
        立牌画在**波束层之下**：
        波束（扇面、视场锥、距离环）是水面上「躺平」的，
        设备是「立着」的，立牌应当遮挡波束而不是被波束压住。
      -->
      <g v-if="sensorSprites[s.id]" class="ws-sen-sprite-group">
        <!-- 接地阴影：让设备与地面之间有接触感 -->
        <ellipse
          :cx="s.cx"
          :cy="s.cy + 1"
          :rx="sensorSprites[s.id].w * 0.26"
          ry="3.5"
          fill="#03141f"
          fill-opacity="0.45"
          class="ws-sen-sprite-shadow"
        />
        <!-- 设备本体 -->
        <image
          :href="sensorSprites[s.id].href"
          :x="sensorSprites[s.id].left"
          :y="sensorSprites[s.id].top"
          :width="sensorSprites[s.id].w"
          :height="sensorSprites[s.id].h"
          preserveAspectRatio="xMidYMax meet"
          class="ws-sen-sprite"
          :class="{ 'is-sel': s.isSel }"
        />

        <!--
          ===== 雷达天线旋转指示 =====

          【为什么只在塔顶加一小段旋转，而不是转整张 PNG】
          素材是整机静态图，转整张图会让「塔身也在转」，
          视觉上非常假（README 也明确提醒了这一点）。

          正确做法：塔身静止，在天线的位置叠加一小段**波瓣指示**，
          角度与水面扫描线一致 —— 于是读出「天线在扫」。

          【角度】
          `a.angle` 与水面扫描线同源（`scanAngle()`），
          但这里用的是**屏幕角**而非地理方位角：
          天线指示器本就是「屏幕上的一根指针」，
          不参与地理投影，所以可以直接 rotate。
        -->
        <g
          v-for="a in radarAntennas"
          v-show="a.id === s.id"
          :key="'ant-' + a.id"
          class="ws-sen-antenna"
          :style="{
            transform: `translate(${a.pivotX}px, ${a.pivotY}px) rotate(${a.angle}deg)`,
            animationDuration: a.period + 's',
            animationDelay: '-' + a.phase + 's',
          }"
        >
          <!-- 波瓣：一条短的亮弧，表示天线此刻的指向 -->
          <line
            x1="0"
            y1="0"
            :x2="a.arm"
            y2="0"
            stroke="#9ff4ff"
            stroke-width="2"
            stroke-linecap="round"
            stroke-opacity="0.85"
          />
          <!-- 波瓣端点 -->
          <circle cx="0" cy="0" r="2.2" fill="#d9fbff" />
        </g>

        <!--
          ===== 光电云台朝向指示 =====

          【语义】
          光电转塔不是匀速自转，而是**盯着被跟踪的船**。
          这里画一小段镜头指向箭头，角度指向当前跟踪目标的屏幕方向，
          让观众一眼看出「转塔正在看哪艘船」。

          与视场锥（黄色）配合：
            · 视场锥 = 光电能看到的范围（大范围）；
            · 镜头箭头 = 云台此刻精确指向的方向（小范围）。
        -->
        <g
          v-if="eoTurret && eoTurret.id === s.id"
          class="ws-sen-eo-head"
          :class="{ 'is-tracking': eoTurret.tracking }"
          :style="{
            transform: `translate(${eoTurret.pivotX}px, ${eoTurret.pivotY}px) rotate(${eoTurret.angle}deg)`,
          }"
        >
          <!-- 镜头指向箭头 -->
          <path
            d="M 0 0 L 5 -3.4 L 2.4 0 L 5 3.4 Z"
            fill="#ffe9a8"
            stroke="#3a2c04"
            stroke-width="0.5"
          />
        </g>

        <!--
          ===== AIS 天线接收脉冲 =====

          AIS 是被动接收：天线顶端在收到报文时闪一下绿光。
          与绿色链路线的闪烁同源（周期 = AIS 报文周期）。
        -->
        <g
          v-for="a in aisAntennas"
          v-show="a.id === s.id"
          :key="'aisant-' + a.id"
          class="ws-sen-ais-antenna"
          :style="{ animationDuration: a.period + 's', animationDelay: '-' + a.phase + 's' }"
        >
          <circle :cx="a.tipX" :cy="a.tipY" r="2.6" fill="#8affc0" class="ws-sen-ais-antenna-dot" />
          <circle :cx="a.tipX" :cy="a.tipY" r="6" fill="none" stroke="#4fe07a" stroke-width="1" class="ws-sen-ais-antenna-ring" />
        </g>
      </g>

      <!-- 光晕（收敛到小范围，不再是 13px 的大光球） -->
      <circle :cx="s.cx" :cy="s.cy" r="9" fill="url(#senGlow)" class="ws-sen-glow" />
      <!-- 外环（雷达=青， AIS=绿，光电=黄） -->
      <circle
        :cx="s.cx"
        :cy="s.cy"
        r="5"
        :fill="s.kind === 'radar' ? '#4fd8ff' : s.kind === 'ais' ? '#4fe07a' : '#ffd54a'"
        :stroke="s.isSel ? '#ffffff' : '#04182a'"
        :stroke-width="s.isSel ? 2 : 1.4"
      />
      <!-- 内点 -->
      <circle :cx="s.cx" :cy="s.cy" r="1.8" fill="#ffffff" opacity="0.95" />
      <!-- 名称标签 -->
      <text
        :x="s.cx"
        :y="s.cy + 21"
        text-anchor="middle"
        :fill="s.kind === 'radar' ? '#a9e9ff' : s.kind === 'ais' ? '#a9f0c4' : '#ffe6a3'"
        font-size="11"
        class="ws-sen-label"
      >{{ s.name }}</text>
      <!-- 设备编号（与设备表里的 source_id 对应） -->
      <text
        v-if="s.sourceId"
        :x="s.cx"
        :y="s.cy + 33"
        text-anchor="middle"
        fill="#7fb6cc"
        fill-opacity="0.75"
        font-size="9.5"
        class="ws-sen-label"
      >{{ s.sourceId }}</text>
      <!-- 雷达方位标注（说明这是示意方位，不是实测安装角） -->
      <text
        v-if="s.kind === 'radar'"
        :x="s.cx"
        :y="s.cy + 46"
        text-anchor="middle"
        fill="#7fb6cc"
        fill-opacity="0.7"
        font-size="9.5"
        class="ws-sen-label"
      >扫描示意 · {{ Math.round(scanAngle(s.period, s.phase, s.bearing)) % 360 }}°</text>
    </g>
  </svg>
</template>

<style scoped>
.ws-sensors {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 2;
}

/* ===== 雷达 =====
 * 扫描线位置由「参考角度 + 经过时间 ÷ 周期」在 ENU 平面算出，
 * 这里**不再有 CSS rotate 动画** —— 屏幕上的旋转与地理方位角不是一回事。
 */

/* 扫尾：沿拖尾方向的「推进感」由渐变本身表达，这里只做轻微呼吸 */
.ws-sen-radar-trail {
  animation-name: ws-sen-trail-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 2.2s;
}

/* 扇面填充：极轻的呼吸，避免像贴纸 */
.ws-sen-radar-sector {
  animation-name: ws-sen-sector-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 3.4s;
}

/* ================================================================
 * 设备本体立牌（斜俯视贴图）
 * ================================================================ */

/*
 * 【为什么立牌不随水面斜面倾斜】
 * 素材是「固定 45° 斜俯视」的立牌，
 * 而本场景的底图也是固定斜俯视（`campus_oblique_v3.png`）。
 * 若把贴图放进 2.5D 倾斜组里，会被二次压扁，
 * 设备就会「躺倒」，与底图视角不一致。
 *
 * 因此贴图保持正立，只用底座锚点与地面位置对齐。
 */
.ws-sen-sprite {
  /* 轻微的环境光衰减，让设备不至于像贴纸一样突兀 */
  filter: drop-shadow(0 2px 3px rgba(0, 12, 20, 0.55));
  transform-box: fill-box;
  transform-origin: bottom center;
  /* 极缓慢的「呼吸」，让固定设备有一点点生气 */
  animation-name: ws-sen-sprite-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 5.5s;
}

/* 被选中时略微提亮 + 描边高光 */
.ws-sen-sprite.is-sel {
  filter: drop-shadow(0 2px 3px rgba(0, 12, 20, 0.55))
    drop-shadow(0 0 7px rgba(120, 240, 255, 0.85));
  animation-duration: 2.4s;
}

/* 接地阴影：让设备「站」在地面上 */
.ws-sen-sprite-shadow {
  animation-name: ws-sen-sprite-shadow;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 5.5s;
}

/* ================================================================
 * 雷达天线旋转指示
 * ================================================================ */

/*
 * 【角度】
 * transform 由模板内联给出（与水面扫描线同源的 scanAngle），
 * 所以这里**不能再加 CSS 动画的 rotate** ——
 * 否则会与内联 transform 打架（内联优先级更高，动画就失效了）。
 *
 * 因此只做**透明度呼吸**，旋转完全交给内联 transform。
 */
.ws-sen-antenna {
  transform-box: view-box;
  transform-origin: 0 0;
  animation-name: ws-sen-antenna-glow;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  /* filter 独立于 transform，不会与内联矩阵冲突 */
  filter: drop-shadow(0 0 4px rgba(140, 244, 255, 0.8));
}

@keyframes ws-sen-antenna-glow {
  0%,
  100% {
    opacity: 0.72;
  }
  50% {
    opacity: 1;
  }
}

/* ================================================================
 * 光电云台朝向指示
 * ================================================================ */

/*
 * 角度由模板内联给出（指向当前跟踪目标），
 * 因此这里只做 opacity 呼吸，不做 CSS rotate ——
 * 否则会与内联 transform 冲突。
 */
.ws-sen-eo-head {
  transform-box: view-box;
  transform-origin: 0 0;
  animation-name: ws-sen-eo-head-pulse;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 1.6s;
  filter: drop-shadow(0 0 5px rgba(255, 213, 74, 0.9));
}

/* 无跟踪目标时淡出（表示云台处于待机） */
.ws-sen-eo-head:not(.is-tracking) {
  opacity: 0.35;
}

@keyframes ws-sen-eo-head-pulse {
  0%,
  100% {
    opacity: 0.7;
  }
  50% {
    opacity: 1;
  }
}

/* ================================================================
 * AIS 天线接收脉冲
 * ================================================================ */

.ws-sen-ais-antenna-dot {
  animation-name: ws-sen-ais-dot-blink;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}

.ws-sen-ais-antenna-ring {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-sen-ais-antenna-ring;
  animation-timing-function: cubic-bezier(0.2, 0.6, 0.4, 1);
  animation-iteration-count: infinite;
}

@keyframes ws-sen-ais-dot-blink {
  0%,
  100% {
    opacity: 0.25;
  }
  12% {
    opacity: 1;
  }
  40% {
    opacity: 0.5;
  }
}

@keyframes ws-sen-ais-antenna-ring {
  0% {
    transform: scale(0.4);
    opacity: 0.8;
  }
  10% {
    opacity: 0.6;
  }
  55% {
    transform: scale(1.7);
    opacity: 0;
  }
  100% {
    transform: scale(1.7);
    opacity: 0;
  }
}

@keyframes ws-sen-sprite-breathe {
  0%,
  100% {
    opacity: 0.97;
  }
  50% {
    opacity: 1;
  }
}

@keyframes ws-sen-sprite-shadow {
  0%,
  100% {
    opacity: 0.4;
  }
  50% {
    opacity: 0.52;
  }
}

/* ================================================================
 * 雷达回波脉冲（与扫描线同步）
 * ================================================================
 *
 * 【相位锚定】
 * 每个目标的动画 `animation-delay` 由 `b.progress * b.cycle` 给出，
 * 其中 `progress` 是「扫描角落后目标方位角」的比例。
 * 这样回波峰值**恰好落在扫描线经过该目标的瞬间**。
 *
 * 【一个扫描周期内的节拍】
 *   0%    —— 扫描线刚扫过：回波最强（亮点最亮、环最大）
 *   8%    —— 回波环扩散到最大并开始消隐
 *   10%~95% —— 余波期，亮点快速暗下，环消失
 *   100%  —— 下一圈即将扫到，亮度回升
 */

/* 回波扩散环：从目标向外扩散并消隐 */
.ws-sen-radar-echo-ring {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-radar-echo-ring;
  animation-timing-function: cubic-bezier(0.15, 0.7, 0.35, 1);
  animation-iteration-count: infinite;
}

/* 回波核心亮点：被扫到的瞬间最亮，随后快速衰减 */
.ws-sen-radar-echo-dot {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-radar-echo-dot;
  animation-timing-function: ease-out;
  animation-iteration-count: infinite;
}

/* 回波引线：只在扫描经过时短暂显现 */
.ws-sen-radar-echo-line {
  animation-name: ws-radar-echo-line;
  animation-timing-function: ease-out;
  animation-iteration-count: infinite;
}

@keyframes ws-radar-echo-ring {
  0% {
    transform: scale(0.35);
    opacity: 0;
  }
  2% {
    opacity: 0.9;
  }
  9% {
    transform: scale(2.4);
    opacity: 0;
  }
  100% {
    transform: scale(2.4);
    opacity: 0;
  }
}

@keyframes ws-radar-echo-dot {
  0% {
    transform: scale(1.5);
    opacity: 1;
  }
  6% {
    transform: scale(1);
    opacity: 0.95;
  }
  14% {
    transform: scale(0.7);
    opacity: 0.28;
  }
  100% {
    transform: scale(0.7);
    opacity: 0.28;
  }
}

@keyframes ws-radar-echo-line {
  0% {
    opacity: 0.55;
    stroke-width: 1.2;
  }
  8% {
    opacity: 0.22;
    stroke-width: 0.9;
  }
  100% {
    opacity: 0.22;
    stroke-width: 0.9;
  }
}

/* AIS 接收更新提示：小幅脉冲 */
.ws-sen-ais-pulse {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-sen-pulse;
  animation-timing-function: cubic-bezier(0.2, 0.6, 0.4, 1);
  animation-iteration-count: infinite;
}

/* ================================================================
 * 动态 AIS 链路
 * ================================================================ */

/* 断续连线的呼吸 —— 表示「链路在线」 */
.ws-ais-link-base {
  animation-name: ws-ais-base-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 2.6s;
}

/*
  报文流动光点。
  【方向】船在广播、岸台在接收 → 能量方向是「目标 → 岸台」。
  路径 M 起点是岸台、终点是目标，
  所以要让光点沿路径**反向**跑 → `animation-direction: reverse`。
*/
.ws-ais-link-flow {
  animation-name: ws-ais-flow;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  animation-direction: reverse;
}

/* 目标端报文框：轻微闪烁，模拟报文更新 */
.ws-ais-target {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-ais-target-blink;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 2.6s;
}

@keyframes ws-ais-base-breathe {
  0%,
  100% {
    opacity: 0.18;
  }
  50% {
    opacity: 0.52;
  }
}

@keyframes ws-ais-flow {
  from {
    stroke-dashoffset: 27;
  }
  to {
    stroke-dashoffset: 0;
  }
}

@keyframes ws-ais-target-blink {
  0%,
  100% {
    opacity: 0.72;
  }
  50% {
    opacity: 1;
  }
}

/* ================================================================
 * 光电跟踪
 * ================================================================ */

/* 视场锥：轻微明暗起伏，像镜头在调整增益 */
.ws-sen-eo-cone {
  animation-name: ws-eo-cone-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 2.8s;
}

/* 视线：虚线缓慢流动，表示持续凝视 */
.ws-sen-eo-sight {
  animation-name: ws-eo-sight-flow;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  animation-duration: 1.8s;
}

/* 跟踪框：四角标记轻微收放（模拟伺服微调） */
.ws-sen-eo-lock {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-eo-lock-pulse;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 1.4s;
}

@keyframes ws-eo-cone-breathe {
  0%,
  100% {
    opacity: 0.62;
  }
  50% {
    opacity: 1;
  }
}

@keyframes ws-eo-sight-flow {
  from {
    stroke-dashoffset: 7;
  }
  to {
    stroke-dashoffset: 0;
  }
}

@keyframes ws-eo-lock-pulse {
  0%,
  100% {
    transform: scale(0.965);
    opacity: 0.78;
  }
  50% {
    transform: scale(1.035);
    opacity: 1;
  }
}

/* 传感器本体光晕：呼吸（幅度收敛） */
.ws-sen-glow {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-sen-breathe;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 3s;
}

/* 十字分划：轻微旋转（模拟测角仪） */
.ws-sen-eo-reticle {
  transform-box: fill-box;
  transform-origin: center;
  animation-name: ws-sen-reticle;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-duration: 6s;
}

@keyframes ws-sen-trail-breathe {
  0%,
  100% {
    opacity: 0.7;
  }
  50% {
    opacity: 1;
  }
}

@keyframes ws-sen-sector-breathe {
  0%,
  100% {
    opacity: 0.7;
  }
  50% {
    opacity: 1;
  }
}

@keyframes ws-sen-pulse {
  0%,
  100% {
    opacity: 0.18;
    transform: scale(0.82);
  }
  25% {
    opacity: 0.65;
    transform: scale(1);
  }
}

@keyframes ws-sen-breathe {
  0%,
  100% {
    opacity: 0.3;
  }
  50% {
    opacity: 0.7;
  }
}

@keyframes ws-sen-reticle {
  0%,
  100% {
    transform: rotate(0deg);
  }
  50% {
    transform: rotate(45deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ws-sen-radar-trail,
  .ws-sen-radar-sector,
  .ws-sen-radar-echo-ring,
  .ws-sen-radar-echo-dot,
  .ws-sen-radar-echo-line,
  .ws-sen-ais-pulse,
  .ws-sen-glow,
  .ws-sen-eo-reticle,
  .ws-sen-sprite,
  .ws-sen-sprite-shadow,
  .ws-sen-antenna,
  .ws-sen-eo-head,
  .ws-sen-ais-antenna-dot,
  .ws-sen-ais-antenna-ring,
  .ws-ais-link-base,
  .ws-ais-link-flow,
  .ws-ais-target,
  .ws-sen-eo-cone,
  .ws-sen-eo-sight,
  .ws-sen-eo-lock {
    animation: none !important;
  }
}
</style>
