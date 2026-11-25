"""
N-Sonar Simulator — 通用 N 个虚拟声纳仿真器

从 1 个或多个参考航迹生成 N 个虚拟声纳的观测数据。

设计原则:
  - 复用 `simulate_two_sonars.VirtualSonar` / `ReferenceTrack` / `DistractorTrajectory`
    （它们已经是 sensor-agnostic 的，只是 main() 写死了 2 个声纳）
  - 配置文件中的 sonars 列表可任意长度
  - 每个声纳可有独立的 distractor 集
  - 输出 schema 与现有 2-sonar 一致：
        sonar_{X}_measurements.csv  (含 sensor_id 列)
        sonar_{X}_tracks.csv         (含 local_track_id)
        {X}_distractors.csv
        reference_track_{T}.csv      (每个目标一个文件)
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

# 让 `python n_sonar_simulator.py` 也能 import 兄弟模块
sys.path.insert(0, str(Path(__file__).parent))

from simulate_two_sonars import (  # noqa: E402
    ReferenceTrack,
    VirtualSonar,
    DistractorTrajectory,
    load_reference_track_from_dfki,
    load_reference_track_from_gt,
)


def _build_distractor_records(
    sonar: VirtualSonar,
    ref_track: ReferenceTrack,
    distractor_cfgs: Dict[str, Dict],
    base_seed: int,
) -> List[Dict]:
    """为一个声纳生成所有 distractor 测量记录。"""
    records: List[Dict] = []
    for local_track_id, cfg in distractor_cfgs.items():
        rng = np.random.default_rng(base_seed + hash(local_track_id) % 10000)
        gen = DistractorTrajectory(cfg, local_track_id, rng)
        dist_t, dist_pos = gen.generate(
            ref_track, ref_track.x[0], ref_track.y[0]
        )
        for i in range(len(dist_t)):
            meas = sonar.generate_measurement(
                dist_pos[i, 0], dist_pos[i, 1], add_noise=True
            )
            if meas is not None:
                records.append({
                    'timestamp': dist_t[i],
                    'sensor_id': sonar.sensor_id,
                    'local_track_id': local_track_id,
                    'x': meas['x_common'],
                    'y': meas['y_common'],
                    'vx': 0, 'vy': 0,
                    'measurement_available': True,
                })
    return records


def run_simulation(
    config: Dict,
    output_dir: Path,
    *,
    reference_tracks: Dict[str, ReferenceTrack],
    seed: int,
    experiment_type: str = "easy",
) -> Dict:
    """
    主仿真入口。

    参数:
        config          : 解析后的 json 字典, 含 "sonars" 列表和 "distractors_per_sonar" 字典
        output_dir      : 输出目录
        reference_tracks: {track_label: ReferenceTrack}
        seed            : 随机种子
        experiment_type: "easy" / "difficult" (调整噪声)
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    sonars_cfg: List[Dict] = config["sonars"]

    # 实验类型：整体放大噪声/漏检
    noise_mult = 1.0
    miss_a, miss_b = 1.0, 1.0
    if experiment_type == "easy":
        noise_mult = 0.8
        miss_a, miss_b = 0.02, 0.03
    elif experiment_type == "difficult":
        noise_mult = 1.5
        miss_a, miss_b = 0.10, 0.12

    results: Dict[str, Dict] = {}

    for s_cfg in sonars_cfg:
        s_id = s_cfg["id"]
        s_cfg_eff = s_cfg.copy()
        s_cfg_eff["sigma_range_m"] *= noise_mult
        s_cfg_eff["sigma_bearing_deg"] *= noise_mult
        # 漏检率：sonar A/B/C/D ... 取自己的 miss, 然后按实验整体缩放
        s_cfg_eff["miss_probability"] = s_cfg.get("miss_probability", 0.02)
        if experiment_type != "default":
            # 对 easy/difficult 统一应用 miss_probability
            s_cfg_eff["miss_probability"] = max(
                s_cfg.get("miss_probability", 0.02),
                miss_a if s_id.endswith("A") else (miss_b if s_id.endswith("B") else 0.04),
            )

        sonar = VirtualSonar(s_cfg_eff, s_id, rng)

        # 对每个参考航迹都生成测量 (这里是单目标场景：每个声纳测每个目标)
        all_meas: List[pd.DataFrame] = []
        for ref_label, ref in reference_tracks.items():
            df = sonar.simulate_from_reference(ref, add_noise=True)
            df["ref_label"] = ref_label
            df["local_track_id"] = f"{s_id}_{ref_label}"
            all_meas.append(df)
        meas_df = pd.concat(all_meas, ignore_index=True)

        # 保存测量
        meas_csv = output_dir / f"sonar_{s_cfg['short']}_measurements.csv"
        meas_df.to_csv(meas_csv, index=False)
        results[s_id] = {"measurements_csv": str(meas_csv), "n_meas": len(meas_df)}

        # 干扰
        distractor_cfgs = config.get("distractors_per_sonar", {}).get(s_id, {})
        dist_records = _build_distractor_records(
            sonar,
            list(reference_tracks.values())[0],  # 干扰只相对第一个 ref
            distractor_cfgs,
            base_seed=seed + 1000,
        )
        if dist_records:
            dist_df = pd.DataFrame(dist_records)
            dist_csv = output_dir / f"{s_cfg['short']}_distractors.csv"
            dist_df.to_csv(dist_csv, index=False)
            results[s_id]["distractors_csv"] = str(dist_csv)

    # 保存参考航迹
    for label, ref in reference_tracks.items():
        ref.to_csv(output_dir / f"reference_track_{label}.csv")

    return results


def main():
    parser = argparse.ArgumentParser(description="N-sonar simulator")
    parser.add_argument("--config", required=True, help="N-sonar config JSON")
    parser.add_argument("--input", required=True,
                        help="DFKI tracks_raw.csv (PRIMARY_DIVER)")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--experiment", default="easy",
                        choices=["easy", "difficult", "default"])
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)
    seed = args.seed if args.seed is not None else config.get("random_seed", 42)

    reference = load_reference_track_from_dfki(args.input, "PRIMARY_DIVER")
    print(f"Reference track: {reference.length} points, "
          f"t=[{reference.timestamps[0]:.2f}, {reference.timestamps[-1]:.2f}]s")

    results = run_simulation(
        config=config,
        output_dir=Path(args.output),
        reference_tracks={"T1": reference},
        seed=seed,
        experiment_type=args.experiment,
    )
    print("\n=== N-sonar simulation complete ===")
    for s_id, info in results.items():
        print(f"  {s_id}: {info['n_meas']} measurements")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
