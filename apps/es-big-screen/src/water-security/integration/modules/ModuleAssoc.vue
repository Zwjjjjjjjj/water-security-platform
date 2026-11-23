<script setup lang="ts">

/** 模块标题图标 */
const MOD_ICON =
  '<circle cx="5" cy="7" r="2.6" fill="currentColor"/><circle cx="19" cy="7" r="2.6" fill="currentColor"/><circle cx="12" cy="18" r="2.6" fill="currentColor"/><path d="M7 8.6l3.4 6.4M17 8.6l-3.4 6.4" stroke="currentColor" stroke-width="1.5" fill="none"/>'

/**
 * 模块 3：航迹关联
 *
 * 【效果图版式】
 *   左：航迹匹配 —— 三列（源航迹 A / 源航迹 B / 全局目标），
 *       已确认用实线连接，待确认用虚线，下方列出「待确认」条目。
 *   右：航迹对比（ENU）—— 各源航迹在同一坐标系里叠加，
 *       橙色圈标出「全局目标 G00x」的重合位置。
 *
 * 【数据来源】pipeline.buildAssocModule()，全部来自 tracks.json 的 source_tracks。
 */
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { KIND_LABEL, type AssocModule, type Candidate } from '../pipeline'
import { PALETTE, KIND_COLOR, STATE_COLOR, STATE_LABEL, scale, extent } from '../chart-utils'

const props = defineProps<{
  mod: AssocModule
  selectedTargetId: string | null
  domainFilter: string | null
}>()

const emit = defineEmits<{
  (e: 'select-target', id: string): void
  (e: 'select-candidate', c: Candidate): void
  (e: 'open-detail', id: string): void
}>()

/** 域筛选后的目标 */
const visibleTracks = computed(() =>
  props.domainFilter
    ? props.mod.tracks.filter((t) => t.domain === props.domainFilter)
    : props.mod.tracks,
)

/* ---------- 左：航迹匹配三列 ---------- */
const matchCols = computed(() => {
  /* 取出已确认的配对（数据层面同属一个全局目标） */
  const confirmed = props.mod.confirmed.filter((c) => c.globalId)

  /* 全局目标列 —— 只列有跨源配对的目标 */
  const gidMap = new Map<string, { gid: string; domain: string; sources: string[]; pending: boolean }>()
  for (const t of props.mod.tracks) {
    if (props.domainFilter && t.domain !== props.domainFilter) continue
    const srcs = t.sources.map((s) => s.sourceId)
    gidMap.set(t.globalId, { gid: t.globalId, domain: t.domain, sources: srcs, pending: false })
  }
  /* 待确认的目标也进「全局目标」列（虚线框），方便人工复核 */
  const pendingList = props.mod.pending.slice(0, 5)
  for (const c of pendingList) {
    if (!c.globalId) continue
    const e = gidMap.get(c.globalId)
    if (e) e.pending = true
    else gidMap.set(c.globalId, { gid: c.globalId, domain: '', sources: [c.a.sourceId], pending: true })
  }

  /**
   * 左侧源航迹列。
   *
   * 【为什么不能按设备聚合】
   * 一台设备（R01）可以同时承载多条彼此独立的航迹，
   * 每条航迹只覆盖某个全局目标的一部分时间。
   * 如果按设备聚合成一个盒子，就只剩「R01」一个条目，
   * 完全看不出「哪条源航迹配到了哪个全局目标」。
   *
   * 正确口径：**一条 source_track = 一个航迹盒子**，
   * 盒子编号用「源航迹序号」（R017 / R023 / R031 这样）。
   */
  const srcCol = new Map<string, { sourceId: string; kind: string; count: number; gids: string[] }>()
  /** 每条源航迹的唯一键：全局目标 + 源 */
  const trackKey = (gid: string, sourceId: string) => `${gid}::${sourceId}`
  /** 源航迹 → 展示编号（R017 / A012 …） */
  const srcTrackLabel = new Map<string, string>()
  /** 按源类型累计序号 */
  const kindSeq = new Map<string, number>()

  for (const t of props.mod.tracks) {
    if (props.domainFilter && t.domain !== props.domainFilter) continue
    for (const s of t.sources) {
      const key = trackKey(t.globalId, s.sourceId)
      const seq = (kindSeq.get(s.kind) ?? 0) + 1
      kindSeq.set(s.kind, seq)
      /* 编号规则：源类型前缀 + 三位序号（雷达 R017 / AIS A012） */
      const prefix = (KIND_LABEL[s.kind] ?? s.kind).replace(/[^A-Za-z]/g, '').slice(0, 1).toUpperCase()
        || s.sourceId.replace(/[^A-Za-z]/g, '').slice(0, 1).toUpperCase()
        || 'T'
      const base = s.sourceId.replace(/[^0-9A-Za-z]/g, '').slice(0, 1).toUpperCase() || prefix
      srcTrackLabel.set(key, `${base}${String(seq).padStart(3, '0')}`)

      const e = srcCol.get(key) ?? {
        sourceId: srcTrackLabel.get(key)!,
        kind: s.kind,
        count: 0,
        gids: [],
      }
      e.count += 1
      e.gids.push(t.globalId)
      srcCol.set(key, e)
    }
  }

  /* 按感知源类型分组成列（效果图上是「雷达航迹」「AIS航迹」各一列） */
  const kindCols = new Map<string, { kind: string; items: { sourceId: string; gids: string[]; key: string }[] }>()
  for (const [key, e] of srcCol.entries()) {
    const col = kindCols.get(e.kind) ?? { kind: e.kind, items: [] }
    col.items.push({ sourceId: e.sourceId, gids: e.gids, key })
    kindCols.set(e.kind, col)
  }
  const srcCols = [...kindCols.values()].sort((a, b) => a.kind.localeCompare(b.kind))

  /* 配对连线：源航迹 → 全局目标 */
  const links: {
    from: string
    to: string
    kind: string
    state: string
    cost: number
    dt: number
    dist: number
  }[] = []
  for (const c of confirmed) {
    if (!c.globalId) continue
    if (props.domainFilter) {
      const inDom = props.mod.tracks.find((t) => t.globalId === c.globalId)
      if (!inDom || inDom.domain !== props.domainFilter) continue
    }
    links.push({
      from: c.a.sourceId,
      to: c.globalId,
      kind: c.a.kind,
      state: c.state,
      cost: c.cost,
      dt: c.dt,
      dist: c.dist,
    })
  }

  /* 待确认条目 */
  const pending = pendingList

  return {
    srcCols,
    srcList: [...srcCol.values()].sort((a, b) => a.sourceId.localeCompare(b.sourceId)),
    gidList: [...gidMap.values()]
      .filter((g) => g.sources.length > 1 || g.pending)
      .slice(0, 6),
    links,
    pending,
    confirmed: confirmed.slice(0, 6),
  }
})

/** 连线区尺寸（像素） */
const LINK_W = 56
const LINK_H = 220

/**
 * 均匀抽稀采样点。
 *
 * 航迹动辄上百个点，全画会糊成一片实心带，
 * 这里按固定上限等距抽取，保证视觉密度可控。
 */
function sampleMarks(pts: { x: number; y: number }[], max = 14): { x: number; y: number }[] {
  if (pts.length <= max) return pts
  const step = (pts.length - 1) / (max - 1)
  const out: { x: number; y: number }[] = []
  for (let i = 0; i < max; i++) out.push(pts[Math.round(i * step)])
  return out
}

/**
 * 连线纵坐标。
 *
 * 按「源航迹 → 全局目标」的真实配对逐条连线：
 * 已确认的排在前面且用绿色实线，待确认的用灰色虚线。
 */
function linkY(l: { from: string; state: string }, i: number): number {
  return 26 + (i % 6) * 30
}

/**
 * ENU 图宽度（像素）—— 与容器等宽，SVG 单位 = CSS 像素，
 * 避免 `preserveAspectRatio="none"` 把文字横向拉变形。
 */
const chartW = ref(420)

/**
 * 航迹对比图绘制高度。
 *
 * 固定 260px 时，容器有 380px 高，图表就缩在中间、上下留白。
 * 跟着容器走才能铺满右上面板。
 */
const overlayH = ref(260)

let ro: ResizeObserver | null = null
onMounted(() => {
  const el = document.querySelector('.ws-mod--assoc .ws-svg')
  if (!el) return
  ro = new ResizeObserver((entries) => {
    const rect = entries[0]?.contentRect
    const w = rect?.width ?? 0
    const h = rect?.height ?? 0
    if (w > 40) chartW.value = Math.round(w)
    if (h > 80) overlayH.value = Math.round(h)
  })
  ro.observe(el)
})
onBeforeUnmount(() => ro?.disconnect())

/* ---------- 右：航迹对比（ENU） ---------- */
const overlay = computed(() => {
  const H = Math.max(160, overlayH.value)
  const W = chartW.value
  const all: { east: number; north: number }[] = []
  for (const t of visibleTracks.value) {
    for (const s of t.sources) {
      for (const p of s.pts) {
        if (Number.isFinite(p.east) && Number.isFinite(p.north)) all.push({ east: p.east, north: p.north })
      }
    }
  }
  if (!all.length) {
    return { H, paths: [] as any[], ring: null as { x: number; y: number; gid: string } | null, legend: [] as any[], ax: [] as any[] }
  }
  const [eMin, eMax] = extent(all.map((p) => p.east))
  const [nMin, nMax] = extent(all.map((p) => p.north))
  const padE = Math.max(40, (eMax - eMin) * 0.1)
  const padN = Math.max(40, (nMax - nMin) * 0.1)
  const e0 = eMin - padE
  const e1 = eMax + padE
  const n0 = nMin - padN
  const n1 = nMax + padN
  const X = (v: number) => scale(v, e0, e1, 48, W - 16)
  const Y = (v: number) => scale(v, n0, n1, H - 28, 16)

  const paths = visibleTracks.value.map((t) => ({
    globalId: t.globalId,
    dim: !!props.selectedTargetId && props.selectedTargetId !== t.globalId,
    lines: t.sources
      .filter((s) => s.pts.length > 1)
      .map((s) => ({
        sourceId: s.sourceId,
        kind: s.kind,
        d: s.pts
          .filter((p) => Number.isFinite(p.east) && Number.isFinite(p.north))
          .map((p, i) => `${i ? 'L' : 'M'}${X(p.east).toFixed(1)},${Y(p.north).toFixed(1)}`)
          .join(' '),
        head: s.pts.length
          ? { x: X(s.pts[s.pts.length - 1].east), y: Y(s.pts[s.pts.length - 1].north) }
          : null,
        /* 均匀抽稀的采样点标记（最多 14 个，避免糊成一片） */
        marks: sampleMarks(
          s.pts
            .filter((p) => Number.isFinite(p.east) && Number.isFinite(p.north))
            .map((p) => ({ x: X(p.east), y: Y(p.north) })),
        ),
      })),
  }))

  /* 「全局目标」参考圈 —— 取已确认配对的中心 */
  let ring: { x: number; y: number; gid: string } | null = null
  const conf = props.mod.confirmed.filter((c) => c.globalId)
  if (conf.length) {
    const t = props.mod.tracks.find((x) => x.globalId === conf[0].globalId)
    if (t) {
      const pts = t.sources.flatMap((s) => s.pts)
      if (pts.length) {
        const ce = pts.reduce((a, p) => a + (p.east ?? 0), 0) / pts.length
        const cn = pts.reduce((a, p) => a + (p.north ?? 0), 0) / pts.length
        ring = { x: X(ce), y: Y(cn), gid: t.globalId }
      }
    }
  }

  /* 图例：按源分色（效果图左下角：雷达(R017) / AIS(A012) / 全局目标G001） */
  const seen = new Map<string, { color: string; label: string }>()
  for (const path of paths) {
    for (const ln of path.lines) {
      const key = `${ln.kind}:${ln.sourceId}`
      if (seen.has(key)) continue
      seen.set(key, {
        color: KIND_COLOR[ln.kind] ?? PALETTE.cyan,
        label: `${KIND_LABEL[ln.kind] ?? ln.kind}(${ln.sourceId})`,
      })
    }
  }
  const legend = [...seen.values()].slice(0, 4)

  return {
    H,
    paths,
    ring,
    legend,
    ax: [
      { x: 48, y: H - 28, label: `E ${e0.toFixed(0)}` },
      { x: W - 16, y: H - 28, label: `E ${e1.toFixed(0)}` },
      { x: 48, y: 16, label: `N ${n1.toFixed(0)}` },
    ],
  }
})

</script>

<template>
  <section class="ws-mod ws-mod--assoc">
    <header class="ws-mod-head">
      <h3 class="ws-mod-title">
        <span class="ws-mod-ico">
          <svg viewBox="0 0 24 24" v-html="MOD_ICON" />
        </span>
        航迹关联
        <em class="ws-mod-sub">| 跨源匹配 · 统一目标编号</em>
      </h3>
      <div class="ws-mod-right">
        <button class="ws-mod-more" type="button" title="查看详情" @click="emit('open-detail', 'assoc')">查看详情 ↗</button>
      </div>
    </header>

    <div v-if="!mod.hasData" class="ws-mod-empty">暂无数据</div>

    <template v-else>
      <div class="ws-assoc-main">
        <!-- ===== 左：航迹匹配三列（药丸 + 箭头连线） ===== -->
        <div class="ws-assoc-match">
          <div class="ws-match-cols" :style="{ '--ncol': matchCols.srcCols.length }">
            <!-- 各感知源航迹列（雷达航迹 / AIS航迹 / …） -->
            <div
              v-for="(col, ci) in matchCols.srcCols"
              :key="col.kind"
              class="ws-match-col"
              :style="{ 'grid-column': ci + 1 }"
            >
              <h5>{{ (KIND_LABEL[col.kind] ?? col.kind) + '航迹' }}</h5>
              <div class="ws-trk-boxes">
                <button
                  v-for="it in col.items"
                  :key="it.sourceId"
                  type="button"
                  class="ws-trk-box"
                  :style="{ '--c': KIND_COLOR[col.kind] ?? PALETTE.cyan }"
                >
                  {{ it.sourceId }}
                </button>
              </div>
            </div>

            <!-- 连接线区：实心箭头（效果图上是绿色/青色实心箭头） -->
            <div class="ws-match-links" :style="{ 'grid-column': matchCols.srcCols.length + 1 }">
              <svg :viewBox="`0 0 ${LINK_W} ${LINK_H}`" class="ws-ml-svg" preserveAspectRatio="none">
                <g v-for="(l, i) in matchCols.links" :key="i">
                  <line
                    :x1="4"
                    :y1="linkY(l, i)"
                    :x2="LINK_W - 10"
                    :y2="linkY(l, i)"
                    :stroke="l.state === 'confirmed' ? '#3ddc97' : 'rgba(143,185,204,0.5)'"
                    stroke-width="1.6"
                    :stroke-dasharray="l.state === 'confirmed' ? 'none' : '4 3'"
                  />
                  <path
                    :d="`M ${LINK_W - 10} ${linkY(l, i) - 4} L ${LINK_W - 2} ${linkY(l, i)} L ${LINK_W - 10} ${linkY(l, i) + 4} Z`"
                    :fill="l.state === 'confirmed' ? '#3ddc97' : 'rgba(143,185,204,0.5)'"
                  />
                </g>
              </svg>
            </div>

            <!-- 全局目标列 -->
            <div class="ws-match-col" :style="{ 'grid-column': matchCols.srcCols.length + 2 }">
              <h5>全局目标</h5>
              <div class="ws-trk-boxes">
                <button
                  v-for="g in matchCols.gidList"
                  :key="g.gid"
                  type="button"
                  class="ws-trk-box ws-trk-box--gid"
                  :class="{ 'is-on': selectedTargetId === g.gid, 'is-pending': g.pending }"
                  @click="emit('select-target', g.gid)"
                >
                  {{ g.gid }}
                </button>
              </div>
              <div v-if="!matchCols.gidList.length" class="ws-none">暂无跨源目标</div>
            </div>
          </div>

          <!-- 对应关系 -->
          <div class="ws-pending">
            <h5>对应关系</h5>
            <div class="ws-pd">
              <span class="ws-pd-tag is-confirmed">已确认</span>
              <span class="ws-pd-pair">
                {{ matchCols.confirmed.slice(0, 4).map((c) => `${c.a.sourceId}→${c.globalId}`).join('、') || '—' }}
              </span>
            </div>
            <div class="ws-pd">
              <span class="ws-pd-tag is-pending">待确认</span>
              <span class="ws-pd-pair">
                <template v-if="matchCols.pending.length">
                  <em v-for="(c, i) in matchCols.pending" :key="i" class="ws-pd-item">
                    {{ c.a.sourceId }} ↔ {{ c.b.sourceId }}
                    <b>{{ c.cost.toFixed(2) }}</b>
                  </em>
                </template>
                <template v-else>—</template>
              </span>
            </div>
          </div>
        </div>

        <!-- ===== 右：航迹对比（ENU） ===== -->
        <div class="ws-assoc-ov">
          <h4>航迹对比（ENU） <em>统一坐标</em></h4>
          <svg
            :viewBox="`0 0 ${chartW} ${overlay.H}`"
            class="ws-svg"
            preserveAspectRatio="xMidYMid meet"
          >
            <line
              v-for="i in 6"
              :key="'gx' + i"
              :x1="(chartW / 6) * i"
              :x2="(chartW / 6) * i"
              y1="0"
              :y2="overlay.H - 22"
              :stroke="PALETTE.grid"
              stroke-width="1"
            />
            <line
              v-for="i in 5"
              :key="'gy' + i"
              x1="0"
              :x2="chartW"
              :y1="16 + ((overlay.H - 44) / 4) * (i - 1)"
              :y2="16 + ((overlay.H - 44) / 4) * (i - 1)"
              :stroke="PALETTE.grid"
              stroke-width="1"
            />
            <g v-for="p in overlay.paths" :key="p.globalId" :opacity="p.dim ? 0.14 : 1">
              <g v-for="l in p.lines" :key="l.sourceId">
                <path :d="l.d" fill="none" :stroke="KIND_COLOR[l.kind] ?? PALETTE.cyan" stroke-width="2" />
                <!-- 采样点标记 -->
                <circle
                  v-for="(pt, pi) in l.marks"
                  :key="'m' + pi"
                  :cx="pt.x"
                  :cy="pt.y"
                  r="2.6"
                  :fill="KIND_COLOR[l.kind] ?? PALETTE.cyan"
                />
                <!-- 航迹末端箭头 -->
                <circle v-if="l.head" :cx="l.head.x" :cy="l.head.y" r="4" :fill="KIND_COLOR[l.kind] ?? PALETTE.cyan" />
              </g>
            </g>
            <!-- 全局目标重合圈 -->
            <g v-if="overlay.ring">
              <circle :cx="overlay.ring.x" :cy="overlay.ring.y" r="13" fill="none" :stroke="PALETTE.cyan" stroke-width="2" />
              <text :x="overlay.ring.x + 17" :y="overlay.ring.y + 4" class="ws-svg-cyan">
                {{ overlay.ring.gid }}
              </text>
            </g>
            <text v-for="a in overlay.ax" :key="'a' + a.label" :x="a.x" :y="a.y + 13" class="ws-svg-tick">
              {{ a.label }}
            </text>
            <text x="6" :y="14" class="ws-svg-tick">N/米</text>
            <text :x="chartW - 36" :y="overlay.H - 6" class="ws-svg-tick">E/米</text>
          </svg>
          <div class="ws-legend">
            <span v-for="(lg, li) in overlay.legend" :key="'lg' + li">
              <i :style="{ background: lg.color }" />{{ lg.label }}
            </span>
            <span v-if="overlay.ring"><i style="background: #22e4ee" />全局目标{{ overlay.ring.gid }}</span>
          </div>
        </div>
      </div>

      <!-- ===== 底部关键统计 ===== -->
      <footer class="ws-mod-foot">
        <div class="ws-kpi"><span>确认关联</span><b class="is-ok">{{ mod.counts.confirmed }}</b></div>
        <div class="ws-kpi"><span>待确认</span><b :class="mod.counts.pending ? 'is-warn' : ''">{{ mod.counts.pending }}</b></div>
        <div class="ws-kpi"><span>未关联</span><b :class="mod.counts.unlinked ? 'is-bad' : ''">{{ mod.counts.unlinked }}</b></div>
        <div class="ws-kpi"><span>全局目标</span><b>{{ visibleTracks.length }}</b></div>
        <div class="ws-kpi ws-kpi--grow">
          <span>统一编号</span>
          <div class="ws-srcchips">
            <button
              v-for="t in visibleTracks"
              :key="t.globalId"
              type="button"
              class="ws-chip"
              :class="{ 'is-on': selectedTargetId === t.globalId }"
              @click="emit('select-target', t.globalId)"
            >
              {{ t.globalId }}
              <em>{{ t.domain === 'underwater' ? '水下' : '水面' }}</em>
            </button>
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
.ws-assoc-main {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1.15fr 1fr;
  gap: 12px;
  padding-top: 8px;
}

/* ---- 左：匹配三列 ---- */
.ws-assoc-match {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: rgba(2, 16, 32, 0.5);
  border: 1px solid rgba(34, 228, 238, 0.14);
  border-radius: 4px;
  padding: 7px 9px;
}
.ws-match-cols {
  flex: 1;
  min-height: 0;
  display: grid;
  /* 源航迹列（ncol 列） | 连接列 | 统一编号列
     统一编号列给足宽度 —— 编号是这一块的主角，
     挤在 58px 里只能看到半截编号。 */
  grid-template-columns:
    repeat(var(--ncol, 2), minmax(56px, 1fr))
    58px
    minmax(96px, 1.15fr);
  gap: 5px;
}
.ws-match-links { min-width: 0; }
.ws-match-col {
  min-height: 0;
  overflow: auto;
  /* 纵向 flex：让 .ws-trk-boxes 能 flex:1 撑满列高 */
  display: flex;
  flex-direction: column;
}
.ws-match-col h5 {
  margin: 0 0 8px;
  font-size: 14.5px;
  line-height: 1.3;
  font-weight: 600;
  color: #22e4ee;
  text-align: center;
  flex: 0 0 auto;
}
/* ========== 航迹框（效果图：圆角描边框 + 编号） ========== */
/**
 * 航迹框列 —— 顶部对齐，框之间均匀分布。
 *
 * 【为什么要 justify-content】
 * 每列的条目数不一样（雷达 5 条、雷达目标 3 条、全局目标 3 条），
 * 之前全部顶部对齐，条目少的列下方空一大条。
 * 这里用 `space-between` + `flex: 1` 让框均匀撑满列高，
 * 三列的框在视觉上对齐成行 —— 正是效果图的排法。
 */
.ws-trk-boxes {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
}
.ws-trk-box {
  width: 100%;
  padding: 9px 6px;
  text-align: center;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: 0.6px;
  color: #d9f2ff;
  background: rgba(8, 40, 66, 0.85);
  border: 1px solid var(--c, #22e4ee);
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.15s;
  box-shadow: 0 0 8px rgba(34, 228, 238, 0.1);
}
.ws-trk-box:hover { background: rgba(14, 66, 104, 0.95); box-shadow: 0 0 12px rgba(34, 228, 238, 0.3); }
.ws-trk-box--gid {
  color: #22e4ee;
  border-color: rgba(34, 228, 238, 0.75);
  background: rgba(6, 52, 84, 0.9);
}
.ws-trk-box--gid.is-on {
  color: #04141f;
  background: linear-gradient(135deg, #22e4ee, #17b7c9);
  box-shadow: 0 0 14px rgba(34, 228, 238, 0.45);
}
.ws-trk-box--gid.is-pending {
  color: #ffb547;
  border-color: rgba(255, 181, 71, 0.65);
  border-style: dashed;
  background: rgba(60, 42, 12, 0.5);
}

/* 药丸框 —— 效果图：源航迹是暗底白字，全局目标是青框青字 */
.ws-pill {
  display: block;
  width: 100%;
  padding: 5px 6px;
  margin-bottom: 7px;
  text-align: center;
  font-size: 14.5px;
  font-weight: 600;
  letter-spacing: 0.5px;
  font-family: 'Consolas', 'Courier New', monospace;
  color: #d6e9f2;
  background: rgba(4, 25, 46, 0.85);
  border: 1px solid rgba(143, 185, 204, 0.3);
  border-radius: 4px;
  cursor: default;
}
.ws-pill--gid {
  cursor: pointer;
  color: #22e4ee;
  border-color: rgba(34, 228, 238, 0.55);
  background: rgba(6, 45, 72, 0.85);
}
.ws-pill--gid:hover { background: rgba(34, 228, 238, 0.14); }
.ws-pill--gid.is-on {
  color: #04141f;
  background: linear-gradient(135deg, #22e4ee, #17b7c9);
  border-color: #22e4ee;
  box-shadow: 0 0 12px rgba(34, 228, 238, 0.45);
}
.ws-ml-svg { width: 100%; height: 100%; }

/* 待确认 */
.ws-pending {
  border-top: 1px dashed rgba(143, 185, 204, 0.22);
  padding-top: 6px;
  max-height: 84px;
  overflow: auto;
}
.ws-pending h5 { margin: 0 0 4px; font-size: 14px; color: #22e4ee; font-weight: 600; }
.ws-pd {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 14.5px;
  padding: 3px 0;
  border-bottom: 1px dashed rgba(143, 185, 204, 0.1);
}
.ws-pd-tag {
  flex: 0 0 auto;
  font-size: 15px;
  padding: 0 6px;
  border-radius: 8px;
  line-height: 15px;
}
.ws-pd-tag.is-confirmed { color: #39d98a; background: rgba(57, 217, 138, 0.16); border: 1px solid rgba(57, 217, 138, 0.45); }
.ws-pd-tag.is-pending { color: #ffb547; background: rgba(255, 181, 71, 0.16); border: 1px solid rgba(255, 181, 71, 0.45); }
.ws-pd-pair { color: #e1f7ff; display: flex; flex-wrap: wrap; gap: 4px 10px; }
.ws-pd-item { font-style: normal; color: #7fa8bd; }
.ws-pd-item b { color: #ffb547; font-weight: 600; margin-left: 3px; }

/* ---- 右：航迹对比 ---- */
.ws-assoc-ov {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(2, 16, 32, 0.5);
  border: 1px solid rgba(34, 228, 238, 0.14);
  border-radius: 4px;
  padding: 7px 9px;
}
.ws-assoc-ov h4 {
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
.ws-assoc-ov h4 em { font-style: normal; color: #7fa8bd; font-weight: 400; font-size: 15px; }
.ws-svg { flex: 1; width: 100%; min-height: 0; overflow: visible; }
.ws-svg-tick { fill: #7fa8bd; font-size: 14px; text-anchor: middle; }
.ws-svg-amber { fill: #ffb547; font-size: 15px; }
.ws-svg-cyan { fill: #22e4ee; font-size: 15px; font-weight: 700; }
.ws-legend {
  display: flex;
  gap: 11px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 14.5px;
  color: #8fb4c8;
}
.ws-legend span { display: inline-flex; align-items: center; gap: 5px; font-size: 14.5px; }
.ws-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.ws-none { color: #7fa8bd; text-align: center; padding: 12px 0 !important; font-size: 14px; }

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
  cursor: pointer;
}
.ws-chip em { font-style: normal; color: #7fa8bd; }
.ws-chip.is-on { border-color: #22e4ee; background: rgba(34, 228, 238, 0.12); }
</style>
