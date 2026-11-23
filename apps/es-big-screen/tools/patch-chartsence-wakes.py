#!/usr/bin/env python3
"""更新 ChartSceneV2.vue：
1. shipWakes 改用新的 WakeParams（含时刻/船长/船宽）。
2. 船体贴图的航向改为沿航迹平滑插值，避免跳变。
"""
path = 'src/water-security/integration/ChartSceneV2.vue'
src = open(path, encoding='utf-8').read()

# ---------- 1. shipWakes ----------
old_wakes = """const shipWakes = computed(() => {
  void assetsReady.value
  const out: WakeSegment[] = []
  const targets = (props.snapshot as any)?.targets || []
  for (const t of targets) {
    const speed = t.speed_mps ?? 0
    const hist: [number, number][] = (t.history || [])
      .filter((p: any) => p.east_m != null && p.north_m != null)
      .map((p: any) => [p.east_m, p.north_m] as [number, number])
    if (hist.length < 2) continue
    out.push(...buildWake(hist, enuToImage, speed))
  }
  return out
})"""

new_wakes = """const shipWakes = computed(() => {
  void assetsReady.value
  const out: WakeSegment[] = []
  const targets = (props.snapshot as any)?.targets || []
  const nowMs = (props.snapshot as any)?.time_ms ?? 0
  for (const t of targets) {
    /* 水下目标不画水面尾迹 */
    if ((t.domain || 'surface') !== 'surface') continue
    const speed = t.speed_mps ?? 0
    const hist: WakeSample[] = (t.history || [])
      .filter((p: any) => p.east_m != null && p.north_m != null && p.time_ms != null)
      .map((p: any) => ({ e: p.east_m, n: p.north_m, t: p.time_ms }))
    if (hist.length < 2) continue
    const asset = shipSpriteOf(TARGET_SHIP_ASSET[t.global_track_id])
    const lengthM = t.length_m ?? SHIP_LEN_M[t.ship_type] ?? asset?.length_m ?? 60
    const beamM = t.beam_m ?? asset?.beam_m ?? 12
    out.push(
      ...buildWake(hist, enuToImage, {
        speedMps: speed,
        lengthM,
        beamM,
        nowMs,
      }),
    )
  }
  return out
})"""

assert old_wakes in src, "shipWakes not found"
src = src.replace(old_wakes, new_wakes, 1)

# ---------- 2. import ----------
old_imp = "import { buildWake, type WakeSegment } from './water-effects'"
new_imp = "import { buildWake, type WakeSegment, type WakeSample } from './water-effects'"
assert old_imp in src
src = src.replace(old_imp, new_imp, 1)

open(path, 'w', encoding='utf-8').write(src)
print("shipWakes updated")
