/**
 * 校验水下目标的运动速度是否锚定到真实物理速度。
 *
 * 用法：npx tsx tools/verify-diver-speed.mjs
 *
 * 【校验内容】
 *  1. 比例速率 = 真实速度 ÷ 航迹米长（与像素缩放无关）；
 *  2. 走完一条航迹的耗时 ≈ 该航迹的真实时间跨度；
 *  3. 折线插值（pointAt）沿**弧长**推进，单位时间走过的弧长
 *     = 真实速度 × 该航迹的局部比例。
 */
import { readFileSync } from 'node:fs'

const tracks = JSON.parse(readFileSync('public/water-security/data/tracks.json', 'utf-8'))

/* ===== 与 SceneUnderwaterV2.vue 一致的算法 ===== */

/** 收集样本（与 collectSamples 等价） */
function collectSamples(t) {
  const raw = t.history ?? t.samples ?? []
  return raw
    .filter((p) => p && p.view_u != null && p.view_v != null)
    .map((p) => ({
      time_ms: p.time_ms ?? 0,
      u: p.view_u,
      v: p.view_v,
      east: Number.isFinite(p.east_m) ? p.east_m : 0,
      north: Number.isFinite(p.north_m) ? p.north_m : 0,
    }))
}

/** 标定：真实速度与局部比例 */
function calibrate(samples, pts) {
  let meters = 0
  for (let i = 1; i < samples.length; i++) {
    meters += Math.hypot(
      samples[i].east - samples[i - 1].east,
      samples[i].north - samples[i - 1].north,
    )
  }
  const spanS = (samples[samples.length - 1].time_ms - samples[0].time_ms) / 1000
  const realSpeedMps = spanS > 0 ? meters / spanS : 0
  let polylinePx = 0
  for (let i = 1; i < pts.length; i++) {
    polylinePx += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
  }
  const pxPerMeter = meters > 0.5 ? polylinePx / meters : 0
  return { meters, spanS, realSpeedMps, polylinePx, pxPerMeter }
}

/** 折线上的点（按弧长比例） */
function pointAt(pts, ratio) {
  const cum = [0]
  for (let i = 1; i < pts.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y))
  }
  const total = cum[cum.length - 1] || 1
  const s = Math.max(0, Math.min(1, ratio)) * total
  let i = 0
  while (i < cum.length - 2 && cum[i + 1] < s) i++
  const segLen = cum[i + 1] - cum[i]
  const t = segLen > 0 ? (s - cum[i]) / segLen : 0
  return {
    x: pts[i].x + (pts[i + 1].x - pts[i].x) * t,
    y: pts[i].y + (pts[i + 1].y - pts[i].y) * t,
    arc: s,
  }
}

let failures = 0
const check = (name, cond, detail) => {
  if (!cond) failures++
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

console.log('目标    真实均速   米长     跨度    比例速率    走完一趟   校验')
console.log('-'.repeat(72))

for (const t of tracks.items) {
  if (!t.global_track_id.startsWith('U')) continue
  const samples = collectSamples(t)
  if (samples.length < 2) continue

  /* 模拟任意像素缩放（归一化坐标 → 容器像素） */
  const fitR = 1.0
  const pts = samples.map((s) => ({ x: s.u * 1920 * fitR, y: s.v * 700 * fitR }))
  const cal = calibrate(samples, pts)

  const ratioPerSec = cal.realSpeedMps / cal.meters
  const fullSec = 1 / ratioPerSec

  /* 用弧长验证：1 秒后走了多少像素 */
  const p0 = pointAt(pts, 0.3)
  const p1 = pointAt(pts, 0.3 + ratioPerSec)
  const arcPerSec = p1.arc - p0.arc

  /* 期望：1 秒走过的像素 = 真实速度(m/s) × 局部比例(px/m) */
  const expectedPx = cal.realSpeedMps * cal.pxPerMeter
  const ok = Math.abs(arcPerSec - expectedPx) < 1e-6
  const okDur = Math.abs(fullSec - cal.spanS) / cal.spanS < 0.01

  console.log(
    `${t.global_track_id.padEnd(7)} ${cal.realSpeedMps.toFixed(2).padStart(6)} m/s ` +
    `${cal.meters.toFixed(0).padStart(6)} m ${cal.spanS.toFixed(0).padStart(6)} s ` +
    `${ratioPerSec.toFixed(6).padStart(10)} /s ${fullSec.toFixed(1).padStart(8)} s`,
  )

  check(`  ${t.global_track_id} 弧长推进 = 真实速度×局部比例`, ok,
    `实测 ${arcPerSec.toFixed(4)} px/s vs 期望 ${expectedPx.toFixed(4)} px/s`)
  check(`  ${t.global_track_id} 走完一趟 ≈ 真实时间跨度`, okDur,
    `${fullSec.toFixed(1)} s vs 真实 ${cal.spanS.toFixed(1)} s`)
}

/* ===== 缩放无关性 ===== */
console.log('')
{
  const t = tracks.items.find((x) => x.global_track_id === 'U001')
  const samples = collectSamples(t)
  const results = []
  for (const fitR of [0.5, 1.0, 2.0]) {
    const pts = samples.map((s) => ({ x: s.u * 1920 * fitR, y: s.v * 700 * fitR }))
    const cal = calibrate(samples, pts)
    results.push(cal.realSpeedMps / cal.meters)
  }
  const same = results.every((r) => Math.abs(r - results[0]) < 1e-12)
  check('比例速率与像素缩放无关', same, results.map((r) => r.toFixed(8)).join(' / '))
}

/* ===== 与旧版的对比 ===== */
console.log('')
{
  const t = tracks.items.find((x) => x.global_track_id === 'U001')
  const samples = collectSamples(t)
  const pts = samples.map((s) => ({ x: s.u * 1920, y: s.v * 700 }))
  const cal = calibrate(samples, pts)
  /* 旧版：speedFactor = max(0.35, min(1.6, avg/3))，相位 = animT/9000 × factor */
  const avgSpeed = samples.reduce((a, s) => a + (s.speed || 0), 0) / samples.length
  const oldFactor = Math.max(0.35, Math.min(1.6, avgSpeed / 3))
  const oldFullSec = 9000 / oldFactor / 1000
  const oldRatioPerSec = 1 / oldFullSec
  const oldPxPerSec = oldRatioPerSec * cal.polylinePx
  const newPxPerSec = (cal.realSpeedMps / cal.meters) * cal.polylinePx
  check(
    '新速度明显慢于旧版（这是本次修改的目的）',
    newPxPerSec < oldPxPerSec * 0.5,
    `旧 ${oldPxPerSec.toFixed(2)} px/s → 新 ${newPxPerSec.toFixed(2)} px/s（慢 ${(oldPxPerSec / newPxPerSec).toFixed(1)} 倍）`,
  )
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`)
process.exit(failures === 0 ? 0 : 1)
