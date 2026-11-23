import { defineStore } from 'pinia'

export interface LayerState {
  coverage: boolean
  source_tracks: boolean
  fused_tracks: boolean
}

/**
 * 数据源模式。
 *
 * · `live`   —— 实时监控：时间轴走**真实墙钟**，末端永远贴着「现在」，
 *               数据从历史段末尾开始循环播放，像监控系统一样持续滚动。
 * · `replay` —— 数据回放：时间轴是这一段数据的**相对偏移**（00:00 → 05:02），
 *               可拖拽、可倒放，用于复盘取证。
 */
export type TimelineMode = 'live' | 'replay'

export const useWaterStore = defineStore({
  id: 'water',
  state: () => {
    return {
      /** 数据集内时间（毫秒），0 ~ duration */
      time_ms: 45000,
      playing: false,
      speed: 1,
      /**
       * 播放方向。
       *  1 = 正放，-1 = 倒放。
       *  实时监控模式下正放表示「追着现在走」，倒放表示「回看刚才」。
       */
      direction: 1 as 1 | -1,
      /** 时间轴模式 */
      timelineMode: 'live' as TimelineMode,
      /**
       * 实时模式的「现在」锚点。
       * 用 epoch 毫秒保存，避免受系统时区变化影响。
       */
      liveNowMs: Date.now(),
      selectedTargetId: 'T001',
      mode: 'overview' as 'overview' | 'surface' | 'underwater' | 'replay',
      follow: false,
      scripted: false,
      layers: { coverage: true, source_tracks: true, fused_tracks: true } as LayerState,
      dataMode: 'simulated' as string,
    }
  },
  getters: {
    /** 是否处于倒放 */
    isReversing: (s) => s.direction === -1,
    /** 实时监控模式 */
    isLive: (s) => s.timelineMode === 'live',
  },
  actions: {
    setTime(t: number) {
      this.time_ms = Math.max(0, Math.min(302000, Number(t) || 0))
    },
    select(id: string) {
      this.selectedTargetId = id
      this.scripted = false
    },
    setMode(m: 'overview' | 'surface' | 'underwater' | 'replay') {
      this.mode = m
      this.scripted = false
    },
    /** 正放 / 倒放切换 */
    setDirection(d: 1 | -1) {
      this.direction = d
    },
    toggleDirection() {
      this.direction = this.direction === 1 ? -1 : 1
    },
    /**
     * 切换时间轴模式。
     *
     * 实时模式：时间轴贴住数据末端，向「现在」滚动。
     * 回放模式：时间轴是整段数据，末端即 05:02。
     */
    setTimelineMode(m: TimelineMode) {
      if (this.timelineMode === m) return
      this.timelineMode = m
      if (m === 'live') {
        /* 进入实时：播放头从头开始，随墙钟持续滚动 */
        this.time_ms = 0
        this.direction = 1
        this.playing = true
      } else {
        this.direction = 1
      }
    },
  },
})
