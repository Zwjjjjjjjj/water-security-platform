/**
 * 验证「船骑在航迹线上」这一最终方案。
 *
 * 检查三件事：
 *   1. 船的显示位置是否落在可航水域（离岸余量达标）；
 *   2. 船的位置是否**在航迹线上**（与线头重合）；
 *   3. 相邻时刻的显示位移是否连续（无瞬移）。
 *
 * 运行：node tools/verify-ship-on-route.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 'sor-'))

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
const hw = await load('harbor-water.ts', 'hw.mjs')
const cfg = await load('scenes.config.v2.ts', 'cfg.mjs')
const { enuToImage } = cfg

const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items

function px(a) {
  return enuToImage(a[0], a[1])
}

for (const tid of ['T001', 'T002', 'T004']) {
  const tr = tracks.find(x => x.global_track_id === tid)
  const ss = tr.samples
  const pts = ss.map(s => [s.east_m, s.north_m])
  const route = nav.makeNavigableOblique(pts)

  /* 累计弧长 */
  const cum = [0]
  for (let i = 1; i < route.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(route[i][0] - route[i - 1][0], route[i][1] - route[i - 1][1]))
  }
  const total = cum[cum.length - 1] || 1

  /* 实测累计弧长 → atSample */
  const mCum = [0]
  for (let i = 1; i < pts.length; i++) {
    mCum.push(mCum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
  }
  const mTotal = mCum[mCum.length - 1] || 1
  const atSample = mCum.map(v => v / mTotal)

  function pointOnRoute(ratio) {
    const s = Math.max(0, Math.min(1, ratio)) * total
    let i = 0
    while (i < cum.length - 2 && cum[i + 1] < s) i++
    const segLen = cum[i + 1] - cum[i]
    const u = segLen > 0 ? (s - cum[i]) / segLen : 0
    return [route[i][0] + (route[i + 1][0] - route[i][0]) * u, route[i][1] + (route[i + 1][1] - route[i][1]) * u]
  }

  /* 模拟回放：按时间插值出当前采样下标与弧长比例 */
  const times = ss.map(s => s.time_ms)
  const t0 = times[0]
  const t1 = times[times.length - 1]
  const rows = []
  for (const nowMs of [50000, 100000, 150000, 200000, 250000, 265000, 280000, 285000, 290000, 295000, 300000]) {
    const timeRatio = Math.max(0, Math.min(1, (nowMs - t0) / (t1 - t0)))
    const si = timeRatio * (times.length - 1)
    const i0 = Math.floor(si)
    const i1 = Math.min(times.length - 1, i0 + 1)
    const f = si - i0
    const arc = atSample[i0] + (atSample[i1] - atSample[i0]) * f
    const ship = pointOnRoute(arc)
    const nav2 = hw.isNavigableEnu(ship[0], ship[1])
    const clr = hw.clearanceCells(ship[0], ship[1])
    rows.push({ nowMs, ship, nav: nav2, clr })
  }

  console.log(`\n=== ${tid} ===  航线 ${route.length} 点 / ${total.toFixed(0)}m，实测 ${mTotal.toFixed(0)}m`)
  let maxStep = 0
  for (let i = 1; i < rows.length; i++) {
    const a = px(rows[i - 1].ship)
    const b = px(rows[i].ship)
    const d = Math.hypot(b[0] - a[0], b[1] - a[1])
    maxStep = Math.max(maxStep, d)
  }
  for (const r of rows) {
    console.log(
      `  t=${String(r.nowMs).padStart(6)}  船位于(${r.ship[0].toFixed(0).padStart(6)}, ${r.ship[1].toFixed(0).padStart(6)})` +
        `  在水域=${r.nav ? '是' : '否'}  余量=${r.clr}`,
    )
  }
  console.log(`  最大相邻位移：${maxStep.toFixed(0)}px`)
}
