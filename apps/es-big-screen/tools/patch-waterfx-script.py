#!/usr/bin/env python3
"""重写 SceneWaterFxV2.vue：渲染符合物理规律的海面动效。

核心变化：
1. 尾迹按 kind 分层渲染（arm / wash / crest），并叠加漂移泡沫白点。
2. 泡沫按「涌浪周期」采样：推进 → 爆发 → 回落。
3. 波纹按传播方向持续平移。
4. 全部动画用 CSS transform/opacity，交给合成器，不触发重排。
"""
import re

path = 'src/water-security/integration/SceneWaterFxV2.vue'
src = open(path, encoding='utf-8').read()

# ---------- 1. script 部分整体替换 ----------
script_start = src.index('<script setup lang="ts">')
script_end = src.index('</script>') + len('</script>')

new_script = '''<script setup lang="ts">
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
</script>'''

src = src[:script_start] + new_script + src[script_end:]
open(path, 'w', encoding='utf-8').write(src)
print("script replaced")
