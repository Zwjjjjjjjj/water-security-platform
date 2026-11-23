<script setup lang="ts">
/**
 * V2 水下监视场景层
 *
 * 【设计目标 —— 参照「水下目标平面态势」效果图】
 * 效果图的构成（自下而上）：
 *   1. **实景底图**：航拍视角的港湾全景（岸线、厂区、防波堤围成的水域）
 *   2. **声纳覆盖**：三个声纳站（S01/S02/S03）各发出一圈**同心圆**声纳波，
 *      波纹**由内向外扩散**（这是画面的动感来源）
 *   3. **水下目标**：黄色四角选框 + 深底黄字标签
 *   4. **角标**：右下角标注底图性质
 *
 * 【为什么单独做成组件】
 * 水下场景与水面场景是**两套完全不同的渲染体系**：
 *   · 水面 = 航拍底图 + ENU 配准的航迹 + 船体贴图
 *   · 水下 = 声纳覆盖 + 波纹动画 + 水下目标
 * 混在一起会让两边的坐标语义互相污染。
 * 因此水下走独立组件，只从 `devices` / `targets` 取需要的数据。
 *
 * 【声纳波为什么用 SVG 而不是位图】
 * 波纹是**同心圆**，本身就是几何图形：
 *   · 位图放大后会模糊
 *   · SVG 可以用 `stroke-dasharray` + `stroke-dashoffset` 做「圆弧推进」，
 *     再配合 `transform: scale()` 缩放，形成「由内向外扩散」的连续动画
 * 纯 CSS 动画即可，不需要 JS 逐帧计算，性能好。
 *
 * 【坐标锚定 —— 缩放时为什么不会跑偏】
 * 声纳站的数据只有归一化的 `view_u / view_v`（0~1），
 * 没有 ENU 米制坐标。若直接乘以容器宽高，
 * 缩放/平移时站位会**钉死在屏幕上**，与底图脱节。
 *
 * 正确做法：把 `view_u / view_v` 当作**底图图片自身坐标系里的归一化位置**，
 * 先换算成图片原生像素，再经 `obliqueImageToContainer()` 换算到容器像素。
 * 这条链路与底图渲染（`SceneObliqueV2`）**完全一致**，
 * 因此底图怎么缩放平移，声纳站就怎么跟着动。
 */
import { computed } from 'vue'
import { computeObliqueFit, obliqueImageToContainer, OBLIQUE_NAT_W, OBLIQUE_NAT_H } from './oblique-fit'

const props = defineProps({
  /** 快照（含 devices / targets / sonar_frame） */
  snapshot: { type: Object, default: () => ({}) as any },
  /** 当前选中的目标 */
  selectedTargetId: { type: String, default: '' },
  /** 图层开关 */
  layers: { type: Object, default: () => ({ coverage: true, source_tracks: true, fused_tracks: true }) },
  /** 容器尺寸（容器像素） */
  chartW: { type: Number, default: 1920 },
  chartH: { type: Number, default: 870 },
  /** 当前 ENU 视口（米）—— 缩放/平移时变化 */
  xMin: { type: Number, default: -2600 },
  xMax: { type: Number, default: 1000 },
  yMin: { type: Number, default: 3000 },
  yMax: { type: Number, default: 5000 },
  /** 坐标轴绘图区边距（像素） */
  gridLeft: { type: Number, default: 0 },
  gridRight: { type: Number, default: 0 },
  gridTop: { type: Number, default: 0 },
  gridBottom: { type: Number, default: 0 },
  /** 播放是否暂停（暂停时冻结动画） */
  paused: { type: Boolean, default: false },
  /**
   * 当前回放时刻（毫秒）。
   *
   * 【为什么水下目标必须绑定这个时刻】
   * 水下航迹折线由 `history` 绘制 —— 也就是**当前时刻之前**的实测航迹。
   * 若目标符号用独立时钟沿折线自由行进，就会出现：
   *   · 符号跑到已绘制航迹之外（前面的轨迹、后面的孤点）；
   *   · 播放进度与符号位置对不上。
   *
   * 有了 `timeMs`，符号位置就由「当前时刻对应的航迹点」唯一确定，
   * 永远落在自己那条已绘制的轨迹末端。
   */
  timeMs: { type: Number, default: 0 },
})

/**
 * 底图映射参数。
 *
 * 与 `SceneObliqueV2` 用的是**同一个函数、同一组入参**，
 * 所以声纳站的定位与底图永远一致 —— 缩放时同步跟随。
 */
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
    /* 底图锚在容器原点铺满，声纳层必须用同一个锚点 */
    anchorX: 0,
    anchorY: 0,
  }),
)

/**
 * 归一化坐标（底图图片坐标系 0~1）→ 容器像素。
 *
 * 走的是「图片原生像素 → 容器像素」这条链，
 * 与底图 `<image>` 的 `transform` 用的是同一个 `fit`，
 * 因此天然跟随缩放平移。
 */
function normalizedToContainer(u: number, v: number): [number, number] {
  const imgX = u * OBLIQUE_NAT_W
  const imgY = v * OBLIQUE_NAT_H
  return obliqueImageToContainer(fit.value, imgX, imgY)
}

/** 设备列表 */
const devices = computed<any[]>(() => (props.snapshot as any)?.devices || [])

/**
 * 声纳站列表。
 *
 * 数据来源：`devices` 中 `kind === 'sonar'` 的项。
 * 每个站可能带 `display_nodes`（演示用的多个显示节点），
 * 站位用 `view_u / view_v`（0~1 的归一化坐标，与效果图一致）。
 */
const sonarStations = computed(() => {
  return devices.value
    .filter((d) => d.kind === 'sonar')
    .map((d) => {
      /* 优先用 display_nodes（演示有三个节点 S01-1/2/3）；
       * 没有则退化为站位本身。 */
      const nodes = (d.display_nodes || []).length
        ? d.display_nodes
        : [{ id: d.source_id, view_u: d.view_u, view_v: d.view_v }]
      return {
        id: d.source_id,
        label: d.label || d.source_id,
        status: d.status,
        rangeM: d.coverage?.range_m ?? 60,
        nodes: nodes.map((n: any) => {
          /* 归一化坐标 → 容器像素（跟随缩放平移） */
          const [x, y] = normalizedToContainer(n.view_u ?? d.view_u ?? 0.5, n.view_v ?? d.view_v ?? 0.5)
          return { id: n.id, x, y }
        }),
      }
    })
    .filter((s) => s.nodes.length > 0)
})

/**
 * 扇形波束的半径（容器像素）。
 *
 * 【量纲换算 —— 这里最容易出错】
 * `coverage.range_m` 是**米**，容器像素是**像素**，两者不能直接混用。
 *
 * 声纳的有效量程一般只有几十到几百米，
 * 而屏幕上一整个海湾是几千米 —— 若按真实比例画，扇面会小得看不见。
 *
 * 因此这里采用「**视口比例**」而非「真实比例」：
 *   扇面半径 = 容器对角线的固定百分比
 * 这样扇面大小与屏幕分辨率无关，视觉效果稳定。
 * 真实量程以文字标注在图上（见 stationLabel）。
 */
/**
 * 声纳波的半径（容器像素）。
 *
 * 【量纲换算 —— 这里最容易出错】
 * `coverage.range_m` 是**米**，容器像素是**像素**，两者不能直接混用。
 *
 * 声纳的有效量程一般只有几十到几百米，
 * 而屏幕上一整个海湾是几千米 —— 若按真实比例画，波圈会小得看不见。
 *
 * 因此这里采用「**底图图片像素比例**」：
 *   半径 = 底图图片宽度 × 固定系数 × 底图当前缩放比 r
 * 这样波圈与底图里的岸线尺度成正比，
 * **缩放时波圈跟着底图一起变大变小**，而不是在屏幕上保持固定大小。
 */
const waveRadius = computed(() => {
  /* 底图图片宽度（原生像素）→ 容器像素 的比例 */
  const r = fit.value.r
  /* 波圈半径约占底图宽度的 1/6 */
  return OBLIQUE_NAT_W * 0.17 * r
})

/**
 * 画一个**贴在水面上**的波纹路径（透视椭圆）。
 *
 * 【为什么是椭圆而不是正圆】
 * 声纳波是在**水面这个平面上**传播的物理圆。
 * 但底图 `campus_oblique_v3.png` 是**斜俯视**视角 ——
 * 水平面上的圆，在这个投影下必然被压扁成椭圆，
 * 长轴沿水平方向，短轴沿画面纵向。
 *
 * 如果画成正圆，就会出现用户反馈的「角度不对」：
 * 波纹看起来像竖起来的圆环立在水面上，而不是躺在水面上。
 *
 * 【压缩比怎么定】
 * 底图的斜视角在 y∈[40,1000] 区间内，
 * 纵向 960 个图片像素对应 2200 米 ENU；
 * 横向 1536 个图片像素对应 3600 米 ENU。
 * 两轴的「米/像素」不同，说明底图本身就带纵向压缩。
 *
 * 这里取 **ry / rx = 0.46**：
 *   · 比 1 小很多 —— 明确表达「躺平」，符合斜俯视观感；
 *   · 不取更小的值 —— 否则在远端（画面上部）会压成一条线；
 *   · 与底图厂区边缘线的倾角观感一致。
 *
 * @param cx 圆心 X（容器像素）
 * @param cy 圆心 Y（容器像素）
 * @param r  长半轴半径（容器像素）
 * @param ryRatio 短轴/长轴比例
 */
function surfaceEllipsePath(cx: number, cy: number, r: number, ryRatio = 0.46): string {
  const rx = r
  const ry = r * ryRatio
  const cyy = cy.toFixed(1)
  /* 左右两个端点（长轴端点），两段半椭圆拼成整圈 */
  const l = (cx - rx).toFixed(1)
  const rr = (cx + rx).toFixed(1)
  return (
    `M ${l} ${cyy} A ${rx.toFixed(1)} ${ry.toFixed(1)} 0 1 0 ${rr} ${cyy}` +
    ` A ${rx.toFixed(1)} ${ry.toFixed(1)} 0 1 0 ${l} ${cyy} Z`
  )
}

/**
 * 水下目标 · 航迹 · 深度分层
 *
 * 【为什么水下要有自己的航迹】
 * 水面航迹来自 AIS/雷达的**实测 ENU**；
 * 水下航迹来自声纳浮标的**水下目标量测**（含深度），
 * 二者数据来源、误差特性、可视化语义都不同，不能共用一套渲染。
 *
 * 【航迹坐标的选取】
 * 水下目标的历史点带 `view_u / view_v`（底图归一化），
 * 与水面目标一样走 `normalizedToContainer()` → 跟随缩放平移。
 *
 * 【深度如何表达 —— 三维压成二维的约定】
 * 底图是斜俯视的，天然带纵向压缩（ry/rx ≈ 0.46）。
 * 若再把深度也压进同一个方向，水下目标会与水面目标混淆。
 * 因此采用**独立的「深度偏移」轴**：
 *   · 深度越大 → 目标在画面上**越往上**（越靠近水面层）
 *   · 深度越浅 → 越往下（越靠近海底层）
 * 这与「俯视看下去，越深的目标被遮得越靠上」的实际观感一致。
 *
 * 偏移量按深度差做**非线性映射**（平方根），
 * 因为深度范围是 0~30m，而线性映射会让深浅目标挤在一起。
 */
interface UnderSample {
  time_ms: number
  u: number
  v: number
  depth: number
  speed: number
  course: number
  /** 东向坐标（米）—— 用于计算真实航迹长度与速度 */
  east: number
  /** 北向坐标（米） */
  north: number
}

interface UnderTrack {
  id: string
  name: string
  type: string
  color: string
  samples: UnderSample[]
  /**
   * 已走过的航迹点（容器像素）。
   *
   * 【关键 —— 只包含「当前回放时刻之前」的采样点】
   * 目标还没走过的路段**不能提前画出来**，
   * 否则画面就变成「整条路线先摊在图上，符号再从起点慢慢爬」，
   * 完全失去了实时感。
   * 航迹必须随回放**逐段生长**，走一段才留一段。
   */
  drawnPts: { x: number; y: number; d: number }[]
  /** 已绘制部分各点的时标（毫秒），与 `drawnPts` 一一对应 */
  drawnSamplesTime: number[]
  /** 绘制用的折线（容器像素）—— 只画已走过的部分 */
  polyline: string
  /** 折线上的点（容器像素），用于计算运动方向 */
  pts: { x: number; y: number; d: number }[]
  depthMin: number
  depthMax: number
  /**
   * 真实平均速度（米/秒）。
   *
   * 【由 ENU 米制航迹长度 ÷ 时间跨度算出】
   * 这是水下目标**唯一可信的速度来源** ——
   * `speed_mps` 字段在部分样本里为空，
   * 而航迹的几何与时标是始终存在的。
   */
  realSpeedMps: number
  /**
   * 该航迹的**局部比例**（容器像素 / 米）。
   *
   * 【为什么要按航迹各自标定，而不是全局常数】
   * 底图 `campus_oblique_v3.png` 是概念场景图，
   * 与实测 ENU **没有地理配准** ——
   * 同一 ENU 距离在图上对应的像素数会随位置变化。
   * 所以全局常数必然有一处错、处处不准。
   *
   * 而「本航迹的像素长 ÷ 本航迹的米长」是**自洽**的：
   * 它保证了这条航迹上「画面位移」与「真实位移」的比例一致，
   * 于是「画面速度 = 真实速度」在这条航迹上严格成立。
   */
  pxPerMeter: number
  /** 航迹的容器像素总长 */
  polylinePx: number
}

/** 水下目标的配色（按 id 稳定分配） */
const UNDER_COLORS: Record<string, string> = {
  U001: '#ffd54a',
  U002: '#3ef3a3',
  U003: '#4da6ff',
  U004: '#ff8a5c',
}

/** 深度 → 屏幕纵向偏移（容器像素）的映射参数 */
const DEPTH_SPAN_M = 32 // 深度量程（米）
const DEPTH_OFFSET_PX = 42 // 满量程对应的纵向偏移（容器像素）

/**
 * 深度 → 纵向偏移（容器像素）。
 *
 * 深度 0m（贴着水面）→ 偏移 0；
 * 深度 DEPTH_SPAN_M → 偏移 -DEPTH_OFFSET_PX（往画面上方）。
 * 用平方根压缩，让浅水区的分辨率更高（那里目标更密集）。
 */
function depthOffset(depth: number): number {
  const d = Math.max(0, Math.min(DEPTH_SPAN_M, depth || 0))
  return -Math.sqrt(d / DEPTH_SPAN_M) * DEPTH_OFFSET_PX * fit.value.r
}

/**
 * 采集目标航迹点（含深度、速度、航向）。
 *
 * 【为什么优先用 `full_track` 而不是 `history`】
 * `history` 是「当前回放时刻之前」的实测航迹，只有一段；
 * `full_track` 是该目标的**完整**航迹。
 *
 * 水下场景要表达的是「潜航器沿航线巡弋」：
 *   · 航迹折线画完整航线（观众看到的是完整路径）；
 *   · 目标符号按当前回放时刻在航线上的位置绘制（符号随播放推进）。
 *
 * 若两者都只用 `history`：
 *   折线只有当前时刻之前的一小段，符号又被钉在折线末端，
 *   画面就退化成「一小段轨迹 + 旁边一个不动的素材」。
 */
function collectSamples(t: any): UnderSample[] {
  const raw: any[] = t.full_track?.length ? t.full_track : t.history || []
  return raw
    .filter((p: any) => p && p.view_u != null && p.view_v != null)
    .map((p: any) => ({
      time_ms: p.time_ms ?? 0,
      u: p.view_u,
      v: p.view_v,
      depth: p.depth_m ?? t.depth_m ?? 0,
      speed: p.speed_mps ?? 0,
      course: p.course_deg ?? 0,
      east: Number.isFinite(p.east_m) ? p.east_m : 0,
      north: Number.isFinite(p.north_m) ? p.north_m : 0,
    }))
}

/**
 * 截断航迹：只保留「当前回放时刻之前（及此刻）」已走过的部分。
 *
 * 【为什么必须截断 —— 这是「实时感」的核心】
 *
 * 目标还没走过的路段如果提前画出来，画面就变成
 * 「整条路线先摊在图上，符号再从起点慢慢爬」——
 * 用户一眼就能看出轨迹是**预先录好的**，而不是实时发生的。
 *
 * 需求是「走过之后才会留下轨迹」，所以：
 *   · 采样点时间 ≤ 当前回放时刻 → 画；
 *   · 采样点时间 >  当前回放时刻 → 不画（还没走到）；
 *   · 恰好落在当前时刻的那个点 → 画（目标此刻就在那儿）。
 *
 * 这样航迹就会随着回放**逐段生长**，与水面场景
 * （`ChartSceneV2` 用 `arcRatioAtTime` 截断航线）的做法完全一致。
 */
function truncateByTime(
  samples: UnderSample[],
  timeMs: number,
): { samples: UnderSample[]; cutIndex: number; partial: UnderSample | null } {
  if (!samples.length) return { samples: [], cutIndex: -1, partial: null }

  /* 找到第一个晚于当前时刻的采样点 */
  let i = 0
  while (i < samples.length && samples[i].time_ms <= timeMs) i++
  const cutIndex = i

  /* 全部都在当前时刻之前 —— 航迹已画完 */
  if (cutIndex >= samples.length) return { samples, cutIndex, partial: null }

  /**
   * 落在两帧之间的部分。
   *
   * 采样间隔可能有十几秒，若只按整点截断，
   * 目标符号会明显「滞后」于它的轨迹末端（符号在轨迹末端前方）。
   * 这里在「最后一个已走过点」与「第一个未到达点」之间线性插值，
   * 补一个当前时刻的虚拟点，让轨迹末端与符号位置严丝合缝。
   */
  const before = samples[cutIndex - 1]
  const after = samples[cutIndex]
  if (!before) return { samples: samples.slice(0, cutIndex), cutIndex, partial: null }

  const span = after.time_ms - before.time_ms
  const f = span > 0 ? Math.max(0, Math.min(1, (timeMs - before.time_ms) / span)) : 0
  const partial: UnderSample = {
    time_ms: timeMs,
    u: before.u + (after.u - before.u) * f,
    v: before.v + (after.v - before.v) * f,
    depth: before.depth + (after.depth - before.depth) * f,
    speed: before.speed + (after.speed - before.speed) * f,
    course: before.course,
    east: before.east + (after.east - before.east) * f,
    north: before.north + (after.north - before.north) * f,
  }
  return { samples: [...samples.slice(0, cutIndex), partial], cutIndex, partial }
}

/** 水下目标（含航迹）。 */
const underTracks = computed<UnderTrack[]>(() => {
  const list = (props.snapshot as any)?.targets || []
  const nowMs = props.timeMs
  return list
    .filter((t: any) => t.domain === 'underwater')
    .map((t: any) => {
      const allSamples = collectSamples(t)
      const color = UNDER_COLORS[t.global_track_id] || '#ffd54a'

      /**
       * 【只保留已走过的采样点】
       * `samples` 保留**完整**采样点集（算弧长、深度范围、速度都要用），
       * `drawnPts` / `polyline` 才是真正画到屏幕上的那段。
       */
      const { samples: drawnSamples } = truncateByTime(allSamples, nowMs)

      /* 折线点：归一化 → 容器像素，并叠加深度偏移 */
      const pts = allSamples.map((s) => {
        const [x, y] = normalizedToContainer(s.u, s.v)
        return { x, y: y + depthOffset(s.depth), d: s.depth }
      })

      /* 已走过部分的折线点 */
      const drawnPts = drawnSamples.map((s) => {
        const [x, y] = normalizedToContainer(s.u, s.v)
        return { x, y: y + depthOffset(s.depth), d: s.depth }
      })

      /* 折线路径字符串 —— 只画已走过的部分 */
      let polyline = ''
      for (let i = 0; i < drawnPts.length; i++) {
        polyline += `${i === 0 ? 'M' : 'L'} ${drawnPts[i].x.toFixed(1)} ${drawnPts[i].y.toFixed(1)} `
      }

      /* 深度范围 / 真实速度：统计用**完整**采样点集（与画面截断无关） */
      const depths = allSamples.map((s) => s.depth)

      /**
       * 【真实速度标定】
       * 由 ENU 米制航迹长度与时间跨度算出真实平均速度（m/s）。
       * 这是唯一可信的速度来源 —— `speed_mps` 在部分样本里是空的。
       */
      let meters = 0
      for (let i = 1; i < allSamples.length; i++) {
        meters += Math.hypot(
          allSamples[i].east - allSamples[i - 1].east,
          allSamples[i].north - allSamples[i - 1].north,
        )
      }
      const spanS = (allSamples[allSamples.length - 1].time_ms - allSamples[0].time_ms) / 1000
      const realSpeedMps = spanS > 0 ? meters / spanS : 0

      /* 折线的容器像素总长 */
      let polylinePx = 0
      for (let i = 1; i < pts.length; i++) {
        polylinePx += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
      }

      /**
       * 【局部比例】
       * 该航迹的像素长 ÷ 米长。
       * 底图与 ENU 没有地理配准，全局常数必然不准；
       * 按航迹各自标定则「画面位移 : 真实位移」的比例自洽。
       */
      const pxPerMeter = meters > 0.5 ? polylinePx / meters : 0

      return {
        id: t.global_track_id,
        name: t.name,
        type: t.target_type || '水下目标',
        color,
        samples: allSamples,
        drawnPts,
        drawnSamplesTime: drawnSamples.map((s) => s.time_ms),
        polyline,
        pts,
        depthMin: depths.length ? Math.min(...depths) : 0,
        depthMax: depths.length ? Math.max(...depths) : 0,
        realSpeedMps,
        pxPerMeter,
        polylinePx,
      }
    })
    .filter((tr) => tr.samples.length >= 2)
})

/**
 * 水下目标符号的**当前显示位置**。
 *
 * 【为什么不用「当前回放时刻」的插值点】
 * 快照里的 `t.east_m / t.view_u / t.view_v` 已经是当前时刻的插值结果，
 * 直接用它会让目标「跳」到最新点，而**航迹尾迹**留在历史点 ——
 * 两者之间没有连续性，看起来像瞬移。
 *
 * 正确做法：目标沿**已绘制的航迹**按时间比例连续前进。
 * 这样「航迹是路径、符号在路径上按回放时刻推进、两者严丝合缝」。
 *
 * 【重要 —— 位置完全由 `props.timeMs` 决定】
 * 早期版本用一个与回放无关的自由时钟推进相位，
 * 结果是「符号位置」与「已绘制轨迹」互相脱节：
 *   · 折线只有当前时刻之前的一小段（history），符号却在整条折线上乱跑；
 *   · 播放进度与符号位置对不上；
 *   · 用户看到的就是「素材只是小范围晃动、没跟着轨迹走，
 *     后半段只剩轨迹没有素材」。
 *
 * 现在位置由回放时刻唯一确定：
 *   · 折线画完整航线（`full_track`）；
 *   · 符号落在该时刻对应的航线位置；
 *   · 拖动进度条，符号与轨迹同步变化。
 *
 * 因此这里**不再需要**独立的动画时钟 —— 位置是回放时刻的纯函数。
 */

/**
 * 目标在航迹上的行进位置（0~1 的弧长比例）。
 *
 * 【关键改动 —— 从「自由时钟」改为「绑定回放时刻」】
 *
 * 旧实现用一个与回放无关的连续时钟 `animT` 推进相位，
 * 让目标「永远在动」。但这样做的后果是：
 *   1. 航迹折线画的是 `history`（当前时刻之前的实测航迹），
 *      而符号沿**整条**折线自由行进 —— 符号会跑到已绘制轨迹之外，
 *      画面上出现「前面一段轨迹，后面一个孤零零的符号」；
 *   2. 播放进度与符号位置完全对不上。
 *
 * 用户反馈的正是这个现象：素材只是小范围移动、没跟着轨迹走，
 * 后半段只剩轨迹没有素材。
 *
 * 【现在的做法】
 * 目标位置由「当前回放时刻在航迹上的投影」唯一确定：
 *   时间比例 = (当前时刻 − 航迹起点时刻) ÷ (航迹终点时刻 − 航迹起点时刻)
 * 再按弧长换算成位置。
 *
 * 于是天然满足三件事：
 *   · 符号永远落在**自己那条已绘制的轨迹**上；
 *   · 符号的速度 = 真实速度（因为时间与弧长同源）；
 *   · 拖动进度条时，符号与轨迹同步变化。
 */
function progressOf(tr: UnderTrack): number {
  /**
   * 【必须在「已绘制的那段」上算比例】
   *
   * 旧实现用**完整**采样点集算位置，而折线只画了一部分 ——
   * 于是符号会跑到「还没画出来」的轨迹上，
   * 画面上就是「孤零零一个符号，前面还有一段没长出来的轨迹」。
   *
   * 现在折线与位置**同源**：
   *   · 折线 = 已走过的点（`drawnPts`）；
   *   · 符号 = 已走过的那段上的末端位置。
   * 两者永远严丝合缝。
   */
  const n = tr.drawnPts.length
  if (n < 2) return 0

  const t0 = tr.samples[0].time_ms
  const t1 = tr.samples[tr.samples.length - 1].time_ms
  if (t1 <= t0) return 0

  /* 当前回放时刻在航迹时间跨度中的位置（0~1） */
  const timeRatio = Math.max(0, Math.min(1, (props.timeMs - t0) / (t1 - t0)))

  /* 时间比例 → 弧长比例。
   * 航迹的弧长在各段时间上并不均匀（采样间隔不等），
   * 因此不能直接用时间比例当弧长比例，需要按时间分段折算。 */
  const cum: number[] = [0]
  for (let i = 1; i < tr.drawnPts.length; i++) {
    cum.push(
      cum[i - 1] +
        Math.hypot(
          tr.drawnPts[i].x - tr.drawnPts[i - 1].x,
          tr.drawnPts[i].y - tr.drawnPts[i - 1].y,
        ),
    )
  }
  const total = cum[cum.length - 1] || 1

  const targetMs = t0 + timeRatio * (t1 - t0)
  /* 找到 targetMs 落在哪一段（按已绘制部分的采样时标） */
  let i = 0
  while (i < tr.drawnPts.length - 2 && tr.drawnSamplesTime[i + 1] < targetMs) i++
  const segT0 = tr.drawnSamplesTime[i]
  const segT1 = tr.drawnSamplesTime[i + 1]
  const f = segT1 > segT0 ? Math.max(0, Math.min(1, (targetMs - segT0) / (segT1 - segT0))) : 0

  return Math.max(0, Math.min(1, (cum[i] + (cum[i + 1] - cum[i]) * f) / total))
}

/** 在折线上按比例取点（含切线方向） */
function pointAt(tr: UnderTrack, ratio: number): {
  x: number
  y: number
  depth: number
  course: number
  idx: number
} {
  /* 用「已绘制的那段」取点 —— 与折线、进度比例同源 */
  const pts = tr.drawnPts
  if (!pts.length) return { x: 0, y: 0, depth: 0, course: 0, idx: 0 }
  if (pts.length === 1) return { x: pts[0].x, y: pts[0].y, depth: pts[0].d, course: 0, idx: 0 }

  /* 累计弧长 */
  const cum: number[] = [0]
  for (let i = 1; i < pts.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y))
  }
  const total = cum[cum.length - 1] || 1
  const s = Math.max(0, Math.min(1, ratio)) * total

  let i = 0
  while (i < cum.length - 2 && cum[i + 1] < s) i++
  const segLen = cum[i + 1] - cum[i]
  const t = segLen > 0 ? (s - cum[i]) / segLen : 0
  const a = pts[i]
  const b = pts[i + 1]
  return {
    x: a.x + (b.x - a.x) * t,
    y: a.y + (b.y - a.y) * t,
    depth: a.d + (b.d - a.d) * t,
    /* 航向：屏幕上 y 向下，取负号得到几何角 */
    course: (Math.atan2(-(b.y - a.y), b.x - a.x) * 180) / Math.PI,
    idx: i,
  }
}

/**
 * 当前时刻正在显示的水下目标（含位置、航向、深度、尾迹）。
 */
const underTargets = computed(() => {
  return underTracks.value.map((tr) => {
    const ratio = progressOf(tr)
    const cur = pointAt(tr, ratio)

    /* 尾迹：从当前点往回取若干历史点 */
    const trailLen = 14
    const trail: { x: number; y: number }[] = []
    const startIdx = Math.max(0, cur.idx - trailLen)
    for (let i = startIdx; i <= cur.idx; i++) {
      trail.push({ x: tr.drawnPts[i].x, y: tr.drawnPts[i].y })
    }
    trail.push({ x: cur.x, y: cur.y })

    /* 目标贴图尺寸（与底图同比例缩放）
     *
     * 【为什么系数压到 0.24】
     * 潜水员贴图是「俯视统一视角的人物」，
     * 在斜俯视底图上按原始设计尺寸画会显得过大 ——
     * 一个蛙人比旁边的万吨货轮还醒目，完全抢了主目标的戏。
     *
     * 水下目标在这里是「次要信息」：
     * 主画面主角是水面船舶与核电厂区，
     * 水下目标只需要「看得见、在动、有深度」即可。
     *
     * 系数 0.24 让蛙人在 1920 宽的画面里约 40px，
     * 与水面船体的视觉重量大致相当，不喧宾夺主。
     */
    const raw = (props.snapshot as any)?.targets?.find(
      (t: any) => t.global_track_id === tr.id,
    )?.sprite_width_design_px || 46
    const size = Math.max(8, raw * 0.24 * fit.value.r)

    return {
      id: tr.id,
      name: tr.name,
      type: tr.type,
      color: tr.color,
      x: cur.x,
      y: cur.y,
      depth: cur.depth,
      course: cur.course,
      selected: tr.id === props.selectedTargetId,
      sprite: (props.snapshot as any)?.targets?.find((t: any) => t.global_track_id === tr.id)?.sprite
        ? `/water-security/${(props.snapshot as any).targets.find((t: any) => t.global_track_id === tr.id).sprite}`
        : '',
      size,
      trail,
      track: tr,
    }
  })
})

/**
 * 声纳探测联动：目标是否落在某个声纳站的覆盖圈内。
 *
 * 【为什么需要】
 * 水下场景的核心叙事是「声纳发现了水下目标」。
 * 若目标只是静静漂着，声纳波与目标之间没有因果关系，画面就散了。
 *
 * 因此当目标进入某个声纳节点的覆盖圈时：
 *   · 目标符号高亮（加一层扫描环）；
 *   · 画一条「声纳站 → 目标」的测向连线。
 * 这就复现了真实的「主动声呐测向」过程。
 */
const sonarDetections = computed(() => {
  const out: { station: { x: number; y: number; id: string }; target: any; range: number }[] = []
  const stations = sonarStations.value
  for (const st of stations) {
    for (const n of st.nodes) {
      for (const t of underTargets.value) {
        const d = Math.hypot(t.x - n.x, t.y - n.y)
        /* 覆盖半径与声纳波圈一致（波圈 = 覆盖范围的视觉化） */
        if (d <= waveRadius.value * 0.98) {
          out.push({ station: { x: n.x, y: n.y, id: n.id }, target: t, range: d })
        }
      }
    }
  }
  return out
})

/**
 * 声纳帧里的检测框。
 *
 * `sonar_frame.detections` 给出的是**帧内像素坐标**（帧自身 480×360），
 * 需要按比例映射到容器像素。
 * 这块信息在效果图右侧的「回波示意图」里会用到。
 */
const sonarFrame = computed(() => (props.snapshot as any)?.sonar_frame || null)

const emit = defineEmits(['select-target'])
</script>

<template>
  <!--
    声纳波纹层。
    独立成一个 SVG（而非塞进底图 SVG），是为了能用 CSS class 控制动画，
    并且让底图与波纹各自独立缩放（不影响底图的像素级对齐）。
  -->
  <svg
    class="ws-under-scene"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
    :data-paused="paused ? 'true' : 'false'"
  >
    <defs>
      <!--
        水下色调滤镜。
        效果图里的水下场景整体偏**深蓝青**，和水面场景的暖色实景明显不同。
        这里用一层半透明青蓝 + 暗角来实现这种「水下压深」的感觉。
      -->
      <linearGradient id="uTint" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#0a3d55" stop-opacity="0.42" />
        <stop offset="0.5" stop-color="#062a3d" stop-opacity="0.3" />
        <stop offset="1" stop-color="#021a2b" stop-opacity="0.5" />
      </linearGradient>
      <radialGradient id="uVignette" cx="50%" cy="46%" r="72%">
        <stop offset="0.55" stop-color="#000000" stop-opacity="0" />
        <stop offset="1" stop-color="#000a14" stop-opacity="0.62" />
      </radialGradient>
      <!-- 声纳波束渐变：从站位处的亮青 → 边缘透明 -->
      <radialGradient id="uBeam" cx="50%" cy="50%" r="50%">
        <stop offset="0" stop-color="#22f3ff" stop-opacity="0.42" />
        <stop offset="0.45" stop-color="#0fd0e8" stop-opacity="0.2" />
        <stop offset="1" stop-color="#00a8c8" stop-opacity="0" />
      </radialGradient>
      <!-- 弧状波纹渐变 -->
      <linearGradient id="uWave" x1="0" y1="0" x2="1" y2="0.2">
        <stop offset="0" stop-color="#7ffcff" stop-opacity="0.9" />
        <stop offset="1" stop-color="#19c8e6" stop-opacity="0.25" />
      </linearGradient>
      <!--
        水下航迹的「尾段渐隐」渐变。

        【为什么要渐变而不是实线】
        水下目标的历史航迹应该**越靠近当前时刻越亮**，
        越久远的部分越淡（与水面航迹的 6 段淡入思路一致）。
        但 SVG 的 `stroke` 渐变是**沿路径的线性渐变**，
        需要知道路径的起止方向 —— 这里用 `gradientUnits="userSpaceOnUse"`，
        把渐变轴对准路径的首尾两点，起点透明、终点最亮。
      -->
      <linearGradient
        v-for="tr in underTracks"
        :id="'uTrackGrad-' + tr.id"
        :key="'grad-' + tr.id"
        gradientUnits="userSpaceOnUse"
        :x1="tr.drawnPts[0]?.x ?? 0"
        :y1="tr.drawnPts[0]?.y ?? 0"
        :x2="tr.drawnPts[tr.drawnPts.length - 1]?.x ?? 0"
        :y2="tr.drawnPts[tr.drawnPts.length - 1]?.y ?? 0"
      >
        <stop offset="0" :stop-color="tr.color" stop-opacity="0.06" />
        <stop offset="0.45" :stop-color="tr.color" stop-opacity="0.34" />
        <stop offset="1" :stop-color="tr.color" stop-opacity="0.92" />
      </linearGradient>
    </defs>

    <!-- ===== 0. 水下色调：把实景底图压成「水下」的深蓝青 ===== -->
    <rect x="0" y="0" :width="chartW" :height="chartH" fill="url(#uTint)" class="ws-under-tint" />
    <rect x="0" y="0" :width="chartW" :height="chartH" fill="url(#uVignette)" class="ws-under-vignette" />

    <!--
      ===== 1. 水下航迹层 =====

      【绘制顺序 —— 为什么在声纳波之下】
      航迹是「水下目标走过的路径」，属于**被观测对象**；
      声纳波是「探测手段」，属于**观测工具**。
      工具画在对象之上，才符合「波扫过航迹」的视觉逻辑。
    -->
    <g v-if="layers.source_tracks || layers.fused_tracks" class="ws-under-track-layer">
      <g v-for="tr in underTracks" :key="'tr-' + tr.id" class="ws-under-track">
        <!--
          航迹主线：沿历史轨迹的折线，尾端最亮。

          画两遍：
            · 底层宽而暗（辉光底）
            · 上层窄而亮（主线）
          这样在深蓝底图上既有存在感，又不会像荧光笔一样刺眼。
        -->
        <path
          :d="tr.polyline"
          fill="none"
          :stroke="tr.color"
          stroke-width="5"
          stroke-opacity="0.14"
          stroke-linecap="round"
          stroke-linejoin="round"
          class="ws-under-track-glow"
        />
        <path
          :d="tr.polyline"
          fill="none"
          :stroke="`url(#uTrackGrad-${tr.id})`"
          stroke-width="1.9"
          stroke-linecap="round"
          stroke-linejoin="round"
          class="ws-under-track-line"
        />
        <!--
          流动虚线：叠加在主线上的「推进光点」效果。

          【动画原理】
          `stroke-dashoffset` 递减 → 虚线沿路径向前流动，
          视觉上就是「目标正在朝这个方向巡弋」。
          这比单纯的静态虚线更有「活着」的感觉。
        -->
        <path
          :d="tr.polyline"
          fill="none"
          :stroke="tr.color"
          stroke-width="2.4"
          stroke-opacity="0.75"
          stroke-linecap="round"
          stroke-dasharray="9 26"
          class="ws-under-track-flow"
        />
        <!-- 历史测点：小圆点，间距约每 3 个采样点一个 -->
        <g class="ws-under-track-nodes">
          <circle
            v-for="(p, i) in tr.drawnPts"
            :key="'nd-' + tr.id + '-' + i"
            v-show="i % 3 === 0"
            :cx="p.x"
            :cy="p.y"
            r="1.5"
            :fill="tr.color"
            fill-opacity="0.55"
          />
        </g>
      </g>
    </g>

    <!-- ===== 1. 声纳覆盖：贴水面的椭圆波束 + 同心椭圆扩散波纹 ===== -->
    <g v-if="layers.coverage" class="ws-sonar-layer">
      <g v-for="st in sonarStations" :key="st.id" class="ws-sonar-station">
        <g v-for="n in st.nodes" :key="n.id" class="ws-sonar-node" :style="{ transformOrigin: `${n.x}px ${n.y}px` }">
          <!--
            波束本体（静止底色）：以站位为圆心的**贴面椭圆**。
            声纳是全向的，波纹向四周扩散，不是朝某个方向的扇形；
            但它躺在**水面这个斜面上**，所以投影后是椭圆而非正圆。

            椭圆的长轴半径随底图缩放比变化，因此缩放时波圈跟着底图一起缩放。
          -->
          <path
            :d="surfaceEllipsePath(n.x, n.y, waveRadius)"
            fill="url(#uBeam)"
            class="ws-sonar-beam"
          />
          <!--
            量程刻度环：静态的同心椭圆，标出 1/3、2/3、满量程三档距离。
            相当于 PPI 显示器上的距离刻度环，让「波纹扩散」有参照。
          -->
          <g class="ws-sonar-rings">
            <path
              v-for="k in 3"
              :key="'ring' + k"
              :d="surfaceEllipsePath(n.x, n.y, waveRadius * (0.34 + k * 0.3))"
              fill="none"
              stroke="#5fe8f7"
              stroke-opacity="0.3"
              stroke-width="1"
              stroke-dasharray="4 7"
            />
          </g>
          <!--
            扩散波纹：三道同心椭圆，各自独立延迟做「由内向外」扩散。

            【为什么每道波纹单独一个 <g>】
            CSS 的 `transform: scale()` 默认以元素**自身包围盒**为中心。
            对一个圆来说，包围盒中心恰好就是圆心 ——
            但对 `<path>` 组成的圆，包围盒中心可能与圆心差半个像素，
            多道波纹叠加后会出现轻微抖动。

            因此每道波纹各自包一层 `<g>`，
            并用 inline `transform-origin` 精确指定站位像素坐标，
            保证每道波纹都以站位为圆心扩散。

            【动画为什么放在 <g> 而不是 <path>】
            `opacity` 在 SVG 里是**元素自身的属性，不会从父级继承生效** ——
            父 `<g>` 的 opacity 只是作为一个整体乘上去，
            子 `<path>` 上若写了 `opacity: 0`，那条波纹就永远是隐形的。
            所以：动画属性全部写在 `<g>` 上，`<path>` 不设 opacity。

            【为什么用 scale 而非直接改 rx/ry】
            SVG 几何属性（rx/ry）不能做 CSS 动画（兼容性差），
            用 `scale()` 放大包围盒则天然保持椭圆形状 ——
            长轴短轴按同一比例放大，椭圆始终「躺平」在水面视角上。
          -->
          <g
            v-for="k in 3"
            :key="'wave' + k"
            class="ws-sonar-waves"
            :style="{ transformOrigin: `${n.x}px ${n.y}px`, animationDelay: `${(k - 1) * 0.9}s` }"
          >
            <path
              :d="surfaceEllipsePath(n.x, n.y, waveRadius * (0.3 + k * 0.28))"
              fill="none"
              stroke="url(#uWave)"
              stroke-width="1.6"
              stroke-dasharray="26 16"
              class="ws-sonar-wave"
            />
          </g>
          <!-- 站位标记 -->
          <circle :cx="n.x" :cy="n.y" r="4" fill="#8ff6ff" opacity="0.95" />
          <circle :cx="n.x" :cy="n.y" r="8" fill="none" stroke="#8ff6ff" stroke-opacity="0.5" stroke-width="1" />
        </g>

        <!-- 站名标签（S01 / S02 / S03） -->
        <g v-for="n in st.nodes" :key="n.id + '-label'">
          <rect
            :x="n.x - 15"
            :y="n.y - 33"
            width="30"
            height="19"
            rx="3"
            fill="#04192b"
            fill-opacity="0.86"
            stroke="#2fd8ea"
            stroke-opacity="0.85"
          />
          <text
            :x="n.x"
            :y="n.y - 19"
            fill="#bff4ff"
            font-size="13"
            text-anchor="middle"
            font-family="'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif"
          >{{ n.id.split('-')[0] }}</text>
        </g>
      </g>
    </g>

    <!--
      ===== 2. 水下目标（动画） =====

      每个目标包含：
        · 尾迹（沿航迹的渐细拖尾）
        · 目标贴图（潜水员/潜航器），航向随运动方向旋转
        · 声纳探测扫描环（进入声纳覆盖圈时出现）
        · 四角选框 + 编号/深度标签
    -->

    <!-- 2a. 声纳测向连线：声纳站 → 被探测到的目标 -->
    <g v-if="layers.coverage" class="ws-sonar-detection-layer">
      <g v-for="(d, i) in sonarDetections" :key="'det-' + i" class="ws-sonar-detection">
        <!--
          测向连线：从声纳站指向目标。

          用虚线 + 流动动画，表达「这是主动声呐发出的探测线」。
        -->
        <line
          :x1="d.station.x"
          :y1="d.station.y"
          :x2="d.target.x"
          :y2="d.target.y"
          :stroke="d.target.color"
          stroke-width="1.2"
          stroke-opacity="0.55"
          stroke-dasharray="6 8"
          class="ws-sonar-det-line"
        />
        <!-- 目标侧的「回波标记」：一个小菱形，标出回波点 -->
        <circle
          :cx="d.target.x"
          :cy="d.target.y"
          r="3.4"
          :fill="d.target.color"
          fill-opacity="0.9"
          class="ws-sonar-det-blip"
        />
      </g>
    </g>

    <g v-for="t in underTargets" :key="t.id" class="ws-under-target">
      <!--
        2b. 尾迹：沿航迹的渐细拖尾。

        【为什么尾迹要单独画一条 polyline】
        目标符号沿航迹移动，若直接画「当前位置到最近 14 个采样点」的连线，
        在目标倒退时尾迹会甩到前面。单独一条拖尾 polyline 更自然。
      -->
      <polyline
        :points="t.trail.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')"
        fill="none"
        :stroke="t.color"
        stroke-width="3"
        stroke-opacity="0.4"
        stroke-linecap="round"
        stroke-linejoin="round"
        class="ws-under-trail"
      />

      <!--
        2c. 声纳探测扫描环。

        只有当目标落在某个声纳站覆盖圈内才出现 ——
        这是「声纳发现了它」的视觉证据。
      -->
      <g v-if="sonarDetections.some((d) => d.target.id === t.id)" class="ws-under-detect">
        <circle
          :cx="t.x"
          :cy="t.y"
          :r="t.size * 0.7 + 8"
          fill="none"
          :stroke="t.color"
          stroke-width="1.4"
          stroke-opacity="0.8"
          class="ws-under-detect-ring"
        />
        <circle
          :cx="t.x"
          :cy="t.y"
          :r="t.size * 0.7 + 8"
          fill="none"
          :stroke="t.color"
          stroke-width="1"
          stroke-opacity="0.5"
          class="ws-under-detect-ring ws-under-detect-ring-2"
        />
      </g>

      <!--
        2d. 目标贴图（潜水员 / 潜航器）。

        【航向旋转】
        贴图按 `course` 旋转，跟随运动方向 ——
        水下目标必须有艏向，否则「在游动」这件事完全看不出来。
      -->
      <g :transform="`translate(${t.x} ${t.y}) rotate(${t.course})`" class="ws-under-sprite">
        <image
          v-if="t.sprite"
          :href="t.sprite"
          :x="-t.size / 2"
          :y="-t.size / 3"
          :width="t.size"
          :height="(t.size * 2) / 3"
          preserveAspectRatio="xMidYMid meet"
          opacity="0.96"
        />
        <!--
          艏向箭头（贴图缺失时的兜底，也是运动方向的强调）。
        -->
        <path
          d="M 0 0 L 0 -1"
          :stroke="t.color"
          stroke-width="0"
          fill="none"
        />
      </g>

      <!--
        2e. 四角选框。

        【为什么选框也要跟着贴图缩小】
        选框原本是固定 22×16 像素（按大号蛙人贴图设计）。
        贴图缩小后若选框不变，就会出现「小小一个人被大框框住」的反差。
        因此选框按贴图尺寸等比缩放，保持视觉比例一致。
      -->
      <g
        :transform="`translate(${t.x} ${t.y}) scale(${Math.max(0.55, t.size / 46)})`"
        class="ws-target-bracket"
      >
        <path
          d="M -22 -16 L -22 -22 L -14 -22 M 14 -22 L 22 -22 L 22 -16
             M 22 16 L 22 22 L 14 22 M -14 22 L -22 22 L -22 16"
          fill="none"
          :stroke="t.color"
          stroke-width="2.2"
          stroke-linecap="round"
        />
      </g>

      <!--
        2f. 标签框：编号 + 深度 + 速度。

        【深度为什么要常驻】
        水下场景最关键的信息就是深度 ——
        目标在哪个深度层，直接决定它能不能被声纳探测到。
      -->
      <g :transform="`translate(${t.x + 26} ${t.y - 30})`" class="ws-under-label">
        <rect
          width="146"
          height="34"
          rx="3"
          fill="#2a1e00"
          fill-opacity="0.9"
          :stroke="t.color"
          stroke-width="1.2"
        />
        <text x="9" y="14" :fill="t.color" font-size="14" font-weight="700" font-family="'Microsoft YaHei','PingFang SC',sans-serif">{{ t.id }}</text>
        <text x="9" y="27" fill="#e8d9a0" font-size="10.5" font-family="'Microsoft YaHei','PingFang SC',sans-serif">
          深度 {{ t.depth.toFixed(1) }} 米 · {{ t.type }}
        </text>
        <!-- 选中态：左侧色条 -->
        <rect v-if="t.selected" x="0" y="0" width="3" height="34" :fill="t.color" />
      </g>
    </g>

    <!-- ===== 3. 右下角底图性质标注（与效果图一致，说明这是实景底图上的水下态势） ===== -->
    <text
      :x="chartW - 24"
      :y="chartH - 20"
      fill="#9fd8e8"
      fill-opacity="0.82"
      font-size="15"
      text-anchor="end"
      font-family="'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif"
    >实景底图 · 水下目标航迹演示</text>
  </svg>
</template>

<style scoped>
/* 水下场景层：与 .ws-chart 完全重合，共用容器像素坐标系。 */
.ws-under-scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 1;
}
</style>

<!--
  动画样式单独放在**非 scoped** 块里。

  【为什么不能用 scoped】
  Vue 的 scoped CSS 依赖给元素加上 `data-v-xxxx` 属性来限定作用域。
  但在 **SVG 根元素**上，Vue 的模板编译器有一个已知行为差异：
  根 `<svg>` 及其部分子元素（如通过 `v-for` 渲染的 `<path>`）
  **不会**被注入 scope 属性 ——
  实测中只有 `<defs>` 内的静态子元素拿到了 `data-v-xxxx`。

  结果就是：`.ws-sonar-wave[data-v-xxxx]` 这类选择器**匹配不到任何元素**，
  动画规则等于没写，`animation-name` 计算值是 `none`，声纳波完全不动。

  这里改用**全局样式 + 专属类名前缀**（`ws-under-scene` 已在组件内唯一），
  不依赖 scope 属性，动画行为完全确定。
  类名带 `ws-` 前缀，与全局样式表不会冲突。
-->
<style>
/* ===== 水下色调 ===== */
.ws-under-scene .ws-under-tint {
  /* 极缓慢的明暗呼吸，模拟水体光线变化，让静止画面也有呼吸感 */
  animation-name: u-tint-breathe;
  animation-duration: 11s;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}
@keyframes u-tint-breathe {
  0%,
  100% {
    opacity: 0.86;
  }
  50% {
    opacity: 1;
  }
}

/* ===== 声纳波束 ===== */
.ws-under-scene .ws-sonar-beam {
  opacity: 0.72;
  /*
   * 【为什么用长写属性而不是 animation 简写】
   * Vite 的 CSS 转换器（esbuild/postcss）在处理 `animation` 简写时
   * 会把它**重排**成规范顺序：
   *   `animation: u-wave-out 2.7s ease infinite`
   *   → `animation: 2.7s ease 0s infinite normal none running u-wave-out`
   * 结果 `none`（合法的 animation-name）出现在真名之前，
   * 解析器取第一个「名字样」的值 —— 于是 animation-name 变成 `none`，
   * **动画完全不播**。
   *
   * 长写属性没有这个问题，行为完全确定。
   */
  animation-name: u-beam-breathe;
  animation-duration: 4.2s;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}

/* 波束缓慢「呼吸」，模拟持续发射的强度起伏 */
@keyframes u-beam-breathe {
  0%,
  100% {
    opacity: 0.55;
  }
  50% {
    opacity: 0.9;
  }
}

/* ===== 扩散波纹 ===== */
/*
 * 【transform-origin 的坐标系 —— 这里最容易出错】
 * `transform-box: fill-box` 会把 transform-origin 的参照系
 * 从「用户坐标系」改成「元素自身的包围盒」。
 *
 * 波纹是 `<g>` 包裹的多个 `<path>`，包围盒中心会随波纹半径变化，
 * 导致缩放时圆心漂移。因此这里用 `view-box`：
 * transform-origin 的数值直接按 **SVG 用户坐标（容器像素）** 解释，
 * 与模板里 inline 设的 `${n.x}px ${n.y}px` 完全一致 ——
 * 圆心就精确落在站位上，缩放时只变半径、不动圆心。
 */
.ws-under-scene .ws-sonar-waves {
  transform-box: view-box;
  /* 长写属性：避免 CSS 转换器重排 `animation` 简写导致动画失效（见上方说明） */
  animation-name: u-wave-out;
  animation-duration: 2.7s;
  animation-timing-function: cubic-bezier(0.32, 0.02, 0.38, 1);
  animation-iteration-count: infinite;
  /* 明确指定为 running —— 避免被其它规则（如 data-paused 残留）冻结 */
  animation-play-state: running;
}

/*
 * 单条波纹只负责描边样式。
 *
 * 【注意：这里绝对不能设 opacity】
 * SVG 的 `opacity` 是元素**自身**的属性，不从父级继承生效。
 * 父 `<g>` 上的动画 opacity 会与子 `<path>` 上的 opacity 相乘，
 * 若子元素写死 `opacity: 0`，波纹就永远看不见 ——
 * 这正是之前「波不是动态的、看不见在扩散」的根本原因。
 */
.ws-under-scene .ws-sonar-wave {
  stroke-linecap: round;
}

/*
 * 波纹由内向外扩散：
 *   scale 0.55 → 1.15  （半径变大）
 *   opacity 0 → .85 → 0（到外圈时淡出，形成「一圈圈往外推」）
 * dashoffset 让弧线本身也在流动，强化「扫描」感。
 */
@keyframes u-wave-out {
  0% {
    transform: scale(0.55);
    opacity: 0;
    stroke-dashoffset: 42;
  }
  18% {
    opacity: 0.9;
  }
  85% {
    opacity: 0.12;
  }
  100% {
    transform: scale(1.15);
    opacity: 0;
    stroke-dashoffset: -42;
  }
}

/*
 * 播放暂停时的冻结范围。
 *
 * 【为什么只冻结波纹，不冻结目标运动】
 * 水下模式的定位是「动画演示」——
 * 目标应当始终在沿航迹巡弋，声纳波与目标之间才有持续的因果关系。
 * 若回放一暂停就冻结全部效果，整个水下场景会彻底死掉。
 *
 * 因此这里只冻结「声纳波与色调呼吸」这类**环境动画**，
 * 目标运动、航迹流动、探测联动继续运行。
 */
.ws-under-scene[data-paused='true'] .ws-sonar-waves,
.ws-under-scene[data-paused='true'] .ws-sonar-beam,
.ws-under-scene[data-paused='true'] .ws-under-tint {
  animation-play-state: paused;
}

/* 目标选框轻微闪烁，提示「这是当前跟踪目标」 */
.ws-under-scene .ws-target-bracket {
  animation-name: u-bracket-pulse;
  animation-duration: 1.8s;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  transform-box: fill-box;
  transform-origin: 50% 50%;
}
@keyframes u-bracket-pulse {
  0%,
  100% {
    opacity: 0.75;
  }
  50% {
    opacity: 1;
  }
}

/* ===== 水下航迹 ===== */
/*
 * 航迹主线的「流动」效果。
 *
 * 【为什么用 stroke-dashoffset 而不是画一串移动的小圆点】
 * dashoffset 递减 = 虚线沿路径整体向前平移，
 * 视觉上就是「有东西正朝这个方向走」。
 * 开销极低（纯合成器属性），且不需要 JS 逐帧更新路径。
 */
.ws-under-scene .ws-under-track-flow {
  animation-name: u-track-flow;
  animation-duration: 2.4s;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}
@keyframes u-track-flow {
  from {
    stroke-dashoffset: 70;
  }
  to {
    stroke-dashoffset: 0;
  }
}

/* 航迹辉光缓慢呼吸，让静止的线也有「在水中发散」的质感 */
.ws-under-scene .ws-under-track-glow {
  animation-name: u-track-glow;
  animation-duration: 3.6s;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}
@keyframes u-track-glow {
  0%,
  100% {
    stroke-opacity: 0.1;
  }
  50% {
    stroke-opacity: 0.22;
  }
}

/* 尾迹渐隐：靠近目标的一端最亮 */
.ws-under-scene .ws-under-trail {
  animation-name: u-trail-fade;
  animation-duration: 2s;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}
@keyframes u-trail-fade {
  0%,
  100% {
    stroke-opacity: 0.28;
  }
  50% {
    stroke-opacity: 0.5;
  }
}

/* ===== 声纳探测联动 ===== */
/*
 * 测向连线上的「回波脉冲」向目标方向流动。
 */
.ws-under-scene .ws-sonar-det-line {
  animation-name: u-det-flow;
  animation-duration: 1.6s;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}
@keyframes u-det-flow {
  from {
    stroke-dashoffset: 28;
  }
  to {
    stroke-dashoffset: 0;
  }
}

/* 回波点闪烁 —— 「ping」一下的观感 */
.ws-under-scene .ws-sonar-det-blip {
  animation-name: u-blip;
  animation-duration: 1.2s;
  animation-timing-function: ease-out;
  animation-iteration-count: infinite;
}
@keyframes u-blip {
  0%,
  100% {
    opacity: 0.35;
    r: 2.6;
  }
  30% {
    opacity: 1;
    r: 4.2;
  }
}

/*
 * 探测扫描环：向外扩散并淡出。
 *
 * 【为什么不用 scale 放大】
 * 环是 `<circle>`，用 `r` 做动画在 SVG2 里兼容性一般；
 * 用 `transform: scale()` 放大包围盒则行为确定，
 * 且 `transform-box: fill-box` + `transform-origin: 50% 50%`
 * 能让圆心精确落在目标上。
 */
.ws-under-scene .ws-under-detect-ring {
  transform-box: fill-box;
  transform-origin: 50% 50%;
  animation-name: u-detect-ring;
  animation-duration: 2s;
  animation-timing-function: cubic-bezier(0.25, 0.6, 0.35, 1);
  animation-iteration-count: infinite;
}
@keyframes u-detect-ring {
  0% {
    transform: scale(0.55);
    opacity: 0.9;
  }
  100% {
    transform: scale(1.5);
    opacity: 0;
  }
}

/* 第二圈错开相位，形成连续扩散 */
.ws-under-scene .ws-under-detect-ring-2 {
  animation-delay: 1s;
}

/* 尊重系统的「减少动态效果」设置 */
@media (prefers-reduced-motion: reduce) {
  .ws-under-scene .ws-sonar-waves,
  .ws-under-scene .ws-sonar-beam,
  .ws-under-scene .ws-under-tint,
  .ws-under-scene .ws-target-bracket,
  .ws-under-scene .ws-under-track-flow,
  .ws-under-scene .ws-under-track-glow,
  .ws-under-scene .ws-under-trail,
  .ws-under-scene .ws-sonar-det-line,
  .ws-under-scene .ws-sonar-det-blip,
  .ws-under-scene .ws-under-detect-ring {
    animation-name: none !important;
  }
}
</style>
