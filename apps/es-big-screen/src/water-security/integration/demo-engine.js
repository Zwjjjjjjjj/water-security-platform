/** Deterministic replay helpers; no timers, network requests or global mutable state. */
export const DATA_FILES = ['scene', 'targets', 'tracks', 'devices', 'events', 'timeline', 'sonar_frames'];
export async function loadDemoData(base = '.') {
  const entries = await Promise.all(DATA_FILES.map(async name => {
    const response = await fetch(`${base.replace(/\/$/, '')}/data/${name}.json`);
    if (!response.ok) throw new Error(`${name}.json: HTTP ${response.status}`);
    return [name, await response.json()];
  }));
  return Object.fromEntries(entries);
}
export function clampTime(data, time) {
  return Math.min(data.scene.duration_ms, Math.max(0, Number(time) || 0));
}
export function interpolate(samples, time) {
  if (!samples.length) return null;
  if (time <= samples[0].time_ms) return {...samples[0]};
  if (time >= samples.at(-1).time_ms) return {...samples.at(-1)};
  let lo = 0, hi = samples.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (samples[mid].time_ms <= time) lo = mid; else hi = mid; }
  const a = samples[lo], b = samples[hi], k = (time - a.time_ms) / (b.time_ms - a.time_ms);
  const point = {...a, time_ms: time};
  /* 【depth_m 为什么必须插值】
   * 水下目标（UUV / 蛙人 / 潜航器）的关键观测量就是**深度** ——
   * 声纳浮标给出的就是「水下多深、什么方位」这一对量。
   * 若深度不插值，回放时深度会阶梯式跳变（每帧才更新一次），
   * 画面上的深度分层与深度标签就会「一格一格地跳」，完全不像连续航迹。
   *
   * 因此这里把 depth_m 加入线性插值列表。
   * 深度为负表示在水面以下（向下为正），线性插值对它同样成立。 */
  for (const key of ['east_m', 'north_m', 'view_u', 'view_v', 'speed_mps', 'lat', 'lon', 'latitude_deg', 'longitude_deg', 'bearing_deg', 'range_m', 'depth_m']) {
    if (a[key] == null || b[key] == null) { if (point[key] == null) point[key] = a[key] ?? b[key]; continue; }
    point[key] = a[key] + (b[key] - a[key]) * k;
  }
  // course is circular: pick nearest branch then normalize
  if (a.course_deg != null && b.course_deg != null) {
    let d = b.course_deg - a.course_deg;
    if (d > 180) d -= 360; if (d < -180) d += 360;
    point.course_deg = ((a.course_deg + d * k) % 360 + 360) % 360;
  } else {
    point.course_deg = a.course_deg ?? b.course_deg ?? null;
  }
  return point;
}
export function historyAt(samples, time, activeFrom = 0) {
  if (time < activeFrom) return [];
  const result = samples.filter(p => p.time_ms >= activeFrom && p.time_ms <= time).map(p => ({...p}));
  if (!result.length || result.at(-1).time_ms < time) result.push(interpolate(samples, time));
  return result;
}
export function getDemoSnapshot(data, time) {
  const time_ms = clampTime(data, time);
  const scene = data.scene || {};
  const duration_ms = scene.duration_ms ?? 302000;
  const data_mode = scene.data_mode ?? 'field';
  const session_id = scene.session_id ?? 'yantai-0305-413203610';
  const targets = data.targets.items.filter(t => time_ms >= t.active_from_ms && time_ms <= t.active_until_ms).map(t => {
    const track = data.tracks.items.find(x => x.global_track_id === t.global_track_id);
    return {...t, ...interpolate(track.samples, time_ms), history: historyAt(track.samples, time_ms, t.active_from_ms),
      source_histories: track.source_tracks.map(s => ({source_id: s.source_id, history: historyAt(s.samples, time_ms, t.active_from_ms)})),
      /**
       * 【完整航迹 —— 整条实测轨迹】
       *
       * `history` 是「当前时刻之前」的实测航迹，随回放推进而增长；
       * `full_track` 是该目标的**整条**轨迹，一次性给全。
       *
       * 两处都要用它，理由不同：
       *
       * 1. 水下场景 —— 表现「目标沿完整航迹巡弋」：
       *    折线画完整路径，符号按当前回放时刻落在路径上。
       *    若两者都用 `history`，符号会永远钉在折线末端，
       *    表现为「只有一小段轨迹、符号在旁边小幅晃动」。
       *
       * 2. 水面场景 —— 计算**稳定的**可航航线：
       *    船的显示位置要投影到自己的可航航线上（`displayPositionOnRoute`），
       *    而航线若只用 `history` 就会随回放推进反复重算 A*，
       *    路径一变、船就跟着「跳」一下（实测 T002 末段一次跳 114px）。
       *    用整条轨迹算一次，航线从头到尾稳定，船不再瞬移。
       */
      full_track: track.samples
        .filter(p => p.time_ms >= t.active_from_ms)
        .map(p => ({...p}))};
  });
  const frame = data.sonar_frames.frames.filter(f => f.time_ms <= time_ms).at(-1) || data.sonar_frames.frames[0];
  return {session_id, time_ms, duration_ms, data_mode, targets,
    origin: scene.origin || { lat: 37.53297, lon: 121.4199 },
    station: { lat: 37.53297, lon: 121.4199 },
    devices: data.devices.items, sonar_frame: frame, events: data.events.items.filter(e => e.time_ms <= time_ms)};
}
/**
 * 读取脚本化演示状态。
 *
 * 【关于 mode 的重要约定】
 * `mode` 表示「脚本要求切换到哪个页面」。
 * 这里默认是 **null 而不是 'overview'** ——
 * 因为 timeline.json 里目前只有 select_target / show_event 关键帧，
 * 没有 set_mode。若默认填 'overview'，每次读状态都会把用户
 * 从「水面监视」强行拽回「综合态势」，页面自己会莫名其妙地跳走。
 *
 * 只有真正存在 set_mode 关键帧时，mode 才是有值的字符串。
 */
export function getScriptedState(data, time) {
  const result = {selected_target_id: 'T001', mode: null};
  for (const key of data.timeline.keyframes) {
    if (key.time_ms > time) break;
    if (key.action === 'select_target') result.selected_target_id = key.target_id;
    if (key.action === 'set_mode') result.mode = key.mode;
  }
  return result;
}
export function pathD(points, width = 1920, height = 700) {
  return points.map((p, i) => `${i ? 'L' : 'M'}${(p.view_u * width).toFixed(2)},${(p.view_v * height).toFixed(2)}`).join(' ');
}
export function formatTime(ms) {
  const seconds = Math.floor(ms / 1000); return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}
