#!/usr/bin/env python3
"""重写 SceneWaterFxV2.vue 的 template + style：渲染物理化的海面动效。"""
path = 'src/water-security/integration/SceneWaterFxV2.vue'
src = open(path, encoding='utf-8').read()

tpl_start = src.index('<template>')
tpl_end = src.index('</template>') + len('</template>')

new_tpl = '''<template>
  <svg
    class="ws-water-fx"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <!-- 泡沫柔光：让白沫边缘柔化，不是生硬的圆点 -->
      <radialGradient id="fxFoam">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.95" />
        <stop offset="0.55" stop-color="#dff4ff" stop-opacity="0.5" />
        <stop offset="1" stop-color="#bfe8ff" stop-opacity="0" />
      </radialGradient>
      <!-- 灯桩光晕 -->
      <radialGradient id="fxBeacon">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.9" />
        <stop offset="0.3" stop-color="#ffd54a" stop-opacity="0.45" />
        <stop offset="1" stop-color="#ffd54a" stop-opacity="0" />
      </radialGradient>
      <!-- 尾迹柔化：船尾白水用，中心亮、边缘散 -->
      <linearGradient id="fxWake" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#d8f6ff" stop-opacity="0.85" />
        <stop offset="1" stop-color="#8fd8f0" stop-opacity="0.15" />
      </linearGradient>
      <!--
        泡沫的「迎浪面」渐变：
        白沫靠近堤体的一侧最亮（破碎最剧烈），
        向海一侧迅速衰减 —— 与真实破浪的形态一致。
      -->
      <radialGradient id="fxSurf" cx="0.5" cy="0.5" r="0.5">
        <stop offset="0" stop-color="#ffffff" stop-opacity="0.9" />
        <stop offset="0.6" stop-color="#e6f7ff" stop-opacity="0.42" />
        <stop offset="1" stop-color="#cfeaff" stop-opacity="0" />
      </radialGradient>
    </defs>

    <!-- ===== 第 1 层：海面细波纹（会向前推进，不是原地闪） ===== -->
    <g class="ws-fx-ripples">
      <path
        v-for="rp in ripplePaths"
        :key="rp.key"
        :d="rp.d"
        fill="none"
        stroke="#9fd8ee"
        :stroke-width="1"
        :stroke-opacity="rp.opacity"
        :style="{
          animationDuration: rp.duration,
          animationDelay: rp.delay,
          '--fx-travel': rp.travel.toFixed(1) + 'px',
          '--fx-angle': rp.angle + 'deg',
        }"
      />
    </g>

    <!-- ===== 第 2 层：岸线破碎泡沫（涌上堤面 → 爆发 → 回落） ===== -->
    <g class="ws-fx-foam">
      <g
        v-for="fp in foamShapes"
        :key="fp.key"
        :style="{
          animationDuration: fp.duration,
          animationDelay: fp.delay,
          '--fx-surge-x': fp.dx + 'px',
          '--fx-surge-y': fp.dy + 'px',
          '--fx-burst': fp.burstScale,
        }"
      >
        <ellipse
          :cx="fp.cx"
          :cy="fp.cy"
          :rx="fp.rx"
          :ry="fp.ry"
          fill="url(#fxSurf)"
          :opacity="fp.opacity"
        />
      </g>
    </g>

    <!-- ===== 第 3 层：泊位标线 ===== -->
    <g class="ws-fx-berth">
      <line
        v-for="bm in berthLines"
        :key="bm.key"
        :x1="bm.x1"
        :y1="bm.y1"
        :x2="bm.x2"
        :y2="bm.y2"
        stroke="#cfe8f5"
        stroke-width="1.6"
        :stroke-opacity="bm.opacity"
        stroke-dasharray="7 5"
      />
    </g>

    <!-- ===== 第 4 层：航标（浮标 / 堤头灯桩） ===== -->
    <g class="ws-fx-navaids">
      <g v-for="na in navAidShapes" :key="na.key" :transform="`translate(${na.cx} ${na.cy})`">
        <!-- 灯桩光晕（闪烁） -->
        <circle
          v-if="na.blinking"
          r="9"
          fill="url(#fxBeacon)"
          class="ws-fx-beacon-glow"
          :style="{ animationDuration: '2.2s', animationDelay: na.delay }"
        />
        <!-- 本体 -->
        <circle r="3.2" :fill="na.color" opacity="0.95" />
        <circle r="3.2" fill="none" stroke="#0a2233" stroke-width="0.9" opacity="0.8" />
        <!-- 浮标顶标 -->
        <rect v-if="na.kind === 'buoy'" x="-1.1" y="-6.4" width="2.2" height="3.4" :fill="na.color" opacity="0.9" />
      </g>
    </g>

    <!-- ===== 第 5 层：船舶尾迹 ===== -->
    <!--
      【分三层渲染的原因】
        · wash  —— 船尾螺旋桨白水：柔和、宽、亮
        · arm   —— Kelvin 波臂：细、半透明
        · crest —— 波峰高光：极细、偏亮
      三者的视觉语义完全不同，混在一起就变成了「一条白带」。
    -->
    <g class="ws-fx-wakes">
      <!-- 5a. Kelvin 波臂 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'arm')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="#bfe9f7"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <!-- 5b. 波峰高光 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'crest')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="#eafaff"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
      />
      <!-- 5c. 船尾白水 -->
      <path
        v-for="wk in wakePaths.filter((w) => w.kind === 'wash')"
        :key="wk.key"
        :d="wk.d"
        fill="none"
        stroke="url(#fxWake)"
        :stroke-width="wk.width"
        :stroke-opacity="wk.opacity"
        stroke-linecap="round"
      />
    </g>

    <!-- ===== 第 6 层：尾迹上漂移的泡沫白点 ===== -->
    <!--
      让尾迹「动」起来的关键：
      白点沿尾迹向船尾方向持续漂移，并在漂移中淡出。
    -->
    <g class="ws-fx-wake-foam">
      <circle
        v-for="dot in wakeFoamDots"
        :key="dot.key"
        :cx="dot.x"
        :cy="dot.y"
        :r="dot.r"
        fill="#ffffff"
        :opacity="dot.opacity"
        :style="{
          animationDuration: dot.duration,
          animationDelay: dot.delay,
          '--fx-drift-x': dot.dx,
          '--fx-drift-y': dot.dy,
        }"
      />
    </g>
  </svg>
</template>'''

src = src[:tpl_start] + new_tpl + src[tpl_end:]

# ---------- style ----------
style_start = src.index('<style scoped>')
new_style = '''<style scoped>
.ws-water-fx {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 1;
}

/* ================================================================
   第 1 层：海面细波纹 —— 向前推进
   ================================================================ */
/**
 * 【为什么要平移而不是只改透明度】
 * 真实的涌浪是**传播**的：波峰自外海向岸边推进。
 * 只做透明度起伏的话，海面看起来像「贴了一层花纹」，
 * 而不是在「动」。
 *
 * 位移用 `--fx-travel`（速度 × 周期）与 `--fx-angle`（传播方向）
 * 两个自定义属性表达，由 CSS 变量在关键帧里插值。
 */
.ws-fx-ripples path {
  animation-name: ws-fx-ripple;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-ripple {
  0% {
    opacity: 0;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * -1));
  }
  /* 淡入：波峰从远处「出现」 */
  22% {
    opacity: 1;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * -0.55));
  }
  /* 推进到近处 */
  78% {
    opacity: 1;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(calc(var(--fx-travel) * 0.55));
  }
  /* 走出画面 → 淡出并回到起点 */
  100% {
    opacity: 0;
    transform: translate(0, 0) rotate(var(--fx-angle)) translateY(var(--fx-travel));
  }
}

/* ================================================================
   第 2 层：岸线破碎泡沫 —— 涌浪周期
   ================================================================ */
/**
 * 【一个周期的物理剧本】
 *   0%   平静（尺寸小、几乎不可见）
 *   30%  涌浪逼近，开始沿外法线前推
 *   52%  **破碎爆发** —— 最亮、最大（白水翻上堤面）
 *   78%  回落（沿法线退回，尺寸缩小、亮度下降）
 *   100% 恢复平静
 *
 * 关键帧里只用 transform / opacity —— 不触发重排。
 */
.ws-fx-foam g {
  animation-name: ws-fx-surf;
  animation-timing-function: cubic-bezier(0.4, 0.05, 0.55, 1);
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-surf {
  0%,
  100% {
    opacity: 0.12;
    transform: translate(0, 0) scale(0.62);
  }
  /* 涌浪逼近：白水开始向堤面爬升 */
  30% {
    opacity: 0.42;
    transform: translate(calc(var(--fx-surge-x) * 0.35), calc(var(--fx-surge-y) * 0.35)) scale(0.86);
  }
  /* 破碎爆发：白水翻上堤面，最亮最大 */
  52% {
    opacity: 1;
    transform: translate(var(--fx-surge-x), var(--fx-surge-y)) scale(var(--fx-burst));
  }
  /* 回落：白水沿堤面回流 */
  78% {
    opacity: 0.34;
    transform: translate(calc(var(--fx-surge-x) * 0.2), calc(var(--fx-surge-y) * 0.2)) scale(0.72);
  }
}

/* ================================================================
   第 6 层：尾迹泡沫白点 —— 沿尾迹漂移
   ================================================================ */
/**
 * 白点沿尾迹方向**持续向后漂移**，同时淡出 ——
 * 于是尾迹读起来是「正在消散的水面痕迹」，
 * 而不是「画在地图上的线条」。
 */
.ws-fx-wake-foam circle {
  animation-name: ws-fx-drift;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform, opacity;
}
@keyframes ws-fx-drift {
  0% {
    opacity: 0;
    transform: translate(0, 0);
  }
  25% {
    opacity: 0.85;
  }
  100% {
    opacity: 0;
    transform: translate(var(--fx-drift-x), var(--fx-drift-y));
  }
}

/* ================================================================
   航标：灯桩规律闪烁
   ================================================================ */
.ws-fx-beacon-glow {
  animation-name: ws-fx-beacon;
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
}
@keyframes ws-fx-beacon {
  0%,
  100% {
    opacity: 0.12;
  }
  50% {
    opacity: 0.9;
  }
}

@media (prefers-reduced-motion: reduce) {
  .ws-fx-ripples path,
  .ws-fx-foam g,
  .ws-fx-wake-foam circle,
  .ws-fx-beacon-glow {
    animation: none !important;
  }
}
</style>'''

src = src[:style_start] + new_style
open(path, 'w', encoding='utf-8').write(src)
print("template + style replaced")
