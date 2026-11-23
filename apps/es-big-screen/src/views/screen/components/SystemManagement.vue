<template>
  <div class="ws-sysmgmt">
    <!-- ==================== 页头 ==================== -->
    <header class="ws-sysmgmt-head">
      <div class="ws-sysmgmt-head-main">
        <h2>系统管理</h2>
        <p>水域安防数字孪生平台 · 设备接入、链路状态与平台配置</p>
      </div>
      <div class="ws-sysmgmt-head-meta">
        <span class="ws-tag" :class="{ 'is-warn': health.level !== 'good' }">
          {{ health.label }}
        </span>
        <span class="ws-sysmgmt-clock">{{ clockText }}</span>
      </div>
    </header>

    <!-- ==================== 概览指标 ==================== -->
    <section class="ws-sysmgmt-metrics">
      <div v-for="m in metrics" :key="m.label" class="ws-sysmgmt-metric">
        <span class="ws-sysmgmt-metric-label">{{ m.label }}</span>
        <strong class="ws-sysmgmt-metric-value" :class="m.cls">{{ m.value }}</strong>
        <span class="ws-sysmgmt-metric-unit">{{ m.unit }}</span>
      </div>
    </section>

    <!-- ==================== 主体两栏 ==================== -->
    <div class="ws-sysmgmt-body">
      <!-- ---------- 左：感知设备接入（含设备详情） ---------- -->
      <section class="ws-panel ws-sysmgmt-col">
        <h2 class="ws-panel-title">
          感知设备接入
          <small>{{ devices.length }} 台设备 · {{ activeDevices }} 台在线</small>
        </h2>
        <div class="ws-panel-content">
          <table class="ws-sysmgmt-table">
            <thead>
              <tr>
                <th>设备编号</th>
                <th>类型 / 型号</th>
                <th>部署位置</th>
                <th>探测能力</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="d in deviceRows"
                :key="d.source_id"
                class="ws-sysmgmt-devrow"
                :class="{ 'is-active': d.source_id === activeDeviceId }"
                @click="activeDeviceId = d.source_id"
              >
                <td class="is-strong">{{ d.source_id }}</td>
                <td>
                  <span class="ws-sysmgmt-devkind">{{ d.kindLabel }}</span>
                  <span class="ws-sysmgmt-devmodel">{{ d.model }}</span>
                </td>
                <td>
                  <span class="ws-sysmgmt-devloc">{{ d.locText }}</span>
                  <span class="ws-sysmgmt-elev">高程 {{ d.elevationM }} m</span>
                </td>
                <td>
                  <span class="ws-sysmgmt-devcap">{{ d.capability }}</span>
                </td>
                <td>
                  <span class="ws-sysmgmt-state" :class="'is-' + d.status">
                    <i />{{ d.state_label }}
                  </span>
                </td>
              </tr>
              <tr v-if="!devices.length">
                <td colspan="5" class="ws-sysmgmt-empty">暂无设备数据</td>
              </tr>
            </tbody>
          </table>

          <!-- ============ 选中设备详情 ============ -->
          <div v-if="activeDevice" class="ws-sysmgmt-detail">
            <h3 class="ws-sysmgmt-sub">
              设备详情 · {{ activeDevice.source_id }}
              <span class="ws-sysmgmt-detail-state">
                {{ activeDevice.kindLabel }} · {{ activeDevice.state_label }}
              </span>
            </h3>
            <div class="ws-sysmgmt-detailgrid">
              <div v-for="f in activeDeviceFields" :key="f.k" class="ws-sysmgmt-detailitem">
                <span class="ws-sysmgmt-detailkey">{{ f.k }}</span>
                <span class="ws-sysmgmt-detailval" :class="f.cls">{{ f.v }}</span>
              </div>
            </div>
            <!-- 链路健康条 -->
            <div v-if="activeDeviceLinks.length" class="ws-sysmgmt-links">
              <div
                v-for="l in activeDeviceLinks"
                :key="l.name"
                class="ws-sysmgmt-link"
                :class="'is-' + l.state"
              >
                <span class="ws-sysmgmt-linkname">{{ l.name }}</span>
                <span class="ws-sysmgmt-linkbar">
                  <i :style="{ width: l.pct + '%' }" />
                </span>
                <span class="ws-sysmgmt-linkval">{{ l.pct }}%</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ---------- 右：平台配置 ---------- -->
      <section class="ws-panel ws-sysmgmt-col">
        <h2 class="ws-panel-title">平台配置<small>演示环境</small></h2>
        <div class="ws-panel-content">
          <div class="ws-sysmgmt-config">
            <label class="ws-sysmgmt-field">
              <span>回放倍速</span>
              <select v-model.number="store.speed" class="ws-sysmgmt-select">
                <option :value="0.5">0.5×</option>
                <option :value="1">1×</option>
                <option :value="2">2×</option>
                <option :value="4">4×</option>
              </select>
            </label>
            <label class="ws-sysmgmt-field">
              <span>场景版本</span>
              <select v-model="sceneVersion" class="ws-sysmgmt-select">
                <option value="v1">场景 V1（2.5D 手绘底图）</option>
                <option value="v2">场景 V2（写实航拍斜俯视）</option>
              </select>
            </label>
          </div>

          <h3 class="ws-sysmgmt-sub">监视图层开关</h3>
          <label class="ws-sysmgmt-switch">
            <span>雷达覆盖</span>
            <input v-model="store.layers.coverage" type="checkbox" />
          </label>
          <label class="ws-sysmgmt-switch">
            <span>源点迹</span>
            <input v-model="store.layers.source_tracks" type="checkbox" />
          </label>
          <label class="ws-sysmgmt-switch">
            <span>融合航迹</span>
            <input v-model="store.layers.fused_tracks" type="checkbox" />
          </label>

          <!--
            时间轴控制 —— 与底部进度条同一套状态。

            · 实时监控：时钟走真实墙钟，播放头贴着「现在」，可倒放回看；
            · 数据回放：在这一段数据里定位/倒放，用于复盘取证。
          -->
          <h3 class="ws-sysmgmt-sub">时间轴控制</h3>
          <div class="ws-sysmgmt-timemodes" role="group" aria-label="时间轴模式">
            <button
              type="button"
              class="ws-sysmgmt-timemode"
              :class="{ 'is-on': store.timelineMode === 'live' }"
              @click="store.setTimelineMode('live')"
            >
              实时监控
            </button>
            <button
              type="button"
              class="ws-sysmgmt-timemode"
              :class="{ 'is-on': store.timelineMode === 'replay' }"
              @click="store.setTimelineMode('replay')"
            >
              数据回放
            </button>
          </div>

          <div class="ws-sysmgmt-playrow">
            <button class="ws-button ws-button--primary" @click="togglePlay">
              {{ store.playing ? '暂停' : '继续' }}
            </button>
            <button
              class="ws-button ws-sysmgmt-rev"
              :class="{ 'is-on': store.direction === -1 }"
              :aria-pressed="store.direction === -1"
              @click="store.toggleDirection()"
            >
              {{ store.direction === -1 ? '倒放中' : '倒放' }}
            </button>
            <button class="ws-button" @click="restart">回到起点</button>
            <button class="ws-button" @click="jumpToEnd">跳到末尾</button>
          </div>

          <!-- 实时时钟 -->
          <div class="ws-sysmgmt-clockbox">
            <div class="ws-sysmgmt-clockmain">
              <span class="ws-sysmgmt-clock">{{ clockText }}</span>
              <span class="ws-sysmgmt-clockdate">{{ dateText }}</span>
            </div>
            <div class="ws-sysmgmt-clockmeta">
              <span class="ws-tag" :class="store.timelineMode === 'live' ? 'is-live' : 'is-replay'">
                {{ store.timelineMode === 'live' ? '实时监控' : '数据回放' }}
              </span>
              <span class="ws-sysmgmt-clocknote">
                {{ store.timelineMode === 'live' ? '时钟与墙钟同步 · 数据滚动刷新' : '时基 2025-03-05 10:35:38 · 相对偏移 ' + offsetText }}
              </span>
            </div>
          </div>

          <div class="ws-sysmgmt-progress">
            <div class="ws-sysmgmt-progressbar">
              <i :style="{ width: progressPct + '%' }" />
            </div>
            <span class="ws-sysmgmt-progresstext">
              {{ clockText }} / {{ totalClockText }}
            </span>
          </div>

          <h3 class="ws-sysmgmt-sub">数据源</h3>
          <div class="ws-sysmgmt-source">
            <span class="ws-tag">实测航迹数据</span>
            <span class="ws-sysmgmt-source-note">
              站心坐标 · 北纬37.53297° / 东经121.4199°
            </span>
            <span class="ws-sysmgmt-source-note">
              坐标系统 ENU（米）· 采样 1 s · 会话 {{ sessionId }}
            </span>
          </div>

          <h3 class="ws-sysmgmt-sub">目标接入概览</h3>
          <div class="ws-sysmgmt-targets">
            <div v-for="t in targetRows" :key="t.id" class="ws-sysmgmt-trow">
              <span class="ws-sysmgmt-tid" :class="'is-' + t.domain">{{ t.id }}</span>
              <span class="ws-sysmgmt-tmmsi">{{ t.mmsi }}</span>
              <span class="ws-sysmgmt-tsrc">{{ t.sourceText }}</span>
              <span class="ws-sysmgmt-tstatus" :class="'is-' + t.status">{{ t.statusText }}</span>
            </div>
            <p v-if="!targetRows.length" class="ws-sysmgmt-empty">当前时刻无目标</p>
          </div>
        </div>
      </section>
    </div>

    <!-- ==================== 底部：运行日志 ==================== -->
    <section class="ws-panel ws-sysmgmt-log">
      <h2 class="ws-panel-title">
        系统日志
        <small>最近 {{ logRows.length }} 条 · 会话 {{ sessionId }}</small>
      </h2>
      <div class="ws-panel-content">
        <ul class="ws-sysmgmt-loglist">
          <li v-for="l in logRows" :key="l.id">
            <span class="ws-sysmgmt-log-time">{{ l.time }}</span>
            <span class="ws-sysmgmt-log-level" :class="'is-' + l.level">{{ l.levelText }}</span>
            <span class="ws-sysmgmt-log-msg">{{ l.msg }}</span>
          </li>
          <li v-if="!logRows.length" class="ws-sysmgmt-empty">暂无日志</li>
        </ul>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * 系统管理视图。
 *
 * 【定位】
 * 顶部导航第 4 个页签原为「航迹回放」，现改为「系统管理」——
 * 页面从「看数据」转为「管平台」：设备接入状态、平台配置、
 * 图层开关与运行日志，都放在这一页。
 *
 * 【本页的三层结构】
 *   1. 概览指标条 —— 设备/目标/链路的实时计数（全部由快照算出）
 *   2. 主体两栏 —— 左「设备接入 + 设备详情」，右「平台配置 + 回放控制 + 目标概览」
 *   3. 底部日志 —— 由当前回放时刻的数据**推导**，不是随机生成
 *
 * 【数据来源】
 * 设备表来自 `useWaterDemo()` 的 `data.devices.items`（与左侧「感知设备」
 * 面板同源，不另造一套）；回放倍速与图层开关直接双向绑定 `store`，
 * 改完立即作用于主场景。
 *
 * 【设备静态档案的来源】
 * `devices.json` 里只有编号/类型/状态/经纬度这些**接入层**信息；
 * 型号、探测半径、扫描周期、方位等**装备层**信息定义在
 * `SceneSensorsV2.vue` 的 `SENSORS` 表里（那是场景实际使用的参数）。
 * 本页把那份档案引过来，让「平台管理」能显示设备究竟能干什么，
 * 而不是只显示它「在线」。
 */
import { computed, ref, watch } from 'vue'
import { useWaterDemo } from '@/water-security/useWaterDemo'
import { SENSOR_ARCHIVE, archiveBySourceId, capabilityText, linkHealth } from '@/water-security/integration/sensor-archive'
import { dataMsToEpoch, formatClock } from '@/water-security/integration/live-clock'

const { store, data, snapshot, clockText, absoluteText, offsetText } = useWaterDemo()

/* 场景版本：与顶部「场景 V1 / V2」按钮共用同一个 localStorage 键，
 * 两处保持一致，改动后主场景立即切换。 */
const SCENE_VERSION_KEY = 'es-scene-version'
const sceneVersion = ref<'v1' | 'v2'>(
  (localStorage.getItem(SCENE_VERSION_KEY) as 'v1' | 'v2') || 'v1',
)
watch(sceneVersion, v => localStorage.setItem(SCENE_VERSION_KEY, v))

const devices = computed<any[]>(() => (data.value?.devices?.items as any[]) || [])

/** 当前选中的设备编号（点击设备行切换详情） */
const activeDeviceId = ref('')

/** 会话 ID */
const sessionId = computed(() => String(data.value?.meta?.session_id || '—'))

/**
 * 时钟文本。
 *
 * · 实时监控 —— 真实墙钟「11:28:05」；
 * · 数据回放 —— 数据集时基换算出的绝对时刻「03-05 10:38:22」。
 */
const dateText = computed(() => absoluteText.value.slice(0, 10))

const totalClockText = computed(() => {
  const dur = Number(data.value?.meta?.duration_ms ?? data.value?.scene?.duration_ms ?? 302000)
  const mm = String(Math.floor(dur / 60000)).padStart(2, '0')
  const ss = String(Math.floor((dur % 60000) / 1000)).padStart(2, '0')
  return `${mm}:${ss}`
})

const progressPct = computed(() => {
  const dur = Number(data.value?.meta?.duration_ms ?? data.value?.scene?.duration_ms ?? 302000)
  if (!dur) return 0
  return Math.max(0, Math.min(100, (store.time_ms / dur) * 100))
})

/* ------------------------------------------------------------------ *
 * 设备档案：接入信息（devices.json）× 装备信息（SENSOR_ARCHIVE）
 * ------------------------------------------------------------------ */

/** 设备类型 → 中文标签 */
const KIND_LABEL: Record<string, string> = {
  radar: '雷达',
  ais: 'AIS',
  sonar: '声纳',
  camera: '光电',
}

/**
 * 设备行数据。
 *
 * 装备参数从 `SENSOR_ARCHIVE` 取（型号/探测半径/扫描周期/方位），
 * 接入状态从 `devices.json` 取（status / state_label）。
 * 两边按 `source_id` 关联（devices.json 用 R01/A01/S01/E01，
 * 档案用 RADAR-01/AIS-01/SO-01/EO-01，通过 `sourceId` 字段映射）。
 */
const deviceRows = computed(() =>
  devices.value.map((d) => {
    const arch = archiveBySourceId(d.source_id)
    return {
      ...d,
      kindLabel: KIND_LABEL[d.kind] || d.kind || '设备',
      model: arch?.model || '—',
      elevationM: arch?.elevationM ?? '—',
      locText:
        d.lat != null
          ? `${d.lat.toFixed(4)}°N / ${d.lon.toFixed(4)}°E`
          : arch?.location || '—',
      capability: arch ? capabilityText(arch) : '—',
      arch,
    }
  }),
)

const activeDevices = computed(
  () => devices.value.filter((d) => d.status === 'active' || d.status === 'online').length,
)

/**
 * 当前选中设备的详情。
 *
 * 默认选中第一台「有装备档案」的设备 ——
 * 因为声纳 S01 在水面场景里没有贴图实体，
 * 默认选中它会让详情区显出一堆「—」，观感很差。
 */
const activeDevice = computed(() => {
  const rows = deviceRows.value
  if (!rows.length) return null
  const picked = rows.find((r) => r.source_id === activeDeviceId.value)
  if (picked) return picked
  return rows.find((r) => r.arch) ?? rows[0]
})

/**
 * 设备详情字段表。
 *
 * 「探测能力」「扫描周期」等字段直接取自装备档案，
 * 不做任何随机化 —— 页面显示的必须是场景真正在用的参数。
 */
const activeDeviceFields = computed(() => {
  const d = activeDevice.value
  if (!d) return []
  const a = d.arch
  const out: { k: string; v: string; cls?: string }[] = [
    { k: '设备类型', v: d.kindLabel },
    { k: '设备型号', v: d.model },
    { k: '接入状态', v: d.state_label || '—', cls: 'is-' + (d.status || 'unknown') },
  ]
  if (a) {
    out.push({ k: '部署位置', v: a.location })
    out.push({ k: '高程', v: `${a.elevationM} m` })
    out.push({ k: '探测半径', v: `${a.rangeM} m` })
    out.push({ k: '扫描周期', v: `${a.period} s` })
    out.push({ k: '工作方位', v: `${a.bearingDeg}°（0° 正北顺时针）` })
    if (a.fovDeg) out.push({ k: '视场角', v: `${a.fovDeg}°` })
  }
  /* 当前时刻该设备实际关联到的目标 */
  const linked = linkedTargets.value.filter((t) => (t.sources || []).includes(d.source_id))
  out.push({
    k: '当前关联目标',
    v: linked.length ? linked.map((t: any) => t.global_track_id).join('、') : '无',
    cls: linked.length ? 'is-ok' : 'is-idle',
  })
  return out
})

/* ------------------------------------------------------------------ *
 * 目标接入概览
 * ------------------------------------------------------------------ */

const linkedTargets = computed<any[]>(() => (snapshot.value?.targets || []) as any[])

/**
 * 当前时刻的链路健康度。
 *
 * 每一项都从**真实数据**推出（见 `sensor-archive.ts` 的 `linkHealth`）：
 *   · 探测链路 —— 落在雷达探测圈内的目标占比
 *   · 报文链路 —— AIS 当前上报数 / 历史峰值
 *   · 凝视链路 —— 光电是否有跟踪目标 + 距离余量
 *   · 声呐覆盖 —— 水下目标被覆盖的比例
 */
const linkHealthRows = computed(() => {
  const targets = linkedTargets.value
  const under = targets.filter((t) => t.domain === 'underwater')
  const surface = targets.filter((t) => (t.domain || 'surface') === 'surface')

  return SENSOR_ARCHIVE.map((a) => {
    /* 被该设备关联到的目标 */
    const linked = targets.filter(
      (t) => Array.isArray(t.sources) && t.sources.includes(a.sourceId),
    )
    /* 该设备历史上报过的最大目标数 ——
     * 用「该设备在**整个会话**里关联过的目标数量」近似，
     * 它代表这条链路**曾经**承载过的峰值，
     * 于是「当前上报数 / 峰值」= 链路利用率。 */
    const maxReported = Math.max(
      1,
      (data.value?.targets?.items as any[] || []).filter(
        (t: any) => Array.isArray(t.sources) && t.sources.includes(a.sourceId),
      ).length,
      linked.length,
    )

    /* 光电跟踪目标：与场景层 `SceneSensorsV2.eoTarget` 的选取规则一致 ——
     * 优先用户选中的目标，否则取**探测范围内**的第一个；
     * 若范围内一个都没有，场景层会退回到「最近的目标」继续跟，
     * 这里同样跟随，避免管理页显示 0% 而画面里明明锁着一艘船。 */
    let eoTracking = false
    let eoRangeM = Number.POSITIVE_INFINITY
    if (a.kind === 'eo') {
      const withPos = surface.filter(
        (t) => t.east_m != null && t.north_m != null,
      )
      const inRange = withPos.filter(
        (t) => Math.hypot(t.east_m - a.east, t.north_m - a.north) <= a.rangeM,
      )
      const pool = inRange.length ? inRange : withPos
      const sel = pool.find((t) => t.global_track_id === store.selectedTargetId)
      const picked = sel ?? pool[0]
      if (picked) {
        eoTracking = true
        eoRangeM = Math.hypot(picked.east_m - a.east, picked.north_m - a.north)
      }
    }

    /* 声纳覆盖：水下目标被声纳节点探测到的比例。
     *
     * 【量纲 —— 必须与场景层一致】
     * `SceneUnderwaterV2.sonarDetections` 判定覆盖用的是
     * **屏幕像素距离**与**波圈半径**（`waveRadius`）的比较，
     * 而不是 ENU 米制距离 ——
     * 因为声纳波圈在画面上是按底图比例画的，不是按真实比例。
     *
     * 这里直接复用 `devices.json` 里的 `sources` 关联来统计：
     * 被声纳关联的水下目标即「在覆盖圈内」的目标，
     * 与场景里画出的测向连线数量一致。 */
    const underCovered = under.filter(
      (t) => Array.isArray(t.sources) && t.sources.includes(a.sourceId),
    ).length

    const links = linkHealth(a, {
      linked: linked.map((t) => ({
        east: t.east_m ?? 0,
        north: t.north_m ?? 0,
        sources: t.sources || [],
      })),
      maxReported,
      eoTracking,
      eoRangeM: Number.isFinite(eoRangeM) ? eoRangeM : a.rangeM,
      underTotal: under.length,
      underCovered,
    })

    return { archive: a, links }
  })
})

/**
 * 选中设备的链路健康度。
 *
 * 【光电跟踪的判定】
 * 与 `linkHealthRows` 用同一套逻辑 ——
 * 「当前是否有目标进入光电探测范围」，
 * 与场景里 `SceneSensorsV2` 的 `eoTarget` 选取规则一致：
 * 优先用户选中的目标，否则取范围内最近的一个。
 */
const activeDeviceLinks = computed(() => {
  const d = activeDevice.value
  if (!d?.arch) return []
  const row = linkHealthRows.value.find((r) => r.archive.id === d.arch.id)
  return row?.links ?? []
})

/** 感知来源 → 中文 */
function sourceTextOf(sources: string[] | undefined): string {
  if (!sources || !sources.length) return '未关联'
  return sources
    .map((s) => (s === 'A01' ? 'AIS' : s === 'R01' ? '雷达' : s === 'S01' ? '声纳' : s === 'E01' ? '光电' : s))
    .join(' + ')
}

const targetRows = computed(() =>
  linkedTargets.value.map((t) => ({
    id: t.global_track_id,
    mmsi: t.mmsi || '—',
    domain: t.domain || 'surface',
    sourceText: sourceTextOf(t.sources),
    status: t.update_status || 'measured',
    statusText:
      t.update_status === 'predicted'
        ? '外推维持'
        : (t.sources || []).length > 1
          ? '多源融合'
          : '单源实测',
  })),
)

/* ------------------------------------------------------------------ *
 * 概览指标
 * ------------------------------------------------------------------ */

/**
 * 概览指标 —— 全部由当前快照**实时算出**，不写死。
 */
const metrics = computed(() => {
  const targets = (snapshot.value?.targets || []) as any[]
  const surface = targets.filter((t) => (t.domain || 'surface') === 'surface')
  const under = targets.filter((t) => t.domain === 'underwater')
  const linked = targets.filter((t) => (t.sources || []).includes('A01'))
  const online = devices.value.filter((d) => d.status === 'active' || d.status === 'online').length
  const fused = targets.filter((t) => (t.sources || []).length > 1).length
  return [
    { label: '接入设备', value: devices.value.length, unit: '台', cls: '' },
    { label: '设备在线', value: online, unit: '台', cls: online === devices.value.length ? 'is-ok' : 'is-warn' },
    { label: '水面目标', value: surface.length, unit: '艘', cls: '' },
    { label: '水下目标', value: under.length, unit: '个', cls: '' },
    { label: '多源融合', value: fused, unit: '个', cls: 'is-ok' },
    { label: 'AIS 关联', value: linked.length, unit: '个', cls: 'is-ok' },
  ]
})

/**
 * 链路健康度。
 *
 * 【语义】
 * 按「设备在线率 + 当前时刻的多源融合占比」综合出一个档位：
 *   good —— 全部设备在线且至少有一个多源融合目标；
 *   warn —— 有设备离线，或当前时刻还没有多源融合目标（数据链路尚未闭合）。
 *
 * 这个值不是装饰：它直接反映当前回放时刻的数据链路是否真的在跑。
 */
const health = computed(() => {
  const total = devices.value.length
  const online = activeDevices.value
  const fused = metrics.value[4].value
  if (!total) return { level: 'unknown', label: '等待数据' }
  if (online < total) return { level: 'warn', label: `链路降级 ${online}/${total}` }
  if (fused === 0) return { level: 'warn', label: '单源运行' }
  return { level: 'good', label: '链路正常' }
})

/* ------------------------------------------------------------------ *
 * 回放控制
 * ------------------------------------------------------------------ */

function togglePlay() {
  store.playing = !store.playing
}
/**
 * 回到起点。
 *
 * 实时监控下没有「起点」概念 —— 数据是滚动的，
 * 所以这时切到数据回放模式并回到 0，才是用户预期的动作。
 */
function restart() {
  if (store.timelineMode === 'live') {
    store.timelineMode = 'replay'
    store.direction = 1
  }
  store.setTime(0)
  store.playing = true
}

/** 跳到数据末尾（实时监控下即「贴到现在」）。 */
function jumpToEnd() {
  const dur = Number(data.value?.meta?.duration_ms ?? data.value?.scene?.duration_ms ?? 302000)
  store.setTime(dur)
  store.direction = 1
  store.playing = false
}

/* ------------------------------------------------------------------ *
 * 运行日志
 * ------------------------------------------------------------------ */

/**
 * 运行日志 —— 由当前回放时刻的数据**推导**，
 * 而不是随机生成，这样日志与画面始终自洽。
 */
const logRows = computed(() => {
  const rows: { id: string; time: string; level: string; levelText: string; msg: string }[] = []
  /**
   * 日志时间戳用**绝对时间**（HH:mm:ss）。
   *
   * 实时监控下日志里的「发生时刻」应该落在真实时间轴上，
   * 而不是数据集内的相对秒数 —— 后者在实时场景里没有意义。
   */
  const fmt = (ms: number) => formatClock(dataMsToEpoch(ms))
  const t = snapshot.value?.time_ms ?? store.time_ms

  rows.push({
    id: 'boot',
    time: fmt(0),
    level: 'info',
    levelText: 'INFO',
    msg: '平台启动完成，场景版本 ' + sceneVersion.value.toUpperCase() + ' 已加载',
  })
  rows.push({
    id: 'devices',
    time: fmt(0),
    level: 'info',
    levelText: 'INFO',
    msg: `感知设备接入 ${devices.value.length} 台，在线 ${metrics.value[1].value} 台`,
  })
  /* 场景版本切换也要留痕 —— 否则日志与界面显示的版本会不一致 */
  rows.push({
    id: 'scene-version',
    time: fmt(0),
    level: 'info',
    levelText: 'INFO',
    msg: `当前场景版本 ${sceneVersion.value.toUpperCase()}，素材基址 ${String(data.value?.meta?.coordinate_frame_id || 'site_enu')}`,
  })

  const targets = (snapshot.value?.targets || []) as any[]
  for (const tg of targets.slice(0, 4)) {
    const sources = sourceTextOf(tg.sources)
    rows.push({
      id: 'tg-' + tg.global_track_id,
      time: fmt(tg.time_ms ?? t),
      level: tg.update_status === 'predicted' ? 'warn' : 'info',
      levelText: tg.update_status === 'predicted' ? 'WARN' : 'INFO',
      msg: `目标 ${tg.global_track_id} 航迹更新（来源：${sources}）`,
    })
  }

  const events = (snapshot.value?.events || []) as any[]
  for (const ev of events.slice(-3)) {
    rows.push({
      id: 'ev-' + ev.event_id,
      time: fmt(ev.time_ms),
      level: ev.level === 'warning' ? 'warn' : 'info',
      levelText: ev.level === 'warning' ? 'WARN' : 'INFO',
      msg: `${ev.title} · ${ev.description}`,
    })
  }

  return rows.sort((a, b) => a.time.localeCompare(b.time)).slice(-12).reverse()
})
</script>

<style scoped lang="scss">
.ws-sysmgmt {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: 14px;
  /*
   * 【留白】
   *
   * 「系统管理」是**整页独占**的工作台 ——
   * 左右侧栏、底部操作条与时间进度条在这一页都不出现，
   * 所以内容可以铺满整个宽度。
   *
   * · 左右：16px（页面边距）
   * · 上：16px（world 容器已从屏幕 y=105 开始，
   *        与场景页的侧栏顶部对齐）
   * · 下：16px（原先留给操作栏 + 时间条的 63px 不再需要）
   */
  padding: 16px;
  box-sizing: border-box;
  overflow: hidden;
  background:
    radial-gradient(1200px 620px at 22% 8%, rgba(0, 138, 200, 0.16), transparent 62%),
    radial-gradient(900px 520px at 82% 92%, rgba(0, 210, 190, 0.1), transparent 60%),
    linear-gradient(180deg, #031626 0%, #020d19 100%);
  font-family: var(--ws-font);
  color: var(--ws-text);
}

/* ============ 页头 ============ */
.ws-sysmgmt-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 0 4px 12px;
  border-bottom: 1px solid rgba(45, 185, 227, 0.28);
}

.ws-sysmgmt-head-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ws-sysmgmt-head h2 {
  margin: 0;
  font-size: 22px;
  line-height: 1.25;
  font-weight: 600;
  letter-spacing: 4px;
  text-shadow: 0 0 14px rgba(3, 168, 255, 0.55);
}

.ws-sysmgmt-head p {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.4;
  letter-spacing: 1px;
  color: #7fb3c9;
}

.ws-sysmgmt-head-meta {
  display: flex;
  align-items: center;
  gap: 14px;
}

.ws-sysmgmt-head-meta .ws-tag.is-warn {
  border-color: #ffcf5a;
  background: rgba(66, 51, 8, 0.6);
  color: #ffcf5a;
}

.ws-sysmgmt-clock {
  font-size: 15px;
  color: #8fd8e6;
  letter-spacing: 1px;
}

/* ============ 概览指标 ============ */
.ws-sysmgmt-metrics {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
}

.ws-sysmgmt-metric {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 13px 16px;
  border: 1px solid rgba(24, 134, 177, 0.55);
  border-radius: 8px;
  background: linear-gradient(160deg, rgba(7, 63, 99, 0.85), rgba(4, 28, 51, 0.85));
  box-shadow: inset 0 0 20px rgba(0, 117, 181, 0.16);
  overflow: hidden;
}

.ws-sysmgmt-metric::after {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  width: 34px;
  height: 2px;
  background: #23e8ff;
  box-shadow: 0 0 10px #23e8ff88;
}

.ws-sysmgmt-metric-label {
  font-size: 14px;
  color: #8dbdd0;
  letter-spacing: 1px;
  margin-right: auto;
}

.ws-sysmgmt-metric-value {
  font-size: 26px;
  font-weight: 600;
  line-height: 1;
  color: #d6f7ff;
  text-shadow: 0 0 12px rgba(60, 220, 255, 0.4);
}

.ws-sysmgmt-metric-value.is-ok {
  color: #6ff0b8;
  text-shadow: 0 0 12px rgba(80, 240, 180, 0.4);
}

.ws-sysmgmt-metric-value.is-warn {
  color: #ffcf5a;
  text-shadow: 0 0 12px rgba(255, 207, 90, 0.4);
}

.ws-sysmgmt-metric-unit {
  font-size: 13px;
  color: #6f9bb0;
}

/* ============ 主体两栏 ============ */
.ws-sysmgmt-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: 14px;
}

.ws-sysmgmt-col {
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.ws-sysmgmt-col :deep(.ws-panel-title) {
  font-size: 20px;
  line-height: 1.4;
  letter-spacing: 1px;
}

.ws-sysmgmt-col :deep(.ws-panel-title small) {
  font-size: 13.5px;
  line-height: 1.4;
  font-weight: 400;
  color: #7fb3c9;
  margin-left: 10px;
  letter-spacing: 0;
}

.ws-sysmgmt-col :deep(.ws-panel-content) {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding-top: 6px;
}

/* ============ 设备表 ============ */
.ws-sysmgmt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 15px;
}

.ws-sysmgmt-table th {
  text-align: left;
  padding: 9px 10px;
  font-weight: 500;
  color: #7fb3c9;
  border-bottom: 1px solid rgba(45, 185, 227, 0.28);
  white-space: nowrap;
}

.ws-sysmgmt-table td {
  padding: 8px 10px;
  border-bottom: 1px solid rgba(21, 81, 113, 0.4);
  color: #cfeaf5;
  vertical-align: middle;
}

.ws-sysmgmt-table td.is-strong {
  color: #6ff0ff;
  font-weight: 600;
  letter-spacing: 1px;
}

/* 设备行可点击 —— 点击切换右侧详情 */
.ws-sysmgmt-devrow {
  cursor: pointer;
  transition: background 0.18s;
}

.ws-sysmgmt-devrow:hover td {
  background: rgba(0, 200, 230, 0.06);
}

.ws-sysmgmt-devrow.is-active td {
  background: rgba(35, 232, 255, 0.1);
}

.ws-sysmgmt-devrow.is-active td.is-strong {
  text-shadow: 0 0 10px rgba(111, 240, 255, 0.6);
}

/* 类型 / 型号：两行排布 */
.ws-sysmgmt-devkind {
  display: block;
  font-size: 15px;
  color: #d6f7ff;
}

.ws-sysmgmt-devmodel {
  display: block;
  font-size: 12.5px;
  color: #7fb3c9;
  margin-top: 2px;
}

.ws-sysmgmt-devloc {
  display: block;
  font-size: 13.5px;
  color: #cfeaf5;
  font-variant-numeric: tabular-nums;
}

.ws-sysmgmt-elev {
  display: block;
  font-size: 12.5px;
  color: #6f9bb0;
  margin-top: 2px;
}

.ws-sysmgmt-devcap {
  font-size: 13.5px;
  color: #8fd8e6;
  white-space: nowrap;
}

.ws-sysmgmt-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.ws-sysmgmt-state i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #6f9bb0;
}

.ws-sysmgmt-state.is-online i,
.ws-sysmgmt-state.is-normal i,
.ws-sysmgmt-state.is-active i {
  background: #4fe89a;
  box-shadow: 0 0 8px #4fe89a;
}

.ws-sysmgmt-state.is-warning i,
.ws-sysmgmt-state.is-degraded i {
  background: #ffcf5a;
  box-shadow: 0 0 8px #ffcf5a;
}

.ws-sysmgmt-state.is-offline i,
.ws-sysmgmt-state.is-error i {
  background: #ff7a7a;
  box-shadow: 0 0 8px #ff7a7a;
}

.ws-sysmgmt-empty {
  padding: 24px 10px;
  text-align: center;
  color: #6f9bb0;
  font-size: 14px;
}

/* ============ 设备详情 ============ */
.ws-sysmgmt-detail {
  margin-top: 16px;
  padding-top: 4px;
  border-top: 1px dashed rgba(45, 185, 227, 0.25);
}

.ws-sysmgmt-detail-state {
  float: right;
  font-size: 13px;
  font-weight: 400;
  color: #6f9bb0;
  letter-spacing: 0;
}

.ws-sysmgmt-detailgrid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.ws-sysmgmt-detailitem {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border: 1px solid rgba(21, 81, 113, 0.5);
  border-radius: 6px;
  background: rgba(3, 26, 46, 0.6);
}

.ws-sysmgmt-detailkey {
  font-size: 12.5px;
  color: #7fb3c9;
  letter-spacing: 0.5px;
}

.ws-sysmgmt-detailval {
  font-size: 15px;
  color: #d6f7ff;
  font-variant-numeric: tabular-nums;
}

.ws-sysmgmt-detailval.is-ok {
  color: #6ff0b8;
}

.ws-sysmgmt-detailval.is-idle {
  color: #6f9bb0;
}

.ws-sysmgmt-detailval.is-active,
.ws-sysmgmt-detailval.is-online {
  color: #6ff0b8;
}

/* ============ 链路健康条 ============ */
.ws-sysmgmt-links {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.ws-sysmgmt-link {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13.5px;
  color: #a8d4e4;
}

.ws-sysmgmt-linkname {
  flex: none;
  width: 118px;
  color: #8dbdd0;
}

.ws-sysmgmt-linkbar {
  flex: 1;
  height: 7px;
  border-radius: 4px;
  background: rgba(15, 55, 80, 0.8);
  overflow: hidden;
  border: 1px solid rgba(21, 81, 113, 0.6);
}

.ws-sysmgmt-linkbar i {
  display: block;
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, #1f8ea8, #4fe89a);
  transition: width 0.4s ease;
}

.ws-sysmgmt-link.is-warn .ws-sysmgmt-linkbar i {
  background: linear-gradient(90deg, #b8842a, #ffcf5a);
}

.ws-sysmgmt-link.is-idle .ws-sysmgmt-linkbar i {
  background: #2b4152;
}

.ws-sysmgmt-linkval {
  flex: none;
  width: 42px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: #cfeaf5;
}

/* ============ 配置 ============ */
.ws-sysmgmt-config {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 18px;
}

.ws-sysmgmt-field {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  font-size: 15px;
  color: #a8d4e4;
}

.ws-sysmgmt-select {
  min-width: 240px;
  padding: 7px 10px;
  font: inherit;
  font-size: 14px;
  color: #d6f7ff;
  background: #062840;
  border: 1px solid #1886b1;
  border-radius: 5px;
  cursor: pointer;
}

.ws-sysmgmt-select:focus-visible {
  outline: none;
  border-color: #29f2f2;
  box-shadow: 0 0 10px #10d7ed55;
}

.ws-sysmgmt-sub {
  margin: 16px 0 10px;
  font-size: 15px;
  font-weight: 500;
  letter-spacing: 1px;
  color: #8fd8e6;
  padding-left: 9px;
  border-left: 2px solid #23e8ff;
  clear: both;
}

.ws-sysmgmt-switch {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 2px;
  font-size: 15px;
  color: #cfeaf5;
  cursor: pointer;
}

.ws-sysmgmt-switch input {
  width: 17px;
  height: 17px;
  accent-color: #23e8ff;
  cursor: pointer;
}

/* ============ 时间轴控制 ============ */
.ws-sysmgmt-timemodes {
  display: inline-flex;
  border: 1px solid #1886b1;
  border-radius: 5px;
  overflow: hidden;
  margin-bottom: 10px;
}
.ws-sysmgmt-timemode {
  cursor: pointer;
  border: 0;
  background: #06283c;
  color: #8fb9cc;
  font-size: 15px;
  padding: 7px 16px;
  letter-spacing: 1px;
  transition: all 0.18s;
}
.ws-sysmgmt-timemode + .ws-sysmgmt-timemode { border-left: 1px solid #1886b1; }
.ws-sysmgmt-timemode:hover { color: #d9f8ff; background: #0a3a55; }
.ws-sysmgmt-timemode.is-on {
  color: #04141f;
  background: linear-gradient(#22e4ee, #17b7c9);
  font-weight: 600;
}

/* 实时时钟卡片 */
.ws-sysmgmt-clockbox {
  margin-top: 12px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  background: linear-gradient(90deg, rgba(7, 101, 158, 0.35), rgba(4, 30, 55, 0.2));
  border: 1px solid rgba(45, 185, 227, 0.3);
  border-left: 3px solid #22e4ee;
  border-radius: 5px;
}
.ws-sysmgmt-clockmain { display: flex; align-items: baseline; gap: 12px; }
.ws-sysmgmt-clock {
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 32px;
  font-weight: 700;
  letter-spacing: 2px;
  color: #7ff6ff;
  text-shadow: 0 0 12px rgba(0, 234, 255, 0.55);
  line-height: 1.1;
}
.ws-sysmgmt-clockdate {
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 15px;
  color: #7fb3c9;
}
.ws-sysmgmt-clockmeta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ws-sysmgmt-clockmeta .ws-tag.is-live {
  color: #04141f;
  background: linear-gradient(#22e4ee, #17b7c9);
  border-color: #22e4ee;
  font-weight: 600;
}
.ws-sysmgmt-clockmeta .ws-tag.is-replay {
  color: #ffb547;
  background: rgba(255, 181, 71, 0.14);
  border-color: rgba(255, 181, 71, 0.5);
}
.ws-sysmgmt-clocknote { font-size: 13px; color: #7fb3c9; }

.ws-sysmgmt-playrow {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.ws-sysmgmt-rev.is-on {
  color: #04141f;
  background: linear-gradient(#ffb547, #e09a2c);
  border-color: #ffb547;
  font-weight: 600;
}

.ws-sysmgmt-playrow .ws-button {
  font-size: 15px;
  padding: 7px 14px;
}

.ws-sysmgmt-progress {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
}

.ws-sysmgmt-progressbar {
  flex: 1;
  height: 8px;
  border-radius: 4px;
  background: rgba(15, 55, 80, 0.8);
  border: 1px solid rgba(21, 81, 113, 0.6);
  overflow: hidden;
}

.ws-sysmgmt-progressbar i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #1886b1, #23e8ff);
  box-shadow: 0 0 10px rgba(35, 232, 255, 0.55);
  transition: width 0.2s linear;
}

.ws-sysmgmt-progresstext {
  flex: none;
  font-size: 13px;
  color: #7fb3c9;
  font-variant-numeric: tabular-nums;
}

.ws-sysmgmt-source {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.ws-sysmgmt-source-note {
  font-size: 13px;
  color: #7fb3c9;
}

/* ============ 目标接入概览 ============ */
.ws-sysmgmt-targets {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.ws-sysmgmt-trow {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 5px 8px;
  border-radius: 5px;
  background: rgba(3, 26, 46, 0.55);
  font-size: 13.5px;
}

.ws-sysmgmt-tid {
  flex: none;
  width: 48px;
  font-weight: 600;
  color: #6ff0ff;
}

.ws-sysmgmt-tid.is-underwater {
  color: #ffd54a;
}

.ws-sysmgmt-tmmsi {
  flex: none;
  width: 96px;
  color: #7fb3c9;
  font-variant-numeric: tabular-nums;
}

.ws-sysmgmt-tsrc {
  flex: 1;
  color: #a8d4e4;
}

.ws-sysmgmt-tstatus {
  flex: none;
  font-size: 12.5px;
  padding: 1px 8px;
  border-radius: 3px;
  border: 1px solid rgba(79, 232, 154, 0.4);
  color: #6ff0b8;
}

.ws-sysmgmt-tstatus.is-predicted {
  border-color: rgba(255, 207, 90, 0.5);
  color: #ffcf5a;
}

/* ============ 日志 ============ */
.ws-sysmgmt-log {
  flex: 0 0 210px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.ws-sysmgmt-log :deep(.ws-panel-title) {
  font-size: 18px;
  letter-spacing: 1px;
}

.ws-sysmgmt-log :deep(.ws-panel-title small) {
  font-size: 12px;
  font-weight: 400;
  color: #6f9bb0;
  margin-left: 10px;
}

.ws-sysmgmt-log :deep(.ws-panel-content) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.ws-sysmgmt-loglist {
  list-style: none;
  margin: 0;
  padding: 0;
  font-size: 14px;
}

.ws-sysmgmt-loglist li {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding: 6px 2px;
  border-bottom: 1px solid rgba(21, 81, 113, 0.28);
  color: #b8d9e7;
}

.ws-sysmgmt-log-time {
  font-variant-numeric: tabular-nums;
  color: #6f9bb0;
  min-width: 44px;
}

.ws-sysmgmt-log-level {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 1px;
  padding: 1px 7px;
  border-radius: 3px;
  min-width: 42px;
  text-align: center;
  color: #6ff0b8;
  border: 1px solid rgba(79, 232, 154, 0.45);
  background: rgba(4, 60, 42, 0.6);
}

.ws-sysmgmt-log-level.is-warn {
  color: #ffcf5a;
  border-color: rgba(255, 207, 90, 0.5);
  background: rgba(66, 51, 8, 0.6);
}

.ws-sysmgmt-log-msg {
  flex: 1;
}
</style>
