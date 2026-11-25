"""
Regression Benchmark Metrics
回归测试指标计算器

核心指标（按你提出的 9 维度 + 1 个补充）：
1.  Association Precision
2.  Association Recall
3.  ID Switches
4.  False Associations
5.  Missed Associations
6.  Global Fragmentations
7.  Global Track Purity
8.  Global Track Recall (补充：跨声纳 Local Track 是否归入同一 Global Track)
9.  Reassociation Success
10. Confirmation Time

注意：
- 当前阶段是 *Track-to-Track Association*，不是 Track Fusion
- 评价的是 Global ID 稳定性，而不是位置估计精度
- Association-level 指标衡量 Association Manager 的关联质量
- Global-level 指标衡量 Global Track Manager 的整合质量
"""
import numpy as np
import pandas as pd
import json
from typing import Dict, List, Optional
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))


def compute_association_precision_recall(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str],
    confirmed_pairs: List[tuple],
    associations_history_df: pd.DataFrame,
    tracks_all_df: pd.DataFrame
) -> Dict[str, float]:
    """
    计算 Association Precision 和 Recall（Association Manager 层面）

    定义：
    - True Positive (TP): 在 GT 中确实同属一个目标的一对 Local Track 被当前 Association Manager 确认关联
    - False Positive (FP): 实际上不是同一目标但被关联了
    - False Negative (FN): 实际上是同一目标但 Association Manager 没确认关联
    """
    if len(local_to_global_df) == 0:
        return {
            'precision': 0.0,
            'recall': 0.0,
            'tp': 0,
            'fp': 0,
            'fn': 0
        }

    # 收集所有 GT 同源对
    target_to_locals: Dict[str, List[str]] = {}
    for local_id, gt_target in gt_mapping.items():
        target_to_locals.setdefault(gt_target, []).append(local_id)

    gt_pairs = set()
    for target, locals_ in target_to_locals.items():
        for i in range(len(locals_)):
            for j in range(i + 1, len(locals_)):
                gt_pairs.add(tuple(sorted([locals_[i], locals_[j]])))

    actual_pairs = set()
    for ta, tb in confirmed_pairs:
        actual_pairs.add(tuple(sorted([ta, tb])))

    tp = len(gt_pairs & actual_pairs)
    fp = len(actual_pairs - gt_pairs)
    fn = len(gt_pairs - actual_pairs)

    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)

    return {
        'precision': precision,
        'recall': recall,
        'tp': tp,
        'fp': fp,
        'fn': fn
    }


def count_id_switches(local_to_global_df: pd.DataFrame,
                       gt_mapping: Dict[str, str]) -> int:
    """
    计数 ID Switch（Global-level）

    定义：如果一个 GLOBAL_ID 下包含的 Local Track 来自多个不同的 GT target，
    则记为一次 ID Switch。
    """
    if len(local_to_global_df) == 0 or 'gt_target' not in local_to_global_df.columns:
        return 0

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

    switches = 0
    for gid, group in merged.groupby('global_id'):
        targets = set(group['gt_target'].dropna())
        if len(targets) > 1:
            switches += 1

    return switches


def count_false_and_missed_associations(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str]
) -> Dict[str, int]:
    """
    计数 False Associations 和 Missed Associations（Global-level）

    False Association: 一个 Global Track 包含了本不属于同一目标的 Local Track
    Missed Association: 一个 GT target 被分散到多个 Global Track
    """
    false_assoc = 0
    missed_assoc = 0

    if len(local_to_global_df) == 0:
        return {'false_associations': 0, 'missed_associations': 0}

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

    for gid, group in merged.groupby('global_id'):
        targets = set(group['gt_target'].dropna())
        if len(targets) > 1:
            false_assoc += 1

    for gt_target, group in merged.dropna(subset=['gt_target']).groupby('gt_target'):
        gids = set(group['global_id'])
        if len(gids) > 1:
            missed_assoc += 1

    return {
        'false_associations': false_assoc,
        'missed_associations': missed_assoc
    }


def count_global_fragmentations(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str]
) -> int:
    """Global Fragmentations：一个 GT target 跨越多个 Global Track 就算一次 fragmentation"""
    if len(local_to_global_df) == 0:
        return 0

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

    fragmentation = 0
    for gt_target, group in merged.dropna(subset=['gt_target']).groupby('gt_target'):
        if group['global_id'].nunique() > 1:
            fragmentation += 1

    return fragmentation


def compute_global_track_purity(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str]
) -> float:
    """
    Global Track Purity：所有 Global Track 中，纯 Global Track（仅对应一个 GT target）的比例
    purity = (纯 Global Track 数) / (Global Track 总数)
    """
    if len(local_to_global_df) == 0:
        return 1.0

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

    total_global = merged['global_id'].nunique()
    pure_global = 0

    for gid, group in merged.groupby('global_id'):
        targets = set(group['gt_target'].dropna())
        if len(targets) <= 1:
            pure_global += 1

    return pure_global / max(1, total_global)


def compute_reassociation_success(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str],
    associations_history_df: pd.DataFrame,
    tracks_all_df: pd.DataFrame = None
) -> Optional[float]:
    """
    Reassociation Success Rate

    定义：发生过失配或断轨的 Local Track，最终是否被正确归入原 Global Track

    "应当发生 Reassociation" 判定（满足任一）：
    1. 该 Local Track 在历史上有过 COASTING/LOST 状态
    2. 该 Local Track 与同 GT target 的另一个 Local Track 在时间上有断层（断轨）

    成功条件：所有同 GT target 的 Local Track 最终都在同一 Global Track
    """
    if len(associations_history_df) == 0 or len(local_to_global_df) == 0:
        return None

    status_col = None
    for col_name in ['status_after', 'status']:
        if col_name in associations_history_df.columns:
            status_col = col_name
            break
    if status_col is None:
        return None

    history = associations_history_df.copy()

    # 条件 1: 检测每个 track 是否曾经"失联"（COASTING/LOST）
    lost_tracks = set()
    for tid, grp in history.groupby('track_id'):
        statuses = grp[status_col].astype(str).tolist()
        if 'LOST' in statuses or 'COASTING' in statuses:
            lost_tracks.add(tid)

    # 条件 2: 基于时间间隔检测断轨
    if tracks_all_df is not None and len(tracks_all_df) > 0:
        merged = local_to_global_df.copy()
        merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

        for gt_target, grp in merged.dropna(subset=['gt_target']).groupby('gt_target'):
            if len(grp) < 2:
                continue
            local_ids = grp['local_track_id'].tolist()
            intervals = []
            for lid in local_ids:
                track_data = tracks_all_df[tracks_all_df['track_id'] == lid]
                if len(track_data) > 0:
                    intervals.append((track_data['timestamp'].min(),
                                       track_data['timestamp'].max(), lid))
            intervals.sort()
            for i in range(len(intervals) - 1):
                end_i = intervals[i][1]
                start_next = intervals[i + 1][0]
                if start_next - end_i > 2.0:  # 间隔超过 2 秒认为断轨
                    lost_tracks.add(intervals[i + 1][2])

    if not lost_tracks:
        return None

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)

    success = 0
    target_total = 0
    for gt_target, grp in merged.dropna(subset=['gt_target']).groupby('gt_target'):
        local_tracks = set(grp['local_track_id'])
        if local_tracks & lost_tracks:
            target_total += 1
            if grp['global_id'].nunique() == 1:
                success += 1

    if target_total == 0:
        return None

    return success / target_total


def compute_confirmation_time(
    associations_history_df: pd.DataFrame,
    tracks_all_df: pd.DataFrame,
    scenario_start_time: float
) -> Optional[float]:
    """
    Confirmation Time：每个 Local Track 从首次出现到首次达到 CONFIRMED 状态的平均时间
    """
    if len(associations_history_df) == 0:
        return None

    status_col = None
    for col_name in ['status_after', 'status']:
        if col_name in associations_history_df.columns:
            status_col = col_name
            break
    if status_col is None:
        return None
    if 'track_id' not in associations_history_df.columns:
        return None

    confirmation_times = []

    for tid, grp in associations_history_df.groupby('track_id'):
        grp = grp.sort_values('timestamp')
        first_time = grp['timestamp'].min()

        confirmed_time = None
        for _, row in grp.iterrows():
            if str(row[status_col]) == 'CONFIRMED':
                confirmed_time = row['timestamp']
                break

        if confirmed_time is not None:
            confirmation_times.append(confirmed_time - first_time)

    if not confirmation_times:
        return None

    return float(np.mean(confirmation_times))


def compute_global_track_recall(
    local_to_global_df: pd.DataFrame,
    gt_mapping: Dict[str, str]
) -> float:
    """
    Global Track Recall：所有同 GT 的 Local Track 是否归入同一 Global Track

    与 Association Recall 不同：
    - Association Recall 衡量 Association Manager 的关联质量（短窗口）
    - Global Track Recall 衡量 Global Track Manager 的整合质量（全局一致）

    这是"Global Track Manager 真正发挥作用"的最终证据：
    即使 Association Manager 因为保护 CONFIRMED 而拒绝切换，
    Global Track Manager 仍能把新 track 归入正确的 Global Track。
    """
    if len(local_to_global_df) == 0:
        return 0.0

    merged = local_to_global_df.copy()
    merged['gt_target'] = merged['local_track_id'].map(gt_mapping)
    merged_valid = merged.dropna(subset=['gt_target'])

    if len(merged_valid) == 0:
        return 1.0

    targets_unified = (merged_valid.groupby('gt_target')['global_id']
                      .nunique() == 1).sum()
    return targets_unified / merged_valid['gt_target'].nunique()


def compute_all_metrics(
    output_dir: str,
    gt_mapping: Dict[str, str],
    scenario_start_time: float = 0.0
) -> Dict:
    """
    从实验输出目录计算全部指标
    """
    output_path = Path(output_dir)

    local_to_global_df = pd.read_csv(output_path / 'local_to_global.csv') if (
        output_path / 'local_to_global.csv').exists() else pd.DataFrame()
    tracks_all_df = pd.read_csv(output_path / 'all_local_tracks.csv') if (
        output_path / 'all_local_tracks.csv').exists() else pd.DataFrame()
    assoc_history_df = pd.read_csv(output_path / 'association_history.csv') if (
        output_path / 'association_history.csv').exists() else pd.DataFrame()

    # 构造 confirmed_pairs
    summary_path = output_path / 'association_summary.csv'
    confirmed_pairs = []
    if summary_path.exists():
        summary = pd.read_csv(summary_path)
        if 'current_partner' in summary.columns and 'track_id' in summary.columns:
            confirmed = summary[summary['current_status'] == 'CONFIRMED']
            for _, row in confirmed.iterrows():
                partner = row['current_partner']
                if isinstance(partner, str) and partner:
                    confirmed_pairs.append(tuple(sorted([row['track_id'], partner])))

    # 1-2: Precision / Recall (Association-level)
    prec_recall = compute_association_precision_recall(
        local_to_global_df, gt_mapping, confirmed_pairs, assoc_history_df, tracks_all_df
    )

    # 3: ID Switches (Global-level)
    id_switches = count_id_switches(local_to_global_df, gt_mapping)

    # 4-5: False / Missed Associations (Global-level)
    false_missed = count_false_and_missed_associations(local_to_global_df, gt_mapping)

    # 6: Global Fragmentations (Global-level)
    global_frag = count_global_fragmentations(local_to_global_df, gt_mapping)

    # 7: Global Track Purity (Global-level)
    purity = compute_global_track_purity(local_to_global_df, gt_mapping)

    # 8 (新): Global Track Recall (Global-level)
    global_recall = compute_global_track_recall(local_to_global_df, gt_mapping)

    # 9: Reassociation Success (Global-level)
    reassoc_success = compute_reassociation_success(
        local_to_global_df, gt_mapping, assoc_history_df, tracks_all_df
    )

    # 10: Confirmation Time
    conf_time = compute_confirmation_time(
        assoc_history_df, tracks_all_df, scenario_start_time
    )

    return {
        'association_precision': prec_recall['precision'],
        'association_recall': prec_recall['recall'],
        'precision_tp_fp_fn': f"{prec_recall['tp']}/{prec_recall['fp']}/{prec_recall['fn']}",
        'id_switches': id_switches,
        'false_associations': false_missed['false_associations'],
        'missed_associations': false_missed['missed_associations'],
        'global_fragmentations': global_frag,
        'global_track_purity': purity,
        'global_track_recall': global_recall,
        'reassociation_success': reassoc_success,
        'confirmation_time_s': conf_time,
        'num_global_tracks': int(local_to_global_df['global_id'].nunique()) if len(local_to_global_df) > 0 else 0,
        'num_gt_targets': len(set(gt_mapping.values())),
        'num_local_tracks': len(local_to_global_df),
    }


def metrics_to_table_row(metrics: Dict) -> Dict[str, str]:
    """将指标 dict 转为可打印的表格行"""
    def fmt(v, suffix='', digits=3):
        if v is None:
            return 'N/A'
        if isinstance(v, float):
            if digits == 0:
                return f"{v:.0f}{suffix}"
            return f"{v:.{digits}f}{suffix}"
        return f"{v}{suffix}"

    return {
        'Association Precision': fmt(metrics['association_precision'], digits=3),
        'Association Recall': fmt(metrics['association_recall'], digits=3),
        'ID Switches': fmt(metrics['id_switches'], digits=0),
        'False Associations': fmt(metrics['false_associations'], digits=0),
        'Missed Associations': fmt(metrics['missed_associations'], digits=0),
        'Global Fragmentations': fmt(metrics['global_fragmentations'], digits=0),
        'Global Track Purity': fmt(metrics['global_track_purity'], digits=3),
        'Global Track Recall': fmt(metrics.get('global_track_recall', 0.0), digits=3),
        'Reassociation Success': fmt(metrics['reassociation_success'], digits=3),
        'Confirmation Time (s)': fmt(metrics['confirmation_time_s'], digits=2),
    }