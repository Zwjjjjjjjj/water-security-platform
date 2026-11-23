import { ref, shallowRef, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useWaterStore } from '@/store/water'
import { loadDemoData, getDemoSnapshot, getScriptedState, formatTime, clampTime } from '@/water-security/integration/demo-engine.js'
import {
  DATASET_DURATION_MS,
  dataMsToEpoch,
  formatAbsolute,
  formatClock,
  formatOffset,
  formatShort,
  liveDataMs,
} from '@/water-security/integration/live-clock'

const dataRef = shallowRef<any>(null)
let cached: any = null
let loading: Promise<any> | null = null
let rafId = 0
let prevTs = 0
let subscribers = 0
let activeStore: any = null

/**
 * 实时模式的锚点。
 *
 * 进入实时监控那一刻记录：
 *   · 墙钟时间 `anchorEpochMs`
 *   · 数据集内的位置 `anchorDataMs`
 * 之后播放头位置完全由墙钟推导，天然与真实时间同步。
 */
let anchorEpochMs = 0
let anchorDataMs = DATASET_DURATION_MS

function loop(now: number) {
  if (!subscribers) return
  if (prevTs && activeStore?.playing && dataRef.value) {
    if (activeStore.timelineMode === 'live') {
      /* ---------- 实时监控：播放头跟着墙钟走 ---------- */
      const next = liveDataMs(anchorEpochMs, anchorDataMs, Date.now(), activeStore.direction)
      activeStore.time_ms = next
      /* 锚点跟随，避免长时间运行后累积漂移 */
      anchorEpochMs = Date.now()
      anchorDataMs = next
    } else if (activeStore.timelineMode === 'replay') {
      /* ---------- 数据回放：按倍速推进/倒退 ---------- */
      const dt = Math.min(250, now - prevTs) * activeStore.speed * activeStore.direction
      let t = clampTime(dataRef.value, activeStore.time_ms + dt)
      if (activeStore.scripted) {
        const s = getScriptedState(dataRef.value, t)
        activeStore.selectedTargetId = s.selected_target_id
        /* 只有脚本里真的有 set_mode 关键帧才切页面，
           否则用户会被强行拽到 overview。 */
        if (s.mode) activeStore.mode = s.mode
      }
      activeStore.time_ms = t
      const dur = dataRef.value.scene?.duration_ms ?? DATASET_DURATION_MS
      if (activeStore.direction === 1 && t >= dur) {
        /* 正放到头 → 停住（回放模式下不循环，避免看不清） */
        activeStore.playing = false
      }
      if (activeStore.direction === -1 && t <= 0) {
        activeStore.playing = false
      }
    }
  }
  prevTs = now
  rafId = requestAnimationFrame(loop)
}

function ensureLoop(store: any) {
  activeStore = store
  if (!rafId) {
    prevTs = 0
    rafId = requestAnimationFrame(loop)
  }
}

function releaseLoop() {
  subscribers -= 1
  if (subscribers <= 0) {
    subscribers = 0
    cancelAnimationFrame(rafId)
    rafId = 0
    prevTs = 0
  }
}

async function ensureData(assetBase: string) {
  if (cached) return cached
  if (!loading) {
    loading = loadDemoData(assetBase).then((d: any) => {
      cached = d
      dataRef.value = d
      return d
    })
  }
  return loading
}

export function useWaterDemo() {
  const store = useWaterStore()
  const assetBase = `${import.meta.env.BASE_URL}water-security`.replace(/\/\//g, '/')
  const error = ref('')
  const ready = ref(!!cached)

  const snapshot = computed(() => {
    if (!dataRef.value) return null
    return getDemoSnapshot(dataRef.value, store.time_ms)
  })

  const scriptedState = computed(() => {
    if (!dataRef.value) return null
    return getScriptedState(dataRef.value, store.time_ms)
  })

  /**
   * 实时心跳。
   *
   * 【为什么需要它】
   * 实时模式下时钟要跟着墙钟走，而 `Date.now()` **不是响应式的** ——
   * 写在 computed 里只会在依赖变化时重算，秒针不会自己走。
   * 这里用一个 1 s 间隔的 ref 作为「秒针触发器」，
   * 让所有依赖实时时刻的计算属性每秒重算一次。
   */
  const heartbeat = ref(0)
  let heartbeatTimer = 0

  /**
   * 当前时刻的绝对时间戳。
   *
   * 实时模式下 = 真实墙钟；
   * 回放模式下 = 数据集时基 + 数据内偏移。
   */
  const currentEpochMs = computed(() => {
    if (store.timelineMode === 'live') {
      /* 依赖 heartbeat，保证每秒刷新 */
      void heartbeat.value
      return Date.now()
    }
    return dataMsToEpoch(store.time_ms)
  })

  /** 绝对时间文本「2026-11-21 11:28:05」 */
  const absoluteText = computed(() => formatAbsolute(currentEpochMs.value))

  /** 时钟文本「11:28:05」 */
  const clockText = computed(() => formatClock(currentEpochMs.value))

  /** 紧凑文本「11-21 11:28:05」 */
  const shortText = computed(() => formatShort(currentEpochMs.value))

  /** 相对「现在」的偏移「-00:42」 */
  const offsetText = computed(() => {
    if (store.timelineMode === 'live') {
      const dt = Date.now() - currentEpochMs.value
      return formatOffset(dt)
    }
    return formatOffset(store.time_ms - DATASET_DURATION_MS)
  })

  /**
   * 方向切换 → 重设锚点。
   *
   * 不这么做的话，倒放瞬间会因为 dt 沿用上一方向的累积值而瞬移。
   */
  watch(
    () => store.direction,
    () => {
      if (store.timelineMode === 'live') {
        anchorEpochMs = Date.now()
        anchorDataMs = store.time_ms
      }
    },
  )

  onMounted(async () => {
    subscribers += 1
    ensureLoop(store)
    /* 实时心跳：每秒触发一次时钟重算 */
    heartbeatTimer = window.setInterval(() => {
      heartbeat.value = Date.now()
    }, 1000)
    try {
      await ensureData(assetBase)
      ready.value = true
      /**
       * 默认进入实时监控。
       *
       * 【为什么初始位置是 0 而不是末端】
       * 实时监控是**持续滚动**的监控回放：
       * 播放头从数据集开头开始，随墙钟一秒一秒往前走，
       * 走完 302 s 自动从头再来一轮 —— 画面永远在动。
       *
       * 若从末端起步，`liveDataMs` 的取模会立刻把它绕回 0，
       * 看起来就是「刚打开就跳一下」。
       */
      store.timelineMode = 'live'
      store.time_ms = 0
      anchorEpochMs = Date.now()
      anchorDataMs = 0
      store.playing = true
    } catch (e: any) {
      error.value = e?.message || String(e)
    }
  })
  onBeforeUnmount(() => {
    releaseLoop()
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = 0
    }
  })

  function seek(t: number) {
    if (!dataRef.value) { store.setTime(t); return }
    const ct = clampTime(dataRef.value, t)
    if (store.scripted) {
      const s = getScriptedState(dataRef.value, ct)
      store.selectedTargetId = s.selected_target_id
      /* 同上：没有 set_mode 关键帧就不动当前页面 */
      if (s.mode) store.mode = s.mode as any
    }
    store.setTime(ct)
    /* 手动拖动后要重设锚点，否则实时模式会把播放头弹回去 */
    if (store.timelineMode === 'live') {
      anchorEpochMs = Date.now()
      anchorDataMs = ct
    }
  }

  /**
   * 重设实时锚点。
   *
   * 切换倒放方向时必须调用：
   * 锚点记录的是「某个墙钟时刻 ↔ 某个数据位置」的对应关系，
   * 方向一变，播放头就该从**当前位置**往回走，
   * 否则会因为 dt 是上一方向的累积量而瞬移。
   */
  function resyncAnchor() {
    anchorEpochMs = Date.now()
    anchorDataMs = store.time_ms
  }

  /**
   * 一键演示。
   *
   * 【为什么不切时间轴模式】
   * 原来这里会把 `timelineMode` 强制改成 'replay'，
   * 于是点一下「一键演示」，实时监控就被踢回数据回放，
   * 时钟从真实墙钟跳回数据集时基 —— 用户看着像程序出错。
   *
   * 演示脚本本质是「按关键帧自动选目标、自动切场景视角」，
   * 与时间轴是实时还是回放无关，所以保持当前模式即可。
   *
   * 【为什么不跳到 0】
   * 同样地，从头播会打断当前正在看的内容。
   * 这里从**当前时刻**起播，只是接管目标选择，
   * 用户随时可以再点一次暂停。
   */
  function startDemo() {
    store.scripted = true
    store.follow = false
    store.playing = true
  }

  return {
    store,
    data: dataRef,
    snapshot,
    scriptedState,
    ready,
    error,
    assetBase,
    seek,
    startDemo,
    formatTime,
    currentEpochMs,
    absoluteText,
    clockText,
    shortText,
    offsetText,
    resyncAnchor,
  }
}
