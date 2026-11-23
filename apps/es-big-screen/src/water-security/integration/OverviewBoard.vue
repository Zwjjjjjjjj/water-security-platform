<script setup lang="ts">
/**
 * 综合态势 —— 四模块工作总览。
 *
 * 【布局】
 * 四块等大、等独立的模块卡片：
 *   左上 数据标准化   右上 时空对齐
 *   左下 航迹关联     右下 目标识别
 *
 * 每块保持「标题与状态 — 核心图表 — 关键统计」三层结构，
 * 各有自己的「查看详情」入口。
 *
 * 【交互】
 * 1. 「查看详情」→ 该模块放大铺满（其余隐藏），可返回四宫格。
 * 2. 四个模块跟随**同一回放时刻**（props.timeMs）。
 *    当前时刻无对应数据的模块明确显示「暂无数据」。
 * 3. 模块可独立选择数据源。
 * 4. 联动高亮：点击任一模块里的目标，
 *    其余模块同步突出与之相关的记录、航迹和识别帧；
 *    不同对象之间**不强制关联**（各自只高亮能确定对应关系的部分）。
 */
import { computed, ref, watch } from 'vue'
import ModuleStd from './modules/ModuleStd.vue'
import ModuleAlign from './modules/ModuleAlign.vue'
import ModuleAssoc from './modules/ModuleAssoc.vue'
import ModuleRecog from './modules/ModuleRecog.vue'
import { buildPipeline, KIND_LABEL, type PipelineOutput } from './pipeline'
import { KIND_COLOR } from './chart-utils'

/** 顶部感知源按钮图标（与 ModuleStd 保持一致） */
const SENSOR_ICONS: Record<string, string> = {
  radar:
    '<circle cx="12" cy="12" r="2.4" fill="currentColor"/>' +
    '<path d="M7.4 12a4.6 4.6 0 0 1 9.2 0" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M4.6 12a7.4 7.4 0 0 1 14.8 0" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  ais:
    '<path d="M12 20V8" stroke="currentColor" stroke-width="1.7" fill="none"/>' +
    '<path d="M7 20h10" stroke="currentColor" stroke-width="1.7"/>' +
    '<circle cx="12" cy="6" r="2" fill="currentColor"/>' +
    '<path d="M6.2 9.2a7.4 7.4 0 0 1 11.6 0" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.7"/>',
  sonar:
    '<path d="M4 8.5c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<path d="M4 13c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.7" opacity="0.75"/>' +
    '<path d="M4 17.5c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.7" opacity="0.5"/>',
  eo:
    '<rect x="2.5" y="6.5" width="14" height="11" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<path d="M16.5 11.2l4.2-2.6v6.8l-4.2-2.6z" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<circle cx="8" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.6"/>',
}

const props = defineProps<{
  /** 原始全量数据 */
  data: any
  /** 当前回放时刻（毫秒） */
  timeMs: number
  /** 会话总时长 */
  durationMs: number
  /** 资源基地址 */
  assetBase: string
  /** 当前选中的目标 */
  selectedTargetId: string | null
}>()

const emit = defineEmits<{
  (e: 'select-target', id: string | null): void
}>()

/** 放大中的模块 id（null = 四宫格） */
const zoomed = ref<string | null>(null)

/** 样例回放场景（切换数据集） */
const sceneKey = ref<'a' | 'b'>('a')

/** 各模块独立的数据源筛选 */
const srcFilter = ref<Record<string, string | null>>({
  std: null,
  align: null,
  assoc: null,
  recog: null,
})

/** 识别模块选中的帧 */
const frameId = ref<string | null>(null)

/** 标准化模块选中的记录（驱动「原始报文 ↔ 标准记录」对照） */
const selectedRecordId = ref<string | null>(null)

/** 顶部感知源按钮（效果图上的四个） */
const SENSOR_KINDS = ['radar', 'ais', 'sonar', 'eo'] as const

/** 某类传感器对应的设备编号（取第一个） */
function sensorIdOf(kind: string): string | null {
  const rows = pipe.value?.std.bySource ?? []
  return rows.find((r) => r.kind === kind)?.sourceId ?? null
}

/** 点击顶部源按钮 → 切换标准化模块的数据源筛选 */
function toggleSensor(kind: string) {
  const id = sensorIdOf(kind)
  srcFilter.value.std = srcFilter.value.std === id ? null : id
}

/**
 * 四模块输出。
 *
 * 整棵管道很重（要遍历全部航迹样本），
 * 所以只在「数据」或「时刻」真的变了才重算。
 * 选中状态变化不影响管道结果，因此不进依赖。
 */
const pipe = computed<PipelineOutput | null>(() => {
  if (!props.data) return null
  return buildPipeline(props.data, props.timeMs)
})

/** 当前是否所有模块都无数据 */
const allEmpty = computed(() => {
  const p = pipe.value
  if (!p) return true
  return !p.std.hasData && !p.align.hasData && !p.assoc.hasData && !p.recog.hasData
})

/** 目标列表（供顶部选择器） */
const targetOptions = computed(() => {
  const p = pipe.value
  if (!p) return []
  const seen = new Map<string, string>()
  for (const t of p.assoc.tracks) seen.set(t.globalId, t.domain === 'underwater' ? '水下' : '水面')
  return [...seen.entries()].map(([id, domain]) => ({ id, domain }))
})

/** 识别模块当前帧 —— 受 frameId 控制（点击时间轴可跳转） */
const recogModule = computed(() => {
  const p = pipe.value
  if (!p) return null
  if (!frameId.value) return p.recog
  const f = p.recog.recent.find((r) => r.frameId === frameId.value)
  if (!f) return p.recog
  return { ...p.recog, current: f, results: f.detections }
})

/** 切换放大 */
function zoom(id: string) {
  zoomed.value = zoomed.value === id ? null : id
}

/** 返回四宫格 */
function backToGrid() {
  zoomed.value = null
}

/** 选中目标 —— 通知外部，同时联动 */
function selectTarget(id: string | null) {
  emit('select-target', id)
}

/** 选中记录 —— 标准化模块里的记录点击 */
function selectRecord(id: string) {
  /* 记录 id 形如 T001-R01-3000 —— 前缀是目标编号 */
  const m = /^([A-Z0-9]+)-/.exec(id)
  if (m && m[1] !== props.selectedTargetId) selectTarget(m[1])
  selectedRecordId.value = id
}

watch(
  () => props.timeMs,
  () => {
    /* 时间推进时，跟随回放的帧自动更新（用户手动选帧后则保持） */
    frameId.value = null
    /* 记录选中态也随时间失效，避免对照面板显示过期报文 */
    selectedRecordId.value = null
  },
)
</script>

<template>
  <div class="ws-ov">
    <!-- 顶部工具条 -->
    <header class="ws-ov-bar">
      <div class="ws-ov-brand">
        <span class="ws-ov-logo">◈</span>
        多源数据处理总览
      </div>

      <!-- 感知源切换（效果图上的四个源按钮） -->
      <div class="ws-ov-sensors">
        <button
          v-for="k in SENSOR_KINDS"
          :key="k"
          type="button"
          class="ws-sensor"
          :class="{ 'is-on': srcFilter.std === sensorIdOf(k) }"
          :style="{ '--c': KIND_COLOR[k] }"
          @click="toggleSensor(k)"
        >
          <span class="ws-sensor-ico">
            <svg viewBox="0 0 24 24" v-html="SENSOR_ICONS[k]" />
          </span>
          {{ KIND_LABEL[k] }}
        </button>
      </div>

      <!-- 样例回放 -->
      <label class="ws-ov-sel ws-ov-scene">
        <span>样例回放</span>
        <select v-model="sceneKey">
          <option value="a">示例场景 A</option>
          <option value="b">示例场景 B</option>
        </select>
      </label>

      <div class="ws-ov-tools">
        <!-- 联动目标 -->
        <label class="ws-ov-sel">
          <span>联动目标</span>
          <select
            :value="selectedTargetId ?? ''"
            @change="selectTarget(($event.target as HTMLSelectElement).value || null)"
          >
            <option value="">全部目标</option>
            <option v-for="t in targetOptions" :key="t.id" :value="t.id">{{ t.id }}（{{ t.domain }}）</option>
          </select>
        </label>

        <!-- 关联域 -->
        <label class="ws-ov-sel">
          <span>关联域</span>
          <select v-model="srcFilter.assoc">
            <option :value="null">全部</option>
            <option value="surface">水面</option>
            <option value="underwater">水下</option>
          </select>
        </label>

        <span class="ws-ov-clock">{{ (timeMs / 1000).toFixed(1) }}s / {{ (durationMs / 1000).toFixed(0) }}s</span>

        <span class="ws-ov-badge">综合态势 · 实测回放</span>

        <button v-if="zoomed" class="ws-ov-back" type="button" @click="backToGrid">← 返回四宫格</button>
      </div>
    </header>

    <div v-if="allEmpty" class="ws-ov-empty">暂无数据</div>

    <div v-else class="ws-grid" :class="{ 'is-zoomed': !!zoomed }">
      <div v-show="!zoomed || zoomed === 'std'" class="ws-cell">
        <ModuleStd
          v-if="pipe"
          :mod="pipe.std"
          :selected-target-id="selectedTargetId"
          :selected-record-id="selectedRecordId"
          :source-filter="srcFilter.std"
          @select-record="selectRecord"
          @select-target="selectTarget"
          @filter-source="(id) => (srcFilter.std = id)"
          @open-detail="zoom"
        />
      </div>

      <div v-show="!zoomed || zoomed === 'align'" class="ws-cell">
        <ModuleAlign
          v-if="pipe"
          :mod="pipe.align"
          :selected-target-id="selectedTargetId"
          :duration-ms="durationMs"
          @select-row="() => {}"
          @select-target="selectTarget"
          @open-detail="zoom"
        />
      </div>

      <div v-show="!zoomed || zoomed === 'assoc'" class="ws-cell">
        <ModuleAssoc
          v-if="pipe"
          :mod="pipe.assoc"
          :selected-target-id="selectedTargetId"
          :domain-filter="srcFilter.assoc"
          @select-target="selectTarget"
          @select-candidate="(c) => c.globalId && selectTarget(c.globalId)"
          @open-detail="zoom"
        />
      </div>

      <div v-show="!zoomed || zoomed === 'recog'" class="ws-cell">
        <ModuleRecog
          v-if="pipe && recogModule"
          :mod="recogModule"
          :selected-target-id="selectedTargetId"
          :asset-base="assetBase"
          :frame-id="frameId"
          @select-frame="(id) => (frameId = id)"
          @select-target="selectTarget"
          @open-detail="zoom"
        />
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.ws-ov {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 10px 14px 12px;
  background: radial-gradient(120% 90% at 50% 0%, #062036 0%, #030d1a 100%);
}
/* ============ 顶部工具条 ============ */
.ws-ov-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 0 2px 9px;
  margin-bottom: 10px;
  border-bottom: 1px solid rgba(34, 228, 238, 0.24);
  flex-wrap: wrap;
}
.ws-ov-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 19px;
  line-height: 1.3;
  font-weight: 700;
  color: #e1f7ff;
  letter-spacing: 1px;
  white-space: nowrap;
  flex: 0 0 auto;
}
.ws-ov-logo { color: #22e4ee; }

/* 感知源按钮组 */
.ws-ov-sensors {
  display: flex;
  gap: 7px;
  padding-left: 14px;
  border-left: 1px solid rgba(34, 228, 238, 0.24);
  flex: 0 0 auto;
}
.ws-sensor {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  font-size: 14.5px;
  color: #9fe8f5;
  background: rgba(6, 40, 68, 0.6);
  border: 1px solid rgba(34, 228, 238, 0.45);
  border-radius: 3px;
  cursor: pointer;
  transition: all 0.15s;
}
.ws-sensor-ico { color: #22e4ee; display: flex; align-items: center; }
.ws-sensor-ico svg { width: 15px; height: 15px; display: block; }
.ws-sensor:hover { border-color: #22e4ee; color: #d9fbff; }
.ws-sensor.is-on {
  color: #04141f;
  background: linear-gradient(135deg, #22e4ee, #17b7c9);
  border-color: #22e4ee;
  box-shadow: 0 0 10px rgba(34, 228, 238, 0.35);
}
.ws-sensor.is-on .ws-sensor-ico { color: #04141f; }

.ws-ov-scene { margin-left: 4px; }
.ws-ov-tools {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-left: auto;
}
.ws-ov-sel { display: flex; align-items: center; gap: 6px; font-size: 14px; color: #7fa8bd; white-space: nowrap; }
.ws-ov-sel select {
  background: #062540;
  color: #e1f7ff;
  border: 1px solid rgba(34, 228, 238, 0.35);
  border-radius: 4px;
  padding: 3px 7px;
  font-size: 14px;
}
.ws-ov-clock {
  font-size: 14.5px;
  color: #22e4ee;
  font-variant-numeric: tabular-nums;
  padding: 2px 10px;
  border: 1px solid rgba(34, 228, 238, 0.34);
  border-radius: 11px;
  background: rgba(4, 25, 46, 0.7);
  white-space: nowrap;
}
.ws-ov-badge {
  font-size: 14px;
  color: #9fe8f5;
  padding: 3px 12px;
  border: 1px solid rgba(34, 228, 238, 0.4);
  border-radius: 3px;
  background: rgba(8, 60, 82, 0.6);
  white-space: nowrap;
}
.ws-ov-back {
  background: rgba(34, 228, 238, 0.14);
  border: 1px solid rgba(34, 228, 238, 0.42);
  color: #22e4ee;
  font-size: 14px;
  padding: 4px 11px;
  border-radius: 4px;
  cursor: pointer;
}
.ws-ov-back:hover { background: rgba(34, 228, 238, 0.24); }
.ws-ov-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #7fa8bd;
  font-size: 16px;
}

.ws-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  gap: 10px;
}
.ws-grid.is-zoomed { grid-template-columns: 1fr; grid-template-rows: 1fr; }
.ws-cell { min-height: 0; min-width: 0; display: flex; }
.ws-cell > :deep(.ws-mod) { flex: 1; min-width: 0; }
</style>
