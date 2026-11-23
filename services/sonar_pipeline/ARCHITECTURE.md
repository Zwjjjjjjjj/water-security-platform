# ARCHITECTURE — 模块映射与数据流

## 1. 总览：三层结构

```
┌─────────────────────────────────────────────────────────────────┐
│ L0 统一层  /root/sonar_pipeline/  —— 唯一入口 + 唯一配置 + 编排  │
│   run_pipeline.py / config/pipeline.json / sonar_pipeline/*.py  │
└──────────────┬────────────────────────────────┬─────────────────┘
               │复用（只读 import / subprocess） │复用（只读 import）
┌──────────────▼──────────────┐   ┌──────────────▼──────────────────┐
│ L1 跟踪层 Tritech_.../src/  │   │ L2 关联融合层 two_sonar_.../src/ │
│ 检测·Kalman·标准化·B合成·评估 │   │ 对齐·关联·融合（官方引擎）        │
└─────────────────────────────┘   └─────────────────────────────────┘
```

统一层不复制算法：Stage 1-4/7-8 用 `import src.*` 或 `subprocess run_all/run_dfki` 调用 L1；
Stage 5/6 只调 L2 官方引擎。旧胶水（`/root/run_e2e_pipeline.py`、L2 的 `run_*_association.py`）
的功能已被 `run_pipeline.py` 覆盖，保留仅供对照，不再写入新产物。

## 2. 文件级映射（旧 → 新）

| 能力 | 旧位置（只读） | 统一层位置 | 说明 |
|---|---|---|---|
| CLI 总入口 | `/root/run_e2e_pipeline.py` + `.sh` | `sonar_pipeline/run_pipeline.py` | `--mode/--datasets/--stages/--sensors/--n-sonars`，写 `PIPELINE_REPORT.md + pipeline_summary.json + run_summary.json` |
| 配置 | Tritech `default_config.json` + `dfki_config.json`；two_sonar `two/n_sonar_association.json`；B 合成硬编码 | `sonar_pipeline/config/pipeline.json` | single source of truth；旧文件不再编辑 |
| 路径/数据集注册 | 各脚本散落 `Path(...)` | `sonar_pipeline/common.py` | `TRITECH_ROOT / TWO_SONAR_ROOT / DATASETS / dataset_kind / dfki_set_num` |
| Stage 1-3 | `Tritech/run_all.py`、`src/run_dfki.py`、`src/standardize(_dfki).py` | `stages.stage1_manifest/stage2_tracking/stage3_standardize` | subprocess/import 复用，无算法改动；Stage 2/3 支持缓存复用（新输出 → `outputs_e2e` → Tritech `outputs*`） |
| Stage 4 合成 | `src/e2e_bridge.read_standardized_tracks + _synthesise_sonar_b` | `stages.stage4_build_local_tracks` | 只复用这两个函数；N>2 时同 yaw + seed/采样率/时偏/平移微调（`ARCH` §5.6 约束） |
| Stage 5 关联 | L2 `align_tracks/TrackAssociation/n_way_align/NPartite`；旧 `e2e_bridge` 内联对齐/代价/匈牙利（已废弃） | `association_fusion.associate_n_way` | 唯一算法位：N=2→`TrackAligner+TrackAssociation`；N≥3→`NWayAligner+NPartiteAssociator`；含 inf→1e6 消毒 + 贪心兜底 + 残留文件清理 + 空簇 schema |
| Stage 6 融合 | L2 `track_fusion.CovarianceWeightedFusion` / `n_way_fusion.InformationFusionN`；旧 bridge 内联 `_fuse_one_frame`（已废弃） | `association_fusion.fuse_n_way` + `_extract_one` | 唯一算法位：信息形式融合 ×`p_scale=15.0`；REAL 路关 innovation 门（`1e9`），SIM 路保留 |
| SIM 仿真 | L2 `n_sonar_simulator/simulate_two_sonars/run_n_sonar_association` | `sim_branch.run_sim_branch / run_sim_from_standardized` | 包一层：reference→仿真→本地 Kalman→同一 `associate/fuse_n_way`，输出 `sim/` + `n{N}/`（`metrics.json`） |
| Stage 7/8 评估 | `src/e2e_evaluate(+_extended)` | `stages.stage7_8_evaluate` | 复用 `run_full_evaluation + run_extended_evaluation`；`EvalPaths` 桥接 stage4/5/6 产物 |

## 3. 数据流（REAL，GLF / DFKI set_1..4）

```
GLF log / DFKI jpg(+GT csv)
  │ Stage 1 manifest（存在性 + 计数 → stage_1_manifest.json）
  ▼
Stage 2 跟踪：GLF run_all / DFKI run_dfki
  → tracks_raw.csv（frame级检测+Kalman：x,y,vx,vy,Pxx…）+ track_summary_all.csv
  │ Stage 3 标准化 → (timestamp,track_id,sensor_id,x,y,vx,vy,Pxx,Pyy,measurement_available)
  ▼
Stage 4 本地航迹：A=真实复制；B(/C/D)=_synthesise_sonar_b（重采样+ jitter + 绕原点yaw/平移 + 独立噪声 + dropout）
  → sonar_{A,B,C,D}_{measurements,tracks}.csv
  │ Stage 5 关联（association_real 门限）
  │   N=2: TrackAligner(±0.15s 最近邻) → aligned_track_pairs → TrackAssociation 六维代价
  │        (pos/vel/heading/mahalanobis/overlap/gate) → 匈牙利 → association_results + local_to_global(Gxxx)
  │   N≥3: NWayAligner(anchor=A, dt=0.1s公共轴) → aligned_long → NPartiteAssociator(anchor+extension,
  │        soft_gate=1.2×threshold, min_pair_pass_ratio=0.7) → n_clusters + local_to_global
  ▼
Stage 6 融合（fusion_cfg, p_scale=15, REAL门限1e9）
  → fused_global_tracks.csv（timestamp,global_id,x/y/vx/vy_fused,Pxx/Pyy_fused,n_sensors_used,sensors_used,fusion_mode）
  │ Stage 7/8：e2e_evaluate（跟踪质量+关联P/R/F1+融合统计）+ extended（RMSE_A/B/Fused vs GT、
  │   NEES χ²(2)、both_predicted honesty、duplicate审计）
  ▼
stage_8_evaluation/{evaluation.json,evaluation.md,stage_8_extended_evaluation.json}
```

`fusion_mode` 语义：N=2（`BOTH_MEASURED/A_ONLY/B_ONLY/BOTH_PREDICTED`，官方 `CovarianceWeightedFusion` 命名）；
N≥3（`N_MEASURED/SINGLE_SONAR_X/NONE/N_MEASURED_REJECTED`，`InformationFusionN` 命名）。

## 4. 数据流（SIM，N 虚拟声呐）

```
ReferenceTrack（默认：dfki_set_1 PRIMARY_DIVER tracks_raw；或标准化最长轨）
  → n_sonar_simulator.run_simulation（sonars布局+distractors+miss/jitter → sonar_{A..}_measurements.csv）
  → build_local_tracks（每声呐本地Kalman → sonar_{A..}_tracks.csv）
  → NWayAligner → NPartiteAssociator（association stock门限）→ InformationFusionN（保留官方门限）
  → aligned_long.csv + n_clusters.csv + fused_global_tracks.csv + metrics.json
     （single_rmse{SONAR_X}+fused_rmse/median/p90+n_clusters）
```

SIM 与 REAL 在 Stage 5/6 共用同一 `associate_n_way / fuse_n_way`；
唯一差别是配置段（`association` vs `association_real`）与融合门限开关（见 README §5.3）。

## 5. 输出布局

```
<out>/                                   # --out 根（规范：/root/outputs；N=3：/root/outputs_n3）
  PIPELINE_REPORT.md  pipeline_summary.json
  real/<dataset>/                        # glf | dfki_set_1..4
    run_summary.json
    stage_1_manifest.json
    stage_2_tracking/**/tracks_raw.csv
    stage_3_standardized/*.csv
    stage_4_local_tracks/sonar_*.csv
      stage_5_association/{aligned_*,association_*,n_clusters,local_to_global,global_tracks}.csv
      stage_6_fusion/fused_global_tracks.csv
    stage_8_evaluation/{evaluation.json,evaluation.md,stage_8_extended_evaluation.json}
  sim/<experiment>/n<N>/{sim/,n<N>/{aligned_long,fused_global_tracks,metrics.json,…}}
```

Stage 2/3 允许缓存复用：新输出缺失时按序回退到 `/root/outputs_e2e/**` 与 Tritech 遗留 `outputs*`，
`run_summary.json` 不记录 stages 2/3 时间但 Stage 4 起产物完全由统一层重算，保证关联/融合可复现。
