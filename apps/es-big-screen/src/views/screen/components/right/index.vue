<template>
    <section class="ws-panel">
      <h2 class="ws-panel-title">目标详情</h2>
      <div id="details" class="ws-panel-content">
        <div v-if="!snapshot" class="empty-state">加载中…</div>
        <template v-else-if="current">
          <table class="details-table"><tbody>
            <tr><td>统一编号</td><td class="strong">{{ current.global_track_id }}</td></tr>
            <tr><td>MMSI</td><td>{{ current.mmsi || '—' }}</td></tr>
            <tr><td>目标类型</td><td>船舶 · {{ current.association_status === 'confirmed' ? '雷达/AIS 已关联' : '雷达单源' }}</td></tr>
            <tr><td>数据来源</td><td>{{ sourceLabel(current) }}</td></tr>
            <tr><td>经纬度</td><td>{{ fmtLatLon(current) }}</td></tr>
            <tr><td>站心坐标</td><td>东 {{ current.east_m?.toFixed(1) }}米 / 北 {{ current.north_m?.toFixed(1) }}米</td></tr>
            <tr><td>距离/方位</td><td>{{ current.range_m?.toFixed(1) ?? '—' }}米 / {{ current.bearing_deg?.toFixed(2) ?? '—' }}°</td></tr>
            <tr><td>速度/艏向</td><td>{{ current.speed_mps?.toFixed(2) ?? '—' }}米/秒 / {{ current.course_deg != null ? current.course_deg.toFixed(1) + '°' : '—' }}</td></tr>
            <tr><td>观测更新</td><td>{{ current.utc || '—' }}</td></tr>
          </tbody></table>
          <!-- 数据可信度：把「这条信息来自哪、现在是什么状态」讲清楚。
               融合结果 / 雷达单测 / AIS 快照 / 预测维持，用不同标记区分，
               避免把预测值当成实测值来看。 -->
          <div class="credibility" :class="credibilityClass">
            <span class="credibility-badge">{{ credibilityLabel }}</span>
            <span class="credibility-desc">{{ credibilityDesc }}</span>
          </div>
          <div class="detail-actions">
            <button class="ws-button" data-action="locate" @click="store.follow = true"><img src="/water-security/assets/icons/location.svg" alt="" />定位目标</button>
            <button class="ws-button" data-action="track" @click="store.layers.fused_tracks = true"><img src="/water-security/assets/icons/track.svg" alt="" />查看轨迹</button>
          </div>
        </template>
        <div v-else class="empty-state">当前回放时刻没有该目标。</div>
      </div>
    </section>
    <section class="ws-panel sonar-panel">
      <h2 class="ws-panel-title">量测信息 · R01</h2>
      <div class="ws-panel-content">
        <div v-if="!current" class="empty-state">当前回放时刻没有该目标。</div>
        <template v-else>
          <table class="details-table"><tbody>
            <tr><td>量测种类</td><td>{{ kindLabel(current) }}</td></tr>
            <tr><td>距离/方位</td><td>{{ current.range_m?.toFixed(1) ?? '—' }}米 / {{ current.bearing_deg?.toFixed(2) ?? '—' }}°</td></tr>
            <tr><td>平台航向</td><td>固定岸基站，站心坐标换算不含平台航向</td></tr>
            <tr><td>数据时刻</td><td>{{ current.utc || '' }}</td></tr>
          </tbody></table>
          <div class="frame-meta"><span>站址 北纬37.53297°，东经121.4199°</span><span>真北方位基准</span></div>
        </template>
      </div>
    </section>
    <section class="ws-panel sensor-panel">
      <h2 class="ws-panel-title">传感器状态</h2>
      <div class="ws-panel-content">
        <!--
          三类传感器的工作状态。

          【为什么放在右侧面板而不是地图上】
          地图上已经有传感器的动效（扫描扇面 / 接收脉冲 / 视场锥），
          那是「空间位置」的表达；
          这里给出的是「工作状态」的表达 ——
          是否在线、当前在探测什么、覆盖率多少。
          两层信息互补，不重复。
        -->
        <div v-for="s in sensorStatus" :key="s.id" class="sensor-row" :class="'sensor-row--' + s.kind">
          <span class="sensor-dot" />
          <div class="sensor-main">
            <div class="sensor-head">
              <strong>{{ s.name }}</strong>
              <span class="sensor-badge">{{ s.kindLabel }}</span>
            </div>
            <div class="sensor-meta">{{ s.meta }}</div>
          </div>
        </div>
        <div class="frame-meta">
          <span>传感器位置为场景示意</span>
          <span>未与实测 ENU 配准</span>
        </div>
      </div>
    </section>
    <section class="ws-panel events-panel">
      <h2 class="ws-panel-title">事件提示</h2>
      <div id="events" class="ws-panel-content">
        <button v-for="e in lastEvents" :key="e.event_id" class="event-row" :class="{ warning: e.level === 'warning' }" :data-event="e.event_id" @click="jumpEvent(e)">
          <span class="event-time">{{ formatTime(e.time_ms) }}</span>
          <strong>{{ e.title }}</strong>
          <small>{{ e.description }}</small>
        </button>
        <div v-if="!lastEvents.length" class="empty-state">暂无事件</div>
      </div>
    </section>
</template>

<script setup lang='ts'>
import { computed } from 'vue'
import { useWaterDemo } from '@/water-security/useWaterDemo'
const { store, snapshot, seek, formatTime } = useWaterDemo()
const current = computed(() => snapshot.value?.targets?.find((t: any) => t.global_track_id === store.selectedTargetId))
const lastEvents = computed(() => (snapshot.value?.events || []).slice(-2).reverse())
const nameMap: Record<string, string> = { R01: '雷达', A01: 'AIS', S01: '声纳', E01: '光电' }

/**
 * 三类传感器的状态摘要。
 *
 * 【状态是「真实算出来」的，不是写死的】
 * · 雷达：统计当前**在雷达单源目标**（`kind === 'radar_detected'`）的数量；
 * · AIS ：统计当前**有 AIS 关联**（`sources` 含 `A01`）的目标数量；
 * · 光电：统计当前**被选中/高亮关注**的目标（选中目标落在光电监视方向）。
 *
 * 这样传感器面板与目标面板的数据是自洽的 ——
 * 面板上的数字直接来自当前回放时刻的真实目标集合，
 * 不会和地图上的目标对不上。
 */
const sensorStatus = computed(() => {
  const targets = snapshot.value?.targets || []
  const radarOnly = targets.filter((t: any) => t.kind === 'radar_detected')
  const aisLinked = targets.filter((t: any) => (t.sources || []).includes('A01'))
  const focused = current.value

  return [
    {
      id: 'RADAR-01',
      kind: 'radar',
      kindLabel: '雷达',
      name: '岸基雷达站',
      meta: radarOnly.length
        ? `主动扫描中 · 独立探测 ${radarOnly.length} 个目标`
        : '主动扫描中 · 暂无独立探测目标',
    },
    {
      id: 'AIS-01',
      kind: 'ais',
      kindLabel: 'AIS',
      name: 'AIS 岸台',
      meta: aisLinked.length
        ? `被动接收中 · 已关联 ${aisLinked.length} 个目标`
        : '被动接收中 · 暂无关联目标',
    },
    {
      id: 'EO-01',
      kind: 'eo',
      kindLabel: '光电',
      name: '光电转塔',
      meta: focused
        ? `云台跟踪中 · 当前锁定 ${focused.global_track_id}`
        : '云台巡航中 · 未指定跟踪目标',
    },
  ]
})
function sourceLabel(t: any) { return (t.sources || []).map((s: string) => nameMap[s] || s).join('、') }
function fmtLatLon(t: any) {
  const lat = t.lat ?? t.latitude_deg
  const lon = t.lon ?? t.longitude_deg
  if (lat == null || lon == null) return '—'
  return `北纬${Number(lat).toFixed(5)}°，东经${Number(lon).toFixed(5)}°`
}
function jumpEvent(e: any) {
  store.playing = false
  seek(e.time_ms)
  if (e.target_id) store.select(e.target_id)
}

/**
 * 量测种类 → 中文说明。
 *
 * 【为什么要区分】
 * 「雷达独立检测」和「AIS 快照关联」的可信度不一样：
 * 前者是本站雷达单独发现（可能有杂波、航迹未确认）；
 * 后者是 AIS 身份已确认（位置可信，但更新周期取决于目标上报）。
 * 界面上不区分，用户会把两者当成同等可信的实测。
 */
function kindLabel(t: any): string {
  switch (t.kind) {
    case 'radar_detected': return '雷达独立检测（未关联 AIS）'
    case 'ais_associated': return 'AIS 快照关联'
    case 'fused': return '多源融合航迹'
    case 'predicted': return '预测位置（外推）'
    default: return t.kind || '—'
  }
}

/**
 * 数据可信度标记。
 *
 * 【判定顺序】
 * 1. 预测状态（`update_status === 'predicted'`）：位置是外推出来的，
 *    不是新观测 —— 风险最高，单独提示。
 * 2. 融合结果（多源）：两个以上来源交叉验证，可信度高。
 * 3. 单源实测：有新观测，但来源单一。
 * 4. 其余：未知状态。
 */
const credibility = computed(() => {
  const t = current.value
  if (!t) return { label: '—', desc: '无目标数据', cls: 'is-unknown' }
  const status = t.update_status || t.kind || ''
  const sources: string[] = t.sources || []
  if (status === 'predicted' || t.predicted === true) {
    return { label: '预测维持', desc: '暂无新观测，位置为航迹外推值', cls: 'is-predicted' }
  }
  if (sources.length >= 2) {
    return { label: '多源融合', desc: `雷达与 AIS 交叉验证，观测更新于 ${t.utc || '—'}`, cls: 'is-fused' }
  }
  if (sources.length === 1) {
    return { label: '单源实测', desc: `仅 ${nameMap[sources[0]] || sources[0]} 单源观测，观测更新于 ${t.utc || '—'}`, cls: 'is-single' }
  }
  return { label: '状态未知', desc: '未标注观测来源', cls: 'is-unknown' }
})
const credibilityLabel = computed(() => credibility.value.label)
const credibilityDesc = computed(() => credibility.value.desc)
const credibilityClass = computed(() => credibility.value.cls)
</script>
