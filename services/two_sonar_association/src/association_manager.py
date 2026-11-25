"""
Track-Centric Association Manager
航迹为中心的关联管理器

支持多目标航迹关联，每个 Local Track 在任何时刻最多只有一个 active partner
支持跨时间解除/重新关联，支持 UNASSOCIATED、TENTATIVE、CONFIRMED、COASTING、LOST 状态
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Set
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
from two_sonar_paths import DEFAULT_CONFIG


class AssociationStatus(Enum):
    """关联状态枚举"""
    UNASSOCIATED = "UNASSOCIATED"
    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    COASTING = "COASTING"
    LOST = "LOST"


@dataclass
class TrackAssociationState:
    """
    单个 Local Track 的关联状态
    """
    track_id: str
    sensor_id: str
    current_partner: Optional[str] = None
    current_partner_sensor: Optional[str] = None
    current_status: AssociationStatus = AssociationStatus.UNASSOCIATED
    
    consecutive_hits: int = 0
    consecutive_misses: int = 0
    total_updates: int = 0
    
    partner_history: List[Tuple[str, float, AssociationStatus]] = field(default_factory=list)
    current_cost: float = np.nan
    
    def add_partner_history(self, partner_id: str, timestamp: float, status: AssociationStatus):
        self.partner_history.append((partner_id, timestamp, status))
    
    def clear_partner(self):
        self.current_partner = None
        self.current_partner_sensor = None
        self.current_status = AssociationStatus.UNASSOCIATED
        self.consecutive_hits = 0
        self.consecutive_misses = 0


class TrackCentricAssociationManager:
    """
    Track-Centric 关联管理器
    
    核心约束：
    1. 同一时刻一个 Local Track 最多只有一个 active confirmed partner
    2. 支持跨时间解除/重新关联（reassociation）
    3. 每个窗口执行 N×M Cost Matrix + Hungarian 匹配
    4. 但匹配结果根据历史状态决定能否被确认
    """
    
    def __init__(self, config: Dict):
        self.confirmation_hits = config.get('confirmation_hits', 4)
        self.coasting_frames = config.get('coasting_frames', 5)
        self.lost_hits = config.get('lost_hits', 10)
        
        self.max_cost_for_tentative = config.get('max_cost_for_tentative', 0.7)
        self.max_cost_for_confirmation = config.get('max_cost_for_confirmation', 0.5)
        
        self.allow_reassociation = config.get('allow_reassociation', True)
        self.min_hits_for_reassociation = config.get('min_hits_for_reassociation', 2)
        
        self.track_states: Dict[str, TrackAssociationState] = {}
        self.history: List[Dict] = []
        self.current_pairings: Dict[Tuple[str, str], Dict] = {}
    
    def register_track(self, track_id: str, sensor_id: str):
        if track_id not in self.track_states:
            self.track_states[track_id] = TrackAssociationState(
                track_id=track_id,
                sensor_id=sensor_id
            )
    
    def process_time_window(self, timestamp: float,
                             window_pairs: pd.DataFrame,
                             cost_threshold: float = 0.5):
        """
        处理单个时间窗口的关联
        """
        # 收集本窗口涉及的所有 tracks
        all_tracks_in_window = set()
        for _, r in window_pairs.iterrows():
            all_tracks_in_window.add(r['track_a_id'])
            all_tracks_in_window.add(r['track_b_id'])
        
        # 获取 Hungarian 匹配结果
        hungarian_assignments = self._hungarian_match_window(window_pairs, cost_threshold)
        
        # 转换为本窗口的关联映射
        window_match_map = {}
        for ta, tb in hungarian_assignments:
            window_match_map[ta] = tb
            window_match_map[tb] = ta
        
        # 更新每个 track 的状态
        for track_id in all_tracks_in_window:
            self._update_track_in_window(
                timestamp, track_id, window_match_map, window_pairs
            )
        
        # 更新当前确认关联
        self._update_current_confirmed_pairings(timestamp)
    
    def _hungarian_match_window(self, window_pairs: pd.DataFrame, 
                                  cost_threshold: float) -> List[Tuple[str, str]]:
        from scipy.optimize import linear_sum_assignment
        
        if len(window_pairs) == 0:
            return []
        
        track_ids_a = sorted(window_pairs['track_a_id'].unique())
        track_ids_b = sorted(window_pairs['track_b_id'].unique())
        
        if len(track_ids_a) == 0 or len(track_ids_b) == 0:
            return []
        
        n_a = len(track_ids_a)
        n_b = len(track_ids_b)
        cost_matrix = np.full((n_a, n_b), 1.0)
        
        for _, row in window_pairs.iterrows():
            ta = row['track_a_id']
            tb = row['track_b_id']
            cost = row.get('cost', 1.0)
            if not pd.isna(cost) and ta in track_ids_a and tb in track_ids_b:
                i = track_ids_a.index(ta)
                j = track_ids_b.index(tb)
                cost_matrix[i, j] = min(cost_matrix[i, j], cost)
        
        try:
            row_ind, col_ind = linear_sum_assignment(cost_matrix)
            assignments = []
            for i, j in zip(row_ind, col_ind):
                if i < n_a and j < n_b:
                    cost = cost_matrix[i, j]
                    if cost < cost_threshold:
                        assignments.append((track_ids_a[i], track_ids_b[j]))
            return assignments
        except Exception:
            return []
    
    def _update_track_in_window(self, timestamp: float, track_id: str,
                                  window_match_map: Dict[str, str],
                                  window_pairs: pd.DataFrame):
        """更新单个 track 在当前窗口的状态"""
        
        if track_id not in self.track_states:
            sensor_id = self._infer_sensor_id(track_id)
            self.register_track(track_id, sensor_id)
        
        state = self.track_states[track_id]
        state.total_updates += 1
        
        # 获取本窗口该 track 的代价信息
        track_cost = None
        pos_dist = None
        partner_id = None
        
        if track_id in window_match_map:
            partner_id = window_match_map[track_id]
            for _, r in window_pairs.iterrows():
                if (r['track_a_id'] == track_id and r['track_b_id'] == partner_id) or \
                   (r['track_b_id'] == track_id and r['track_a_id'] == partner_id):
                    track_cost = r.get('cost', np.nan)
                    pos_dist = r.get('position_distance', np.nan)
                    break
        
        history_record = {
            'timestamp': timestamp,
            'track_id': track_id,
            'matched_partner': partner_id,
            'match_cost': track_cost,
            'position_distance': pos_dist,
            'status_before': state.current_status.value,
            'partner_before': state.current_partner
        }
        
        if partner_id is not None and track_cost is not None and not np.isnan(track_cost):
            # 本窗口有匹配
            self._handle_match(state, timestamp, partner_id, track_cost, history_record)
        else:
            # 本窗口无匹配
            self._handle_no_match(state, timestamp, history_record)
    
    def _handle_match(self, state: TrackAssociationState, timestamp: float,
                      partner_id: str, track_cost: float,
                      history_record: Dict):
        """处理窗口中有匹配的情况"""
        state.consecutive_misses = 0
        state.current_cost = track_cost
        
        if state.current_partner is None:
            # 首次匹配
            state.current_partner = partner_id
            state.current_partner_sensor = self._infer_sensor_id(partner_id)
            state.consecutive_hits = 1
            state.current_status = AssociationStatus.TENTATIVE
            state.add_partner_history(partner_id, timestamp, AssociationStatus.TENTATIVE)
            
        elif state.current_partner == partner_id:
            # 继续同一匹配
            state.consecutive_hits += 1
            
            if state.current_status == AssociationStatus.TENTATIVE:
                if (state.consecutive_hits >= self.confirmation_hits and
                    state.current_cost < self.max_cost_for_confirmation):
                    state.current_status = AssociationStatus.CONFIRMED
                    state.add_partner_history(partner_id, timestamp, AssociationStatus.CONFIRMED)
            
            elif state.current_status == AssociationStatus.COASTING:
                state.current_status = AssociationStatus.TENTATIVE
                state.consecutive_hits = 1
                state.add_partner_history(partner_id, timestamp, AssociationStatus.TENTATIVE)
            
            elif state.current_status == AssociationStatus.LOST:
                if state.consecutive_hits >= self.min_hits_for_reassociation:
                    state.current_status = AssociationStatus.TENTATIVE
                    state.add_partner_history(partner_id, timestamp, AssociationStatus.TENTATIVE)
            
            # CONFIRMED: 保持
            
        else:
            # partner 改变 - reassociation
            old_partner = state.current_partner
            old_status = state.current_status
            
            if not self.allow_reassociation:
                history_record['event'] = 'REJECTED_SWITCH'
                state.consecutive_misses += 1
                history_record['status_after'] = state.current_status.value
                self.history.append(history_record)
                return
            
            if state.current_status == AssociationStatus.CONFIRMED:
                # CONFIRMED 不切换
                history_record['event'] = 'REJECTED_CONFIRMED_SWITCH'
                state.consecutive_misses += 1
                history_record['status_after'] = state.current_status.value
                self.history.append(history_record)
                return
            
            # 允许切换
            state.add_partner_history(old_partner, timestamp, old_status)
            state.current_partner = partner_id
            state.current_partner_sensor = self._infer_sensor_id(partner_id)
            state.consecutive_hits = 1
            state.current_cost = track_cost
            state.current_status = AssociationStatus.TENTATIVE
            state.add_partner_history(partner_id, timestamp, AssociationStatus.TENTATIVE)
            history_record['event'] = 'REASSOCIATION'
        
        history_record['status_after'] = state.current_status.value
        history_record['consecutive_hits'] = state.consecutive_hits
        self.history.append(history_record)
    
    def _handle_no_match(self, state: TrackAssociationState, timestamp: float,
                          history_record: Dict):
        """处理窗口中没有匹配的情况"""
        state.current_cost = np.nan
        
        if state.current_status == AssociationStatus.TENTATIVE:
            state.consecutive_misses += 1
            if state.consecutive_misses >= self.coasting_frames:
                state.current_status = AssociationStatus.COASTING
                state.consecutive_misses = 0
                history_record['status_after'] = state.current_status.value
                self.history.append(history_record)
        
        elif state.current_status == AssociationStatus.CONFIRMED:
            state.consecutive_misses += 1
            if state.consecutive_misses >= self.coasting_frames:
                state.current_status = AssociationStatus.COASTING
                state.consecutive_misses = 0
                history_record['status_after'] = state.current_status.value
                self.history.append(history_record)
        
        elif state.current_status == AssociationStatus.COASTING:
            state.consecutive_misses += 1
            if state.consecutive_misses >= self.lost_hits:
                state.current_status = AssociationStatus.LOST
                state.add_partner_history(
                    state.current_partner, timestamp, AssociationStatus.LOST
                )
                state.current_partner = None
                state.consecutive_misses = 0
                history_record['status_after'] = state.current_status.value
                self.history.append(history_record)
        
        elif state.current_status == AssociationStatus.LOST:
            state.consecutive_misses += 1
        
        # UNASSOCIATED: 不做任何事
    
    def _update_current_confirmed_pairings(self, timestamp: float):
        """更新当前所有 confirmed 关联对"""
        self.current_pairings = {}
        
        for state in self.track_states.values():
            if (state.current_status == AssociationStatus.CONFIRMED and 
                state.current_partner):
                partner_state = self.track_states.get(state.current_partner)
                if (partner_state and 
                    partner_state.current_partner == state.track_id and
                    partner_state.current_status == AssociationStatus.CONFIRMED):
                    pair = tuple(sorted([state.track_id, state.current_partner]))
                    if pair not in self.current_pairings:
                        self.current_pairings[pair] = {
                            'timestamp': timestamp,
                            'status': 'CONFIRMED',
                            'cost': state.current_cost
                        }
    
    def _infer_sensor_id(self, track_id: str) -> str:
        if track_id.startswith('A_'):
            return 'SONAR_A'
        elif track_id.startswith('B_'):
            return 'SONAR_B'
        return 'UNKNOWN'
    
    def get_confirmed_associations(self) -> List[Tuple[str, str]]:
        """获取所有当前已确认的关联对"""
        confirmed = []
        for pair, info in self.current_pairings.items():
            if info['status'] == 'CONFIRMED':
                confirmed.append(pair)
        return confirmed
    
    def get_all_pairings_at_window(self) -> List[Dict]:
        """获取所有当前窗口的配对信息"""
        pairings = []
        seen = set()
        
        for state in self.track_states.values():
            if state.current_partner:
                pair = tuple(sorted([state.track_id, state.current_partner]))
                if pair not in seen:
                    seen.add(pair)
                    pairings.append({
                        'track_a_id': state.track_id,
                        'track_b_id': state.current_partner,
                        'status': state.current_status.value,
                        'cost': state.current_cost,
                        'consecutive_hits': state.consecutive_hits,
                        'consecutive_misses': state.consecutive_misses
                    })
        
        return pairings
    
    def get_history_dataframe(self) -> pd.DataFrame:
        if not self.history:
            return pd.DataFrame()
        return pd.DataFrame(self.history)
    
    def get_associations_summary(self) -> pd.DataFrame:
        records = []
        for state in self.track_states.values():
            record = {
                'track_id': state.track_id,
                'sensor_id': state.sensor_id,
                'current_partner': state.current_partner,
                'current_status': state.current_status.value,
                'consecutive_hits': state.consecutive_hits,
                'consecutive_misses': state.consecutive_misses,
                'total_updates': state.total_updates,
                'current_cost': state.current_cost,
                'num_partner_changes': len(state.partner_history)
            }
            records.append(record)
        return pd.DataFrame(records)
    
    def get_reassociation_log(self) -> pd.DataFrame:
        records = []
        for state in self.track_states.values():
            prev_partner = None
            prev_status = None
            for partner_id, timestamp, status in state.partner_history:
                if prev_partner is not None and partner_id != prev_partner:
                    records.append({
                        'track_id': state.track_id,
                        'timestamp': timestamp,
                        'old_partner': prev_partner,
                        'new_partner': partner_id,
                        'old_status': prev_status.value if prev_status else None,
                        'new_status': status.value,
                        'event_type': 'REASSOCIATION'
                    })
                prev_partner = partner_id
                prev_status = status
        
        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records)


def process_aligned_pairs_with_association(aligned_pairs: pd.DataFrame,
                                            config: Dict) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    manager = TrackCentricAssociationManager(config['association'])
    
    timestamps = sorted(aligned_pairs['timestamp'].unique())
    cost_threshold = config['association'].get('max_cost_for_tentative', 0.7)
    
    for timestamp in timestamps:
        window_data = aligned_pairs[aligned_pairs['timestamp'] == timestamp].copy()
        manager.process_time_window(timestamp, window_data, cost_threshold)
    
    return (manager.get_history_dataframe(),
            manager.get_associations_summary(),
            manager.get_reassociation_log())


def main():
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description='Run track-centric association')
    parser.add_argument('--aligned-pairs', type=str, required=True)
    parser.add_argument('--output', type=str, required=True)
    parser.add_argument('--config', type=str,
                       default=DEFAULT_CONFIG)
    
    args = parser.parse_args()
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    aligned_pairs = pd.read_csv(args.aligned_pairs)
    
    history_df, summary_df, reassociation_df = process_aligned_pairs_with_association(
        aligned_pairs, config
    )
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    history_df.to_csv(output_dir / 'association_history.csv', index=False)
    summary_df.to_csv(output_dir / 'association_summary.csv', index=False)
    if len(reassociation_df) > 0:
        reassociation_df.to_csv(output_dir / 'reassociation_log.csv', index=False)


if __name__ == '__main__':
    main()