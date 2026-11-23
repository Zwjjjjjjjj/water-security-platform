/**
 * 实时监控时间轴 —— 统一管理「数据时间 ↔ 墙钟时间」的换算。
 *
 * 【为什么需要这一层】
 * 原始数据集是一段 302 s 的历史录像（相对时基 t0）。
 * 监控大屏要表现的是「实时」：
 *   · 时钟要显示**真实日期时间**（2026-11-21 11:28:05），不是 00:45；
 *   · 播放头要贴着「现在」，随墙钟持续前进；
 *   · 要能倒放（回看刚才发生了什么）。
 *
 * 【时间基准】
 * 数据集自带一条真值注释：
 *   「雷达/AIS 03-05 包真值，相对时基 t0 = 2025-03-05 10:35:38(local)」
 * 也就是数据集内的 t=0 对应 **2025-03-05 10:35:38**。
 * 把它换算成 epoch 毫秒后，数据里的每个时刻都能映射到一个绝对时间。
 *
 * 【实时模式怎么「持续有数据」】
 * 实时监控不可能等到 2025 年的录像才出画面，
 * 所以实时模式采用**滚动窗口**：
 * 数据末端（t = duration）锚定到「现在」，
 * 播放头随墙钟前进，超出末端后数据从头循环（表现为持续滚动的监控回放）。
 */

/** 数据集时基：2025-03-05 10:35:38 本地时间 */
export const DATASET_EPOCH_ISO = '2025-03-05T10:35:38'

/** 数据集时基的 epoch 毫秒 */
export const DATASET_EPOCH_MS = new Date(DATASET_EPOCH_ISO).getTime()

/** 数据集总时长（毫秒） */
export const DATASET_DURATION_MS = 302000

/**
 * 数据集内时刻 → 绝对时间戳。
 */
export function dataMsToEpoch(dataMs: number): number {
  return DATASET_EPOCH_MS + dataMs
}

/**
 * 绝对时间戳 → 数据集内时刻（超界则取模循环）。
 */
export function epochToDataMs(epochMs: number): number {
  const rel = epochMs - DATASET_EPOCH_MS
  if (rel < 0) return 0
  return rel % DATASET_DURATION_MS
}

/** 补零 */
function p2(n: number): string {
  return String(n).padStart(2, '0')
}

/**
 * 格式化为「YYYY-MM-DD HH:mm:ss」。
 */
export function formatAbsolute(epochMs: number): string {
  const d = new Date(epochMs)
  return (
    `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ` +
    `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
  )
}

/**
 * 格式化为「HH:mm:ss」。
 */
export function formatClock(epochMs: number): string {
  const d = new Date(epochMs)
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
}

/**
 * 格式化为「MM-DD HH:mm:ss」（紧凑版，用于图表轴）。
 */
export function formatShort(epochMs: number): string {
  const d = new Date(epochMs)
  return `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
}

/**
 * 相对偏移，格式化为「+MM:SS」或「-MM:SS」。
 */
export function formatOffset(ms: number): string {
  const sign = ms < 0 ? '-' : '+'
  const abs = Math.abs(ms)
  const s = Math.floor(abs / 1000)
  return `${sign}${p2(Math.floor(s / 60))}:${p2(s % 60)}`
}

/**
 * 计算实时模式下的播放头位置。
 *
 * 【原理】
 * `anchor` 是进入实时模式那一刻的墙钟时间，
 * `anchorDataMs` 是那一刻数据集内的位置（通常是末端）。
 * 经过 `dt` 毫秒后，数据内位置就是 `anchorDataMs + dt * direction`。
 *
 * 超出 [0, duration] 就取模循环 —— 表现为监控画面持续滚动。
 */
export function liveDataMs(
  anchorEpochMs: number,
  anchorDataMs: number,
  nowEpochMs: number,
  direction: 1 | -1,
): number {
  const dt = nowEpochMs - anchorEpochMs
  const raw = anchorDataMs + dt * direction
  /**
   * 取模循环，让监控画面持续滚动。
   *
   * 注意 JS 的 `%` 对负数会返回负值，所以要先归一化。
   */
  const wrapped = ((raw % DATASET_DURATION_MS) + DATASET_DURATION_MS) % DATASET_DURATION_MS
  return wrapped
}
