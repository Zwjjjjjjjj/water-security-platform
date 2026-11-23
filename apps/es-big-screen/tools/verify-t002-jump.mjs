/**
 * 复现并诊断「T002 末段轨迹穿模瞬移」。
 *
 * 【要查清的问题】
 * T002 的 ENU 实测航迹是平滑单调的，但画面上船却在末段来回瞬移。
 * 怀疑是 `displayPosition()`（把落在陆域的实测点推到可航水域）
 * 对每个采样点单独求解，落点不连续 —— 于是点与点之间出现跳变。
 *
 * 【本脚本做什么】
 * 1. 读出 T002 全部采样点；
 * 2. 对每个点调用与运行时完全相同的 `displayPosition()`（斜视模式分支）；
 * 3. 计算相邻落点的像素距离，报告超过阈值的跳变。
 *
 * 运行：node tools/verify-t002-jump.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '..')

/* ---------------- 载入底图水域掩码相关模块 ---------------- */
/* harbor-water.ts 是 TypeScript，这里直接复用其算法：
 * 运行时用的是 `enuToMapPx` → 掩码查表 → `clampToOpenWater`。
 * 为了让本脚本与运行时**完全一致**，这里通过 esbuild 打包后导入。 */
const { build } = await import('esbuild')
const { writeFileSync, mkdtempSync } = await import('node:fs')
const { tmpdir } = await import('node:os')

const tmp = mkdtempSync(resolve(tmpdir(), 't002-'))
const outfile = resolve(tmp, 'harbor-water.mjs')
await build({
  entryPoints: [resolve(ROOT, 'src/water-security/integration/harbor-water.ts')],
  outfile,
  bundle: true,
  format: 'esm',
  platform: 'node',
  logLevel: 'silent',
})
const hw = await import(outfile)

/* ---------------- 载入数据 ---------------- */
const tracks = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/tracks.json'), 'utf8'),
).items
const targets = JSON.parse(
  readFileSync(resolve(ROOT, 'public/water-security/data/targets.json'), 'utf8'),
).items

/* ---------------- 复刻 displayPosition（斜视分支） ---------------- */
const CLEARANCE_MIN = 3
function displayPosition(eastM, northM) {
  if (hw.isNavigableEnu(eastM, northM) && hw.clearanceCells(eastM, northM) >= CLEARANCE_MIN) {
    return { east: eastM, north: northM, adjusted: false }
  }
  const [e, n] = hw.clampToOpenWater([eastM, northM])
  return { east: e, north: n, adjusted: true }
}

/* ---------------- ENU → 底图像素（用于算像素距离） ---------------- */
const { enuToImage } = await (async () => {
  const o2 = resolve(tmp, 'scenes.config.v2.mjs')
  await build({
    entryPoints: [resolve(ROOT, 'src/water-security/integration/scenes.config.v2.ts')],
    outfile: o2,
    bundle: true,
    format: 'esm',
    platform: 'node',
    logLevel: 'silent',
  })
  return import(o2)
})()

function pixelDistance(a, b) {
  const [ax, ay] = enuToImage(a.east, a.north)
  const [bx, by] = enuToImage(b.east, b.north)
  return Math.hypot(bx - ax, by - ay)
}

/* ---------------- 诊断 ---------------- */
const surface = targets.filter(t => t.domain !== 'underwater')
let totalJumps = 0

for (const t of surface) {
  const tr = tracks.find(x => x.global_track_id === t.global_track_id)
  if (!tr) continue
  const ss = tr.samples.filter(s => Number.isFinite(s.east_m) && Number.isFinite(s.north_m))

  const disp = ss.map(s => displayPosition(s.east_m, s.north_m))
  const jumps = []
  for (let i = 1; i < disp.length; i++) {
    const d = pixelDistance(disp[i - 1], disp[i])
    /* 正常相邻点位移应 < 60px；超过即认为是跳变 */
    if (d > 60) {
      jumps.push({
        i,
        t_prev: ss[i - 1].time_ms,
        t: ss[i].time_ms,
        px: Math.round(d),
        raw_m: Math.round(Math.hypot(ss[i].east_m - ss[i - 1].east_m, ss[i].north_m - ss[i - 1].north_m)),
        adj: disp[i].adjusted && disp[i - 1].adjusted,
      })
    }
  }
  totalJumps += jumps.length
  if (jumps.length) {
    console.log(`\n=== ${t.global_track_id} —— ${jumps.length} 处跳变 ===`)
    for (const j of jumps) {
      console.log(
        `  采样 #${j.i}  t=${j.t_prev}→${j.t}  实测位移 ${j.raw_m}m  显示位移 ${j.px}px  ` +
          `(两侧都被clamp: ${j.adj ? '是' : '否'})`,
      )
    }
  }
}

console.log(`\n总计跳变：${totalJumps} 处`)
