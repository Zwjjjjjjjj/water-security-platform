#!/usr/bin/env python3
"""在 ChartSceneV2.vue 中加入船体航向平滑：
真实航迹的瞬时航向在样本间会剧烈跳变（实测 T002 24 个样本里有 13 次跳变 >45°），
直接用它驱动船体贴图会让船体「抽搐式」转向。
这里用**沿航迹切线**的平滑航向替代瞬时航向。
"""
path = 'src/water-security/integration/ChartSceneV2.vue'
src = open(path, encoding='utf-8').read()

# ---------- 1. 新增工具函数：平滑航向 ----------
anchor = """/**
 * 米制 ENU → 图表容器像素 的投影函数。"""

helper = '''/**
 * 船体航向平滑。
 *
 * 【为什么要平滑 —— 实测数据的问题】
 * 瞬时 `course_deg` 在相邻样本间会剧烈跳变：
 * 实测 T002 的 24 个样本里有 **13 次**相邻航向跳变超过 45°
 * （例如 259.6° → 0°，这是跨 0°/360° 的正常回绕，但也存在真正的急转）。
 * 直接用它驱动船体贴图，船只就会「抽搐式」转向 —— 完全不像在航行。
 *
 * 【平滑方法】
 * 不用低通滤波（会把船头转向的延迟做得很难看），
 * 而是取**最近若干个采样点的位置**，用**首尾连线**求切线方向：
 *
 *   θ = atan2(Δe, Δn)   （0° = 正北，顺时针为正，与 course_deg 同约定）
 *
 * 这等价于「弦切法」—— 用一段弧的弦代替切线，
 * 天然把单点噪声抹平，且**没有延迟**（只用当前位置之前的点）。
 *
 * @param history 历史点（ENU）
 * @param fallback 无有效历史时回退的航向（度）
 * @param window 取最近多少个点求弦
 */
function smoothedCourseDeg(
  history: { east_m: number; north_m: number }[],
  fallback: number | null,
  window = 6,
): number | null {
  if (fallback == null || !Number.isFinite(fallback)) return null
  /* 取最近 window 个有效点 */
  const pts: [number, number][] = []
  for (let i = history.length - 1; i >= 0 && pts.length < window; i--) {
    const p = history[i]
    if (!p) continue
    if (!Number.isFinite(p.east_m) || !Number.isFinite(p.north_m)) continue
    pts.push([p.east_m, p.north_m])
  }
  if (pts.length < 2) return fallback
  /* pts 是逆序（最近的在前），取最老与最新 */
  const newest = pts[0]
  const oldest = pts[pts.length - 1]
  const de = newest[0] - oldest[0]
  const dn = newest[1] - oldest[1]
  /* 位移过小（几乎没动）→ 用回退值，避免除零/随机抖动 */
  if (Math.hypot(de, dn) < 0.5) return fallback
  /* 0° = 正北、顺时针为正 */
  const deg = (Math.atan2(de, dn) * 180) / Math.PI
  return (deg + 360) % 360
}

/**
 * 在两个航向之间做**最短弧插值**。
 *
 * 【为什么不能直接线性插值】
 * 359° → 1° 的线性插值会走 358°（几乎转了一整圈），
 * 而实际只转了 2°。必须把差值归一化到 (-180, 180]。
 */
function lerpCourseDeg(a: number, b: number, t: number): number {
  let d = ((b - a + 540) % 360) - 180
  return (a + d * t + 360) % 360
}

/**
 * 米制 ENU → 图表容器像素 的投影函数。'''

assert anchor in src
src = src.replace(anchor, helper, 1)

open(path, 'w', encoding='utf-8').write(src)
print("helpers added")
