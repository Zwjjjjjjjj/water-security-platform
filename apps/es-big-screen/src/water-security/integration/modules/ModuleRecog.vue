<script setup lang="ts">

/** 模块标题图标 */
const MOD_ICON =
  '<circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="3.4" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 1.8v3.4M12 18.8v3.4M1.8 12h3.4M18.8 12h3.4" stroke="currentColor" stroke-width="1.7"/>'

/**
 * 模块 4：目标识别
 *
 * 【效果图版式】
 *   左：声呐帧示意 —— 用 `sonar_frames.json` 里真实的 `image_url`，
 *       把 `detections[].bbox_xywh` 直接画成检测框叠在图像上。
 *       框的位置和大小**完全来自原始数据**，不是手摆的示意框。
 *   右：识别结果 —— 目标编号 / 候选类别 / 置信度 / 数据来源。
 *
 * 【置信度】
 * 原始数据只有 `label`，**没有置信度字段**，
 * 所以置信度列一律显示「—」，绝不伪造。
 *
 * 【数据来源】pipeline.buildRecognitionModule()。
 */
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { type RecogModule, type Detection } from '../pipeline'
import { PALETTE } from '../chart-utils'

const props = defineProps<{
  mod: RecogModule
  selectedTargetId: string | null
  assetBase: string
  frameId: string | null
}>()

const emit = defineEmits<{
  (e: 'select-frame', id: string): void
  (e: 'select-target', id: string): void
  (e: 'open-detail', id: string): void
}>()

/** 帧图片的完整 URL */
const frameUrl = computed(() => {
  const u = props.mod.current?.imageUrl ?? ''
  if (!u) return ''
  if (u.startsWith('http')) return u
  return `${props.assetBase}/${u.replace(/^\//, '')}`
})

/** 检测框（帧内像素坐标） */
const boxes = computed(() => {
  const cur = props.mod.current
  if (!cur || !cur.width || !cur.height) return []
  return cur.detections.map((d: Detection) => {
    const [x, y, w, h] = d.bbox
    const isSel = !!props.selectedTargetId && d.targetId === props.selectedTargetId
    return {
      ...d,
      x: x ?? 0,
      y: y ?? 0,
      w: w ?? 0,
      h: h ?? 0,
      dim: !!props.selectedTargetId && !isSel,
      isSel,
    }
  })
})

/** 帧时间轴（底部小条） */
/**
 * 帧时间轴（近期帧条带）。
 *
 * 【为什么宽度要跟随容器】
 * 之前固定 1000 单位宽 + 默认 `meet` 缩放，
 * 容器只有 ~600px 宽，viewBox 被压缩到 60%，
 * 条带只占了中间一段、左右空出两条。
 * 现在按容器实际像素宽来画，条带正好铺满整行。
 */
const frameAxisW = ref(600)

const frameAxis = computed(() => {
  const W = Math.max(120, frameAxisW.value)
  const list = props.mod.recent
  if (!list.length) return { W, H: 30, bars: [] as any[] }
  const H = 30
  const bw = Math.max(5, (W - 16) / list.length - 2)
  const bars = list.map((f, i) => ({
    id: f.frameId,
    x: 8 + i * ((W - 16) / list.length),
    w: bw,
    y: 4,
    h: f.detections.length ? 18 : 8,
    has: !!f.detections.length,
    isCur: f.frameId === props.mod.current?.frameId,
    count: f.detections.length,
  }))
  return { W, H, bars }
})

/**
 * 帧时间轴容器宽度监听。
 *
 * 条带要铺满整行，就必须知道容器有多宽 ——
 * 固定 1000 单位宽再靠 `meet` 缩放会缩窄、左右留白。
 */
let ro: ResizeObserver | null = null
onMounted(() => {
  const el = document.querySelector('.ws-mod--recog .ws-frame-axis')
  if (!el) return
  ro = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect.width ?? 0
    if (w > 60) frameAxisW.value = Math.round(w)
  })
  ro.observe(el)
})
onBeforeUnmount(() => ro?.disconnect())

function fmtTime(ms: number) {
  const s = Math.floor(ms / 1000)
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}
</script>

<template>
  <section class="ws-mod ws-mod--recog">
    <header class="ws-mod-head">
      <h3 class="ws-mod-title">
        <span class="ws-mod-ico">
          <svg viewBox="0 0 24 24" v-html="MOD_ICON" />
        </span>
        目标识别
        <em class="ws-mod-sub">| 检测证据 · 类别结果 · 来源追溯</em>
      </h3>
      <div class="ws-mod-right">
        <button class="ws-mod-more" type="button" title="查看详情" @click="emit('open-detail', 'recog')">查看详情 ↗</button>
      </div>
    </header>

    <div v-if="!mod.hasData" class="ws-mod-empty">暂无数据</div>

    <template v-else>
      <div class="ws-recog-main">
        <!-- ===== 左：声呐帧 + 检测框 ===== -->
        <div class="ws-recog-frame">
          <h4 class="ws-recog-h">
            声呐帧示意
            <em>{{ mod.current?.frameId }} · {{ fmtTime(mod.current?.timeMs ?? 0) }}</em>
          </h4>
          <div class="ws-framewrap">
            <svg
              v-if="mod.current"
              :viewBox="`0 0 ${mod.current.width} ${mod.current.height}`"
              class="ws-frame"
              preserveAspectRatio="xMidYMid meet"
            >
              <image :href="frameUrl" :width="mod.current.width" :height="mod.current.height" />
              <g
                v-for="b in boxes"
                :key="b.targetId + b.x"
                :opacity="b.dim ? 0.28 : 1"
                style="cursor: pointer"
                @click="emit('select-target', b.targetId)"
              >
                <rect
                  :x="b.x"
                  :y="b.y"
                  :width="b.w"
                  :height="b.h"
                  fill="none"
                  :stroke="b.isSel ? '#ffffff' : '#22e4ee'"
                  :stroke-width="b.isSel ? 3 : 2"
                />
                <rect
                  :x="b.x"
                  :y="Math.max(0, b.y - 15)"
                  :width="b.w"
                  height="15"
                  fill="#ffb547"
                />
                <text :x="b.x + 3" :y="Math.max(0, b.y - 15) + 11" class="ws-frame-lbl">
                  {{ b.targetId }}
                </text>
              </g>
            </svg>
            <div v-else class="ws-none">无帧数据</div>
          </div>
          <!-- 帧时间轴 -->
          <svg :viewBox="`0 0 ${frameAxis.W} ${frameAxis.H}`" class="ws-frame-axis">
            <rect
              v-for="b in frameAxis.bars"
              :key="b.id"
              :x="b.x"
              :y="b.y"
              :width="b.w"
              :height="b.h"
              :fill="b.has ? PALETTE.blue : 'rgba(143,185,204,0.22)'"
              :fill-opacity="b.isCur ? 1 : 0.7"
              :stroke="b.isCur ? PALETTE.amber : 'none'"
              stroke-width="1.4"
              rx="1.5"
              style="cursor: pointer"
              @click="emit('select-frame', b.id)"
            >
              <title>{{ b.id }} · {{ b.count }} 个目标</title>
            </rect>
          </svg>
          <div class="ws-legend">
            <span><i style="background: #22e4ee" />检测框</span>
            <span><i style="background: #ffb547" />目标标签</span>
            <span><i style="background: #3ea8ff" />含检测帧</span>
          </div>
        </div>

        <!-- ===== 右：识别结果 ===== -->
        <div class="ws-recog-res">
          <h4 class="ws-recog-h">识别结果</h4>
          <table class="ws-res">
            <thead>
              <tr>
                <th>目标编号</th>
                <th>候选类别</th>
                <th>置信度</th>
                <th>数据来源</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="r in mod.results"
                :key="r.targetId"
                :class="{ 'is-on': selectedTargetId === r.targetId }"
                @click="emit('select-target', r.targetId)"
              >
                <td class="k">{{ r.targetId }}</td>
                <td>{{ r.label }}</td>
                <td class="num">{{ r.confidence == null ? '—' : (r.confidence * 100).toFixed(1) + '%' }}</td>
                <td class="src">{{ mod.current?.sourceLabel ?? '实测回放' }}</td>
              </tr>
              <tr v-if="!mod.results.length">
                <td colspan="4" class="ws-none">当前帧暂无识别结果</td>
              </tr>
            </tbody>
          </table>

          <!-- 光电通道（真实设备信息，非示意图） -->
          <div v-if="mod.eo" class="ws-eo">
            <h5>
              <span class="ws-eo-dot" :class="{ 'is-on': mod.eo.status === 'active' }" />
              光电通道 · {{ mod.eo.sourceId }}
              <em>{{ mod.eo.stateLabel }}</em>
            </h5>
            <div class="ws-eo-grid">
              <div class="ws-rm"><span>设备</span><b>{{ mod.eo.label }}</b></div>
              <div class="ws-rm"><span>视距</span><b>{{ mod.eo.rangeM }} m</b></div>
              <div class="ws-rm"><span>视场角</span><b>{{ mod.eo.angleDeg }}°</b></div>
              <div class="ws-rm"><span>流协议</span><b>RTSP</b></div>
            </div>
          </div>

          <!-- 附加信息条 -->
          <div class="ws-recog-meta">
            <div class="ws-rm">
              <span>模型</span><b>{{ mod.model ?? '—' }}</b>
            </div>
            <div class="ws-rm">
              <span>类别</span><b>{{ mod.classes.length ? mod.classes.join('、') : '—' }}</b></div>
            <div class="ws-rm">
              <span>检测目标</span><b>{{ mod.totalDetections }}</b>
            </div>
            <div class="ws-rm">
              <span>有效帧</span><b>{{ mod.framesWithDetection }}/{{ mod.totalFrames }}</b>
            </div>
          </div>
        </div>
      </div>

      <!-- ===== 底部关键统计 ===== -->
      <footer class="ws-mod-foot">
        <div class="ws-kpi"><span>检测目标</span><b class="is-ok">{{ mod.totalDetections }}</b></div>
        <div class="ws-kpi"><span>有效帧</span><b>{{ mod.framesWithDetection }}</b></div>
        <div class="ws-kpi"><span>总帧数</span><b>{{ mod.totalFrames }}</b></div>
        <div class="ws-kpi"><span>识别类别</span><b>{{ mod.classes.length || '—' }}</b></div>
        <div class="ws-kpi ws-kpi--grow">
          <span>当前帧</span>
          <div class="ws-srcchips">
            <span class="ws-chip"><i style="background: #3ea8ff" />{{ mod.current?.frameId ?? '—' }}</span>
            <span class="ws-chip"><i style="background: #ffb547" />{{ mod.current?.isReal ? '实测声呐' : '合成帧' }}</span>
          </div>
        </div>
      </footer>
    </template>
  </section>
</template>

<style scoped lang="scss">
.ws-mod {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 10px 14px 10px;
  background: linear-gradient(180deg, rgba(8, 42, 68, 0.9), rgba(4, 25, 46, 0.9));
  border: 1px solid rgba(34, 228, 238, 0.3);
  border-radius: 6px;
  overflow: hidden;
}
.ws-mod-head {
  display: flex;
  align-items: center;
  justify-content: center;
  padding-bottom: 7px;
  border-bottom: 1px solid rgba(34, 228, 238, 0.2);
}
.ws-mod-title {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  font-size: 19px;
  line-height: 1.3;
  font-weight: 700;
  color: #e1f7ff;
  letter-spacing: 1px;
  white-space: nowrap;
}
.ws-mod-ico {
  color: #22e4ee;
  display: flex;
  align-items: center;
  flex: 0 0 auto;
}
.ws-mod-ico svg { width: 21px; height: 21px; display: block; }
.ws-mod-sub {
  font-size: 15px;
  font-weight: 400;
  font-style: normal;
  color: #7fa8bd;
  padding-left: 9px;
  border-left: 1px solid rgba(143, 185, 204, 0.35);
}
.ws-mod-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.ws-mod-more {
  background: none;
  border: none;
  color: #22e4ee;
  font-size: 14.5px;
  line-height: 1.3;
  cursor: pointer;
  padding: 0;
  white-space: nowrap;
}
.ws-mod-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: #7fa8bd; font-size: 14px; }

/* ===== 主体两列 ===== */
.ws-recog-main {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 12px;
  padding-top: 8px;
}
.ws-recog-frame,
.ws-recog-res {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(2, 16, 32, 0.5);
  border: 1px solid rgba(34, 228, 238, 0.14);
  border-radius: 4px;
  padding: 7px 9px;
}
.ws-recog-h {
  padding-left: 8px;
  border-left: 3px solid #22e4ee;
  margin: 0 0 5px;
  font-size: 15px;
  font-weight: 600;
  color: #22e4ee;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.ws-recog-h em { font-style: normal; color: #7fa8bd; font-weight: 400; font-size: 15px; }

.ws-framewrap {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #000;
  border-radius: 3px;
  overflow: hidden;
}
/**
 * 声纳帧图 —— 按原始宽高比（480×360 = 4:3）撑满面板高度。
 *
 * 【为什么不用 slice】
 * 声纳图是扇形扫描，切掉两侧会把扇面边缘裁没。
 * 这里保持 `meet` 保证扇形完整，
 * 同时用 `aspect-ratio` 让图片本身按 4:3 撑到容器高度，
 * 横向多出来的部分由黑色背景自然填满（本来就是扫描暗区）。
 */
.ws-frame {
  max-width: 100%;
  max-height: 100%;
  width: auto;
  height: 100%;
  aspect-ratio: 4 / 3;
  display: block;
}
.ws-frame-lbl { fill: #0b1b28; font-size: 14px; font-weight: 700; }
.ws-frame-axis { width: 100%; height: 30px; margin-top: 5px; }

.ws-res {
  width: 100%;
  /* 撑满面板高度，底部不留空白 */
  flex: 1;
  border-collapse: collapse;
  font-size: 14px;
}
.ws-res th {
  text-align: left;
  color: #9fd9c9;
  font-weight: 500;
  font-size: 14.5px;
  padding: 6px 7px;
  background: rgba(4, 32, 56, 0.9);
  border-bottom: 1px solid rgba(34, 228, 238, 0.3);
}
.ws-res td { padding: 7px; border-bottom: 1px solid rgba(143, 185, 204, 0.12); }
.ws-res tbody tr { cursor: pointer; }
.ws-res tbody tr:hover { background: rgba(34, 228, 238, 0.07); }
.ws-res tbody tr.is-on { background: rgba(34, 228, 238, 0.14); }
.ws-res td.k {
  color: #04141f;
  background: linear-gradient(135deg, #22e4ee, #17b7c9);
  font-weight: 700;
  font-family: 'Consolas', 'Courier New', monospace;
  letter-spacing: 0.5px;
  border-radius: 2px;
  text-align: center;
}
.ws-res tbody tr.is-on td.k { background: linear-gradient(135deg, #ffb547, #e09a2c); }
.ws-res td.num { color: #e1f7ff; }
.ws-res td.src { color: #7fa8bd; }
.ws-none { color: #7fa8bd; text-align: center; padding: 14px !important; }

/* 光电通道 */
.ws-eo {
  margin-top: 8px;
  padding: 6px 8px;
  border: 1px solid rgba(34, 228, 238, 0.16);
  border-radius: 3px;
  background: rgba(4, 25, 46, 0.5);
}
.ws-eo h5 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
  color: #22e4ee;
}
.ws-eo h5 em {
  font-style: normal;
  margin-left: auto;
  font-size: 15px;
  color: #39d98a;
}
.ws-eo-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #7fa8bd;
}
.ws-eo-dot.is-on {
  background: #39d98a;
  box-shadow: 0 0 6px rgba(57, 217, 138, 0.9);
}
.ws-eo-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 10px;
}

/* 附加信息 */
.ws-recog-meta {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed rgba(143, 185, 204, 0.18);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ws-rm { display: flex; justify-content: space-between; gap: 10px; font-size: 14px; }
.ws-rm span { color: #7fa8bd; }
.ws-rm b { color: #e1f7ff; font-weight: 600; }

.ws-legend {
  display: flex;
  gap: 11px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 14.5px;
  color: #8fb4c8;
}
.ws-legend span { display: inline-flex; align-items: center; gap: 4px; }
.ws-legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }

/* ===== 底部关键统计 ===== */
.ws-mod-foot {
  display: flex;
  gap: 0;
  align-items: center;
  justify-content: space-around;
  padding-top: 9px;
  margin-top: 8px;
  border-top: 1px solid rgba(34, 228, 238, 0.2);
}
.ws-kpi {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  position: relative;
  flex: 0 0 auto;
  padding: 0 14px;
}
.ws-kpi + .ws-kpi::before {
  content: '';
  position: absolute;
  left: 0;
  top: 12%;
  bottom: 12%;
  width: 1px;
  background: rgba(143, 185, 204, 0.24);
}
.ws-kpi--grow {
  flex: 1;
  min-width: 0;
  align-items: flex-start;
  padding-left: 16px;
}
.ws-kpi span { font-size: 14px; color: #7fa8bd; }
.ws-kpi b { font-size: 19px; color: #e1f7ff; line-height: 1.2; }
.ws-kpi b.is-ok { color: #39d98a; }
.ws-kpi b.is-bad { color: #ff6b6b; }
.ws-kpi b.is-warn { color: #ffb547; }
.ws-srcchips { display: flex; gap: 5px; flex-wrap: wrap; }
.ws-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 7px;
  font-size: 14px;
  color: #cfeaf5;
  background: rgba(4, 25, 46, 0.7);
  border: 1px solid rgba(143, 185, 204, 0.24);
  border-radius: 9px;
}
.ws-chip i { width: 6px; height: 6px; border-radius: 50%; background: var(--c); }
</style>
