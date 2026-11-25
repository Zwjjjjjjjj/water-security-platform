# 端到端流水线报告

*Generated: 2026-10-10*

## 一、本报告目的

把 `/root/Tritech_GLF_Track_Project`（GLF + DFKI 单声呐处理）和
`/root/two_sonar_association`（多目标关联 + 协方差加权融合）这两条独立
的工程链路，用一个根目录入口串成一条端到端流水线，使得：

1. 任意数据集（GLF Tanana / DFKI set_1..4）都能从原始数据跑到融合输出。
2. 每个 stage 之间的数据契约（CSV schema）显式、单一，便于单步调试。
3. 已有 4 份报告不退化（regression test 通过）。

## 二、流水线结构

```
[Stage 1] 数据清单          stage_1_manifest.json
    GLF (Tanana) + DFKI (set_1..4)  ──────────┐
                                               ↓
[Stage 2] 单声呐处理 (复用现有脚本)              检测+Kalman+CSV
    run_all.py (GLF)   ─┐                       ↑
    src/run_dfki.py    ─┴── 都产 tracks_raw.csv │
                                               ↓
[Stage 3] 标准化 (SONAR_FAN vs dfki_pixel)     统一字段
    src/standardize.py (GLF)
    standardize_dfki_tracks()  ← 新增          统一字段
                                               ↓
[Stage 4] 第二声呐构造                          A=真实, B=仿真
    simulate_second_sonar.py / e2e_bridge.synthesise_sonar_b
                                               ↓
[Stage 5] 多目标关联 (N×M Hungarian + Global)   local→global
    e2e_bridge.stage5_associate (与 tsa.track_association 等价)
                                               ↓
[Stage 6] 协方差加权融合 (A+B → G)             fused_global_tracks.csv
    e2e_bridge.stage6_fuse (与 tsa.track_fusion 等价)
                                               ↓
[Stage 7] 评估 + 报告                          Markdown 汇总
    e2e_bridge.stage7_evaluate
```

## 三、新增/改动文件

### 新建
| 文件 | 作用 |
|---|---|
| `/root/Tritech_GLF_Track_Project/src/standardize_dfki.py` | DFKI 像素坐标标准化器 |
| `/root/Tritech_GLF_Track_Project/src/e2e_bridge.py` | 跨项目桥接层（stages 4-7） |
| `/root/run_e2e_pipeline.py` | 根入口（CLI + 主调度） |
| `/root/run_e2e_pipeline.sh` | Bash 一键脚本 |

### 不修改（按 `run_dfki.py` 顶部 "禁止修改" 规则）
- `Tritech_GLF_Track_Project/src/detect_track.py`
- `Tritech_GLF_Track_Project/src/frame_store.py`
- `Tritech_GLF_Track_Project/src/associate_tracks.py`
- `Tritech_GLF_Track_Project/src/standardize.py`
- `Tritech_GLF_Track_Project/src/visualize.py`
- `Tritech_GLF_Track_Project/run_all.py`
- `two_sonar_association/src/*`（18 个模块全部保留不动）
- 已有 4 份 Markdown 报告

## 四、Stage 间数据契约

### Stage 1 → Stage 2
无 IO。`DatasetConfig` 描述每个数据集的输入路径。

### Stage 2 → Stage 3
CSV：`tracks_raw.csv` + `track_summary_all.csv`
- GLF：`timestamp_utc, track_id, range_m, bearing_deg, fan_u_meas_m, fan_v_meas_m, …`
- DFKI：相同列名，但 `fan_u_meas_m/fan_v_meas_m` 实际是像素（不是 m）

### Stage 3 → Stage 4
CSV：`standardized_sonar_tracks.csv` (或 `dfki_set_N_standardized.csv`)
- `coordinate_frame` 字段区分 SONAR_FAN / dfki_pixel
- `horizontal_xy_valid` / `enu_valid` 全部为 false
- GLF 多了 `range_m / bearing_deg / fan_u_filt_m / fan_v_filt_m`
- DFKI 多了 `x_pix / y_pix / x_pred_pix / y_pred_pix`（重命名 + 协方差带 `_pix2` 后缀）

### Stage 4 → Stage 5
4 个 CSV（每个 sonar 2 个）：
- `sonar_A_measurements.csv` / `sonar_B_measurements.csv`
  - `timestamp, sensor_id, local_track_id, x_common, y_common, measurement_available`
- `sonar_A_tracks.csv` / `sonar_B_tracks.csv`
  - `timestamp, track_id, sensor_id, local_track_id, x, y, vx, vy, Pxx, Pxy, Pyy, Pvxvx, Pvyvy, measurement_available, track_status`

### Stage 5 → Stage 6
CSV：
- `aligned_track_pairs.csv` — `(timestamp, track_a_id, track_b_id, Ax, Ay, Bx, By, position_distance, velocity_difference, heading_difference)`
- `association_cost_matrix.csv` — 每对航迹的 cost
- `association_results.csv` — Hungarian 分配结果
- `local_to_global.csv` — `local_track_id → global_id` 映射
- `global_tracks.csv` — global track 汇总

### Stage 6 → Stage 7
CSV：`fused_global_tracks.csv`
- 字段：`timestamp, global_id, x_fused, y_fused, vx_fused, vy_fused, Pxx_fused, Pyy_fused, n_sensors_used, sensors_used, fusion_mode, is_valid, fallback_reason`

## 五、实测结果

### 5.1 GLF (Tanana) 链路

| 指标 | 值 |
|---|---|
| 原始帧数 (extracted) | 1 frame per `frames_u8.bin` row |
| 单声呐 track 数量 | 1（4IN_ABS_PIPE） |
| 标准化后行数 | 100（meas）+ ~12（filtered） |
| B 声呐合成 | 4.7 Hz, 80 ms offset, 12° yaw, 0.18 m position std |
| 关联结果 | 1 (A_4IN_ABS_PIPE ↔ B_4IN_ABS_PIPE) |
| Global Tracks | 6 (target + 5 CANDIDATE) |
| 融合输出 | 357 帧 |
| Fusion modes | A_ONLY 236 / B_ONLY 0 / BOTH_MEASURED 71 / BOTH_PREDICTED 50 |
| Mean association cost | **0.080**（B 噪声小，关联稳定） |
| Stage 2 耗时 | 9.12 s（重用 01_extracted） |
| Stages 4-7 耗时 | 0.15 s |

### 5.2 DFKI 链路（4 个 set）

| Dataset | N fused frames | N associations | N global tracks | A_ONLY | B_ONLY | BOTH_MEASURED | Cost |
|---|---|---|---|---|---|---|---|
| dfki_set_1 | 455 | 1 | 3 | 110 | 24 | 321 | 0.846 |
| dfki_set_2 | 333 | 1 | 6 | 166 | 0  | 167 | 0.841 |
| dfki_set_3 | 756 | 1 | 3 | 81  | 30 | 645 | 0.817 |
| dfki_set_4 | 740 | 1 | 3 | 112 | 4  | 624 | 0.818 |

观察：
- DFKI cost 普遍在 0.81-0.85，高于 GLF 的 0.08。这是预期：DFKI 像素域真实噪声大。
- BOTH_MEASURED 比例 50-90%，验证了 A 5Hz / B 4.7Hz 双采样率下融合层覆盖完整。
- Global tracks 数量等于"真实目标 + 候选/CANDIDATE 数量"，没有 ID 切换。

### 5.3 GLF vs DFKI 跨数据集对比

| 维度 | GLF (SONAR_FAN, m) | DFKI (dfki_pixel, px) |
|---|---|---|
| 物理含义 | 声呐扇面坐标 (m) | 图像像素坐标 (无物理尺度) |
| ENU 转换 | `horizontal_xy_valid=false, enu_valid=false` | 同上 |
| 噪声水平 (合成 B) | 0.18 m / 0.12 m/s | 8 px / 0.5 px/s |
| 关联 cost | 0.08 | 0.82 (高出 10×) |
| 融合模式 | A_ONLY 多，BOTH_MEASURED 较少（覆盖率 20%） | A/B 较均衡（覆盖率 70%+） |
| 适合做 | 验证算法逻辑 | 验证多目标 + ID 切换鲁棒性 |

成本差异主要源于：
1. DFKI 像素噪声 std=8 px 在 50 px norm 下 → 0.16 成本
2. DFKI 合成 B 用了 translation_x=40 px，抬升了 heading 差异
3. GLF 噪声 std=0.18 m 在 30 m norm 下 → 0.006 成本

**结论**：E2E 流水线在两种坐标系下都能正常工作，差异仅来自坐标系语义和噪声量级。

## 六、与已有 4 份报告的关系

| 已有报告 | 与 E2E 流水线的对应关系 |
|---|---|
| `TWO_SONAR_ASSOCIATION_REPORT.md` | 仿真层参考；E2E 在真实 GLF/DFKI 输出上复现 |
| `MULTI_TARGET_REPORT.md` | 4 个 multi-target case；E2E 当前用单目标 + distractors |
| `REGRESSION_BENCHMARK_REPORT.md` | 4 个 regression case；E2E 用了同等关联代价体系 |
| `CASE5_FUSION_REPORT.md` | 融合层详评；E2E 复现了协方差加权融合 + fallback |

E2E 流水线是"真实数据 + 真实单声呐 + 仿真第二声呐"的轻量级集成验证，
不替代上述 4 份报告的算法深度分析。

### 6.1 Regression test 验证

E2E 流水线不修改任何被 `run_dfki.py` 标记为"禁止修改"的文件，且不修改
`two_sonar_association/src/*` 18 个模块的接口。已通过以下回归测试：

1. `run_regression_benchmark.py --cases case1_single` 输出与引入 E2E 前一致：
   - Association Precision = 1.000
   - Association Recall = 1.000
   - ID Switches = 0
   - Global Track Purity = 1.000
   - Global Track Recall = 1.000
2. `from src.standardize import standardize_tracks` 在 Tritech 内仍可独立 import
3. `from two_sonar_association.src.track_association import TrackAssociation` 仍可工作
4. `outputs/regression_benchmark/case1_single/fused_global_tracks.csv` 行数与内容未变
5. 6 个"禁止修改"文件 md5 校验不变（见 §三）

## 七、运行方式

```bash
# 一键全跑（所有 dataset + 所有 stage）
cd /root
./run_e2e_pipeline.sh

# 只跑 GLF
./run_e2e_pipeline.sh glf

# 只跑 DFKI set_1
./run_e2e_pipeline.sh dfki_set_1

# 跑所有 dataset 但只跑下游 stage 4-7（重用 stage 2/3 缓存）
./run_e2e_pipeline.sh all 4,5,6,7

# 等价的 Python 调用
python3 /root/run_e2e_pipeline.py --datasets all --stages all
```

## 八、限制与未来工作

- 真实外参 + ENU 转换未实现（README §12 已明确 TODO）
- B 声呐是合成的，未接入真实第二台声呐
- 弱目标 / 交叉目标鲁棒跟踪、误报抑制未做
- 实时性测试未做

下一阶段 TODO：
1. 真实双声呐数据接入
2. ENU 坐标系 + 真实外参标定
3. 5+ 目标压力测试
4. 弱目标/误报鲁棒性增强
