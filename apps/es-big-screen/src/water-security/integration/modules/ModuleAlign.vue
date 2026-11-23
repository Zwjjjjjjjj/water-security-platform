<script setup lang="ts">

/** 模块标题图标 */
const MOD_ICON =
  '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 7v5.3l3.6 2.1" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'

/**
 * 模块 2：时空对齐
 *
 * 【效果图版式】
 *   左半：时间对齐 —— 多源时间轴，每源一条泳道，
 *         画出该源的报文覆盖段（青/绿/蓝/紫），中间一条橙色的「统一时刻 t₀」虚线。
 *   右半：空间对齐 —— 统一坐标（ENU）下的点迹散点图，
 *         各源点迹用各自颜色，重叠处呈青色，橙色圈为「配置位置」。
 *
 * 【数据来源】pipeline.buildAlignModule()，全部来自 tracks.json 实测样本。
 */
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { KIND_LABEL, type AlignModule } from '../pipeline'
import { PALETTE, KIND_COLOR, scale, extent, linePath, areaPath } from '../chart-utils'

const props = defineProps<{
  mod: AlignModule
  selectedTargetId: string | null
  durationMs: number
}>()

const emit = defineEmits<{
  (e: 'select-row', id: string): void
  (e: 'select-target', id: string): void
  (e: 'open-detail', id: string): void
}>()

/**
 * 图表宽度（像素）。
 *
 * 【为什么不用固定 viewBox】
 * 之前用 `viewBox="0 0 1000 H"` + `preserveAspectRatio="none"`，
 * 容器实际只有 300px 宽，viewBox 被横向拉伸 3 倍，
 * 文字和圆点全被压扁 —— 看起来就是「糊成一团」。
 * 现在改成：容器多少像素，viewBox 就是多少像素，
 * 一个 SVG 单位 = 一个 CSS 像素，文字永远不变形。
 */
const chartW = ref(480)

/** 时间轴容器高度（用于分配泳道高度） */
const timeAxisH = ref(260)

/** 散点图容器宽度（与时间轴各自独立） */
const scatterW = ref(480)

/**
 * 图表尺寸监听。
 *
 * 两个 SVG（时间轴 / 散点图）分别有自己的容器尺寸，
 * 所以要**分开观察**：
 * · 宽度  —— 两个图共用，取任一个即可（左右两列宽度接近，
 *            各自用自己的更准，见下）
 * · 高度  —— 散点图必须跟着自己的容器走，否则会被时间轴的高度污染。
 */
let ro: ResizeObserver | null = null
onMounted(() => {
  const svgs = Array.from(
    document.querySelectorAll('.ws-mod--align .ws-svg'),
  ) as SVGSVGElement[]
  const timeSvg = svgs[0]
  const scatterSvg = svgs[1]
  if (!timeSvg) return

  ro = new ResizeObserver((entries) => {
    for (const e of entries) {
      const rect = e.contentRect
      const w = rect.width ?? 0
      const h = rect.height ?? 0
      const el = e.target as SVGSVGElement
      if (el === scatterSvg) {
        /* 散点图：宽高都跟着自己的容器 */
        if (w > 40) scatterW.value = Math.round(w)
        if (h > 80) scatterH.value = Math.round(h)
      } else {
        /* 时间轴：宽度 + 容器高度（泳道高度要按它均分） */
        if (w > 40) chartW.value = Math.round(w)
        if (h > 80) timeAxisH.value = Math.round(h)
      }
    }
  })
  ro.observe(timeSvg)
  if (scatterSvg) ro.observe(scatterSvg)
})
onBeforeUnmount(() => ro?.disconnect())

/* ---------- 时间对齐：多源时间轴（效果图版式） ----------
 * 每个源一条泳道，泳道上是「采样点」（圆点）而不是色块，
 * 点按该源在各时刻的样本落位；中间一条橙色虚线 = 统一时刻 t₀。
 */
const timeAxis = computed(() => {
  const dur = Math.max(1, props.durationMs)
  const axisH = 24
  const laneTop = 18
  /**
   * 泳道高度按容器可用高度分配。
   *
   * 【为什么要动态算】
   * 源数量是固定的（4 个），但容器高度随视口变化。
   * 固定 34px 时，容器有 380px 高而泳道只占 ~180px，
   * 下方就空出一条 —— 看起来像「图没画满」。
   * 现在按「可用高度 ÷ 泳道数」均分，任何高度都能铺满。
   */
  /**
   * 泳道可用高度：viewBox 高 − 顶部留白 − 底部时间轴。
   * 泳道在此区域内**均匀铺开**，第一条贴着顶部、最后一条贴着时间轴，
   * 这样泳道区域不会在底部留一条空白。
   */
  const usable = Math.max(120, timeAxisH.value) - laneTop - axisH
  const nLanes = Math.max(1, props.mod.lanes.length)
  const laneH = Math.max(30, usable / nLanes)
  /**
   * viewBox 高度 = 容器高度。
   *
   * 【为什么必须严格相等】
   * SVG 用 `preserveAspectRatio="xMidYMid meet"` 缩放时，
   * viewBox 与容器宽高比不一致就会留白：
   * 容器比 viewBox 宽 → 左右留白；
   * 容器比 viewBox 高 → 上下留白。
   * 让 viewBox 高度直接等于容器高度，比例就一致了，
   * 图形既铺满又不变形（宽高各自独立映射）。
   */
  const totalH = Math.max(160, timeAxisH.value)

  /* 泳道 → 该源在该时刻附近的采样点 */
  const W = chartW.value
  /* 左侧留出泳道名空间 */
  const LANE_PAD = 46
  const lanes = props.mod.lanes.map((l) => {
    const rows = props.mod.rows.filter((r) => r.sourceId === l.sourceId)
    /* 按时间聚类：同一时刻的样本合并成一点 */
    const buckets = new Map<number, { t: number; n: number }>()
    for (const r of rows) {
      const key = Math.round(r.rawTimeMs / 1500) * 1500
      const b = buckets.get(key) ?? { t: r.rawTimeMs, n: 0 }
      b.n += 1
      buckets.set(key, b)
    }
    const pts = [...buckets.values()]
      .map((b) => ({ x: scale(b.t, 0, dur, LANE_PAD, W), n: b.n }))
      .filter((p) => p.x >= LANE_PAD - 2 && p.x <= W + 2)
      .sort((a, b) => a.x - b.x)
    const idx = props.mod.lanes.indexOf(l)
    /* 泳道中心均匀分布在 [laneTop, laneTop + usable] 之间 */
    const y = laneTop + (usable * (idx + 0.5)) / nLanes
    return { ...l, pts, y }
  })

  /* 统一时刻 t₀ —— 取各源样本时刻的中位数 */
  const all = props.mod.rows.map((r) => r.rawTimeMs).sort((a, b) => a - b)
  const base = all.length ? all[Math.floor(all.length / 2)] : 0

  return {
    lanes,
    laneH,
    axisH,
    laneTop,
    totalH,
    baseX: scale(base, 0, dur, LANE_PAD, W),
    baseLabel: (base / 1000).toFixed(1) + 's',
    ticks: Array.from({ length: 6 }, (_, i) => {
      const t = (dur / 5) * i
      return { x: scale(t, 0, dur, LANE_PAD, W), label: (t / 1000).toFixed(0) + 's' }
    }),
  }
})

/* ---------- 空间对齐：ENU 散点 ---------- */
/**
 * 散点图绘制高度。
 *
 * 【为什么要跟随容器高度】
 * 之前固定 300px，而容器往往有 380px 高，
 * 于是图表缩在中间、上下各留一条空白带 —— 视觉上就是「没占满」。
 * 现在跟着容器走，viewBox 高度 = 实际可用高度，
 * 图形就能铺满整个面板，且不会变形（宽高各自独立计算）。
 */
const scatterH = ref(300)

const scatter = computed(() => {
  const H = Math.max(160, scatterH.value)
  const W = scatterW.value
  const pts = props.mod.points
  if (!pts.length) {
    return { H, dots: [] as any[], lines: [] as any[], legend: [] as any[], ax: [] as any[], ring: null as any }
  }
  const [eMin, eMax] = extent(pts.map((p) => p.east))
  const [nMin, nMax] = extent(pts.map((p) => p.north))
  const padE = Math.max(40, (eMax - eMin) * 0.1)
  const padN = Math.max(40, (nMax - nMin) * 0.1)
  const e0 = eMin - padE
  const e1 = eMax + padE
  const n0 = nMin - padN
  const n1 = nMax + padN
  const X = (v: number) => scale(v, e0, e1, 44, W - 14)
  const Y = (v: number) => scale(v, n0, n1, H - 26, 16)

  const dots = pts.map((p) => ({
    ...p,
    x: X(p.east),
    y: Y(p.north),
    dim: !!props.selectedTargetId && p.targetId !== props.selectedTargetId,
  }))

  /* 各源点迹的连线（同一目标的不同源应重合） */
  const lines: { d: string; dim: boolean }[] = []
  const byTarget = new Map<string, typeof dots>()
  for (const d of dots) {
    if (!byTarget.has(d.targetId)) byTarget.set(d.targetId, [])
    byTarget.get(d.targetId)!.push(d)
  }
  for (const [, group] of byTarget) {
    if (group.length < 2) continue
    const ordered = [...group].sort((a, b) => a.x - b.x)
    lines.push({
      d: linePath(ordered),
      dim: !!props.selectedTargetId && !group.some((g) => g.targetId === props.selectedTargetId),
    })
  }

  /* 「配置位置」参考圈 —— 取所有点的几何中心 */
  const cx = pts.reduce((a, p) => a + p.east, 0) / pts.length
  const cy = pts.reduce((a, p) => a + p.north, 0) / pts.length

  /* 图例：按源分色（效果图上是「雷达轨迹 / AIS轨迹 / …」） */
  const kinds = [...new Set(pts.map((p) => p.kind))]
  const legend = kinds.map((k) => ({
    label: (KIND_LABEL[k] ?? k) + '轨迹',
    color: KIND_COLOR[k] ?? PALETTE.cyan,
  }))

  return {
    H,
    dots,
    lines,
    legend,
    ax: [
      { x: 46, y: H - 26, label: `E ${e0.toFixed(0)}` },
      { x: W - 14, y: H - 26, label: `E ${e1.toFixed(0)}` },
      { x: 46, y: 16, label: `N ${n1.toFixed(0)}` },
    ],
    ring: { x: X(cx), y: Y(cy) },
  }
})


function fmtMs(v: number) {
  if (!Number.isFinite(v)) return '—'
  return Math.abs(v) >= 1000 ? (v / 1000).toFixed(2) + 's' : v.toFixed(0) + 'ms'
}
</script>

<template>
  <section class="ws-mod ws-mod--align">
    <header class="ws-mod-head">
      <h3 class="ws-mod-title">
        <span class="ws-mod-ico">
          <svg viewBox="0 0 24 24" v-html="MOD_ICON" />
        </span>
        时空对齐
        <em class="ws-mod-sub">| 时间匹配 · 坐标转换 · 统一参考</em>
      </h3>
      <div class="ws-mod-right">
        <button class="ws-mod-more" type="button" title="查看详情" @click="emit('open-detail', 'align')">查看详情 ↗</button>
      </div>
    </header>

    <div v-if="!mod.hasData" class="ws-mod-empty">暂无数据</div>

    <template v-else>
      <div class="ws-align-main">
        <!-- ===== 左：时间对齐 ===== -->
        <div class="ws-align-col ws-align-col--time">
          <h4 class="ws-align-h">时间对齐 <em>统一时刻 t₀</em></h4>
          <svg
            :viewBox="`0 0 ${chartW} ${timeAxis.totalH}`"
            class="ws-svg"
            preserveAspectRatio="xMidYMid meet"
          >
            <!-- 竖向刻度网格 -->
            <line
              v-for="t in timeAxis.ticks"
              :key="'g' + t.label"
              :x1="t.x"
              :x2="t.x"
              :y1="timeAxis.laneTop - 4"
              :y2="timeAxis.totalH - timeAxis.axisH"
              :stroke="PALETTE.grid"
              stroke-width="1"
            />
            <!-- 每源一条泳道 -->
            <g v-for="l in timeAxis.lanes" :key="l.sourceId">
              <!-- 泳道虚线基线 -->
              <line
                :x1="44"
                :x2="chartW"
                :y1="l.y"
                :y2="l.y"
                :stroke="PALETTE.axis"
                stroke-width="0.9"
                stroke-dasharray="5 6"
              />
              <!-- 泳道名 -->
              <text x="40" :y="l.y + 4" text-anchor="end" class="ws-svg-lbl">
                {{ KIND_LABEL[l.kind] ?? l.kind }}
              </text>
              <!-- 采样点 -->
              <circle
                v-for="(p, j) in l.pts"
                :key="j"
                :cx="p.x"
                :cy="l.y"
                :r="p.n > 3 ? 5 : 3.6"
                :fill="KIND_COLOR[l.kind] ?? PALETTE.cyan"
                :fill-opacity="p.n > 3 ? 1 : 0.82"
                stroke="#04141f"
                stroke-width="0.8"
              >
                <title>{{ l.sourceId }} · {{ p.n }} 条</title>
              </circle>
            </g>
            <!-- 统一时刻 t₀ —— 橙色竖虚线 + 顶部箭头 -->
            <line
              :x1="timeAxis.baseX"
              :x2="timeAxis.baseX"
              :y1="timeAxis.laneTop - 2"
              :y2="timeAxis.totalH - timeAxis.axisH"
              :stroke="PALETTE.amber"
              stroke-width="1.6"
              stroke-dasharray="6 4"
            />
            <path
              :d="`M ${timeAxis.baseX - 4} ${timeAxis.laneTop + 3} L ${timeAxis.baseX} ${timeAxis.laneTop - 3} L ${timeAxis.baseX + 4} ${timeAxis.laneTop + 3} Z`"
              :fill="PALETTE.amber"
            />
            <text :x="timeAxis.baseX" :y="timeAxis.laneTop - 7" class="ws-svg-amber ta-c">统一时刻 t₀</text>
            <!-- 时间刻度 -->
            <text
              v-for="t in timeAxis.ticks"
              :key="'t' + t.label"
              :x="t.x"
              :y="timeAxis.totalH - 6"
              class="ws-svg-tick"
            >
              {{ t.label }}
            </text>
          </svg>
          <div class="ws-legend">
            <span v-for="k in ['radar', 'ais', 'sonar', 'eo']" :key="k">
              <i :style="{ background: KIND_COLOR[k] }" />{{ KIND_LABEL[k] }}
            </span>
            <span><i style="background: #ffb547" />统一时刻</span>
          </div>
        </div>

        <!-- ===== 右：空间对齐 ===== -->
        <div class="ws-align-col">
          <h4 class="ws-align-h">空间对齐 <em>统一坐标 ENU（米）</em></h4>
          <svg
            :viewBox="`0 0 ${chartW} ${scatter.H}`"
            class="ws-svg"
            preserveAspectRatio="xMidYMid meet"
          >
            <!-- 网格 -->
            <line
              v-for="i in 6"
              :key="'gx' + i"
              :x1="(chartW / 6) * i"
              :x2="(chartW / 6) * i"
              y1="0"
              :y2="scatter.H - 20"
              :stroke="PALETTE.grid"
              stroke-width="1"
            />
            <line
              v-for="i in 5"
              :key="'gy' + i"
              x1="0"
              :x2="chartW"
              :y1="16 + ((scatter.H - 40) / 4) * (i - 1)"
              :y2="16 + ((scatter.H - 40) / 4) * (i - 1)"
              :stroke="PALETTE.grid"
              stroke-width="1"
            />
            <!-- 同目标跨源连线 -->
            <path
              v-for="(l, i) in scatter.lines"
              :key="'l' + i"
              :d="l.d"
              fill="none"
              :stroke="PALETTE.cyan"
              stroke-width="1.5"
              stroke-dasharray="6 4"
              :opacity="l.dim ? 0.12 : 0.8"
            />
            <!-- 配置位置参考圈 -->
            <g v-if="scatter.ring">
              <circle
                :cx="scatter.ring.x"
                :cy="scatter.ring.y"
                r="11"
                fill="none"
                :stroke="PALETTE.amber"
                stroke-width="2"
              />
              <circle :cx="scatter.ring.x" :cy="scatter.ring.y" r="3.4" :fill="PALETTE.amber" />
            </g>
            <!-- 点迹 -->
            <circle
              v-for="(d, i) in scatter.dots"
              :key="i"
              :cx="d.x"
              :cy="d.y"
              :r="selectedTargetId && d.targetId === selectedTargetId ? 6 : 4"
              :fill="KIND_COLOR[d.kind] ?? PALETTE.cyan"
              :fill-opacity="d.dim ? 0.22 : 0.88"
              :stroke="selectedTargetId && d.targetId === selectedTargetId ? '#ffffff' : 'none'"
              stroke-width="1.5"
              style="cursor: pointer"
              @click="emit('select-target', d.targetId)"
            >
              <title>{{ d.targetId }} · {{ KIND_LABEL[d.kind] ?? d.kind }}</title>
            </circle>
            <!-- 坐标轴标注 -->
            <text v-for="a in scatter.ax" :key="'a' + a.label" :x="a.x" :y="a.y + 13" class="ws-svg-tick">
              {{ a.label }}
            </text>
            <text x="4" :y="14" class="ws-svg-tick">N/米</text>
            <text :x="chartW - 36" :y="scatter.H - 6" class="ws-svg-tick">E/米</text>
            <!-- 图内图例（按源分色，与效果图一致） -->
            <g :transform="`translate(56, 20)`">
              <g v-for="(lg, i) in scatter.legend" :key="lg.label" :transform="`translate(0, ${i * 15})`">
                <line x1="0" :y1="0" x2="18" y2="0" :stroke="lg.color" stroke-width="1.6" />
                <circle cx="9" cy="0" r="3" :fill="lg.color" />
                <text x="24" y="3.5" class="ws-svg-lbl">{{ lg.label }}</text>
              </g>
            </g>
          </svg>
          <div class="ws-legend">
            <span><i style="background: #ffb547" />配置位置</span>
          </div>
        </div>
      </div>

      <!-- ===== 底部关键统计 ===== -->
      <footer class="ws-mod-foot">
        <div class="ws-kpi"><span>匹配记录</span><b class="is-ok">{{ mod.matchedCount }}</b></div>
        <div class="ws-kpi"><span>总记录</span><b>{{ mod.totalCount }}</b></div>
        <div class="ws-kpi"><span>时间残差</span><b>{{ fmtMs(mod.avgDt) }}</b></div>
        <div class="ws-kpi"><span>最大残差</span><b :class="mod.maxDt > mod.tolerance ? 'is-bad' : ''">{{ fmtMs(mod.maxDt) }}</b></div>
        <div class="ws-kpi ws-kpi--grow">
          <span>参与对齐的源</span>
          <div class="ws-srcchips">
            <span v-for="l in mod.lanes" :key="l.sourceId" class="ws-chip" :style="{ '--c': KIND_COLOR[l.kind] }">
              <i />{{ KIND_LABEL[l.kind] ?? l.kind }} {{ l.sourceId }}
              <em>{{ l.segments.length }} 段</em>
            </span>
          </div>
        </div>
      </footer>
    </template>
  </section>
</template>

<style scoped lang="scss">
.ws-mod {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 10px 14px 10px;
  background: linear-gradient(180deg, rgba(8, 42, 68, 0.9), rgba(4, 25, 46, 0.9));
  border: 1px solid rgba(34, 228, 238, 0.3);
  border-radius: 6px;
  overflow: hidden;
}
.ws-mod-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 7px;
  border-bottom: 1px solid rgba(34, 228, 238, 0.2);
}
.ws-mod-title {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  font-size: 19px;
  line-height: 1.3;
  font-weight: 700;
  color: #e1f7ff;
  letter-spacing: 1px;
  white-space: nowrap;
}
.ws-mod-ico {
  color: #22e4ee;
  display: flex;
  align-items: center;
  flex: 0 0 auto;
}
.ws-mod-ico svg { width: 21px; height: 21px; display: block; }
.ws-mod-sub {
  font-size: 15px;
  font-weight: 400;
  font-style: normal;
  color: #7fa8bd;
  padding-left: 9px;
  border-left: 1px solid rgba(143, 185, 204, 0.35);
}
.ws-mod-right { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.ws-mod-more {
  background: none;
  border: none;
  color: #22e4ee;
  font-size: 14.5px;
  line-height: 1.3;
  cursor: pointer;
  padding: 0;
  white-space: nowrap;
}
.ws-mod-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: #7fa8bd; font-size: 14px; }

/* ===== 主体两列 ===== */
.ws-align-main {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 0.92fr 1.08fr;
  gap: 12px;
  padding-top: 8px;
}
.ws-align-col {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(2, 16, 32, 0.5);
  border: 1px solid rgba(34, 228, 238, 0.14);
  border-radius: 4px;
  padding: 7px 9px;
}
/* 时间轴：拉伸填满整列高度，不再缩成一小条留大片空白 */
.ws-align-col--time .ws-svg {
  flex: 1;
  min-height: 0;
  width: 100%;
}
.ws-align-h {
  padding-left: 8px;
  border-left: 3px solid #22e4ee;
  margin: 0 0 5px;
  font-size: 15px;
  font-weight: 600;
  color: #22e4ee;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.ws-align-h em { font-style: normal; color: #7fa8bd; font-weight: 400; font-size: 14.5px; }
.ws-svg { flex: 1; width: 100%; min-height: 0; overflow: visible; }
.ws-svg-lbl { fill: #a8e2d4; font-size: 14px; }
.ws-svg-tick { fill: #7fa8bd; font-size: 14px; text-anchor: middle; }
.ws-svg-amber { fill: #ffb547; font-size: 15px; }
.ta-c { text-anchor: middle; }
.ws-legend {
  display: flex;
  gap: 11px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 14.5px;
  color: #8fb4c8;
}
.ws-legend span { display: inline-flex; align-items: center; gap: 4px; }
.ws-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

/* ===== 底部关键统计 ===== */
.ws-mod-foot {
  display: flex;
  gap: 0;
  align-items: center;
  justify-content: space-around;
  padding-top: 9px;
  margin-top: 8px;
  border-top: 1px solid rgba(34, 228, 238, 0.2);
}
.ws-kpi {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  position: relative;
  flex: 0 0 auto;
  padding: 0 14px;
}
.ws-kpi + .ws-kpi::before {
  content: '';
  position: absolute;
  left: 0;
  top: 12%;
  bottom: 12%;
  width: 1px;
  background: rgba(143, 185, 204, 0.24);
}
.ws-kpi--grow {
  flex: 1;
  min-width: 0;
  align-items: flex-start;
  padding-left: 16px;
}
.ws-kpi span { font-size: 14px; color: #7fa8bd; }
.ws-kpi b { font-size: 19px; color: #e1f7ff; line-height: 1.2; }
.ws-kpi b.is-ok { color: #39d98a; }
.ws-kpi b.is-bad { color: #ff6b6b; }
.ws-kpi b.is-warn { color: #ffb547; }
.ws-srcchips { display: flex; gap: 5px; flex-wrap: wrap; }
.ws-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  font-size: 14px;
  color: #cfeaf5;
  background: rgba(4, 25, 46, 0.7);
  border: 1px solid rgba(143, 185, 204, 0.24);
  border-radius: 9px;
}
.ws-chip i { width: 6px; height: 6px; border-radius: 50%; background: var(--c); }
.ws-chip em { font-style: normal; color: #7fa8bd; }
</style>
