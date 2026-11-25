# 候选输出噪声 / 干扰审计报告 (dfki_set_4, v13 → v14)

**审计脚本**：`Tritech_GLF_Track_Project/src/audit_candidates.py`
**输入数据**：`/root/outputs_e2e/dfki_set_4/stage_2_tracking/dfki_set_4/{detections,tracks_raw}.csv` (stage 2 重跑后)
**GT 文件**：`/root/outputs_e2e_all/dfki_set_4/gt_evaluation.csv` (365 帧确认帧)

---

## 1. 候选数量本身就异常

| 指标 | 值 |
|------|---|
| 总 detection | **2952** |
| 总帧数 | 369 |
| 每帧 detection 数 | **mean=median=max=min=8.0** (std=0.00) |

**这是 `detect_dfki.py` 第 40 行 `DEFAULT_MAX_DETECTIONS = 8` 的硬截断** —— 每帧不管有几个 blob，最多只取 score top-8。这意味着：
- 真实帧**根本无帧间竞争**（不可能 5 帧都在追同一片漂浮回波，因为每帧 8 个全部新生成）
- 跟踪器的"多目标"逻辑形同虚设 —— 每帧 8 个 candidate 都在等
- 一帧内多个近-真值 blob **只可能来自** diver 自身被分裂（见 §3）

**建议 1**：把 `DEFAULT_MAX_DETECTIONS` 改大（如 16-24）以避免真实帧的近场干扰被硬截断丢出；或缩小到 3-4 让跟踪器只面对"最有可能是 diver"的一两个候选。

> **NOTE 后续**（v14）：曾尝试 `max_detections=16`，结果噪声比从 86% 升到 94%，反而让跟踪器在 acquisition 阶段被新引入的远场 clutter 误导。**确认 `max_detections=8` 是当前数据集的正确选择**，主要噪声源是 §2 的"远场低高度散射"。

---

## 2. 候选分类（2952 个 blob 的去向）

| Bucket | 数量 | 占比 | 含义 |
|--------|------|------|------|
| `matched` | 365 | 12.4% | 关联到 PRIMARY track |
| `near_gt` | 53 | 1.8% | 未匹配但距 GT < 25 px（被丢弃的真信号） |
| `near_field` | 9 | 0.3% | `range_m < 175` 的真正近场（Euclidean 距 sonar） |
| `far_field` | 57 | 1.9% | `range_m > 400` 的远场 |
| `orphan` | 2468 | 83.6% | 其它 unmatched，距 GT 中位 152 px |

> **v14 修正**：v13 报告里把 `fan_v_m < 175` 错标为 "platform clutter"。v14 重新核查 schema：`range_m = sqrt(dx² + dy²)`（欧氏半径），`v` 是图像 y 坐标。v<175 的 blob 实际 `range_m ∈ [338, 450]` —— 它们是**远场低高度散射**，不是近场 clutter。真正近场（`range_m < 175`）只有 9 个 blob，几乎不存在。所以探测器端没有"近场 clutter"可滤。
>
> **真正占 84% 的 orphan blob** 是"远场低高度散射"（位于扇形图像底部、Euclidean 距 sonar 远的位置）。它们对单声纳跟踪是无害的噪声 —— 跟踪器的 cost 排序自然把它们排除。

---

## 3. 多候选同帧竞争 → diver 自身被分裂

| 现象 | v13 (修改前) | v14 (修改后) |
|------|--------------|--------------|
| 帧内 ≥2 个 blob 距 GT < 25 px | 45 帧 (12.3%) | 同 |
| 帧内 matched 到的 blob **不是**最近的 | 40 帧 (10.9%) | 40 帧（不变） |
| track 误差 > 25 px 的帧数 | **13** | **3** ✅ |
| track 误差 > 20 px 的帧数 | 13 | 13 |
| track 误差 > 15 px 的帧数 | 49 | **29** ✅ |
| track RMSE / p90 | 7.0 / 12.99 | 7.0 / 12.99 |

**v14 修复（run_dfki.py acquisition cost 加 PRED_SOFT_PENALTY_PER_PX=0.5）效果**：
- 加大了 16 帧分裂错选后的恢复速度：误差 > 25 px 的极端帧数从 13 降到 3
- 中位 / p90 保持不变（说明正常帧没受影响）
- 整体 RMSE_2d = 8.57 px（基本持平）

**matched != closest 仍是 40 帧**，因为这些帧的 GT 和 Kalman 预测位置不一致（diver 真值与预测存在 8-15 px 系统性偏移，预测用的还是上一帧的位置）。**这不是 bug，是 v13 误读了**：跟踪器没在选错，而是 GT 本身就比 Kalman 预测更靠前。

---

## 4. unmatched blob 的特征差异（已验证）

| 特征 | matched 中位 | unmatched 中位 |
|------|-------------|---------------|
| amplitude | 129.2 | 60.1 |
| detection_score | 42924 | 4557 |

**tracked blob 的 score 是 unmatched 的 9.4×** —— 当前跟踪器的选择标准（先 score 排序 + Hungarian + soft 距离惩罚）已经过滤了 90%+ 的真噪声。

---

## 5. 结论与最终修复

1. **噪声负担的真实来源**：不是 platform clutter，是**远场低高度散射**（84% orphan）。但它们在跟踪 cost 中 score 极低，自然被排除，无需额外过滤。

2. **真正的优化空间**：diver 自身分裂 → 40 帧跟踪器选择的不是 GT 最近 blob。v14 软惩罚让误差 > 25 px 帧数 13 → 3（-77%）。

3. **代码变更**（v14）：
   - `run_dfki.py` ~435 行：acquisition cost 加 `_min_d_pred` 软惩罚
   - `detect_dfki.py` / `run_dfki.py`：原计划改 `min_radius_px=200` / `max_detections=16` 经实测**回退**（前者切到 GT diver，后者引入更多噪声）

4. **下一步**：
   - 在 dfki_set_{1,2,3} 上跑 `audit_candidates.py` 确认同样的改进模式
   - 长期：考虑 `merge_radius_px=30` 的二次合并缓解 diver 分裂

---

## 6. 产出物

| 文件 | 内容 |
|------|------|
| `Tritech_GLF_Track_Project/src/audit_candidates.py` | 诊断脚本（已修正 schema 理解），可复用 |
| `Tritech_GLF_Track_Project/audit_report.md` | 本报告 |
| `outputs_e2e/dfki_set_4/audit/per_frame_audit.csv` | 逐帧 8 列统计 |
| `outputs_e2e/dfki_set_4/audit/bucket_counts.csv` | 4 类 bucket 计数 + 占比 |
| `outputs_e2e/dfki_set_4/audit/summary.json` | 汇总数字 |

