<template>
    <section class="ws-panel">
      <h2 class="ws-panel-title">感知设备</h2>
      <div id="devices" class="ws-panel-content">
        <div v-if="error" class="empty-state">素材加载失败：{{ error }}</div>
        <div v-else-if="!data" class="empty-state">加载中…</div>
        <div v-else v-for="d in data.devices.items" :key="d.source_id" class="device-row">
          <img :src="icon(d.kind)" :alt="d.label" />
          <span>{{ d.label }}</span>
          <span class="ws-tag">{{ d.state_label }}</span>
        </div>
      </div>
    </section>
    <section class="ws-panel">
      <h2 class="ws-panel-title">目标列表<small>{{ snapshot?.targets?.length ?? 0 }} 个实测目标</small></h2>
      <div id="targets" class="ws-panel-content">
        <button
          v-for="t in snapshot?.targets || []"
          :key="t.global_track_id"
          class="target-row"
          :class="{ selected: store.selectedTargetId === t.global_track_id }"
          :data-target="t.global_track_id"
          :aria-pressed="store.selectedTargetId === t.global_track_id"
          @click="store.select(t.global_track_id)"
        >
          <img src="/water-security/assets/icons/ship.svg" alt="" />
          <span>{{ t.global_track_id }}<em class="mmsi">{{ t.mmsi }}</em></span>
          <small>{{ t.speed_mps?.toFixed?.(1) ?? '-' }} 米/秒 · {{ t.course_deg != null ? t.course_deg.toFixed(0) + '°' : '—' }}</small>
          <span class="arrow">›</span>
        </button>
        <div v-if="!(snapshot?.targets?.length)" class="empty-state">当前时刻无目标</div>
      </div>
    </section>
    <section class="ws-panel layer-panel">
      <h2 class="ws-panel-title">监视图层</h2>
      <div class="ws-panel-content">
        <label class="layer-row"><span><img src="/water-security/assets/icons/layers.svg" alt="" />雷达覆盖</span><input id="coverage" v-model="store.layers.coverage" type="checkbox" /></label>
        <label class="layer-row"><span><img src="/water-security/assets/icons/track.svg" alt="" />源点迹</span><input id="source_tracks" v-model="store.layers.source_tracks" type="checkbox" /></label>
        <label class="layer-row"><span><img src="/water-security/assets/icons/water.svg" alt="" />融合航迹</span><input id="fused_tracks" v-model="store.layers.fused_tracks" type="checkbox" /></label>
        <div class="compass-row">
          <img src="/water-security/assets/ui/compass.svg" alt="场景指北示意" />
          <div><div class="scale-line"></div><span>0　0.5公里　1.0公里</span></div>
        </div>
      </div>
    </section>
</template>

<script setup lang='ts'>
import { useWaterDemo } from '@/water-security/useWaterDemo'
const { store, data, snapshot, error } = useWaterDemo()
/* 设备类型 → 图标。
 * 光电的类型标记有两种写法：`devices.json` 里是 "eo"，
 * 而旧版数据里曾写成 "camera" —— 两者都指向 camera.svg。 */
const icon = (kind: string) => `/water-security/assets/icons/${kind === 'eo' || kind === 'camera' ? 'camera' : kind === 'ais' ? 'ais' : kind === 'sonar' ? 'sonar' : kind === 'radar' ? 'radar' : 'layers'}.svg`
</script>
