/**
 * 比较几种「实测采样点 → 航线位置」的映射方式。
 *
 * 问题：T002 的 A* 航线长 7563m，而实测航迹只有 2104m
 *      （因为实测航迹横穿厂区，A* 必须绕行到南边）。
 * 若按「等分航线」映射，船会远远落后在真实位置。
 *
 * 候选：
 *   ① 等分航线        —— 实测累计弧长比例 → 航线弧长比例
 *   ② 最近航线点      —— 实测点 → 航线上的最近点（可能退化/回退）
 *   ③ 最近点 + 单调化 —— 取最近点，但强制弧长单调不回退
 *
 * 指标：
 *   · 船是否在航线上（必然）
 *   · 船与实测点的距离（越接近越好）
 *   · 相邻采样点的显示位移（越小越好 = 不瞬移）
 *
 * 运行：node tools/verify-route-mapping.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 'rm-'))

async function load(entry, name) {
  const outfile = resolve(tmp, name)
  await build({
    entryPoints: [resolve(ROOT, 'src/water-security/integration', entry)],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'node',
    logLevel: 'silent',
  })
  return import(outfile)
}

const nav = await load('navigation.ts', 'nav.mjs')
const cfg = await load('scenes.config.v2.ts', 'cfg.mjs')
const { enuToImage } = cfg

const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items

function pxDist(a, b) {
  const [ax, ay] = enuToImage(a[0], a[1])
  const [bx, by] = enuToImage(b[0], b[1])
  return Math.hypot(bx - ax, by - ay)
}
function mDist(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1])
}

for (const tid of ['T001', 'T002', 'T004']) {
  const tr = tracks.find(x => x.global_track_id === tid)
  const ss = tr.samples
  const pts = ss.map(s => [s.east_m, s.north_m])
  const route = nav.makeNavigableOblique(pts)

  const cum = [0]
  for (let i = 1; i < route.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(route[i][0] - route[i - 1][0], route[i][1] - route[i - 1][1]))
  }
  const total = cum[cum.length - 1] || 1

  const mCum = [0]
  for (let i = 1; i < pts.length; i++) {
    mCum.push(mCum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
  }
  const mTotal = mCum[mCum.length - 1] || 1

  /* ① 等分航线 */
  const mapEqual = mCum.map(v => v / mTotal)

  /* ② 最近航线点 */
  const mapNearest = pts.map(p => {
    let bi = 0
    let bd = Infinity
    route.forEach((r, i) => {
      const d = (r[0] - p[0]) ** 2 + (r[1] - p[1]) ** 2
      if (d < bd) {
        bd = d
        bi = i
      }
    })
    return cum[bi] / total
  })

  /* ③ 最近点 + 单调化（不回退，且不超过下一采样点的弧长） */
  const mapMono = []
  for (let i = 0; i < mapNearest.length; i++) {
    let v = mapNearest[i]
    if (i > 0 && v < mapMono[i - 1]) v = mapMono[i - 1]
    mapMono.push(v)
  }

  function pointOnRoute(ratio) {
    const s = Math.max(0, Math.min(1, ratio)) * total
    let i = 0
    while (i < cum.length - 2 && cum[i + 1] < s) i++
    const segLen = cum[i + 1] - cum[i]
    const u = segLen > 0 ? (s - cum[i]) / segLen : 0
    return [route[i][0] + (route[i + 1][0] - route[i][0]) * u, route[i][1] + (route[i + 1][1] - route[i][1]) * u]
  }

  console.log(`\n=== ${tid} === 航线 ${total.toFixed(0)}m / 实测 ${mTotal.toFixed(0)}m`)
  for (const [name, map] of [
    ['①等分', mapEqual],
    ['②最近', mapNearest],
    ['③单调', mapMono],
  ]) {
    const positions = map.map(pointOnRoute)
    let maxStep = 0
    let avgDev = 0
    for (let i = 0; i < positions.length; i++) {
      avgDev += mDist(positions[i], pts[i])
      if (i > 0) maxStep = Math.max(maxStep, pxDist(positions[i - 1], positions[i]))
    }
    avgDev /= positions.length
    console.log(
      `  ${name}: 平均偏离实测 ${avgDev.toFixed(0).padStart(5)}m  最大相邻位移 ${maxStep.toFixed(0).padStart(5)}px`,
    )
  }

  /* ③单调的逐点明细 */
  console.log('  ③单调 逐点:')
  const positions = mapMono.map(pointOnRoute)
  for (let i = 0; i < ss.length; i++) {
    if (i < ss.length - 8 && i % 4 !== 0) continue
    console.log(
      `    #${String(i).padStart(2)} t=${String(ss[i].time_ms).padStart(6)} ` +
        `实测(${ss[i].east_m.toFixed(0).padStart(6)}, ${ss[i].north_m.toFixed(0).padStart(5)}) → ` +
        `航线(${positions[i][0].toFixed(0).padStart(6)}, ${positions[i][1].toFixed(0).padStart(5)})  ` +
        `偏离 ${mDist(positions[i], pts[i]).toFixed(0)}m`,
    )
  }
}
