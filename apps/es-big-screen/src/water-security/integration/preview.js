import {loadDemoData, getDemoSnapshot, getScriptedState, pathD, formatTime, clampTime} from './demo-engine.js';
const $ = id => document.getElementById(id);
const state = {time:45000, playing:false, speed:1, selected:'T001', mode:'overview', follow:false, scripted:false, layers:{coverage:true,source_tracks:true,fused_tracks:true}};
const visible = t => ['overview','replay'].includes(state.mode) || t.domain === state.mode;
let data, snapshot, lastPanelUpdate=-Infinity, previousTime;
const image = (src,x,y,w,h,extra='') => `<image href="${src}" x="${x}" y="${y}" width="${w}" height="${h}" ${extra}/>`;
const label = (text,x,y,color='#38e5eb') => `<g><rect x="${x-80}" y="${y-24}" width="160" height="29" rx="3" fill="#031a2ee6" stroke="${color}"/><text x="${x}" y="${y-4}" text-anchor="middle" fill="${color}" font-size="18">${text}</text></g>`;
function fit() {const s=Math.min(innerWidth/1920,innerHeight/1080);$('screen').style.transform=`scale(${s})`;$('screen').style.left=`${(innerWidth-1920*s)/2}px`;$('screen').style.top=`${(innerHeight-1080*s)/2}px`;}
addEventListener('resize',fit);fit();
function setTime(t) {state.time=clampTime(data,t);if(state.scripted){const s=getScriptedState(data,state.time);state.selected=s.selected_target_id;state.mode=s.mode;}lastPanelUpdate=-Infinity;}
function choose(id) {state.selected=id;state.scripted=false;lastPanelUpdate=-Infinity;}
function setMode(mode){state.mode=mode;state.scripted=false;lastPanelUpdate=-Infinity;}
function renderWorld() {
 const scene=$('scene');
 if(!scene.dataset.built){
  const parts=[`<defs><linearGradient id="beam"><stop stop-color="#24ffff" stop-opacity=".6"/><stop offset="1" stop-color="#00ddec" stop-opacity="0"/></linearGradient></defs><g id="coverage-group"><path class="ws-sweep" d="M547 91 1000 12 1060 232Z" fill="url(#beam)"/><path class="ws-wave" d="M730 189 890 315 1060 293Z" fill="#ffdf79" opacity=".2"/>`];
  for(const n of data.devices.items.find(d=>d.source_id==='S01').display_nodes){let x=n.view_u*1920,y=n.view_v*700;parts.push(image('assets/ui/sonar_cone.svg',x-190,y,380,210,'class="ws-wave"'),image('assets/sprites/sonar_head.svg',x-20,y-24,40,45),label(n.id,x,y-27));}
  parts.push('</g>',image('assets/sprites/radar_tower.svg',487,72,120,220),label('雷达 R01',547,75),image('assets/sprites/optical_camera.svg',671,158,120,100),label('光电 E01',730,159));
  for(const t of data.targets.items){
   const id=t.global_track_id,w=t.sprite_width_design_px,color=t.domain==='underwater'?'#ffd54a':'#28eae5';
   parts.push(`<g id="tracks-${id}"><g id="sources-${id}">${t.sources.map(s=>`<path id="source-${id}-${s}" data-layer="source_tracks" class="ws-track ws-track--source"/>`).join('')}</g><path id="fused-${id}" data-layer="fused_tracks" class="ws-track ws-flow ${t.domain==='underwater'?'ws-track--underwater':''}"/></g>`);
   parts.push(`<g id="target-${id}" data-target="${id}" role="button" tabindex="0" aria-label="选择 ${id}" style="cursor:pointer"><rect x="${-w/2}" y="${-w/3}" width="${w}" height="${w*2/3}" fill="transparent"/>${image(t.sprite,-w/2,-w/3,w,w*2/3)}<g id="bracket-${id}">${image(`assets/ui/target_bracket_${t.domain==='underwater'?'yellow':'cyan'}.svg`,-w*.42,-w*.24,w*.84,w*.46,'class="selected-bracket"')}</g>${label((t.domain==='surface'?'水面目标 ':'水下目标 ')+id,0,-w*.29,color)}</g>`);
  }
  scene.innerHTML=parts.join('');scene.dataset.built='true';
 }
 $('coverage-group').style.display=state.layers.coverage?'':'none';
 for(const meta of data.targets.items){
  const id=meta.global_track_id,t=snapshot.targets.find(x=>x.global_track_id===id),show=!!t&&visible(t);
  $('target-'+id).style.display=show?'':'none';$('tracks-'+id).style.display=show?'':'none';
  if(!show)continue;
  $('target-'+id).setAttribute('transform',`translate(${t.view_u*1920},${t.view_v*700})`);
  $('bracket-'+id).style.display=state.selected===id?'':'none';
  $('sources-'+id).style.display=state.layers.source_tracks?'':'none';
  for(const source of t.source_histories)$(`source-${id}-${source.source_id}`).setAttribute('d',pathD(source.history));
  $('fused-'+id).style.display=state.layers.fused_tracks?'':'none';$('fused-'+id).setAttribute('d',pathD(t.history));
 }
 const selected=snapshot.targets.find(t=>t.global_track_id===state.selected);
 $('world').style.transform=state.follow&&selected?`translate(${(0.5-selected.view_u)*1920*.8}px,${(0.5-selected.view_v)*850*.5}px) scale(1.12)`:'none';
}
function renderPanels(){
 $('target-count').textContent=`${snapshot.targets.length} 个模拟目标`;
 const listKey=snapshot.targets.map(t=>t.global_track_id).join(',')+'|'+state.selected;
 if($('targets').dataset.key!==listKey){
  $('targets').innerHTML=snapshot.targets.map(t=>`<button class="target-row ${state.selected===t.global_track_id?'selected':''}" data-target="${t.global_track_id}" aria-pressed="${state.selected===t.global_track_id}"><img src="assets/icons/${t.domain==='underwater'?'diver':'ship'}.svg" alt=""><span>${t.global_track_id}</span><small>${t.target_type}</small><span class="arrow">›</span></button>`).join('');
  $('targets').dataset.key=listKey;
 }
 const t=snapshot.targets.find(x=>x.global_track_id===state.selected);
 $('details').innerHTML=t?`<table class="details-table"><tr><td>统一编号</td><td>${t.global_track_id}</td></tr><tr><td>目标类型</td><td>${t.target_type}</td></tr><tr><td>数据来源</td><td>${t.sources.map(s=>({R01:'雷达',A01:'AIS',S01:'声纳',E01:'光电'}[s])).join(' / ')}</td></tr><tr><td>关联状态</td><td class="ok">${t.association_status==='confirmed'?'已关联 · 演示':'单源跟踪 · 演示'}</td></tr><tr><td>场景坐标</td><td>E ${t.east_m.toFixed(1)} / N ${t.north_m.toFixed(1)} m</td></tr></table><div class="detail-actions"><button class="ws-button" data-action="locate"><img src="assets/icons/location.svg" alt="">定位目标</button><button class="ws-button" data-action="track"><img src="assets/icons/track.svg" alt="">查看轨迹</button></div>`:'<p class="empty-state">当前回放时刻没有该目标。</p>';
 const events=snapshot.events.slice(-2).reverse();
 $('events').innerHTML=events.map(e=>`<button class="event-row ${e.level==='warning'?'warning':''}" data-event="${e.event_id}"><span class="event-time">${formatTime(e.time_ms)}</span><strong>${e.title}</strong><small>${e.description}</small></button>`).join('')||'<p class="empty-state">暂无事件</p>';
 const f=snapshot.sonar_frame;
 if($('sonar-image').getAttribute('src')!==f.image_url)$('sonar-image').src=f.image_url;
 $('sonar-annotations').innerHTML=f.detections.map(d=>{const[x,y,w,h]=d.bbox_xywh;return `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="none" stroke="#ffd34b" stroke-width="2"/><path d="M${x+w} ${y}l20-20h52" fill="none" stroke="#ffd34b"/><text x="${x+w+21}" y="${y-24}" fill="#ffd34b" font-size="16">${d.target_id}</text>`}).join('');
 $('frame-label').textContent=f.frame_id;$('frame-time').textContent=`帧时间 ${formatTime(f.time_ms)}`;
 document.querySelectorAll('[data-mode]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.mode===state.mode));
 $('follow').setAttribute('aria-pressed',state.follow);$('demo').setAttribute('aria-pressed',state.scripted);
 $('play').setAttribute('aria-label',state.playing?'暂停':'播放');if($('play').dataset.playing!==String(state.playing)){$('play').innerHTML=`<img src="assets/icons/${state.playing?'pause':'play'}.svg" alt="">`;$('play').dataset.playing=String(state.playing);}
}
function tick(now){
 if(previousTime!==undefined&&state.playing){state.time=clampTime(data,state.time+Math.min(250,now-previousTime)*state.speed);if(state.scripted){const s=getScriptedState(data,state.time);state.selected=s.selected_target_id;state.mode=s.mode;}if(state.time>=data.scene.duration_ms)state.playing=false;}
 previousTime=now;snapshot=getDemoSnapshot(data,state.time);renderWorld();
 $('clock').value=formatTime(state.time);$('seek').value=String(state.time);
 if(lastPanelUpdate===-Infinity || (state.playing && now-lastPanelUpdate>200)){renderPanels();lastPanelUpdate=now;}
 requestAnimationFrame(tick);
}
document.addEventListener('click',e=>{
 const target=e.target.closest('[data-target]');if(target){choose(target.dataset.target);return;}
 const mode=e.target.closest('[data-mode]');if(mode){setMode(mode.dataset.mode);return;}
 const ev=e.target.closest('[data-event]');if(ev){const item=data.events.items.find(x=>x.event_id===ev.dataset.event);state.playing=false;setTime(item.time_ms);if(item.target_id)choose(item.target_id);return;}
 const action=e.target.closest('[data-action]');if(action?.dataset.action==='locate'){state.follow=true;lastPanelUpdate=-Infinity;}if(action?.dataset.action==='track'){state.layers.fused_tracks=true;$('fused_tracks').checked=true;}
});
$('play').addEventListener('click',()=>{if(state.time>=120000)setTime(0);state.playing=!state.playing;lastPanelUpdate=-Infinity;});
$('speed').addEventListener('change',e=>state.speed=Number(e.target.value));
$('seek').addEventListener('input',e=>setTime(Number(e.target.value)));
$('overview').addEventListener('click',()=>{setMode('overview');state.follow=false;});
$('water-switch').addEventListener('click',()=>setMode(state.mode==='underwater'?'surface':'underwater'));
$('follow').addEventListener('click',()=>{state.follow=!state.follow;lastPanelUpdate=-Infinity;});
$('demo').addEventListener('click',()=>{state.scripted=true;state.follow=false;setTime(0);state.playing=true;lastPanelUpdate=-Infinity;});
$('scene').addEventListener('keydown',e=>{const target=e.target.closest('[data-target]');if(target&&['Enter',' '].includes(e.key)){e.preventDefault();choose(target.dataset.target);}});
for(const key of Object.keys(state.layers))$(key).addEventListener('change',e=>state.layers[key]=e.target.checked);
try{
 data=await loadDemoData('.');
 $('devices').innerHTML=data.devices.items.map(d=>`<div class="device-row"><img src="assets/icons/${d.kind}.svg" alt=""><span>${d.label}</span><span class="ws-tag">${d.state_label}</span></div>`).join('');
 window.demoPreview={state,data,getSnapshot:()=>snapshot,seek:setTime};
 requestAnimationFrame(tick);
}catch(error){$('error').hidden=false;$('error').textContent=`无法载入素材数据：${error.message}\n请运行 start_preview.bat 或 python tools/serve_preview.py --open，避免用 file:// 直接打开。`;console.error(error);}
