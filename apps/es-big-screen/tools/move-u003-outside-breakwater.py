#!/usr/bin/env python3
"""
生成 U003（微型潜航器）的**堤外外海**航迹并写回 tracks.json / targets.json。

【地形事实 —— 全部用底图水域掩码核验】
1. 主防波堤堤体 (883,698)→(793,716)→(731,729) 及堤头延伸段，
   在掩码里是**一整块连续陆地**，横跨 x 690~890、y 698~780，**没有堤口**。
   → 港池与外海完全隔开，U003 无法「穿过」堤坝。
2. 绕堤可行但代价大：BFS 最短路 1932 m，
   而 U003 速度 2.6 m/s × 282 s 时间窗只有 733 m，
   绕堤会把它压慢到 0.9 m/s（不像微型潜航器）。
3. 外海侧存在一条干净的东西向水道：v ≈ 0.76~0.80（y 788~820），
   全程为水，且多段落在声纳站 S01-3 的波圈内。

【结论】
U003 **整条航迹都在堤外外海**作业 —— 既满足「一个目标到堤坝外围」的要求，
又符合真实逻辑（微型潜航器在防波堤外侧做水下测绘/巡检），
且保持物理速度 2.6 m/s 不被压缩。

【航迹设计】（底图归一化坐标）
  东端堤外入水 → 沿堤外水道向西南巡弋 → 折返向东
  全部锚点掩码核验为水；大部分区段在 S01-3 声纳圈内。
"""
import base64, json, math, os, re, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "public", "water-security", "data")
MASK_COLS, MASK_ROWS, MASK_CELL = 192, 128, 8
MAP_W, MAP_H = 1536, 1024
E_MIN, E_MAX = -2600.0, 1000.0
N_MIN, N_MAX = 2900.0, 5100.0
A = MAP_W / (E_MAX - E_MIN)
D = -(1000.0 - 40.0) / (N_MAX - N_MIN)
E_OFF = 0.0 - A * E_MIN
F = 40.0 - D * N_MAX

def enu_to_px(e, n): return (A*e + E_OFF, D*n + F)
def px_to_enu(x, y):
    det = A*D; dx = x-E_OFF; dy = y-F
    return ((D*dx)/det, (A*dy)/det)
def px_to_enu_uv(u, v): return px_to_enu(u*MAP_W, v*MAP_H)
def u_from_enu(e, n):
    x, y = enu_to_px(e, n); return (x/MAP_W, y/MAP_H)

src = open(os.path.join(ROOT, "src/water-security/integration/harbor-water.ts"), encoding="utf-8").read()
i = src.find("const MASK_B64"); j = src.find(";", i)
raw = base64.b64decode("".join(re.findall(r"'([^']*)'", src[i:j])))
G = bytearray(MASK_COLS*MASK_ROWS)
for k in range(len(G)):
    G[k] = (raw[k >> 3] >> (7 - (k & 7))) & 1

def is_water_px(x, y):
    cx, cy = int(x//MASK_CELL), int(py//MASK_CELL)
    if cx<0 or cy<0 or cx>=MASK_COLS or cy>=MASK_ROWS: return False
    return G[cy*MASK_COLS+cx] == 1
def is_water_enu(e, n): return is_water_px(*enu_to_px(e, n))

def clearance_field():
    INF=255; d=bytearray([INF])*(MASK_COLS*MASK_ROWS)
    for i in range(len(d)):
        if G[i]!=1: d[i]=0
    for y in range(MASK_ROWS):
        for x in range(MASK_COLS):
            i=y*MASK_COLS+x
            if d[i]==0: continue
            b=d[i]
            if x>0: b=min(b,d[i-1]+1)
            if y>0: b=min(b,d[i-MASK_COLS]+1)
            if x>0 and y>0: b=min(b,d[i-MASK_COLS-1]+1.414)
            if x<MASK_COLS-1 and y>0: b=min(b,d[i-MASK_COLS+1]+1.414)
            d[i]=min(INF,int(round(b)))
    for y in range(MASK_ROWS-1,-1,-1):
        for x in range(MASK_COLS-1,-1,-1):
            i=y*MASK_COLS+x
            if d[i]==0: continue
            b=d[i]
            if x<MASK_COLS-1: b=min(b,d[i+1]+1)
            if y<MASK_ROWS-1: b=min(b,d[i+MASK_COLS]+1)
            if x<MASK_COLS-1 and y<MASK_ROWS-1: b=min(b,d[i+MASK_COLS+1]+1.414)
            if x>0 and y<MASK_ROWS-1: b=min(b,d[i+MASK_COLS-1]+1.414)
            d[i]=min(INF,int(round(b)))
    return d
CLEAR=clearance_field()
def clearance_enu(e,n):
    x,y=enu_to_px(e,n)
    cx=min(MASK_COLS-1,max(0,int(x//MASK_CELL))); cy=min(MASK_ROWS-1,max(0,int(y//MASK_CELL)))
    return CLEAR[cy*MASK_COLS+cx]

SONAR_NODES=[(0.40,0.568),(0.53,0.565),(0.66,0.557)]
SONAR_PX=[(u*MAP_W,v*MAP_H) for u,v in SONAR_NODES]
WAVE_R=MAP_W*0.17
def sonar_hit(px,py):
    return any(math.hypot(px-sx,py-sy)<=WAVE_R for sx,sy in SONAR_PX)

# ---------------------------------------------------------------- 航迹锚点
# 【全部在堤外（v ≥ 0.755）】—— 堤体最大 v = 780/1024 = 0.762，
# 但堤体在 x 690~890；U003 走的是 x 500~1080 的东西向水道，
# 在 x>940 与 x<690 两段都是纯外海。
U003_UV = [
    (0.684, 0.768),   # 堤外东端（S01-3 波圈内）
    (0.666, 0.790),
    (0.636, 0.798),   # 外海
    (0.602, 0.796),
    (0.572, 0.788),   # 堤头正南（堤头 x=731, y=729；此点在 y=807，已在其南侧）
    (0.542, 0.788),
    (0.516, 0.798),   # 堤头西南侧
    (0.540, 0.810),
]

print("=== U003 锚点核验 ===")
anchors=[]
for u,v in U003_UV:
    e,n = px_to_enu_uv(u,v)
    px,py = enu_to_px(e,n)
    w=is_water_px(px,py); cl=clearance_enu(e,n); sh=sonar_hit(px,py)
    print(f"  ({u:.3f},{v:.3f}) px=({px:6.1f},{py:6.1f}) water={w!s:5} clear={cl:2d} sonar={sh!s:5}")
    if not w:
        raise SystemExit(f"锚点 ({u},{v}) 落在陆地上，需调整")
    anchors.append((e,n))

# ---------------------------------------------------------------- 平滑 + 重采样
def catmull_rom(p0,p1,p2,p3,t):
    t2,t3=t*t,t*t*t
    return tuple(0.5*((2*p1[i])+(-p0[i]+p2[i])*t+
                       (2*p0[i]-5*p1[i]+4*p2[i]-p3[i])*t2+
                       (-p0[i]+3*p1[i]-3*p2[i]+p3[i])*t3) for i in range(2))
def smooth_path(anchors, per=14):
    ext=[anchors[0]]+list(anchors)+[anchors[-1]]
    out=[]
    for i in range(len(anchors)-1):
        p0,p1,p2,p3=ext[i],ext[i+1],ext[i+2],ext[i+3]
        for s in range(per):
            out.append(catmull_rom(p0,p1,p2,p3,s/per))
    out.append(anchors[-1]); return out
def resample(path, step_m=18.0):
    cum=[0.0]
    for i in range(1,len(path)):
        cum.append(cum[-1]+math.hypot(path[i][0]-path[i-1][0],path[i][1]-path[i-1][1]))
    total=cum[-1]
    if total<=0: return list(path)
    n=max(2,int(total/step_m)+1); out=[]; j=0
    for k in range(n):
        s=total*k/(n-1)
        while j<len(cum)-2 and cum[j+1]<s: j+=1
        seg=cum[j+1]-cum[j]; t=0 if seg<=0 else (s-cum[j])/seg
        out.append((path[j][0]+(path[j+1][0]-path[j][0])*t,
                    path[j][1]+(path[j+1][1]-path[j][1])*t))
    return out

path = resample(smooth_path(anchors), 18.0)
# 落陆点小修正（仅在落陆时挪到最近水域，不改变走向）
def nudge(e,n,radius=3):
    if is_water_enu(e,n): return (e,n)
    x,y=enu_to_px(e,n); cx,cy=int(x//8),int(y//8)
    best,bd=None,None
    for dy in range(-radius,radius+1):
        for dx in range(-radius,radius+1):
            gx,gy=cx+dx,cy+dy
            if gx<0 or gy<0 or gx>=MASK_COLS or gy>=MASK_ROWS: continue
            if G[gy*MASK_COLS+gx]!=1: continue
            d=math.hypot(dx,dy)
            if bd is None or d<bd:
                bd,best=d,px_to_enu((gx+0.5)*8,(gy+0.5)*8)
    return best if best else (e,n)
path=[nudge(e,n) for e,n in path]

bad=sum(1 for p in path if not is_water_enu(*p))
segbad=0
for k in range(len(path)-1):
    a,b=path[k],path[k+1]
    steps=max(2,int(math.hypot(b[0]-a[0],b[1]-a[1])/4))
    if any(not is_water_enu(a[0]+(b[0]-a[0])*s/steps, a[1]+(b[1]-a[1])*s/steps) for s in range(steps+1)):
        segbad+=1
L=sum(math.hypot(path[i][0]-path[i-1][0],path[i][1]-path[i-1][1]) for i in range(1,len(path)))
us=[u_from_enu(e,n)[0] for e,n in path]; vs=[u_from_enu(e,n)[1] for e,n in path]
son=sum(1 for e,n in path if sonar_hit(*enu_to_px(e,n)))
clears=[clearance_enu(e,n) for e,n in path]

print()
print("=== 结果 ===")
print(f"  点数 {len(path)}  落陆点 {bad}  穿陆段 {segbad}/{len(path)-1}")
print(f"  离岸余量 min/avg = {min(clears)}/{sum(clears)/len(clears):.1f}")
print(f"  航程 = {L:.0f} m → 2.6 m/s 需 {L/2.6:.0f} 秒")
print(f"  view u {min(us):.4f}~{max(us):.4f}  v {min(vs):.4f}~{max(vs):.4f}")
print(f"  声纳圈内 {son}/{len(path)} ({son/len(path)*100:.0f}%)")

# 与 U001/U002 对比
kdata=json.load(open(os.path.join(DATA,"tracks.json"),encoding="utf-8"))
for tid in ("U001","U002"):
    s=next(t for t in kdata["items"] if t["global_track_id"]==tid)["samples"]
    print(f"  对比 {tid}: view u {min(x['view_u'] for x in s):.4f}~{max(x['view_u'] for x in s):.4f}"
          f"  v {min(x['view_v'] for x in s):.4f}~{max(x['view_v'] for x in s):.4f}")
print(f"  U003 新: view u {min(us):.4f}~{max(us):.4f}  v {min(vs):.4f}~{max(vs):.4f}")

# ---------------------------------------------------------------- 写回
for f in ("tracks.json","targets.json"):
    srcp=os.path.join(DATA,f)
    if os.path.exists(srcp+".bak"):
        os.remove(srcp+".bak")
    shutil.copy(srcp, srcp+".bak")

V_CRUISE, MAX_SPEED = 2.6, 4.0
T0, T1 = 20000, 302000
n=len(path)
seg_len=[0.0]
for i in range(1,n):
    seg_len.append(seg_len[-1]+math.hypot(path[i][0]-path[i-1][0],path[i][1]-path[i-1][1]))
total_len=seg_len[-1] or 1.0
travel_ms=(total_len/V_CRUISE)*1000.0
window=float(T1-T0)
v_eff=max(total_len/(window/1000.0),0.15) if travel_ms>window else V_CRUISE

times=[]; acc=0.0
for i in range(n):
    times.append(int(round(T0+acc)))
    if i<n:
        step=math.hypot(path[i+1][0]-path[i][0],path[i+1][1]-path[i][1]) if i+1<n else 0.0
        acc+=step/v_eff*1000.0

samples=[]; prev=None
for i,(e,n_m) in enumerate(path):
    u,v=u_from_enu(e,n_m)
    if prev is not None:
        dist=math.hypot(e-prev[0],n_m-prev[1])
        dt=max(0.001,(times[i]-times[i-1])/1000.0)
        speed=min(dist/dt,MAX_SPEED)
        course=(math.degrees(math.atan2(e-prev[0],n_m-prev[1])))%360
    else:
        speed, course = 0.0, 0.0
    prev=(e,n_m)
    samples.append({
        "time_ms":times[i], "east_m":round(e,3), "north_m":round(n_m,3), "depth_m":None,
        "view_u":round(u,6), "view_v":round(v,6),
        "speed_mps":round(speed,2), "course_deg":round(course,1),
        "lat":None,"lon":None,"latitude_deg":None,"longitude_deg":None,
        "bearing_deg":None,"range_m":None,
        "update_status":"measured","data_mode":"simulated","kind":"sonar_track",
    })

# 深度剖面：weave（外海侧更深：28m → 12m，中途起伏）
d_start,d_end=28.0,12.0
depths=[]
for i in range(n):
    t=i/max(1,n-1)
    base=(d_start+d_end)/2+(d_end-d_start)/2*math.sin(t*math.pi*1.6)
    wob=1.1*math.sin(t*math.pi*2*4.0)
    depths.append(round(max(0.4,base+wob),2))
for i,s in enumerate(samples): s["depth_m"]=depths[i]

kpath=os.path.join(DATA,"tracks.json")
kdata=json.load(open(kpath,encoding="utf-8"))
idx=next(i for i,t in enumerate(kdata["items"]) if t["global_track_id"]=="U003")
kdata["items"][idx]["samples"]=samples
kdata["items"][idx]["source_tracks"][0]["samples"]=[dict(s) for s in samples]
with open(kpath,"w",encoding="utf-8") as f:
    json.dump(kdata,f,ensure_ascii=False,indent=2); f.write("\n")
print(f"\ntracks.json: U003 已更新为堤外航迹，{len(samples)} 个采样点")
print("备份：tracks.json.bak / targets.json.bak")
