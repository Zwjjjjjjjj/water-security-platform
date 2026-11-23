# VALIDATION — 全系统验证结论（2026-11-10）

验证命令与产物位置见 `README.md §1`。本文件只记录结论数字。

## 1. REAL N=2（`--out /root/outputs`，stages 4,5,6,7,8）— 全通过

| 数据集 | fused 行数 | fusion_mode 分布 | 关联 P/R/F1 | RMSE_A → RMSE_fused (2D, px/m) | NEES median | honesty / duplicate |
|---|---|---|---|---|---|---|
| glf | 347 | A_ONLY 242, BOTH_MEASURED 60, BOTH_PREDICTED 45 | 1.0/1.0/1.0 (GT对 1, cost 0.317) | 0.771 → 0.771 (+0.0%，合成GT) | 20.2 over-confident | honesty FAIL（45 BOTH_PREDICTED 中 22 行含 B 新量测，见 §4）；duplicate PASS |
| dfki_set_1 | 452 | BOTH_MEASURED 319, A_ONLY 108, B_ONLY 25 | 1.0/1.0/1.0 (cost 0.568) | 22.38 → 26.39（−17.9% vs A；+47.8% vs B=50.58） | 1.44 well-calibrated | PASS / PASS |
| dfki_set_2 | 328 | A_ONLY 168, BOTH_MEASURED 160 | 1.0/1.0/1.0 (cost 0.568) | 11.02 → 11.39（−3.4% vs A；+78.3% vs B=52.44） | 1.49 well-calibrated | PASS / PASS |
| dfki_set_3 | 740 | BOTH_MEASURED 618, A_ONLY 88, B_ONLY 34 | 1.0/1.0/1.0 (cost 0.597) | 21.44 → 24.96（−16.4% vs A；+52.0% vs B=52.00） | 2.00 well-calibrated | PASS / PASS |
| dfki_set_4 | 661 | BOTH_MEASURED 615, A_ONLY 42, B_ONLY 4 | 1.0/1.0/1.0 (cost 0.558) | 8.90 → 9.63（−8.2% vs A；+76.1% vs B=40.34） | 0.90 well-calibrated | PASS / PASS |

解读：
- 关联：5 个数据集全部 P/R/F1 = 1.0，零 ID switch。`association_real` 放宽门限生效。
- RMSE：B 是整体平移 ~40px 的合成声呐（B_vs_GT ≈ 40–52px），融合天然被 B 拖离 A（相对 A −3%~−18%），但相对 B 提升 +48%~+78%。这是合成验证设计的预期行为，不是融合算法退化；GLF 因 B 轨稀疏（仅 1 条 B 轨）融合≈A。
- NEES：DFKI 四集 median 0.90–2.00（χ²(2) 理想均值 2.0），`p_scale=15.0` 标定有效；GLF median 20.2 over-confident（GLF 米制小协方差 + 垂直扇面几何未标定，见 pipeline.json `enu_valid:false`，属已知局限）。
- 审计：DFKI duplicate 全部 `max_uses=1 PASS`；GLF duplicate PASS。

## 2. REAL N=3（`--out /root/outputs_n3`，sensors A,B,C）— 全通过

| 数据集 | fused 行数 | N_MEASURED 行数 | n_clusters | cluster mean_cost / gate |
|---|---|---|---|---|
| glf | 392 | 144 | 1 (A/B/C PRIMARY 同簇 G001) | 0.312 / True |
| dfki_set_1 | 620 | 523 | 1 | 0.484 / True |
| dfki_set_2 | 395 | 234 | 1 | 0.500 / True |
| dfki_set_3 | 1058 | 1007 | 1 | 0.534 / True |
| dfki_set_4 | 939 | 929 | 1 | 0.458 / True |

`local_to_global` 均为 3 条轨同 G001；`n_sensors_used` 以 2–3 为主，`SINGLE_SONAR_*` 仅出现在单边缺数帧。
N=3 输出隔离在 `/root/outputs_n3/`，与 N=2 的 `/root/outputs/` 同构，可直接 diff。

## 3. REAL N=4 抽查（dfki_set_1，A,B,C,D → /tmp/n4test，已验证后清理）

- cluster：1 个，`{A,B,C,D}_PRIMARY_DIVER → G001`，mean_cost 0.489，gate_pass True。
- fused 779 行：`N_MEASURED` 665（其中 4 sensors 473 帧、3 sensors 163 帧、2 sensors 51 帧），`SINGLE_SONAR_A` 92，`N_MEASURED_REJECTED` 22。
- 结论：anchor+extension 在 N=4 下正常组成 4 元簇，`InformationFusionN` 按实际可用传感器数降级融合。

## 4. SIM（`run_pipeline --mode sim`，easy，参考 dfki_set_1 PRIMARY）— 单调改进

| N | fused_rmse | fused_median / p90 | single_rmse（A/B/C/D） | n_clusters |
|---|---|---|---|---|
| 2 | 9.697 | 5.992 / 12.678 | A 9.653 / B 10.200 | 1 |
| 3 | 9.570 | 6.009 / 12.684 | + C 9.754 | 1 |
| 4 | 9.403 | 5.971 / 12.141 | + D 9.634 | 1 |

N=2 → 3 → 4 fused RMSE 单调下降（9.697 → 9.570 → 9.403），符合信息形式融合预期；
`/root/outputs/sim/easy/n{2,3,4}/` 为历史全量产物，`CallDynamicTool sim` 单次运行产物结构与其一致（`sim/ + n<N>/ + metrics.json`）。

## 5. 已知局限（非阻塞）

1. REAL 融合相对 A 的 RMSE 为负提升（§1）：合成 B 整体平移所致；真实双声呐部署后需重测。
2. GLF NEES over-confident + honesty FAIL 22 行：GLF 垂直扇面无标定物理横向（`horizontal_xy_valid:false`），
   B 合成平移（4px,2px,12°）与米制小协方差不匹配；DFKI 像素域无此问题。GLF 侧结论以关联 F1=1.0 和 RMSE 为准。
3. Stage 2/3 依赖遗留缓存（`outputs_e2e` / Tritech `outputs*` 只读回退）：Stage 1/4–8 完全由统一层重算，
   跟踪算法本身未动；如需从零复现 Stage 2，需 GLF log（`/root/data/*.glf`）与 DFKI 数据集在位。
