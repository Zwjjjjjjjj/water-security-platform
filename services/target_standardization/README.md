# 标准化、模拟场景生成与时空对齐

三个数据源（RadarPWN 雷达 TTM、RadarPWN 包内 AIS !AIVDM、MTDSP AIS CSV）分别适配，
输出同一套公共标准记录；再以 RadarPWN AIS 轨迹为模板生成同场景的
雷达—AIS 模拟数据集，用于验证对齐流程。

标准化阶段只做"表达统一"，不含坐标转换、时钟偏移修正与状态预测。

## 目录

> 第三部分（时空对齐模块 v1.0.0）的完整文档见
> [`docs/alignment_v1.md`](docs/alignment_v1.md)。

```
target_standardization/
├── standardization/       公共标准化
│   schema.py             公共记录结构、字段约束、record_id 生成
│   units.py              距离/速度/角度换算（含 TTM 第 10 字段单位表）
│   time_fields.py        时间解析、来源标记、仿真日期上下文与 ZDA 关联
│   validation.py         字段检查、异常分类、有效性判定、重复与跳变检查
│   radar_ttm_adapter.py  TTM 读取、解析与字段映射
│   ais_vdm_adapter.py    RadarPWN 包内 !AIVDM 报文流适配
│   ais_csv_adapter.py    MTDSP AIS 动态 CSV 与静态属性补充
│   pipeline.py           组织适配器/校验器/输出
├── simulation/            模拟场景生成
│   geo.py                局部 ENU 坐标与方位/距离换算
│   trajectory.py         从实测 AIS 轨迹提取共同参考轨迹
│   nmea_encode.py        TTM / ZDA / RMC / AIS !AIVDM 编码
│   scenario.py           场景配置、观测生成与落盘
├── alignment/             时空对齐
│   config.py             对齐配置（现场基准、雷达站、时间偏移、插值参数）
│   transform.py          ECEF ↔ 现场 ENU、雷达极坐标 → ENU
│   spatial.py            空间对齐（两路统一到同一坐标系）
│   temporal.py           时间整理与异步匹配（区间内线性插值）
│   evaluation.py         独立评测（只读真值与对应关系）
│   pipeline.py           流水线编排与落盘
├── geodesy.py             坐标数学唯一来源（WGS84 ECEF ↔ 局部 ENU）
├── configs/
│   radarpwn_radar.json     雷达 TTM 配置（字段映射按索引）
│   radarpwn_ais_vdm.json   RadarPWN AIS VDM 配置
│   mtdsp_ais.json          MTDSP AIS CSV 配置（字段映射按表头名）
│   scenarios/              模拟场景 A/B/C 配置
│   alignment/              对齐配置（含各场景的已知时钟偏移）
├── data/
│   radarpwn/             雷达包数据（TTM / all_incoming / AIS VDM）
│   mtdsp/                MTDSP AIS CSV（动态 + 静态）
├── tests/                 验收测试（真实数据 + 明确标注的构造场景）
├── output/                运行产物（standardization/、scenarios/、alignment/）
├── run_standardization.py     标准化命令行入口
├── run_scenario_generation.py 场景生成命令行入口
└── run_alignment.py           时空对齐与评测命令行入口
```

## 三个数据源一览

| 数据源 | source_type | 输入 | 输出条数 | 拒绝 |
|---|---|---|---|---|
| RadarPWN 雷达 TTM | `radar` | `radar_ttm_with_capture_time.csv` | 383 | 0 |
| RadarPWN 包内 AIS | `ais_vdm` | `ais_with_capture_time.csv` | 7719 | 0 |
| MTDSP AIS | `ais` | `AIS_Trajectory_*.csv` | 260 | 0 |

## 调用方式

```python
from standardization import standardize
standardize(input_path, source_config, output_dir)
```

命令行（一次运行全部三个数据源）：

```bash
python3 run_standardization.py --all
```

单独运行某个数据源：

```bash
python3 run_standardization.py --config configs/radarpwn_radar.json \
    --input data/radarpwn/radar_ttm_with_capture_time.csv \
    --all-incoming data/radarpwn/all_incoming_nmea_with_capture_time.csv \
    --output output/radar_ttm

python3 run_standardization.py --config configs/radarpwn_ais_vdm.json \
    --input data/radarpwn/ais_with_capture_time.csv \
    --all-incoming data/radarpwn/all_incoming_nmea_with_capture_time.csv \
    --output output/radarpwn_ais

python3 run_standardization.py --config configs/mtdsp_ais.json \
    --input data/mtdsp --static-dir data/mtdsp \
    --output output/mtdsp_ais
```

## 公共输出结构

字段分组与填写规则见 `schema.py` 的 `FIELD_ORDER`。要点：

- `session_id` 区分采集批次；`local_track_id` 雷达为本地编号（保留前导零），AIS 为 MMSI。
- `record_id = sha1(source_id|session_id|source_file|source_row)[:16]`。
  `source_file` 保存为**相对于数据根目录的稳定路径**（`source_config["data_root"]`，
  缺省为输入路径所在目录），因此移动整个数据目录不会改变 ID；
  `session_id` 纳入键中，同一编号在不同批次复用时 ID 不同。
- `state_time_raw` 保留原值，`state_time_utc` 仅在有可靠依据时填写。
- `position_type` 区分 `range_bearing`（雷达）与 `latlon`（AIS），`frame_id` 标明坐标系。
- `course_reference`（真/相对）与 `motion_reference` 分开；COG 与船首向分别落在
  `course_rad` 与 `heading_rad`。
- `issues` 记录限制；`valid_for_spatial_alignment` / `valid_for_time_alignment`
  依据下列明确条件判定。
- `source_file` / `source_row` / `raw_record` 保证每条输出可回查。

### valid_for_* 的判定条件

**valid_for_spatial_alignment = true** 当且仅当：

- `position_type = latlon`：经度、纬度均存在且在合法范围内，且 `frame_id` 已知；
- `position_type = range_bearing`：距离、方位均存在，距离非负，方位在 [0, 2π) 内，
  且方位参考（T/M/R）已知。

该标记只表示**本记录自身**的空间表达完整可用。跨源对齐还需要数据集层面的条件
（雷达外参、本船位置、坐标系换算关系），这些缺失项逐条列在记录的
`spatial_alignment_requirements_missing` 字段中：

- 雷达记录典型值：`["own_ship_position", "radar_extrinsics"]`；
- AIS 经纬度记录典型值：`[]`。

**valid_for_time_alignment = true** 当且仅当：`state_time_utc` 存在，
且 `time_precision`、`time_basis` 均已声明（`time_precision` 不为 `unknown`）。

该标记只表示**状态时间可用**。它不代表具备状态预测条件——速度缺失或运动参考
未知时，相关限制记录在 `issues` 中。

## 雷达侧核验结果

### TTM 字段布局（标准布局，按厂家手册）

| 手册字段 | 含义 | 配置索引（0-based） |
|---|---|---|
| 1 | 目标编号 | 0 |
| 2 | 距离 | 1 |
| 3 | 方位（度） | 2 |
| 4 | 方位参考 T/M/R | 3 |
| 5 | 速度 | 4 |
| 6 | 航向（度） | 5 |
| 7 | 航向参考 T/M/R | 6 |
| 8 | CPA 距离 | 7 |
| 9 | CPA 时间 | 8 |
| 10 | Speed/distance units | 9 |
| 11 | 目标名称 | 10 |
| 12 | 跟踪状态 Q/T/L | 11 |
| 13 | 参考目标标记 | 12 |
| 14 | 日内时间（仿真 UTC 日内时间） | 13 |
| 15 | 获取类型 A/M | 14 |

索引计数说明：配置中的索引为 0-based，**不含**开头的 `$RATTM`；手册中的
"第 N 字段"对应索引 N-1。`$RATTM` 首字段在解析时剥离。

### 单位来自报文本身，不做假定

第 10 字段为 "Speed/distance units"，同时规定距离与速度单位：

| 标记 | 距离单位 | 速度单位 |
|---|---|---|
| N | 海里 | 节 |
| K | 千米 | 千米/小时 |
| S | 英里 | 英里/小时 |

本数据集该字段为 `N`，因此距离按海里、速度按节换算，单位来源记录为
`unit_source = "ttm_field_10_speed_distance_units"`，记录中**不再出现**
`speed_unit_assumed`。配置里的 `unit_fallback` 仅在报文单位字段缺失时兜底。

速度的**运动参考**（对地/对水）与速度**单位**是两件事：报文未声明运动参考，
`motion_reference` 保持未知，不生成 vx、vy。

### 时间核验与 ZDA 关联规则

不假设 406 条 ZDA 与 383 条 TTM "一一对应"。实际采用的关联规则：

> 对每条 TTM，按其仿真日内时间在**同一数据流**中选取"时间不晚于该 TTM 的
> 最近一条 ZDA"，用该 ZDA 的日期声明与 TTM 日内时间组合出仿真 UTC。

每条记录保存 `zda_association`：

| 字段 | 含义 |
|---|---|
| `zda_csv_row` | 所用 ZDA 在 `all_incoming_nmea_with_capture_time.csv` 中的行号 |
| `zda_time_of_day` | 所用 ZDA 的日内时间原值 |
| `sim_time_delta_s` | TTM 日内时间 − ZDA 日内时间（秒，应 ≥ 0） |
| `capture_time_delta_s` | 两条报文的抓包时间差（秒） |
| `residual_s` | `capture_time_delta_s − sim_time_delta_s`，两个时钟在该区间上的偏差 |
| `association` | `ok` / `fallback_earliest` / `no_zda` / `no_context` |

核验结果：

- 406 条 ZDA 的日期声明全部为 2019-09-30，无日期冲突；
- ZDA 日内时间无回退（0 处），无跨午夜；
- 383 条 TTM 全部成功关联（`association = ok`），用到的 ZDA 行号为 272 个不同行；
- TTM 仿真日内时间无回退；
- 残差（`capture_time_delta_s − sim_time_delta_s`）范围 −0.814 s ~ +0.911 s，
  均值 −0.107 s。

关于两个时长的口径（此前表述不够明确）：

- `1089.910 s` = **第一条 ZDA 到最后一条 ZDA 的抓包时间跨度**（记录时钟，2022-06-03）；
- `1093.000 s` = **同两条 ZDA 的仿真日内时间跨度**（仿真时钟，2019-09-30）；
- 两者用的是**同一组起止记录**（ZDA 流的首末两条），因此可以直接比较，说明仿真
  日内时间相对抓包时钟略慢（约 0.28%）。
- 作为对照，TTM 流自身的跨度为抓包 1038.015 s / 仿真 1041.000 s。

仿真时间与抓包时间分开保存，两条时间线不互相换算。

## RadarPWN 包内 AIS（!AIVDM）核验结果

**数据包中确实含有 AIS 轨迹数据**，此前未纳入处理。文件 `ais.nmea`（7719 行）与
`ais_with_capture_time.csv`（7719 行，带 PCAP 记录时间）均为原始 `!AIVDM` 报文。

核验结果：

- 7719 条报文全部为 AIS **位置报文**（消息类型 1/2/3），校验和全部通过，可全部解码；
- 解码后得到 **76 个 MMSI** 的动态位置轨迹；
- 位置在德国波罗的海海域（约 54°N / 12°E），速度 0–20 kn，航行状态以
  UnderWayUsingEngine / AtAnchor 为主；
- **18 条报文的位置不可用**（`lat=0 且 lon=0` 的哨兵值）。这类报文本身有效，
  只是位置缺失 → **输出并标记限制**（`missing_position`、
  `valid_for_spatial_alignment = false`），不丢弃、不拒绝；
- 报文自带的 `second` 是**仿真时钟**的分钟内秒数（0–59），跨分钟回绕。

### 时间还原（与雷达 TTM 侧不同，需两步）

`second` 属于仿真时钟，抓包时间属于记录时钟，两者相差约 **13 小时 16 分**
（抓包 16:31 ↔ 仿真 15:00），且速率略有差异（实测约 0.28%）。因此不能用抓包
时间的分钟直接拼报文秒数。还原步骤：

1. 用同一数据流中的 ZDA 条目作锚点（取抓包时间不晚于它的最近一条），把抓包
   时间映射到仿真日内时间；
2. 锚点推进值可能落在分钟边界附近，用报文自身的 `second` 在相邻两个分钟候选
   中选择偏差最小者，确定仿真分钟；
3. 仿真 UTC = ZDA 日期 + 还原后的仿真日内时间。

实测钟差（锚点推进值 − 还原值）范围 **−1.513 s ~ +1.218 s**，逐条记录在
`zda_association.clock_skew_s` 中。每条记录的 `zda_association` 还给出
`anchor_zda_csv_row`、`capture_delta_s` 与 `method`。

> 与雷达 TTM 的关系：TTM 报文自带完整日内时间（第 14 字段），还原是直接取值；
> VDM 只有分钟内秒数，必须借 ZDA 锚点推算。两者都以 ZDA 日期补全，规则一致。

### 与雷达目标的关系

**这两者是不同场景，不构成可直接配对的目标对。**

- 雷达目标 00 的距离范围 0–1.2 海里；包内 76 条 AIS 航迹到本船（RMC 位置
  5408.298 N / 01205.688 E）的距离为 1.14–19.44 海里；
- 雷达 TTM 目标的速度约 4 节，AIS 航迹速度 0–20 kn。

因此 RadarPWN AIS 只能作为**同场景下的另一类观测**做独立标准化，不能据此认定
任何 MMSI 与雷达目标 00 对应。README 原述"未证明目标 00 与某个 AIS MMSI 的对应关系"
仍然成立。

## MTDSP AIS 侧核验结果

- `AIS_Trajectory_*.csv` 共 6 个文件、260 条有效动态记录；编码 UTF-8 with BOM、CRLF。
- 表头：`MMSI, 经度(°), 纬度(°), 时间, 速度(kn), 船艏向(°), 对地航向(°),
  航行状态, 相对视角(°), 姿态信息, 浪高(m), 方位角(°), 距离(m)`。
- 时间原值为 `2025-03-05 10:38:21`，秒级精度（不受 Excel 分钟级显示影响）。
- 时间字符串不含时区标记 → 未确认时区，`state_time_utc` 留空，
  `time_domain = local_naive`，并标 `timezone_unknown` / `utc_time_unknown`。
  配置中一旦声明 `timezone_known` 与 `timezone_name`，UTC 才会补全（已有测试覆盖）。
- 速度按表头声明的节转换为 m/s，`speed_unit_source = "csv_header_declared"`；
  对地航向与船艏向分开保存。
- 动态 CSV 的方位角/距离定义未确认，只作为扩展字段 `bearing_deg_unverified` /
  `distance_m_unverified` 保留，位置以经纬度为主。
- 静态属性通过 `AIS_ShipInformation_<MMSI>.csv` 按 MMSI 关联，存入 `static_info`，
  并记录 `static_info_source`。

## 位置跳变检测

### 量化分辨率的依据

量化裕度**不由相邻位置差值反推**——相邻差值同时包含量化误差与真实运动，
反推结果可能远大于真实分辨率，从而掩盖真实跳变。

改为按数据说明/已核验的原始 CSV 数值分辨率配置，经纬度分别设置：

| 字段 | 分辨率 | 依据 | confident |
|---|---|---|---|
| 经度 | 0.001° | 已核验的 CSV 小数位（多数记录 3 位） | true |
| 纬度 | 0.0001° | 已核验的 CSV 小数位（多数记录 4 位） | true |

少数记录小数位更少，属截断而非更粗量化。

换算按各自分方向进行，不能统一取值：37.56° 附近 0.001° 经度约 88 m，
0.001° 纬度约 111 m。

### 判定规则与保留量

每条记录保留完整的三组量，便于人工复核：

- `raw_distance_m` / `raw_speed_mps`：由原始经纬度直接计算；
- `quantization_margin_m`：按配置分辨率换算（经度与纬度分别换算后合成）；
- `adjusted_speed_mps`：扣除裕度后的速度。

判定：

- 量化依据可靠（两轴均 `confident = true`）：以 `adjusted_speed_mps` 超阈值告警；
- 量化依据不可靠：**不自动扣除裕度**，`raw_speed_mps` 与 `adjusted_speed_mps`
  并列保留，告警同时打上 `position_jump_uncertain`，交人工复核。

测试覆盖两类场景：粗量化下的正常航行不告警；同样粗量化下的真实跳变必须告警。

### 本批数据结果

**采用当前规则后，本批数据未触发跳变告警**（0 条）。此前用"最小非零步长"反推
裕度时出现过 7 条告警；该反推方式已移除。无独立真值，因此**不能认定误报已经
消除**，只能说当前规则下未触发。

## 样例核对

报文 `$RATTM,00,0.1,9.7,T,4.0,2.7,T,0.0,0.3,N,TGT00,T,,150054.00,A*48` 的标准化结果：

| 项目 | 结果 |
|---|---|
| 本地编号 | `"00"` |
| 距离 | 185.2 m（0.1 海里，第 10 字段 N） |
| 方位 | 0.169296937 rad，真北参考 |
| 速度 | 2.057778 m/s（4.0 节，第 10 字段 N） |
| 航向 | 0.047123890 rad，参考 `T` |
| 跟踪状态 | tracking（`T`） |
| 状态时间 | 2019-09-30T15:00:54+00:00（ZDA 日期 + TTM 日内时间） |
| 接收时间 | 2022-06-03T16:31:48.952602+00:00（PCAP 记录时间） |
| ZDA 关联 | 行 464，日内时间 150054.00，残差 −0.361 s |

## 异常处理

- 拒绝并保留原始记录：TTM 校验和错误、非 TTM 报文、字段无法解析、位置数值非法；
  AIS 缺 MMSI、纬度/经度越界、负速度、时间缺失、VDM 解码失败。
- 输出并标记限制：缺速度、缺完整 UTC 状态时间、运动参考未知、时区未知、
  静态属性缺失、重复记录（标记不删除）、位置跳变（报告不改轨迹）、
  跳变判定不确定、**位置不可用（VDM 的 lat=lon=0）**。
- 缺失值不填 0；重复记录不自动删除。

## 验收结果

`python3 -m unittest discover -s tests`：52 项全部通过。

| 验收项 | 结果 |
|---|---|
| 数量可追溯 | 雷达 383 = 383 + 0；RadarPWN AIS 7719 = 7719 + 0；MTDSP AIS 260 = 260 + 0，每条可定位源文件与行号 |
| 雷达正确性 | 383 条全部处理，编号 `00`、状态 `tracking`、原始报文保留 |
| VDM 正确性 | 7719 条全部解码，76 个 MMSI，18 条位置不可用记录标记后保留 |
| 单位正确性 | 海里/节/千米/英里换算与人工计算一致；单位取自 TTM 第 10 字段 |
| AIS 正确性 | 抽样逐字段核对通过，静态属性按 MMSI 关联成功 |
| 时间正确性 | 抓包时间与仿真时间分别保存；ZDA 关联规则明确并逐条留痕；VDM 钟差 ≤ 1.513 s；MTDSP 未知时区不擅自补全 |
| 异常处理 | 坏校验和、非法纬度、空速度字段、重复记录、位置不可用均有测试覆盖 |
| 跳变检测 | 粗量化正常航行不告警；粗量化下真实跳变告警；量化依据不可靠时标记不确定 |
| 接口稳定性 | record_id 跨目录移动保持稳定；跨 session 不相同；valid_for_* 条件已列出 |

## 尚未完成 / 后续阶段

- 多目标、`Q/L` 状态、编号复用场景在真实数据中不存在，由
  `tests/test_synthetic_scenarios.py` 构造并明确标注为测试数据。
- 现场坐标转换、时钟偏移修正、状态预测属于下一阶段时空对齐模块；
  雷达侧还需补齐本船位置与外参，才能真正进入跨源对齐。
- MTDSP AIS 时区需向数据提供方确认后再在配置中声明，本阶段不擅自假设。
- 雷达侧速度的参考（对地/对水）仍未核实，`motion_reference` 保持未知。
- RadarPWN 包内 AIS 与雷达目标 00 是否存在对应关系**仍未确认**。同一抓包说明
  二者来自同一场景，但距离与速度的总体范围不足以排除对应关系。

---

# 第二部分：同场景雷达—AIS 模拟数据集

## 两处数据语义的澄清

1. **"未找到同目标对应关系" ≠ "不是同一场景"。** RadarPWN 的 TTM 与 AIS VDM
   来自同一次抓包，属于同一场景；准确表述是"**尚未确认任何 MMSI 与 TTM 目标
   00 对应**"。距离与速度的总体范围不足以排除对应关系。

2. **(0, 0) 是本数据集的疑似占位值，不是通用规则。** AIS 标准的位置不可用
   编码是**纬度 91°、经度 181°**；通用适配器不把 `(0, 0)` 一律判为不可用。
   本数据集的 `(0, 0)` 通过 RadarPWN 来源配置的 `placeholder_positions`
   声明为本场景下的异常占位位置：

   ```json
   "placeholder_positions": [[0.0, 0.0]]
   ```

   适配器分两层判定：`ais_unavailable_encoding`（通用，不可配置关闭）与
   `suspected_placeholder_position`（来源配置声明）。未声明时 `(0,0)` 被视为
   普通有效位置——已有测试覆盖这两种配置。

## 共同参考轨迹

从 76 个 MMSI 中按评分挑选模板：点数、实际位移、采样间隔稳定性、无大间隔、
航向与位移方向一致性。选中的 **228050803**（173 点，5.41 nm，采样间隔中位
6.23 s，无 >15 s 间隙，COG 与实际运动方向平均差 0.62°）。

提取步骤（全部记录在 `scenario_manifest.json` 的 `reference_trajectory`）：

1. 读取并解码 `!AIVDM`，剔除 `(0,0)` 占位值与 91/181 编码；
2. 转为局部 ENU 米制坐标（原点 = 模板首点）；
3. 剔除离群点（与前后点线性插值的偏差 > 60 m）；
4. 重采样到 1 s 均匀栅格；
5. 滑动平均平滑（窗口 5，边缘用端点值填充）；
6. 局部最小二乘线性拟合求速度与航向。

> 实现中修正了两处会污染真值的问题：滑动平均若按"可用点变少"处理会在端部
> 伪造速度渐变（首端 2.1 m/s → 8.4 m/s）；相邻差分在端点跨度与除数不匹配会
> 得到偏小值（首点 5.03 m/s vs 中位 9.27 m/s）。改为端点填充 + 局部最小二乘后，
> 速度序列为 7.54–9.79 m/s，连续平滑。

**语义**：该轨迹代表**新模拟场景中的设定运动**，取自实测轨迹的形态，
**不代表任何原始船舶的实测真值**。

## 场景生成

```bash
python3 run_scenario_generation.py --all          # 生成 A/B/C
python3 run_scenario_generation.py --scenario A_geometry_baseline
```

每个场景一个目录，产出 6 个文件：

| 文件 | 内容 |
|---|---|
| `radar_ttm_with_capture_time.csv` | 雷达 TTM 观测（可被现有适配器直接读取） |
| `ais_with_capture_time.csv` | AIS !AIVDM 观测 |
| `all_incoming_nmea_with_capture_time.csv` | ZDA / RMC 时间与位置上下文 |
| `reference_truth.csv` | 评测真值：参考位置（ENU + 经纬度）、速度、目标编号 |
| `target_mapping.json` | 雷达编号 ↔ 模拟 MMSI 对应表（**仅供评测**） |
| `scenario_manifest.json` | 场景清单与全部来源记录 |

两路观测**分别**从参考轨迹采样，误差也**分别**施加——不把带误差的 AIS 观测
复制成雷达观测。观测状态时间与接收时间分开保存：接收时间 = 状态时间 + 接收
延迟，且落在记录时钟上（与仿真时钟同速、起点不同）。

### 三个场景

| 场景 | 雷达采样 | AIS 采样 | 时钟偏移 | 接收延迟 | 位置误差 | 量化 | 漏报 |
|---|---|---|---|---|---|---|---|
| A 几何基线 | 1 s | 1 s | 0 | 0 | 0 | 0 | 0 |
| B 异步观测 | 2 s | 5 s | 雷达 +0.25 s / AIS −0.5 s | 雷达 0.4 s / AIS 1.2 s | 0 | 0 | 0 |
| C 观测扰动 | 1 s | 2 s | 0 | 雷达 0.2 s / AIS 0.6 s | 雷达 12 m / AIS 25 m | AIS 0.0001° | 雷达 3 s / AIS 5 s |

误差参数为**模拟设定值**，无独立真值支持。

## 场景验证结果

生成 → 现有适配器读取 → 标准记录 → 与真值比对：

| 场景 | 雷达观测 | AIS 观测 | 关键校验 |
|---|---|---|---|
| A | 1086 | 1086 | 雷达距离误差 < 2 m、方位误差 < 0.02°；AIS 位置误差 < 3 m；仿真时间 15:00:00 与接收时间 16:30:00 分离正确 |
| B | 543 | 218 | 采样间隔实测 {2 s} / {5 s}；时钟偏移与接收延迟在状态时间/接收时间上如实体现 |
| C | 1086（漏 12） | 543（漏 3） | 位置误差均值/中位数符合瑞利分布理论值；量化步长可验证；漏报表现为采样间隙 |

**A 场景的几何基线说明单位与坐标转换均正确**：0.1 海里 → 185.2 m、
4.0 节 → 2.057778 m/s，与雷达侧实测数据的换算完全一致。

### 验证中发现的两处格式固有限制

1. **AIS 状态时间只能精确到整秒。** `!AIVDM` 的 `second` 字段是 0–59 的
   分钟内秒数，亚秒偏移无法从报文本身还原。B 场景中 AIS 时钟偏移 −0.5 s，
   真值状态时间为 14:59:59.5，还原后为 14:59:59。雷达 TTM 第 14 字段有
   0.01 s 分辨率，亚秒偏移可完整还原。

2. **分钟候选必须包含前一分钟。** 若按"报文秒数 + 推进值所在分钟"两候选选择，
   当推进值越过分钟边界时会整体推后 60 s。改为按秒数反推候选起点后取三个
   候选（前/中/后），边界情形正确。

## 测试

`python3 -m unittest discover -s tests`：**73 项全部通过**（新增 19 项场景测试）。

新增覆盖：TTM 布局与手册一致、AIS 编解码往返、局部坐标往返、模板选择与
评分、来源记录完整性、三场景端到端回读与真值比对、占位值规则可配置性、
AIS 整秒精度限制。

## 尚未完成 / 后续阶段

- 多目标交叉、接近与分离尚未扩展，当前为单目标。
- 尚未建立跨源目标关联（配对）算法；`target_mapping.json` 是给定的真值，
  不是算法输出。
- 场景 A/B/C 的误差参数没有独立真值支持，仅为设定值。
- 真实数据可提供的采样间隔、缺失模式已用于模板与配置，但真实数据的
  传感器误差未知。

---
# 第三部分：时空对齐模块 v1.0.0

把雷达 TTM（距离/方位）与 AIS（经纬度）整理到**同一现场 ENU 坐标系**与
**同一公共时间栅格**上，产出可追溯的公共时空记录，并给出可量化验收的误差统计。

完整文档见 [`docs/alignment_v1.md`](docs/alignment_v1.md)（参数说明、运行命令、
验收结果、缺失前提处理）。

## 运行命令

```bash
# 1. 直接对齐已有的标准化数据（真实数据入口）
python3 run_alignment.py \
    --radar-records  <radar_standard_records.jsonl> \
    --ais-records    <ais_standard_records.jsonl> \
    --config         configs/alignment/alignment_site_rostock.json \
    --output-dir     output/alignment/run1 \
    [--truth <reference_truth.csv>] [--mapping <target_mapping.json>]

# 2. 模拟场景一键跑通（含标准化步骤）
python3 run_scenario_generation.py --all
python3 run_alignment.py --all
python3 run_acceptance.py          # 汇总验收报告
```

**完成标准的实测**：移除真值文件后，对齐流程照常产出记录与质量报告，只是不
产出评测报告；更换现场原点后对齐照常运行，且评测对原点选择免疫。

## 坐标数学

水平位置采用**局部切平面**投影（`geodesy.py`）：

```
North = (lat − lat0) · M0
East  = (lon − lon0) · N0 · cos(lat0)
```

`M0`/`N0` 为原点处子午圈/卯酉圈曲率半径。

> 不用"经纬度 → ECEF → ENU"的三维映射定义水平位置：该映射会一并确定目标高程，
> 把目标压回海平面再转回等价于改变其水平距离，偏差随距离增长（12.3 km 处约
> 0.023 m）——足以让径向误差略微超出报文量化界限。切平面投影是精确二维双射，
> 往返无损，雷达与 AIS 两条路径因此共享同一个位置定义。

代价是切平面与椭球大地线相差 O(d²/2R)（3 km 处约 0.36 m），这是局部平面近似
本身的固有量，已在校验报告中如实标注。

## 空间转换

| 数据 | 转换流程 |
|---|---|
| AIS | 经纬度 → 现场 ENU |
| 雷达 | 距离、真方位 → 站址局部平面（东 = R·sinθ，北 = R·cosθ）→ 大地坐标 → 现场 ENU |

### 方位参考的处理

| 参考 | 处理 |
|---|---|
| `T`（真北） | 已是真方位，**不叠加**安装方向或本船艏向 |
| `M`（磁北） | 叠加磁差；磁差未配置 → 转换失败并记录原因 |
| `R`（相对艏向） | 叠加本船艏向；艏向未配置 → 转换失败并记录原因 |

`install_heading_deg`（雷达天线安装方向）在标准 TTM 下**不参与**计算，只作记录
与追溯。位置参考点偏移 `reference_point_offset_*_m` 在站址 ENU 上单独叠加。

## 插值方式

两种方式都实现，由配置 `interpolation_mode` 选择：

| 方式 | 说明 |
|---|---|
| `enu_first`（**首轮基线**） | 先转 ENU，再对 east/north 线性插值。与真值同坐标系，逐点可比性最好；转弯段把弧拉成弦（半径 500 m、30° 弧段误差 3.4 m），采样越密误差越小 |
| `observation_domain` | 在各自观测域内插值（雷达插距离与方位，AIS 插经纬度），插完再各自转 ENU。转弯段更贴合，但两路进入 ENU 的路径不同 |

方位跨零（359° → 1°）按最短路径插值，不绕远路。两种方式都**不外推**：
超出航迹范围标记 `outside_track_range`；源间隔超过 `max_interpolation_gap_s`
标记 `gap_exceeds_limit`。源记录空间转换失败时，插值点标记
`source_enu_unavailable` 而不是静默通过。

## 位置误差上界

误差向量在雷达几何下分解为径向与切向：

```
e = e_rad · (sinθ, cosθ) + e_tan · (cosθ, −sinθ)
```

距离量化只影响径向，方位量化只影响切向（随距离线性放大）。各源还要计入
解码端舍入（AIS 解码把经纬度舍入到 1e-5 度）与观测侧量化。

**噪声按逐轴 k·σ 计入**，不按 √2 分摊到两轴——分摊会把逐轴上限低估 σ/√2，
把未超限的记录误判为超限。高斯噪声无界，k·σ 是置信上界而非严格上界；
k=3 时理论约 0.27%/轴。

## 独立坐标校验

坐标转换的正确性是全部验收结论的前提。若校验代码与被校验代码共用实现，
两边出现同一错误时校验会"通过"，反而掩盖问题。

`alignment/verification.py` 用**另一套数学**独立实现并校验五项：

| 校验 | 内容 | 实测 |
|---|---|---|
| `axis_orientation` | 正北 → north 为正、正东 → east 为正 | 通过 |
| `enu_roundtrip` | ENU → 经纬度 → ENU 往返残差（≤ 10 mm） | 0 |
| `ecef_consistency` | 与独立 ECEF 路径的差异落在解析预期 d²/2R 内 | 0.64 m / 预期 0.70 m |
| `tangent_plane_cross` | 与独立切平面公式逐位一致（≤ 1 µm） | 0 |
| `geodesic_consistency` | 与 Vincenty 椭球反解的距离/方位对照 | 通过 |

测试点用**独立公式**构造，不借用被校验方的 `inverse`——否则被校验方若把两轴
整体颠倒且保持自洽，往返检查会照样通过。

## 独立评测

`reference_truth.csv` 与 `target_mapping.json` **只由评测器读取**，对齐过程
不接触真值，也不使用对应关系表——否则评测结果会自我印证。

真值按**经纬度**在当前对齐原点下重算 ENU（而非直接用真值文件里的米制坐标），
因此评测对现场原点的选择免疫。

| 文件 | 内容 |
|---|---|
| `aligned_records.csv` | 逐条对齐记录：ENU、误差上界、转换状态与失败原因、修正后时间 |
| `aligned_grid_records.csv` | 插值到公共时间栅格后的记录：插值方式、间隔、前后源记录行号 |
| `alignment_quality.json` | 独立坐标校验、配置校验、空间/时间统计、缺失前提汇总 |
| `alignment_evaluation.json` | 位置误差、时间误差、覆盖率、界限比较、跨源自洽、时钟偏移检验 |
| `tracks/` | **按传感器分组的完整航迹**（见下文「航迹导出」） |

对齐结果**保留雷达编号 `00` 与 MMSI `228050803` 分开**，不提前合并成全局目标。

## 航迹导出（为航迹关联做准备）

### 为什么单独一层

对齐流水线产出的是**逐条记录**与**插值栅格**，但航迹关联的输入单位是**航迹**。
直接拿栅格做关联有三个问题：

1. 栅格是**均匀时间采样**的，各传感器采样率与起止范围不同，
   同一物理目标在不同传感器上的栅格点几乎不对齐；
2. 栅格点是**插值产物**，拿它做关联等于把插值误差引入关联门限，
   掩盖真实测量误差；
3. 栅格丢掉了**观测语义**（测量类型、误差上界、原始行号），关联后无法回溯。

因此 `alignment/track_export.py` 从**原始观测**出发，按
`(传感器类型, 航迹号)` 分组，输出每条航迹的完整点序列。

### 三种航迹

| 传感器 | 目录 | 航迹号 | 稳定性 |
|---|---|---|---|
| 雷达 | `tracks/radar/` | MMSI（AT 行）/ `900xxx`（CDT 行） | MMSI 稳定；`900xxx` **不稳定** |
| AIS | `tracks/ais/` | MMSI | 稳定 |
| 光电 | `tracks/optical/` | `<MMSI>@Inf` / `<MMSI>@TV` / `optical_det_<n>@<设备>` | 稳定 |

光电按设备分离：Inf 与 TV 是两个独立设备，同时刻各自成轨，
不产生重复时间戳。

### 雷达 900xxx 的特别处理

MTDSP 的 CDT（雷达检测）行用 `目标编号 = 900xxx` 内部号。经核验
（`standardization/radar_cutdata_adapter.py` 第 3 条），**该编号不是稳定航迹号**：
同一物理目标会在多个内部号之间切换，编号会整块跳变。

因此 `900xxx` 分组出的序列**不是航迹**，标记为
`track_stability = unstable_internal_id`，**不可直接作关联主键**。
同时单独输出 `tracks/radar_detections.csv`（逐帧检测点，保留帧号），
供下游做真正的跨帧 `(方位, 距离)` 聚类重建。

实测证据：900xxx 分组的推导速度普遍在 10–12 m/s（真实目标约 4.7 m/s），
正是编号跳变的表现。

### 运动学量的补齐：窗口最小二乘

MTDSP 雷达与光电的原始数据**只有位置，没有速度与航向**；AIS 两者都有。
为让三种航迹在关联时可比，缺项由**滑动窗口最小二乘**推导：

- 在以该点为中心、半宽 ±30 s 的窗口内，对 `(t, E)` 与 `(t, N)` 各做一次
  直线拟合，斜率即速度分量，方向即航向；
- 窗口点数 < 4 或时间跨度 < 12 s 时退回逐段差分（并剔除量化跳变段）。

**为什么必须用窗口拟合而不是逐段差分**：光电测距是**整数米档位**，
方位是 **0.1° 档位**（5.2 km 处约 9.19 m），位置因此呈**阶梯状** ——
连续若干点完全相同，然后跳 120–145 m。逐段差分被量化噪声主导，
实测瞬时速度在 0–29 m/s 之间乱跳。窗口拟合让量化噪声按 1/√N 衰减，
而真实匀速运动在窗口内保持线性，斜率不受量化影响。

推导值以 `speed_source` / `course_source` 标注为 `derived`，与实测
（`reported`）严格区分；无法可靠推导时保持缺失，不臆造。

**实测效果**（03-12 数据包，MMSI 413203610）：

| 传感器 | 逐段差分 | 窗口最小二乘 | AIS 实测 |
|---|---|---|---|
| 光电 Inf | 5.39 m/s | **4.75 m/s** | 4.72 m/s |
| 光电 TV | 5.47 m/s | **4.76 m/s** | 4.72 m/s |
| 雷达 AT | 11.02 m/s | **4.89 m/s** | 4.72 m/s |

偏差从 14% 降到 0.5%。光电 Inf 与 TV 是两个独立设备，
同刻速度差中位仅 **0.0091 m/s**，互相印证。

### 输出结构

```
tracks/
├── radar/<航迹号>.csv        每条雷达航迹的完整点序列
├── ais/<航迹号>.csv
├── optical/<航迹号>.csv
├── track_index.csv           航迹清单（关联的候选集）
├── track_summary.json        汇总统计
└── radar_detections.csv      雷达逐帧检测点（900xxx 未聚类）
```

单条航迹 CSV 的列：`track_id` / `sensor_type` / `source_id` / `point_index` /
`time_utc` / `time_epoch_s` / `latitude_deg` / `longitude_deg` /
`east_m` / `north_m` / `up_m` / `speed_mps` / `speed_source` /
`course_deg` / `course_source` / `range_m` / `bearing_deg` / `position_type` /
`alignment_status` / `error_bound_planar_m` / `radar_measurement_kind` /
`radar_target_type` / `radar_frame_index` / `optical_device` /
`optical_record_kind` / `optical_detection_index` / `platform_heading_deg` /
`source_file` / `source_row` / `record_id`。

`track_index.csv` 额外给出关联需要的航迹级元数据：点数、起止时间、时长、
中位采样间隔、平均/最大速度、平均航向、质心、航程、直线距离、
量化跳变率、实测/推导标记。

### 运行

```bash
# 对齐时自动产出
python3 run_alignment.py --radar-records <radar.jsonl> \
    --ais-records <ais.jsonl> --extra-records <optical.jsonl> \
    --config <config.json> --output-dir output/alignment/<场景>

# 从已有对齐输出单独导出（不重跑对齐）
python3 run_tracks.py --alignment-dir output/alignment/mtdsp2_yantai
```

### 现状

四个 MTDSP 场景已全部产出：

| 场景 | 航迹总数 | 雷达 | AIS | 光电 | 雷达检测点 |
|---|---|---|---|---|---|
| `mtdsp_yantai` | 16 | 10（稳定 6） | 6 | — | 522 |
| `mtdsp_yantai_optical` | 21 | 10（稳定 6） | 6 | 5 | 522 |
| `mtdsp2_yantai` | 50 | 35（稳定 5） | 5 | 10 | 1620 |
| `mtdsp2_yantai_optical` | 50 | 35（稳定 5） | 5 | 10 | 1620 |

光电点数 141（03-05 包）vs 201（03-12 包）的差异来自两包记录数不同
（279 vs 317）与误检比例不同，非缺陷。

### 航迹关联尚需的前置工作

1. **雷达 CDT 的跨帧聚类重建** —— 用 `radar_detections.csv` 按
   `(方位, 距离)` + 时间做聚类，得到真正稳定的雷达航迹；
2. **统一的时间基准** —— 当前 `aligned_time_utc` 是**本地无时区**时间，
   源间相对时差确定但绝对 UTC 不可知，关联时按相对时差配对即可；
3. **跨源坐标系统差** —— 雷达 CDT 极坐标参考点比 AIS 极坐标参考点
   偏东约 159 m，关联门限需扣除该系统差（详见
   `radar_cutdata_adapter.py` 第 8 条），否则门限会整体失效。

## 验收结果

`python3 run_acceptance.py` → `output/alignment/acceptance_report.{json,txt}`。
A/B/C 三场景独立坐标校验全部通过，位置误差全部落在上界内。

### A：几何基线（坐标、方向、单位与协议量化误差）

| 指标 | 雷达 | AIS |
|---|---|---|
| 转换成功 | 1086 / 1086 | 1086 / 1086 |
| 位置误差均值 | 0.625 m | 0.066 m |
| 位置误差最大 | 1.363 m | 0.149 m |
| 上界内 | 1086 / 1086 | 1086 / 1086 |
| 平面 / 径向 / 切向超限 | 0 / 0 / 0 | 0 / 0 / 0 |

雷达最大误差 1.363 m 的来源：距离量化 ±0.926 m（径向）+ 方位量化 ±1.079 m
（切向，在 12.3 km 处）→ 合成上界 1.42 m。误差落在界内，径向/切向分解与量化
分量逐项对应。

### B：异步观测（已知偏移修正、不同采样率匹配）

| 项 | 雷达 | AIS |
|---|---|---|
| 采样周期 | 2 s（543 点） | 5 s（218 点） |
| 修正前时间误差 | +0.250 s | −1.000 s |
| 修正后时间误差 | **0.000 s** | **0.000 s** |

AIS 读数偏差 −1.0 s = 时钟偏移 −0.5 s + **时间字段整秒截断** −0.5 s。
`!AIVDM` 的 `second` 字段只有整秒分辨率，仿真时刻 15:00:00.5 只能编码成
15:00:00。被截断的那半秒**无法从报文反推**（报文只保留了所在的整秒），因此
只能作为已知先验**显式配置**（`known_quantum_bias_s`），而不是自动推断。

公共栅格 1737 点，其中 1412 点由插值补充（雷达 543、AIS 869），1 点因超出航迹
范围不可用。

### C：观测扰动（噪声、漏报、长间隔）

| 问题 | 回答 |
|---|---|
| 实际收到多少观测？ | 雷达 1074/1086（漏报 12），AIS 540/543（漏报 3）；未解释缺失 **0** |
| 插值补了多少点？ | 雷达 13 点、AIS 539 点 |
| 多少点因间隔过长不可用？ | AIS 7 点（`gap_exceeds_limit`）+ 1 点（`outside_track_range`） |
| 误差统计用了哪些有效点？ | 1614 条（记录级）/ 2164 条（栅格级）；无状态时间 0、真值配对失败 0、未转换 0 |

位置误差：雷达 mean 15.3 m / max 50.8 m，AIS mean 32.5 m / max 106.0 m，与配置的
噪声量级（12 m / 25 m）一致。平面超限 0 条；逐轴超限 5 条（0.233%/轴），来自
高斯噪声尾部，与 3σ 理论 0.27% 一致。

## 缺失前提的处理

缺少站址、外参或可靠时间时输出明确的不可用原因，不静默丢弃：

| 情况 | `alignment_status` | `alignment_failure_reason` |
|---|---|---|
| 缺现场原点 | `failed` | `missing_site_frame_origin` |
| 缺雷达站位置 | `failed` | `missing_radar_station_position` |
| M 参考但无磁差 | `failed` | `bearing_reference_m_not_convertible` |
| R 参考但无本船艏向 | `failed` | `bearing_reference_r_not_convertible` |
| 无任何时间基准 | 不进入插值序列 | `no_reliable_time_base` |
| 源类型不支持 | `failed` | `unsupported_source_type:<type>` |

汇总在 `alignment_quality.json` 的 `missing_requirements` 段。

## 本轮补验中发现并修复的缺陷

1. **水平位置经 ECEF 三维映射定义** → 引入随距离增长的系统偏差
   （12.3 km 处 0.023 m），使径向误差略微超出报文量化界限。改为局部切平面
   投影，往返精确到纳米级。
2. **`bearing_reference` 读取位置错误** → 字段在 `position` 内而代码读顶层，
   导致 M/R 参考的处理在真实数据上**从未生效**。
3. **误差上界的噪声项按 √2 分摊到两轴** → 逐轴上限被低估 σ/√2，把未超限的
   记录误判为超限（C 场景误报 16 条）。
4. **校验器自身的 `mean_radius` 公式写反**（返回曲率而非半径）→ 所有解析预期
   值失真，校验形同虚设。
5. **校验器的测试点借用被校验方的 `inverse`** → 被校验方若整体颠倒两轴且保持
   自洽，往返检查照样通过，轴向错误抓不出来。改用独立公式构造测试点。
6. **`to_latlng` 丢弃高程** → 反算时按 h=0 处理，等价于把目标压回海平面。
7. **评测直接用真值文件的米制坐标** → 更换现场原点后凭空多出 1 km 级假误差。
   改为按经纬度在当前原点下重算。
8. **AIS 整秒截断残差被当作"格式固有限制"搁置** → 改为显式建模
   （`time_quantum_s` + `known_quantum_bias_s`），修正后残差归零。
9. **界限比较未留数值容差** → 量化误差恰好等于 ±LSB/2 时，浮点表示会超出
   上界约 0.05 mm，被误判为超限。

## 测试

`python3 -m unittest discover -s tests`：**265 项全部通过**
（此处为 v1.0 冻结时的 127 项，当前总数见文末「本轮测试」）。

覆盖：ECEF 往返精度、雷达/经纬双路径一致性、平面近似偏差量化、
T/M/R 三种方位参考语义、位置参考点偏移、时钟偏移与量化偏差修正、
按航迹分组、两种插值方式（匀速直线/转弯/方位跨零/长间隔/不外推/源失败）、
误差上界模型（噪声逐轴）、独立校验器自检（能抓出人为制造的轴向错误）、
评测对原点的免疫性，以及 A/B/C 三场景端到端位置误差、时间误差、覆盖率、
界限比较与跨源自洽性。

## 尚未完成 / 后续阶段

- 对齐参数目前全部为**已知值**；参数自动估计（站址标定、时钟偏移估计）
  尚未实现。
- 真实数据侧缺雷达本船位置与外参，暂以配置给定。
- 栅格评测按就近时间配对，尚未实现严格的时空关联（同一目标判定）。
- 尚未建立跨源目标关联（配对）算法。

# 第四部分：2026-11-08 补充（Inf/TV 航迹分离 + IF 平台状态接入）

## 一、光电 Inf / TV 分离为独立航迹

### 问题

`Inf`（红外）与 `TV`（可见光）是**两个独立设备**，但原先共用同一个
`local_track_id`（MMSI），后果是：

1. 光电航迹点序列出现**重复时间戳**，且同一时刻存在两个位置；
2. 对齐模块的二分 bracket 只能隐式收敛到其中一条（依赖记录顺序），
   另一条被静默吞掉；
3. 下游跨源关联看到"同一目标同一时刻两个位置"，无法判定取舍。

### 实测：两设备差异有多大

| 数据集 | 同时刻 Inf/TV 主目标 | 距离方位相同 | 位置不同 | 最大距离差 |
| ------ | ---------------------- | ------------ | -------- | ---------- |
| 03-05  | 46 组                 | 46           | 0        | 0.00 m    |
| 03-12  | 60 组                 | 58           | 2        | 121.72 m  |

**画面内检测**（`_DLDete.txt`）差异极大：03-05 包 92 组同时刻检测的距离差
最大 320 km，03-12 包 65 组最大 1.8 Mm —— 这是两个设备各自的 AI 检测
结果，互不对应。

### 改动

`standardization/optical_photo_adapter.py` 新增 `device_scoped_track_id()`，
`local_track_id` 在 MMSI 之后追加设备标识：

- 主目标：`413203610@Inf` / `413203610@TV`
- 检测目标：`optical_det_1@Inf` / `optical_det_1@TV`

原始 MMSI 仍保留在 `optical_primary_mmsi` 字段，语义不变。
去重键本已包含 `optical_device`，无需改动。

### 效果

| 场景 | 改动前栅格 | 改动后栅格 | 同时刻多点组 |
| ---- | ---------- | ---------- | ------------ |
| `mtdsp_yantai_optical` | 12705 行 | 12796 行 | 0 |
| `mtdsp2_yantai_optical` | **0 行（配置错配）** | 31203 行 | 0 |

顺带修正：`mtdsp2_yantai_optical` 原先误用 03-05 的配置
（`alignment_site_yantai_optical.json`），导致全部记录 `no_reliable_time_base`
而栅格为空；改用 `alignment_site_yantai_mtdsp2.json` 后正常。

## 二、IF 帧头平台自身航向角接入对齐模块

### 接入的是什么

`IF_*.csv` 帧头的 `航向角(°)` 与 `速度(nm/h)` 描述**雷达平台自身**
（固定岸基站的 GNSS 状态），不是被观测目标。解码方法见
`standardization/radar_if_header`（float32 次正规数可逆编码）。

### 关键结论：方位基准**不随**平台艏向变化

#### 决定性判据：用**权威 AIS** 反算真方位

对每条有 AIS 对应目标的雷达记录，取同一时刻 AIS 的经纬度，用 IF 帧头
给出的**雷达自记站址** (37.53297, 121.42020) 反算该目标的真方位
`implied`，与雷达上报的 `目标方位(°)` 比较，残差 Δ = implied − tb。
再对各种候选修正量比较叠加前后的 |Δ|：

| 候选修正量 | 03-05 \|Δ\|中位数 | 03-05 RMS | 03-12 \|Δ\|中位数 | 03-12 RMS |
| ---------- | ----------------- | --------- | ----------------- | --------- |
| **不修正 (0°)** | **0.446°** | 1.276° | **0.530°** | 1.335° |
| 平台航向 (≈353°) | 6.597° | 7.527° | 6.427° | 6.814° |
| 角度修正 (344° / 345°) | 15.554° | 16.431° | 15.869° | 16.180° |
| 平台航向 − 角度修正 | 9.403° | 8.658° | 9.573° | 9.411° |
| 平台航向 + 180° | 173.403° | 172.577° | 173.573° | 173.322° |

⇒ **`目标方位(°)` 已经是真方位**，任何叠加都会显著恶化残差。
该判据**完全独立于 IF 的方位码**，只依赖 AIS 与权威站址，结论稳固。

残余的 0.45°/0.53° 偏差来自目标运动与 AIS 上报时延 ——
按距离分层可见残差随距离增大而放大（03-12：3.5–4.0 km 中位 −0.076°，
4.5–5.0 km 中位 −0.261°，5.0–5.5 km 中位 −0.358°），
不是坐标基准错误。

这与 `角度修正(°)=344` 不参与换算的既有结论一致（叠加后站址反算残差
从 14 m 恶化到 400 m）。

#### 辅助判据：IF 方位码一致性

用 IF 帧的方位码做全角一致性统计（预测码须落在该帧实际覆盖的约 40°
区间内）：最优偏移 03-05 为 345.1°、03-12 为 3.5°，一致率 98.1% / 97.7%
—— 说明方位码与 `目标方位(°)` 之间**不存在一个全角上唯一且两数据集
一致的常量偏移**（帧覆盖仅 40°，且两包的帧序/时间对应关系不同）。
因此该路径只作辅助，结论以 AIS 判据为准。

**因此平台航向不参与方位→ENU 换算**，只作附加记录。

### 改动

- 新增 `alignment/if_platform_state.py`：建立 IF 帧索引，提供
  「帧号优先、时间兜底」的双通道关联；
- `alignment/config.py` 新增 `platform_state` 配置段（未配置时行为完全不变）；
- `alignment/spatial.py` 在雷达路径上附加
  `platform_heading_deg` / `platform_speed_mps` 等字段；
- `alignment/pipeline.py` 把平台状态纳入 `ALIGNED_FIELDS` 与
  `alignment_quality.json` 的 `platform_state` 段；
- `run_acceptance.py` 在验收报告中呈现平台状态。

### `.dat` 补齐缺失的航向角

CSV 的 `航向角(°)` **只在部分行**填写：

| 数据集 | 帧数 | CSV 含航向角的帧 |
| ------ | ---- | ---------------- |
| 03-05  | 127  | 126              |
| 03-12  | 152  | **6**            |

但 `.dat` 的**每条**脉冲记录都带完整帧头，且：

- CSV 行序与 `.dat` 记录序**逐行一一对应**（03-05 68327 行、03-12
  69462 行，行数相同，且 CSV 有值的行与 `.dat` 逐位相等）；
- 帧内所有脉冲的航向/速度**完全相同**（0/127 与 0/152 帧出现多值）。

因此可用 `.dat` 补齐每帧缺失的航向角 —— 03-12 包由 6/152 帧
提升到 **152/152 帧全覆盖**。

### 实测结果

| 场景 | IF 帧 | 覆盖记录 | 匹配方式 | 平台航向 | 平台速度 |
| ---- | ----- | -------- | -------- | -------- | -------- |
| `mtdsp_yantai`        | 127 | 663   | frame_index 522 / time 141 | 352.0..353.9° | 1..25 mm/s |
| `mtdsp2_yantai`       | 152 | 1656  | frame_index 1620 / time 36 | 353.0..353.9° | 1..26 mm/s |

平台速度 0~26 mm/s 是固定岸基站的 **GNSS 定位抖动**，不是真实位移
（与路径积分反推的站址抖动量 3.47 m 一致）。

## 本轮测试

`python3 -m unittest discover -s tests`：**265 项全部通过**

| 测试文件 | 项数 | 覆盖内容 |
| -------- | ---- | -------- |
| `tests/test_track_export.py` | 17 | 三种传感器分组、光电 Inf/TV 分离、900xxx 不稳定标记、量化跳变识别、**窗口最小二乘在位置量化下恢复真实速度**、实测速度不被覆盖、落盘结构 |
| `tests/test_if_platform_state.py` | 14 | IF 帧分组读取、`.dat` 补齐缺失航向角、帧号/时间双通道关联、平台状态不改 ENU |
| `tests/test_bearing_reference_truth.py` | 4 | **方位基准是真北的回归保护**：用权威 AIS 反算真方位，断言「不修正」优于任何叠加；安装方位与本船艏向不得被误用 |
| `tests/test_optical_photo_adapter.py` | +3 | Inf/TV 分离为独立航迹、同时刻不产生重复时间戳 |

其中 `test_track_export.py` 固化了两条关键结论：

- **位置量化下窗口最小二乘才能恢复真实速度** —— 逐段差分会把光电速度
  虚增到 5.4 m/s（真值 4.72），窗口拟合才能得到 4.75 m/s；
- **雷达 900xxx 不得被当作可用关联主键** —— 若有人改动使其变成
  `stable`，测试立即失败。

`test_bearing_reference_truth.py` 把「平台航向不参与方位→ENU 换算」
这一结论**固化为可执行断言**：若后人误把平台航向或 `角度修正` 当成方位
修正项，测试会立即失败。

---
# 第五部分：航迹关联 v1.0（2026-11-09）

## 输入与规则

- 候选来自 `output/alignment/<场景>/tracks/`：AIS 稳定轨、光电 Inf/TV
  分离轨、雷达纯测量点（RT 整条；MMSI 混合轨只取
  `radar_measurement_kind != ais_associated` 的点；`900xxx` 不用，
  由 `radar_reconstructed/R<n>` 代替）。
- AT 快照拷贝不参与评分，避免 AIS 自己跟自己比。
- 时间可比性断言复用 `association/timebase.check_timebase`
 （相对本地基准即可；无光电场景只检查实际存在的传感器）。
- 重建统一只用纯 CDT（`load_detections` 默认过滤 AT/RT），
  否则重建轨会混入 AIS 拷贝、虚增 7~61 条假轨。

## 方法

1. 重叠段残差：点数少的一方为基准，向另一方线性插值
   （`MAX_INTERP_DT_S=15 s`，`MAX_INTERP_SPAN_S=300 s` 容忍 AIS 稀疏），
   得平面距离序列 → 中位/均值/最大；航迹级航向差（绕回）与速度差辅助。
2. 评分前显式系统差修正（ENU 绕站心旋转，不改原轨）：
   光电默认 `+5.5°`（03-05 为 +5.68°、03-12 为 +5.44°，极差仅 0.24°，
   设备级常量），雷达 CDT 默认 `−2.4°`（03-05 稳定 −2.404°±0.200°）。
   修正后 03-05 同目标 radar-ais 153→27 m、optical-ais 358→15 m、
   radar-optical 522→23 m；`--no-bias-correction` 可关闭做消融对比。
3. 门限粗筛 + 指派：radar-ais 200 m、optical-ais 300 m、
   radar-optical 600 m（含低置信标记 >300 m）；锚泊（任一方 <1.0 m/s）
   跳过航向门限；`optical_ais` 与 `radar_optical` 及碎片场景用多对一
   （B 端可重复命中，Inf/TV 双设备与重建碎片需要），其余 Hungarian 一对一；
   不可行矩阵退回贪心。

## 运行

```bash
python3 run_associate.py --alignment-dir output/alignment/mtdsp_yantai_optical
python3 run_associate.py --alignment-dir output/alignment/mtdsp2_yantai_optical
```

产物 `output/association/<场景>/`：`radar_ais/optical_ais/radar_optical`
的 `accepted/rejected` CSV + `association_summary.json`
（含候选、门限、MMSI 评估与落选原因）。

## 四场景结果（修正后）

| 场景 | radar-ais | optical-ais | MMSI 误配 |
|---|---|---|---|
| `mtdsp_yantai` | 413203610↔413203610 27 m；重建 R21↔413203610 36 m | — | 0 |
| `mtdsp_yantai_optical` | 同上 | Inf/TV 主轨↔413203610 均为 15 m；det_1@TV↔413203610 16 m | 0 |
| `mtdsp2_yantai(_optical)` | R154↔412332820 32 m；混合 413↔AIS413 61 m；R63/R120↔AIS413 80/97 m | 主轨 Inf 121 / TV 107 m；det_1/2/3@TV 66/75/67 m | 0 |

- RT-1 距所有 AIS ≥612 m（修正后 645 m），正常落选——它是另一目标。
- 最近异船修正后 radar-ais ≥295 m、optical-ais ≥1014 m，门限可分。
- radar-optical 主对 03-05 为 22 m（high）、03-12 为 104 m（high），
  碎片杂波 289~593 m 标 low。重建碎片多段命中同一 AIS 属预期
  （CDT 碎裂未合并），不判错。

## 测试

`python3 -m pytest tests/`：**290 项全部通过**（含 8 项新增
`tests/test_association.py`：旋转恒等/往返、零距、重叠不足、
修正前后、锚泊跳过航向、运动航向否决、多对一、快照过滤）。


---

# 第六部分：光电观测导出到前端场景数据集（2026-11-26）

## 问题

前端 `apps/es-big-screen/public/water-security/data/` 里
`devices.json` 声明了光电设备 E01，但 `tracks.json` 里**没有任何 E01 的源航迹**。
于是「数据标准化」模块里光电永远显示 **0 条**，
而「目标识别」模块与场景画面却把光电画成在正常工作的样子
（视场锥在转、云台在跟），造成**演示层与数据层口径不一致**。

根因不在前端渲染，而在**导出环节漏了光电这一路**：
后端三源流水线（雷达 / AIS / 光电）本就���光电算得出来，
前端数据集是当初只导雷达 + AIS（+ 声纳）时留下的子集。

## 后端本来就有的光电数据

| 环节 | 位置 | 规模 |
|---|---|---|
| 原始截图 | `data/mtdsp/Photo_ScreenShot_Inf_*.txt` | 276 个 |
| AI 检测结果 | `data/mtdsp/*_DLDete.txt` | 92 个 |
| 标准化 | `standardization/optical_photo_adapter.py` | 279 条记录 |
| 空间可用 | `valid_for_spatial_alignment` | 141 条 |
| 关联结论 | `output/.../optical_ais_accepted.csv` | Inf/TV 主轨 + det_1@TV 均 high |

## 三项关键处理

### 1. 方位系统差修正（+5.28°）

光电设备有**恒定的方位零点偏差**：03-05 包 +5.681°、03-12 包 +5.439°，
跨数据集极差仅 0.24° ⇒ 设备级常量。

适配器**刻意不隐式补偿**（`optical_azimuth_bias_applied=false`），
把这件事留给下游显式处理 —— 不修正的话，光电点迹会稳定偏在真值东侧约 350 m，
页面上的光电航迹会与雷达/AIS 明显分叉，看起来就像「光电坏了」。

`verify_optical_bias.py` 用**权威 AIS** 作真值参照实测该偏差：

```
实测方位偏差    : 中位 +5.292°  最小 +5.292°  最大 +5.292°
平均残差：未修正 357.6 m → 修正后 5.1 m （改善 352.5 m）
```

### 2. Inf / TV 双设备并列观测

光电转塔有 **Inf（红外）/ TV（可见光）两台相机**，共享同一个 `local_track_id`
与同一时间戳，但**位置不同**（03-05 实测 138 组同时刻记录中 48 组位置不同，
最大相距 727 m）。这是两次独立测量，不是重复记录。

导出时按 `local_track_id` 分组，同一时刻取**离站心更近**的一条
（距离量测相对误差更小），并按目标合并成一条 E01 源航迹
（前端 `source_tracks` 的语义是「一个感知源在一个目标下的观测」，
同一 `source_id` 只能有一条）。

### 3. 超量程误检的剔除

DLDete 的 AI 检测结果里有大量误检：距离散布到 28/45/234–350 km，
置信度高达 0.81–0.94 **无法用置信度区分**。
后端已按 `max_range_m = 20000 m` 标记 `optical_detection_out_of_range`，
导出时只取 `alignment_status == "ok"` 的记录，误检在这里被自然过滤。

## 运行

```bash
# 1) 跑三源流水线（标准化 → 对齐 → 航迹 → 重建 → 关联 → 主目标）
python3 multisensor_system.py --scenario mtdsp_0305 --output-root output/system

# 2) 实测方位偏差（可选，导出脚本的修正参数依据）
python3 verify_optical_bias.py

# 3) 导出到前端数据集
python3 export_optical_to_frontend.py --dry-run   # 先看统计
python3 export_optical_to_frontend.py             # 实际写入
```

脚本**幂等**：重复执行不会产生重复条目。

## 效果

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 数据标准化 · 光电 | 0 条 | **46 条**（有效 46 / 异常 0） |
| 时空对齐 · 光电 E01 | 无 | **1 段**，与雷达/AIS 同轨 |
| 航迹关联 | 无光电列 | **光电航迹 E001**，R01-E01 距离 **1 m**（confirmed） |
| 系统管理 · 当前关联目标 | 无 | **T001**，凝视链路 73% |
| 场景画面 | 视场锥在转但无数据 | **EO 跟踪 T001** + 锁定框 + 视线 |

光电与雷达在同一时刻的 ENU 坐标差 **0.8 m**（3.9 km 处），
说明方位偏差修正到位。

## 附带修复：设备类型命名不一致

`devices.json` 里光电的类型写的是 `camera`，
而前端 `pipeline.ts` / `chart-utils.ts` / `ModuleStd.vue` 用的是 `eo`
（`KIND_LABEL` / `KIND_COLOR` 都没有 `camera` 这一项）。

导出脚本统一改为 `eo`；前端三处也改为**同时接受** `eo` 与 `camera`，
避免旧数据或第三方数据因命名不一致而丢失显示。

## 附带确认：`t₀` 统一时刻仍单调

新增光电源航迹后重新验证：303 个采样点上 `t₀` **回退 0 次**，
保持单调不减 —— 之前修复的「按 (targetId, sourceId) 分组计数」依然有效。
