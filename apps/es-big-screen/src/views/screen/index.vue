<template>
  <div class="es-screen-container">
    <div ref="screenRef" id="screen" :class="screenModeClass" aria-label="水域安防数字孪生平台演示">
      <div class="world-viewport">
        <div id="world">
          <!-- 场景版本切换：V1 = 2.5D 手绘底图版（保留）；V2 = 写实航拍正俯视版 -->
          <!-- 「综合态势」页：四模块工作总览（数据标准化 / 时空对齐 / 航迹关联 / 目标识别） -->
          <OverviewBoard
            v-if="snapshot && store.mode === 'overview'"
            :data="data"
            :time-ms="store.time_ms"
            :duration-ms="durationMs"
            :asset-base="assetBase"
            :selected-target-id="store.selectedTargetId"
            @select-target="onOverviewSelect"
          />
          <!-- 「系统管理」页签：整页替换主场景 -->
          <SystemManagement v-else-if="snapshot && store.mode === 'replay'" />
          <component
            :is="sceneComponent"
            v-else-if="snapshot"
            ref="sceneRef"
            :snapshot="snapshot"
            :selected-target-id="store.selectedTargetId"
            :layers="store.layers"
            :follow="store.follow"
            :paused="!store.playing"
            :mode="store.mode"
            @select-target="store.select($event)"
          />
          <div v-else-if="error" id="error">{{ error }}</div>
          <div v-else class="ws-loading">场景加载中…</div>
        </div>
      </div>
      <Header />

      <!--
        以下四块（左右侧栏、场景注记、底部操作条与时间进度条）
        只服务于「水面监视 / 水下监视」两个场景页。
        「综合态势」与「系统管理」是整页工作台，
        保留它们会压住内容、让画面显脏 —— 所以按模式整体隐藏。
      -->
      <template v-if="!isFullPageMode">
        <aside class="left-column">
          <Left />
        </aside>
        <aside class="right-column">
          <Right />
        </aside>
        <div class="scene-notes">
          <span class="ws-tag">站心坐标 · 实测回放</span>
        </div>
        <div class="action-bar">
          <button class="ws-button" id="reset-scale" title="复位缩放比例到初始值" @click="resetSceneScale"><img src="/water-security/assets/icons/reset-scale.svg" alt="" />复位比例</button>
          <button class="ws-button" id="water-switch" @click="switchWater"><img src="/water-security/assets/icons/water.svg" alt="" />水面 / 水下</button>
          <button class="ws-button" id="follow" :aria-pressed="store.follow" @click="store.follow = !store.follow"><img src="/water-security/assets/icons/target.svg" alt="" />目标跟随</button>
          <button class="ws-button ws-button--primary" id="demo" :aria-pressed="store.scripted" @click="startDemo"><img src="/water-security/assets/icons/play.svg" alt="" />一键演示</button>
          <button class="ws-button" id="scene-version" :title="'切换场景版本：当前 ' + sceneVersionLabel" @click="toggleSceneVersion">{{ sceneVersionLabel }}</button>
        </div>
        <!--
          时间进度条 —— 实时监控模式。

          【两种时间轴】
          · 实时监控（live）—— 时钟走真实墙钟，播放头贴着「现在」，
            末端右侧是不断滚动的「实时」指示带。
          · 数据回放（replay）—— 时间轴是这一段数据的相对偏移，
            可拖拽定位，配合倒放按钮复盘。

          倒放：direction = -1。实时模式下表示「回看刚才」，
          回放模式下表示「倒着播放这段数据」。
        -->
        <div class="playbar ws-panel" :class="{ 'is-rev': store.direction === -1 }">
          <!-- 时间轴模式 -->
          <div class="playbar-modes" role="group" aria-label="时间轴模式">
            <button
              type="button"
              class="playbar-mode"
              :class="{ 'is-on': store.timelineMode === 'live' }"
              @click="store.setTimelineMode('live')"
            >
              实时监控
            </button>
            <button
              type="button"
              class="playbar-mode"
              :class="{ 'is-on': store.timelineMode === 'replay' }"
              @click="store.setTimelineMode('replay')"
            >
              数据回放
            </button>
          </div>

          <button id="play" class="ws-button" :aria-label="store.playing ? '暂停' : '播放'" @click="toggle">
            <img :src="store.playing ? '/water-security/assets/icons/pause.svg' : '/water-security/assets/icons/play.svg'" alt="" />
          </button>

          <!-- 倒放 -->
          <button
            id="reverse"
            class="ws-button playbar-rev"
            :class="{ 'is-on': store.direction === -1 }"
            :aria-pressed="store.direction === -1"
            :aria-label="store.direction === -1 ? '倒放中，点击恢复正放' : '倒放'"
            title="倒放 / 回看刚才"
            @click="store.toggleDirection()"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="M11 5V2L4 9l7 7V11a5 5 0 0 1 5 5h2a7 7 0 0 0-7-7z"
                fill="currentColor"
              />
            </svg>
          </button>

          <select id="speed" v-model.number="store.speed" aria-label="播放速度">
            <option :value="0.5">0.5×</option>
            <option :value="1">1×</option>
            <option :value="2">2×</option>
            <option :value="4">4×</option>
          </select>

          <!-- 实时时钟：真实日期时间 -->
          <output id="clock" class="playbar-clock" :title="absoluteText">
            {{ clockText }}
          </output>
          <span class="playbar-date">{{ dateText }}</span>

          <!-- 进度条 -->
          <div class="playbar-track">
            <input
              type="range"
              id="seek"
              min="0"
              :max="durationMs"
              step="100"
              :value="store.time_ms"
              aria-label="时间轴进度"
              @input="onSeek"
            />
            <!--
              实时模式：右侧滚动指示带表示「数据持续流入」。
              播放头贴着右端走 —— 因为数据是「现在正在发生」的，
              越靠右代表越接近当下。
            -->
            <div v-if="store.timelineMode === 'live'" class="playbar-live">
              <i v-for="n in 14" :key="n" :style="{ animationDelay: (n * 0.13).toFixed(2) + 's' }" />
            </div>
          </div>

          <span class="playbar-end" :class="{ 'is-live': store.timelineMode === 'live' }">
            <template v-if="store.timelineMode === 'live'">
              <i class="playbar-dot" />现在
            </template>
            <template v-else>{{ formatTime(durationMs) }}</template>
          </span>
        </div>
      </template>

      <footer style="display:none">站心坐标 / 1920 × 1080 / 实测航迹回放</footer>
    </div>
  </div>
</template>

<script setup lang='ts'>
import { computed, ref } from 'vue'
import Header from './components/Header.vue'
import Left from './components/left/index.vue'
import Right from './components/right/index.vue'
import SystemManagement from './components/SystemManagement.vue'
import OverviewBoard from '@/water-security/integration/OverviewBoard.vue'
import ChartScene from '@/water-security/integration/ChartScene.vue'
import ChartSceneV2 from '@/water-security/integration/ChartSceneV2.vue'
import { useResize } from '@/utils/useResize'
import { useWaterDemo } from '@/water-security/useWaterDemo'

const { screenRef } = useResize()
const {
  store,
  data,
  snapshot,
  error,
  seek,
  startDemo,
  formatTime,
  assetBase,
  absoluteText,
  clockText,
} = useWaterDemo()

const durationMs = computed(() => (snapshot.value as any)?.duration_ms ?? 302000)

/** 日期文本「2026-11-21」 */
const dateText = computed(() => absoluteText.value.slice(0, 10))

/**
 * 「整页模式」判定。
 *
 * 【为什么要分这两种模式】
 * · 水面监视 / 水下监视 —— 港区场景页。
 *   左边是设备与目标列表，右边是目标详情与量测，
 *   底下是场景操作按钮与回放时间条。
 *   这些是**看场景**用的，围绕中央三维画面组织。
 *
 * · 综合态势 / 系统管理 —— 整页工作台。
 *   内容自己就是满屏的（四宫格 / 设备接入表），
 *   再叠上左右栏和底部条只会：
 *     ① 压住内容，图表被挤到变形；
 *     ② 视觉噪音大 —— 一屏里出现三套不同的信息层，
 *        看不清当前页到底是干什么的。
 *
 * 所以这两种页面**整页独占**：隐藏左右栏、场景注记、
 * 操作条与回放条，让内容铺满页头以下的空间。
 */
const isFullPageMode = computed(() => store.mode === 'overview' || store.mode === 'replay')

/** 供 CSS 使用的模式类名 */
const screenModeClass = computed(() => ({
  'is-fullpage': isFullPageMode.value,
  [`is-mode-${store.mode}`]: true,
}))

function toggle() {
  /* 回放模式播到头后再点播放 → 从头开始；
     实时模式是循环滚动的，不需要重置。 */
  if (store.timelineMode === 'replay' && store.direction === 1 && store.time_ms >= durationMs.value) {
    seek(0)
  }
  store.playing = !store.playing
}
function onSeek(e: Event) {
  seek(Number((e.target as HTMLInputElement).value))
}
/**
 * 场景组件引用。
 *
 * 「全景航迹」按钮要调用场景内部的 `resetView()`，
 * 让视窗回到全景取景 —— 而不是跳到「综合态势」那个页面。
 */
const sceneRef = ref<any>(null)

/**
 * 「复位比例」—— 把缩放恢复到初始自适应比例。
 *
 * 只复位比例尺，取景模式（主体 / 跟随）保持不变。
 * 若想要「全域总览」视野，用场景右上角缩放面板里的「总览」按钮。
 */
function resetSceneScale() {
  sceneRef.value?.resetScale?.()
}
function switchWater() {
  store.setMode(store.mode === 'underwater' ? 'surface' : 'underwater')
}

/**
 * 四模块页的目标联动。
 *
 * 传入 null 表示「取消选中」，此时恢复显示全部。
 */
function onOverviewSelect(id: string | null) {
  if (!id) {
    store.selectedTargetId = '' as any
    store.scripted = false
    return
  }
  store.select(id)
}

/**
 * 场景版本切换。
 *
 * V1 = 2.5D 手绘底图版（保留不动，作为已有成果）
 * V2 = 写实航拍正俯视版（新素材到位后替换）
 *
 * 用 localStorage 记住选择，刷新后仍是同一版本。
 */
const SCENE_VERSION_KEY = 'es-scene-version'
const sceneVersion = ref<'v1' | 'v2'>(
  (localStorage.getItem(SCENE_VERSION_KEY) as 'v1' | 'v2') || 'v1',
)
const sceneComponent = computed(() => (sceneVersion.value === 'v2' ? ChartSceneV2 : ChartScene))
const sceneVersionLabel = computed(() => (sceneVersion.value === 'v2' ? '场景 V2' : '场景 V1'))
function toggleSceneVersion() {
  sceneVersion.value = sceneVersion.value === 'v1' ? 'v2' : 'v1'
  localStorage.setItem(SCENE_VERSION_KEY, sceneVersion.value)
}
</script>

<style lang='scss' scoped>
.es-screen-container {
  --es-screen-width: 1920px;
  --es-screen-height: 1080px;
  --es-header-height: 112px;
  position: relative;
  margin: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #020912;
}
#screen {
  position: absolute;
  user-select: none;
  top: 50%;
  left: 50%;
  width: var(--es-screen-width);
  height: var(--es-screen-height);
  /* 居中由 transform 完成（translate(-50%,-50%) 与 scale 合并在同一次变换里）。
     若改用负 margin 抵消 left/top:50%，缩放后会出现偏移：
     变换原点为 top left 时，负 margin 是按未缩放尺寸计算的，
     缩放后画布会整体偏移（1600x900 溢出左侧 160px / 上方 90px）。 */
  transform-origin: center center;
}
.world-viewport #world :deep(.ws-scene),
.world-viewport #world :deep(.ws-chart-wrap) {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  aspect-ratio: auto;
}
.world-viewport #world :deep(.ws-chart-wrap) { aspect-ratio: auto; }
.ws-loading, #error { color: #8fb9cc; padding: 40px; font-size: 18px; }
#error { color: #ff8a8a; }
/* 素材 preview.css 里 header 背景是相对路径，在 Vite 下解析不到，用 public 绝对路径覆盖，保证和效果图一致 */
#screen > header {
  background: url('/water-security/assets/ui/header_border_dark.png') top / 100% 112px no-repeat !important;
}

/* ============================================================
 * 整页模式（综合态势 / 系统管理）
 *
 * 这些页面要「干净」—— 只保留页头 + 满屏内容，
 * 两侧栏、底部操作条与时间进度条全部让位。
 * ============================================================ */
#screen.is-fullpage .world-viewport {
  /* 页头实测高 112px（含底部描边线）。
     这里必须用 112 而不是 105，
     否则页头那条青色底线会横穿到内容区顶部 —— 就是截图里那条「横线」。 */
  inset: 112px 0 0 0;
}
</style>
