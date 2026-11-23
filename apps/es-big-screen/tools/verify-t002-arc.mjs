/**
 * 诊断「航线弧长定位」是否真的生效。
 *
 * 检查 T002 的：
 *   · A* 航线（整条 full_track）
 *   · 每个采样点对应的航线弧长比例 atSample
 *   · 各时刻按弧长定位得到的显示位置
 *
 * 重点：t=280000~287000 这段实测位置完全相同，
 * 但若 atSample 在这段里跳变，说明「采样点 → 弧长」的映射本身就不连续。
 *
 * 运行：node tools/verify-t002-arc.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 't002arc-'))

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
const ss = t2.samples
const pts = ss.map(s => [s.east_m, s.north_m])

const route = nav.makeNavigableOblique(pts)
console.log(`A* 航线 ${route.length} 点`)

/* 累计弧长 */
const cum = [0]
for (let i = 1; i < route.length; i++) {
  cum.push(cum[i - 1] + Math.hypot(route[i][0] - route[i - 1][0], route[i][1] - route[i - 1][1]))
}
const total = cum[cum.length - 1] || 0
console.log(`航线总弧长 ${total.toFixed(0)}m`)

/* 每个采样点 → 最近的航线点 → 弧长比例 */
const atSample = pts.map(p => {
  let bi = 0
  let bd = Infinity
  route.forEach((r, i) => {
    const d = (r[0] - p[0]) ** 2 + (r[1] - p[1]) ** 2
    if (d < bd) {
      bd = d
      bi = i
    }
  })
  return { ratio: total > 0 ? cum[bi] / total : 0, idx: bi, dist: Math.sqrt(bd) }
})

console.log('\n采样点 → 航线弧长比例:')
for (let i = 0; i < ss.length; i++) {
  const a = atSample[i]
  console.log(
    `  #${String(i).padStart(2)} t=${String(ss[i].time_ms).padStart(6)} ` +
      `实测(${ss[i].east_m.toFixed(0).padStart(7)}, ${ss[i].north_m.toFixed(0).padStart(7)})  ` +
      `航线idx=${String(a.idx).padStart(3)}  弧长比例=${a.ratio.toFixed(4)}  最近距离=${a.dist.toFixed(0)}m`,
  )
}

/* 相邻采样点的弧长比例差 —— 这才是决定「船是否跳」的关键 */
console.log('\n相邻采样点的弧长比例差（差值大 = 船会跳）:')
for (let i = 1; i < ss.length; i++) {
  const d = Math.abs(atSample[i].ratio - atSample[i - 1].ratio)
  const flag = d > 0.08 ? '  ←←← 跳变' : ''
  console.log(
    `  #${i - 1}→#${i}  t=${ss[i - 1].time_ms}→${ss[i].time_ms}  Δ比例=${d.toFixed(4)}${flag}`,
  )
}
