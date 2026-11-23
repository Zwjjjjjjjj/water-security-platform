# sonar_pipeline — 统一声呐航迹系统（原始数据 → 单声呐跟踪 → 航迹关联 → 融合）

唯一入口：`python3 /root/sonar_pipeline/run_pipeline.py`

```
原始数据 ──► Stage 1/2/3 单声呐跟踪/标准化 ──► Stage 4 本地航迹 ──► Stage 5 关联 ──► Stage 6 融合 ──► Stage 7/8 评估
   │                    │                              │                  │                 │                │
   │              Tritech_GLF_Track_Project            │      two_sonar_association        │         Tritech e2e_evaluate
   │              (run_all / run_dfki /                │      (TrackAssociation /          │         (+extended: RMSE/NEES/
   │               standardize*)                       │       NPartite / CWF / IF-N)      │          honesty/duplicate审计)
   │                                                   │                                 │
   └──── REAL: GLF + DFKI set_1..4（A 真实，B/C/D 合成）┘                                 │
         SIM:  参考航迹 → N 虚拟声呐仿真 → 本地 Kalman → 同一套关联/融合引擎 ──────────────┘
```

## 1. 快速开始

```bash
# REAL 全量 N=2（GLF + DFKI 1-4，含评估）
python3 /root/sonar_pipeline/run_pipeline.py --mode real --datasets all --stages 4,5,6,7,8 --out /root/outputs
# REAL N=3（输出到独立目录，避免覆盖 N=2）
python3 /root/sonar_pipeline/run_pipeline.py --mode real --datasets all --stages 4,5,6 --sensors SONAR_A,SONAR_B,SONAR_C --out /root/outputs_n3
# REAL N=4 抽查
python3 /root/sonar_pipeline/run_pipeline.py --mode real --datasets dfki_set_1 --stages 4,5,6 --sensors SONAR_A,SONAR_B,SONAR_C,SONAR_D --out /tmp/n4test
# SIM（N 虚拟声呐，默认 easy）
python3 /root/sonar_pipeline/run_pipeline.py --mode sim --n-sonars 2 --out /tmp/simtest
python3 /root/sonar_pipeline/run_pipeline.py --mode sim --n-sonars 4 --out /tmp/simtest4
# 全部（real all + sim N=2）
python3 /root/sonar_pipeline/run_pipeline.py --mode all --out /root/outputs
```

常用参数：`--mode real|sim|all`、`--datasets all|glf,dfki_set_1...`、
`--stages all|4,5,6,7,8`、`--sensors SONAR_A,SONAR_B[,SONAR_C[,SONAR_D]]`、
`--n-sonars 2|3|4`、`--experiment easy|difficult|default`、
`--config <pipeline.json>`、`--out <dir>`。

## 2. 目录结构

```
/root/sonar_pipeline/
  run_pipeline.py              # 唯一 CLI 入口（real / sim / all）
  config/pipeline.json         # 唯一可信配置（见 §4）
  sonar_pipeline/
    __init__.py                # 包说明 + 导出 associate_n_way / fuse_n_way
    common.py                  # 路径/数据集/配置解析（TRITECH_ROOT / TWO_SONAR_ROOT / DATASETS）
    stages.py                  # Stage 1-8 编排（stage1_manifest … stage7_8_evaluate）
    association_fusion.py      # Stage 5/6 唯一算法实现（官方引擎路由，无内联复刻）
    sim_branch.py              # SIM 分支（reference → n_sonar_simulator → run_n_sonar_pipeline）
  README.md                    # 本文件（使用手册）
  ARCHITECTURE.md              # 架构 / 数据流 / 新旧模块映射
  VALIDATION.md                # 验证结论（数字 + 产物位置）
```

遗留工程只读复用，不复制算法：
- `/root/Tritech_GLF_Track_Project/src/` — Stage 1-3 跟踪、Stage 4 B 合成（`e2e_bridge._synthesise_sonar_b` 仅此一处）、Stage 7/8 评估器
- `/root/two_sonar_association/src/` — Stage 5/6 官方引擎（`TrackAligner/TrackAssociation/CovarianceWeightedFusion/NWayAligner/NPartiteAssociator/InformationFusionN`）
- 旧顶层胶水（`/root/run_e2e_pipeline.py`、各 `run_*_association.py`）已被 `run_pipeline.py` 取代，保留仅供对照。

## 3. 八个阶段与产物

| Stage | 职责 | REAL 实现 | SIM 实现 | 产物（每数据集目录下） |
|---|---|---|---|---|
| 1 manifest | 数据清点 | `stages.stage1_manifest`（GLF 文件 / DFKI set jpg+GT 计数） | — | `stage_1_manifest.json` |
| 2 tracking | 单声呐检测+Kalman | 复用 `run_all.py`（GLF）/ `src.run_dfki`（DFKI），可缓存复用 | `n_sonar_simulator.run_simulation` 生成量测 | `stage_2_tracking/**/tracks_raw.csv` + `track_summary_all.csv` |
| 3 standardize | 统一 schema | 复用 `standardize(_dfki).py` → `(timestamp,track_id,sensor_id,x,y,vx,vy,Pxx,Pyy,measurement_available)` | —（仿真量测已是标准 schema） | `stage_3_standardized/*.csv` |
| 4 local tracks | A 真实 + B/C/D 合成 | `e2e_bridge.read_standardized_tracks + _synthesise_sonar_b`（同 yaw，多 seed/采样率/时偏/平移微调，见 §5） | `simulate_two_sonars.ReferenceTrack` + 本地 Kalman（`build_local_tracks`） | `stage_4_local_tracks/sonar_{A,B,C,D}_{measurements,tracks}.csv` |
| 5 associate | N 路航迹关联 | `association_fusion.associate_n_way`（N=2: `TrackAligner+TrackAssociation` 匈牙利；N≥3: `NWayAligner+NPartiteAssociator` anchor+extension） | 同一 `associate_n_way`（经 `run_n_sonar_pipeline`） | `stage_5_association/{aligned_*,association_*,n_clusters,local_to_global,global_tracks}.csv` |
| 6 fuse | N 路协方差加权融合 | `association_fusion.fuse_n_way`（N=2: `CovarianceWeightedFusion`；N≥3: `InformationFusionN`，信息形式 $P_f^{-1}=\sum P_i^{-1}$，`p_scale=15.0`；REAL 路关掉 innovation 门限 `1e9`，因 B 是整体平移合成） | 同一 `fuse_n_way`（保留官方门限） | `stage_6_fusion/fused_global_tracks.csv` |
| 7/8 evaluate | 跟踪/关联/融合评估 | 复用 `e2e_evaluate.run_full_evaluation` + `e2e_evaluate_extended`（RMSE_A/B/Fused、关联 P/R/F1、NEES $\chi^2(2)$、honesty、duplicate 审计） | `metrics.json`（single_rmse / fused_rmse / median / p90 / n_clusters） | `stage_8_evaluation/{evaluation.json, evaluation.md, stage_8_extended_evaluation.json}` |

`run_pipeline.py` 每次运行还在 `--out` 根目录写 `PIPELINE_REPORT.md + pipeline_summary.json`；
每个数据集目录写 `run_summary.json`（stages_run / n_fused / fusion_modes / evaluation 摘要 / error）。

## 4. 配置（single source of truth）

`config/pipeline.json` 合并了四处旧配置，旧文件只读：
`Tritech default_config.json`（GLF 跟踪）＋ `dfki_config.json`（DFKI 跟踪）＋
`two_sonar_association.json`（2 路关联/融合）＋ `n_sonar_association.json`（N 路）＋
桥接 B 合成参数（原散落在 `run_e2e_pipeline.py`）。

关键段：
- `glf_tracking / dfki_tracking / glf_sensor / dfki_sensor` — Stage 2/3 参数与坐标系声明
- `bridge.{glf,dfki}` — Stage 4 合成（translation/yaw/rate/jitter/dropout/seed）
- `association_real` — REAL 路放宽门限（`position_gate_m=80, association_threshold=0.9, mahalanobis=1e6`，容纳 40px 合成平移）；`association` — SIM 路 stock 门限；`n_way_association` — anchor/clique 裁剪
- `fusion`（`p_scale=15.0`，NEES 标定值）— SIM/REAL 共用，REAL 路运行时另把 outlier 门限抬到 `1e9`（见 `stages.stage6_fuse` 注释）
- `sonars[4] + distractors + kalman_filter + global_track` — SIM 分支 N 声呐布局
- `datasets: [glf, dfki_set_1..4]`、`output_root`、`glf_path/dfki_root`

## 5. 易错点（已修复，请勿回退）

1. **匈牙利不可行**：官方 cost 矩阵对零对齐航迹对留 `+inf`，整行 `inf` 会使 `linear_sum_assignment` 抛 `cost matrix is infeasible`。
   修复：`association_fusion.associate_n_way` 以 `1e6` 代 `inf` 再加贪心兜底。
2. **REAL 关联过门限**：stock `position_gate_m=30` 拦掉 40px 平移的合成 B，零关联。
   修复：`association_real` 放宽到 80px / threshold 0.9。
3. **合成 B 被融合外点门限误杀**：`CovarianceWeightedFusion` 的 3σ 门限（~25px）< 40px 系统平移，85% 帧拒 B。
   修复：REAL 路 `outlier_sigma_threshold = innovation_outlier_sigma = 1e9`（SIM 路保留原门限）。
4. **N≥3 残留文件误导**：N=2 切 N=3 时 `aligned_track_pairs.csv / association_results.csv` 残留。
   修复：`stages.stage5_associate` 每次先删 4 个 stage5 旧文件。
5. **N≥3 空簇 DataFrame 崩**：零簇时 `pd.DataFrame([])` 无列，下游读 `cluster_id` 失败。
   修复：显式带列初始化 `columns=[cluster_id, global_id, mean_cost, gate_pass, pair_pass_ratio]`。
6. **N≥3 合成发散（最隐蔽）**：`_synthesise_sonar_b` 是绕原点旋转，对半径 ~485px 的 DFKI 航迹，每多 6° yaw ≈ 51px 弧移；旧 N≥3 代码每声呐 +6°，C 距 A ~128px 超出 80px 门限 → 零簇。
   修复：所有合成声呐 yaw 保持与 B 相同，仅 seed/采样率/时偏/平移微调（+5/+8px 每声呐，A–C ≲ 46px）。

## 6. 输出根目录约定

- `/root/outputs/` — 规范输出（REAL N=2 全量 + SIM N=2/3/4，`PIPELINE_REPORT.md / pipeline_summary.json`）
- `/root/outputs_n3/` — REAL N=3 全量（与 N=2 同构，仅 sensors=A,B,C）
- `/tmp/n4test`、`/tmp/simtest*` — 一次性抽查目录，可删
- `/root/outputs_e2e*`、`Tritech …/outputs*`、`dfki_output_v*` — 历史遗留输出，只读缓存（Stage 2/3 cache 命中源），不再写入
