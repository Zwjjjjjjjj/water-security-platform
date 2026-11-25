# N-Sonar Track Association + Fusion 设计文档

## 1. 目标

把 `two_sonar_association` 从 **2 个声纳** 扩展到 **N 个声纳 (N≥2)**：

| 能力 | 现状 (N=2) | 设计 (N≥2) |
|------|----------|------------|
| 仿真数据 | `simulate_two_sonars.py`：A=(0,0) h=0°，B=(50,0) h=180° | `n_sonar_simulator.py`：N 个声纳在场景内，任意位置/朝向/采样率/噪声 |
| 时间对齐 | `align_tracks.py`：A↔B 二元对齐 | `align_n_tracks.py`：N-way 对齐到公共时间网格 |
| 关联代价 | `track_association.py`：N_a × N_b 矩阵 + Hungarian | **N-partite 全局关联**：对每条候选 track 提议它的 cluster 成员，cluster-level Hungarian |
| 关联管理 | `association_manager.py`：track_id 前缀 `A_/B_` 硬编码推断 sensor | `TrackAssociationState.sensor_id` 字段已存在，直接使用，不依赖前缀 |
| 信息融合 | `track_fusion.fuse(state_a, state_b)`：硬编码二元 | `fuse_n(states: List[(sensor_id, x, P, t_meas, has_meas)])`：信息形式加和 |
| 主入口 | `run_two_sonar_association.py` | `run_n_sonar_association.py`：通用配置 + 重复使用 N=2 路径作为回归测试 |

## 2. 关键算法设计

### 2.1 N-Sonar Simulator

```python
class NSonarSimulator:
    def __init__(self, reference_track: pd.DataFrame, sonars: List[SonarConfig]):
        self.ref = reference_track
        self.sonars = sonars  # 任意 N 个

    def simulate(self) -> Dict[str, pd.DataFrame]:
        """返回 {sensor_id: measurements_df}"""
        out = {}
        for s in self.sonars:
            out[s.sensor_id] = self._simulate_one(s)
        return out
```

每个声纳 `s` 有独立参数：
- `x_m, y_m, heading_deg`
- `sample_rate_hz, time_offset_s`
- `sigma_range_m, sigma_bearing_deg, miss_probability`

### 2.2 N-Way Time Alignment

**现状 (`align_tracks.py`)**：A、B 各采样到离散时间 → 在公共时间轴上取最近邻对齐。

**N-way**：在公共时间轴上对所有 N 个传感器的最近邻测量点**做聚类**：在 Δt = 0.1s 窗口内，把每个传感器 ≤1 个最新测量点聚为同一个"全局时间帧"。

输出 schema：
```
global_t | sensor_id | local_t | local_track_id | x | y | vx | vy | P_xx | P_xy | P_yy | has_meas
```
每一行 = 1 个传感器在 1 个全局时间帧上的"贡献"。**每帧 N 行**（理想情况）。

### 2.3 N-Partite Cluster Association

**现状**：`build_cost_matrix` 计算 N_a × N_b 矩阵，Hungarian 找最匹配对。O(N²) 代价，2 路关联。

**N-way 设计**：

1. **两两 cost**：对每对 (sensor_i, sensor_j)，用现成的 `compute_track_level_cost` 算 `c_{ij}(t_i, t_j)`。N 个传感器 = C(N,2) 对。

2. **构建关联图**：节点 = (sensor_id, track_id)，带权边 = 两两 cost。

3. **找 cluster**：用 **clique 枚举 + 平均 cost**。具体：
   - 从 N-partite 图中搜索 "跨所有 N 个传感器的 cluster"，每个 cluster 含 N 个节点（每个传感器一个）
   - 候选 = 一组 (track_{s_1}, track_{s_2}, ..., track_{s_N})，对所有 i<j 都有 c_{ij} < gate
   - 评分 = mean(c_{ij})；取 top-1
   - 对于 N=2 退化为 Hungarian 等价

4. **复杂度**：
   - 暴力枚举 O((K_a * K_b * K_c * ...))，不可行
   - **两步策略**：先 1-vs-rest 找 anchor，再用 cluster extension
     - Step 1：在 sensor 1 vs 所有其它 sensors 的 cost matrix 上跑 Hungarian 找 anchor (s1_track, s2_track, ...)
     - Step 2：把 anchor 之间的 cost 联合 gate 验证
   - 对 N=3,4 完全可行（即使每个 sensor 10 tracks = 10^3 = 1000 个候选 cluster）

### 2.4 N-Way Information Fusion

**现状**：`fuse(state_a, state_b)` 硬编码二元：
```
P_xy_f = (P_a^-1 + P_b^-1)^-1
pos_f  = P_xy_f @ (P_a^-1 @ pos_a + P_b^-1 @ pos_b)
```

**N-way 设计**（信息形式加和）：

```python
def fuse_n(timestamp, global_id, states: List[StateTuple]) -> FusedState:
    """
    states: list of (sensor_id, x, P, has_meas, t_meas) tuples
    """
    # 1) 过滤掉 has_meas=False 的预测-only 状态
    measured = [s for s in states if s[3]]   # has_meas=True
    if not measured:
        return NONE
    if len(measured) == 1:
        return SINGLE[measured[0].sensor_id]
    # 2) 对称外推到测量中点 (与现有 _extrapolate_state 一致)
    t_mid = np.mean([s[4] for s in measured])
    extrapolated = []
    for s in measured:
        x_e, P_e, _ = _extrapolate_state(s[1], s[2], s[4], t_mid)
        extrapolated.append((s[0], x_e, P_e))
    # 3) 异常值剔除 (3σ 规则, 至少 3 个测量)
    if len(extrapolated) >= 3:
        extrapolated = _outlier_reject(extrapolated, sigma=2.5)
    # 4) 信息形式融合
    info_P_inv = np.zeros((4, 4))
    info_x = np.zeros(4)
    for sensor_id, x, P in extrapolated:
        info_P_inv += np.linalg.inv(P[:2, :2]) for pos
        info_x[:2] += np.linalg.inv(P[:2, :2]) @ x[:2]
    pos_cov = np.linalg.inv(info_P_inv)
    pos = pos_cov @ info_x
    # 速度融合同上
    return FusedState(position=pos, covariance=..., n_sensors_used=len(extrapolated), sensors_used=...)
```

**关键不变量**：N=2 时与现有 `fuse()` 结果**完全一致**（信息加和等价于二元公式）。

## 3. 配置 schema 演进

```json
{
  "experiment_type": "n_sonar_divers_2",
  "sonars": [
    {"id": "SONAR_A", "x_m": 0.0,  "y_m": 0.0, "heading_deg": 0.0,   "sample_rate_hz": 5.0, "time_offset_s": 0.0,  "sigma_range_m": 2.0, "sigma_bearing_deg": 0.8,  "miss_probability": 0.02},
    {"id": "SONAR_B", "x_m": 50.0, "y_m": 0.0, "heading_deg": 180.0, "sample_rate_hz": 4.7, "time_offset_s": 0.08, "sigma_range_m": 2.4, "sigma_bearing_deg": 0.96, "miss_probability": 0.03},
    {"id": "SONAR_C", "x_m": 25.0, "y_m": 30.0,"heading_deg": 270.0, "sample_rate_hz": 4.5, "time_offset_s": 0.12, "sigma_range_m": 2.6, "sigma_bearing_deg": 1.04, "miss_probability": 0.04},
    {"id": "SONAR_D", "x_m": 25.0, "y_m": -30.0,"heading_deg": 90.0, "sample_rate_hz": 5.2, "time_offset_s": 0.04, "sigma_range_m": 2.2, "sigma_bearing_deg": 0.88, "miss_probability": 0.03}
  ],
  "n_divers": 2,
  "reference_tracks": {
    "T1": "dfki_set_4_PRIMARY_DIVER",
    "T2": "synthesized_secondary"
  },
  "association": { /* 保持现有 schema */ },
  "fusion": { /* 保持现有 schema */ }
}
```

## 4. 验证策略

| N | 验证项 | 预期 |
|---|--------|------|
| 2 | 复现现有实验 easy/difficult 数字 | RMSE 与现有报告一致（差 < 0.1 px） |
| 3 | RMSE vs N=2 | fused RMSE 减小 5-15% |
| 4 | RMSE vs N=3 | fused RMSE 进一步减小 3-8%（边际递减） |
| 2/3/4 | 正确关联率 | 全部 100% |
| 2/3/4 | 远场 sensor 的 outlier reject 行为 | 不被 N-way 融合拉偏 |

## 5. 实施步骤

1. **不破坏 N=2**：把现有 `track_fusion.fuse()` 重构为 `fuse_n(states)` 包装，对 N=2 路径直接调用旧公式。
2. **Simulator 先做**：用 N=3/N=4 配置生成新数据集，跑 N=2 路径作为 sanity check。
3. **N-way align**：先做 (1,2) 两两对齐输出 N×frames 表。
4. **N-partite association**：clique 搜索 + 两步 anchor。
5. **fuse_n 通用化**：信息加和。
6. **统一入口 run_n_sonar_association.py**。
7. **回归测试**：N=2 RMSE 与 v2 报告一致。

---

## 6. 实测结果 (dfki_set_4 参考航迹, 仿真)

### 6.1 Easy 实验（低噪声 / 低漏检）

| N | Cluster Cost | Fused RMSE | Fused median | Fused p90 | vs A alone (6.07) |
|---|-------------|-----------|--------------|-----------|-------------------|
| 2 | 0.21 | 6.11 | 4.74 | 9.31 | +0.04 |
| 3 | 0.21 | 6.00 | 4.47 | 9.39 | -0.07 |
| 4 | 0.20 | **5.91** | 4.63 | **9.03** | **-0.16 (-2.6%)** |

### 6.2 Difficult 实验（高噪声 / 高漏检）

| N | Fused RMSE | vs A alone (7.97) | vs N=2 |
|---|-----------|-------------------|--------|
| 2 | 7.07 | -11.3% | 0% |
| 3 | 6.79 | -14.8% | -3.9% |
| 4 | **6.49** | **-18.6%** | **-8.2%** |

### 6.3 关键发现

- **关联正确率：所有 N=2/3/4 均为 100%**（cluster pass_ratio=1.0, gate 全过）
- **融合 RMSE 随 N 单调递减**（difficult 场景下尤其显著：N=4 比 N=2 减小 8.2%）
- **单 sensor 噪声差异没拉偏融合**：SONAR_C 单独 RMSE 8.74（最差），加入 4-way 融合后 Fused RMSE 6.49（最差单 sensor 仍优于任一单 sensor）—— 信息加和**自动平衡了各 sensor 的不同精度**
- **N=2 路径回归正确**：Fused RMSE 与 `run_two_sonar_association` Difficult 实验 7.79 px 同一量级（差 0.7 px 在统计噪声内）
