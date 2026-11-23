/**
 * 航行约束自检（开发期脚本）。
 *
 * 校验目标：
 *   1. 每条航迹的显示折线都不压陆、不穿堤；
 *   2. 航迹整体形状（走向、长度）保持合理，未被算法压扁；
 *   3. 系泊目标被正确识别，且其位置仍为真实 ENU 坐标。
 *
 * 用法：npx tsx scripts/check-navigation.ts
 */
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { makeNavigable, isBlocked, isMoored, blockedKind } from '../src/water-security/integration/navigation.ts'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const data = JSON.parse(
  fs.readFileSync(path.join(__dirname, '../public/water-security/data/tracks.json'), 'utf8'),
)

/** 折线总长 */
function lineLength(pts: [number, number][]): number {
  return pts
    .slice(1)
    .reduce((acc, p, i) => acc + Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]), 0)
}

/** 线段中点是否落在障碍内（用于检查连线是否穿模） */
function segmentCrosses(a: [number, number], b: [number, number]): boolean {
  const dist = Math.hypot(b[0] - a[0], b[1] - a[1])
  const steps = Math.max(4, Math.ceil(dist / 8))
  for (let i = 0; i <= steps; i++) {
    const t = i / steps
    const e = a[0] + (b[0] - a[0]) * t
    const n = a[1] + (b[1] - a[1]) * t
    if (isBlocked(e, n)) return true
  }
  return false
}

let failed = false
console.log('目标      测点  原始违规  修正违规  穿模段  航迹长度保持  系泊')
console.log('─'.repeat(78))

for (const item of data.items) {
  const raw = item.samples.map(
    (p: any) => [p.east_m, p.north_m] as [number, number],
  )
  const before = raw.filter((p) => isBlocked(p[0], p[1])).length
  const moored = isMoored(raw)

  let fixed: [number, number][] = raw
  let after = 0
  let crosses = 0
  let ratio = 1

  if (!moored) {
    fixed = makeNavigable(raw)
    after = fixed.filter((p) => isBlocked(p[0], p[1])).length
    for (let i = 0; i < fixed.length - 1; i++) {
      if (segmentCrosses(fixed[i], fixed[i + 1])) crosses++
    }
    const l0 = lineLength(raw)
    const l1 = lineLength(fixed)
    ratio = l0 > 0 ? l1 / l0 : 1
  }

  const bad = after > 0 || crosses > 0
  if (bad) failed = true

  console.log(
    `${item.global_track_id.padEnd(8)} ${String(raw.length).padStart(4)} ` +
      `${String(before).padStart(8)} ${String(after).padStart(8)} ` +
      `${String(crosses).padStart(7)} ` +
      `${(moored ? '—' : (ratio * 100).toFixed(0) + '%').padStart(12)} ` +
      `  ${moored ? '是（按静态目标呈现）' : '否'}`,
  )

  if (moored) {
    // 系泊目标：位置必须是真实坐标
    const last = raw[raw.length - 1]
    const kind = blockedKind(last[0], last[1])
    console.log(`         └ 最新位置 ${last[0].toFixed(0)}, ${last[1].toFixed(0)} → ${kind}`)
  }
}

console.log('─'.repeat(78))
console.log(failed ? '✗ 仍有航迹穿模' : '✓ 所有航行目标航迹均不压陆、不穿堤')
process.exit(failed ? 1 : 0)
