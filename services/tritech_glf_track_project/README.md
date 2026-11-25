# Gemini GLF 声呐数据处理与航迹工程

本工程把当前已经跑通的链路整合成一个可重复运行的项目：

`Gemini GLF -> TritechFiles解析 -> 原始回波矩阵 -> 背景抑制 -> 运动目标检测 -> Kalman跟踪 -> SONAR_FAN标准航迹 -> 可视化 -> 第二声呐模拟观测 -> 可选航迹关联验证`

## 1. 工程结构

```text
Tritech_GLF_Track_Project/
├─ run_all.py                       # 一键入口
├─ requirements.txt
├─ config/
│  ├─ default_config.json           # 检测/跟踪/模拟/关联参数
│  ├─ tanana_203913_profile.json    # 当前Tanana记录的实验事件标签配置
│  └─ sensor_extrinsics_partial.json
├─ libs/
│  └─ TritechFiles-local.jar        # 由上传的TritechFiles源码编译
├─ java/
│  └─ ExportGLF.java                # 用TritechFiles读取全部GLF帧
├─ src/
│  ├─ extract_glf.py                # 编译/调用Java导出器
│  ├─ frame_store.py                # Python随机读取导出的帧
│  ├─ detect_track.py               # 背景抑制+检测+Kalman+帧间关联
│  ├─ standardize.py                # SONAR_FAN标准化/置信度/可选实验标签
│  ├─ visualize.py                  # 航迹PNG/GIF
│  ├─ simulate_second_sonar.py      # 从真实航迹构造第二台声呐
│  ├─ associate_tracks.py           # 可选：A/B关联验证
│  └─ coordinate_transform.py       # 真实外参具备后的坐标变换模板
├─ scripts/
│  ├─ run_example_linux.sh
│  └─ run_example_windows.bat
├─ data/
│  └─ README.md                     # GLF放置说明
└─ vendor/TritechFiles/
   ├─ LICENSE                       # LGPL 2.1
   └─ src/                          # 上游源码
```

## 2. 环境

### Python

建议 Python 3.10+：

```bash
pip install -r requirements.txt
```

依赖：NumPy、Pandas、SciPy、OpenCV、Matplotlib、Pillow。

### Java

需要 JDK（不仅是 JRE），确保：

```bash
java -version
javac -version
```

都能执行。

## 3. 一键运行

以当前 `log_2025-06-19-203913.glf` 为例：

```bash
python run_all.py \
  --glf /path/to/log_2025-06-19-203913.glf \
  --profile config/tanana_203913_profile.json \
  --with-association
```

Windows：

```bat
python run_all.py --glf "D:\data\log_2025-06-19-203913.glf" --profile config\tanana_203913_profile.json --with-association
```

如果只是处理新的GLF，不希望使用Tanana实验日志标签：

```bash
python run_all.py --glf /path/to/new.glf
```

这时 `target_label=UNKNOWN`，但检测、跟踪、航迹输出仍然正常运行。

## 4. 第一步：TritechFiles读取GLF

`java/ExportGLF.java` 直接调用：

```java
GeminiFileCatalog<?> cat = GeminiFileCatalog.getFileCatalog(path, true);
GLFImageRecord r = (GLFImageRecord) cat.getFullRecord(i);
byte[] image = r.getImageData();
double[] bearings = r.getBearingTable();
```

每帧导出：

- `time_millis`
- `n_range`
- `n_beam`
- `max_range_m`
- `sound_speed_mps`
- `gain_percent`
- 原始 `uint8` 回波矩阵
- 每帧 bearing table

为了避免生成数百个小文件，工程把所有帧顺序写入：

```text
01_extracted/frames_u8.bin
```

把每帧波束角表写入：

```text
01_extracted/bearings_f64le.bin
```

再由 `frame_metadata.csv` 中的字节偏移进行随机访问。

Python 中：

```python
store = FrameStore("outputs/01_extracted")
frame = store.frame(100)          # [n_range, n_beam], uint8
angles = store.bearings_deg(100)  # 波束角
```

因此后续算法不再直接解析GLF二进制格式；GLF解析由TritechFiles负责。

## 5. 第二步：背景抑制与目标检测

`src/detect_track.py` 对每帧执行：

1. 指数滑动背景：

   `B_k = (1-alpha) B_(k-1) + alpha I_k`

2. 差分：

   `D_k = I_k - B_(k-1)`

3. 3x3高斯平滑。
4. 固定阈值与MAD自适应阈值取较大值。
5. 形态学开运算去除孤立噪声。
6. 连通域检测。
7. 面积、距离边界、波束角范围过滤。
8. 相邻候选目标合并。

每个检测输出：

```text
frame_id
timestamp
range_m
bearing_deg
fan_u_m
fan_v_m
score
area_px
```

其中：

```text
u = -r sin(bearing)
v =  r cos(bearing)
```

这里只能称为 `SONAR_FAN` 扇面坐标，不能直接解释为水平XY或ENU。

## 6. 第三步：Kalman单声呐跟踪

状态：

```text
x = [u, v, du/dt, dv/dt]^T
```

采用恒速度状态模型和白加速度过程噪声。

帧间匹配使用：

- Mahalanobis门控
- 欧氏距离门控
- Hungarian (`scipy.optimize.linear_sum_assignment`)

航迹管理包括：

- 新航迹创建
- 命中计数
- 漏检计数
- `TENTATIVE -> CONFIRMED`
- 连续漏检后删除
- 漏检期间仍保留Kalman预测状态

输出 `02_tracking/tracks_raw.csv` 中包括：

```text
track_id
measurement_available
fan_u_filt_m
fan_v_filt_m
du_mps
dv_mps
Puu/Puv/Pvv
hits/misses/age
track_quality_score
```

协方差字段是后续多声呐Mahalanobis航迹关联需要的基础。

## 7. 第四步：标准化

`src/standardize.py` 增加统一字段：

```text
sensor_id
track_id
coordinate_frame=SONAR_FAN
range_m
bearing_deg
fan_u / fan_v
速度
状态协方差
track_confidence
track_status
```

并明确：

```text
horizontal_xy_valid=false
enu_valid=false
```

因为当前公开实验资料缺少完整绝对航向和平移外参。

如果传入 `tanana_203913_profile.json`，程序会依据实验日志事件时间给候选航迹添加实验标签。此标签是**日志时间关联结果，不是GLF内置真值标签**。

## 8. 第五步：航迹可视化

输出：

```text
04_visualization/tracks_overview.png
04_visualization/tracks_animation.gif
```

声呐位于原点，显示各Track的历史轨迹与当前位置。

## 9. 第六步：模拟第二台声呐

`src/simulate_second_sonar.py` 从一条真实航迹出发，不是简单复制，而是加入：

- 人工安装平移
- 人工Yaw旋转
- 不同采样率
- 时间偏移
- 时间抖动
- 位置噪声
- 速度噪声
- 随机漏检

默认：

```text
translation = [4, 2] m
yaw = 12 deg
rate = 4 Hz
time offset = 80 ms
position sigma = 0.18 m
velocity sigma = 0.12 m/s
drop rate = 12%
```

输出：

```text
05_second_sonar_simulation/sonar_A_real_common.csv
05_second_sonar_simulation/sonar_B_simulated_observations.csv
05_second_sonar_simulation/simulation_parameters.json
05_second_sonar_simulation/sonar_A_B_simulation_overview.png
```

注意：模拟中的 `ASSOCIATION_COMMON_LOCAL` 只是算法验证公共坐标系，不是真实ENU。

## 10. 可选：双声呐关联验证

使用：

```bash
--with-association
```

会执行：

```text
时间近邻
-> 位置门控
-> 速度门控
-> Mahalanobis门控
-> 关联代价
-> Global Track ID
```

当前模拟只有A/B各一条对应航迹，因此 `associate_tracks.py` 是单对单验证器；以后接入多目标多声呐时，将同一代价计算扩展为矩阵，再用Hungarian即可。

## 11. 输出目录

一次完整运行后：

```text
outputs/
├─ 01_extracted/
│  ├─ frame_metadata.csv
│  ├─ frames_u8.bin
│  ├─ bearings_f64le.bin
│  └─ summary.txt
├─ 02_tracking/
│  ├─ detections.csv
│  ├─ tracks_raw.csv
│  └─ track_summary_all.csv
├─ 03_standardized/
│  └─ standardized_sonar_tracks.csv
├─ 04_visualization/
│  ├─ tracks_overview.png
│  └─ tracks_animation.gif
├─ 05_second_sonar_simulation/
│  ├─ sonar_A_real_common.csv
│  ├─ sonar_B_simulated_observations.csv
│  └─ ...
└─ 06_association/
   ├─ association_results.csv
   └─ association_summary.csv
```

## 12. 目前工程边界

已经完成：

1. TritechFiles真实读取GLF；
2. 原始回波矩阵导出；
3. 背景抑制；
4. 运动目标候选检测；
5. Kalman单声呐跟踪；
6. 标准化航迹；
7. 航迹动画；
8. 第二声呐模拟数据；
9. 基础关联验证。

尚未宣称完成真实工程验证的部分：

- 任意复杂场景下的误报抑制；
- 弱目标/交叉目标鲁棒跟踪；
- 真实第二台声呐；
- 真实多声呐安装外参；
- 真实ENU转换；
- 多目标Track-to-Track Hungarian完整关联。

这些是下一阶段的工程化内容。
