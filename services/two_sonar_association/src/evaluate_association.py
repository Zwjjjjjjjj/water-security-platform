"""
Evaluate Association Results - Evaluation Metrics for Track-to-Track Association
评估航迹关联结果
"""
import numpy as np
import pandas as pd
import json
from typing import Dict, List, Tuple
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG


def evaluate_association(
    associations: pd.DataFrame,
    cost_matrix: pd.DataFrame,
    aligned_pairs: pd.DataFrame,
    ground_truth: Dict[str, str],
    config: Dict
) -> Dict:
    """
    评估航迹关联结果
    
    参数:
        associations: 关联结果 DataFrame
        cost_matrix: 代价矩阵
        aligned_pairs: 对齐后的航迹对
        ground_truth: GT 映射，例如 {'A_DIVER': 'B_DIVER', 'A_DIST_01': None, ...}
        config: 配置
    
    返回:
        评估指标字典
    """
    results = {
        'experiment_type': config.get('experiment_type', 'unknown'),
        'random_seed': config.get('random_seed', 0),
        'sonar_A_config': config.get('sonar_A', {}),
        'sonar_B_config': config.get('sonar_B', {}),
        'metrics': {}
    }
    
    if len(associations) == 0:
        results['metrics'] = {
            'true_association_rate': 0.0,
            'false_association_rate': 0.0,
            'missed_association_rate': 1.0,
            'association_precision': 0.0,
            'association_recall': 0.0,
            'num_associations': 0,
            'num_correct': 0,
            'num_incorrect': 0,
            'num_gt_pairs': 1,
            'diver_mean_distance': np.nan,
            'diver_median_distance': np.nan,
            'diver_p90_distance': np.nan
        }
        return results
    
    # 统计关联对
    true_pairs = 0
    false_pairs = 0
    missed_pairs = 0
    
    # DIVER 航迹的距离统计
    diver_distances = []
    
    for _, assoc in associations.iterrows():
        track_a_id = assoc['track_a_id']
        track_b_id = assoc['track_b_id']
        
        # 查找 GT 中的对应关系
        expected_b = ground_truth.get(track_a_id)
        
        if expected_b is None:
            # A 航迹没有 GT 对应
            continue
        
        if track_b_id == expected_b:
            true_pairs += 1
        else:
            false_pairs += 1
        
        # 计算 DIVER-DIVER 的距离
        if 'DIVER' in track_a_id and 'DIVER' in track_b_id:
            pairs_data = aligned_pairs[
                (aligned_pairs['track_a_id'] == track_a_id) &
                (aligned_pairs['track_b_id'] == track_b_id)
            ]
            if len(pairs_data) > 0:
                diver_distances.extend(pairs_data['position_distance'].dropna().tolist())
    
    # 计算 GT 中有多少对需要关联
    num_gt_pairs = sum(1 for v in ground_truth.values() if v is not None)
    
    # 正确识别但未关联的
    for track_a_id, expected_b in ground_truth.items():
        if expected_b is None:
            continue
        found = any(
            (associations['track_a_id'] == track_a_id) &
            (associations['track_b_id'] == expected_b)
        )
        if not found:
            missed_pairs += 1
    
    # 计算指标
    total_associations = true_pairs + false_pairs
    num_correct = true_pairs
    num_incorrect = false_pairs
    
    # True Association Rate: 正确关联数 / 应该关联数
    true_association_rate = true_pairs / num_gt_pairs if num_gt_pairs > 0 else 0.0
    
    # False Association Rate: 错误关联数 / 总关联数
    false_association_rate = false_pairs / total_associations if total_associations > 0 else 0.0
    
    # Missed Association Rate: 未识别数 / 应该关联数
    missed_association_rate = missed_pairs / num_gt_pairs if num_gt_pairs > 0 else 0.0
    
    # Precision: 正确关联 / 总关联
    precision = true_pairs / total_associations if total_associations > 0 else 0.0
    
    # Recall: 正确关联 / 应该关联
    recall = true_pairs / num_gt_pairs if num_gt_pairs > 0 else 0.0
    
    results['metrics'] = {
        'true_association_rate': true_association_rate,
        'false_association_rate': false_association_rate,
        'missed_association_rate': missed_association_rate,
        'association_precision': precision,
        'association_recall': recall,
        'num_associations': total_associations,
        'num_correct': num_correct,
        'num_incorrect': num_incorrect,
        'num_missed': missed_pairs,
        'num_gt_pairs': num_gt_pairs,
        'diver_mean_distance': np.mean(diver_distances) if diver_distances else np.nan,
        'diver_median_distance': np.median(diver_distances) if diver_distances else np.nan,
        'diver_p90_distance': np.percentile(diver_distances, 90) if diver_distances else np.nan,
        'diver_num_samples': len(diver_distances)
    }
    
    # 添加关联确认时间
    if 'CONFIRMED' in associations['status'].values if 'status' in associations.columns else False:
        confirmed_assoc = associations[associations['status'] == 'CONFIRMED']
        if len(confirmed_assoc) > 0:
            # 简化：使用第一个确认关联的代价作为确认时间代理
            results['metrics']['mean_association_cost'] = confirmed_assoc['cost'].mean()
    
    return results


def compute_association_latency(history: pd.DataFrame) -> Dict:
    """
    计算关联延迟
    """
    if len(history) == 0:
        return {'confirmation_time': np.nan, 'confirmation_count': 0}
    
    # 找到第一次 CONFIRMED 的时间
    confirmed = history[history['status_after'] == 'CONFIRMED']
    
    if len(confirmed) == 0:
        return {'confirmation_time': np.nan, 'confirmation_count': 0}
    
    first_confirmed_time = confirmed['timestamp'].min()
    first_update_time = history['timestamp'].min()
    
    latency = first_confirmed_time - first_update_time if first_update_time else np.nan
    
    return {
        'confirmation_time': latency,
        'confirmation_count': len(confirmed)
    }


def generate_evaluation_report(results: Dict, output_path: str):
    """
    生成评估报告
    """
    report = []
    report.append("=" * 60)
    report.append("TRACK-TO-TRACK ASSOCIATION EVALUATION REPORT")
    report.append("=" * 60)
    report.append("")
    
    # 实验配置
    report.append("EXPERIMENT CONFIGURATION")
    report.append("-" * 40)
    report.append(f"Experiment Type: {results.get('experiment_type', 'N/A')}")
    report.append(f"Random Seed: {results.get('random_seed', 'N/A')}")
    report.append("")
    
    # 传感器配置
    report.append("SONAR A CONFIGURATION")
    report.append("-" * 40)
    sonar_a = results.get('sonar_A_config', {})
    for k, v in sonar_a.items():
        report.append(f"  {k}: {v}")
    report.append("")
    
    report.append("SONAR B CONFIGURATION")
    report.append("-" * 40)
    sonar_b = results.get('sonar_B_config', {})
    for k, v in sonar_b.items():
        report.append(f"  {k}: {v}")
    report.append("")
    
    # 评估指标
    report.append("EVALUATION METRICS")
    report.append("-" * 40)
    metrics = results.get('metrics', {})
    
    report.append(f"Total Associations: {metrics.get('num_associations', 0)}")
    report.append(f"Correct Associations: {metrics.get('num_correct', 0)}")
    report.append(f"Incorrect Associations: {metrics.get('num_incorrect', 0)}")
    report.append(f"Missed Associations: {metrics.get('num_missed', 0)}")
    report.append("")
    
    report.append(f"True Association Rate: {metrics.get('true_association_rate', 0):.2%}")
    report.append(f"False Association Rate: {metrics.get('false_association_rate', 0):.2%}")
    report.append(f"Missed Association Rate: {metrics.get('missed_association_rate', 0):.2%}")
    report.append("")
    
    report.append(f"Association Precision: {metrics.get('association_precision', 0):.2%}")
    report.append(f"Association Recall: {metrics.get('association_recall', 0):.2%}")
    report.append("")
    
    # DIVER-DIVER 距离
    report.append("DIVER-DIVER TRACK DISTANCE")
    report.append("-" * 40)
    report.append(f"Mean Distance: {metrics.get('diver_mean_distance', np.nan):.2f} m")
    report.append(f"Median Distance: {metrics.get('diver_median_distance', np.nan):.2f} m")
    report.append(f"P90 Distance: {metrics.get('diver_p90_distance', np.nan):.2f} m")
    report.append(f"Number of Samples: {metrics.get('diver_num_samples', 0)}")
    report.append("")
    
    report.append("=" * 60)
    
    # 保存报告
    report_text = "\n".join(report)
    
    with open(output_path, 'w') as f:
        f.write(report_text)
    
    print(f"Evaluation report saved to {output_path}")
    print("\n" + report_text)
    
    return report_text


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Evaluate association results')
    parser.add_argument('--associations', type=str, required=True,
                       help='Path to association results CSV')
    parser.add_argument('--cost-matrix', type=str, required=True,
                       help='Path to cost matrix CSV')
    parser.add_argument('--aligned-pairs', type=str, required=True,
                       help='Path to aligned pairs CSV')
    parser.add_argument('--output', type=str, required=True,
                       help='Output evaluation JSON')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Config file')
    
    args = parser.parse_args()
    
    # 加载数据
    associations = pd.read_csv(args.associations)
    cost_matrix = pd.read_csv(args.cost_matrix)
    aligned_pairs = pd.read_csv(args.aligned_pairs)
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    # GT 映射（用于评估）
    ground_truth = {
        'A_DIVER': 'B_DIVER',
        # Distractors 没有 GT 对应
    }
    
    # 评估
    results = evaluate_association(
        associations, cost_matrix, aligned_pairs,
        ground_truth, config
    )
    
    # 保存结果
    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2)
    
    # 生成报告
    report_path = args.output.replace('.json', '_report.txt')
    generate_evaluation_report(results, report_path)


if __name__ == '__main__':
    main()
