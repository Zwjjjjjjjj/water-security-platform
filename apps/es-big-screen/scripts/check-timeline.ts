/**
 * 全时间轴回归校验（开发期脚本）。
 *
 * 逐个回放时刻检查渲染出的航迹折线，
 * 确认任何时刻都不会出现压陆 / 穿堤。
 *
 * 用法：npx tsx scripts/check-timeline.ts
 */
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { makeNavigable, isBlocked, isMoored } from '../src/water-security/integration/navigation.ts'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const data = JSON.parse(
  fs.readFileSync(path.join(__dirname, '../public/water-security/data/tracks.json'), 'utf8'),
)

/** 线段是否穿越障碍 */
function segmentBlocked(a: [number, number], b: [number, number]): boolean {
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

const DURATION = data.meta.duration_ms as number
const STEP = 5000
let totalSeg = 0
let totalBad = 0
const badSamples: string[] = []

for (let time = 0; time <= DURATION; time += STEP) {
  for (const item of data.items) {
    const samples = item.samples.filter((p: any) => p.time_ms <= time)
    if (samples.length < 2) continue

    // 融合航迹
    const fusedRaw = samples.map((p: any) => [p.east_m, p.north_m] as [number, number])
    const moored = isMoored(fusedRaw)
    if (!moored) {
      const fused = makeNavigable(fusedRaw)
      for (let i = 0; i < fused.length - 1; i++) {
        totalSeg++
        if (segmentBlocked(fused[i], fused[i + 1])) {
          totalBad++
          if (badSamples.length < 10) {
            badSamples.push(
              `t=${time}ms ${item.global_track_id}-fused seg${i} ` +
                `[${fused[i].map((v) => v.toFixed(0))}]→[${fused[i + 1].map((v) => v.toFixed(0))}]`,
            )
          }
        }
      }
    }

    // 源点迹
    for (const st of item.source_tracks ?? []) {
      const srcRaw = st.samples
        .filter((p: any) => p.time_ms <= time)
        .map((p: any) => [p.east_m, p.north_m] as [number, number])
      if (srcRaw.length < 2) continue
      if (isMoored(srcRaw)) continue
      const src = makeNavigable(srcRaw)
      for (let i = 0; i < src.length - 1; i++) {
        totalSeg++
        if (segmentBlocked(src[i], src[i + 1])) {
          totalBad++
          if (badSamples.length < 10) {
            badSamples.push(
              `t=${time}ms ${item.global_track_id}-${st.source_id} seg${i}`,
            )
          }
        }
      }
    }
  }
}

console.log(`回放时长 ${DURATION}ms，步进 ${STEP}ms`)
console.log(`检查线段总数：${totalSeg}`)
console.log(`穿模线段：${totalBad}`)
if (badSamples.length) {
  console.log('\n穿模样例：')
  badSamples.forEach((s) => console.log('  ' + s))
}
console.log(totalBad === 0 ? '\n✓ 全时间轴无穿模' : '\n✗ 存在穿模')
process.exit(totalBad === 0 ? 0 : 1)
