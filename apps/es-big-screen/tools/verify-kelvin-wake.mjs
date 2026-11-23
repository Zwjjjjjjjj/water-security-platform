/**
 * 校验 Kelvin 尾迹几何是否符合物理规律。
 *
 * 用法：node tools/verify-kelvin-wake.mjs
 */
import { buildWake } from '../src/water-security/integration/water-effects.ts'

/* 线性 ENU → 图片像素（与 scenes.config.v2.ts 的 enuToImage 一致） */
const A = 1536 / 3600
const D = -960 / 2200
const E_OFF = A * 2600
const F = 40 - D * 5100
const toPx = (e, n) => [A * e + E_OFF, D * n + F]

let failures = 0
function check(name, cond, detail) {
  const ok = !!cond
  if (!ok) failures++
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

/* ---------- 1. 速度门槛 ---------- */
{
  const hist = [
    { e: 0, n: 4000, t: 0 },
    { e: 10, n: 4000, t: 1000 },
  ]
  const slow = buildWake(hist, toPx, { speedMps: 0.4, lengthM: 60, beamM: 12, nowMs: 1000 })
  check('速度 0.4 m/s → 无尾迹', slow.length === 0, `得到 ${slow.length} 段`)

  const fast = buildWake(hist, toPx, { speedMps: 8, lengthM: 60, beamM: 12, nowMs: 1000 })
  check('速度 8 m/s → 有尾迹', fast.length > 0, `得到 ${fast.length} 段`)
}

/* ---------- 2. Kelvin 半角 ---------- */
{
  /* 匀速直线航迹，沿正北（东向不变） */
  const hist = []
  for (let i = 0; i <= 40; i++) {
    hist.push({ e: 0, n: 4000 - i * 20, t: i * 1000 })
  }
  const segs = buildWake(hist, toPx, { speedMps: 10, lengthM: 100, beamM: 20, nowMs: 40000 })

  const arms = segs.filter((s) => s.kind === 'arm')
  check('生成了 Kelvin 波臂', arms.length >= 4, `${arms.length} 段`)

  /* 船尾 = 最新的位置 */
  const stern = toPx(0, 4000 - 40 * 20)
  /* 把每段的远端点按到船尾的方向分组，计算左右两侧的夹角 */
  const left = []
  const right = []
  for (const s of arms) {
    const p = s.points[s.points.length - 1]
    const dx = p[0] - stern[0]
    const dy = p[1] - stern[1]
    /* 航迹沿正北（屏幕 y 向上为北，图片像素 y 向下 → 北 = -y） */
    /* 夹角相对「船尾→该点」与「正北方向」的夹角 */
    const ang = Math.atan2(dx, -dy) * (180 / Math.PI)
    if (Math.abs(ang) < 90) (ang > 0 ? right : left).push(Math.abs(ang))
  }
  const avg = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : NaN)
  const half = (avg(left) + avg(right)) / 2
  check(
    'Kelvin 半角接近 17°',
    Number.isFinite(half) && half > 10 && half < 25,
    `实测半角 ${half.toFixed(2)}°（左 ${avg(left).toFixed(2)}° / 右 ${avg(right).toFixed(2)}°）`,
  )
}

/* ---------- 3. 强度随年龄衰减 ---------- */
{
  const hist = []
  for (let i = 0; i <= 40; i++) {
    hist.push({ e: 0, n: 4000 - i * 20, t: i * 1000 })
  }
  const segs = buildWake(hist, toPx, { speedMps: 10, lengthM: 100, beamM: 20, nowMs: 40000 })
  const arms = segs.filter((s) => s.kind === 'arm')
  /* 第 0 段最靠近船尾（年龄最小），最后一段年龄最大 */
  const opacities = arms.map((s) => s.opacity)
  const first = Math.max(...opacities)
  const last = Math.min(...opacities)
  check(
    '尾迹强度沿长度衰减',
    last < first * 0.6,
    `最亮 ${first.toFixed(3)} → 最淡 ${last.toFixed(3)}`,
  )

  /* 宽度也应衰减 */
  const widths = arms.map((s) => s.width)
  check(
    '尾迹宽度沿长度衰减',
    Math.min(...widths) < Math.max(...widths) * 0.85,
    `最宽 ${Math.max(...widths).toFixed(2)} → 最窄 ${Math.min(...widths).toFixed(2)}`,
  )
}

/* ---------- 4. 尾迹寿命 ---------- */
{
  const hist = []
  for (let i = 0; i <= 120; i++) {
    hist.push({ e: 0, n: 4000 - i * 20, t: i * 1000 })
  }
  const old = buildWake(hist, toPx, { speedMps: 10, lengthM: 100, beamM: 20, nowMs: 120000 })
  const oldArms = old.filter((s) => s.kind === 'arm')
  check(
    '超出寿命的尾迹被丢弃',
    oldArms.length < 20,
    `120 秒前的历史只剩 ${oldArms.length} 段（寿命 60 秒）`,
  )
}

/* ---------- 5. 尾流类型分层 ---------- */
{
  const hist = []
  for (let i = 0; i <= 30; i++) {
    hist.push({ e: i * 10, n: 4000, t: i * 1000 })
  }
  const segs = buildWake(hist, toPx, { speedMps: 8, lengthM: 90, beamM: 18, nowMs: 30000 })
  const kinds = new Set(segs.map((s) => s.kind))
  check('包含 wash（螺旋桨白水）', kinds.has('wash'), [...kinds].join(','))
  check('包含 arm（Kelvin 波臂）', kinds.has('arm'), [...kinds].join(','))

  /* 白水应该比波臂更宽更亮 */
  const wash = segs.filter((s) => s.kind === 'wash')
  const arm = segs.filter((s) => s.kind === 'arm')
  check(
    '白水比波臂更宽',
    Math.max(...wash.map((s) => s.width)) > Math.max(...arm.map((s) => s.width)),
    `白水最大 ${Math.max(...wash.map((s) => s.width)).toFixed(2)} vs 波臂 ${Math.max(...arm.map((s) => s.width)).toFixed(2)}`,
  )
}

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`)
process.exit(failures === 0 ? 0 : 1)
