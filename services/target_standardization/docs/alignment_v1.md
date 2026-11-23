# 时空对齐模块 v1.0.0

把雷达 TTM（距离/方位）与 AIS（经纬度）整理到**同一个现场 ENU 坐标系**与
**同一个公共时间栅格**上，产出可追溯的公共时空记录，并给出可量化验收的
误差统计。

## 完成标准与达成情况

> 仅读取标准化数据和运行配置，就能输出可追溯的公共时空记录；真值文件仅供评测。

已达成，并已实测验证：

- 移除真值文件后，对齐流程照常产出记录与质量报告，只是不产出评测报告；
- 更换现场原点后，对齐照常运行，且评测结果对原点选择免疫（真值按经纬度
  在当前原点下重算）。

## 目录

```
alignment/
  config.py       对齐配置（现场基准/雷达站/时间偏移/编码精度/误差模型/栅格）
  transform.py    坐标转换：经纬度 ↔ 现场 ENU、雷达极坐标 → 现场 ENU
  spatial.py      空间对齐（两路统一坐标系，记录不可用原因）
  temporal.py     时间整理与异步匹配（偏移修正 + 线性插值到公共栅格）
  bounds.py       位置误差上界（径向/切向/平面三档）
  verification.py 独立实现的坐标校验（与 transform 不同数学路径）
  evaluation.py   独立评测（只读真值与对齐结果）
  pipeline.py     编排与落盘
configs/alignment/
  alignment_site_rostock.json  默认配置（现场原点=雷达站位置）
  A_geometry_baseline.json     场景 A（含该场景已知偏移）
  B_asynchronous.json          场景 B（含已知偏移 + AIS 量化偏差）
  C_observation_perturbation.json  场景 C（含噪声/量化参数）
```

## 运行命令

### 1. 直接对齐已有的标准化数据（真实数据入口）

```bash
python3 run_alignment.py \
    --radar-records  <radar_standard_records.jsonl> \
    --ais-records    <ais_standard_records.jsonl> \
    --config         configs/alignment/alignment_site_rostock.json \
    --output-dir     output/alignment/run1 \
    [--truth <reference_truth.csv>] [--mapping <target_mapping.json>]
```

`--truth` / `--mapping` 只影响是否产出评测报告，对齐过程不读取它们。

### 2. 按模拟场景一键跑通（含标准化步骤）

```bash
python3 run_scenario_generation.py --all     # 生成 A/B/C 场景数据
python3 run_alignment.py --all                # 标准化 + 对齐 + 评测
python3 run_acceptance.py                     # 汇总验收报告
```

单个场景：`python3 run_alignment.py --scenario A_geometry_baseline`

### 3. 测试

```bash
python3 -m unittest discover -s tests
```

## 产出

每个输出目录（`output/alignment/<场景>/`）：

| 文件 | 内容 |
|---|---|
| `aligned_records.csv` | 逐条对齐记录：ENU 坐标、误差上界、转换状态与失败原因、修正后时间 |
| `aligned_grid_records.csv` | 插值到公共栅格后的记录：插值方式、间隔、前后源记录行号、不可用原因 |
| `alignment_quality.json` | 质量报告：独立坐标校验、配置校验、空间/时间统计、缺失前提汇总 |
| `alignment_evaluation.json` | 评测报告：位置误差、时间误差、覆盖率、界限比较、跨源自洽、时钟偏移修正检验 |
| `standardized/` | 中间产物：两路的标准记录 JSONL |

全局：`output/alignment/acceptance_report.{json,txt}`。

## 参数说明

配置文件的结构（`AlignmentConfig.from_dict`）：

### `site_frame` —— 现场坐标基准

| 参数 | 说明 |
|---|---|
| `frame_id` | 坐标系标识，出现在每条输出记录里 |
| `datum` | 基准，目前只支持 `WGS84` |
| `origin_latitude_deg` / `origin_longitude_deg` | 现场 ENU 原点（必填，缺则空间转换报 `missing_site_frame_origin`） |
| `origin_height_m` | 原点高程 |

### `radar_station` —— 雷达站

| 参数 | 说明 |
|---|---|
| `station_latitude_deg` / `station_longitude_deg` | 站址（必填，缺则报 `missing_radar_station_position`） |
| `install_heading_deg` | 雷达天线安装方向。**不参与方位换算**——TTM 方位参考为 T 时已是真方位，再叠加会引入双重旋转；仅作记录与追溯 |
| `reference_point_offset_*_m` | 位置参考点相对站址的偏移 |
| `magnetic_variation_deg` | 磁差。缺省时 M 参考方位无法换算，相关记录标记 `bearing_reference_m_not_convertible` |

### `sources[].time` —— 时间修正

| 参数 | 说明 |
|---|---|
| `known_clock_offset_s` | 已知时钟偏移。正表示该源读数比公共基准**快**多少；修正后时刻 = 读数 − 偏移 |
| `time_resolution_s` | 该源时间字段的分辨率，用于如实标注不可还原的亚秒部分 |
| `time_quantum_s` | 时间字段的量化步长（0 表示不量化） |
| `known_quantum_bias_s` | 量化引入的**已知恒定偏差**（默认 0）。报文时间字段只有整秒分辨率时为 1.0；该值必须显式给出——被截断掉的那一半无法从报文反推 |
| `use_state_time_utc` / `fallback_to_receive_time` | 时间整理的字段选择 |

### `sources[].encoding` —— 报文编码精度

用于计算位置误差上界：

| 参数 | 说明 |
|---|---|
| `distance_lsb` + `distance_unit` | 距离字段的最小刻度（TTM 编码为 3 位小数海里 → 0.001） |
| `bearing_lsb_deg` | 方位字段的最小刻度（TTM 为 2 位小数度 → 0.01） |
| `position_lsb_deg` | 位置字段（AIS 经纬度）的**线上**最小刻度 |
| `decoded_position_lsb_deg` | 解码端输出的最小刻度（pyais 把经纬度舍入到 1e-5 度 → 0.00001） |

### `sources[].error_model` —— 传感器误差模型

| 参数 | 说明 |
|---|---|
| `position_noise_std_m` | 位置噪声标准差 σ |
| `noise_sigma_multiplier` | 噪声的置信倍数 k，默认 3 |
| `quantization_lsb_deg` | 观测侧主动施加的量化步长 |

### 栅格与插值

| 参数 | 说明 |
|---|---|
| `output_time_step_s` | 公共时间栅格间隔 |
| `max_interpolation_gap_s` | 允许的最大插值间隔，超过则该栅格点标记 `gap_exceeds_limit` |
| `interpolation_mode` | `enu_first`（基线：先转 ENU 再线性插值）或 `observation_domain`（在各自观测域内插值后再转 ENU） |
| `time_grid_start_seconds_of_day` | 栅格起点的日内秒 |

## 实现要点

### 水平位置的定义：局部切平面投影

水平位置采用**局部切平面**投影：

```
North = (lat − lat0) · M0
East  = (lon − lon0) · N0 · cos(lat0)
```

`M0`/`N0` 是原点处的子午圈与卯酉圈曲率半径。

不用"经纬度 → ECEF → ENU"的三维映射定义水平位置：该映射会一并确定目标高程，
把目标压回海平面再转回等价于改变其水平距离，偏差随距离增长（12.3 km 处约
0.023 m）——足以让径向误差略微超出报文量化界限。切平面投影是精确的二维双射，
往返无损，雷达与 AIS 两条路径因此共享同一个位置定义。

代价是切平面与椭球大地线相差 O(d²/2R)（3 km 处约 0.36 m），这是局部平面
近似本身的固有量，已在校验报告中如实标注。

### 雷达的极坐标转换

TTM 的距离与方位定义在**雷达站址处的局部水平面**内，因此：

```
d_east  = R · sin(θ)
d_north = R · cos(θ)
→ 站址局部 ENU → 大地坐标 → 现场 ENU
```

方位参考为 `T` 时方位已是真方位，**不叠加**安装方向或本船艏向。
`M` 参考需要磁差，`R` 参考需要本船艏向；缺少时记录标记不可用而不是猜测。

### 误差上界

误差向量在雷达几何下分解为径向与切向：

```
e = e_rad · (sinθ, cosθ) + e_tan · (cosθ, −sinθ)
```

距离量化只影响径向，方位量化只影响切向（且随距离线性放大）。噪声按**逐轴**
k·σ 计入——不按 √2 分摊到两轴，否则逐轴比较会把未超限的记录误判为超限。

高斯噪声无界，因此 k·σ 是置信上界而非严格上界；评测报告超限点数与比例
（k=3 时约 0.27%/轴），实测 0.23%，与理论一致。

### 独立坐标校验

坐标转换的正确性是全部验收结论的前提。若校验代码与被校验代码共用实现，
两边出现同一错误时校验会"通过"，反而掩盖问题。

`verification.py` 用**另一套数学**独立实现并校验五项：

| 校验 | 内容 |
|---|---|
| `axis_orientation` | 正北 → north 为正、正东 → east 为正（正负号搞反时误差平方和看起来仍正常，最难发现） |
| `enu_roundtrip` | ENU → 经纬度 → ENU 往返残差（要求 ≤ 10 mm，实测 0） |
| `ecef_consistency` | 与独立 ECEF 路径的差异落在解析预期 d²/2R 内 |
| `tangent_plane_cross` | 与独立切平面公式逐位一致（要求 ≤ 1 µm，实测 0） |
| `geodesic_consistency` | 与 Vincenty 椭球反解的距离/方位对照 |

## 验收结果

`python3 run_acceptance.py` 生成 `output/alignment/acceptance_report.txt`。
A/B/C 三场景的独立坐标校验全部通过，位置误差全部落在上界内。

### 场景 A：坐标方向、单位、参考点

- 独立坐标校验 5/5 通过（轴向、往返、ECEF 交叉、切平面交叉、大地测量恒等式）；
- 雷达平面误差 mean 0.63 m / max 1.36 m，AIS mean 0.07 m / max 0.15 m；
- 逐点误差全部落在按报文编码精度算出的上界内（雷达 1086/1086、AIS 1086/1086，
  平面与径向/切向均无超限）。

雷达最大误差 1.36 m 的来源：距离量化 ±0.926 m（径向）与方位量化 ±1.08 m
（切向，在 12.3 km 处）的合成上界 1.42 m。实测值落在界内，
径向/切向分解与量化分量逐项对应。

### 场景 B：异步与时间偏移

- 雷达已知偏移 +0.25 s：修正前 +0.2500 s → 修正后 0.0000 s；
- AIS 读数偏差 −1.0 s = 时钟偏移 −0.5 s + 时间字段整秒截断 −0.5 s，
  两项分别配置、分别修正，修正后残差 0.0000 s，残差已被完整解释；
- 公共栅格 1737 点，其中 1412 点由插值补充（雷达 543、AIS 869），1 点
  因超出航迹范围不可用（不外推）。

AIS 整秒精度的影响单独说明：`second` 字段只有 1 s 分辨率，仿真时刻
15:00:00.5 在报文中只能表达为 15:00:00。被截断的那半秒**无法从报文反推**
（报文只保留了所在的整秒），因此只能作为已知先验显式配置，而不是自动推断。

### 场景 C：观测扰动

| 问题 | 回答 |
|---|---|
| 实际收到多少观测？ | 雷达 1074/1086（漏报 12），AIS 540/543（漏报 3）；未解释缺失 0 |
| 插值补了多少点？ | 雷达 13 点、AIS 539 点插值补充 |
| 多少点因间隔过长不可用？ | AIS 7 点（`gap_exceeds_limit`）+ 1 点（`outside_track_range`），共 8 点 |
| 误差统计用了哪些有效点？ | 1614 条（原始记录级）/ 2164 条（栅格级）；无状态时间 0、真值配对失败 0、未转换 0 |

误差：雷达 planar mean 15.3 m / max 50.8 m，AIS mean 32.5 m / max 106.0 m，
与配置的噪声量级（12 m / 25 m）一致。平面超限 0 条；逐轴超限 5 条
（0.233%/轴），来自高斯噪声尾部，与 3σ 理论 0.27% 一致。

## 数值边界约定

界限是**闭区间**：距离量化误差恰好等于 ±LSB/2 时（真值正好落在两个刻度的
中点上），实测误差会等于上界。浮点表示上这可能表现为超出上界约 0.05 mm。
评测比较时留 0.1 mm 容差（`AlignmentEvaluator.COMPARISON_TOLERANCE_M`），
避免把"恰好在边界上"判成超限。

## 缺失前提的处理

缺少站址、外参或可靠时间时，输出明确的不可用原因，不静默丢弃：

| 情况 | `alignment_status` | `alignment_failure_reason` |
|---|---|---|
| 缺现场原点 | `failed` | `missing_site_frame_origin` |
| 缺雷达站位置 | `failed` | `missing_radar_station_position` |
| M 参考但无磁差 | `failed` | `bearing_reference_m_not_convertible` |
| R 参考但无本船艏向 | `failed` | `bearing_reference_r_not_convertible` |
| 无任何时间基准 | 不进入插值序列 | `no_reliable_time_base` |
| 源类型不支持 | `failed` | `unsupported_source_type:<type>` |

汇总在 `alignment_quality.json` 的 `missing_requirements` 段。

## 真实数据验证（2026-11-08）

除模拟场景外，模块已在**两个真实数据集**上完成端到端验证。

### MTDSP（岸基固定雷达站，烟台）

数据来源：海上目标数据共享计划（MTDSP），DOI `10.12000/JR25001`，海军航空大学采集。
两个采集包：`03-05`（522 雷达 + 260 AIS + 279 光电）与 `03-12`（1620 雷达 + 660 AIS + 317 光电）。

| 验证项 | 脚本 | 03-05 | 03-12 |
|---|---|---|---|
| 空间自洽（AIS 双重表达互检） | `verification/verify_mtdsp_spatial.py` | 通过 | 通过 |
| 三源一致性 | `verification/verify_mtdsp_three_source.py` | 通过 | 通过 |
| 时间对齐 | `verification/verify_mtdsp_temporal.py` | 通过 | 通过 |
| 跨源误差 | `verification/evaluate_mtdsp_cross_source.py` | 通过 | 通过 |

**时间对齐独立验证**（不复用模块插值实现，重新实现一套再逐点比对）：

| | 03-05 | 03-12 |
|---|---|---|
| 模块栅格点 / 独立重算 / 共同点 | 2671 / 2671 / 2671 | 9013 / 9013 / 9013 |
| **位置残差 max** | **0.000000000 m** | **0.000000000 m** |
| gap 拒绝判定不一致数 | 0 | 0 |
| 栅格间距违规数 | 0 | 0 |

**跨源时序残差**（同一栅格时刻各源插值位置之差）：

| 源对 | 03-05 中位 | 03-12 中位 | 解释 |
|---|---|---|---|
| AIS vs 雷达 | 4.84 m | 13.18 m | 基准一致 |
| 光电 vs 雷达 | 357.31 m | 436.83 m | 光电方位零点偏差的切向量级 |
| AIS vs 光电 | 357.59 m | 427.40 m | 同上 |

残差稳定在数百米量级而非随目标速度发散，即为时间对齐正确的直接证据。

**时区**：数据集中无任何时区声明（无元数据字段、无包内说明、文件名无时区标注）。
三个源均为 naive 本地时间。**不臆造时区** —— 标准化层保持
`timezone_known=false` / `time_domain=local_naive` / `state_time_utc=None`，
配置以顶层 `time_basis` 块显式声明 `absolute_timezone: "unresolved"`。

三源**共用同一时钟**（相对时基确定），证据是数据集自带的跨设备时间对应：

- 雷达 vs 光电：`CutDataTarget.csv` 的「光电匹配时间」与该帧「起始时间(帧)」之差
  ∈ {0, −1, −2} s（中位 −1 s），|差| ≤ 2 s 远小于雷达旋转周期 2.5 s 与光电采样 5 s；
- AIS vs 雷达：AT 行时刻与其拷贝的 AIS 报告时刻之差（中位 +4 s / +6 s），
  等于 **AIS 报告周期**而非时钟偏差。

### RadarPWN（机载雷达，罗斯托克）

数据来源：[RadarPWN v1.0.2](https://zenodo.org/records/7188636)，DOI `10.1109/LCN53696.2022.9843801`。

| 项 | 结果 |
|---|---|
| 标准化 | 雷达 383 条（TTM）、AIS 7719 条（VDM），全部可用于时空对齐 |
| 空间转换 | 雷达 383/383 成功；AIS 7701/7719 成功，18 条位置不可用（已记原因） |
| 坐标独立校验 | 全部通过 |
| 时间对齐 | 74 条航迹，1089 个栅格点，雷达侧 0 不可用 |

配置：`configs/alignment/alignment_site_rostock_radarpwn.json`。

**注意**：RadarPWN 的 TTM 距离/方位相对**本船**（机载雷达），站址即本船位置，
与 MTDSP 的岸基固定站不同源。雷达目标 `00` 距离中位 1296 m，而最近的 AIS 目标
在 2106 m —— 雷达跟踪的是 AIS 覆盖范围外的近距小目标，**本就没有 AIS 对应**。
这不是缺陷，而是验证了模块能处理「雷达有目标、AIS 无对应」的真实情形。

### 数据集特性与已知限制

1. **光电 Inf/TV 双设备并列**：Inf 与 TV 是两个独立设备，共享同一 `local_track_id`
   与同一时间戳，但位置不同（03-12 实测 10:14:02 有 north=4999.52 与 north=4877.87
   两条，相距 122 m；全包 137 组同时刻记录中 55 组位置不同）。这不是重复记录，
   而是同一时刻的两次独立测量。对齐模块的 `_bracket` 二分在重复键上收敛到
   重复段的特定一条（落在重复段之前 ⇒ 第一条；正好等于重复时刻 ⇒ 最后一条）。
   该行为确定但**依赖记录顺序**，属隐式约定。
   ⚠️ 下游跨源关联若把光电当作单序列使用，必须先决定如何处置这两个设备的并列观测
   （取其一 / 平均 / 分别保留为两条航迹）。彻底解决应给 Inf/TV 分配不同
   `local_track_id`（标准化层改动）。
2. **AIS 极坐标列的逐目标参考偏差**：AIS 行内的极坐标列与同行经纬度列之间存在
   逐目标稳定偏差（03-12 实测三目标分别 −8.6 m / +5.9 m / −21.0 m；
   03-05 亦有逐目标差异）。这不是随机测距误差，空间验证已改为**逐目标去偏后**
   评估测距随机精度。
3. **超量程目标**（> 6 km）解算不可信，已从精度判据中剔除并单列报告。
   03-12 包的 412355160 全程 25~36 km（575/660 样本），若不剔除会主导全部统计。
4. **AT 行不是独立测量**：AT 行的方位/距离是 AIS 极坐标的快照拷贝
   （逐点残差 0.0000°/0.000 m），不能当作独立雷达观测参与误差统计。

## 版本

- 模块版本：`alignment v1.0.0`（`alignment/config.py` 的 `MODULE_VERSION`）
- 参数版本：`parameter_version`，用于区分"换了参数的同版本"

冻结后进入下一阶段：多目标场景与航迹关联。
