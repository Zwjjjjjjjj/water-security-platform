<script setup lang="ts">
/**
 * V2 固定斜俯视背景层
 *
 * 【为什么单独做一个组件】
 * `campus_oblique.png` 是一张**已经烘焙了投影**的固定斜俯视背景：
 * 山体起伏、建筑侧面、防波堤侧壁都已经画在像素里。
 *
 * 这带来两个硬约束：
 *  1. **不能再对它套 2.5D 倾斜变换** ——
 *     否则会二次压缩，且覆盖层（航迹/船/标签）会与背景错位。
 *     所以本组件不走 `projectScene()`，而是按 ENU 视口直接定位。
 *  2. **必须关闭重复的旧内容** ——
 *     背景里已经有山体、厂区、岸线、码头、防波堤，
 *     因此正俯视底图、旧建筑贴图、旧防波堤层、旧灯杆都要关掉，
 *     只保留：动态目标、源点迹、航迹、标签、交互层。
 *
 * 【与 V1 保持一致的实现方式】
 * 背景**不是**独立的 HTML `<img>` 绝对定位层，而是与 V1 的底图完全同构：
 *
 *   <svg viewBox="0 0 chartW chartH">
 *     <g :transform="matrix(...)">
 *       <image href="campus_oblique.png" ... />
 *     </g>
 *   </svg>
 *
 * 这样背景与航迹层共用**同一个 SVG 容器像素坐标系**
 * （与 `projectEnu()` 的输出空间一致），
 * 于是：
 *   - 缩放/平移天然同步，不需要额外的同步逻辑；
 *   - 背景不再是「另一层」，而是与航迹同处一个 SVG 内的底图组；
 *   - 视觉上背景、航迹、船、标签成为同一个画面的组成部分。
 *
 * 【铺满全屏 —— 三层结构】
 * 斜视图是一张**有限**的图（1536×1024）。若只画它，
 * 视口一旦超出这张图的范围，边缘就会露出底色（=「画外」）。
 *
 * 因此这里画三层，自下而上：
 *   1. **海面底纹层**：`sea_surface.png` 四象限镜像平铺，
 *      铺满**整个容器**。它在任何缩放/平移下都在，
 *      因此画面永远不会出现空白边。
 *   2. **斜视概念图层**：`campus_oblique.png`，
 *      承载厂区、建筑、岸线、防波堤等主体内容。
 *   3. （航迹/船/标签由 `ChartSceneV2.vue` 在本组件之上叠加）
 *
 * 镜像平铺是素材包 `developer_notes.md` 给出的方案：
 * 生成纹理的边界不保证逐像素周期匹配，
 * 四象限镜像可以让相邻边界的采样方向相反、视觉上连续。
 *
 * 【配准说明】
 * 斜视图没有已核验的 ENU 控制点，无法与实测航迹做真实地理配准。
 * 这里采用「**示意局部坐标系**」：
 *   - 背景：图片像素坐标
 *   - ENU 点位：经 `enuToImage()` 映射到该坐标系
 * 界面上必须标注「场景示意」，不能据此读取真实岸线距离。
 */
import { computed } from 'vue'
import { getSceneAssetsV2, type SceneAssetV2 } from './scene-assets.v2'
import { computeObliqueFit } from './oblique-fit'

const props = defineProps({
  /** 素材是否已加载完成 */
  assetsReady: { type: Boolean, default: false },
  /** 容器像素尺寸 */
  chartW: { type: Number, default: 1920 },
  chartH: { type: Number, default: 870 },
  /** 当前 ENU 视口（米） */
  xMin: { type: Number, default: -2600 },
  xMax: { type: Number, default: 1000 },
  yMin: { type: Number, default: 3000 },
  yMax: { type: Number, default: 5000 },
  /** 绘图区边距（像素） */
  gridLeft: { type: Number, default: 8 },
  gridRight: { type: Number, default: 8 },
  gridTop: { type: Number, default: 8 },
  gridBottom: { type: Number, default: 8 },
})

/** 斜视背景素材 */
const obliqueTex = computed<SceneAssetV2 | undefined>(() => {
  void props.assetsReady
  return getSceneAssetsV2().find((a) => a.id === 'campus_oblique')
})

/** 海面纹理素材（底层平铺用） */
const seaTex = computed<SceneAssetV2 | undefined>(() => {
  void props.assetsReady
  return getSceneAssetsV2().find((a) => a.id === 'sea_surface')
})

/**
 * 海面平铺单元的显示尺寸（容器像素）。
 *
 * 用 `r` 换算：让海面纹理与斜视图处于同一缩放层级，
 * 这样两层之间的海面质感一致，看不出是两张图拼的。
 */
const seaTile = computed(() => {
  const t = seaTex.value
  if (!t) return null
  const nat = Math.max(1, t.width_px || 1254)
  /* 一格海面对应约 1/3 张斜视图宽度，尺度与斜视图内的海面接近 */
  const px = (nat / 3) * layout.value.r
  return { href: t.url, tile: Math.max(60, px) }
})

/**
 * 背景图在容器中的位置与尺寸。
 *
 * 【核心映射】
 * 背景图的 ENU→图片映射由 `enuToImage()` 定义（仿射变换）。
 * 我们反过来求：当前 ENU 视口的四个角，在图片坐标系里是什么位置；
 * 再把这些位置换算成容器像素。
 *
 * 【公式收敛到 oblique-fit.ts】
 * 原本这段计算只存在于本组件里，而标签层需要**完全相同**的变换，
 * 两边各写一份迟早会漂移。现在统一由 `computeObliqueFit()` 提供，
 * 背景与文字共用同一份映射。
 *
 * 【单位换算】
 * 「图片像素 → 容器像素」的比率 r 必须满足：
 *     1 米 ENU → a 个图片像素        （enuToImage 的 x 系数）
 *     1 米 ENU → pxPerM 个容器像素    （视图比例）
 *   因此 r = pxPerM / a，而不是 pxPerM * a。
 * 写成乘法会让背景被缩小约 (a²) 倍，导致背景只占视口一小块、
 * 右侧大片留空，目标与背景严重脱节。
 */
const layout = computed(() => {
  /* 映射公式收敛到 oblique-fit.ts，与标签层共用同一份实现，
     保证「背景图」与「背景图上的文字」永远使用完全一致的变换。 */
  const fit = computeObliqueFit({
    xMin: props.xMin,
    xMax: props.xMax,
    yMin: props.yMin,
    yMax: props.yMax,
    gridLeft: props.gridLeft,
    gridTop: props.gridTop,
    plotW: Math.max(50, props.chartW - props.gridLeft - props.gridRight),
    plotH: Math.max(50, props.chartH - props.gridTop - props.gridBottom),
    /* 【铺满全屏】背景锚在容器原点，而不是绘图区原点。
       斜视模式下 grid 已归零，两者本就是同一个点，
       这里显式传 0,0 让意图明确：背景要铺满整个显示区。 */
    anchorX: 0,
    anchorY: 0,
  })

  return {
    src: obliqueTex.value?.url,
    width: fit.width,
    height: fit.height,
    left: fit.left,
    top: fit.top,
    /* 图片像素 → 容器像素 的比率（海面平铺需要用到） */
    r: fit.r,
  }
})

/**
 * 背景组的 SVG 变换矩阵。
 *
 * 与 V1 的 `bgTransform` 同构：把图片像素坐标映射到容器像素坐标。
 * `<image>` 用 x=0,y=0 定位，靠 `<g transform="matrix(...)">` 整体搬运。
 */
const imageMatrix = computed(() => {
  const l = layout.value
  /* 仅平移（图片已按容器像素算好尺寸），保留图片像素 1:1 的比例 */
  return `matrix(1 0 0 1 ${l.left.toFixed(3)} ${l.top.toFixed(3)})`
})

/**
 * 海面底纹的平铺图案（四象限镜像）。
 *
 * 用 4 个 `<image>` 拼成 2×2 的镜像块：
 *   左上 原图 | 右上 水平镜像
 *   左下 垂直镜像 | 右下 双向镜像
 * 这样一个 2×2 的周期块里，相邻边界两侧的纹理方向相反，
 * 接缝在视觉上不突兀（生成纹理不保证逐像素无缝时的标准做法）。
 */
const seaPatternId = 'obliqueSeaTile'
</script>

<template>
  <svg
    class="ws-oblique-bg-svg"
    :viewBox="`0 0 ${chartW} ${chartH}`"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <!--
        海面平铺图案：四象限镜像。
        尺寸取 tile×2 作为 pattern 的周期，
        于是 4 个子图刚好铺满一个 pattern，pattern 再在容器里平铺。
      -->
      <pattern
        v-if="seaTile"
        :id="seaPatternId"
        :width="seaTile.tile * 2"
        :height="seaTile.tile * 2"
        patternUnits="userSpaceOnUse"
      >
        <image
          :href="seaTile.href"
          x="0"
          y="0"
          :width="seaTile.tile"
          :height="seaTile.tile"
          preserveAspectRatio="none"
        />
        <image
          :href="seaTile.href"
          x="0"
          y="0"
          :width="seaTile.tile"
          :height="seaTile.tile"
          preserveAspectRatio="none"
          :transform="`translate(${seaTile.tile * 2} 0) scale(-1 1)`"
        />
        <image
          :href="seaTile.href"
          x="0"
          y="0"
          :width="seaTile.tile"
          :height="seaTile.tile"
          preserveAspectRatio="none"
          :transform="`translate(0 ${seaTile.tile * 2}) scale(1 -1)`"
        />
        <image
          :href="seaTile.href"
          x="0"
          y="0"
          :width="seaTile.tile"
          :height="seaTile.tile"
          preserveAspectRatio="none"
          :transform="`translate(${seaTile.tile * 2} ${seaTile.tile * 2}) scale(-1 -1)`"
        />
      </pattern>
    </defs>

    <!-- ===== 第 1 层：海面底纹（铺满整个容器，任何缩放平移都不露白） ===== -->
    <rect
      x="0"
      y="0"
      :width="chartW"
      :height="chartH"
      fill="#0a2c3c"
    />
    <rect
      v-if="seaTile"
      x="0"
      y="0"
      :width="chartW"
      :height="chartH"
      :fill="`url(#${seaPatternId})`"
      opacity="0.55"
    />

    <!-- ===== 第 2 层：斜视概念图（厂区 / 建筑 / 岸线 / 防波堤） ===== -->
    <image
      v-if="obliqueTex"
      :href="layout.src"
      :x="0"
      :y="0"
      :width="layout.width"
      :height="layout.height"
      :transform="imageMatrix"
      preserveAspectRatio="none"
      opacity="0.94"
    />
  </svg>
</template>

<style scoped>
/* 与 V1 的 .ws-harbor-bg 同构：与 .ws-chart 完全重合，
   共用容器像素坐标系，底图与航迹因此像素级对齐。 */
.ws-oblique-bg-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  /* 兜底底色：万一海面贴图缺失，也不至于出现刺眼空白 */
  background: linear-gradient(180deg, #123444 0%, #0a2233 55%, #071a28 100%);
  filter: brightness(1.02) saturate(1.04);
}
</style>
