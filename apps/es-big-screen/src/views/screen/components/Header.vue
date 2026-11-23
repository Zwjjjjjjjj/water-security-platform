<template>
  <header>
    <h1>水域安防数字孪生平台</h1>
    <p>水域安防·实测回放</p>
    <nav aria-label="视图">
      <button
        v-for="t in tabs"
        :key="t.mode"
        :data-mode="t.mode"
        :aria-pressed="store.mode === t.mode"
        @click="setMode(t.mode)"
      >{{ t.label }}</button>
    </nav>
    <span class="mode-badge">实测回放</span>
  </header>
</template>

<script setup lang='ts'>
import { useWaterStore } from '@/store/water'
const store = useWaterStore()
const tabs = [
  { mode: 'overview', label: '综合态势' },
  { mode: 'surface', label: '水面监视' },
  { mode: 'underwater', label: '水下监视' },
  { mode: 'replay', label: '系统管理' },
] as const
function setMode(m: any) {
  store.setMode(m)
  store.follow = false
}
</script>
