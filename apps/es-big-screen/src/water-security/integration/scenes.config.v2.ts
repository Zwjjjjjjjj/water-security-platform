/**
 * V2 场景素材接入配置（写实航拍正俯视风格）
 *
 * 与 V1 的差异：
 *  - V1 是「手绘矢量底图 + 45° 立牌」的 2.5D 风格；
 *  - V2 是「**一张写实航拍底图铺满整个视口 + 正俯视实体贴图**」的 2D 风格，
 *    对应用户提供的参考图（核电厂航拍俯视 + 真实海水 + 石砌防波堤）。
 *
 * 【与 V1 的类型兼容】
 * 本文件的类型是 V1 的**超集**（多出若干可选字段），
 * 这样 ChartSceneV2 可以复用 V1 的全部渲染代码，
 * 而 V1 的既有行为不受任何影响。
 *
 * 【坐标约定】
 * 设计坐标系：原点在底图左上，x 向右（东），y 向下（南），范围 1000×500。
 *   mE = BG_ANCHOR.e + dx * BG_UNIT.e
 *   mN = BG_ANCHOR.n - dy * BG_UNIT.n
 * 与 V1 完全一致，实测航迹数据无需改动即可复用。
 */

/** 底图锚定参数（米制），与 V1 相同 */
export const BG_ANCHOR = { e: -2200, n: 4900 }
/** 每设计单位对应的米数（东向 / 北向） */
export const BG_UNIT = { e: 3.4, n: 3.8 }

/** V2 素材 id（与 scenes-v2/manifest.json 对应） */
export const V2_ASSET = {
  campusAerial: 'campus_aerial',
  campusOblique: 'campus_oblique',
  seaSurface: 'sea_surface',
  seaNormal: 'sea_normal',
  breakwaterTex: 'breakwater_tex',
  shorelineTex: 'shoreline_tex',
  jettyTex: 'jetty_tex',
  shipWake: 'ship_wake',
  steamPlume: 'steam_plume',
  radarStation: 'radar_station',
  navBeacon: 'nav_beacon',
} as const

/**
 * 固定斜俯视模式的配置。
 *
 * 【重要 —— 配准状态】
 * `campus_oblique.png` 是概念场景素材（图像生成工具产出），
 * **没有已核验的 ENU 控制点**，无法与实测航迹做真实地理配准。
 *
 * 因此这里采用「**示意局部坐标系**」：
 *   - 背景：图片原生像素坐标（0..1536, 0..1024）
 *   - ENU 点位：通过 `enuToImage` 映射到该坐标系
 *
 * 界面必须标注「场景示意」，
 * 不能据此读取真实岸线距离或宣称已地理配准。
 *
 * 【结构】
 * `enuToImage` 是一个 **仿射变换**（2×3 矩阵）：
 *   [x_img]   [a c e] [E_m]
 *   [y_img] = [b d f] [N_m]
 *   [  1  ]   [0 0 1] [ 1 ]
 *
 * 【参考窗口】
 * 斜视图是一张固定的概念场景图，**不应随视口缩放而改变它在图上的位置**。
 * 因此这里定义一个固定的「参考 ENU 窗口」，
 * 该窗口恰好对应图片的**有效内容区**：
 *
 *   图片内容区：x ∈ [0, 1536]（整幅宽度都是场景）
 *                y ∈ [40, 1000]（上方 40px 是天空/远山，下方是海面）
 *
 *   参考 ENU 窗口：E ∈ [-2600, 1000]（3600m）
 *                  N ∈ [2900, 5100]（2200m）
 *
 * 取景时若视口正好等于参考窗口，图片就**恰好铺满绘图区**，
 * 厂区落在画面上部、海面在下部（与素材包 preview 的目标布局一致）。
 */
export const OBLIQUE_FIT = {
  /** 参考 ENU 窗口（米）——图片内容区所对应的示意米制范围 */
  ref: {
    eMin: -2600,
    eMax: 1000,
    nMin: 2900,
    nMax: 5100,
  },
  /** 参考窗口对应的图片内容区（原生像素） */
  img: {
    x0: 0,
    y0: 40,
    x1: 1536,
    y1: 1000,
  },
  /** ENU → 图片像素的仿射系数 */
  get a() {
    return (this.img.x1 - this.img.x0) / (this.ref.eMax - this.ref.eMin)
  },
  b: 0,
  c: 0,
  get d() {
    return -(this.img.y1 - this.img.y0) / (this.ref.nMax - this.ref.nMin)
  },
  /** 平移：使参考窗口的左上角落在图片内容区左上角 */
  get e() {
    return this.img.x0 - this.a * this.ref.eMin
  },
  get f() {
    return this.img.y0 - this.d * this.ref.nMax
  },
}

/** ENU 米制 → 斜视图片像素（示意局部坐标系） */
export function enuToImage(e: number, n: number): [number, number] {
  return [OBLIQUE_FIT.a * e + OBLIQUE_FIT.c * n + OBLIQUE_FIT.e, OBLIQUE_FIT.b * e + OBLIQUE_FIT.d * n + OBLIQUE_FIT.f]
}

/** 斜视图片像素 → ENU 米制（逆向映射，用于点击拾取） */
export function imageToEnu(x: number, y: number): [number, number] {
  const det = OBLIQUE_FIT.a * OBLIQUE_FIT.d - OBLIQUE_FIT.b * OBLIQUE_FIT.c
  if (Math.abs(det) < 1e-9) return [0, 0]
  const dx = x - OBLIQUE_FIT.e
  const dy = y - OBLIQUE_FIT.f
  return [(OBLIQUE_FIT.d * dx - OBLIQUE_FIT.c * dy) / det, (-OBLIQUE_FIT.b * dx + OBLIQUE_FIT.a * dy) / det]
}

/**
 * 斜视模式下需要关闭的旧内容。
 *
 * 背景图里已经烘焙了山体、厂区、岸线、码头、防波堤和静态小设施，
 * 再叠一遍旧版矢量陆地 / 建筑 / 防波堤 / 灯杆就会「重复出现」。
 *
 * 保留：动态目标、源点迹、航迹、标签、交互层。
 */
export const OBLIQUE_SUPPRESS = {
  /** 关闭正俯视底图（SceneBackgroundV2） */
  topBackground: true,
  /** 关闭旧建筑贴图层 */
  nppBuildings: true,
  /** 关闭旧防波堤/护岸/明渠层 */
  harborStructures: true,
  /** 关闭旧灯杆/浮标 */
  verticalProps: true,
  /** 关闭 2.5D 倾斜（斜视背景已烘焙投影，再倾斜会二次压缩） */
  tilt: true,
  /** 关闭背景船（斜视模式只保留实测目标船） */
  backgroundShips: true,
}

export interface NppPlacement {
  /** 素材 id（2D 俯视地面平面用） */
  id: string
  /** 2.5D 立牌用素材 id；V2 为纯 2D，留空 */
  id25d?: string
  /** 地面锚点（设计坐标，底图坐标系；y 向下） */
  dx: number
  dy: number
  /** 显示尺寸（设计单位），指 alpha 轮廓的实际占地 */
  w: number
  h: number
  /** 标签文本 */
  label?: string
  /** 标签相对锚点的偏移（设计单位） */
  labelDx?: number
  labelDy?: number
  /** 是否默认显示 */
  enabled?: boolean
  /** 说明：贴图语义来源 */
  note?: string
  /** 2.5D 落地锚点（V2 未使用，保留以兼容类型） */
  base?: [number, number]
  /** 2.5D 地坪对角线长度（V2 未使用） */
  plateW?: number
  /** 2.5D 基座高度比例（V2 未使用） */
  massH?: number
  /** 基座顶面配色 */
  massFill?: string
  /** 基座侧面配色 */
  massSide?: string
}

/**
 * 厂区建筑放置表（V2）。
 *
 * 【重要】当 `campus_aerial`（写实厂区航拍图）到位后，
 * 厂区里的建筑**已经在底图里了**，此处这些单体贴图应当默认关闭，
 * 否则会出现「写实底图上又叠一套贴图建筑」的违和感。
 *
 * 保留这些配置是为了：素材没到位时仍有建筑可看（回退方案）。
 */
export const NPP_PLACEMENTS: NppPlacement[] = [
  /* 厂区关键构筑物。
   *
   * 参考图里厂区是一整块写实航拍底图，核岛（安全壳）虽可见，
   * 但不需要再单独贴一个「核岛」贴图 —— 只保留文字标注即可。
   * 下面默认关闭，作用是素材未到位时作为回退。
   */
  {
    id: 'nuclear_island_top',
    dx: 300, dy: 118, w: 92, h: 92, label: '核岛', enabled: false,
    note: '写实底图到位后由底图自带，此处仅作标注锚点',
  },
  {
    id: 'conventional_island_top',
    dx: 470, dy: 112, w: 136, h: 136, label: '常规岛厂房', enabled: false,
  },
  {
    id: 'cooling_tower_top',
    dx: 660, dy: 110, w: 66, h: 66, label: '冷却塔', enabled: false,
  },
  {
    id: 'switchyard_top',
    dx: 770, dy: 112, w: 68, h: 68, label: '开关站', enabled: false,
  },
  {
    id: 'power_pylon_top',
    dx: 886, dy: 102, w: 32, h: 32, label: '输电铁塔', enabled: false,
  },
  {
    id: 'chimney_top',
    dx: 938, dy: 98, w: 24, h: 24, label: '烟囱', enabled: false,
  },
]

export interface BackgroundShip {
  /** 船型素材 id */
  id: string
  /** 锚点（设计坐标） */
  dx: number
  dy: number
  /** 航向（度，0=正北，顺时针） */
  course: number
  /** 显示船长（米）；留空则用素材的示意模板尺寸 */
  lengthM?: number
  /** 不透明度 */
  opacity?: number
}

/**
 * 背景船只（氛围元素，不参与交互与量测）。
 * 位置参照参考图：海面上零星几条作业船，与实测目标 T001/T004 错开。
 */
export const BACKGROUND_SHIPS: BackgroundShip[] = [
  { id: 'container_160m_top', dx: 232, dy: 268, course: 24, lengthM: 160 },
  { id: 'tanker_140m_top', dx: 706, dy: 352, course: -18, lengthM: 140 },
  { id: 'bulk_180m_top', dx: 862, dy: 212, course: 46, lengthM: 180 },
  { id: 'tug_32m_top', dx: 138, dy: 372, course: 78, lengthM: 32 },
  { id: 'fishing_24m_top', dx: 418, dy: 436, course: -40, lengthM: 24 },
  { id: 'engineering_60m_top', dx: 588, dy: 414, course: 10, lengthM: 60 },
]

export interface HarborVectorPlacement {
  /** 素材 id */
  id: string
  /** 锚点（设计坐标） */
  dx: number
  dy: number
  /** 显示尺寸（设计单位） */
  w: number
  h: number
  /** 不透明度 */
  opacity: number
  /** 是否默认显示 */
  enabled?: boolean
}

/**
 * 港区矢量结构放置表（V2）。
 * V2 改用「写实贴图」，矢量结构只作为素材缺失时的回退方案。
 */
export const HARBOR_VECTOR_PLACEMENTS: HarborVectorPlacement[] = [
  { id: 'channel_lines', dx: 470, dy: 180, w: 512, h: 320, opacity: 0.5, enabled: false },
  { id: 'berth_lines', dx: 490, dy: 140, w: 280, h: 84, opacity: 0.62, enabled: false },
]

/**
 * V2 场景标签（写实航拍风格）。
 *
 * 参考图里只有两处明显标签：
 *   - 厂区上方「核电厂区（示意）」的深色圆角胶囊
 *   - 右侧取水口旁「取水口」
 * 都是「深底 + 细边 + 白字」，不是彩色小圆点。
 */
export interface SceneLabel {
  text: string
  dx: number
  dy: number
  style?: 'chip' | 'plain'
  group?: 'factory' | 'intake' | 'berth' | 'chart'
  size?: number
  opacity?: number
}

export const V2_SCENE_LABELS: SceneLabel[] = [
  { text: '核电厂区（示意）', dx: 372, dy: 56, style: 'chip', group: 'factory', size: 12 },
  { text: '取水口', dx: 566, dy: 158, style: 'chip', group: 'intake', size: 12 },
  { text: '排放口', dx: 706, dy: 172, style: 'chip', group: 'intake', size: 12 },
  { text: '西侧堤', dx: 186, dy: 300, style: 'plain', group: 'chart', size: 11, opacity: 0.55 },
  { text: '警戒浮标', dx: 96, dy: 396, style: 'plain', group: 'chart', size: 11, opacity: 0.45 },
  { text: '禁航区连线', dx: 452, dy: 470, style: 'plain', group: 'chart', size: 11, opacity: 0.45 },
]

/**
 * 固定斜视背景（`campus_oblique_v3.png`）专用的场景标签。
 *
 * 【为什么要单独一套】
 * 正俯视模式用 `designToMeter()` 把设计坐标换算成 ENU 再投影；
 * 但斜视背景是**已经烘焙了透视**的概念图，
 * 厂区/码头在图上的位置与 ENU 平面**没有对应关系**。
 * 若沿用 ENU 中转，文字就会与背景里的建筑错位。
 *
 * 所以斜视模式的标签直接用**图片原生像素坐标**（1536×1024）定位，
 * 与背景图处于同一套图像局部坐标系 ——
 * 缩放/平移时按同一比例变换，文字始终贴住背景里的对应物。
 *
 * 【坐标来源 —— 连通域分析逐像素核验过】
 * 对 `森林海岸工业港湾.png`（1536×1024）做连通域 + 颜色特征分析得到：
 *
 *   · 大型白色罐区（储罐群）
 *       中心 (1113,421)，范围 x[1032,1181] y[401,439]
 *   · 主厂房（灰白大型厂房群）
 *       中心 (660,400)
 *   · 变电/开关站
 *       中心 (627,289)，范围 x[589,654] y[281,303]
 *   · 码头/栈桥（灰白长条斜向构筑物）
 *       中心 (1092,623)，范围 x[944,1211] y[508,694]
 *   · 防波堤（三段灰白斜向堤体，沿岸线排布）
 *       (883,698) → (793,716) → (731,729)
 *   · 红顶厂房链
 *       (628,360) → (635,392) → (646,484)
 *   · 林地山体（绿色茂密区）
 *       (276,334) 范围 x[0,812] y[120,551]；(986,273) 范围 x[675,1379] y[129,404]
 *   · 开阔水域
 *       厂前水域 (700,850)、外海域 (1400,900)
 *
 * 标签锚点即取上述实测位置，并按阅读习惯加一点偏移，
 * 保证文字落在设施**旁边**而不是压在屋顶上。
 */
export interface ObliqueSceneLabel {
  text: string
  /** 图片原生像素 X（0 ~ 1536） */
  px: number
  /** 图片原生像素 Y（0 ~ 1024） */
  py: number
  style?: 'chip' | 'plain'
  /** 文字相对锚点的像素偏移 */
  offsetX?: number
  offsetY?: number
  size?: number
  opacity?: number
  /** 优先级：数字越小越优先占位（避让时先安置） */
  priority?: number
}

/**
 * 【字号说明】
 * 用户要求「水上模式的那些字需要放大」。
 *   · chip（设施标注：厂区、取水口、排放口）12 → 15px
 *   · plain（构筑物与区域名）11 → 14px
 *
 * 字号放大后，标签的**占位也随之变大**，
 * 因此偏移量（offsetX/offsetY）同步放大，
 * 避免文字压在被标注的设施本体上。
 */
export const OBLIQUE_SCENE_LABELS: ObliqueSceneLabel[] = [
  // —— 厂区主标注：贴在厂区建筑群上方 ——
  { text: '工业港湾厂区（示意）', px: 770, py: 300, style: 'chip', size: 15, priority: 0, offsetY: -24 },
  // —— 取排水口：位于厂区临海侧的岸线附近 ——
  { text: '取水口', px: 1027, py: 408, style: 'chip', size: 15, priority: 1, offsetX: 52 },
  { text: '排放口', px: 1369, py: 281, style: 'chip', size: 15, priority: 1, offsetX: 36 },
  // —— 关键构筑物（锚点即实测连通域中心）——
  { text: '罐区', px: 1113, py: 421, style: 'plain', size: 14, opacity: 0.9, priority: 2, offsetX: 58, offsetY: -7 },
  { text: '主厂房', px: 660, py: 400, style: 'plain', size: 14, opacity: 0.85, priority: 2, offsetX: -62, offsetY: 5 },
  { text: '开关站', px: 627, py: 289, style: 'plain', size: 14, opacity: 0.85, priority: 2, offsetX: -58, offsetY: -7 },
  { text: '厂区码头', px: 1092, py: 623, style: 'plain', size: 14, opacity: 0.8, priority: 2, offsetX: 46, offsetY: 12 },
  // —— 区域名（最弱层）——
  { text: '北侧林地', px: 276, py: 334, style: 'plain', size: 14, opacity: 0.5, priority: 3 },
  { text: '厂前水域', px: 700, py: 850, style: 'plain', size: 14, opacity: 0.5, priority: 3 },
  { text: '外海域', px: 1400, py: 900, style: 'plain', size: 14, opacity: 0.45, priority: 3 },
  { text: '进港航路', px: 1220, py: 760, style: 'plain', size: 14, opacity: 0.45, priority: 3 },
]
