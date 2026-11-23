/**
 * 对比两种「压到水域」算法的连续性。
 *
 *   ① clampToOpenWater         —— 挑「离岸余量最大」的格子（旧）
 *   ② clampToOpenWaterNearest  —— 挑「离原点最近」的达标格（新）
 *
 * 指标：相邻采样点的显示位移（像素）。
 * 瞬移的本质就是「相邻点落点相差很远」，所以这个指标直接对应用户观感。
 *
 * 运行：node tools/verify-clamp-nearest.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 'cn-'))

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

const hw = await load('harbor-water.ts', 'hw.mjs')
const cfg = await load('scenes.config.v2.ts', 'cfg.mjs')
const { enuToImage } = cfg

const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items
const targets = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/targets.json'), 'utf8'),
).items

const CLEARANCE_MIN = 3

function oldWay(e, n) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) return [e, n]
  return hw.clampToOpenWater([e, n])
}
function newWay(e, n) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) return [e, n]
  return hw.clampToOpenWaterNearest([e, n])
}

function pxDist(a, b) {
  const [ax, ay] = enuToImage(a[0], a[1])
  const [bx, by] = enuToImage(b[0], b[1])
  return Math.hypot(bx - ax, by - ay)
}

let gOld = 0
let gNew = 0

for (const t of targets.filter(x => x.domain !== 'underwater')) {
  const tr = tracks.find(x => x.global_track_id === t.global_track_id)
  if (!tr) continue
  const ss = tr.samples.filter(s => Number.isFinite(s.east_m) && Number.isFinite(s.north_m))
  if (ss.length < 3) continue

  const oP = ss.map(s => oldWay(s.east_m, s.north_m))
  const nP = ss.map(s => newWay(s.east_m, s.north_m))

  let oMax = 0
  let nMax = 0
  let oAt = ''
  let nAt = ''
  let oSum = 0
  let nSum = 0
  for (let i = 1; i < ss.length; i++) {
    const dO = pxDist(oP[i - 1], oP[i])
    const dN = pxDist(nP[i - 1], nP[i])
    oSum += dO
    nSum += dN
    if (dO > oMax) {
      oMax = dO
      oAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
    if (dN > nMax) {
      nMax = dN
      nAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
  }
  gOld = Math.max(gOld, oMax)
  gNew = Math.max(gNew, nMax)
  console.log(
    `${t.global_track_id}: 最大 ${oMax.toFixed(0).padStart(4)}px → ${nMax.toFixed(0).padStart(4)}px` +
      `  |  累计 ${oSum.toFixed(0).padStart(6)}px → ${nSum.toFixed(0).padStart(6)}px` +
      `  |  ${oAt} → ${nAt}`,
  )
}

console.log(`\n全局最大跳变：旧 ${gOld.toFixed(0)}px → 新 ${gNew.toFixed(0)}px`)

/* T002 末段细节 */
const t2 = tracks.find(x => x.global_track_id === 'T002')
console.log('\nT002 末段（旧 → 新）：')
for (const s of t2.samples.slice(-7)) {
  const o = oldWay(s.east_m, s.north_m)
  const nw = newWay(s.east_m, s.north_m)
  console.log(
    `  t=${String(s.time_ms).padStart(6)}  实测(${s.east_m.toFixed(0).padStart(6)}, ${s.north_m.toFixed(0)})` +
      `  旧(${o[0].toFixed(0).padStart(6)}, ${o[1].toFixed(0)})  新(${nw[0].toFixed(0).padStart(6)}, ${nw[1].toFixed(0)})`,
  )
}
