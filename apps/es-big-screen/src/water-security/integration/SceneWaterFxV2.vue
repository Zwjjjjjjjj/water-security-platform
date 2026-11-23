<script setup lang="ts">
/**
 * V2 斜视场景的「海面动效 + 水岸细节 + 航标」覆盖层。
 *
 * 【为什么单独一层】
 * 这些元素与背景图是**同一套图像坐标系**里的东西：
 *   · 泡沫要贴着防波堤画；
 *   · 波纹要避开陆地；
 *   · 航标要立在堤头。
 * 若把它们画在 ENU 坐标层里，缩放/平移时就会与背景错位。
 *
 * 所以本组件复刻 `SceneObliqueV2` 的映射方式
 * （同一份 `computeObliqueFit`），保证与背景像素级对齐。
 *
 * 【图层顺序（自下而上）】
 *   1. 海面细波纹（最淡，在水下）—— 会**推进**，不是原地闪
 *   2. 岸线破碎泡沫 —— 会**涌上堤面再回落**
 *   3. 泊位标线
 *   4. 航标（浮标 / 灯桩）
 *   5. 船舶尾迹（最亮，在水上）—— Kelvin 波臂 + 螺旋桨白水
 */
import { computed } from 'vue'
import { computeObliqueFit } from './oblique-fit'
import { getSceneAssetsV2, type SceneAssetV2 } from './scene-assets.v2'
import { OBLIQUE_NAT_W, OBLIQUE_NAT_H } from './oblique-fit'
import {
  buildRipples,
  buildWake,
  buildWakeFoam,
  foamAlong,
  isOuterSide,
  NAV_AIDS,
  BERTH_MARKS,
  type FoamPatch,
  type NavAid,
  type WakeSegment,
} from './water-effects'
import { isWaterPx } from './harbor-water'

const props = defineProps({
  assetsReady: { type: Boolean, default: false },
  chartW: { type: Number, default: 1920 },
  chartH: { type: Number, default: 870 },
  xMin: { type: Number, default: -2600 },
  xMax: { type: Number, default: 1000 },
  yMin: { type: Number, default: 3000 },
  yMax: { type: Number, default: 5000 },
  gridLeft: { type: Number, default: 0 },
  gridRight: { type: Number, default: 0 },
  gridTop: { type: Number, default: 0 },
  gridBottom: { type: Number, default: 0 },
  /** 尾迹数据：由父组件按目标速度与历史生成 */
  wakes: { type: Array as () => WakeSegment[], default: () => [] },
})

/**
 * 背景映射参数 —— 与 `SceneObliqueV2` 完全相同，
 * 因此本层与背景严格重合。
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
    anchorX: 0,
    anchorY: 0,
  }),
)

/** 图片像素 → 容器像素 */
function toContainer(px: number, py: number): [number, number] {
  const f = fit.value
  return [f.left + px * f.r, f.top + py * f.r]
}

/** ENU → 图片像素（用于尾迹：ENU 点先转图片像素，再走同一套容器映射） */
const enuToImageFn = computed(() => {
  void props.assetsReady
  const a = 1536 / 3600
  const d = -960 / 2200
  const eOff = a * 2600
  const f = 40 - d * 5100
  return (e: number, n: number): [number, number] => [
    a * e + eOff,
    d * n + f,
  ]
})

/** 水域采样接口（底图掩码） */
const samplerIface = {
  isWater: (x: number, y: number) => isWaterPx(x, y),
  mapW: OBLIQUE_NAT_W,
  mapH: OBLIQUE_NAT_H,
}

/** 海面细波纹 */
const ripples = computed(() => {
  void props.assetsReady
  return buildRipples(samplerIface, 11, 30)
})

/**
 * 岸线破碎泡沫。
 *
 * 【只画迎浪侧】
 * 从底图掩码里取出岸线，再判断哪一侧是外海，
 * 只在外侧（迎浪面）生成泡沫团 —— 港池内侧保持平静。
 */
const foamPatches = computed<FoamPatch[]>(() => {
  void props.assetsReady
  const samplerIface = {
    isWater: (x: number, y: number) => isWaterPx(x, y),
    mapW: OBLIQUE_NAT_W,
    mapH: OBLIQUE_NAT_H,
  }

  /* 取三条扫描线带，覆盖画面中部到下部的水域 */
  const bands: [number, number][] = [
    [380, 620],
    [560, 780],
    [720, 940],
  ]
  const out: FoamPatch[] = []
  let seed = 3
  for (const [y0, y1] of bands) {
    const left = shorelineScan(samplerIface, y0, y1, 10, true)
    const right = shorelineScan(samplerIface, y0, y1, 10, false)
    for (const line of [left, right]) {
      if (line.length < 3) continue
      if (!isOuterSide(line, samplerIface)) continue
      out.push(...foamAlong(line, null, 14, seed++))
    }
  }
  return out
})

/**
 * 从扫描线取岸线（局部实现，避免与 water-effects 重复导出内部函数）。
 */
function shorelineScan(
  s: { isWater(x: number, y: number): boolean; mapW: number; mapH: number },
  yStart: number,
  yEnd: number,
  step: number,
  fromLeft: boolean,
): [number, number][] {
  const out: [number, number][] = []
  for (let y = yStart; y <= yEnd; y += step) {
    if (y < 0 || y >= s.mapH) continue
    if (fromLeft) {
      for (let x = 0; x < s.mapW; x += 4) {
        if (s.isWater(x, y)) {
          out.push([x, y])
          break
        }
      }
    } else {
      for (let x = s.mapW - 1; x >= 0; x -= 4) {
        if (s.isWater(x, y)) {
          out.push([x, y])
          break
        }
      }
    }
  }
  return out
}

/** 航标 */
const navAids = computed<NavAid[]>(() => NAV_AIDS)

/** 泊位标线 */
const berthMarks = computed(() => BERTH_MARKS)

/**
 * 已换算到容器像素的图形数据。
 *
 * 【为什么要在 script 里算好】
 * 模板表达式里写内联箭头函数会让 Vue 的模板类型检查
 * 失去对局部变量的推断（报 "Property 'cx' does not exist"），
 * 也让每次渲染都重复计算。统一在 computed 里算，模板只取结果。
 */
const ripplePaths = computed(() =>
  ripples.value.map((rp, i) => {
    /* 传播方向的 CSS 角度：图片像素坐标 y 向下，与屏幕一致 */
    const ang = (Math.atan2(rp.dx, -rp.dy) * 180) / Math.PI
    return {
      key: 'rp' + i,
      d: rp.points
        .map((p, j) => {
          const [cx, cy] = toContainer(p[0], p[1])
          return `${j === 0 ? 'M' : 'L'} ${cx.toFixed(1)} ${cy.toFixed(1)}`
        })
        .join(' '),
      opacity: rp.opacity,
      duration: rp.period + 's',
      delay: rp.phase.toFixed(2) + 's',
      /* 平移距离：速度 × 周期（走完一整个周期回到原位） */
      travel: rp.speed * rp.period,
      angle: ang.toFixed(2),
    }
  }),
)

/**
 * 泡沫团：把「涌浪周期」映射到 CSS 动画的关键帧。
 *
 * 【一个周期的物理剧本】
 *   0%   —— 涌浪在远处抬起（尺寸小、亮度低）
 *   35%  —— 波峰抵达堤前（开始前推）
 *   55%  —— **破碎爆发**（最亮、最大）
 *   80%  —— 白水沿堤回落
 *   100% —— 恢复平静
 *
 * 这些关键帧用 CSS `transform` + `opacity` 表达，
 * 交给合成器处理，不触发重排。
 */
const foamShapes = computed(() =>
  foamPatches.value.map((fp, i) => {
    const [cx, cy] = toContainer(fp.cx, fp.cy)
    const surge = fp.surge * fit.value.r
    return {
      key: 'fm' + i,
      cx,
      cy,
      rx: fp.r * fit.value.r,
      ry: fp.r * 0.5 * fit.value.r,
      opacity: fp.opacity,
      duration: fp.period.toFixed(2) + 's',
      delay: (-fp.phase / (2 * Math.PI) * fp.period).toFixed(2) + 's',
      /* 涌动位移向量（容器像素） */
      dx: (fp.nx * surge).toFixed(2),
      dy: (fp.ny * surge).toFixed(2),
      /* 爆发时的尺寸放大倍率 */
      burstScale: (1 + fp.burst * 0.55).toFixed(3),
    }
  }),
)

const berthLines = computed(() =>
  berthMarks.value.map((bm, i) => {
    const [x1, y1] = toContainer(bm.x1, bm.y1)
    const [x2, y2] = toContainer(bm.x2, bm.y2)
    return { key: 'bm' + i, x1, y1, x2, y2, opacity: bm.opacity }
  }),
)

const navAidShapes = computed(() =>
  navAids.value.map((na, i) => {
    const [cx, cy] = toContainer(na.px, na.py)
    return {
      key: 'na' + i,
      cx,
      cy,
      kind: na.kind,
      color: na.color,
      blinking: na.blinking,
      delay: na.phase.toFixed(2) + 's',
    }
  }),
)

/**
 * 尾迹：把图片像素坐标的折线转成容器像素的 path。
 *
 * 【按 kind 分组】
 * 不同类型的尾迹渲染方式不同：
 *   · `wash`  —— 螺旋桨白水：柔化、实心、亮
 *   · `arm`   —— Kelvin 波臂：细描边、半透明
 *   · `crest` —— 波峰高光：极细、偏亮
 */
const wakePaths = computed(() => {
  const r = fit.value.r
  return props.wakes.map((w, i) => {
    const d = w.points
      .map((p, j) => {
        const [cx, cy] = toContainer(p[0], p[1])
        return `${j === 0 ? 'M' : 'L'} ${cx.toFixed(1)} ${cy.toFixed(1)}`
      })
      .join(' ')
    return {
      key: `wake-${i}`,
      d,
      width: Math.max(0.5, w.width * r),
      opacity: w.opacity,
      kind: w.kind,
      period: w.period,
      drift: w.drift,
    }
  })
})

/**
 * 尾迹上的漂移泡沫白点。
 *
 * 【为什么要有】
 * 尾迹的几何是静止的，但真实的泡沫会**沿尾迹向后漂移并消散**。
 * 白点沿段以 `period` 为周期做往复位移 + 淡出，
 * 于是尾迹看起来是「留在水面上、正在消散的痕迹」，
 * 而不是「画在地图上的线」。
 */
const wakeFoamDots = computed(() => {
  const r = fit.value.r
  const segs = props.wakes
  const dots = buildWakeFoam(segs)
  const out: {
    key: string
    x: number
    y: number
    r: number
    opacity: number
    duration: string
    delay: string
    dx: string
    dy: string
  }[] = []
  for (let i = 0; i < dots.length; i++) {
    const dot = dots[i]
    const seg = segs[dot.seg]
    if (!seg) continue
    const p0 = seg.points[0]
    const p1 = seg.points[seg.points.length - 1]
    const px = p0[0] + (p1[0] - p0[0]) * dot.t
    const py = p0[1] + (p1[1] - p0[1]) * dot.t
    const [cx, cy] = toContainer(px, py)
    /* 漂移距离：沿段方向往后（指向船尾） */
    const segLen = Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) || 1
    const ux = (p1[0] - p0[0]) / segLen
    const uy = (p1[1] - p0[1]) / segLen
    const travel = Math.min(6 * r, segLen * r)
    out.push({
      key: `wfd-${i}`,
      x: cx,
      y: cy,
      r: dot.r * r,
      opacity: dot.opacity,
      duration: dot.period.toFixed(2) + 's',
      delay: (-dot.phase).toFixed(2) + 's',
      /* 漂移方向（容器像素单位向量） */
      dx: (-ux * travel).toFixed(2),
      dy: (-uy * travel).toFixed(2),
    })
  }
  return out
})

/** 尾迹是按图片像素生成的，需要父组件传入 ENU 历史 —— 这里做兜底空数组 */
function buildWakeFor(
  history: [number, number][],
  speedMps: number,
): WakeSegment[] {
  return buildWake(
    history.map((p, i) => ({ e: p[0], n: p[1], t: i * 1000 })),
    enuToImageFn.value,
    { speedMps, lengthM: 60, beamM: 12, nowMs: (history.length - 1) * 1000 },
  )
}

defineExpose({ buildWakeFor })
</script>

<template>
  <svg
    class="ws-water-fx"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <!-- 泡沫柔光：让白沫边缘柔化，不是生硬的圆点 -->
      <radialGradient id="fxFoam">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.95" />
        <stop offset="0.55" stop-color="#dff4ff" stop-opacity="0.5" />
        <stop offset="1" stop-color="#bfe8ff" stop-opacity="0" />
      </radialGradient>
      <!-- 灯桩光晕 -->
      <radialGradient id="fxBeacon">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.9" />
        <stop offset="0.3" stop-color="#ffd54a" stop-opacity="0.45" />
        <stop offset="1" stop-color="#ffd54a" stop-opacity="0" />
      </radialGradient>
      <!-- 尾迹柔化：船尾白水用，中心亮、边缘散 -->
      <linearGradient id="fxWake" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#d8f6ff" stop-opacity="0.85" />
        <stop offset="1" stop-color="#8fd8f0" stop-opacity="0.15" />
      </linearGradient>
      <!--
        泡沫的「迎浪面」渐变：
        白沫靠近堤体的一侧最亮（破碎最剧烈），
        向海一侧迅速衰减 —— 与真实破浪的形态一致。
      -->
      <radialGradient id="fxSurf" cx="0.5" cy="0.5" r="0.5">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.9" />
        <stop offset="0.6" stop-color="#e6f7ff" stop-opacity="0.42" />
        <stop offset="1" stop-color="#cfeaff" stop-opacity="0" />
      </radialGradient>
    </defs>

    <!-- ===== 第 1 层：海面细波纹（会向前推进，不是原地闪） ===== -->
    <g class="ws-fx-ripples">
      <path
        v-for="rp in ripplePaths"
        :key="rp.key"
        :d="rp.d"
        fill="none"
        stroke="#9fd8ee"
        :stroke-width="1"
        :stroke-opacity="rp.opacity"
        :style="{
          animationDuration: rp.duration,
          animationDelay: rp.delay,
          '--fx-travel': rp.travel.toFixed(1) + 'px',
          '--fx-angle': rp.angle + 'deg',
        }"
      />
    </g>

    <!-- ===== 第 2 层：岸线破碎泡沫（涌上堤面 → 爆发 → 回落） ===== -->
    <g class="ws-fx-foam">
      <g
        v-for="fp in foamShapes"
        :key="fp.key"
        :style="{
          animationDuration: fp.duration,
          animationDelay: fp.delay,
          '--fx-surge-x': fp.dx + 'px',
          '--fx-surge-y': fp.dy + 'px',
          '--fx-burst': fp.burstScale,
        }"
      >
        <ellipse
          :cx="fp.cx"
          :cy="fp.cy"
          :rx="fp.rx"
          :ry="fp.ry"
          fill="url(#fxSurf)"
          :opacity="fp.opacity"
        />
      </g>
    </g>

    <!-- ===== 第 3 层：泊位标线 ===== -->
    <g class="ws-fx-berth">
      <line
        v-for="bm in berthLines"
        :key="bm.key"
        :x1="bm.x1"
        :y1="bm.y1"
        :x2="bm.x2"
        :y2="bm.y2"
        stroke="#cfe8f5"
        stroke-width="1.6"
        :stroke-opacity="bm.opacity"
        stroke-dasharray="7 5"
      />
    </g>

    <!-- ===== 第 4 层：航标（浮标 / 堤头灯桩） ===== -->
    <g class="ws-fx-navaids">
      <g v-for="na in navAidShapes" :key="na.key" :transform="`translate(${na.cx} ${na.cy})`">
        <!-- 灯桩光晕（闪烁） -->
        <circle
          v-if="na.blinking"
          r="9"
          fill="url(#fxBeacon)"
          class="ws-fx-beacon-glow"
          :style="{ animationDuration: '2.2s', animationDelay: na.delay }"
        />
        <!-- 本体 -->
        <circle r="3.2" :fill="na.color" opacity="0.95" />
        <circle r="3.2" fill="none" stroke="#0a2233" stroke-width="0.9" opacity="0.8" />
        <!-- 浮标顶标 -->
        <rect v-if="na.kind === 'buoy'" x="-1.1" y="-6.4" width="2.2" height="3.4" :fill="na.color" opacity="0.9" />
      </g>
    </g>

    <!-- ===== 第 5 层：船舶尾迹 ===== -->
    <!--
      【分三层渲染的原因】
        · wash  —— 船尾螺旋桨白水：柔和、宽、亮
        · arm   —— Kelvin 波臂：细、半透明
        · crest —— 波峰高光：极细、偏亮
      三者的视觉语义完全不同，混在一起就变成了「一条白带」。
    -->
    <g class="ws-fx-wakes">
      <!-- 5a. Kelvin 波臂 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'arm')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="#bfe9f7"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <!-- 5b. 波峰高光 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'crest')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="#eafaff"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
      />
      <!-- 5c. 船尾白水 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'wash')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="url(#fxWake)"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
      />
    </g>

    <!-- ===== 第 6 层：尾迹上漂移的泡沫白点 ===== -->
    <!--
      让尾迹「动」起来的关键：
      白点沿尾迹向船尾方向持续漂移，并在漂移中淡出。
    -->
    <g class="ws-fx-wake-foam">
      <circle
        v-for="dot in wakeFoamDots"
        :key="dot.key"
        :cx="dot.x"
        :cy="dot.y"
        :r="dot.r"
        fill="#ffffff"
        :opacity="dot.opacity"
        :style="{
          animationDuration: dot.duration,
          animationDelay: dot.delay,
          '--fx-drift-x': dot.dx,
          '--fx-drift-y': dot.dy,
        }"
      />
    </g>
  </svg>
</template>

<style scoped>
.ws-water-fx {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 1;
}

/* ================================================================
   第 1 层：海面细波纹 —— 向前推进
   ================================================================ */
/**
 * 【为什么要平移而不是只改透明度】
 * 真实的涌浪是**传播**的：波峰自外海向岸边推进。
 * 只做透明度起伏的话，海面看起来像「贴了一层花纹」，
 * 而不是在「动」。
 *
 * 位移用 `--fx-travel`（速度 × 周期）与 `--fx-angle`（传播方向）
 * 两个自定义属性表达，由 CSS 变量在关键帧里插值。
 */
.ws-fx-ripples path {
  animation-name: ws-fx-ripple;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-ripple {
  0% {
    opacity: 0;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * -1));
  }
  /* 淡入：波峰从远处「出现」 */
  22% {
    opacity: 1;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * -0.55));
  }
  /* 推进到近处 */
  78% {
    opacity: 1;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * 0.55));
  }
  /* 走出画面 → 淡出并回到起点 */
  100% {
    opacity: 0;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(var(--fx-travel));
  }
}

/* ================================================================
   第 2 层：岸线破碎泡沫 —— 涌浪周期
   ================================================================ */
/**
 * 【一个周期的物理剧本】
 *   0%   平静（尺寸小、几乎不可见）
 *   30%  涌浪逼近，开始沿外法线前推
 *   52%  **破碎爆发** —— 最亮、最大（白水翻上堤面）
 *   78%  回落（沿法线退回，尺寸缩小、亮度下降）
 *   100% 恢复平静
 *
 * 关键帧里只用 transform / opacity —— 不触发重排。
 */
.ws-fx-foam g {
  animation-name: ws-fx-surf;
  animation-timing-function: cubic-bezier(0.4, 0.05, 0.55, 1);
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-surf {
  0%,
  100% {
    opacity: 0.12;
    transform: translate(0, 0) scale(0.62);
  }
  /* 涌浪逼近：白水开始向堤面爬升 */
  30% {
    opacity: 0.42;
    transform: translate(calc(var(--fx-surge-x) * 0.35), calc(var(--fx-surge-y) * 0.35)) scale(0.86);
  }
  /* 破碎爆发：白水翻上堤面，最亮最大 */
  52% {
    opacity: 1;
    transform: translate(var(--fx-surge-x), var(--fx-surge-y)) scale(var(--fx-burst));
  }
  /* 回落：白水沿堤面回流 */
  78% {
    opacity: 0.34;
    transform: translate(calc(var(--fx-surge-x) * 0.2), calc(var(--fx-surge-y) * 0.2)) scale(0.72);
  }
}

/* ================================================================
   第 6 层：尾迹泡沫白点 —— 沿尾迹漂移
   ================================================================ */
/**
 * 白点沿尾迹方向**持续向后漂移**，同时淡出 ——
 * 于是尾迹读起来是「正在消散的水面痕迹」，
 * 而不是「画在地图上的线条」。
 */
.ws-fx-wake-foam circle {
  animation-name: ws-fx-drift;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-drift {
  0% {
    opacity: 0;
    transform: translate(0, 0);
  }
  25% {
    opacity: 0.85;
  }
  100% {
    opacity: 0;
    transform: translate(var(--fx-drift-x), var(--fx-drift-y));
  }
}

/* ================================================================
   航标：灯桩规律闪烁
   ================================================================ */
.ws-fx-beacon-glow {
  animation-name: ws-fx-beacon;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}
@keyframes ws-fx-beacon {
  0%,
  100% {
    opacity: 0.12;
  }
  50% {
    opacity: 0.9;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ws-fx-ripples path,
  .ws-fx-foam g,
  .ws-fx-wake-foam circle,
  .ws-fx-beacon-glow {
    animation: none !important;
  }
}
</style>