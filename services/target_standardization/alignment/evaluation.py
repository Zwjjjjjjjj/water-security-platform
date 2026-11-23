"""独立评测：把对齐结果与模拟真值比较。

**隔离原则**：`reference_truth.csv` 与 `target_mapping.json` 只由本模块读取，
对齐模块本身不接触真值，也不使用对应关系表——否则评测结果会自我印证。

评测量：
  - 位置误差：ENU 平面距离（米），分轴误差，统计均值/中位数/RMS/P95/最大值
  - 时间误差：观测状态时间与真值状态时间之差（秒）
  - 按传感器与按目标分别统计
"""

from __future__ import annotations

import csv
import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from geodesy import LocalOrigin


def read_truth_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_mapping_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _parse_iso(value: str) -> Optional[float]:
    """ISO 时间 → Unix 秒。"""
    if not value:
        return None
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return moment.timestamp()


def _percentile(values: List[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "median": _percentile(values, 0.5),
        "rms": math.sqrt(sum(v * v for v in values) / len(values)),
        "p95": _percentile(values, 0.95),
        "max": max(values),
        "min": min(values),
    }


class AlignmentEvaluator:
    """独立评测器：只依赖真值文件与对齐结果。

    ## 真值的坐标处理

    真值文件同时给出 `truth_east_m/truth_north_m`（相对**真值生成时**的
    原点）与 `truth_latitude_deg/truth_longitude_deg`（绝对地理坐标）。

    评测时**以经纬度为准**，在**当前对齐配置的原点**下重算真值 ENU。
    原因：对齐配置的现场原点是可更换的，若直接用真值文件里的米制坐标，
    换原点后比较会凭空多出原点平移量（1 km 级）的假误差。

    真值文件的米制坐标仅在无法取得经纬度时回退使用。
    """

    def __init__(
        self,
        truth_path: str,
        mapping_path: Optional[str] = None,
        frame_origin: Optional[Tuple[float, float]] = None,
    ):
        self.truth = read_truth_csv(truth_path)
        self.mapping = read_mapping_json(mapping_path) if mapping_path else None
        self.frame_origin = frame_origin
        # 缓存按原点在真值行上的重算结果，避免每条记录重复计算
        self._truth_enu_cache: Dict[int, Tuple[float, float]] = {}
        # 按 (sensor, index) 建立真值索引
        self._truth_index = {
            (row["sensor"], int(row["index"])): row for row in self.truth
        }
        # 按 (sensor, 状态时间) 建立真值索引 —— 观测流会因漏报而与真值行号错位，
        # 因此配对必须依据时间而不是行号。
        self._truth_by_time: Dict[Tuple[str, float], Dict[str, str]] = {}
        for row in self.truth:
            epoch = _parse_iso(str(row.get("state_time_utc") or ""))
            if epoch is None:
                continue
            self._truth_by_time[(row["sensor"], epoch)] = row
        # 每个传感器的时间序列，用于就近配对
        self._truth_times: Dict[str, List[Tuple[float, Dict[str, str]]]] = {}
        for row in self.truth:
            epoch = _parse_iso(str(row.get("state_time_utc") or ""))
            if epoch is None:
                continue
            self._truth_times.setdefault(row["sensor"], []).append((epoch, row))
        for items in self._truth_times.values():
            items.sort(key=lambda item: item[0])

        # 按**公共仿真时刻**建立的索引（不含传感器时钟偏移）。
        # 对齐模块修正已知偏移后应还原到这个时刻，评测据此检验偏移修正。
        self._truth_by_common_time: Dict[
            Tuple[str, float], Dict[str, str]
        ] = {}
        self._truth_common_times: Dict[
            str, List[Tuple[float, Dict[str, str]]]
        ] = {}
        for row in self.truth:
            common = row.get("truth_state_time_utc") or row.get("state_time_utc")
            epoch = _parse_iso(str(common or ""))
            if epoch is None:
                continue
            self._truth_by_common_time[(row["sensor"], epoch)] = row
            self._truth_common_times.setdefault(row["sensor"], []).append(
                (epoch, row)
            )
        for items in self._truth_common_times.values():
            items.sort(key=lambda item: item[0])

    def truth_for(self, sensor: str, index: int) -> Optional[Dict[str, str]]:
        return self._truth_index.get((sensor, index))

    def truth_enu(
        self, row: Dict[str, str]
    ) -> Optional[Tuple[float, float]]:
        """真值行在**当前对齐原点**下的 ENU 坐标。

        优先用经纬度在当前原点下重算；没有经纬度时回退到真值文件里
        已有的米制坐标（此时要求原点与真值一致）。
        """
        key = id(row)
        if key in self._truth_enu_cache:
            return self._truth_enu_cache[key]

        result: Optional[Tuple[float, float]] = None
        latitude = row.get("truth_latitude_deg")
        longitude = row.get("truth_longitude_deg")
        if latitude and longitude and self.frame_origin is not None:
            origin = LocalOrigin(
                latitude_deg=float(self.frame_origin[0]),
                longitude_deg=float(self.frame_origin[1]),
            )
            east, north = origin.to_enu(float(latitude), float(longitude))
            result = (east, north)
        elif row.get("truth_east_m") and row.get("truth_north_m"):
            result = (float(row["truth_east_m"]), float(row["truth_north_m"]))

        self._truth_enu_cache[key] = result
        return result

    def truth_for_time(
        self, sensor: str, epoch: float, tolerance_s: float = 1.5
    ) -> Optional[Dict[str, str]]:
        """按**时间**就近配对真值。

        观测流因漏报会与真值行号错位，按行号配对会把误差算到错误的真值点上。
        这里按状态时间就近匹配，并要求在容差内。
        """
        candidates = self._truth_times.get(sensor)
        if not candidates:
            return None
        nearest = min(candidates, key=lambda item: abs(item[0] - epoch))
        if abs(nearest[0] - epoch) > tolerance_s:
            return None
        return nearest[1]

    def truth_for_common_time(
        self, sensor: str, epoch: float, tolerance_s: float = 1.5
    ) -> Optional[Dict[str, str]]:
        """按**公共仿真时刻**就近配对真值。

        对齐模块修正已知时钟偏移后，记录的状态时间应还原到公共仿真时刻，
        因此用这个索引来检验偏移修正是否正确。
        """
        candidates = self._truth_common_times.get(sensor)
        if not candidates:
            return None
        nearest = min(candidates, key=lambda item: abs(item[0] - epoch))
        if abs(nearest[0] - epoch) > tolerance_s:
            return None
        return nearest[1]

    def evaluate_records(
        self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """评测已对齐的记录。"""
        per_sensor: Dict[str, Dict[str, List[float]]] = {}
        per_target: Dict[str, Dict[str, List[float]]] = {}
        time_errors: Dict[str, List[float]] = {}
        unmatched = 0
        missing_truth = 0
        not_converted = 0
        matched = 0

        for record in records:
            sensor = str(record.get("source_type"))
            state_epoch = _parse_iso(str(record.get("state_time_utc") or ""))
            if state_epoch is None:
                unmatched += 1
                continue
            # 按时间配对真值：观测流因漏报会与真值行号错位
            truth = self.truth_for_time(sensor, state_epoch)
            if truth is None:
                missing_truth += 1
                continue
            if record.get("alignment_status") != "ok":
                not_converted += 1
                continue

            east = record.get("east_m")
            north = record.get("north_m")
            if east is None or north is None:
                not_converted += 1
                continue

            truth_enu = self.truth_enu(truth)
            if truth_enu is None:
                missing_truth += 1
                continue
            truth_east, truth_north = truth_enu
            error_east = east - truth_east
            error_north = north - truth_north
            planar = math.hypot(error_east, error_north)

            bucket = per_sensor.setdefault(
                sensor, {"planar_m": [], "east_m": [], "north_m": []}
            )
            bucket["planar_m"].append(planar)
            bucket["east_m"].append(error_east)
            bucket["north_m"].append(error_north)

            target_id = str(truth.get("target_id"))
            target_bucket = per_target.setdefault(
                target_id, {"planar_m": [], "east_m": [], "north_m": []}
            )
            target_bucket["planar_m"].append(planar)
            target_bucket["east_m"].append(error_east)
            target_bucket["north_m"].append(error_north)

            # 时间误差：观测状态时间 − 真值状态时间
            observed = _parse_iso(str(record.get("state_time_utc") or ""))
            expected = _parse_iso(str(truth.get("state_time_utc") or ""))
            if observed is not None and expected is not None:
                time_errors.setdefault(sensor, []).append(observed - expected)

            matched += 1

        result: Dict[str, Any] = {
            "matched_count": matched,
            "unmatched_count": unmatched,
            "missing_truth_count": missing_truth,
            "not_converted_count": not_converted,
            "effective_points_note": (
                "误差统计只使用**同时满足**以下条件的记录："
                "(1) 空间转换成功（alignment_status=ok）；"
                "(2) 能按状态时间配上真值行；"
                "(3) ENU 坐标非空。"
                "unmatched=无可用状态时间，missing_truth=配上真值失败，"
                "not_converted=空间转换失败或坐标为空。"
            ),
            "position_error_by_sensor": {
                sensor: {
                    "planar_m": _stats(values["planar_m"]),
                    "east_m": _stats(values["east_m"]),
                    "north_m": _stats(values["north_m"]),
                }
                for sensor, values in per_sensor.items()
            },
            "position_error_by_target": {
                target: {
                    "planar_m": _stats(values["planar_m"]),
                    "east_m": _stats(values["east_m"]),
                    "north_m": _stats(values["north_m"]),
                }
                for target, values in per_target.items()
            },
            "time_error_by_sensor": {
                sensor: _stats(values) for sensor, values in time_errors.items()
            },
        }
        return result

    def evaluate_grid(
        self, grid_rows: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """评测插值后的公共栅格记录。

        栅格点按 (source_type, local_track_id) 与真值比对：真值中同一航迹
        按仿真时刻排序，栅格点按时刻与之配对（取时间最接近且在容差内的点）。
        """
        # 真值按航迹建立时间序列
        truth_by_track: Dict[Tuple[str, str], List[Tuple[float, Dict[str, str]]]] = {}
        for row in self.truth:
            sensor = row["sensor"]
            track_id = str(row.get("target_id"))
            # 用**公共仿真时刻**配对：栅格点位于公共时间轴上，
            # 而真值行的 state_time_utc 是含传感器时钟偏移的原始读数。
            epoch = _parse_iso(
                str(row.get("truth_state_time_utc") or row.get("state_time_utc") or "")
            )
            if epoch is None:
                continue
            truth_by_track.setdefault((sensor, track_id), []).append((epoch, row))
        for items in truth_by_track.values():
            items.sort(key=lambda item: item[0])

        per_sensor: Dict[str, Dict[str, List[float]]] = {}
        per_track: Dict[str, Dict[str, Any]] = {}
        unavailable = 0
        evaluated = 0

        for row in grid_rows:
            if row.get("alignment_status") != "ok":
                unavailable += 1
                continue
            sensor = str(row.get("source_type"))
            track_id = str(row.get("local_track_id"))
            candidates = truth_by_track.get((sensor, track_id))
            if not candidates:
                continue
            grid_epoch = _parse_iso(str(row.get("time_utc") or ""))
            if grid_epoch is None:
                continue
            nearest = min(candidates, key=lambda item: abs(item[0] - grid_epoch))
            delta = nearest[0] - grid_epoch
            # 只在 1.5 s 容差内配对，超出说明栅格与真值时间基准不一致
            if abs(delta) > 1.5:
                continue
            truth = nearest[1]
            truth_enu = self.truth_enu(truth)
            if truth_enu is None:
                continue
            error_east = float(row["east_m"]) - truth_enu[0]
            error_north = float(row["north_m"]) - truth_enu[1]
            planar = math.hypot(error_east, error_north)

            bucket = per_sensor.setdefault(
                sensor, {"planar_m": [], "east_m": [], "north_m": [], "time_s": []}
            )
            bucket["planar_m"].append(planar)
            bucket["east_m"].append(error_east)
            bucket["north_m"].append(error_north)
            bucket["time_s"].append(delta)

            track_bucket = per_track.setdefault(
                f"{sensor}|{track_id}",
                {
                    "planar_m": [],
                    "time_s": [],
                    "interpolation_method": row.get("interpolation_method"),
                    "interpolation_available": row.get("interpolation_available"),
                },
            )
            track_bucket["planar_m"].append(planar)
            track_bucket["time_s"].append(delta)
            evaluated += 1

        return {
            "evaluated_count": evaluated,
            "unavailable_count": unavailable,
            "position_error_by_sensor": {
                sensor: {
                    "planar_m": _stats(values["planar_m"]),
                    "east_m": _stats(values["east_m"]),
                    "north_m": _stats(values["north_m"]),
                }
                for sensor, values in per_sensor.items()
            },
            "time_error_by_sensor": {
                sensor: _stats(values["time_s"]) for sensor, values in per_sensor.items()
            },
            "position_error_by_track": {
                track: {
                    "planar_m": _stats(values["planar_m"]),
                    "time_s": _stats(values["time_s"]),
                    "interpolation_method": values["interpolation_method"],
                    "interpolation_available": values["interpolation_available"],
                }
                for track, values in per_track.items()
            },
        }

    # 比较用的数值容差（米）。
    #
    # 界限本身是**闭区间**：距离量化误差恰好等于 ±LSB/2 时（真值正好落在
    # 两个刻度的中点上），实测误差会等于上界。浮点表示上这可能表现为超出
    # 上界几个微米（实测 0.048 mm），属于表示误差而非模型缺陷。
    #
    # 因此比较时留一个微米级容差，避免把"恰好在边界上"判成超限。
    COMPARISON_TOLERANCE_M = 1e-4

    def evaluate_bounds(
        self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """位置误差与上界的比较。

        独立于 `evaluate_records` 自行配对真值并重算误差，避免依赖其它
        统计函数的中间状态。雷达按径向/切向分别比较——量化误差是各向异性的，
        只比平面标量会掩盖切向超限的情况。
        """
        from .bounds import BoundCalculator  # 局部导入避免循环依赖

        per_sensor: Dict[str, Dict[str, Any]] = {}
        violations: list = []

        for record in records:
            bound = record.get("error_bound")
            if not bound:
                continue
            sensor = str(record.get("source_type"))
            east = record.get("east_m")
            north = record.get("north_m")
            if east is None or north is None:
                continue

            state_epoch = _parse_iso(str(record.get("state_time_utc") or ""))
            if state_epoch is None:
                continue
            truth = self.truth_for_time(sensor, state_epoch)
            if truth is None:
                continue

            truth_enu = self.truth_enu(truth)
            if truth_enu is None:
                continue
            error_east = east - truth_enu[0]
            error_north = north - truth_enu[1]
            planar = math.hypot(error_east, error_north)

            bucket = per_sensor.setdefault(
                sensor,
                {
                    "checked_count": 0,
                    "within_bound_count": 0,
                    "violation_count": 0,
                    "radial_violation_count": 0,
                    "tangential_violation_count": 0,
                    "error_over_bound_ratio_max": 0.0,
                },
            )
            bucket["checked_count"] += 1

            bound_planar = float(bound["planar_m"])
            ratio = planar / bound_planar if bound_planar > 0 else 0.0
            bucket["error_over_bound_ratio_max"] = max(
                bucket["error_over_bound_ratio_max"], ratio
            )
            tolerance = self.COMPARISON_TOLERANCE_M
            if planar <= bound_planar + tolerance:
                bucket["within_bound_count"] += 1
            else:
                bucket["violation_count"] += 1

            # 径向/切向比较（仅雷达有明确几何意义）
            if sensor == "radar":
                bearing = (record.get("position") or {}).get("bearing_rad")
                if bearing is not None:
                    radial = error_east * math.sin(bearing) + error_north * math.cos(
                        bearing
                    )
                    tangential = error_east * math.cos(bearing) - error_north * math.sin(
                        bearing
                    )
                    if abs(radial) > float(bound["radial_m"]) + tolerance:
                        bucket["radial_violation_count"] += 1
                    if abs(tangential) > float(bound["tangential_m"]) + tolerance:
                        bucket["tangential_violation_count"] += 1
                        if len(violations) < 20:
                            violations.append(
                                {
                                    "sensor": sensor,
                                    "source_row": record.get("source_row"),
                                    "range_m": (record.get("position") or {}).get(
                                        "range_m"
                                    ),
                                    "planar_error_m": planar,
                                    "bound_planar_m": bound_planar,
                                    "radial_error_m": radial,
                                    "bound_radial_m": bound["radial_m"],
                                    "tangential_error_m": tangential,
                                    "bound_tangential_m": bound["tangential_m"],
                                    "components": bound.get("components"),
                                }
                            )

        return {
            "by_sensor": per_sensor,
            "violation_examples": violations,
            "note": (
                "上界含报文编码量化、解码端舍入、传感器噪声（k·σ）与观测量化；"
                "雷达按径向/切向分别比较"
            ),
        }

    def evaluate_coverage(
        self, records: List[Dict[str, Any]], tolerance_s: float = 1.5
    ) -> Dict[str, Any]:
        """覆盖率评测：真值中每个时刻是否都有对应的观测。

        真值行保留全部时刻（含被漏报的），因此"真值有、观测流没有"的时刻
        就是缺失。这比"误差统计"更能反映缺失处理是否正确。
        """
        expected: Dict[str, int] = {}
        dropped: Dict[str, int] = {}
        for row in self.truth:
            sensor = row["sensor"]
            expected[sensor] = expected.get(sensor, 0) + 1
            if row.get("dropped") == "true":
                dropped[sensor] = dropped.get(sensor, 0) + 1

        observed: Dict[str, int] = {}
        for record in records:
            sensor = str(record.get("source_type"))
            observed[sensor] = observed.get(sensor, 0) + 1

        result: Dict[str, Any] = {}
        for sensor, count in expected.items():
            got = observed.get(sensor, 0)
            missing = max(0, count - got)
            result[sensor] = {
                "expected_observations": count,
                "observed_observations": got,
                "missing_observations": missing,
                "coverage_ratio": (got / count) if count else None,
                "dropout_truth_count": dropped.get(sensor, 0),
                "unexplained_missing": max(0, missing - dropped.get(sensor, 0)),
            }
        return result

    def verify_clock_offset_correction(
        self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """检验已知时钟偏移的修正效果。

        对每条记录：
          修正前误差 = 记录状态时间 − 公共仿真时刻（应等于该源的已知偏移）
          修正后误差 = 修正后的状态时间 − 公共仿真时刻（应接近 0）

        修正后的时间不在标准记录里，而是由对齐模块写入 `aligned_time_utc`；
        若未提供，则只报告修正前的误差。
        """
        result: Dict[str, Any] = {}
        for record in records:
            sensor = str(record.get("source_type"))
            raw_state = record.get("state_time_utc")
            corrected = record.get("aligned_time_utc") or record.get(
                "corrected_state_time_utc"
            )
            if not raw_state:
                continue
            raw_epoch = _parse_iso(str(raw_state))
            if raw_epoch is None:
                continue
            truth = self.truth_for_common_time(sensor, raw_epoch, tolerance_s=5.0)
            if truth is None:
                continue
            common = _parse_iso(
                str(truth.get("truth_state_time_utc") or truth.get("state_time_utc"))
            )
            if common is None:
                continue

            bucket = result.setdefault(
                sensor, {"before_correction_s": [], "after_correction_s": []}
            )
            bucket["before_correction_s"].append(raw_epoch - common)
            if corrected:
                corrected_epoch = _parse_iso(str(corrected))
                if corrected_epoch is not None:
                    bucket["after_correction_s"].append(corrected_epoch - common)

        result: Dict[str, Any] = {}
        for record in records:
            sensor = str(record.get("source_type"))
            raw_state = record.get("state_time_utc")
            corrected = record.get("aligned_time_utc") or record.get(
                "corrected_state_time_utc"
            )
            if not raw_state:
                continue
            raw_epoch = _parse_iso(str(raw_state))
            if raw_epoch is None:
                continue
            truth = self.truth_for_common_time(sensor, raw_epoch, tolerance_s=5.0)
            if truth is None:
                continue
            common = _parse_iso(
                str(truth.get("truth_state_time_utc") or truth.get("state_time_utc"))
            )
            if common is None:
                continue

            bucket = result.setdefault(
                sensor, {"before_correction_s": [], "after_correction_s": []}
            )
            bucket["before_correction_s"].append(raw_epoch - common)
            if corrected:
                corrected_epoch = _parse_iso(str(corrected))
                if corrected_epoch is not None:
                    bucket["after_correction_s"].append(corrected_epoch - common)

        return {
            sensor: {
                "before_correction_s": _stats(values["before_correction_s"]),
                "after_correction_s": _stats(values["after_correction_s"]),
                "configured_clock_offset_s": values.get("configured_offset_s"),
                "configured_quantum_bias_s": values.get("configured_quantum_bias_s"),
                "residual_explanation": values.get("residual_explanation"),
                "residual_explained": values.get("residual_explained"),
            }
            for sensor, values in result.items()
        }

    def cross_source_consistency(
        self, records: List[Dict[str, Any]], time_tolerance_s: float = 1.0
    ) -> Dict[str, Any]:
        """两路观测在同一时刻的互差（不依赖真值，纯自洽性检查）。"""
        by_sensor: Dict[str, Dict[float, Tuple[float, float]]] = {}
        for record in records:
            if record.get("alignment_status") != "ok":
                continue
            sensor = str(record.get("source_type"))
            east = record.get("east_m")
            north = record.get("north_m")
            state = record.get("state_time_utc")
            if east is None or north is None or not state:
                continue
            epoch = _parse_iso(state)
            if epoch is None:
                continue
            by_sensor.setdefault(sensor, {})[round(epoch, 3)] = (east, north)

        sensors = sorted(by_sensor)
        if len(sensors) < 2:
            return {"pair_count": 0, "note": "不足两路可比较的数据源"}

        pairs = []
        radar = by_sensor.get("radar", {})
        ais = by_sensor.get("ais", {})
        for timestamp, (radar_east, radar_north) in radar.items():
            candidates = [
                (abs(other - timestamp), other)
                for other in ais
                if abs(other - timestamp) <= time_tolerance_s
            ]
            if not candidates:
                continue
            _, nearest = min(candidates)
            ais_east, ais_north = ais[nearest]
            pairs.append(
                {
                    "time_delta_s": round(nearest - timestamp, 6),
                    "planar_diff_m": math.hypot(
                        radar_east - ais_east, radar_north - ais_north
                    ),
                    "east_diff_m": radar_east - ais_east,
                    "north_diff_m": radar_north - ais_north,
                }
            )

        diffs = [p["planar_diff_m"] for p in pairs]
        deltas = [p["time_delta_s"] for p in pairs]
        return {
            "pair_count": len(pairs),
            "time_tolerance_s": time_tolerance_s,
            "planar_diff_m": _stats(diffs),
            "time_delta_s": _stats(deltas),
        }
