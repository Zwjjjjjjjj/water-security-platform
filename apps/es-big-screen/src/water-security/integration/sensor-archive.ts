/**
 * 感知设备装备档案（V2 斜俯视场景）
 *
 * 【为什么需要这个文件】
 * `devices.json` 描述的是**接入层**信息：
 *   编号、类型、状态、经纬度。
 * 它回答的是「这台设备在不在、连没连上」。
 *
 * 但「系统管理」页面要回答的是另一个问题：
 *   **「这台设备究竟能干什么？」**
 * 为此需要**装备层**信息：
 *   型号、部署位置、探测半径、扫描周期、工作方位、视场角。
 *
 * 这些参数**已经存在** —— 它们就定义在 `SceneSensorsV2.vue` 的 `SENSORS` 表里，
 * 因为场景真正绘制扫描扇面、距离环、视场锥用的就是那份数据。
 *
 * 【为什么单独抽一份而不是直接 import】
 * `SceneSensorsV2.vue` 是一个 Vue 单文件组件，
 * 它的 `<script setup>` 里导出的东西不能被普通 TS 模块 import
 * （`export interface` 在 `<script setup>` 里虽然可以，但常量不属于模块作用域）。
 *
 * 所以这里把装备档案**独立**成一份纯数据模块，
 * 由 `SceneSensorsV2.vue` 反过来 import 它 —— 保证两边永远一致。
 * 单一事实来源在 `sensor-archive.ts`，场景层与管理层都读它。
 *
 * 【单位约定】
 *   经纬度：WGS84 度
 *   距离：米
 *   方位角：度，0° = 真北，顺时针增加（与罗盘一致）
 */

/** 传感器类型 */
export type SensorKind = 'radar' | 'ais' | 'eo' | 'sonar'

export interface SensorArchive {
  /** 场景内部编号 */
  id: string
  /** 与 `devices.json` 的 `source_id` 对应（R01 / A01 / E01 / S01） */
  sourceId: string
  kind: SensorKind
  /** 设备名称 */
  name: string
  /** 型号（示意） */
  model: string
  /** 部署位置描述 */
  location: string
  /** 站点高程（米） */
  elevationM: number
  /** ENU 坐标（米）—— 与 `SceneSensorsV2` 完全一致 */
  east: number
  north: number
  /** 探测 / 接收半径（米） */
  rangeM: number
  /** 扫描（工作）周期（秒） */
  period: number
  /** 工作方位角（度，0 = 真北顺时针） */
  bearingDeg: number
  /** 光电水平视场角（度），仅光电有 */
  fovDeg?: number
  /** 接收扇面半角（度），AIS 用它表示示意性方向增益 */
  halfAngleDeg?: number
  /** 初始相位（秒），让各设备不同步 */
  phase: number
  /** 贴图素材 id（斜俯视立牌），声纳没有 */
  spriteId?: string
}

/**
 * 装备档案表。
 *
 * 【坐标与 `SceneSensorsV2` 的关系】
 * ENU 坐标取自 `campus_oblique_v3.png` 的实测像素位置反算：
 *   RADAR-01: px(262,505)  → ENU(-1985.9, 4034.4)
 *   AIS-01   : px(790,700)  → ENU( -748.4, 3587.5)
 *   EO-01    : px(1085,630) → ENU(  -57.0, 3747.9)
 */
export const SENSOR_ARCHIVE: SensorArchive[] = [
  {
    id: 'RADAR-01',
    sourceId: 'R01',
    kind: 'radar',
    name: '岸基雷达站',
    model: '岸基监视雷达（示意）',
    location: '厂区西南侧高地',
    elevationM: 42,
    east: -1985.9,
    north: 4034.4,
    rangeM: 2800,
    period: 6,
    bearingDeg: 88,
    phase: 0,
    spriteId: 'coastal_radar_station_45',
  },
  {
    id: 'AIS-01',
    sourceId: 'A01',
    kind: 'ais',
    name: 'AIS 岸台',
    model: 'VHF 岸基接收站（示意）',
    location: '厂区南侧岸边码头',
    elevationM: 6,
    east: -748.4,
    north: 3587.5,
    rangeM: 1600,
    period: 4.5,
    bearingDeg: 20,
    halfAngleDeg: 105,
    phase: 1.2,
    spriteId: 'ais_shore_station_45',
  },
  {
    id: 'EO-01',
    sourceId: 'E01',
    kind: 'eo',
    name: '光电转塔',
    model: '双通道光电转塔（示意）',
    location: '厂区东南角平台',
    elevationM: 8,
    east: -57.0,
    north: 3747.9,
    rangeM: 1000,
    period: 9,
    bearingDeg: 130,
    fovDeg: 26,
    phase: 2.4,
    spriteId: 'eo_turret_45',
  },
  {
    id: 'SO-01',
    sourceId: 'S01',
    kind: 'sonar',
    name: '声纳阵',
    model: '多波束声纳（示意）',
    location: '厂区取水口前沿',
    elevationM: -6,
    east: -330,
    north: 3810,
    rangeM: 80,
    period: 12,
    bearingDeg: 0,
    phase: 3.6,
  },
]

/** 按 devices.json 的 `source_id` 查档案 */
export function archiveBySourceId(sourceId: string): SensorArchive | undefined {
  return SENSOR_ARCHIVE.find((s) => s.sourceId === sourceId)
}

/** 按场景内部编号查档案 */
export function archiveById(id: string): SensorArchive | undefined {
  return SENSOR_ARCHIVE.find((s) => s.id === id)
}

/**
 * 探测能力的**人类可读**描述。
 *
 * 用于「系统管理」页面设备表的「探测能力」列 ——
 * 比裸数字（2800 m）更能让人一眼看出设备能覆盖多大的范围。
 */
export function capabilityText(a: SensorArchive): string {
  switch (a.kind) {
    case 'radar':
      return `主动探测 ${a.rangeM} m`
    case 'ais':
      return `被动接收 ${a.rangeM} m`
    case 'eo':
      return `光电凝视 ${a.rangeM} m / ${a.fovDeg ?? 0}°`
    case 'sonar':
      return `声呐波束 ${a.rangeM} m`
    default:
      return `${a.rangeM} m`
  }
}

/** 设备类型 → 中文标签 */
export const SENSOR_KIND_LABEL: Record<SensorKind, string> = {
  radar: '雷达',
  ais: 'AIS',
  eo: '光电',
  sonar: '声纳',
}

/* ------------------------------------------------------------------ *
 * 链路健康度
 * ------------------------------------------------------------------ */

export interface LinkHealth {
  /** 链路名（如「探测链路」「报文链路」） */
  name: string
  /** 健康度百分比（0~100） */
  pct: number
  /** 状态档位：ok / warn / idle */
  state: 'ok' | 'warn' | 'idle'
}

/**
 * 某台设备在**当前回放时刻**的链路健康度。
 *
 * 【语义 —— 不做随机化】
 * 每条链路的健康度都由**真实数据**推出：
 *
 *   · 探测链路（雷达）
 *     = 当前被该设备探测到的目标里，「处于探测圈内」的比例。
 *     一个目标都没探到 → 0%（idle）。
 *
 *   · 报文链路（AIS）
 *     = 该设备当前上报的目标数 / 该设备历史上报过的最大目标数。
 *     这正是 AIS 岸台「正在收到多少条报文」的直接体现。
 *
 *   · 凝视链路（光电）
 *     = 是否有跟踪目标（100% / 0%），
 *       再按目标当前距离占探测半径的比例微调 —— 目标越近，跟踪质量越高。
 *
 *   · 声纳链路
 *     = 水下目标当前被覆盖的比例。
 *
 * 【为什么这样算】
 * 页面上的每一根健康条都要能对上画面里的现象：
 * 雷达在扫却一个目标都没有 → 探测链路 0%；
 * AIS 岸台上报 2 条船 → 报文链路 100%。
 * 这样管理页与主场景不会各说各话。
 */
export function linkHealth(
  a: SensorArchive,
  ctx: {
    /** 被该设备关联到的目标（含 ENU 与来源） */
    linked: { east: number; north: number; sources: string[] }[]
    /** 该设备历史上报过的最大目标数 */
    maxReported: number
    /** 是否有光电跟踪目标 */
    eoTracking: boolean
    /** 光电跟踪目标的距离（米） */
    eoRangeM: number
    /** 水下目标总数 */
    underTotal: number
    /** 水下目标中被该设备覆盖的数量 */
    underCovered: number
  },
): LinkHealth[] {
  const out: LinkHealth[] = []

  if (a.kind === 'radar') {
    const inRange = ctx.linked.filter(
      (t) => Math.hypot(t.east - a.east, t.north - a.north) <= a.rangeM,
    ).length
    const total = Math.max(1, ctx.linked.length)
    const pct = Math.round((inRange / total) * 100)
    out.push({
      name: '探测链路',
      pct,
      state: inRange === 0 ? 'idle' : pct >= 80 ? 'ok' : 'warn',
    })
  } else if (a.kind === 'ais') {
    const max = Math.max(1, ctx.maxReported)
    const pct = Math.round((Math.min(ctx.linked.length, max) / max) * 100)
    out.push({
      name: '报文链路',
      pct,
      state: ctx.linked.length === 0 ? 'idle' : 'ok',
    })
  } else if (a.kind === 'eo') {
    let pct = 0
    if (ctx.eoTracking) {
      const near = Math.max(0, 1 - ctx.eoRangeM / Math.max(1, a.rangeM))
      pct = Math.round((0.55 + near * 0.45) * 100)
    }
    out.push({
      name: '凝视链路',
      pct,
      state: ctx.eoTracking ? (pct >= 70 ? 'ok' : 'warn') : 'idle',
    })
  } else if (a.kind === 'sonar') {
    const pct = ctx.underTotal
      ? Math.round((ctx.underCovered / ctx.underTotal) * 100)
      : 0
    out.push({
      name: '声呐覆盖',
      pct,
      state: pct === 0 ? 'idle' : pct >= 60 ? 'ok' : 'warn',
    })
  }
  return out
}
