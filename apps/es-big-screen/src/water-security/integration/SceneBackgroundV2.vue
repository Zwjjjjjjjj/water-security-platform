<script setup lang="ts">
/**
 * V2 写实场景底图层
 *
 * 【职责】
 * 把「核电厂区航拍图 + 海面贴图 + 护岸/防波堤 + 灯桩」合成一张
 * 正俯视的写实底图，铺在 ECharts 航迹层之下。
 *
 * 【与 V1 的差异】
 *  - V1：手绘 SVG 色块底图 + 45° 立牌建筑（2.5D）
 *  - V2：一张写实航拍底图铺满视口 + 正俯视实体贴图（2D）
 *
 * 【素材缺失时的降级】
 * 每类素材独立探测；缺失的那一层直接不渲染，
 * 下面一层（海面渐变 → 矢量岸线 → 矢量防波堤）自动顶上，
 * 因此「先搭框架、后补素材」可以无缝衔接。
 */
import { computed } from 'vue'
import { getSceneAssetsV2, type SceneAssetV2 } from './scene-assets.v2'
import { V2_ASSET, BG_ANCHOR, BG_UNIT } from './scenes.config.v2'
import {
  STRUCTURES,
  CHANNEL_WALLS,
  PIERS,
  VERTICAL_PROPS,
  type StructureSegment,
} from './structures.v2'

const props = defineProps({
  /** 素材是否已加载完成（由父组件传入，用于触发重渲染） */
  assetsReady: { type: Boolean, default: false },
  /** 容器像素尺寸 */
  chartW: { type: Number, default: 960 },
  chartH: { type: Number, default: 500 },
  /** 当前视口（米） */
  xMin: { type: Number, default: -2600 },
  xMax: { type: Number, default: 1000 },
  yMin: { type: Number, default: 3000 },
  yMax: { type: Number, default: 5000 },
  /** 绘图区边距（像素） */
  gridLeft: { type: Number, default: 8 },
  gridRight: { type: Number, default: 8 },
  gridTop: { type: Number, default: 8 },
  gridBottom: { type: Number, default: 8 },
})

/* ===== 素材查找 =====
 *
 * 【为什么依赖 assetsReady】
 * getSceneAssetsV2() 是同步读取缓存的，
 * 而缓存要等 manifest 加载 + 逐个 HEAD 探测完成后才填充。
 * 若 computed 不依赖 assetsReady，
 * 首次渲染时缓存为空 → computed 结果被缓存住 → 素材永远不出现。
 *
 * 显式依赖 props.assetsReady 可让缓存变化时重新计算。 */
function tex(id: string): SceneAssetV2 | undefined {
  // 依赖 props.assetsReady，确保素材加载完成后重新求值
  void props.assetsReady
  return getSceneAssetsV2().find((a) => a.id === id)
}

const campusTex = computed(() => tex(V2_ASSET.campusAerial))
const seaTex = computed(() => tex(V2_ASSET.seaSurface))
const seaNormalTex = computed(() => tex(V2_ASSET.seaNormal))
const breakwaterTex = computed(() => tex(V2_ASSET.breakwaterTex))
const jettyTex = computed(() => tex(V2_ASSET.jettyTex))
const beaconTex = computed(() => tex(V2_ASSET.navBeacon))
const radarTex = computed(() => tex(V2_ASSET.radarStation))

/* ===== 坐标换算 ===== */

/** 当前视图的 米→像素 比例（两轴一致，保证等比例） */
const pxPerM = computed(() => {
  const plotW = Math.max(50, props.chartW - props.gridLeft - props.gridRight)
  return plotW / (props.xMax - props.xMin)
})

/** 米制 → 容器像素（与 ECharts 完全一致） */
function meterToPx(e: number, n: number): [number, number] {
  return [
    props.gridLeft + (e - props.xMin) * pxPerM.value,
    props.gridTop + (props.yMax - n) * pxPerM.value,
  ]
}

/** 设计坐标 → 容器像素 */
function designToPx(dx: number, dy: number): [number, number] {
  const mE = BG_ANCHOR.e + dx * BG_UNIT.e
  const mN = BG_ANCHOR.n - dy * BG_UNIT.n
  return meterToPx(mE, mN)
}

/** 设计单位 → 像素 */
function unitToPx(u: number): number {
  return u * BG_UNIT.e * pxPerM.value
}

/**
 * 设计坐标系 → 容器像素的 SVG 变换矩阵。
 * 用于那些直接用设计坐标书写的路径（陆域、岸线）。
 */
const designTransform = computed(() => {
  const [x0, y0] = designToPx(0, 0)
  const [x1, y1] = designToPx(1000, 500)
  const sx = (x1 - x0) / 1000
  const sy = (y1 - y0) / 500
  return `translate(${x0.toFixed(2)},${y0.toFixed(2)}) scale(${sx.toFixed(8)},${sy.toFixed(8)})`
})

/* ===== 写实底图（厂区航拍图）===== */
const campusImg = computed(() => {
  const t = campusTex.value
  if (!t) return null
  const bounds = t.world_bounds_enu || null
  let x = 0
  let y = 0
  let w = props.chartW
  let h = props.chartH
  if (bounds) {
    /* 素材提供了 ENU 范围 → 精确配准 */
    const [bx0, by0, bx1, by1] = bounds
    const [p0x, p0y] = meterToPx(bx0, by1)
    const [p1x, p1y] = meterToPx(bx1, by0)
    x = p0x
    y = p0y
    w = p1x - p0x
    h = p1y - p0y
  } else {
    /* 回退：设计坐标 (0,0) 在左上，(1000,500) 在右下 */
    const [dx0, dy0] = designToPx(0, 0)
    const [dx1, dy1] = designToPx(1000, 500)
    x = dx0
    y = dy0
    w = dx1 - dx0
    h = dy1 - dy0
  }
  return { href: t.url, x, y, w, h }
})

/* ===== 海面贴图 ===== */
const seaPattern = computed(() => {
  const t = seaTex.value
  if (!t) return null
  /* 每 200 设计单位平铺一次 ≈ 自然的纹理尺度 */
  const tile = unitToPx(200)
  return { href: t.url, tile: Math.max(40, tile) }
})

/* ===== 矢量回退：结构线 ===== */
function ribbonPath(seg: StructureSegment): string {
  const pts = seg.pts.map(([dx, dy]) => designToPx(dx, dy))
  if (pts.length < 2) return ''
  const half = unitToPx(seg.width) / 2
  const left: string[] = []
  const right: string[] = []
  for (let i = 0; i < pts.length; i++) {
    const prev = pts[Math.max(0, i - 1)]
    const next = pts[Math.min(pts.length - 1, i + 1)]
    let dx = next[0] - prev[0]
    let dy = next[1] - prev[1]
    const len = Math.hypot(dx, dy) || 1
    dx /= len
    dy /= len
    const nx = -dy * half
    const ny = dx * half
    left.push(`${(pts[i][0] + nx).toFixed(2)},${(pts[i][1] + ny).toFixed(2)}`)
    right.push(`${(pts[i][0] - nx).toFixed(2)},${(pts[i][1] - ny).toFixed(2)}`)
  }
  return `M${left.join('L')}L${right.reverse().join('L')}Z`
}

interface RenderSeg {
  key: string
  d: string
  /** 设计单位宽度（转像素画描边用） */
  widthU: number
  fill: string
  stroke?: string
  strokeW?: number
  opacity?: number
  texHref?: string
}

const allSegs = computed<RenderSeg[]>(() => {
  const groups = [STRUCTURES, CHANNEL_WALLS, PIERS]
  const out: RenderSeg[] = []
  let i = 0
  for (const g of groups) {
    for (const seg of g) {
      const t = seg.texId ? tex(seg.texId) : undefined
      out.push({
        key: `seg-${i++}`,
        d: ribbonPath(seg),
        widthU: seg.width,
        fill: t ? 'none' : (seg.fill || seg.top),
        stroke: t ? undefined : (seg.stroke || seg.edge || seg.side),
        strokeW: t ? undefined : seg.strokeW,
        opacity: seg.opacity ?? 1,
        texHref: t?.url,
      })
    }
  }
  return out
})

/* ===== 航标灯桩 / 浮标 ===== */
const propIcons = computed(() => {
  const out: Array<{
    key: string
    href?: string
    x: number
    y: number
    size: number
    shape: string
    color: string
  }> = []
  VERTICAL_PROPS.forEach((p, i) => {
    if (p.enabled === false) return
    const [px, py] = designToPx(p.dx, p.dy)
    const size = unitToPx(p.size)
    const t = p.id ? tex(p.id) : undefined
    out.push({
      key: `prop-${i}`,
      href: t?.url,
      x: px,
      y: py,
      size,
      shape: p.shape,
      color: p.color,
    })
  })
  return out
})

/* ===== 雷达站 ===== */
const radarIcon = computed(() => {
  const t = radarTex.value
  if (!t) return null
  const [px, py] = designToPx(92, 356)
  return { href: t.url, x: px, y: py, size: unitToPx(26) }
})

/** 是否处于「写实模式」：厂区航拍底图与海面贴图都到位 */
const photoreal = computed(() => !!(campusTex.value && seaTex.value))

/* ===== 供模板调用的辅助 ===== */
/** 结构线段的设计单位宽度 → 像素描边宽度 */
function segStrokePx(widthU: number): number {
  return Math.max(1, unitToPx(widthU))
}
</script>

<template>
  <svg
    class="ws-v2-bg"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <!-- 海面渐变（回退用）：近岸浅青绿 → 远海深蓝 -->
      <linearGradient id="v2Water" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#1a5f6b" />
        <stop offset="0.28" stop-color="#124a5e" />
        <stop offset="0.62" stop-color="#0d3549" />
        <stop offset="1" stop-color="#082334" />
      </linearGradient>
      <!-- 近岸浅水带 -->
      <linearGradient id="v2Shore" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#3aa89b" stop-opacity="0.42" />
        <stop offset="1" stop-color="#3aa89b" stop-opacity="0" />
      </linearGradient>
      <!-- 陆域（回退用） -->
      <linearGradient id="v2Land" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#24413a" />
        <stop offset="0.6" stop-color="#1c332e" />
        <stop offset="1" stop-color="#142622" />
      </linearGradient>

      <!-- 海面贴图（可平铺） -->
      <pattern
        v-if="seaPattern"
        id="v2SeaTile"
        :width="seaPattern.tile"
        :height="seaPattern.tile"
        patternUnits="userSpaceOnUse"
      >
        <image
          :href="seaPattern.href"
          x="0"
          y="0"
          :width="seaPattern.tile"
          :height="seaPattern.tile"
          preserveAspectRatio="none"
        />
      </pattern>
    </defs>

    <!-- ===== 1. 海面基底 ===== -->
    <rect x="0" y="0" :width="chartW" :height="chartH" fill="url(#v2Water)" />

    <!-- ===== 2. 海面写实贴图（平铺）===== -->
    <g v-if="seaPattern" :opacity="photoreal ? 0.92 : 0.55">
      <rect
        x="0"
        y="0"
        :width="chartW"
        :height="chartH"
        fill="url(#v2SeaTile)"
      />
    </g>

    <!-- ===== 3. 陆域（仅在写实厂区底图缺失时用矢量回退）===== -->
    <g v-if="!campusImg">
      <path
        d="M0,0 H1000 L1000,158 L960,166 L900,156 L840,168 L780,150 L720,164 L660,180 L600,170 L540,152 L480,166 L420,178 L360,162 L300,172 L240,158 L180,168 L120,154 L60,160 L0,152 Z"
        fill="url(#v2Land)"
        :transform="designTransform"
      />
      <path
        d="M0,152 L60,160 L120,154 L180,168 L240,158 L300,172 L360,162 L420,178 L480,166 L540,152 L600,170 L660,180 L720,164 L780,150 L840,168 L900,156 L960,166 L1000,158 L1000,170 L960,178 L900,168 L840,180 L780,162 L720,176 L660,192 L600,182 L540,164 L480,178 L420,190 L360,174 L300,184 L240,170 L180,180 L120,166 L60,172 L0,164 Z"
        fill="url(#v2Shore)"
        :transform="designTransform"
      />
    </g>

    <!-- ===== 4. 厂区写实航拍底图 ===== -->
    <image
      v-if="campusImg"
      class="ws-v2-campus"
      :href="campusImg.href"
      :x="campusImg.x"
      :y="campusImg.y"
      :width="campusImg.w"
      :height="campusImg.h"
      preserveAspectRatio="none"
    />

    <!-- ===== 5. 结构线（护岸 / 防波堤 / 明渠 / 栈桥）===== -->
    <g class="ws-v2-structures">
      <template v-for="s in allSegs" :key="s.key">
        <!-- 贴图版：沿路径描一条石砌纹理带 -->
        <path
          v-if="s.texHref"
          :d="s.d"
          :stroke="s.texHref"
          :stroke-width="segStrokePx(s.widthU)"
          fill="none"
          stroke-linejoin="round"
        />
        <!-- 矢量版 -->
        <path
          v-else
          :d="s.d"
          :fill="s.fill"
          :stroke="s.stroke"
          :stroke-width="s.strokeW"
          :opacity="s.opacity"
          stroke-linejoin="round"
        />
      </template>
    </g>

    <!-- ===== 6. 航标灯桩 / 浮标 ===== -->
    <g class="ws-v2-props">
      <template v-for="p in propIcons" :key="p.key">
        <image
          v-if="p.href"
          :href="p.href"
          :x="p.x - p.size / 2"
          :y="p.y - p.size / 2"
          :width="p.size"
          :height="p.size"
          preserveAspectRatio="xMidYMid meet"
        />
        <!-- 回退：简化符号 -->
        <g v-else :transform="`translate(${p.x},${p.y})`">
          <circle
            v-if="p.shape === 'beacon'"
            r="4"
            :fill="p.color"
            stroke="#f0f4f8"
            stroke-width="1.4"
          />
          <circle
            v-else
            :r="p.size * 0.3"
            :fill="p.color"
            stroke="rgba(255,255,255,0.4)"
            stroke-width="1"
          />
        </g>
      </template>
    </g>

    <!-- ===== 7. 雷达站 ===== -->
    <image
      v-if="radarIcon"
      :href="radarIcon.href"
      :x="radarIcon.x - radarIcon.size / 2"
      :y="radarIcon.y - radarIcon.size / 2"
      :width="radarIcon.size"
      :height="radarIcon.size"
      preserveAspectRatio="xMidYMid meet"
    />
  </svg>
</template>

<style scoped>
.ws-v2-bg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  /* 与 .ws-harbor-bg（V1 底图）保持一致：不抬高层级，
     靠 DOM 顺序排在 .ws-chart 之前，
     航迹/船/标签自然叠在底图之上，避免「底图一层、航迹一层」的割裂。 */
  z-index: auto;
}
/* 厂区航拍底图：略微提亮，让它在深色海图上「浮」起来 */
.ws-v2-campus {
  filter: brightness(1.06) saturate(1.02);
}
</style>
