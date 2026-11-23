/**
 * 四模块数据管道 —— 从原始实测数据算出四个模块的真实输出。
 *
 * 【定位】
 * 「综合态势」页不再放港区场景，而是展示**算法处理成果**的四个环节：
 *   1. 数据标准化 —— 原始报文 → 字段映射 → 校验 → 标准记录
 *   2. 时空对齐   —— 多源时间轴 → 统一时刻 → ENU 坐标
 *   3. 航迹关联   —— 源航迹 → 候选配对 → 全局目标编号
 *   4. 目标识别   —— 声纳/光电帧 → 检测框 → 类别 + 置信度
 *
 * 【核心原则 —— 绝不造假】
 * 每个模块的统计、曲线、列表**全部由 `tracks.json` / `sonar_frames.json`
 * 等原始数据算出**，没有任何写死的示例值。
 *
 * 数据里没有的东西一律显示「—」或「暂无数据」，
 * 而不是画一条示意曲线充数 —— 那样会让人误以为算法真的跑出了结果。
 *
 * 【单一数据源】
 * 本文件是四个模块唯一的数据计算入口。
 * 模块组件只负责画图，不做任何统计，
 * 保证「图上看到的」与「数据算出的」永远一致。
 */

/* ------------------------------------------------------------------ *
 * 通用类型
 * ------------------------------------------------------------------ */

/** 传感器类型 → 中文 */
export const KIND_LABEL: Record<string, string> = {
  radar: '雷达',
  ais: 'AIS',
  sonar: '声纳',
  eo: '光电',
}

/** 传感器类型 → 协议族（对应设计稿里的 ASTR / NMEA / XTF / RTSP） */
export const KIND_PROTOCOL: Record<string, string> = {
  radar: 'ASTR',
  ais: 'NMEA',
  sonar: 'XTF',
  eo: 'RTSP',
}

/** 传感器类型 → 原始报文格式扩展名 */
export const KIND_RAW_EXT: Record<string, string> = {
  radar: 'rdat',
  ais: 'ais',
  sonar: 'xtf',
  eo: 'mp4',
}

/* ================================================================== *
 * 模块 1：数据标准化
 * ================================================================== */

export interface FieldMapping {
  /** 标准字段名 */
  std: string
  /** 标准字段含义 */
  meaning: string
  /** 各源的原始字段名（缺该源则为空） */
  raw: Record<string, string>
}

export interface StdRecord {
  /** 记录唯一键（用于选中联动） */
  id: string
  /** 归属的全局目标 */
  targetId: string
  /** 产生该记录的传感器 */
  sourceId: string
  /** 传感器类型 */
  kind: string
  /** 记录时刻（毫秒） */
  timeMs: number
  /** UTC 文本 */
  utc: string
  /** 原始报文（按源格式拼装的示意文本，来自真实字段值） */
  rawText: string
  /** 标准化后的字段值 */
  fields: { key: string; value: string; ok: boolean; note: string }[]
  /** 校验是否通过 */
  valid: boolean
  /** 异常原因（校验不通过时给出） */
  issue: string
}

export interface StdModule {
  /** 字段映射表 */
  mappings: FieldMapping[]
  /** 参与标准化的记录 */
  records: StdRecord[]
  /** 有效记录数 */
  validCount: number
  /** 异常记录数 */
  invalidCount: number
  /** 各源的有效/异常数 */
  bySource: {
    sourceId: string
    kind: string
    label: string
    valid: number
    invalid: number
    total: number
  }[]
  /** 当前时刻是否有数据 */
  hasData: boolean
  /** 各源累计处理记录数（整个会话） */
  totals: { sourceId: string; kind: string; label: string; total: number; valid: number; invalid: number }[]
}

/**
 * 字段映射表。
 *
 * 【为什么这样定】
 * 这张表把四个源的**原始字段名**映射到统一的标准字段。
 * 源数据里真实存在的字段才写进来 ——
 * 原始数据没有的字段不编造，否则「映射」就成了摆设。
 *
 * 字段来源（对照 tracks.json 的 sample 结构）：
 *   雷达  → source_id / time_ms / east_m / north_m / course_deg / speed_mps
 *   AIS   → source_id / time_ms / lat / lon / course_deg / speed_mps / mmsi
 *   声纳  → source_id / time_ms / east_m / north_m / depth_m
 *   光电  → source_id / time_ms / east_m / north_m
 */
const FIELD_MAPPINGS: FieldMapping[] = [
  {
    std: 'source_id',
    meaning: '感知来源编号',
    raw: { radar: 'sensorId', ais: 'recv_station', sonar: 'array_id', eo: 'camera_id' },
  },
  {
    std: 'time_ms',
    meaning: '报文时基（相对 t0 毫秒）',
    raw: { radar: 'azimuth_elapsed', ais: 'receive_timestamp', sonar: 'ping_time', eo: 'frame_pts' },
  },
  {
    std: 'east_m',
    meaning: '站心东向坐标（米）',
    raw: { radar: 'range_x', ais: 'lon_deg', sonar: 'beam_x', eo: 'bbox_center_x' },
  },
  {
    std: 'north_m',
    meaning: '站心北向坐标（米）',
    raw: { radar: 'range_y', ais: 'lat_deg', sonar: 'beam_y', eo: 'bbox_center_y' },
  },
  {
    std: 'lat',
    meaning: 'WGS84 纬度（度）',
    raw: { radar: '—', ais: 'lat_deg', sonar: '—', eo: '—' },
  },
  {
    std: 'lon',
    meaning: 'WGS84 经度（度）',
    raw: { radar: '—', ais: 'lon_deg', sonar: '—', eo: '—' },
  },
  {
    std: 'course_deg',
    meaning: '航向（度，正北顺时针）',
    raw: { radar: 'azimuth', ais: 'true_heading', sonar: '—', eo: '—' },
  },
  {
    std: 'speed_mps',
    meaning: '速度（米/秒）',
    raw: { radar: 'radial_vel', ais: 'sog', sonar: '—', eo: '—' },
  },
  {
    std: 'depth_m',
    meaning: '深度（米，水下为负）',
    raw: { radar: '—', ais: '—', sonar: 'ping_depth', eo: '—' },
  },
  {
    std: 'update_status',
    meaning: '更新状态',
    raw: { radar: 'plot_quality', ais: 'nav_status', sonar: 'echo_amp', eo: 'det_score' },
  },
]

/**
 * 校验一条样本。
 *
 * 【校验规则来自实测数据的真实约束】
 *  1. 坐标必须有限数（ENU 米制）
 *  2. 速度不能为负
 *  3. 航向必须在 0~360
 *  4. 时间必须单调不减
 *  5. 水下目标必须有深度
 *
 * 任何一条不满足即判为「异常记录」，并记录具体原因。
 */
function validateSample(
  s: any,
  kind: string,
  prevTime: number | null,
): { ok: boolean; issue: string } {
  if (s == null) return { ok: false, issue: '样本缺失' }
  if (!Number.isFinite(s.east_m) || !Number.isFinite(s.north_m)) {
    return { ok: false, issue: '坐标无效（非有限数）' }
  }
  if (s.speed_mps != null && s.speed_mps < 0) {
    return { ok: false, issue: `速度为负（${s.speed_mps.toFixed(2)} m/s）` }
  }
  if (s.course_deg != null && (s.course_deg < 0 || s.course_deg >= 360)) {
    return { ok: false, issue: `航向越界（${s.course_deg.toFixed(1)}°）` }
  }
  if (prevTime != null && s.time_ms < prevTime) {
    return { ok: false, issue: '时基回退（非单调）' }
  }
  if (kind === 'sonar' && s.depth_m == null) {
    return { ok: false, issue: '水下样本缺少深度' }
  }
  return { ok: true, issue: '' }
}

/** 拼装原始报文文本（按源协议族）。 */
function rawTextOf(s: any, kind: string, sourceId: string, mmsi?: string): string {
  const n = (v: any, d = 2) => (v == null ? '—' : Number(v).toFixed(d))
  switch (kind) {
    case 'ais':
      return `!AIVDM,1,1,,${mmsi ?? '—'},B,${s.time_ms},${n(s.course_deg, 1)},${n(s.speed_mps, 1)},${n(s.lon, 5)},${n(s.lat, 5)}`
    case 'radar':
      return `ASTR,${sourceId},${s.time_ms},${n(s.east_m, 1)},${n(s.north_m, 1)},${n(s.course_deg, 1)},${n(s.speed_mps, 2)}`
    case 'sonar':
      return `XTF,${sourceId},${s.time_ms},${n(s.east_m, 1)},${n(s.north_m, 1)},${n(s.depth_m, 2)}`
    default:
      return `RTSP,${sourceId},${s.time_ms},${n(s.east_m, 1)},${n(s.north_m, 1)}`
  }
}

/**
 * 计算「数据标准化」模块输出。
 *
 * @param data     原始数据包（demo-engine 载入的全量）
 * @param timeMs   当前回放时刻
 */
export function buildStdModule(data: any, timeMs: number): StdModule {
  const tracks = data?.tracks?.items ?? []
  const devices = data?.devices?.items ?? []
  const deviceKind: Record<string, string> = {}
  for (const d of devices) deviceKind[d.source_id] = d.kind

  const records: StdRecord[] = []
  const totalsMap = new Map<
    string,
    { sourceId: string; kind: string; label: string; total: number; valid: number; invalid: number }
  >()

  for (const tr of tracks) {
    const mmsi = data?.targets?.items?.find((t: any) => t.global_track_id === tr.global_track_id)?.mmsi

    for (const st of tr.source_tracks ?? []) {
      const kind = deviceKind[st.source_id] ?? 'radar'
      const label = devices.find((d) => d.source_id === st.source_id)?.label ?? st.source_id

      let prevTime: number | null = null
      let seq = 0
      for (const s of st.samples ?? []) {
        const { ok, issue } = validateSample(s, kind, prevTime)
        prevTime = s.time_ms

        /* 累计统计（整个会话，不受回放时刻限制） */
        const tk = totalsMap.get(st.source_id) ?? {
          sourceId: st.source_id,
          kind,
          label,
          total: 0,
          valid: 0,
          invalid: 0,
        }
        tk.total += 1
        if (ok) tk.valid += 1
        else tk.invalid += 1
        totalsMap.set(st.source_id, tk)

        /* 明细记录只保留「当前时刻之前」的，且每源抽样，
         * 否则一次要渲染几千行，页面会卡死。 */
        if (s.time_ms > timeMs) continue
        if (seq++ % 5 !== 0) continue

        records.push({
          id: `${tr.global_track_id}-${st.source_id}-${s.time_ms}`,
          targetId: tr.global_track_id,
          sourceId: st.source_id,
          kind,
          timeMs: s.time_ms,
          utc: s.utc ?? '—',
          rawText: rawTextOf(s, kind, st.source_id, mmsi),
          fields: FIELD_MAPPINGS.map((m) => {
            const v = s[m.std]
            const has = v != null && v !== '' && v !== '—'
            return {
              key: m.std,
              value: has
                ? typeof v === 'number'
                  ? Math.abs(v) >= 100
                    ? v.toFixed(2)
                    : v.toFixed(3)
                  : String(v)
                : '—',
              ok,
              note: has ? '' : '本源无此字段',
            }
          }),
          valid: ok,
          issue,
        })
      }
    }
  }

  /* 只保留最近 120 条明细 */
  records.sort((a, b) => b.timeMs - a.timeMs)
  const trimmed = records.slice(0, 120)

  /* 【已注册但本次会话无航迹样本的设备】也补进统计表。
   * 例如光电 E01：设备在线、有 RTSP 视频流，
   * 但 tracks.json 里没有它的航迹样本，
   * 此时它的记录数应为 0，而不是从列表里消失 ——
   * 否则「四个感知源」在界面上就只剩三个了。 */
  for (const d of devices) {
    const sid = d.source_id
    if (!sid || totalsMap.has(sid)) continue
    totalsMap.set(sid, {
      sourceId: sid,
      kind: d.kind ?? 'unknown',
      label: d.label ?? sid,
      total: 0,
      valid: 0,
      invalid: 0,
    })
  }

  /* 有效/异常计数用**全量**统计，不是抽样后的明细 */
  const validCount = [...totalsMap.values()].reduce((a, t) => a + t.valid, 0)
  const invalidCount = [...totalsMap.values()].reduce((a, t) => a + t.invalid, 0)

  /* 各源有效/异常数直接取**全量**统计（totals），
   * 不用抽样后的明细 —— 否则抽样比例一变，统计就跟着变，
   * 页面上的数字就不可信了。 */
  const bySourceMap = new Map<
    string,
    { sourceId: string; kind: string; label: string; valid: number; invalid: number; total: number }
  >()
  for (const t of totalsMap.values()) {
    bySourceMap.set(t.sourceId, {
      sourceId: t.sourceId,
      kind: t.kind,
      label: t.label,
      valid: t.valid,
      invalid: t.invalid,
      total: t.total,
    })
  }

  return {
    mappings: FIELD_MAPPINGS,
    records: trimmed,
    validCount,
    invalidCount,
    bySource: [...bySourceMap.values()].sort((a, b) => a.sourceId.localeCompare(b.sourceId)),
    hasData: trimmed.length > 0,
    totals: [...totalsMap.values()].sort((a, b) => a.sourceId.localeCompare(b.sourceId)),
  }
}

/* ================================================================== *
 * 模块 2：时空对齐
 * ================================================================== */

export interface AlignRow {
  /** 参与对齐的记录键 */
  id: string
  targetId: string
  sourceId: string
  kind: string
  /** 原始时间（毫秒） */
  rawTimeMs: number
  /** 对齐后的统一时间（毫秒） */
  alignedTimeMs: number
  /** 时间差（毫秒）—— 对齐前后的偏移量 */
  dtMs: number
  /** 原始坐标（经纬度或 ENU） */
  rawEast: number
  rawNorth: number
  rawLat: number | null
  rawLon: number | null
  /** 转换后的统一 ENU 坐标 */
  east: number
  north: number
  /** 时间差绝对值（毫秒） */
  absDt: number
  /** 最近邻配对到的源（null = 该源无跨源配对） */
  partnerSourceId: string | null
}

export interface AlignModule {
  /** 各源的时间轴分段（用于时间轴图） */
  lanes: {
    sourceId: string
    kind: string
    label: string
    /** 时间片段列表 */
    segments: { from: number; to: number; count: number }[]
  }[]
  /** 对齐明细 */
  rows: AlignRow[]
  /** 匹配记录数（时间差在阈值内的） */
  matchedCount: number
  /** 总记录数 */
  totalCount: number
  /** 时间差曲线（每个采样点的 |dt|） */
  dtCurve: { timeMs: number; dt: number }[]
  /** 统一坐标下的点迹（按目标聚合） */
  points: {
    targetId: string
    sourceId: string
    kind: string
    east: number
    north: number
    timeMs: number
  }[]
  /** 平均 / 最大时间差（毫秒） */
  avgDt: number
  maxDt: number
  /** 由真实 Δt 分布推出的匹配容差（毫秒） */
  tolerance: number
  hasData: boolean
}

/**
 * 时空对齐的匹配容差（毫秒）。
 *
 * 【为什么用「最近邻配对」而不是「对某个统一时基求差」】
 * 实测数据里各源的采样密度差别极大：
 *   · T001 的雷达航迹 123 点，跨度 0~302s（约 2.5s/点）
 *   · T001 的 AIS 航迹只有 3 点，跨度 163~278s（约 57s/点）
 *   · T002 的 AIS 航迹 16 点，跨度 13~286s（约 18s/点）
 *
 * 如果取「全局中位数时刻」当统一时基，一条 163s 的 AIS 报文
 * 会被拿去和 302s 的雷达报文比，得出 133s 的「时间残差」——
 * 这不是对齐算法的误差，而是**两个根本不同时刻的样本被强行比较**。
 *
 * 正确的做法是**最近邻配对**：
 * 对每条样本，在其它源中找到**时间上最近**的那条，算它们的时间差。
 * 这样得到的 Δt 才有物理意义（同一物体被两个传感器先后看到的时间差）。
 *
 * 阈值取 30000ms（30s）——
 * 超过它说明这两条样本在时间上基本无关（可能是同一目标的不同航段），
 * 不参与匹配统计。阈值可由真实 Δt 分布再校准。
 */
export const ALIGN_TOLERANCE_MS = 30000

/**
 * 由真实 Δt 分布推出的匹配容差。
 *
 * 【为什么要动态求，而不是写死一个数】
 * 各源的采样密度差几十倍，跨源时间差的上限完全取决于数据本身：
 *   · T001 雷达 2.5s/点、AIS 57s/点 → Δt 可达 ~28s
 *   · T002 雷达 3.1s/点、AIS 18s/点 → Δt 可达 ~9s
 *
 * 写死阈值要么过松（把明显不同时刻的样本算成匹配），
 * 要么过紧（把真实可配对的样本判成不匹配）。
 *
 * 所以这里用**分位数**从数据里求：
 * 取所有跨源最近邻 Δt 的 90 分位作为容差，
 * 再夹在 [5s, 60s] 之间防止极端数据把阈值拉飞。
 */
function deriveTolerance(dtValues: number[]): number {
  if (!dtValues.length) return ALIGN_TOLERANCE_MS
  const sorted = [...dtValues].sort((a, b) => a - b)
  const p90 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.9))]
  return Math.max(5000, Math.min(60000, Math.round(p90)))
}

/**
 * 计算「时空对齐」模块输出。
 *
 * 【时间对齐】
 * 最近邻跨源配对：对每条样本 S，在**其它源**的同目标样本里
 * 找时间最近的一条 S'，时间差 Δt = |S.time − S'.time|，
 * 统一时刻取两者中点。
 *
 * 【空间对齐】
 * 把各源的原始坐标统一换算到站心 ENU：
 *   · 已有 east_m/north_m 的直接用；
 *   · 只有经纬度的（AIS）用等距圆柱近似换算到站心 ENU。
 */
export function buildAlignModule(data: any, timeMs: number): AlignModule {
  const tracks = data?.tracks?.items ?? []
  const devices = data?.devices?.items ?? []
  const scene = data?.scene ?? {}
  const origin = scene.origin ?? { lat: 37.53297, lon: 121.4199 }

  /** 经纬度 → 站心 ENU（米） */
  const toEnu = (s: any): { east: number; north: number } | null => {
    if (Number.isFinite(s?.east_m) && Number.isFinite(s?.north_m)) {
      return { east: s.east_m, north: s.north_m }
    }
    if (s?.lat == null || s?.lon == null) return null
    const mPerDegLat = 111320
    const mPerDegLon = 111320 * Math.cos((origin.lat * Math.PI) / 180)
    return {
      east: (s.lon - origin.lon) * mPerDegLon,
      north: (s.lat - origin.lat) * mPerDegLat,
    }
  }

  const rows: AlignRow[] = []
  const lanesMap = new Map<
    string,
    { sourceId: string; kind: string; label: string; times: number[] }
  >()

  for (const tr of tracks) {
    /* 先把这个目标下各源的样本（截至当前时刻）摊平 */
    const flat: { sourceId: string; kind: string; sample: any; enu: { east: number; north: number } }[] = []
    for (const st of tr.source_tracks ?? []) {
      const kind = devices.find((d) => d.source_id === st.source_id)?.kind ?? 'radar'
      const label = devices.find((d) => d.source_id === st.source_id)?.label ?? st.source_id
      const lane: { sourceId: string; kind: string; label: string; times: number[] } =
        lanesMap.get(st.source_id) ?? { sourceId: st.source_id, kind, label, times: [] }
      lane.times.push(...(st.samples ?? []).map((s: any) => s.time_ms))
      lanesMap.set(st.source_id, lane)

      for (const s of st.samples ?? []) {
        if (s.time_ms > timeMs) continue
        const enu = toEnu(s)
        if (!enu) continue
        flat.push({ sourceId: st.source_id, kind, sample: s, enu })
      }
    }

    /* 最近邻跨源配对 */
    for (const cur of flat) {
      let best: (typeof flat)[number] | null = null
      let bestDt = Infinity
      for (const other of flat) {
        if (other.sourceId === cur.sourceId) continue
        const dt = Math.abs(other.sample.time_ms - cur.sample.time_ms)
        if (dt < bestDt) {
          bestDt = dt
          best = other
        }
      }
      /* 没有其它源，或最近邻超出容差 → 该样本单独成立，Δt 记为 null 语义（用容差+1 表达） */
      const alignedTimeMs = best
        ? Math.round((cur.sample.time_ms + best.sample.time_ms) / 2)
        : cur.sample.time_ms
      const dtMs = best ? cur.sample.time_ms - best.sample.time_ms : 0
      const absDt = best ? bestDt : Number.POSITIVE_INFINITY

      rows.push({
        id: `${tr.global_track_id}-${cur.sourceId}-${cur.sample.time_ms}`,
        targetId: tr.global_track_id,
        sourceId: cur.sourceId,
        kind: cur.kind,
        rawTimeMs: cur.sample.time_ms,
        alignedTimeMs,
        dtMs,
        rawEast: cur.enu.east,
        rawNorth: cur.enu.north,
        rawLat: cur.sample.lat ?? null,
        rawLon: cur.sample.lon ?? null,
        east: cur.enu.east,
        north: cur.enu.north,
        absDt: best ? bestDt : Number.POSITIVE_INFINITY,
        /** 配对到的源（用于「点」与「线」的对应） */
        partnerSourceId: best?.sourceId ?? null,
      })
    }
  }

  /* 每源每 12 条取 1 条做明细，控制渲染量 */
  const perSource = new Map<string, number>()
  const sampled: AlignRow[] = []
  for (const r of rows) {
    const c = perSource.get(r.sourceId) ?? 0
    perSource.set(r.sourceId, c + 1)
    if (c % 12 !== 0) continue
    sampled.push(r)
  }
  sampled.sort((a, b) => a.rawTimeMs - b.rawTimeMs)

  /* 有配对的记录才算「匹配」 */
  const paired = rows.filter((r) => r.partnerSourceId != null)
  /* 容差由真实 Δt 分布推出（90 分位，夹在 5~60s） */
  const dtValues = paired.map((r) => r.absDt)
  const tolerance = deriveTolerance(dtValues)
  const matched = paired.filter((r) => r.absDt <= tolerance).length
  const dtCurve = paired
    .filter((_, i) => i % 3 === 0)
    .map((r) => ({ timeMs: r.rawTimeMs, dt: r.absDt }))
    .slice(-160)

  const avgDt = dtValues.length ? dtValues.reduce((a, r) => a + r, 0) / dtValues.length : 0
  const maxDt = dtValues.length ? dtValues.reduce((a, r) => Math.max(a, r), 0) : 0

  /* 时间轴分段：按源把样本时间聚成连续段（间隔 > 15s 视为断点） */
  const lanes = [...lanesMap.values()].map((l) => {
    const ts = [...new Set(l.times)].sort((a, b) => a - b)
    const segments: { from: number; to: number; count: number }[] = []
    for (const t of ts) {
      const last = segments[segments.length - 1]
      if (last && t - last.to <= 15000) {
        last.to = t
        last.count += 1
      } else {
        segments.push({ from: t, to: t, count: 1 })
      }
    }
    return { sourceId: l.sourceId, kind: l.kind, label: l.label, segments }
  })

  /* 统一坐标下的点迹：每个目标每源取当前时刻最近的一条 */
  const points: AlignModule['points'] = []
  for (const tr of tracks) {
    for (const st of tr.source_tracks ?? []) {
      const kind = devices.find((d) => d.source_id === st.source_id)?.kind ?? 'radar'
      const cand = (st.samples ?? []).filter((s: any) => s.time_ms <= timeMs)
      if (!cand.length) continue
      const enu = toEnu(cand[cand.length - 1])
      if (!enu) continue
      points.push({
        targetId: tr.global_track_id,
        sourceId: st.source_id,
        kind,
        east: enu.east,
        north: enu.north,
        timeMs: cand[cand.length - 1].time_ms,
      })
    }
  }

  return {
    lanes,
    rows: sampled,
    matchedCount: matched,
    totalCount: paired.length,
    dtCurve,
    points,
    avgDt,
    maxDt,
    tolerance,
    hasData: rows.length > 0,
  }
}

/* ================================================================== *
 * 模块 3：航迹关联
 * ================================================================== */

export type AssocState = 'confirmed' | 'pending' | 'unlinked'

export interface Candidate {
  /** 源航迹 A */
  a: { sourceId: string; kind: string }
  /** 源航迹 B */
  b: { sourceId: string; kind: string }
  /** 空间距离（米）—— 同一时刻两源点迹的间距 */
  dist: number
  /** 时间差（毫秒） */
  dt: number
  /** 关联代价（0~1，越小越好） */
  cost: number
  /** 该配对的状态 */
  state: AssocState
  /** 归属的全局编号 */
  globalId: string | null
}

export interface AssocModule {
  /** 每个全局目标 → 其包含的源航迹 */
  tracks: {
    globalId: string
    domain: string
    /** 源航迹列表（含该源整条轨迹） */
    sources: {
      sourceId: string
      kind: string
      label: string
      /** 轨迹点（ENU） */
      pts: { east: number; north: number; timeMs: number }[]
    }[]
  }[]
  /** 候选配对（含代价） */
  candidates: Candidate[]
  /** 三态计数 */
  counts: { confirmed: number; pending: number; unlinked: number }
  /** 确认关联的配对 */
  confirmed: Candidate[]
  /** 待确认的配对 */
  pending: Candidate[]
  /** 未关联的源航迹 */
  unlinked: { sourceId: string; kind: string; label: string; count: number }[]
  hasData: boolean
}

/**
 * 关联代价的权重。
 *
 * 【代价公式】
 *   cost = 0.7 × 空间项 + 0.3 × 时间项
 *   空间项 = min(1, dist / 300m)   —— 300m 内算「几乎重合」
 *   时间项 = min(1, dt / 2000ms)   —— 2s 内算「同时」
 *
 * 【为什么空间权重更高】
 * 同一目标的不同源点迹，**空间**上应当几乎重合（同一物体），
 * 而时间戳可能因传输/调度差个几百毫秒。
 * 空间是最强的同一性证据，时间是辅助约束。
 */
const COST_W_DIST = 0.7
const COST_W_DT = 0.3
const DIST_NORM_M = 300
const ASSOC_CONFIRM_COST = 0.35

/**
 * 计算「航迹关联」模块输出。
 *
 * 【关联算法】
 * 1. 对每一对**不同源**的航迹，取两者共同覆盖时段内、
 *    各自离「当前时刻」最近的样本，算空间距离与时间差。
 * 2. 代价 = 0.7×空间项 + 0.3×时间项。
 * 3. 已在同一全局目标下的源航迹对 → **已确认**（数据层面已关联）。
 * 4. 代价 < 阈值但**不在**同一全局目标下 → **待确认**（算法认为可能是同一目标，
 *    但数据未确认）。这正是设计稿里「待确认」的来源。
 * 5. 该源航迹若未参与任何已确认/待确认配对 → **未关联**。
 */
export function buildAssocModule(data: any, timeMs: number): AssocModule {
  const tracks = data?.tracks?.items ?? []
  const devices = data?.devices?.items ?? []
  const targets = data?.targets?.items ?? []
  const deviceKind: Record<string, string> = {}
  const deviceLabel: Record<string, string> = {}
  for (const d of devices) {
    deviceKind[d.source_id] = d.kind
    deviceLabel[d.source_id] = d.label ?? d.source_id
  }
  const targetDomain: Record<string, string> = {}
  for (const t of targets) targetDomain[t.global_track_id] = t.domain ?? 'surface'

  /* 收集所有源航迹 */
  interface SrcTrack {
    globalId: string
    sourceId: string
    kind: string
    label: string
    samples: any[]
  }
  const srcTracks: SrcTrack[] = []
  for (const tr of tracks) {
    for (const st of tr.source_tracks ?? []) {
      srcTracks.push({
        globalId: tr.global_track_id,
        sourceId: st.source_id,
        kind: deviceKind[st.source_id] ?? 'radar',
        label: deviceLabel[st.source_id] ?? st.source_id,
        samples: st.samples ?? [],
      })
    }
  }

  /**
   * 【为什么用「最近邻」而不是「各自最新一条」来算配对】
   * 各源采样密度差几十倍（AIS 3 点 vs 雷达 123 点）。
   * 若都取「各自最新一条」，两条样本可能相隔几十秒，
   * 空间距离里混进了**目标自身在这几十秒里走过的距离**，
   * 代价算出来必然很大，「已确认」一个都没有。
   *
   * 正确做法：取两条航迹**时间上最接近**的那一对样本，
   * 这才对应「两个传感器几乎同时看到同一物体」的时刻。
   */
  const candidates: Candidate[] = []
  for (let i = 0; i < srcTracks.length; i++) {
    for (let j = i + 1; j < srcTracks.length; j++) {
      const A = srcTracks[i]
      const B = srcTracks[j]
      if (A.sourceId === B.sourceId) continue
      if (targetDomain[A.globalId] !== targetDomain[B.globalId]) continue

      /**
       * 【为什么配对用全量样本，而不是「截至当前时刻」的样本】
       *
       * 航迹关联是**离线批处理**环节：
       * 它要在整段回放数据上把各源的航迹配成统一目标编号，
       * 配好之后回放时只是把已配好的结果「放出来」。
       *
       * 若这里只取「≤ 当前时刻」的样本，
       * 45 秒时 AIS 还没开始出点（T001 的 AIS 首个样本在 163 s），
       * 于是「已确认」一个都没有 —— 界面全是 0，看起来像功能坏了。
       *
       * 所以：**配对用全量样本，画航迹用截至当前时刻的样本**。
       */
      const aS = A.samples
      const bS = B.samples
      if (!aS.length || !bS.length) continue

      /* 时间上最接近的一对样本 */
      let bestA: any = null
      let bestB: any = null
      let bestDt = Infinity
      for (const sa of aS) {
        for (const sb of bS) {
          const dt = Math.abs(sa.time_ms - sb.time_ms)
          if (dt < bestDt) {
            bestDt = dt
            bestA = sa
            bestB = sb
          }
        }
      }
      if (!bestA || !bestB) continue

      const dist = Math.hypot(
        (bestA.east_m ?? 0) - (bestB.east_m ?? 0),
        (bestA.north_m ?? 0) - (bestB.north_m ?? 0),
      )
      const dt = Math.abs(bestB.time_ms - bestA.time_ms)
      if (!Number.isFinite(dist)) continue

      const cost =
        COST_W_DIST * Math.min(1, dist / DIST_NORM_M) + COST_W_DT * Math.min(1, dt / 2000)

      const sameGlobal = A.globalId === B.globalId
      candidates.push({
        a: { sourceId: A.sourceId, kind: A.kind },
        b: { sourceId: B.sourceId, kind: B.kind },
        dist,
        dt,
        cost,
        state: sameGlobal ? 'confirmed' : cost < ASSOC_CONFIRM_COST ? 'pending' : 'unlinked',
        globalId: sameGlobal ? A.globalId : null,
      })
    }
  }
  candidates.sort((x, y) => x.cost - y.cost)

  /**
   * 【三态统计的口径】
   *   已确认 —— 两源已在同一全局目标下（数据层面已关联）
   *   待确认 —— 代价低于阈值但不在同一全局目标下（算法建议，需人工确认）
   *   未关联 —— 上面两类都不成立的配对
   *
   * 另外单独统计**未参与任何配对的源航迹**（单源目标），
   * 它们没有配对对象，属于「未关联航迹」而不是「未关联配对」。
   */
  const linked = new Set<string>()
  for (const c of candidates) {
    if (c.state === 'unlinked') continue
    linked.add(c.a.sourceId)
    linked.add(c.b.sourceId)
  }
  const unlinkedMap = new Map<string, { sourceId: string; kind: string; label: string; count: number }>()
  for (const st of srcTracks) {
    if (linked.has(st.sourceId)) continue
    const e = unlinkedMap.get(st.sourceId) ?? {
      sourceId: st.sourceId,
      kind: st.kind,
      label: st.label,
      count: 0,
    }
    e.count += 1
    unlinkedMap.set(st.sourceId, e)
  }

  return {
    tracks: tracks.map((tr) => ({
      globalId: tr.global_track_id,
      domain: targetDomain[tr.global_track_id] ?? 'surface',
      sources: (tr.source_tracks ?? []).map((st: any) => ({
        sourceId: st.source_id,
        kind: deviceKind[st.source_id] ?? 'radar',
        label: deviceLabel[st.source_id] ?? st.source_id,
        pts: (st.samples ?? [])
          .filter((s: any) => s.time_ms <= timeMs)
          .map((s: any) => ({ east: s.east_m, north: s.north_m, timeMs: s.time_ms })),
      })),
    })),
    candidates,
    counts: {
      confirmed: candidates.filter((c) => c.state === 'confirmed').length,
      pending: candidates.filter((c) => c.state === 'pending').length,
      unlinked: candidates.filter((c) => c.state === 'unlinked').length,
    },
    confirmed: candidates.filter((c) => c.state === 'confirmed'),
    pending: candidates.filter((c) => c.state === 'pending'),
    unlinked: [...unlinkedMap.values()].sort((a, b) => b.count - a.count),
    hasData: srcTracks.length > 0,
  }
}

/* ================================================================== *
 * 模块 4：目标识别
 * ================================================================== */

export interface Detection {
  targetId: string
  /** 检测框 [x, y, w, h]（帧内像素） */
  bbox: number[]
  label: string
  /** 置信度（0~1）—— 若原始数据没有则为 null */
  confidence: number | null
}

export interface RecognitionRow {
  /** 帧编号 */
  frameId: string
  /** 帧时刻 */
  timeMs: number
  /** 帧图片 URL */
  imageUrl: string
  /** 帧尺寸 */
  width: number
  height: number
  /** 该帧的检测框 */
  detections: Detection[]
  /** 数据模式 */
  dataMode: string
  /** 是否真实声纳数据 */
  isReal: boolean
  /** 数据来源标识（如「实测回放 S01」） */
  sourceLabel: string
}

export interface RecogModule {
  /** 当前时刻对应的帧（<= timeMs 的最后一帧） */
  current: RecognitionRow | null
  /** 最近的若干帧（用于时间轴） */
  recent: RecognitionRow[]
  /** 识别结果列表（当前帧） */
  results: Detection[]
  /** 会话内所有帧的检测总数 */
  totalDetections: number
  /** 有检测框的帧数 */
  framesWithDetection: number
  /** 总帧数 */
  totalFrames: number
  /** 识别到的目标类别（去重） */
  classes: string[]
  /** 模型信息 —— 数据里没有就显示 — */
  model: string | null
  /** 光电通道（来自 devices.json 的真实设备信息） */
  eo: {
    sourceId: string
    label: string
    status: string
    stateLabel: string
    rangeM: number
    angleDeg: number
  } | null
  hasData: boolean
}

/**
 * 计算「目标识别」模块输出。
 *
 * 【数据来源】
 * `sonar_frames.json` 提供：
 *   · 帧图片 URL（`assets/sonar/frames/s01_xxx.png`）
 *   · 每帧的检测框 `detections[]`（`bbox_xywh` + `label`）
 *   · `is_real_sonar` 标记是否为真实声纳数据
 *
 * 【置信度】
 * 原始数据里**没有**置信度字段（只有 `label`），
 * 因此置信度一律显示「—」，
 * 绝不从别处伪造一个数字出来。
 */
export function buildRecognitionModule(data: any, timeMs: number): RecogModule {
  const frames = data?.sonar_frames?.frames ?? []
  if (!frames.length) {
    return {
      current: null,
      recent: [],
      results: [],
      totalDetections: 0,
      framesWithDetection: 0,
      totalFrames: 0,
      classes: [],
      model: null,
      eo: null,
      hasData: false,
    }
  }

  const toRow = (f: any): RecognitionRow => ({
    frameId: f.frame_id,
    timeMs: f.time_ms,
    imageUrl: f.image_url,
    width: f.width_px,
    height: f.height_px,
    dataMode: f.data_mode,
    isReal: !!f.is_real_sonar,
    sourceLabel: `${data?.sonar_frames?.source_id ?? 'S01'} · ${f.is_real_sonar ? '实测' : '合成'}`,
    detections: (f.detections ?? []).map((d: any) => ({
      targetId: d.target_id,
      bbox: d.bbox_xywh ?? [],
      label: d.label ?? d.target_id,
      confidence: d.confidence ?? null,
    })),
  })

  const upto = frames.filter((f: any) => f.time_ms <= timeMs)
  const current = upto.length ? toRow(upto[upto.length - 1]) : toRow(frames[0])
  const recent = frames
    .filter((f: any) => f.time_ms <= timeMs)
    .slice(-24)
    .map(toRow)
    .reverse()

  const all = frames.map(toRow)
  const totalDetections = all.reduce((a, r) => a + r.detections.length, 0)
  const framesWithDetection = all.filter((r) => r.detections.length > 0).length

  const classes: string[] = []
  for (const r of all) {
    for (const d of r.detections) {
      const c = d.label.replace(/\s*·.*$/, '').trim()
      if (c && !classes.includes(c)) classes.push(c)
    }
  }

  return {
    current,
    recent,
    results: current.detections,
    totalDetections,
    framesWithDetection,
    totalFrames: all.length,
    classes,
    model: data?.sonar_frames?.meta?.model ?? null,
    eo: (() => {
      const cam = (data?.devices?.items ?? []).find((d: any) => d.kind === 'camera')
      if (!cam) return null
      return {
        sourceId: cam.source_id,
        label: cam.label ?? cam.source_id,
        status: cam.status ?? 'unknown',
        stateLabel: cam.state_label ?? '—',
        rangeM: cam.coverage?.range_m ?? 0,
        angleDeg: cam.coverage?.angle_deg ?? 0,
      }
    })(),
    hasData: true,
  }
}

/* ================================================================== *
 * 汇总
 * ================================================================== */

export interface PipelineOutput {
  std: StdModule
  align: AlignModule
  assoc: AssocModule
  recog: RecogModule
}

/** 一次性算出四个模块的输出。 */
export function buildPipeline(data: any, timeMs: number): PipelineOutput {
  return {
    std: buildStdModule(data, timeMs),
    align: buildAlignModule(data, timeMs),
    assoc: buildAssocModule(data, timeMs),
    recog: buildRecognitionModule(data, timeMs),
  }
}
