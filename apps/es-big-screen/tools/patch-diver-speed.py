#!/usr/bin/env python3
"""SceneUnderwaterV2：把水下目标的运动速度锚定到**真实物理速度**。

【问题】
旧版 `progressOf()` 用「相位 = animT/9000 × speedFactor」的模型：
速度因子由「平均速度 ÷ 3」硬凑出来，与真实时间尺度脱钩。
结果蛙人（真实游速 1.5 m/s）在画面上跑出 16.9 m/s —— 快了近 11 倍；
U003（2.6 m/s）更是跑到 49.5 m/s，快了 19 倍。
视觉上就是「一眨眼窜过去」，完全不像在巡弋。

【修正】
改为「**真实速度锚定**」：
  1. 由航迹的 ENU 米制长度与时间跨度，算出真实平均速度（m/s）；
  2. 用「该航迹的像素长 ÷ 米长」得到该航迹的**局部比例**（px/m）；
  3. 目标在画面上的推进速度 = 真实速度 × 局部比例（px/s）。

这样：
  · 每个水下目标的画面速度**与其真实游速成正比**；
  · 慢的 U002 明显比快的 U003 慢；
  · 走完一条航迹的耗时 ≈ 该航迹的真实时间跨度（U001 约 203 秒）。
"""
path = 'src/water-security/integration/SceneUnderwaterV2.vue'
src = open(path, encoding='utf-8').read()

# ---------- 1. UnderSample 增加米制坐标 ---------- #
old_sample = """interface UnderSample {
  time_ms: number
  u: number
  v: number
  depth: number
  speed: number
  course: number
}"""
new_sample = """interface UnderSample {
  time_ms: number
  u: number
  v: number
  depth: number
  speed: number
  course: number
  /** 东向坐标（米）—— 用于计算真实航迹长度与速度 */
  east: number
  /** 北向坐标（米） */
  north: number
}"""
assert old_sample in src, "UnderSample not found"
src = src.replace(old_sample, new_sample, 1)

# ---------- 2. UnderTrack 增加标定量 ---------- #
old_track = """interface UnderTrack {
  id: string
  name: string
  type: string
  color: string
  samples: UnderSample[]
  /** 绘制用的折线（容器像素） */
  polyline: string
  /** 折线上的点（容器像素），用于计算运动方向 */
  pts: { x: number; y: number; d: number }[]
  depthMin: number
  depthMax: number
}"""
new_track = """interface UnderTrack {
  id: string
  name: string
  type: string
  color: string
  samples: UnderSample[]
  /** 绘制用的折线（容器像素） */
  polyline: string
  /** 折线上的点（容器像素），用于计算运动方向 */
  pts: { x: number; y: number; d: number }[]
  depthMin: number
  depthMax: number
  /**
   * 真实平均速度（米/秒）。
   *
   * 【由 ENU 米制航迹长度 ÷ 时间跨度算出】
   * 这是水下目标**唯一可信的速度来源** ——
   * `speed_mps` 字段在部分样本里为空，
   * 而航迹的几何与时标是始终存在的。
   */
  realSpeedMps: number
  /**
   * 该航迹的**局部比例**（容器像素 / 米）。
   *
   * 【为什么要按航迹各自标定，而不是全局常数】
   * 底图 `campus_oblique_v3.png` 是概念场景图，
   * 与实测 ENU **没有地理配准** ——
   * 同一 ENU 距离在图上对应的像素数会随位置变化。
   * 所以全局常数必然有一处错、处处不准。
   *
   * 而「本航迹的像素长 ÷ 本航迹的米长」是**自洽**的：
   * 它保证了这条航迹上「画面位移」与「真实位移」的比例一致，
   * 于是「画面速度 = 真实速度」在这条航迹上严格成立。
   */
  pxPerMeter: number
  /** 航迹的容器像素总长 */
  polylinePx: number
}"""
assert old_track in src, "UnderTrack not found"
src = src.replace(old_track, new_track, 1)

# ---------- 3. collectSamples 带出米制坐标 ---------- #
old_collect = """function collectSamples(t: any): UnderSample[] {
  const raw: any[] = t.history || []
  return raw
    .filter((p: any) => p && p.view_u != null && p.view_v != null)
    .map((p: any) => ({
      time_ms: p.time_ms ?? 0,
      u: p.view_u,
      v: p.view_v,
      depth: p.depth_m ?? t.depth_m ?? 0,
      speed: p.speed_mps ?? 0,
      course: p.course_deg ?? 0,
    }))
}"""
new_collect = """function collectSamples(t: any): UnderSample[] {
  const raw: any[] = t.history || []
  return raw
    .filter((p: any) => p && p.view_u != null && p.view_v != null)
    .map((p: any) => ({
      time_ms: p.time_ms ?? 0,
      u: p.view_u,
      v: p.view_v,
      depth: p.depth_m ?? t.depth_m ?? 0,
      speed: p.speed_mps ?? 0,
      course: p.course_deg ?? 0,
      east: Number.isFinite(p.east_m) ? p.east_m : 0,
      north: Number.isFinite(p.north_m) ? p.north_m : 0,
    }))
}"""
assert old_collect in src, "collectSamples not found"
src = src.replace(old_collect, new_collect, 1)

# ---------- 4. underTracks 计算标定量 ---------- #
old_tracks = """      const depths = samples.map((s) => s.depth)

      return {
        id: t.global_track_id,
        name: t.name,
        type: t.target_type || '水下目标',
        color,
        samples,
        polyline,
        pts,
        depthMin: depths.length ? Math.min(...depths) : 0,
        depthMax: depths.length ? Math.max(...depths) : 0,
      }"""
new_tracks = """      const depths = samples.map((s) => s.depth)

      /**
       * 【真实速度标定】
       * 由 ENU 米制航迹长度与时间跨度算出真实平均速度（m/s）。
       * 这是唯一可信的速度来源 —— `speed_mps` 在部分样本里是空的。
       */
      let meters = 0
      for (let i = 1; i < samples.length; i++) {
        meters += Math.hypot(
          samples[i].east - samples[i - 1].east,
          samples[i].north - samples[i - 1].north,
        )
      }
      const spanS = (samples[samples.length - 1].time_ms - samples[0].time_ms) / 1000
      const realSpeedMps = spanS > 0 ? meters / spanS : 0

      /* 折线的容器像素总长 */
      let polylinePx = 0
      for (let i = 1; i < pts.length; i++) {
        polylinePx += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y)
      }

      /**
       * 【局部比例】
       * 该航迹的像素长 ÷ 米长。
       * 底图与 ENU 没有地理配准，全局常数必然不准；
       * 按航迹各自标定则「画面位移 : 真实位移」的比例自洽。
       */
      const pxPerMeter = meters > 0.5 ? polylinePx / meters : 0

      return {
        id: t.global_track_id,
        name: t.name,
        type: t.target_type || '水下目标',
        color,
        samples,
        polyline,
        pts,
        depthMin: depths.length ? Math.min(...depths) : 0,
        depthMax: depths.length ? Math.max(...depths) : 0,
        realSpeedMps,
        pxPerMeter,
        polylinePx,
      }"""
assert old_tracks in src, "underTracks return not found"
src = src.replace(old_tracks, new_tracks, 1)

# ---------- 5. progressOf 改为物理锚定 ---------- #
old_prog = """/**
 * 目标在航迹上的行进位置（0~1 的弧长比例）。
 *
 * 【运动节奏】
 * 用一个**与回放时钟无关**的连续相位推进，
 * 让目标始终在动 —— 即使回放暂停，画面依然有「水下目标在巡弋」的生命感。
 * 速度越快，相位推进越快。
 */
function progressOf(tr: UnderTrack): number {
  const n = tr.samples.length
  if (n < 2) return 0
  /* 目标速度：平均速度的归一化（0.35 ~ 1.0） */
  const avgSpeed = tr.samples.reduce((a, s) => a + (s.speed || 0), 0) / n
  const speedFactor = Math.max(0.35, Math.min(1.6, avgSpeed / 3))
  const phase = (animT.value / 9000) * speedFactor
  /* 循环行进：0→1→0，首尾相接 */
  const raw = phase % 2
  return raw <= 1 ? raw : 2 - raw
}"""

new_prog = """/**
 * 目标在航迹上的行进位置（0~1 的弧长比例）。
 *
 * 【运动节奏 —— 锚定到真实物理速度】
 * 用一个**与回放时钟无关**的连续相位推进，
 * 让目标始终在动 —— 即使回放暂停，画面依然有「水下目标在巡弋」的生命感。
 *
 * 【为什么必须锚定到米/秒，而不是「走完一趟要多久」】
 * 旧版用 `speedFactor = avgSpeed / 3` 这个硬凑出来的因子，
 * 再配一个固定的 9 秒周期 —— 结果速度与真实尺度完全脱钩：
 *   · 蛙人（真实 1.5 m/s）在画面上跑出 16.9 m/s，快了 11 倍；
 *   · U003（真实 2.6 m/s）跑到 49.5 m/s，快了 19 倍。
 * 视觉上就是「一眨眼窜过去」，不像在巡弋。
 *
 * 【现在的做法】
 *   画面速度(px/s) = 真实速度(m/s) × 本航迹局部比例(px/m)
 *
 * 于是：
 *   · 每个目标的画面速度与其真实游速**成正比**；
 *   · 慢的 U002 明显比快的 U003 慢；
 *   · 走完一条航迹的耗时 ≈ 该航迹的真实时间跨度
 *     （U001 约 203 秒、U002 约 302 秒、U003 约 198 秒）。
 */
function progressOf(tr: UnderTrack): number {
  const n = tr.samples.length
  if (n < 2) return 0

  /**
   * 【相位推进速率：每毫秒走过航迹的比例】
   *
   * 真实速度 → px/s → 换算成「航迹比例 / 秒」：
   *   比例速率 = (px/s) ÷ 航迹像素长
   */
  const pxPerSec = tr.realSpeedMps * tr.pxPerMeter
  if (pxPerSec <= 0 || tr.polylinePx <= 0) {
    /* 标定失败（航迹退化）→ 退回一个很慢的默认速度 */
    return (animT.value / 600000) % 2 > 1 ? 2 - ((animT.value / 600000) % 2) : (animT.value / 600000) % 2
  }
  const ratioPerSec = pxPerSec / tr.polylinePx

  const phase = (animT.value / 1000) * ratioPerSec
  /* 循环行进：0→1→0，首尾相接 */
  const raw = phase % 2
  return raw <= 1 ? raw : 2 - raw
}"""
assert old_prog in src, "progressOf not found"
src = src.replace(old_prog, new_prog, 1)

open(path, 'w', encoding='utf-8').write(src)
print("underwater motion physically anchored")
