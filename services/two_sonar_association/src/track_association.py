"""
Track-to-Track Association - Compute Association Costs and Match Tracks
航迹级关联：计算关联代价并匹配航迹
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from scipy.optimize import linear_sum_assignment
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG
from association_utils import normalize_angle, angle_difference_deg


class TrackAssociation:
    """航迹关联器"""
    
    def __init__(self, config: Dict):
        """
        初始化航迹关联器
        
        参数:
            config: 关联配置
        """
        self.position_gate_m = config.get('position_gate_m', 30.0)
        self.velocity_gate_mps = config.get('velocity_gate_mps', 15.0)
        self.heading_gate_deg = config.get('heading_gate_deg', 60.0)
        self.mahalanobis_gate = config.get('mahalanobis_gate_threshold', 9.21)
        self.min_temporal_overlap = config.get('min_temporal_overlap_ratio', 0.3)
        self.min_gate_pass_ratio = config.get('min_gate_pass_ratio', 0.5)
        
        # Cost weights
        weights = config.get('cost_weights', {})
        self.wp = weights.get('wp', 0.35)  # position
        self.wv = weights.get('wv', 0.25)  # velocity
        self.wh = weights.get('wh', 0.15)  # heading
        self.wm = weights.get('wm', 0.15)  # mahalanobis
        self.wt = weights.get('wt', 0.10)  # temporal penalty
        
        # Cost normalization
        norms = config.get('cost_normalization', {})
        self.pos_norm = norms.get('position_threshold_m', 20.0)
        self.vel_norm = norms.get('velocity_threshold_mps', 10.0)
        self.head_norm = norms.get('heading_threshold_deg', 45.0)
        self.maha_norm = norms.get('mahalanobis_threshold', 5.0)
        
        self.association_threshold = config.get('association_threshold', 0.6)
    
    def compute_mahalanobis_distance(self, ax: float, ay: float, 
                                     bx: float, by: float,
                                     p_ax: float, p_ay: float,
                                     p_bx: float, p_by: float) -> float:
        """
        计算两个航迹状态之间的马氏距离
        
        参数:
            ax, ay: Track A 位置
            bx, by: Track B 位置
            p_ax, p_ay: Track A 位置协方差 (Pxx, Pyy)
            p_bx, p_by: Track B 位置协方差 (Pxx, Pyy)
        """
        delta = np.array([ax - bx, ay - by])
        
        # 合并协方差
        S = np.array([
            [p_ax + p_bx, 0],
            [0, p_ay + p_by]
        ])
        
        try:
            S_inv = np.linalg.inv(S)
            d2 = delta @ S_inv @ delta
            return np.sqrt(max(0, d2))
        except np.linalg.LinAlgError:
            # 使用欧氏距离作为后备
            return np.sqrt(delta @ delta)
    
    def compute_track_level_cost(self, aligned_pairs: pd.DataFrame,
                                  track_a_id: str, track_b_id: str) -> Dict:
        """
        计算两条航迹之间的轨迹级关联代价
        
        返回:
            包含所有代价分量的字典
        """
        # 筛选对应的航迹对
        pairs = aligned_pairs[
            (aligned_pairs['track_a_id'] == track_a_id) & 
            (aligned_pairs['track_b_id'] == track_b_id)
        ]
        
        if len(pairs) == 0:
            return None
        
        # 计算各项指标
        pos_dists = pairs['position_distance'].dropna()
        vel_diffs = pairs['velocity_difference'].dropna()
        head_diffs = pairs['heading_difference'].dropna()
        
        if len(pos_dists) == 0:
            return None
        
        # 位置距离统计
        mean_pos_dist = pos_dists.mean()
        median_pos_dist = pos_dists.median()
        p90_pos_dist = pos_dists.quantile(0.9)
        
        # 速度差异统计
        mean_vel_diff = vel_diffs.mean() if len(vel_diffs) > 0 else 100.0
        
        # 航向差异统计
        mean_head_diff = head_diffs.mean() if len(head_diffs) > 0 else 180.0
        
        # 马氏距离
        mahalanobis_dists = []
        for _, row in pairs.iterrows():
            if all(pd.notna([row.get('Ax'), row.get('Ay'), row.get('Bx'), row.get('By'),
                           row.get('APxx'), row.get('APyy'), row.get('BPxx'), row.get('BPyy')])):
                d = self.compute_mahalanobis_distance(
                    row['Ax'], row['Ay'], row['Bx'], row['By'],
                    row.get('APxx', 1.0), row.get('APyy', 1.0),
                    row.get('BPxx', 1.0), row.get('BPyy', 1.0)
                )
                mahalanobis_dists.append(d)
        
        mean_maha_dist = np.mean(mahalanobis_dists) if mahalanobis_dists else 100.0
        
        # 时间重叠率
        total_time = pairs['timestamp'].max() - pairs['timestamp'].min()
        overlap_ratio = len(pairs) / (total_time / 0.1 + 1) if total_time > 0 else 1.0
        
        # 门控通过率
        gate_pass_count = 0
        for _, row in pairs.iterrows():
            if pd.notna(row['position_distance']) and pd.notna(row['velocity_difference']):
                if (row['position_distance'] < self.position_gate_m and
                    row['velocity_difference'] < self.velocity_gate_mps):
                    gate_pass_count += 1
        
        gate_pass_ratio = gate_pass_count / len(pairs) if len(pairs) > 0 else 0
        
        # 归一化代价
        norm_pos_cost = min(1.0, mean_pos_dist / self.pos_norm)
        norm_vel_cost = min(1.0, mean_vel_diff / self.vel_norm)
        norm_head_cost = min(1.0, mean_head_diff / self.head_norm)
        norm_maha_cost = min(1.0, mean_maha_dist / self.maha_norm)
        
        # 时间惩罚
        temporal_penalty = 1.0 - overlap_ratio
        
        # 总代价
        total_cost = (
            self.wp * norm_pos_cost +
            self.wv * norm_vel_cost +
            self.wh * norm_head_cost +
            self.wm * norm_maha_cost +
            self.wt * temporal_penalty
        )
        
        return {
            'track_a_id': track_a_id,
            'track_b_id': track_b_id,
            'num_aligned_points': len(pairs),
            'temporal_overlap_ratio': overlap_ratio,
            'mean_position_distance': mean_pos_dist,
            'median_position_distance': median_pos_dist,
            'p90_position_distance': p90_pos_dist,
            'mean_velocity_difference': mean_vel_diff,
            'mean_heading_difference': mean_head_diff,
            'mean_mahalanobis_distance': mean_maha_dist,
            'gate_pass_ratio': gate_pass_ratio,
            'norm_position_cost': norm_pos_cost,
            'norm_velocity_cost': norm_vel_cost,
            'norm_heading_cost': norm_head_cost,
            'norm_mahalanobis_cost': norm_maha_cost,
            'temporal_penalty': temporal_penalty,
            'total_cost': total_cost,
            'position_gate_pass': mean_pos_dist < self.position_gate_m,
            'velocity_gate_pass': mean_vel_diff < self.velocity_gate_mps,
            'heading_gate_pass': mean_head_diff < self.heading_gate_deg,
            'mahalanobis_gate_pass': mean_maha_dist < self.mahalanobis_gate,
            'temporal_overlap_pass': overlap_ratio >= self.min_temporal_overlap,
            'gate_pass_ratio_pass': gate_pass_ratio >= self.min_gate_pass_ratio,
            'overall_pass': (
                mean_pos_dist < self.position_gate_m and
                mean_vel_diff < self.velocity_gate_mps and
                mean_head_diff < self.heading_gate_deg and
                mean_maha_dist < self.mahalanobis_gate and
                overlap_ratio >= self.min_temporal_overlap and
                gate_pass_ratio >= self.min_gate_pass_ratio
            )
        }
    
    def build_cost_matrix(self, aligned_pairs: pd.DataFrame) -> Tuple[np.ndarray, List[str], List[str]]:
        """
        构建代价矩阵
        
        返回:
            (cost_matrix, track_ids_a, track_ids_b)
        """
        # 获取所有唯一的航迹ID
        track_ids_a = sorted(aligned_pairs['track_a_id'].unique())
        track_ids_b = sorted(aligned_pairs['track_b_id'].unique())
        
        n_a = len(track_ids_a)
        n_b = len(track_ids_b)
        
        # 初始化代价矩阵
        cost_matrix = np.full((n_a, n_b), np.inf)
        cost_details = {}
        
        # 计算每对航迹的代价
        for i, track_a_id in enumerate(track_ids_a):
            for j, track_b_id in enumerate(track_ids_b):
                cost_info = self.compute_track_level_cost(
                    aligned_pairs, track_a_id, track_b_id
                )
                
                if cost_info is not None:
                    # 如果不满足门控要求，给高代价
                    if not cost_info['overall_pass']:
                        cost = 1.0  # 高的固定代价
                    else:
                        cost = cost_info['total_cost']
                    
                    cost_matrix[i, j] = cost
                    cost_details[(track_a_id, track_b_id)] = cost_info
        
        return cost_matrix, track_ids_a, track_ids_b, cost_details
    
    def compute_hungarian_assignment(self, cost_matrix: np.ndarray,
                                    track_ids_a: List[str],
                                    track_ids_b: List[str],
                                    cost_details: Dict) -> List[Tuple[str, str, float]]:
        """
        使用 Hungarian 算法进行最优分配
        
        返回:
            [(track_a_id, track_b_id, cost), ...]
        """
        # 准备方阵
        n = max(cost_matrix.shape[0], cost_matrix.shape[1])
        cost_sq = np.full((n, n), np.inf)
        cost_sq[:cost_matrix.shape[0], :cost_matrix.shape[1]] = cost_matrix
        
        # Hungarian
        row_ind, col_ind = linear_sum_assignment(cost_sq)
        
        assignments = []
        for i, j in zip(row_ind, col_ind):
            if i < len(track_ids_a) and j < len(track_ids_b):
                cost = cost_matrix[i, j]
                if cost < np.inf:
                    assignments.append((track_ids_a[i], track_ids_b[j], cost))
        
        return assignments
    
    def filter_valid_associations(self, assignments: List[Tuple],
                                  cost_details: Dict) -> List[Dict]:
        """
        过滤出有效的关联对
        
        关联必须满足:
        1. cost < threshold
        2. overall_pass = True
        """
        valid_pairs = []
        
        for track_a_id, track_b_id, cost in assignments:
            if cost >= self.association_threshold:
                continue
            
            key = (track_a_id, track_b_id)
            if key in cost_details:
                details = cost_details[key]
                if details['overall_pass']:
                    details['cost'] = cost
                    details['assigned'] = True
                    valid_pairs.append(details)
        
        return valid_pairs


def run_track_association(aligned_pairs_path: str, output_dir: str,
                          config: Dict) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    运行航迹关联
    
    返回:
        (cost_matrix_df, associations_df)
    """
    # 加载对齐后的航迹对
    aligned_pairs = pd.read_csv(aligned_pairs_path)
    
    print(f"Loaded {len(aligned_pairs)} aligned track pairs")
    print(f"Unique Track A IDs: {aligned_pairs['track_a_id'].unique()}")
    print(f"Unique Track B IDs: {aligned_pairs['track_b_id'].unique()}")
    
    # 创建关联器
    associator = TrackAssociation(config['association'])
    
    # 构建代价矩阵
    cost_matrix, track_ids_a, track_ids_b, cost_details = \
        associator.build_cost_matrix(aligned_pairs)
    
    print(f"\nCost Matrix ({len(track_ids_a)} x {len(track_ids_b)}):")
    print(f"Track A: {track_ids_a}")
    print(f"Track B: {track_ids_b}")
    print(cost_matrix)
    
    # Hungarian 分配
    assignments = associator.compute_hungarian_assignment(
        cost_matrix, track_ids_a, track_ids_b, cost_details
    )
    
    print(f"\nHungarian Assignments: {len(assignments)}")
    for a, b, c in assignments:
        print(f"  {a} <-> {b}: cost = {c:.4f}")
    
    # 过滤有效关联
    valid_associations = associator.filter_valid_associations(assignments, cost_details)
    
    print(f"\nValid Associations: {len(valid_associations)}")
    for assoc in valid_associations:
        print(f"  {assoc['track_a_id']} <-> {assoc['track_b_id']}: cost = {assoc['cost']:.4f}")
    
    # 保存代价矩阵
    cost_matrix_data = []
    for i, track_a_id in enumerate(track_ids_a):
        for j, track_b_id in enumerate(track_ids_b):
            key = (track_a_id, track_b_id)
            record = {
                'track_a_id': track_a_id,
                'track_b_id': track_b_id,
                'cost': cost_matrix[i, j]
            }
            
            if key in cost_details:
                d = cost_details[key]
                record.update({
                    'mean_position_distance': d.get('mean_position_distance'),
                    'median_position_distance': d.get('median_position_distance'),
                    'mean_velocity_difference': d.get('mean_velocity_difference'),
                    'mean_heading_difference': d.get('mean_heading_difference'),
                    'mean_mahalanobis_distance': d.get('mean_mahalanobis_distance'),
                    'temporal_overlap_ratio': d.get('temporal_overlap_ratio'),
                    'gate_pass_ratio': d.get('gate_pass_ratio'),
                    'overall_pass': d.get('overall_pass', False)
                })
            
            cost_matrix_data.append(record)
    
    cost_matrix_df = pd.DataFrame(cost_matrix_data)
    cost_matrix_df.to_csv(Path(output_dir) / 'association_cost_matrix.csv', index=False)
    
    # 保存关联结果
    associations_df = pd.DataFrame(valid_associations)
    if len(associations_df) > 0:
        associations_df.to_csv(Path(output_dir) / 'association_results.csv', index=False)
    
    return cost_matrix_df, associations_df


def main():
    """命令行入口"""
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description='Run track-to-track association')
    parser.add_argument('--aligned-pairs', type=str, required=True,
                       help='Path to aligned track pairs CSV')
    parser.add_argument('--output', type=str, required=True,
                       help='Output directory')
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG,
                       help='Config file')
    
    args = parser.parse_args()
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    Path(args.output).mkdir(parents=True, exist_ok=True)
    
    run_track_association(args.aligned_pairs, args.output, config)


if __name__ == '__main__':
    main()
