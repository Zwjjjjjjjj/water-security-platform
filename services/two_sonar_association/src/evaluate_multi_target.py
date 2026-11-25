"""
Multi-Target Association Evaluation
多目标航迹关联评估器

专门用于评估多目标场景下的关联正确性：
- ID switch 检测
- 每个 Global Track 与 GT target 的对应关系
- 区分跨声纳关联 vs 单声纳关联
"""
import numpy as np
import pandas as pd
import json
from typing import Dict, List, Tuple, Optional, Set
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))


def evaluate_multi_target_association(
    local_to_global: pd.DataFrame,
    ground_truth: Dict[str, str],   # local_track_id -> target_id
    tracks_df: pd.DataFrame,
    associations_df: pd.DataFrame,
    output_path: str
) -> Dict:
    """
    评估多目标关联结果
    
    参数:
        local_to_global: Local Track -> Global ID 映射
        ground_truth: local_track_id -> target_id (用于 evaluation)
        tracks_df: 所有 Local Track 数据 (含 sensor_id, track_id)
        associations_df: 关联结果 (track_a_id, track_b_id, status)
        output_path: 输出评估文件路径
    """
    results = {
        'num_local_tracks': len(local_to_global),
        'num_global_tracks': 0,
        'num_targets_in_gt': len(set(ground_truth.values())),
        'metrics': {}
    }
    
    if len(local_to_global) == 0:
        return results
    
    # 统计 Global Track
    n_global = local_to_global['global_id'].nunique()
    results['num_global_tracks'] = n_global
    
    # 将每个 Local Track 映射到 (sensor, target_id)
    if len(local_to_global) > 0 and 'local_track_id' in local_to_global.columns:
        # 合并 ground truth
        merged = local_to_global.merge(
            tracks_df[['track_id', 'sensor_id']].drop_duplicates(),
            left_on='local_track_id',
            right_on='track_id',
            how='left'
        )
        
        # 应用 GT 映射
        merged['gt_target'] = merged['local_track_id'].map(ground_truth)
        
        # 为每个 Global Track 统计
        gt_per_global = merged.groupby('global_id')['gt_target'].agg(
            lambda x: list(set(x.dropna()))
        )
        
        # ID switch 检测：如果一个 Global Track 包含多个 GT target
        id_switch_count = sum(1 for targets in gt_per_global if len(targets) > 1)
        
        # 统计每个 GT target 跨越的 Global Track 数
        gt_spread = {}
        for gid, targets in gt_per_global.items():
            for target in targets:
                if target not in gt_spread:
                    gt_spread[target] = set()
                gt_spread[target].add(gid)
        
        # Track fragmentation: 每个 GT target 跨越的 Global Track 数
        fragmentation = {target: len(gids) for target, gids in gt_spread.items()}
        
        # ID switch rate
        total_targets = len(gt_spread)
        id_switch_rate = id_switch_count / max(1, n_global)
        fragmentation_rate = sum(1 for v in fragmentation.values() if v > 1) / max(1, total_targets)
        
        results['metrics'] = {
            'num_global_tracks': n_global,
            'num_gt_targets': total_targets,
            'id_switch_count': id_switch_count,
            'id_switch_rate': id_switch_rate,
            'fragmentation_rate': fragmentation_rate,
            'gt_target_spread': {str(k): len(v) for k, v in gt_spread.items()},
            'gt_target_fragmentation': fragmentation
        }
        
        # 关联对数统计
        if len(associations_df) > 0 and 'status' in associations_df.columns:
            confirmed = associations_df[associations_df['status'] == 'CONFIRMED']
            results['metrics']['num_confirmed_pairs'] = len(confirmed)
            results['metrics']['num_tentative_pairs'] = len(
                associations_df[associations_df['status'] == 'TENTATIVE']
            )
            results['metrics']['num_coasting_pairs'] = len(
                associations_df[associations_df['status'] == 'COASTING']
            )
            results['metrics']['num_lost_pairs'] = len(
                associations_df[associations_df['status'] == 'LOST']
            )
    
    # 保存结果
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    return results


def build_ground_truth_from_scenario(
    scenario_type: str,
    seed: int = 42,
    n_divers: int = 2
) -> Dict[str, str]:
    """
    根据场景类型构造 GT 映射
    
    返回: local_track_id -> target_id 映射
    例如: 'A_T1' -> 'T1', 'B_T1' -> 'T1'
    """
    gt = {}
    
    # 默认基于 ID 前缀的映射
    # A_T1 (Sonar A 看到的 Target 1) -> T1
    # B_T2 (Sonar B 看到的 Target 2) -> T2
    
    # 注意：实际的 GT 映射需要根据仿真数据中的 target_id 字段构造
    # 这里只提供通用框架
    return gt


def check_id_switch(merged_df: pd.DataFrame) -> List[Dict]:
    """
    检测 ID switch
    
    输入 DataFrame 应包含: global_id, gt_target
    """
    switches = []
    
    if 'global_id' not in merged_df.columns or 'gt_target' not in merged_df.columns:
        return switches
    
    # 按 global_id 分组，检查每个 global_id 是否包含多个不同的 gt_target
    for gid, group in merged_df.groupby('global_id'):
        unique_targets = group['gt_target'].dropna().unique()
        if len(unique_targets) > 1:
            switches.append({
                'global_id': gid,
                'gt_targets': list(unique_targets),
                'type': 'ID_SWITCH',
                'severity': len(unique_targets)
            })
    
    return switches


def evaluate_experiment(output_dir: str,
                       gt_mapping: Dict[str, str]) -> Dict:
    """
    评估实验结果的统一入口
    """
    output_path = Path(output_dir)
    
    local_to_global_path = output_path / 'local_to_global.csv'
    tracks_path = output_path / 'all_local_tracks.csv'
    associations_path = output_path / 'association_results.csv'
    
    # 加载数据
    local_to_global = pd.read_csv(local_to_global_path) if local_to_global_path.exists() else pd.DataFrame()
    tracks_df = pd.read_csv(tracks_path) if tracks_path.exists() else pd.DataFrame()
    associations_df = pd.read_csv(associations_path) if associations_path.exists() else pd.DataFrame()
    
    eval_path = output_path / 'multi_target_evaluation.json'
    
    results = evaluate_multi_target_association(
        local_to_global, gt_mapping, tracks_df, associations_df, str(eval_path)
    )
    
    return results


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Evaluate multi-target association')
    parser.add_argument('--output-dir', type=str, required=True)
    parser.add_argument('--gt-mapping', type=str, default=None,
                       help='Path to JSON file with GT mapping')
    
    args = parser.parse_args()
    
    gt_mapping = {}
    if args.gt_mapping:
        with open(args.gt_mapping, 'r') as f:
            gt_mapping = json.load(f)
    
    results = evaluate_experiment(args.output_dir, gt_mapping)
    print(json.dumps(results, indent=2, default=str))


if __name__ == '__main__':
    main()