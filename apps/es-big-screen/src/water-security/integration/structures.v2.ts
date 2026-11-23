/**
 * V2 构筑物几何（正俯视 2D 风格）
 *
 * 与 V1 的差异：
 *  - V1 是 2.5D 立体体量（有顶面 + 受光侧壁 + 阴影），服务于倾斜视角；
 *  - V2 是**纯正俯视**，因此构筑物不需要「高度」，
 *    只需要平面走向 + 平面宽度 + 贴图样式。
 *
 * 【类型兼容】
 * 本文件的类型是 V1 的**超集**：V1 的字段（height / top / side / litFill 等）
 * 全部保留，只是 V2 用不上；这样 ChartSceneV2 可复用 V1 的渲染代码。
 *
 * 【为什么保留矢量回退】
 * 目标是写实航拍风格，防波堤、护岸最终会用真实贴图（breakwater_tex）。
 * 但素材到位前，需要一套矢量绘制保证画面完整；
 * 素材到位后，矢量层自动让位给贴图层。
 *
 * 坐标系：设计坐标（dx ∈ [0,1000]、dy ∈ [0,500]，y 向下），与底图完全一致。
 */

/** 构筑物类型 */
export type StructureKind =
  | 'breakwater'
  | 'wall'
  | 'channel'
  | 'pier'
  | 'beacon'
  | 'platform'
  | 'jetty'
  | 'revetment'

export interface StructureSegment {
  /** 类型 */
  kind: StructureKind
  /** 设计坐标折线（中心线） */
  pts: [number, number][]
  /** 顶面宽度（设计单位） */
  width: number
  /** 高度（V2 正俯视不用，保留以兼容类型） */
  height: number
  /** 顶面颜色（V2 用作填充色） */
  top: string
  /** 侧面颜色（V2 用作描边色） */
  side: string
  /** 边缘高光 */
  edge?: string
  /** 受光立面颜色 */
  litFill?: string
  /** 朝向观察者的一侧是否为受光面（V2 未使用） */
  litSouth?: boolean
  /** 不透明度 */
  opacity?: number
  /** 贴图 id（素材到位时用贴图替代矢量填充） */
  texId?: string
  /** 描边色（V2 矢量回退用；兼容 V1 类型） */
  stroke?: string
  /** 描边宽度（设计单位） */
  strokeW?: number
  /** 填充色别名（V2 矢量回退用；等于 top） */
  fill?: string
}

/**
 * 光源方位（V2 正俯视不使用，保留以兼容类型）。
 * 取 (-0.72, +0.69)（设计坐标，x 向东为正、y 向南为正）。
 */
export const SUN_DIR: [number, number] = [-0.72, 0.69]

/**
 * 防波堤与护岸。
 *
 * 对应参考图：
 *   - 左下角一段弧形防波堤（石砌外坡 + 堤顶步道）
 *   - 右下角一段弧形防波堤
 *   - 厂区岸线沿线的一整段石砌护岸
 */
export const STRUCTURES: StructureSegment[] = [
  /* 西南侧弧形防波堤：参考图左下角，自左边缘伸出并向右下弯折。 */
  {
    kind: 'breakwater',
    pts: [[24, 296], [58, 310], [92, 330], [122, 356], [146, 386]],
    width: 13,
    height: 6,
    top: '#6c6255',
    side: '#4a4239',
    edge: '#8d8171',
    texId: 'breakwater_tex',
  },
  /* 东南侧弧形防波堤：参考图右下角，自右边缘向左下延伸。 */
  {
    kind: 'breakwater',
    pts: [[978, 352], [944, 372], [908, 392], [872, 410], [842, 430]],
    width: 13,
    height: 6,
    top: '#6c6255',
    side: '#4a4239',
    edge: '#8d8171',
    texId: 'breakwater_tex',
  },
  /* 厂区岸线护岸：沿厂区南缘的整段石砌海堤。 */
  {
    kind: 'revetment',
    pts: [[0, 150], [60, 158], [120, 152], [180, 166], [240, 158], [300, 170],
          [360, 160], [420, 176], [480, 166], [540, 150], [600, 170], [660, 178],
          [720, 164], [780, 150], [840, 168], [900, 156], [960, 166], [1000, 156]],
    width: 7,
    height: 4,
    top: '#6a6154',
    side: '#4c453c',
    edge: '#8a7f6e',
    texId: 'breakwater_tex',
  },
]

/** 明渠 / 暗渠（取排水通道） */
export const CHANNEL_WALLS: StructureSegment[] = [
  /* 取水明渠：从厂区取水口向西南延伸入海。 */
  {
    kind: 'channel',
    pts: [[540, 160], [520, 196], [498, 236], [478, 278], [462, 320]],
    width: 9,
    height: 0,
    top: 'rgba(24, 74, 96, 0.55)',
    side: 'rgba(96, 176, 200, 0.42)',
  },
  /* 排水暗渠：从排放口向东南延伸。 */
  {
    kind: 'channel',
    pts: [[700, 172], [724, 206], [750, 242], [776, 280]],
    width: 7,
    height: 0,
    top: 'rgba(20, 62, 82, 0.45)',
    side: 'rgba(88, 160, 186, 0.35)',
  },
]

/** 栈桥 / 突堤 / 取排水构筑物 */
export const PIERS: StructureSegment[] = [
  /* 取水口栈桥：参考图右侧伸入海中的细长结构。 */
  {
    kind: 'jetty',
    pts: [[556, 158], [560, 182], [564, 206]],
    width: 4,
    height: 3,
    top: '#7d7264',
    side: '#5a5248',
    edge: '#9a8e7d',
    texId: 'jetty_tex',
  },
  /* 排放口构筑物 */
  {
    kind: 'jetty',
    pts: [[698, 170], [694, 190], [690, 208]],
    width: 3.5,
    height: 3,
    top: '#6f6558',
    side: '#4e473e',
    edge: '#8d8272',
    texId: 'jetty_tex',
  },
]

/** 海上独立构筑物（V2 中只是「在海面上标出位置」，不做立体柱体） */
export const OFFSHORE_STRUCTURES: StructureSegment[] = []

/** 航标灯桩 / 浮标（平面小图标） */
export interface VerticalProp {
  /** 素材 id（优先用贴图） */
  id?: string
  /** 设计坐标 */
  dx: number
  dy: number
  /** 图标显示尺寸（设计单位） */
  size: number
  /** 图标形状（无贴图时回退绘制） */
  shape: 'beacon' | 'buoy'
  /** 配色 */
  color: string
  /** 标注文本 */
  label?: string
  /** 是否默认显示 */
  enabled?: boolean
  /** 柱体宽度（设计单位，兼容 V1） */
  width: number
  /** 柱体高度（设计单位，兼容 V1） */
  height: number
  /** 柱顶配色 */
  capColor?: string
}

export const VERTICAL_PROPS: VerticalProp[] = [
  /* 红白色航标灯桩（参考图右下角） */
  {
    id: 'nav_beacon',
    dx: 686, dy: 396, size: 15,
    width: 5, height: 18,
    shape: 'beacon',
    color: '#e8503c',
    capColor: '#f0f4f8',
    label: '航标灯桩',
    enabled: true,
  },
  /* 浮标 */
  {
    dx: 168, dy: 400, size: 8, width: 5, height: 7,
    shape: 'buoy',
    color: '#c9ac72',
    enabled: true,
  },
  {
    dx: 842, dy: 322, size: 8, width: 5, height: 7,
    shape: 'buoy',
    color: '#7fc9d8',
    enabled: true,
  },
]
