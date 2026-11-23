/**
 * 验证「沿航迹轴压到水域」的落位是否连续。
 *
 * 对比三种算法在 T002 全程各采样点上的相邻显示位移：
 *   ① 单点 clamp（旧）          —— 独立求解，挑余量最大的格子
 *   ② 沿航迹轴 clamp（新）      —— 沿航向轴前后找最近水域
 *   ③ 通用 fallback            —— 沿轴找不到时的兜底
 *
 * 运行：node tools/verify-t002-axis.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 't002ax-'))

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

const CLEARANCE_MIN = 3

function oldWay(e, n) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) {
    return [e, n]
  }
  return hw.clampToOpenWater([e, n])
}

function dirsFor(pts) {
  const n = pts.length
  const W = 2
  const dirs = []
  for (let i = 0; i < n; i++) {
    const lo = Math.max(0, i - W)
    const hi = Math.min(n - 1, i + W)
    let de = pts[hi][0] - pts[lo][0]
    let dn = pts[hi][1] - pts[lo][1]
    if (Math.abs(de) < 1e-6 && Math.abs(dn) < 1e-6) {
      de = 1
      dn = 0
    }
    const L = Math.hypot(de, dn) || 1
    dirs.push([de / L, dn / L])
  }
  return dirs
}

function newWay(e, n, dir) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) {
    return [e, n]
  }
  return hw.clampToOpenWaterAlong([e, n], dir)
}

function pxDist(a, b) {
  const [ax, ay] = enuToImage(a[0], a[1])
  const [bx, by] = enuToImage(b[0], b[1])
  return Math.hypot(bx - ax, by - ay)
}

let globalOld = 0
let globalNew = 0

for (const tr of tracks.filter(t => t.global_track_id.startsWith('T'))) {
  const ss = tr.samples.filter(s => Number.isFinite(s.east_m) && Number.isFinite(s.north_m))
  if (ss.length < 3) continue
  const pts = ss.map(s => [s.east_m, s.north_m])
  const dirs = dirsFor(pts)

  const oldPts = ss.map(s => oldWay(s.east_m, s.north_m))
  const newPts = ss.map((s, i) => newWay(s.east_m, s.north_m, dirs[i]))

  let oldMax = 0
  let newMax = 0
  let oldAt = ''
  let newAt = ''
  for (let i = 1; i < ss.length; i++) {
    const dOld = pxDist(oldPts[i - 1], oldPts[i])
    const dNew = pxDist(newPts[i - 1], newPts[i])
    if (dOld > oldMax) {
      oldMax = dOld
      oldAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
    if (dNew > newMax) {
      newMax = dNew
      newAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
  }
  globalOld = Math.max(globalOld, oldMax)
  globalNew = Math.max(globalNew, newMax)
  console.log(
    `${tr.global_track_id}: 最大相邻显示位移  旧 ${oldMax.toFixed(0).padStart(4)}px (${oldAt})  →  ` +
      `新 ${newMax.toFixed(0).padStart(4)}px (${newAt})`,
  )
}

console.log(`\n全局最大跳变：旧 ${globalOld.toFixed(0)}px → 新 ${globalNew.toFixed(0)}px`)

/* T002 末段细节 */
const t2 = tracks.find(x => x.global_track_id === 'T002')
const ss2 = t2.samples
const pts2 = ss2.map(s => [s.east_m, s.north_m])
const dirs2 = dirsFor(pts2)
console.log('\nT002 末段（旧 → 新）：')
for (let i = Math.max(0, ss2.length - 7); i < ss2.length; i++) {
  const o = oldWay(ss2[i].east_m, ss2[i].north_m)
  const nw = newWay(ss2[i].east_m, ss2[i].north_m, dirs2[i])
  console.log(
    `  t=${String(ss2[i].time_ms).padStart(6)}  实测(${ss2[i].east_m.toFixed(0).padStart(6)}, ${ss2[i].north_m.toFixed(0)})` +
      `  旧(${o[0].toFixed(0).padStart(6)}, ${o[1].toFixed(0)})  新(${nw[0].toFixed(0).padStart(6)}, ${nw[1].toFixed(0)})`,
  )
}
