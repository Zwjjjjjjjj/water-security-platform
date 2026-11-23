/**
 * 验证「沿航迹方向连续的落位」是否消除了 T002 的瞬移。
 *
 * 【对照】
 * 旧逻辑：`clampToOpenWater()` 单点独立求解 → 相邻点落到不同水域 → 瞬移。
 * 新逻辑：`clampToOpenWaterAlong()` 沿航迹切线压到水域 → 沿迹位置不变。
 *
 * 本脚本对 T002 全部采样点分别用两种算法求解，
 * 比较「相邻采样点的显示位移」与「实测位移」的量级关系。
 *
 * 运行：node tools/verify-t002-along.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { build } from 'esbuild'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')
const tmp = mkdtempSync(resolve(tmpdir(), 't002a-'))

const out = resolve(tmp, 'hw.mjs')
await build({
  entryPoints: [resolve(ROOT, 'src/water-security/integration/harbor-water.ts')],
  outfile: out,
  bundle: true,
  format: 'esm',
  platform: 'node',
  logLevel: 'silent',
})
const hw = await import(out)

const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items
const targets = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/targets.json'), 'utf8'),
).items

const CLEARANCE_MIN = 3

/* ---------- 旧逻辑：单点独立 clamp ---------- */
function oldWay(e, n) {
  if (hw.isNavigableEnu(e, n) && hw.clearanceCells(e, n) >= CLEARANCE_MIN) {
    return [e, n]
  }
  return hw.clampToOpenWater([e, n])
}

/* ---------- 新逻辑：沿航迹方向连续 ---------- */
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

/* ---------- ENU → 像素（用于比较屏幕上的跳变幅度） ---------- */
const outCfg = resolve(tmp, 'cfg.mjs')
await build({
  entryPoints: [resolve(ROOT, 'src/water-security/integration/scenes.config.v2.ts')],
  outfile: outCfg,
  bundle: true,
  format: 'esm',
  platform: 'node',
  logLevel: 'silent',
})
const { enuToImage } = await import(outCfg)

function pxDist(a, b) {
  const [ax, ay] = enuToImage(a[0], a[1])
  const [bx, by] = enuToImage(b[0], b[1])
  return Math.hypot(bx - ax, by - ay)
}

/* ---------- 逐目标比较 ---------- */
let worstOld = 0
let worstNew = 0

for (const t of targets.filter(x => x.domain !== 'underwater')) {
  const tr = tracks.find(x => x.global_track_id === t.global_track_id)
  if (!tr) continue
  const ss = tr.samples.filter(s => Number.isFinite(s.east_m) && Number.isFinite(s.north_m))
  if (ss.length < 3) continue

  const pts = ss.map(s => [s.east_m, s.north_m])
  const dirs = dirsFor(pts)

  const oldPts = ss.map(s => oldWay(s.east_m, s.north_m))
  const newPts = ss.map((s, i) => newWay(s.east_m, s.north_m, dirs[i]))

  let oldMax = 0
  let newMax = 0
  let oldMaxAt = ''
  let newMaxAt = ''
  for (let i = 1; i < ss.length; i++) {
    const dOld = pxDist(oldPts[i - 1], oldPts[i])
    const dNew = pxDist(newPts[i - 1], newPts[i])
    if (dOld > oldMax) {
      oldMax = dOld
      oldMaxAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
    if (dNew > newMax) {
      newMax = dNew
      newMaxAt = `${ss[i - 1].time_ms}→${ss[i].time_ms}`
    }
  }
  worstOld = Math.max(worstOld, oldMax)
  worstNew = Math.max(worstNew, newMax)
  console.log(
    `${t.global_track_id}: 最大相邻显示位移  旧 ${oldMax.toFixed(0).padStart(4)}px (${oldMaxAt})  →  ` +
      `新 ${newMax.toFixed(0).padStart(4)}px (${newMaxAt})`,
  )
}

console.log(`\n全局最大跳变：旧 ${worstOld.toFixed(0)}px → 新 ${worstNew.toFixed(0)}px`)

/* ---------- T002 末段细节 ---------- */
const t2 = tracks.find(x => x.global_track_id === 'T002')
const ss2 = t2.samples
const pts2 = ss2.map(s => [s.east_m, s.north_m])
const dirs2 = dirsFor(pts2)
console.log('\nT002 末段（旧 → 新）：')
for (let i = Math.max(0, ss2.length - 6); i < ss2.length; i++) {
  const o = oldWay(ss2[i].east_m, ss2[i].north_m)
  const nw = newWay(ss2[i].east_m, ss2[i].north_m, dirs2[i])
  console.log(
    `  t=${String(ss2[i].time_ms).padStart(6)}  实测(${ss2[i].east_m.toFixed(0)}, ${ss2[i].north_m.toFixed(0)})` +
      `  旧(${o[0].toFixed(0)}, ${o[1].toFixed(0)})  新(${nw[0].toFixed(0)}, ${nw[1].toFixed(0)})`,
  )
}
