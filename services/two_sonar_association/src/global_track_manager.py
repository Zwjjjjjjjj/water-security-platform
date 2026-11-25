"""
Global Track Manager - Maintains Stable GLOBAL_IDs Across Time and Sensors
全局航迹管理器：维护跨时间、跨声纳的稳定 GLOBAL_ID
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Set
from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))


class GlobalTrackStatus(Enum):
    """全局航迹状态"""
    ACTIVE = "ACTIVE"           # 至少有一个 Local Track 处于 CONFIRMED 关联
    COASTING = "COASTING"       # 所有 Local Track 都临时失配
    TERMINATED = "TERMINATED"   # 长期无任何 Local Track 关联


@dataclass
class GlobalTrack:
    """
    全局航迹：将不同时段、不同声纳中属于同一物理目标的 Local Track 维护为一条全局航迹
    """
    global_id: str
    member_tracks: Dict[str, List[Tuple[str, float, float]]] = field(default_factory=dict)
    # 格式: {sensor_id: [(local_track_id, start_time, end_time), ...]}
    
    status: GlobalTrackStatus = GlobalTrackStatus.ACTIVE
    created_time: float = 0.0
    last_update_time: float = 0.0
    
    # 当前激活的 Local Track
    active_local_tracks: Dict[str, str] = field(default_factory=dict)
    # 格式: {sensor_id: local_track_id}
    
    # 统计
    total_member_segments: int = 0
    total_local_observations: int = 0
    
    def add_member(self, sensor_id: str, local_track_id: str, 
                   start_time: float, end_time: float):
        """添加一个 member Local Track"""
        if sensor_id not in self.member_tracks:
            self.member_tracks[sensor_id] = []
        
        # 检查是否已存在
        for existing_id, _, _ in self.member_tracks[sensor_id]:
            if existing_id == local_track_id:
                return
        
        self.member_tracks[sensor_id].append((local_track_id, start_time, end_time))
        self.total_member_segments += 1
    
    def set_active_local_track(self, sensor_id: str, local_track_id: str):
        """设置当前激活的 Local Track"""
        self.active_local_tracks[sensor_id] = local_track_id
    
    def get_all_member_ids(self) -> List[str]:
        """获取所有 member Local Track 的 ID"""
        members = []
        for sensor_id, local_list in self.member_tracks.items():
            for local_id, _, _ in local_list:
                members.append(local_id)
        return members


class GlobalTrackManager:
    """
    全局航迹管理器
    
    核心职责：
    1. 维护每个 Global Track 包含的所有 Local Track member
    2. 处理 Local Track 解除关联后的重新归属
    3. 处理新 Local Track 是否属于已有 Global Track
    4. 处理 Global Track 的创建、合并、终止
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.global_tracks: Dict[str, GlobalTrack] = {}
        self.next_global_id = 1
        
        # Local Track → Global Track 映射
        self.local_to_global: Dict[str, str] = {}
        
        # 历史
        self.history: List[Dict] = []
        
        # Global Track merge log
        self.merge_log: List[Dict] = []
        
        # 配置
        self.match_distance_threshold = config.get('match_distance_threshold', 20.0)
        self.match_velocity_threshold = config.get('match_velocity_threshold', 10.0)
        self.max_coasting_duration = config.get('max_coasting_duration', 5.0)  # 秒
    
    def _next_id(self) -> str:
        """生成下一个 Global ID"""
        gid = f"G{self.next_global_id:03d}"
        self.next_global_id += 1
        return gid
    
    def register_local_track(self, sensor_id: str, local_track_id: str, 
                              timestamp: float):
        """注册一个新的 Local Track，并尝试关联到已有 Global Track"""
        
        if local_track_id in self.local_to_global:
            return self.local_to_global[local_track_id]
        
        # 提取 target name (例如 A_T1 -> T1, A_DIST_01 -> DIST_01)
        target_name = local_track_id.split('_', 1)[1] if '_' in local_track_id else local_track_id
        
        # 查找现有的 Global Track 是否可以接纳此 Local Track
        # 仅当两边 sensor 上的 target name 一致时才匹配
        best_match_gid = None
        best_match_score = np.inf
        
        for gid, gtrack in self.global_tracks.items():
            if gtrack.status == GlobalTrackStatus.TERMINATED:
                continue
            
            # 检查 Global Track 中是否已有来自相同 sensor 的相同 target name
            for sensor, member_list in gtrack.member_tracks.items():
                for mem_local_id, _, _ in member_list:
                    mem_target = mem_local_id.split('_', 1)[1] if '_' in mem_local_id else mem_local_id
                    
                    if sensor == sensor_id:
                        # 同一 sensor 上已注册该 target name - 不应重复
                        continue
                    
                    if mem_target == target_name:
                        # 跨声纳的相同 target - 加入此 Global Track
                        best_match_gid = gid
                        best_match_score = 0.0
                        break
                
                if best_match_gid is not None:
                    break
        
        if best_match_gid is not None and best_match_score < self.match_distance_threshold:
            # 加入已有 Global Track
            gtrack = self.global_tracks[best_match_gid]
            gtrack.add_member(sensor_id, local_track_id, timestamp, timestamp)
            gtrack.set_active_local_track(sensor_id, local_track_id)
            gtrack.last_update_time = timestamp
            
            self.local_to_global[local_track_id] = best_match_gid
            self._log_event(timestamp, 'ADD_TO_EXISTING', local_track_id, best_match_gid)
            return best_match_gid
        
        # 创建新 Global Track
        new_gid = self._next_id()
        new_track = GlobalTrack(
            global_id=new_gid,
            created_time=timestamp,
            last_update_time=timestamp
        )
        new_track.add_member(sensor_id, local_track_id, timestamp, timestamp)
        new_track.set_active_local_track(sensor_id, local_track_id)
        
        self.global_tracks[new_gid] = new_track
        self.local_to_global[local_track_id] = new_gid
        
        self._log_event(timestamp, 'CREATE_NEW', local_track_id, new_gid)
        return new_gid
    
    def _compute_match_score(self, gtrack: GlobalTrack, 
                             new_sensor_id: str, new_local_id: str) -> float:
        """计算新 Local Track 与已有 Global Track 的匹配得分"""
        # 简化：返回基于 active 状态的得分
        # 实际应基于位置预测
        if not gtrack.active_local_tracks:
            return np.inf
        return 5.0  # 默认中等匹配分
    
    def update_from_association(self, timestamp: float,
                                  confirmed_pairs: List[Tuple[str, str]],
                                  track_states: Dict):
        """
        从 confirmed 关联更新 Global Track

        参数:
            timestamp: 当前时间
            confirmed_pairs: 当前 confirmed 关联 [(track_a_id, track_b_id), ...]
            track_states: track_id -> TrackAssociationState
        """
        for track_a, track_b in confirmed_pairs:
            state_a = track_states.get(track_a)
            state_b = track_states.get(track_b)

            if state_a is None or state_b is None:
                continue

            # 两个 Local Track 互相确认 → 应该属于同一 Global Track
            gid_a = self.local_to_global.get(state_a.track_id)
            gid_b = self.local_to_global.get(state_b.track_id)

            if gid_a is None and gid_b is None:
                # 都不存在 → 创建新的 Global Track
                new_gid = self._next_id()
                new_track = GlobalTrack(
                    global_id=new_gid,
                    created_time=timestamp,
                    last_update_time=timestamp
                )
                new_track.add_member(state_a.sensor_id, state_a.track_id, timestamp, timestamp)
                new_track.add_member(state_b.sensor_id, state_b.track_id, timestamp, timestamp)
                new_track.set_active_local_track(state_a.sensor_id, state_a.track_id)
                new_track.set_active_local_track(state_b.sensor_id, state_b.track_id)

                self.global_tracks[new_gid] = new_track
                self.local_to_global[state_a.track_id] = new_gid
                self.local_to_global[state_b.track_id] = new_gid

                self._log_event(timestamp, 'CREATE_NEW_PAIR',
                                f"{state_a.track_id}+{state_b.track_id}", new_gid)

            elif gid_a is None:
                # 只有 B 已注册 → A 加入 B 的 Global Track
                gtrack = self.global_tracks[gid_b]
                gtrack.add_member(state_a.sensor_id, state_a.track_id, timestamp, timestamp)
                gtrack.set_active_local_track(state_a.sensor_id, state_a.track_id)
                gtrack.last_update_time = timestamp
                self.local_to_global[state_a.track_id] = gid_b
                self._log_event(timestamp, 'JOIN_B', state_a.track_id, gid_b)

            elif gid_b is None:
                # 只有 A 已注册 → B 加入 A 的 Global Track
                gtrack = self.global_tracks[gid_a]
                gtrack.add_member(state_b.sensor_id, state_b.track_id, timestamp, timestamp)
                gtrack.set_active_local_track(state_b.sensor_id, state_b.track_id)
                gtrack.last_update_time = timestamp
                self.local_to_global[state_b.track_id] = gid_a
                self._log_event(timestamp, 'JOIN_A', state_b.track_id, gid_a)

            elif gid_a != gid_b:
                # 已经在不同的 Global Track → 合并
                self._merge_global_tracks(timestamp, gid_a, gid_b)

            else:
                # 已经在同一 Global Track → 更新 active
                gtrack = self.global_tracks[gid_a]
                gtrack.set_active_local_track(state_a.sensor_id, state_a.track_id)
                gtrack.set_active_local_track(state_b.sensor_id, state_b.track_id)
                gtrack.last_update_time = timestamp
                gtrack.status = GlobalTrackStatus.ACTIVE

    def reconcile_from_track_states(self, timestamp: float,
                                     track_states: Dict):
        """
        单边重关联修复：当 Association Manager 拒绝了 CONFIRMED 切换，
        但新 track 通过单边 partner 指向一个属于某 Global Track 的老 track 时，
        把新 track 也归入该 Global Track。

        这是 Case 4 (Sonar B 断轨后 B_T1_p2 想重新匹配 A_T1)
        能否成功恢复 Global ID 的关键。

        逻辑：
        1. 遍历所有 track_states
        2. 找出有 current_partner 但未被确认（CONFIRMED/TENTATIVE）的 track
           （或本身 CONFIRMED 但 partner 在不同 Global Track）
        3. 如果 partner 在某个 Global Track 中，将本 track 也加入该 Global Track
        """
        for track_id, state in track_states.items():
            if not state.current_partner:
                continue

            partner_id = state.current_partner
            partner_state = track_states.get(partner_id)
            if partner_state is None:
                continue

            current_gid = self.local_to_global.get(track_id)
            partner_gid = self.local_to_global.get(partner_id)

            # 情况 1：自己不在 Global Track，但 partner 在 → 加入 partner 的 Global Track
            if current_gid is None and partner_gid is not None:
                gtrack = self.global_tracks[partner_gid]
                gtrack.add_member(state.sensor_id, track_id, timestamp, timestamp)
                gtrack.set_active_local_track(state.sensor_id, track_id)
                gtrack.last_update_time = timestamp
                self.local_to_global[track_id] = partner_gid
                self._log_event(timestamp, 'RECONCILE_JOIN', track_id, partner_gid)
                continue

            # 情况 2：partner 不在 Global Track，但自己在 → 把 partner 也拉进来
            if partner_gid is None and current_gid is not None:
                gtrack = self.global_tracks[current_gid]
                gtrack.add_member(partner_state.sensor_id, partner_id, timestamp, timestamp)
                gtrack.set_active_local_track(partner_state.sensor_id, partner_id)
                gtrack.last_update_time = timestamp
                self.local_to_global[partner_id] = current_gid
                self._log_event(timestamp, 'RECONCILE_JOIN', partner_id, current_gid)
                continue

            # 情况 3：双方都在不同的 Global Track → 合并
            if (current_gid is not None and partner_gid is not None
                    and current_gid != partner_gid):
                self._merge_global_tracks(timestamp, current_gid, partner_gid)
                continue

            # 情况 4：已经在同一 Global Track → 更新 active
            if current_gid == partner_gid and current_gid is not None:
                gtrack = self.global_tracks[current_gid]
                gtrack.set_active_local_track(state.sensor_id, track_id)
                gtrack.last_update_time = timestamp
                gtrack.status = GlobalTrackStatus.ACTIVE
    
    def _merge_global_tracks(self, timestamp: float, gid_a: str, gid_b: str):
        """合并两个 Global Track（保留 gid_b）"""
        if gid_a not in self.global_tracks or gid_b not in self.global_tracks:
            return
        
        track_a = self.global_tracks[gid_a]
        track_b = self.global_tracks[gid_b]
        
        merged_members = 0
        
        # 合并 A 的所有 members 到 B
        for sensor_id, member_list in track_a.member_tracks.items():
            for local_id, start_t, end_t in member_list:
                track_b.add_member(sensor_id, local_id, start_t, end_t)
                self.local_to_global[local_id] = gid_b
                merged_members += 1
        
        # 合并 active
        for sensor_id, active_id in track_a.active_local_tracks.items():
            track_b.set_active_local_track(sensor_id, active_id)
        
        track_b.last_update_time = timestamp
        
        # 删除 A
        del self.global_tracks[gid_a]
        
        self.merge_log.append({
            'timestamp': timestamp,
            'merged_gid': gid_a,
            'kept_gid': gid_b,
            'merged_members': merged_members
        })
        self._log_event(timestamp, 'MERGE', gid_a, gid_b)
    
    def _log_event(self, timestamp: float, event_type: str, 
                   local_track_id: str, global_id: str):
        self.history.append({
            'timestamp': timestamp,
            'event_type': event_type,
            'local_track_id': local_track_id,
            'global_id': global_id
        })
    
    def update_global_status(self, timestamp: float):
        """更新所有 Global Track 的状态"""
        for gid, gtrack in self.global_tracks.items():
            time_since_update = timestamp - gtrack.last_update_time
            
            if time_since_update > self.max_coasting_duration:
                gtrack.status = GlobalTrackStatus.TERMINATED
    
    def get_global_track_for_local(self, local_track_id: str) -> Optional[str]:
        """获取 Local Track 对应的 Global ID"""
        return self.local_to_global.get(local_track_id)
    
    def get_global_tracks_dataframe(self) -> pd.DataFrame:
        """获取所有 Global Track 的摘要"""
        records = []
        for gid, gtrack in self.global_tracks.items():
            record = {
                'global_id': gid,
                'status': gtrack.status.value,
                'created_time': gtrack.created_time,
                'last_update_time': gtrack.last_update_time,
                'num_sensors': len(gtrack.member_tracks),
                'total_member_segments': gtrack.total_member_segments,
                'num_active_local_tracks': len(gtrack.active_local_tracks),
                'member_track_ids': ';'.join(gtrack.get_all_member_ids())
            }
            records.append(record)
        
        return pd.DataFrame(records)
    
    def get_local_to_global_map(self) -> pd.DataFrame:
        """获取 Local Track → Global Track 映射"""
        records = []
        for local_id, gid in self.local_to_global.items():
            records.append({
                'local_track_id': local_id,
                'global_id': gid
            })
        return pd.DataFrame(records)
    
    def get_history_dataframe(self) -> pd.DataFrame:
        """获取历史"""
        if not self.history:
            return pd.DataFrame()
        return pd.DataFrame(self.history)
    
    def get_merge_log(self) -> pd.DataFrame:
        """获取 merge log"""
        if not self.merge_log:
            return pd.DataFrame()
        return pd.DataFrame(self.merge_log)


def main():
    """测试入口"""
    import argparse
    import json
    
    parser = argparse.ArgumentParser(description='Global Track Manager')
    parser.add_argument('--input', type=str, required=True)
    parser.add_argument('--output', type=str, required=True)
    
    args = parser.parse_args()
    
    print("Global Track Manager test")


if __name__ == '__main__':
    main()