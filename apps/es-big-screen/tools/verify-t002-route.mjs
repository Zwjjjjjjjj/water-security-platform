/**
 * 诊断「船位沿航迹投影后仍会移动」的问题。
 *
 * 【要查清】
 * T002 在 t=280000~287000 的实测 ENU 完全相同（-725.15, 4666.93），
 * 但画面上的船却从 x=804 走到 x=614。
 *
 * 假设：投影到的航线路径（route）在这段时间里发生了变化。
 * 本脚本复刻 `navigableRouteFor()` 的逻辑：
 *   · 航迹来源 = full_track（整条实测轨迹）；
 *   · A* 寻路 = makeNavigableOblique；
 * 然后对每个时刻的实测点做投影，看结果是否稳定。
 *
 * 运行：node tools/verify-t002-route.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 't002r-'))

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

const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items
const t2 = tracks.find(t => t.global_track_id === 'T002')
const samples = t2.samples

/* 与运行时一致：整条轨迹做 A* */
const raw = samples.map(s => [s.east_m, s.north_m])
const route = nav.makeNavigableOblique(raw)
console.log(`航迹点 ${raw.length} 个 → A* 航线 ${route.length} 个点`)

/* 复刻 displayPosition */
const CLEARANCE_MIN = 3
function displayPosition(e, n) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) {
    return { east: e, north: n, adjusted: false }
  }
  const [ce, cn] = hw.clampToOpenWater([e, n])
  return { east: ce, north: cn, adjusted: true }
}

function projectOnRoute(e, n) {
  const direct = displayPosition(e, n)
  if (!direct.adjusted) return direct
  let best = null
  let bestD = Infinity
  for (let i = 0; i < route.length - 1; i++) {
    const [ax, ay] = route[i]
    const [bx, by] = route[i + 1]
    const vx = bx - ax
    const vy = by - ay
    const len2 = vx * vx + vy * vy
    let u = len2 > 0 ? ((e - ax) * vx + (n - ay) * vy) / len2 : 0
    u = Math.max(0, Math.min(1, u))
    const px = ax + vx * u
    const py = ay + vy * u
    const d = (px - e) ** 2 + (py - n) ** 2
    if (d < bestD) {
      bestD = d
      best = [px, py]
    }
  }
  return { east: best[0], north: best[1], adjusted: true, dist: Math.sqrt(bestD) }
}

/* 模拟各时刻的插值位置 */
function interp(time) {
  if (time <= samples[0].time_ms) return { ...samples[0] }
  if (time >= samples.at(-1).time_ms) return { ...samples.at(-1) }
  let lo = 0
  let hi = samples.length - 1
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (samples[mid].time_ms <= time) lo = mid
    else hi = mid
  }
  const a = samples[lo]
  const b = samples[hi]
  const k = (time - a.time_ms) / (b.time_ms - a.time_ms)
  return {
    east_m: a.east_m + (b.east_m - a.east_m) * k,
    north_m: a.north_m + (b.north_m - a.north_m) * k,
  }
}

console.log('\n时刻        实测east    实测north   显示east    显示north   投影距离m')
for (const t of [270000, 275000, 280000, 282000, 285000, 286000, 287000, 290000, 295000, 300000]) {
  const p = interp(t)
  const d = projectOnRoute(p.east_m, p.north_m)
  console.log(
    `${String(t).padStart(7)}  ${p.east_m.toFixed(2).padStart(10)}  ${p.north_m.toFixed(2).padStart(10)}` +
      `  ${d.east.toFixed(2).padStart(10)}  ${d.north.toFixed(2).padStart(10)}  ${(d.dist ?? 0).toFixed(1)}`,
  )
}

/* 看看航线在这段区域长什么样 */
console.log('\n航线点在 (-725, 4667) 附近的段:')
for (let i = 0; i < route.length - 1; i++) {
  const [ax, ay] = route[i]
  const [bx, by] = route[i + 1]
  const d = Math.min(
    Math.hypot(ax + 725.15, ay - 4666.93),
    Math.hypot(bx + 725.15, by - 4666.93),
  )
  if (d < 400) {
    console.log(
      `  #${i} (${ax.toFixed(1)}, ${ay.toFixed(1)}) → (${bx.toFixed(1)}, ${by.toFixed(1)})  最近 ${d.toFixed(0)}m`,
    )
  }
}
