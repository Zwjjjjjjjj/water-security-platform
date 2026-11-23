#!/usr/bin/env node
/**
 * 水下目标画面速度核验
 *
 * 【核验什么】
 * `SceneUnderwaterV2.progressOf()` 的推进速率是：
 *
 *     pxPerSec      = realSpeedMps × pxPerMeter
 *     ratioPerSec   = pxPerSec / polylinePx
 *     phase         = (animT / 1000) × ratioPerSec
 *
 * 也就是：**画面速度(px/s) = 真实速度(m/s) × 该航迹的 px/m**。
 *
 * 要证明「UUV 变快了」，光看 `tracks.json` 里的真实速度不够 ——
 * 因为 px/m 是按航迹各自标定的，
 * 航迹一变，px/m 也会变，最终画面速度未必同比提升。
 *
 * 所以这里完整复刻渲染端的算法（含深度偏移、容器缩放），
 * 直接算出**每个目标的画面速度(px/s)** 与**走完一条航迹的耗时**。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const DATA = path.join(ROOT, 'public', 'water-security', 'data')

/* ------------------------------------------------------------------ *
 * 渲染端常量（必须与 SceneUnderwaterV2.vue / oblique-fit.ts 一致）
 * ------------------------------------------------------------------ */
const CHART_W = 1920
const CHART_H = 870

/** 深度 → 屏幕纵向偏移（米 → 像素） */
const DEPTH_OFFSET_SCALE = 12
/** 深度偏移的上限（像素） */
const DEPTH_OFFSET_MAX = 26

/** 底图归一化坐标 → 容器像素 */
function normalizedToContainer(u, v, fit) {
  const x = (u - fit.u0) * fit.plotW + fit.gridLeft
  const y = (v - fit.v0) * fit.plotH + fit.gridTop
  return [x, y]
}

/** 与 oblique-fit.ts 相同的 fit 计算（斜视模式的默认视口） */
function computeObliqueFit({ xMin, xMax, yMin, yMax, gridLeft, gridTop, plotW, plotH }) {
  const spanE = xMax - xMin
  const spanN = yMax - yMin
  const scale = Math.min(plotW / spanE, plotH / spanN)
  return {
    u0: 0.5,
    v0: 0.5,
    plotW,
    plotH,
    gridLeft,
    gridTop,
    scale,
    xMin,
    yMin,
  }
}

/* 与 scenes.config.v2.ts 的 enuToImage 一致（仿射） */
const AFFINE = {
  a: 0.4267,
  b: 0.1043,
  c: 0.1156,
  d: -0.4364,
  e: 748.9,
  f: 555.1,
}

function enuToImage(east, north) {
  return [
    AFFINE.a * east + AFFINE.b * north + AFFINE.e,
    AFFINE.c * east + AFFINE.d * north + AFFINE.f,
  ]
}

/** ENU → 归一化 view_u / view_v */
function enuToNormalized(east, north) {
  const [px, py] = enuToImage(east, north)
  return [px / 1920, py / 870]
}

/* ------------------------------------------------------------------ *
 * 读取数据
 * ------------------------------------------------------------------ */
const tracks = JSON.parse(fs.readFileSync(path.join(DATA, 'tracks.json'), 'utf8'))
const targets = JSON.parse(fs.readFileSync(path.join(DATA, 'targets.json'), 'utf8'))

const gridLeft = 0
const gridTop = 0
const plotW = CHART_W - gridLeft
const plotH = CHART_H - gridTop
const fit = computeObliqueFit({
  xMin: -2600,
  xMax: 1000,
  yMin: 3000,
  yMax: 5000,
  gridLeft,
  gridTop,
  plotW,
  plotH,
})

const UNDER = tracks.items.filter((t) => String(t.global_track_id).startsWith('U'))

console.log('='.repeat(78))
console.log('水下目标画面速度核验（复刻 SceneUnderwaterV2.progressOf 算法）')
console.log('='.repeat(78))

const rows = []

for (const t of UNDER) {
  const samples = t.samples.map((s) => {
    const [u, v] = enuToNormalized(s.east_m, s.north_m)
    return {
      u,
      v,
      d: s.depth_m,
      east: s.east_m,
      north: s.north_m,
      time_ms: s.time_ms,
    }
  })

  /* 折线点（叠加深度偏移） */
  const pts = samples.map((s) => {
    const [x, y] = normalizedToContainer(s.u, s.v, fit)
    const depthOffset = Math.min(DEPTH_OFFSET_MAX, s.d * DEPTH_OFFSET_SCALE * 0.06)
    return { x, y: y + depthOffset, d: s.d }
  })

  /* 真实速度标定 */
  let meters = 0
  for (let i = 1; i < samples.length; i++) {
    meters += Math.hypot(
      samples[i].east - samples[i - 1].east,
      samples[i].north - samples[i - 1].north,
    )
  }
  const spanS = (samples[samples.length - 1].time_ms - samples[0].time_ms) / 1000
  const realSpeedMps = spanS > 0 ? meters / spanS : 0

  /* 像素长度 */
  let polylinePx = 0
  for (let i = 1; i < pts.length; i++) {
    polylinePx += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
  }
  const pxPerMeter = meters > 0.5 ? polylinePx / meters : 0

  /* progressOf 的推进速率 */
  const pxPerSec = realSpeedMps * pxPerMeter
  const ratioPerSec = polylinePx > 0 ? pxPerSec / polylinePx : 0

  /* 走完一条航迹（0→1）所需时间 = 1 / ratioPerSec */
  const traversalSec = ratioPerSec > 0 ? 1 / ratioPerSec : Infinity

  const meta = targets.items.find((x) => x.global_track_id === t.global_track_id)

  rows.push({
    id: t.global_track_id,
    type: meta?.target_type ?? '',
    meters,
    spanS,
    realSpeedMps,
    polylinePx,
    pxPerMeter,
    pxPerSec,
    traversalSec,
  })
}

console.log(
  ['目标', '类型', '航程m', '时长s', '真实m/s', '像素长px', 'px/m', '画面px/s', '单程耗时s']
    .map((h, i) => h.padEnd([10, 18, 9, 9, 10, 11, 9, 12, 11][i]))
    .join(''),
)
console.log('-'.repeat(78))

for (const r of rows) {
  console.log(
    [
      r.id,
      r.type,
      r.meters.toFixed(1),
      r.spanS.toFixed(1),
      r.realSpeedMps.toFixed(2),
      r.polylinePx.toFixed(0),
      r.pxPerMeter.toFixed(3),
      r.pxPerSec.toFixed(2),
      r.traversalSec.toFixed(1),
    ]
      .map((v, i) => String(v).padEnd([10, 18, 9, 9, 10, 11, 9, 12, 11][i]))
      .join(''),
  )
}

console.log('-'.repeat(78))

/* ------------------------------------------------------------------ *
 * 断言
 * ------------------------------------------------------------------ */
const uuv = rows.find((r) => r.id === 'U002')
const diver = rows.find((r) => r.id === 'U001')
const midget = rows.find((r) => r.id === 'U003')

let ok = true
function check(cond, msg) {
  console.log(`${cond ? '  ✅' : '  ❌'} ${msg}`)
  if (!cond) ok = false
}

console.log('\n【UUV 加速核验】')
check(uuv.realSpeedMps >= 1.9, `U002 真实速度 ${uuv.realSpeedMps.toFixed(2)} m/s ≥ 1.9（UUV 真实巡航 1.5~2.5 m/s）`)
check(uuv.realSpeedMps > diver.realSpeedMps, `U002 (${uuv.realSpeedMps.toFixed(2)}) 快于蛙人 U001 (${diver.realSpeedMps.toFixed(2)})`)
check(uuv.realSpeedMps < midget.realSpeedMps, `U002 (${uuv.realSpeedMps.toFixed(2)}) 慢于微型潜航器 U003 (${midget.realSpeedMps.toFixed(2)}) — 速度阶梯保持`)
check(uuv.traversalSec < 240, `U002 单程耗时 ${uuv.traversalSec.toFixed(1)} s < 240 s（原先 302 s）`)
check(uuv.pxPerSec > diver.pxPerSec, `U002 画面速度 ${uuv.pxPerSec.toFixed(2)} px/s > 蛙人 ${diver.pxPerSec.toFixed(2)} px/s`)

console.log(`\n结果：${ok ? '全部通过 ✅' : '存在失败 ❌'}`)
process.exit(ok ? 0 : 1)
