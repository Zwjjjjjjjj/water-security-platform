/**
 * 立体构筑物几何（三维体量）。
 *
 * 【为什么要单独做】
 * 之前的防波堤、厂区围墙、取排水构筑物都是「画在地面平面上的描边」，
 * 在 2.5D 倾斜视角下它们始终是平的，看起来像贴纸，
 * 与周围立起来的建筑不在同一个空间层次 —— 这就是「二次元感」的来源。
 *
 * 这里把这些构筑物定义成**有厚度、有高度、有顶面**的三维体，
 * 与建筑立牌使用同一套投影（projectScene），
 * 于是整个场景（地面 / 建筑 / 堤坝 / 船只）处在同一个三维空间里。
 *
 * 坐标系：设计坐标（dx ∈ [0,1000]、dy ∈ [0,500]，y 向下），
 * 与底图 SVG 完全一致 —— 这样体量可以直接叠加在底图上，不会错位。
 */

/** 构筑物类型 */
export type StructureKind =
  | 'breakwater'
  | 'wall'
  | 'channel'
  | 'pier'
  | 'beacon'   // 航标灯桩 / 立柱
  | 'platform' // 海上平台 / 泵房
  | 'jetty'    // 突堤
  | 'revetment' // 护岸

export interface StructureSegment {
  /** 类型 */
  kind: StructureKind
  /** 设计坐标折线（中心线） */
  pts: [number, number][]
  /** 顶面宽度（设计单位） */
  width: number
  /** 高度（设计单位，1 ≈ 3.4m 水平尺度） */
  height: number
  /** 顶面颜色 */
  top: string
  /** 侧面颜色（受光面 / 背光面） */
  side: string
  /** 边缘高光 */
  edge?: string
  /** 受光立面的颜色（比顶面暗、比背光面亮） */
  litFill?: string
  /**
   * 朝向观察者的那一侧是否为受光面。
   *
   * 【为什么需要】
   * 场景的光源统一来自**西南方向**（画面左下），
   * 于是：
   *   - 堤坝的西南侧立面 → 受光（亮）
   *   - 堤坝的东北侧立面 → 背光（暗）
   *
   * 只画一侧立面时，若该侧恰好是背光面，整个体量会显得「凹」进去；
   * 因此按实际朝向决定用受光色还是背光色。
   */
  litSouth?: boolean
}

/**
 * 光源方位：西南方向。
 *
 * 取 (-0.72, +0.69)（设计坐标，x 向东为正、y 向南为正）——
 * 即光线来自画面左下方。
 * 所有立体体的受光/背光判定都以这个方向为准。
 */
export const SUN_DIR: [number, number] = [-0.72, 0.69]

/**
 * 防波堤与长堤。
 *
 * 【尺度依据】
 * 主防波堤顶面宽 16 设计单位 ≈ 54 米 —— 相当于可通行车行的堤顶宽度；
 * 高度 7 设计单位 ≈ 24 米 —— 相当于挡浪墙 + 堤身的高程，
 * 站在 2.5D 视角下能明显看到立面与顶面的厚度差。
 */
export const STRUCTURES: StructureSegment[] = [
  {
    // 主堤（东南环抱，绕开厂区陆域）
    kind: 'breakwater',
    pts: [
      [600, 196], [720, 214], [850, 250], [906, 320], [948, 372],
      [890, 430], [764, 466], [630, 504], [460, 494], [340, 456],
      [250, 428], [216, 378], [246, 322],
    ],
    width: 16,
    height: 7,
    top: '#4a5f6b',
    side: '#1e2d38',
    litFill: '#44596a',
    edge: '#7fb6c9',
    litSouth: true,
  },
  {
    // 西短堤
    kind: 'breakwater',
    pts: [[232, 182], [286, 212], [320, 256], [336, 308]],
    width: 13,
    height: 6,
    top: '#4a5f6b',
    side: '#1e2d38',
    litFill: '#44596a',
    edge: '#7fb6c9',
  },
  {
    // 厂区围墙（北侧陆域边界）
    kind: 'wall',
    pts: [
      [206, 136], [300, 122], [400, 128], [520, 118],
      [640, 126], [762, 116], [858, 124], [952, 114],
    ],
    width: 4,
    height: 2.6,
    top: '#54707e',
    side: '#1b2833',
    litFill: '#435c6d',
  },
  {
    // 取水口防波堤头（护岸）
    kind: 'breakwater',
    pts: [[318, 166], [352, 172], [360, 186]],
    width: 9,
    height: 3.6,
    top: '#48606c',
    side: '#1b2833',
    litFill: '#3f5563',
  },
  {
    // 排水口防波堤头
    kind: 'breakwater',
    pts: [[584, 166], [612, 174], [620, 190]],
    width: 8,
    height: 3.2,
    top: '#48606c',
    side: '#1b2833',
    litFill: '#3f5563',
  },
]

/**
 * 取排水构筑物的堤身（暗渠外护堤）。
 * 取水明渠本身是水道（不是障碍），但两侧要有实体堤壁，
 * 否则它在 2.5D 下会像一条飘在海上的虚线。
 */
export const CHANNEL_WALLS: StructureSegment[] = [
  {
    // 取水明渠西侧渠壁
    kind: 'channel',
    pts: [[326, 166], [346, 209], [376, 255], [416, 299], [454, 341], [498, 375], [548, 403]],
    width: 5,
    height: 2.8,
    top: '#4d6472',
    side: '#1b2a34',
    litFill: '#425765',
  },
  {
    // 取水明渠东侧渠壁
    kind: 'channel',
    pts: [[354, 170], [374, 211], [404, 257], [444, 301], [482, 343], [526, 377], [576, 405]],
    width: 5,
    height: 2.8,
    top: '#4d6472',
    side: '#22333d',
  },
  {
    // 排水暗渠护堤
    kind: 'channel',
    pts: [[600, 172], [626, 202], [660, 234], [704, 264], [748, 294], [796, 320], [846, 344]],
    width: 6,
    height: 3.4,
    top: '#4d6472',
    side: '#22333d',
  },
]

/**
 * 海上构筑物（航标、平台、突堤）。
 *
 * 这些是核电站海域常见的实体：
 *   - 航标灯桩：给主堤与航道提供方位锚点，同时是场景里的垂直体量；
 *   - 海上平台 / 泵房：取排水末端的附属设施；
 *   - 突堤：连接陆域与外构筑物的小型堤路。
 */
export const OFFSHORE_STRUCTURES: StructureSegment[] = [
  {
    // 主堤堤头灯桩基座
    kind: 'beacon',
    pts: [[906, 320], [906, 320]],
    width: 9,
    height: 9,
    top: '#8a9aa6',
    side: '#2d3d49',
    litFill: '#61727f',
  },
  {
    // 西堤堤头灯桩基座
    kind: 'beacon',
    pts: [[336, 308], [336, 308]],
    width: 8,
    height: 8,
    top: '#8a9aa6',
    side: '#2d3d49',
    litFill: '#61727f',
  },
  {
    // 取水末端海上平台（滤网检修）
    kind: 'platform',
    pts: [[566, 406], [586, 416], [600, 414]],
    width: 22,
    height: 3.4,
    top: '#5d7280',
    side: '#1d2a34',
    litFill: '#4a5f6d',
  },
  {
    // 排水末端消能块平台
    kind: 'platform',
    pts: [[846, 344], [862, 356], [874, 354]],
    width: 18,
    height: 3,
    top: '#5d7280',
    side: '#1d2a34',
    litFill: '#4a5f6d',
  },
  {
    // 取水口突堤（连接岸边与取水喇叭口）
    kind: 'jetty',
    pts: [[318, 158], [326, 172], [334, 184]],
    width: 7,
    height: 2.4,
    top: '#586d7b',
    side: '#1f2c36',
    litFill: '#465a68',
  },
  {
    // 排水口突堤
    kind: 'jetty',
    pts: [[592, 158], [598, 172], [606, 184]],
    width: 6,
    height: 2.2,
    top: '#586d7b',
    side: '#1f2c36',
    litFill: '#465a68',
  },
  {
    // 陆域护岸（厂区岸线加固，防止岸线在 3D 下显得单薄）
    kind: 'revetment',
    pts: [
      [206, 136], [300, 122], [400, 128], [520, 118],
      [640, 126], [762, 116], [858, 124], [952, 114],
    ],
    width: 6,
    height: 3.6,
    top: '#4d626f',
    side: '#1a2731',
    litFill: '#405563',
  },
]

/**
 * 栈桥 / 码头（从岸边伸向水域的细长体）。
 * 用于给厂区水侧一点立面层次，也让「靠泊」有物理依托。
 */
export const PIERS: StructureSegment[] = [
  {
    kind: 'pier',
    pts: [[340, 172], [340, 196]],
    width: 10,
    height: 1.8,
    top: '#6b7f8c',
    side: '#1f2d38',
    litFill: '#576d7c',
  },
  {
    kind: 'pier',
    pts: [[600, 176], [600, 200]],
    width: 9,
    height: 1.8,
    top: '#6b7f8c',
    side: '#2a3b46',
  },
]

/**
 * 竖向构筑物（灯桩 / 立杆 / 塔架）。
 *
 * 【与体量构筑物的区别】
 * 堤坝、渠壁这类是「躺着的」—— 高度方向在地面平面内展开，
 * 所以它们的侧面在倾斜地面上呈现为展开的带状。
 *
 * 而灯桩、塔架是「站着的」—— 高度方向垂直于地面，
 * 在屏幕上就是一根竖直的柱体。
 *
 * 这是场景里最强的立体线索：
 * 一个纯俯视的 2D 底图永远画不出「一根杆子」，
 * 只有真正竖起来的体量才能立刻让观众判断出这是三维空间。
 */
export interface VerticalProp {
  /** id */
  id: string
  /** 地面锚点（设计坐标） */
  dx: number
  dy: number
  /** 柱体高度（设计单位，1 ≈ 3.4m） */
  height: number
  /** 柱体底部直径（设计单位） */
  width: number
  /** 柱体颜色 */
  color: string
  /** 顶端颜色（塔顶 / 灯室） */
  capColor: string
  /** 顶部是否发光 */
  glow?: boolean
  /** 发光颜色 */
  glowColor?: string
}

/**
 * 竖向构筑物清单。
 *
 * 灯桩位置与 `STRUCTURES` 中的堤头一致（906,320 / 336,308），
 * 顶端发光，与底图上原有的堤头灯点呼应 —— 现在它们是真正立起来的。
 */
export const VERTICAL_PROPS: VerticalProp[] = [
  {
    // 主堤堤头灯桩
    id: 'beacon-main',
    dx: 906,
    dy: 320,
    height: 34,
    width: 5.2,
    color: '#5c6f7c',
    capColor: '#c9d6dd',
    glow: true,
    glowColor: '#ffd54a',
  },
  {
    // 西堤堤头灯桩
    id: 'beacon-west',
    dx: 336,
    dy: 308,
    height: 27,
    width: 4.6,
    color: '#5c6f7c',
    capColor: '#c9d6dd',
    glow: true,
    glowColor: '#ffd54a',
  },
  {
    // 取水口航标（绿光）
    id: 'beacon-intake',
    dx: 326,
    dy: 172,
    height: 17,
    width: 3.0,
    color: '#4f6572',
    capColor: '#9fc4cf',
    glow: true,
    glowColor: '#8fe0b8',
  },
  {
    // 排水口航标（红光）
    id: 'beacon-discharge',
    dx: 604,
    dy: 180,
    height: 17,
    width: 3.0,
    color: '#4f6572',
    capColor: '#9fc4cf',
    glow: true,
    glowColor: '#ff7a6b',
  },
  {
    // 海上平台上的桅杆
    id: 'mast-platform',
    dx: 582,
    dy: 410,
    height: 24,
    width: 2.8,
    color: '#4a5f6c',
    capColor: '#8fa6b3',
    glow: true,
    glowColor: '#7fe8f0',
  },
]
