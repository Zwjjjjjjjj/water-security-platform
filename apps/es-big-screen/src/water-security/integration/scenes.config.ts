/**
 * 场景素材接入配置
 *
 * 重要约束（来自素材包 README + 实测复核）：
 * - 素材为「示意」概念美术资产，非真实阳江核电站航拍/测绘成果，无真实经纬度；
 * - 背景与建筑的 ENU 框由本配置显式指定，标记为「示意配准」；
 * - 45° (iso) 图片是固定视角渲染，不是真实三维模型，
 *   不得当作地面平面压扁摆放，只可作为竖直展示牌单独摆位；
 * - `harbor_basin_top.png` / `npp_campus_top.png` 经像素复核实为 3/4 透视场景图
 *   （可见建筑立面与透视收缩岸线），不能作为正投影地面平面贴图，已弃用；
 * - 港区结构改用素材包的原生矢量 SVG（正投影，可直接作地面平面）；
 * - 冷却塔/烟囱/蒸汽为通用工业件，不代表真实设施，默认关闭；
 * - 船型 length_m/beam_m 为示意模板尺寸，非数据集目标真实船长。
 *
 * 【坐标约定】
 * 设计坐标系：原点在底图左上，x 向右（东），y 向下（南），范围 1000×500。
 * 陆域位于北侧（y < 岸线），水域位于南侧（y > 岸线）。
 * dx/dy 是建筑「地面锚点」（建筑站立点），不是建筑中心。
 */

export interface NppPlacement {
  /** 素材 id（2D 俯视地面平面用） */
  id: string
  /** 2.5D 立牌用素材 id（45° 固定视角，README 指定用于竖直展示牌）；留空则复用 id */
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
  /**
   * 2.5D 素材的「落地锚点」（素材像素坐标）。
   *
   * 45° 固定视角素材的结构是「建筑 + 一块菱形地坪」，
   * 整张图的底边尖角只是菱形地坪的一个角，并非建筑的落地位置。
   * 若把锚点取在底边尖角上，建筑会整体浮在地坪之上（悬空感）。
   * 正确做法是取「菱形地坪的中心」——即建筑在平面图上的实际占地中心。
   *
   * 数值由素材 alpha 轮廓逐行分析得出（菱形最宽行与底尖的行号推算）。
   */
  base?: [number, number]
  /**
   * 菱形地坪的水平对角线长度（素材像素）。
   * 用于把素材整体缩放到「设计单位占地 w」，保证平面占地与屏幕尺寸一致。
   */
  plateW?: number
  /**
   * 2.5D 基座体高度（占素材显示高度的比例）。
   *
   * 45° 素材自带立面，但底部与地面之间没有实体过渡，
   * 看上去像「贴纸浮在图上」。这里在素材下方补一段棱台基座，
   * 顶端与素材底边重合、底端落在地面上，
   * 让建筑真正「长」在地面上。
   */
  massH?: number
  /** 基座顶面配色 */
  massFill?: string
  /** 基座侧面（暗面）配色 */
  massSide?: string
}

/**
 * 核电站建筑放置表。
 *
 * - 2D：正俯视单体平铺在地面平面（与航迹同一 ENU 坐标系）
 * - 2.5D：改用 45° 版本立起来，底部锚定在地面点（README 指定用法）
 *
 * dx/dy 为建筑地面锚点（设计坐标），w/h 为 alpha 轮廓的设计单位占地尺寸。
 * 全部锚点均落在陆域（岸线以北）内，且彼此留有间距，避免立牌互相遮挡。
 */
export const NPP_PLACEMENTS: NppPlacement[] = [
  /* —— 主厂房区：核岛 / 常规岛 / 开关站 ——
   *
   * 比例依据（换算成真实尺度，避免「一个比三个还大」）：
   *   设计单位 1 = 3.4 米（BG_UNIT.e）
   *   常规岛厂房（长条形主厂房）  ~320 米 → w 94
   *   核岛（反应堆厂房 + 安全壳）  ~200 米 → w 59
   *   开关站（中小型构架）          ~150 米 → w 44
   *   输电铁塔（细高塔）            ~60 米 → w 18
   * 高度 h 取素材自身比例（iso 素材按菱形地坪等比缩放），
   * 不再强行压扁，否则「屋顶变矮、厂房变胖」看着极不自然。
   *
   * 锚点全部落在陆域内（岸线约 y≈150~180），建筑之间留出道路间隔。
   */
  /* 锚点说明：岸线在设计 y ≈ 150~180 之间，
   * 建筑锚点取「可见轮廓底边中点」，故 dy 应比建筑实际中心再靠北一些，
   * 使建筑整体落在陆域内、不越岸。 */
  // 核岛：西侧主厂房群（反应堆厂房 + 安全壳）
  {
    id: 'nuclear_island_top', id25d: 'nuclear_island_iso',
    dx: 286, dy: 128, w: 96, h: 96, label: '核岛', enabled: true,
    base: [641, 859], plateW: 1067,
    massH: 0.42, massFill: '#3a4d5a', massSide: '#1c2a34',
  },
  // 常规岛厂房：厂区中部，体量最大（长条形主厂房）
  {
    id: 'conventional_island_top', id25d: 'conventional_island_iso',
    dx: 476, dy: 120, w: 142, h: 142, label: '常规岛厂房', enabled: true,
    base: [633, 711], plateW: 1170,
    massH: 0.36, massFill: '#3d5260', massSide: '#1e2d38',
  },
  /* 冷却塔：核电站最显著的竖向体量。
   *
   * 【为什么重新启用】
   * 之前为了「画面简洁」把它关掉了，
   * 但冷却塔恰恰是整个厂区**最高的构筑物** ——
   * 有它在，厂区才有明确的垂直尺度参照，
   * 建筑与海堤才显得「有高度」而不是贴在图上。
   *
   * 尺寸换算：w 70 设计单位 ≈ 238m（冷却塔筒体直径约 100m，
   * 含水雾区总宽接近 200m+），与核电站实际尺度相符。 */
  {
    id: 'cooling_tower_top', id25d: 'cooling_tower_iso',
    dx: 676, dy: 118, w: 70, h: 70, label: '冷却塔', enabled: true, note: '通用工业件，示意配准',
    base: [627, 1216], plateW: 887,
    massH: 0.3, massFill: '#425665', massSide: '#202e38',
  },
  // 开关站：厂区东侧
  {
    id: 'switchyard_top', id25d: 'switchyard_iso',
    dx: 778, dy: 118, w: 72, h: 72, label: '开关站', enabled: true,
    base: [606, 768], plateW: 1061,
    massH: 0.3, massFill: '#354857', massSide: '#1a2732',
  },
  // 输电铁塔：厂区最东端，细高
  {
    id: 'power_pylon_top', id25d: 'power_pylon_iso',
    dx: 894, dy: 108, w: 34, h: 34, label: '输电铁塔', enabled: true,
    base: [619, 735], plateW: 585,
    massH: 0.22, massFill: '#2c3d49', massSide: '#18242e',
  },
  /* 烟囱：第二竖向体量，与冷却塔形成高低呼应。
   * w 26 设计单位 ≈ 88m 筒高，是厂区最高的细长构筑物。 */
  {
    id: 'chimney_top', id25d: 'chimney_iso',
    dx: 946, dy: 104, w: 26, h: 26, label: '烟囱', enabled: true, note: '通用工业件，示意配准',
    base: [627, 1209], plateW: 268,
    massH: 0.26, massFill: '#394b58', massSide: '#1b2831',
  },
  // 储罐：罐区，低矮体量，用于填补厂区东侧的空隙
  {
    id: 'storage_tank_iso',
    dx: 908, dy: 132, w: 40, h: 40, label: '储罐', enabled: true, note: '45°固定视角，竖直展示',
    base: [626, 1153], plateW: 954,
    massH: 0.24, massFill: '#3c4f5c', massSide: '#1e2c35',
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
 * 使用素材包的正俯视船型 PNG，与实测目标船体共用同一套放置算法，
 * 保证风格统一且都落在同一 ENU 框架内。
 *
 * 锚点全部选在水域，且刻意避开取排水明渠与实测目标 T001/T002/T004 的位置。
 */
export const BACKGROUND_SHIPS: BackgroundShip[] = [
  // 船长按素材示意模板尺寸（米）显示，与实测目标船体同一量级
  { id: 'container_160m_top', dx: 250, dy: 250, course: 28, lengthM: 160 },
  { id: 'tanker_140m_top', dx: 700, dy: 345, course: -15, lengthM: 140 },
  { id: 'bulk_180m_top', dx: 880, dy: 200, course: 42, lengthM: 180 },
  { id: 'tug_32m_top', dx: 150, dy: 355, course: 75, lengthM: 32 },
  { id: 'fishing_24m_top', dx: 430, dy: 425, course: -38, lengthM: 24 },
  { id: 'engineering_60m_top', dx: 600, dy: 405, course: 12, lengthM: 60 },
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
 * 港区矢量结构放置表。
 * 使用素材包原生 SVG（正投影，无地理信息，需按示意 ENU 框摆放）。
 * 与手绘底图中的长堤 / 航道 / 泊位位置对齐，避免两套结构并存打架。
 */
export const HARBOR_VECTOR_PLACEMENTS: HarborVectorPlacement[] = [
  // 原港区矢量素材（主航道 channel_lines / 取排水泊位 berth_lines）：
  // 这两套是为港区布局设计的，套到核电站厂区后会与底图岸线、厂区形态打架
  // （大航道带横穿陆域、泊位框与核电厂取排水口形态不符），故默认关闭。
  //
  // 核电厂自身的取排水系统（取水明渠 + 排水暗渠 + 取排水口）
  // 已改为直接绘制在底图 SVG 内（见 ChartScene.vue 的「取排水系统」分组），
  // 与底图共享同一设计坐标系，缩放/平移时自动跟随，不会错位。
  { id: 'channel_lines', dx: 470, dy: 180, w: 512, h: 320, opacity: 0.5, enabled: false },
  { id: 'berth_lines', dx: 490, dy: 140, w: 280, h: 84, opacity: 0.62, enabled: false },
]
