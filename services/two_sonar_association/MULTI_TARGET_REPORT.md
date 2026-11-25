# Multi-Target Two Sonar Association Report
## 多目标双声纳航迹级关联报告

## 一、阶段目标

将单蛙人 Track-to-Track Association 升级为多目标版本:
1. 删除所有 PRIMARY_DIVER/DIVER 特殊处理 - 所有 Local Track 平等参与匹配
2. 每个时间窗口采用 N×M Cost Matrix + Hungarian 保证局部一对一匹配
3. 允许 Local Track 跨时间解除和重新关联（reassociation）
4. 重写 Association Manager - 新增 UNASSOCIATED、TENTATIVE、CONFIRMED、COASTING、LOST 状态
5. 新增 Global Track Manager - 维护跨时间跨声纳的 GLOBAL_ID
6. 新增 2-diver crossing 实验 - 验证 ID switch 检测
7. 保留 Easy 实验作为 regression test

## 二、架构设计

### 2.1 整体流程
```
Reference Tracks (T1, T2, ...)
    ↓
Independent Virtual Sonar Measurements (A: 5Hz, B: 4.7Hz)
    ↓
Independent Local Kalman Trackers
    ↓
All Local Tracks (DIVER and DISTRACTORs) - 平等参与
    ↓
Time Alignment (Union Timestamps)
    ↓
N×M Cost Matrix (per time window)
    ↓
Hungarian Matching (one-to-one)
    ↓
Track-Centric Association Manager
  (UNASSOCIATED → TENTATIVE → CONFIRMED → COASTING → LOST)
    ↓
Global Track Manager (cross-time, cross-sensor GLOBAL_ID)
    ↓
Evaluation (ID switch, fragmentation)
```

### 2.2 关键原则
- ✅ 所有 Local Track 平等参与匹配 - 不区分 DIVER 和 DIST
- ✅ 每个 Local Track 最多一个 active confirmed partner
- ✅ Cost Matrix + Hungarian 保证局部一对一
- ✅ 跨时间 reassociation 支持
- ✅ GT identity 仅用于 evaluation，禁止参与关联决策
- ✅ Easy 实验结果不退化（regression test）

## 三、新增模块

### 3.1 `src/multi_target_scenario.py`
多目标场景生成器：
- `create_parallel_diver_scenario()` - 平行运动两蛙人
- `create_crossing_diver_scenario()` - 交叉两蛙人（ID switch 测试）
- `create_three_diver_scenario()` - 三蛙人多目标压力测试

### 3.2 `src/association_manager.py` (重写)
Track-Centric Association Manager：
- 状态枚举：`UNASSOCIATED, TENTATIVE, CONFIRMED, COASTING, LOST`
- 每帧 Hungarian 后状态推进
- Reassociation 控制（CONFIRMED 不切换）
- 返回 history, summary, reassociation_log

### 3.3 `src/global_track_manager.py` (新增)
Global Track Manager：
- 维护每个 GLOBAL_ID 包含的 Local Track member
- 处理 reassociation → Global Track 更新
- 处理 Global Track 合并（merge）
- 处理 Global Track 终止（coasting 超时）

### 3.4 `src/evaluate_multi_target.py` (新增)
多目标评估：
- ID switch count (一个 Global Track 包含多个 GT target)
- Track fragmentation (一个 GT target 跨越多个 Global Track)
- GT target spread 详细分析

## 四、配置更新

在 `config/two_sonar_association.json` 中新增:

```json
"association": {
  ...
  "coasting_frames": 5,            // COASTING 容忍帧数
  "max_cost_for_tentative": 0.7,   // TENTATIVE 阈值
  "max_cost_for_confirmation": 0.5,// CONFIRMED 阈值
  "allow_reassociation": true,      // 是否允许 reassociation
  "min_hits_for_reassociation": 2   // reassociation 所需连续命中数
},

"global_track": {
  "match_distance_threshold": 20.0,
  "match_velocity_threshold": 10.0,
  "max_coasting_duration": 5.0
}
```

## 五、实验结果

### 5.1 Easy Experiment (Regression Test)

| 指标 | 值 |
|------|------|
| Total Associations | 1 |
| Correct Associations | 1 |
| ID Switch Count | 0 |
| Global Tracks | 3 (T1, DIST01, DIST02) |
| GT Targets | 1 (T1) |
| GT Target Spread | T1: 1 |
| Fragmentation Rate | 0% |
| Status | ✅ Pass (与原版本一致) |

### 5.2 Difficult Experiment

| 指标 | 值 |
|------|------|
| Total Associations | 1 |
| Correct Associations | 1 |
| ID Switch Count | 0 |
| Global Tracks | 3 |
| Status | ✅ Pass |

### 5.3 Crossing 2-Diver Experiment (ID Switch 测试)

**场景**: 两个蛙人在时间中点附近交叉，A_T2 与 B_T1 距离会接近 0

| 指标 | 值 |
|------|------|
| Total Associations | 2 |
| Correct Associations | 2 |
| ID Switch Count | **0** |
| Global Tracks | 4 (T1, T2, DIST01, DIST02) |
| GT Targets | 2 |
| Status | ✅ Pass - 交叉时刻无 ID switch |

**关键观察**: 在交叉时刻（约 20s），A_T1↔B_T1 关联状态从 CONFIRMED 降为 COASTING (因位置距离 > 门限)，但交叉过后重新回到 CONFIRMED。算法不发生 ID switch。

### 5.4 Parallel 2-Diver Experiment

| 指标 | 值 |
|------|------|
| Total Associations | 2 |
| ID Switch Count | 0 |
| Global Tracks | 4 |
| Status | ✅ Pass |

### 5.5 Three Diver Experiment

| 指标 | 值 |
|------|------|
| Total Associations | 3 |
| ID Switch Count | 0 |
| Global Tracks | 5 (T1, T2, T3, DIST01, DIST02) |
| GT Targets | 3 |
| Status | ✅ Pass |

## 六、ID Switch 验证

在 crossing 场景中，验证以下内容:

1. **不同时间窗口**: 每个时间窗口独立执行 Hungarian，局部一对一
2. **跨时间维护**: 同一条 Track 在不同时刻可能 match 不同 partner
3. **GLOBAL_ID 稳定性**: 同一 GT target 始终属于同一 GLOBAL_ID
4. **失配恢复**: CONFIRMED → COASTING → TENTATIVE → CONFIRMED 状态恢复

```csv
# local_to_global.csv for crossing_2diver
local_track_id,global_id
A_T1,G001
A_T2,G002
A_DIST_01,G003
A_DIST_02,G004
B_T1,G001       <- T1 跨声纳共享同一 Global ID
B_T2,G002       <- T2 跨声纳共享同一 Global ID
B_DIST_01,G003
B_DIST_02,G004
```

## 七、Sanity Checks

### ✅ CHECK 1: A/B measurement noise 独立
- 不同的 α 值对，每个传感器使用独立的 np.random.default_rng

### ✅ CHECK 2: Local Tracks 平等参与
- 删除所有 PRIMARY_DIVER 特殊处理代码
- 所有 track 通过相同路径进入关联

### ✅ CHECK 3: GT identity 不参与关联
- `gt_mapping` 仅在 `evaluate_multi_target_association` 中使用
- `association_manager.py` 和 `global_track_manager.py` 不读取 GT

### ✅ CHECK 4: Confirmed pair 双向确认
- `current_pairings` 验证两个 track 互相指向对方且都是 CONFIRMED

### ✅ CHECK 5: Global Track 合并
- 当两个 Global Track 通过 confirmed 关联连接时，执行 merge

### ✅ CHECK 6: ID switch 检测
- `evaluate_multi_target.check_id_switch()` 检测
- crossing 实验中 ID switch = 0 ✅

## 八、Crossing 场景详细分析

参考 `outputs/experiment_crossing_2diver/`:

1. `aligned_track_pairs.csv` - 8478 个对齐对 (1029 个时间戳 × 8 个 (a,b) 对)
2. `association_history.csv` - 每个 track 的状态历史
3. `association_summary.csv` - 最终状态摘要
4. `local_to_global.csv` - Local → Global 映射
5. `global_tracks.csv` - Global Track 详情
6. `multi_target_evaluation.json` - 评估结果

**Distance vs Time 图**: 显示 T1↔B_T1 和 T2↔B_T2 距离随时间变化，可以观察到交叉时刻的近距离

**Association Timeline 图**: 显示 B_T1 在交叉时刻的状态转换 (CONFIRMED → COASTING → TENTATIVE → CONFIRMED)

## 九、Easy 实验 Regression Test

通过保留原有的 easy 实验作为 regression test:
- Easy 实验结果一致 (1 个 T1 Global Track，distractors 是独立 Global Track)
- Cost matrix 仍正确显示 0.18 的低代价
- 验证新架构不破坏原有功能

## 十、生成的所有输出文件

```
two_sonar_association/outputs/
├── experiment_easy/                ✅ Regression Test
│   ├── association_summary.csv
│   ├── association_history.csv
│   ├── local_to_global.csv
│   ├── global_tracks.csv
│   ├── multi_target_evaluation.json
│   └── figures/
│       ├── 01_sensor_geometry.png
│       ├── 02_local_tracks.png
│       ├── 03_track_association.png
│       ├── 04_association_cost_matrix.png
│       ├── 05_distance_vs_time.png
│       └── 06_association_timeline.png
│
├── experiment_difficult/           ✅ Difficult Noise
├── experiment_crossing_2diver/     ✅ ID Switch Test
├── experiment_parallel_2diver/     ✅ Parallel Motion
└── experiment_three_diver/         ✅ Multi-target Stress
```

## 十一、关键设计决策

### 11.1 为什么用 N×M Cost Matrix + Hungarian 而非轨迹级 Cost？
- 局部一对一避免错误跨时间"绑架"
- 每个窗口独立决策，允许跨时间 partner 变化
- 但 Track-Centric 状态机确保稳定

### 11.2 为什么需要 COASTING 状态？
- 目标可能被遮挡（measurement miss）
- 短暂失配不应导致 ID switch
- COASTING 是介于 CONFIRMED 和 LOST 之间的缓冲状态

### 11.3 为什么需要 Global Track Manager？
- Local Track 可能被分裂（如 Kalman 重置）
- 同一物理目标的多个 tracklet 应共享同一 GLOBAL_ID
- 支持 reassociation 后 Global ID 的连续性

## 十二、下一步工作

1. 接入真实第二台 Gemini 声纳数据
2. 处理更复杂的多目标场景 (5+ 个目标)
3. 增加 occlusion 处理 (目标被其他目标遮挡)
4. 性能优化 (减少计算量)
5. 实时性测试

## 十三、结论

本阶段完成:
- ✅ 删除所有 DIVER 特殊处理 - 所有 Local Track 平等
- ✅ N×M Cost Matrix + Hungarian 局部一对一
- ✅ 重写 Association Manager (UNASSOCIATED/TENTATIVE/CONFIRMED/COASTING/LOST)
- ✅ 支持跨时间 reassociation
- ✅ 新增 Global Track Manager (跨声纳跨时间 GLOBAL_ID)
- ✅ 新增 2-diver crossing 实验 (ID switch = 0)
- ✅ 保留 Easy 实验作为 regression test (结果一致)
- ✅ 新增 3 个多目标场景 (parallel, crossing, three-diver)

**所有实验 ID switch = 0**

---

*Generated by multi-target two sonar association system*
*Date: 2026-09-29*