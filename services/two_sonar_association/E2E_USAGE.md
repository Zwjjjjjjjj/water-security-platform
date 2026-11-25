# E2E Pipeline 使用说明

## 一、概述

`/root/run_e2e_pipeline.py` 是端到端流水线的根入口。它把以下两个项目
串成一条线：

- `/root/Tritech_GLF_Track_Project` — 单声呐处理（GLF + DFKI）
- `/root/two_sonar_association` — 多目标关联 + 协方差加权融合

## 二、运行

### 2.1 一键脚本

```bash
cd /root
./run_e2e_pipeline.sh                                  # 全跑
./run_e2e_pipeline.sh glf                              # 只跑 GLF
./run_e2e_pipeline.sh dfki_set_1                       # 只跑 DFKI set_1
./run_e2e_pipeline.sh all 4,5,6,7                      # 只跑下游 stage
```

### 2.2 Python 直接调用

```bash
python3 /root/run_e2e_pipeline.py \
    --datasets glf,dfki_set_1,dfki_set_2 \
    --stages all \
    --out /root/outputs_e2e
```

可选参数：
- `--datasets {all|glf|dfki_set_1|dfki_set_2|dfki_set_3|dfki_set_4|name1,name2,...}`
- `--stages {all|1|1,2,3|4,5,6,7|...}`
- `--out <dir>` (默认 `/root/outputs_e2e`)

## 三、Stage 说明

| Stage | 输入 | 输出 | 实现 |
|---|---|---|---|
| 1 | 数据路径 | `stage_1_manifest.json` | `run_e2e_pipeline.stage1_manifest` |
| 2 | Stage 1 输出 | `stage_2_tracking/{tracks_raw.csv, track_summary_all.csv}` | `run_all.py` (GLF) 或 `src.run_dfki` (DFKI) |
| 3 | Stage 2 输出 | `stage_3_standardized/{*_standardized.csv}` | `src.standardize` (GLF) 或 `src.standardize_dfki` (DFKI) |
| 4 | Stage 3 输出 | `stage_4_local_tracks/{sonar_A/B_*.csv}` | `src.e2e_bridge.stage4_build_local_tracks` |
| 5 | Stage 4 输出 | `stage_4_local_tracks/stage_5_association/*.csv` | `src.e2e_bridge.stage5_associate` |
| 6 | Stage 5 输出 | `stage_4_local_tracks/stage_6_fusion/fused_global_tracks.csv` | `src.e2e_bridge.stage6_fuse` |
| 7 | Stage 6 输出 | `stage_4_local_tracks/stage_7_evaluation.json` | `src.e2e_bridge.stage7_evaluate` |

每个 stage 都有缓存机制：单独跑 stage 5-7 时，如果前序 stage 的产物
已存在（不论在 `outputs_e2e/.../stage_X/...` 或在默认 `outputs/`/`outputs_dfki/`），
会自动复用，不重复计算。

## 四、输出结构

```
/root/outputs_e2e/
├── E2E_PIPELINE_REPORT.md            # Markdown 汇总
├── e2e_summary.json                  # JSON 汇总
├── glf/
│   ├── stage_1_manifest.json
│   ├── stage_2_tracking/
│   │   ├── 01_extracted/             # GLF extracted frames
│   │   ├── 02_tracking/
│   │   │   ├── detections.csv
│   │   │   └── tracks_raw.csv
│   │   └── 03_standardized/
│   │       └── standardized_sonar_tracks.csv
│   ├── stage_3_standardized/
│   │   └── glf_standardized.csv
│   └── stage_4_local_tracks/
│       ├── sonar_A_measurements.csv
│       ├── sonar_A_tracks.csv
│       ├── sonar_B_measurements.csv
│       ├── sonar_B_tracks.csv
│       ├── stage_5_association/
│       │   ├── aligned_track_pairs.csv
│       │   ├── association_cost_matrix.csv
│       │   ├── association_results.csv
│       │   ├── local_to_global.csv
│       │   └── global_tracks.csv
│       ├── stage_6_fusion/
│       │   └── fused_global_tracks.csv
│       └── stage_7_evaluation.json
├── dfki_set_1/
│   └── … (same structure as glf/)
└── dfki_set_2/, dfki_set_3/, dfki_set_4/
    └── …
```

## 五、典型工作流

### 5.1 快速验证（无 tracking，重新跑关联+融合）

```bash
# 第一次跑完后，stage 2/3 的输出已缓存。可以单独验证新算法改动
python3 /root/run_e2e_pipeline.py --datasets glf --stages 4,5,6,7
```

### 5.2 加入新的 DFKI 跟踪算法

1. 修改 `Tritech_GLF_Track_Project/src/run_dfki.py` 之外的所有 *.py
   （`run_dfki.py` 顶部声明了"禁止修改"的文件清单）
2. 跑全 stage：`./run_e2e_pipeline.sh dfki_set_1`
3. 对比 `e2e_summary.json` 中 stage 4-7 输出的关联 cost / 融合模式分布

### 5.3 切换到真实第二声呐数据

1. 在 `Tritech_GLF_Track_Project/src/e2e_bridge.py` 的 `stage4_build_local_tracks`
   中，将 `_synthesise_sonar_b()` 替换为读取真实 `sonar_B_tracks.csv`
2. 跑 `./run_e2e_pipeline.sh glf` 验证
3. 真实数据应该 mean_association_cost < 0.5 (远低于 0.85 仿真值)

## 六、错误处理

| 错误 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'src.standardize_dfki'` | cwd 不在 Tritech | `cd /root/Tritech_GLF_Track_Project` 后再跑 |
| `FileNotFoundError: GLF file not found` | GLF 路径错 | 检查 `DEFAULT_GLF` 路径 |
| `DFKI set dir not found` | DFKI 路径错 | 检查 `DEFAULT_DFKI_ROOT` |
| `JAVA_HOME not set` | run_all.py 需要 Java | `apt install default-jdk` |
| Stage 5 关联为 0 | B 合成噪声过大 | 调小 `bridge_position_std` / `bridge_yaw_deg` |
