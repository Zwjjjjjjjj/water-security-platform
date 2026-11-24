# 水域安防数字孪生平台 (Water Security Digital Twin Platform)

多源海上目标观测数据的**标准化 → 关联 → 融合 → 可视化**一体化工程。

本仓库为 Monorepo，整合了三个核心工程：

| 子项目 | 路径 | 技术栈 | 职责 |
|--------|------|--------|------|
| **目标标准化** | `services/target_standardization/` | Python 3.12 | 雷达 / AIS / 光电多源数据解析、统一 schema 标准化、时空对齐、目标关联、航迹重建与验收 |
| **声纳流水线** | `services/sonar_pipeline/` | Python 3.12 | 声纳信号级处理流水线（仿真分支 → 关联融合），统一配置与阶段编排 |
| **大屏可视化** | `apps/es-big-screen/` | Vue 3 + TypeScript + Vite + ECharts + Element Plus | 水域安防数字孪生大屏，水下潜器/水面船舶/传感器态势渲染 |

---

## 目录结构

```
water-security-platform/
├── apps/
│   └── es-big-screen/            # Vue3 大屏应用
│       ├── src/
│       │   ├── views/screen/     # 大屏主视图与面板
│       │   ├── water-security/   # 水域安防业务模块
│       │   │   └── integration/  # 场景引擎、图表、导航、水体特效
│       │   ├── components/       # 通用组件
│       │   ├── store/            # Pinia 状态
│       │   └── assets/           # 静态资源与数据
│       ├── public/water-security/assets/  # 运行时素材（声纳帧、地形贴图等）
│       ├── tools/                # 素材生成与校验脚本
│       └── scripts/              # 导航/时间轴一致性检查
├── services/
│   ├── target_standardization/   # 多源目标标准化与关联服务
│   │   ├── standardization/      # 各传感器适配器 → 统一 schema
│   │   │   ├── radar_ttm_adapter.py / radar_cutdata_adapter.py
│   │   │   ├── radar_if_header.py
│   │   │   ├── ais_csv_adapter.py / ais_vdm_adapter.py
│   │   │   ├── optical_photo_adapter.py
│   │   │   └── schema.py / units.py / time_fields.py / validation.py
│   │   ├── alignment/            # 时空对齐（时间基准、空间变换、外推）
│   │   ├── association/          # 目标关联（CDT 跟踪、偏差修正、主目标选取）
│   │   ├── simulation/           # 场景与轨迹仿真、NMEA 编码
│   │   ├── calibration/          # MTDSP 误差模型
│   │   ├── verification/         # 跨源验证（时/空/三源一致性）
│   │   ├── configs/              # 各数据集与场景配置
│   │   ├── data/                 # 样例数据集（mtdsp / mtdsp2 / radarpwn）
│   │   └── tests/                # pytest 测试套件
│   └── sonar_pipeline/           # 声纳处理流水线
│       ├── sonar_pipeline/       # stages / common / sim_branch / association_fusion
│       ├── config/pipeline.json
│       └── run_pipeline.py
├── docs/                         # 架构与设计文档
└── .gitignore
```

---

## 快速开始

### 1. 目标标准化服务

```bash
cd services/target_standardization

# 依赖
pip install numpy pandas scipy pyproj matplotlib pytest

# 运行入口（均为模块级脚本）
python run_standardization.py       # 多源数据标准化
python run_alignment.py             # 时空对齐
python run_associate.py             # 目标关联
python run_tracks.py                # 航迹重建
python run_reconstruct.py           # 场景重建
python run_acceptance.py            # 验收评估
python run_scenario_generation.py   # 场景生成

# 验证
python -m pytest tests/ -v
```

### 2. 声纳流水线

本工程是**编排层**：Stage 1–3 复用 Tritech 单声呐跟踪工程，Stage 5/6 复用 two_sonar 的官方关联/融合引擎，
这些依赖位于本仓库之外，需通过环境变量指定位置。

```bash
cd services/sonar_pipeline
pip install numpy scipy matplotlib

# 指向外部依赖（不设置则使用下方默认值）
export TRITECH_ROOT=/root/Tritech_GLF_Track_Project
export TWO_SONAR_ROOT=/root/two_sonar_association
export DFKI_ROOT="/root/Diver Tracking - Sonar"
export GLF_PATH=/root/data/log_2025-06-19-203913.glf

python3 run_pipeline.py --mode sim --n-sonars 2 --out /tmp/simtest
python3 run_pipeline.py --mode real --datasets all --stages 4,5,6,7,8 --out /root/outputs
```

配置优先级：**环境变量 > `config/pipeline.json` > 内置默认值**。
支持的变量：`TRITECH_ROOT`、`TWO_SONAR_ROOT`、`GLF_PATH`、`GLF_PROFILE`、
`DFKI_ROOT`、`PIPELINE_OUT`、`LEGACY_E2E_ROOT`、`DFKI_OUTPUT_V15`。

### 3. 大屏可视化

```bash
cd apps/es-big-screen

npm install          # 或 yarn install

npm run dev          # 本地开发
npm run build        # 生产构建 → dist/
npm run preview      # 预览构建产物
```

---

## 设计要点

### 多源标准化

各传感器原始格式差异巨大（雷达 TTM / CutData、IF 头、AIS CSV / VDM、光电截图 + 检测结果），
`standardization/` 下的适配器将它们统一到同一 schema：统一的经纬度、UTC 时间、目标类型、置信度字段，
供下游对齐与关联消费。

### 时空对齐

`alignment/` 先做**时间基准统一**（各源采集时刻偏移、时间同步策略），再做**空间变换**（外参 / 地理配准），
最后插值到统一栅格，输出 `aligned_grid_records.csv`。

### 目标关联

`association/` 基于 CDT（Common Track Data）模型做跨源目标匹配，配合偏差修正与主目标（primary target）选取，
在多源观测中确定同一目标的对应关系。

### 验证

`verification/` 提供时间、空间、三源一致性的交叉验证，配合 `tests/` 下 15 个 pytest 用例回归。

---

## 数据说明

仓库内 `services/target_standardization/data/` 保留了 MTDSP / MTDSP2 / RadarPWN 的样例数据，
`services/target_standardization/output/scenarios/` 保留了 A/B/C 三场景基线
（固定输入 + 参考真值，pytest 的测试固定装置）。
**完整的原始数据集（GLF、雷达原始帧等）不入库**，
请通过项目文档中记录的获取途径单独下载，并按 `.gitignore` 中的规则放置到对应目录。

---

## License

内部研究项目，版权归项目所有者所有。
