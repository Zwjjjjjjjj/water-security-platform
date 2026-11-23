<script setup lang="ts">

/** 模块标题图标 */
const MOD_ICON =
  '<path d="M12 3l8 4.2-8 4.2-8-4.2z" fill="currentColor" opacity="0.9"/><path d="M4 12l8 4.2 8-4.2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M4 16.6l8 4.2 8-4.2" fill="none" stroke="currentColor" stroke-width="1.8" opacity="0.6"/>'

/**
 * 模块 1：数据标准化
 *
 * 【效果图版式】
 *   左列：四个感知源卡片（雷达/AIS/声纳/光电），每个带协议徽章
 *   中列：解析 → 映射 → 校验 三个流程节点，用连接线串起
 *   右列：标准化记录表（字段 / 值 / 有效性校验）
 *   底部：关键统计条
 *
 * 【数据来源】pipeline.buildStdModule()，全部来自 tracks.json 实测样本。
 */
import { computed } from 'vue'
import { KIND_LABEL, KIND_PROTOCOL, KIND_RAW_EXT, type StdModule } from '../pipeline'
import { PALETTE, KIND_COLOR } from '../chart-utils'

const props = defineProps<{
  mod: StdModule
  selectedTargetId: string | null
  selectedRecordId: string | null
  sourceFilter: string | null
}>()

const emit = defineEmits<{
  (e: 'select-record', id: string): void
  (e: 'select-target', id: string): void
  (e: 'open-detail', id: string): void
  (e: 'filter-source', id: string | null): void
}>()

/** 四个源（固定顺序，与效果图一致）；sourceId 从实际数据里取 */
const SOURCES = [
  { kind: 'radar', name: '雷达', proto: 'ASTR', ext: 'rdat' },
  { kind: 'ais', name: 'AIS', proto: 'NMEA', ext: 'ais' },
  { kind: 'sonar', name: '声纳', proto: 'XTF', ext: 'xtf' },
  { kind: 'eo', name: '光电', proto: 'RTSP', ext: 'mp4' },
]

/** 每类传感器对应的实际设备编号 */
function sourceIdOf(kind: string): string | null {
  return props.mod.bySource.find((r) => r.kind === kind)?.sourceId ?? null
}

/**
 * 四个感知源的图标（内联 SVG，与效果图一致）。
 * 雷达=同心弧, AIS=天线塔, 声纳=波纹线, 光电=相机。
 */
const ICONS: Record<string, string> = {
  radar:
    '<circle cx="12" cy="12" r="2.4" fill="currentColor"/>' +
    '<path d="M7.4 12a4.6 4.6 0 0 1 9.2 0" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<path d="M4.6 12a7.4 7.4 0 0 1 14.8 0" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<path d="M1.9 12a10.1 10.1 0 0 1 20.2 0" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.55"/>',
  ais:
    '<path d="M12 20V8" stroke="currentColor" stroke-width="1.6" fill="none"/>' +
    '<path d="M7 20h10" stroke="currentColor" stroke-width="1.6"/>' +
    '<circle cx="12" cy="6" r="2" fill="currentColor"/>' +
    '<path d="M6.2 9.2a7.4 7.4 0 0 1 11.6 0" fill="none" stroke="currentColor" stroke-width="1.4" opacity="0.7"/>' +
    '<path d="M3.4 5.6a11.4 11.4 0 0 1 17.2 0" fill="none" stroke="currentColor" stroke-width="1.4" opacity="0.45"/>',
  sonar:
    '<path d="M4 8.5c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M4 13c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.6" opacity="0.75"/>' +
    '<path d="M4 17.5c2.6-2.2 5.2-2.2 7.8 0s5.2 2.2 7.8 0" fill="none" stroke="currentColor" stroke-width="1.6" opacity="0.5"/>',
  eo:
    '<rect x="2.5" y="6.5" width="14" height="11" rx="1.6" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M16.5 11.2l4.2-2.6v6.8l-4.2-2.6z" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle cx="8" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle cx="8" cy="12" r="0.9" fill="currentColor"/>',
}

/** 每个源的数据统计 */
const sourceStats = computed(() => {
  const m = new Map<string, { valid: number; invalid: number; total: number }>()
  for (const s of props.mod.totals) {
    m.set(s.sourceId, { valid: s.valid, invalid: s.invalid, total: s.total })
  }
  return m
})

/** 当前展示的记录（受源筛选影响） */
const records = computed(() => {
  let list = props.mod.records
  if (props.sourceFilter) list = list.filter((r) => r.sourceId === props.sourceFilter)
  return list
})

/** 选中记录（优先选中项，否则第一条） */
const active = computed(() => {
  const sel = props.mod.records.find((r) => r.id === props.selectedRecordId)
  return sel ?? records.value[0] ?? null
})

/** 标准化记录表 —— 显示实际字段值 */
const stdRows = computed(() => {
  const a = active.value
  if (!a) return []
  return a.fields
    .filter((f) => f.key !== 'source_id')
    .map((f) => ({
      key: f.key,
      value: f.value,
      ok: f.ok,
      note: f.note,
    }))
})

/** 流程三节点 */
const chain = computed(() => {
  const total = props.mod.totals.reduce((a, x) => a + x.total, 0)
  const valid = props.mod.totals.reduce((a, x) => a + x.valid, 0)
  const invalid = props.mod.totals.reduce((a, x) => a + x.invalid, 0)
  return [
    { label: '解析', value: total - invalid, color: PALETTE.blue },
    { label: '映射', value: total, color: PALETTE.cyan },
    { label: '校验', value: valid, color: PALETTE.green },
  ]
})


/** 点击源卡片 → 切换数据源筛选 */
function toggleSource(id: string | null) {
  if (!id) return
  emit('filter-source', props.sourceFilter === id ? null : id)
}
</script>

<template>
  <section class="ws-mod ws-mod--std">
    <header class="ws-mod-head">
      <h3 class="ws-mod-title">
        <span class="ws-mod-ico">
          <svg viewBox="0 0 24 24" v-html="MOD_ICON" />
        </span>
        数据标准化
        <em class="ws-mod-sub">| 统一字段 · 格式解析 · 有效性检查</em>
      </h3>
      <div class="ws-mod-right">
        <button class="ws-mod-more" type="button" title="查看详情" @click="emit('open-detail', 'std')">查看详情 ↗</button>
      </div>
    </header>

    <div v-if="!mod.hasData" class="ws-mod-empty">暂无数据</div>

    <template v-else>
      <div class="ws-std-main">
        <!-- ===== 左列：四个感知源（括号 + 协议/格式徽章） ===== -->
        <div class="ws-std-sources">
          <div class="ws-src-group">
            <button
              v-for="s in SOURCES"
              :key="s.kind"
              type="button"
              class="ws-src"
              :class="{ 'is-on': sourceFilter === sourceIdOf(s.kind) }"
              :style="{ '--c': KIND_COLOR[s.kind] }"
              :disabled="!sourceStats.size"
              @click="toggleSource(sourceIdOf(s.kind))"
            >
              <span class="ws-src-ico">
                <svg viewBox="0 0 24 24" v-html="ICONS[s.kind]" />
              </span>
              <span class="ws-src-name">{{ s.name }}</span>
              <span class="ws-src-badges">
                <em class="ws-src-proto">{{ s.proto }}</em>
                <em class="ws-src-ext">{{ s.ext }}</em>
              </span>
              <span class="ws-src-stat">
                <b>{{ sourceStats.get(sourceIdOf(s.kind) ?? '')?.total ?? 0 }}</b> 条
              </span>
            </button>
            <!-- 四个源用括号收拢，指向流程链 -->
            <span class="ws-src-bracket" aria-hidden="true"></span>
          </div>
        </div>

        <!-- ===== 中列：解析 → 映射 → 校验（横向） ===== -->
        <div class="ws-std-chain">
          <template v-for="(c, i) in chain" :key="c.label">
            <div class="ws-chain-box" :style="{ '--c': c.color }">
              <b>{{ c.value.toLocaleString() }}</b>
              <span>{{ c.label }}</span>
            </div>
            <div v-if="i < chain.length - 1" class="ws-chain-link" aria-hidden="true"></div>
          </template>
        </div>

        <!-- ===== 右列：标准化记录表（字段 / 值 + 校验） ===== -->
        <div class="ws-std-rec">
          <h4 class="ws-std-rech">
            标准化记录（示例）
            <em>{{ active?.sourceId ?? '—' }} · {{ active?.utc ?? '' }}</em>
          </h4>
          <table class="ws-std-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>值（示例）</th>
                <th class="ta-c">校验</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="f in stdRows" :key="f.key">
                <td class="k">{{ f.key }}</td>
                <td class="v">{{ f.value }}</td>
                <td class="ta-c">
                  <i v-if="f.ok" class="ws-ok">✓</i>
                  <i v-else class="ws-ng" :title="f.note">✕</i>
                </td>
              </tr>
              <tr v-if="!stdRows.length">
                <td colspan="3" class="ws-none">暂无记录</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- ===== 底部关键统计 ===== -->
      <footer class="ws-mod-foot">
        <div class="ws-kpi"><span>输入记录</span><b>{{ mod.totals.reduce((a, x) => a + x.total, 0).toLocaleString() }}</b></div>
        <div class="ws-kpi"><span>有效记录</span><b class="is-ok">{{ mod.validCount.toLocaleString() }}</b></div>
        <div class="ws-kpi"><span>异常记录</span><b :class="mod.invalidCount ? 'is-bad' : ''">{{ mod.invalidCount }}</b></div>
        <div class="ws-kpi ws-kpi--grow">
          <span>各源处理量</span>
          <div class="ws-srcchips">
            <button
              v-for="s in mod.bySource"
              :key="s.sourceId"
              type="button"
              class="ws-chip"
              :class="{ 'is-on': sourceFilter === s.sourceId }"
              :style="{ '--c': KIND_COLOR[s.kind] ?? PALETTE.cyan }"
              @click="toggleSource(s.sourceId)"
            >
              <i />{{ KIND_LABEL[s.kind] ?? s.kind }} {{ s.sourceId }}
              <em>{{ s.valid }}/{{ s.total }}</em>
            </button>
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
  justify-content: space-between;
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
.ws-mod-right { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
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

/* ===== 主体三列 ===== */
.ws-std-main {
  flex: 1;
  min-height: 0;
  display: grid;
  /* 左：感知源卡片（字号变大后需要更宽）
     中：解析→映射→校验 流程链（纵向排列，吃满高度）
     右：标准化记录表（吃满剩余宽度与高度） */
  grid-template-columns: 232px 132px minmax(0, 1fr);
  gap: 12px;
  padding-top: 8px;
}

/* ---- 左列：感知源卡片 ---- */
.ws-std-sources { display: flex; flex-direction: column; min-height: 0; }
.ws-src-group {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 8px;
  padding-right: 12px;
}
.ws-src {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 10px;
  text-align: left;
  color: #cfeaf5;
  background: linear-gradient(135deg, rgba(10, 48, 76, 0.95), rgba(5, 25, 44, 0.95));
  border: 1px solid rgba(143, 185, 204, 0.32);
  border-left: 3px solid var(--c);
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}
.ws-src:hover { border-color: var(--c); }
.ws-src.is-on {
  background: linear-gradient(135deg, rgba(12, 120, 153, 0.9), rgba(6, 66, 100, 0.9));
  border-color: var(--c);
  box-shadow: 0 0 12px rgba(34, 228, 238, 0.25);
}
.ws-src-ico {
  color: #22e4ee;
  display: flex;
  align-items: center;
  flex: 0 0 auto;
}
.ws-src-ico svg { width: 21px; height: 21px; display: block; }
.ws-src-name {
  font-size: 15px;
  font-weight: 600;
  color: #e1f7ff;
  flex: 0 0 auto;
}
.ws-src-badges { display: flex; gap: 4px; flex: 0 0 auto; }
.ws-src-proto,
.ws-src-ext {
  font-style: normal;
  font-size: 15px;
  line-height: 16px;
  padding: 0 5px;
  border-radius: 2px;
  font-family: monospace;
}
.ws-src-proto {
  color: #7fe8f2;
  background: rgba(34, 228, 238, 0.08);
  border: 1px solid rgba(34, 228, 238, 0.5);
  font-weight: 600;
  letter-spacing: 0.4px;
}
.ws-src-ext {
  color: #9fd9c9;
  background: rgba(34, 228, 238, 0.06);
  border: 1px solid rgba(34, 228, 238, 0.42);
}
.ws-src-stat {
  font-size: 15px;
  color: #7fa8bd;
  margin-left: auto;
  flex: 0 0 auto;
}
.ws-src-stat b { color: #e1f7ff; font-size: 15px; }
/* 右侧花括号：把四个源收拢，指向流程链的「解析」节点 */
.ws-src-bracket {
  position: absolute;
  right: 0;
  top: 4px;
  bottom: 4px;
  width: 9px;
  border: 1px solid rgba(143, 185, 204, 0.5);
  border-left: none;
  border-radius: 0 10px 10px 0;
}

/* ---- 中列：流程链（纵向三步，撑满列高） ---- */
.ws-std-chain {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  justify-content: space-evenly;
  gap: 4px;
  min-height: 0;
  overflow: hidden;
}
.ws-chain-box {
  width: 100%;
  padding: 9px 4px;
  min-width: 0;
  text-align: center;
  border-radius: 4px;
  background: linear-gradient(135deg, rgba(24, 150, 180, 0.92), rgba(12, 92, 122, 0.92));
  border: 1px solid rgba(34, 228, 238, 0.7);
  box-shadow: 0 0 10px rgba(34, 228, 238, 0.22);
}
.ws-chain-box b { display: block; color: #eafcff; font-size: 14px; line-height: 1.2; }
.ws-chain-box span { color: #cdeef7; font-size: 14.5px; line-height: 1.2; display: block; }
/**
 * 流程链箭头（朝下）。
 *
 * 【为什么不用旋转字符】
 * 把「→」旋转 90° 后字形会被容器裁掉、看着像个小圆点。
 * 这里用 CSS 三角形画 —— 形状精确、不受字体影响。
 */
.ws-chain-link {
  flex: 0 0 auto;
  height: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.ws-chain-link::before {
  content: '';
  width: 0;
  height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 7px solid rgba(34, 228, 238, 0.85);
}

/* ---- 右列：标准化记录表 ---- */
.ws-std-rec {
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: rgba(2, 16, 32, 0.55);
  border: 1px solid rgba(34, 228, 238, 0.16);
  border-radius: 4px;
  padding: 7px 9px;
}
.ws-std-rech {
  margin: 0 0 6px;
  font-size: 15px;
  font-weight: 600;
  color: #22e4ee;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.ws-std-rech em { font-style: normal; color: #7fa8bd; font-weight: 400; font-size: 12.5px; }
.ws-std-table {
  width: 100%;
  /* 撑满右侧列的高度，不再留大片空白 */
  flex: 1;
  border-collapse: collapse;
  font-size: 14px;
  line-height: 1.5;
  table-layout: fixed;
}
.ws-std-table th {
  text-align: left;
  color: #dff6ff;
  font-weight: 600;
  font-size: 14px;
  padding: 7px 10px;
  background: linear-gradient(180deg, #15628c, #0d4670);
  border-bottom: 1px solid rgba(34, 228, 238, 0.45);
}
.ws-std-table th.ta-c, .ws-std-table td.ta-c { text-align: center; }
.ws-std-table td { padding: 7px 10px; border-bottom: 1px solid rgba(143, 185, 204, 0.12); }
.ws-std-table td.k {
  color: #22e4ee;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 14px;
}
.ws-std-table td.v {
  color: #eaf7ff;
  font-weight: 600;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 15px;
}
.ws-ok { color: #39d98a; font-style: normal; font-size: 15px; }
.ws-ng { color: #ff6b6b; font-style: normal; font-size: 14.5px; }
.ws-none { color: #7fa8bd; text-align: center; padding: 14px !important; }

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
  cursor: pointer;
}
.ws-chip i { width: 6px; height: 6px; border-radius: 50%; background: var(--c); }
.ws-chip em { font-style: normal; color: #7fa8bd; }
.ws-chip.is-on { border-color: var(--c); background: rgba(34, 228, 238, 0.12); }
</style>
