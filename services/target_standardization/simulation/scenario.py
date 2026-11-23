"""模拟场景生成：从共同参考轨迹生成雷达 TTM 与 AIS !AIVDM 两路观测。

设计要点（对应任务要求）：
- 两路观测**分别**从参考轨迹采样，误差也**分别**施加；
  不把带误差的 AIS 观测复制成雷达观测。
- 观测状态时间与接收时间分开保存：接收时间 = 观测状态时间 + 接收延迟
  （或已知的时钟偏移）。
- 真值独立保存：每个时刻的参考位置、速度与目标编号。
- 对应关系（雷达本地编号 ↔ 模拟 MMSI）单独保存，仅供评测。

场景定义（configs/scenarios/*.json）：
  A 几何基线：单目标、同步、无额外噪声
  B 异步观测：不同采样周期 + 已知时间偏移 + 接收延迟
  C 观测扰动：位置误差 + 量化 + 短时漏报
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import geo, nmea_encode, trajectory


@dataclass
class SensorConfig:
    """单个传感器的观测配置。"""

    name: str
    enabled: bool = True
    sample_dt_s: float = 1.0
    # 状态时间与接收时间之间的已知偏移（秒）：正表示接收晚于状态时间
    receive_delay_s: float = 0.0
    # 传感器时钟相对仿真时钟的已知偏移（秒）
    clock_offset_s: float = 0.0
    # 位置误差：正态噪声标准差（米）
    position_noise_std_m: float = 0.0
    # 测角噪声标准差（度）。与 position_noise_std_m 不同：
    # 测角噪声作用在**方位角**上，其切向影响随距离线性放大
    # （切向误差 ≈ 方位残差 × 距离），这正是 MTDSP 实测到的结构。
    # 0 表示不施加。
    bearing_noise_std_deg: float = 0.0
    # 稳定的方位系统偏差（度）：每个传感器一个常量偏置，
    # 模拟 MTDSP 各目标独立的稳定方位偏差（+0.008°~+0.806°）。
    bearing_bias_deg: float = 0.0
    # 坐标量化步长（度）；0 表示不量化
    quantization_deg: float = 0.0
    # 短时漏报：每次连续漏报的时长（秒）与发生概率（每采样步）
    dropout_duration_s: float = 0.0
    dropout_probability: float = 0.0
    # 速度误差标准差（m/s）
    speed_noise_std_mps: float = 0.0


@dataclass
class ScenarioConfig:
    """场景配置。"""

    scenario_id: str
    description: str
    # 仿真时间轴
    sim_date: str = "2019-09-30"
    sim_start_time_of_day_s: float = 54000.0
    # 雷达站
    radar_latitude_deg: float = 54.108200
    radar_longitude_deg: float = 12.056800
    radar_target_number: str = "00"
    radar_target_name: str = "TGT00"
    radar_unit: str = "N"
    # 模拟 AIS
    sim_mmsi: int = 228050803
    sim_heading_deg: float = 0.0
    # 目标初始位置（相对雷达站的局部偏移，米）
    target_start_east_m: float = 0.0
    target_start_north_m: float = 3000.0
    # 参考轨迹
    template_source_csv: str = ""
    template_mmsi: str = ""
    template_placeholder_positions: List[List[float]] = field(default_factory=list)
    reference_resample_dt_s: float = 1.0
    reference_outlier_threshold_m: float = 60.0
    reference_smooth_window: int = 5
    # 传感器
    radar_sensor: SensorConfig = field(default_factory=lambda: SensorConfig("radar"))
    ais_sensor: SensorConfig = field(default_factory=lambda: SensorConfig("ais"))
    # 接收时钟（记录时钟）基准；None 表示与仿真时钟同速同起点
    capture_clock_epoch_utc: Optional[str] = "2022-06-03T16:30:00+00:00"
    # 随机种子，保证可复现
    random_seed: int = 20240603
    # 说明性字段（不参与计算）：误差参数出处、已知局限等。
    # 原样保留以便追溯配置里每个数值的来源。
    notes: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ScenarioConfig":
        data = dict(data)
        for key in ("radar_sensor", "ais_sensor"):
            if key in data and isinstance(data[key], dict):
                data[key] = SensorConfig(**data[key])
        # 未被识别的键并入 notes，避免配置里的说明性字段导致加载失败
        known = set(ScenarioConfig.__dataclass_fields__)
        extra = {k: v for k, v in data.items() if k not in known}
        for key in extra:
            data.pop(key)
        if extra:
            merged = dict(data.get("notes") or {})
            merged.update(extra)
            data["notes"] = merged
        return ScenarioConfig(**data)


@dataclass
class Observation:
    """一次观测（尚未编码成报文）。"""

    sensor: str
    index: int
    # 相对场景起点的秒数（不含传感器时钟偏移）
    t_rel_s: float
    # 该传感器的仿真日内时间（秒，已含时钟偏移）——即报文中上报的状态时间
    state_seconds_of_day: float
    state_time_utc: str
    # 公共仿真时刻（不含传感器时钟偏移）——对齐修正后应还原到此
    truth_state_time_utc: str
    receive_time_utc: str
    receive_time_unix: float
    # 参考真值（该采样时刻）
    truth_east_m: float
    truth_north_m: float
    truth_latitude_deg: float
    truth_longitude_deg: float
    truth_speed_mps: float
    truth_course_rad: float
    # 观测值
    obs_east_m: float = 0.0
    obs_north_m: float = 0.0
    obs_latitude_deg: float = 0.0
    obs_longitude_deg: float = 0.0
    obs_speed_kn: float = 0.0
    obs_course_deg: float = 0.0
    obs_range_nm: float = 0.0
    obs_bearing_deg: float = 0.0
    dropped: bool = False
    drop_reason: Optional[str] = None


class ScenarioGenerator:
    """按场景配置生成两路观测、真值与对应关系。"""

    def __init__(self, config: ScenarioConfig):
        self.config = config
        self.random = random.Random(config.random_seed)
        self.origin = geo.LocalOrigin(
            latitude_deg=config.radar_latitude_deg,
            longitude_deg=config.radar_longitude_deg,
        )
        # 仿真时钟：当日起点（午夜）作为基准，日内偏移由 sim_start_time_of_day_s 给出
        self.sim_midnight = datetime.strptime(
            f"{config.sim_date} 00:00:00", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        # 仿真时刻 t=0 对应的绝对时刻
        self.sim_epoch = self.sim_midnight + timedelta(
            seconds=config.sim_start_time_of_day_s
        )
        # 记录时钟基准（与仿真时钟同速，仅起点不同）
        self.capture_epoch = (
            datetime.fromisoformat(config.capture_clock_epoch_utc)
            if config.capture_clock_epoch_utc
            else self.sim_epoch
        )

    # -- 参考轨迹 ------------------------------------------------------------

    def build_reference(self) -> trajectory.TrajectoryExtraction:
        """提取并整理共同参考轨迹，再平移到雷达站坐标系。"""
        extraction = trajectory.extract_reference_trajectory(
            self.config.template_source_csv,
            mmsi=self.config.template_mmsi,
            placeholder_positions=[
                tuple(pair) for pair in self.config.template_placeholder_positions
            ],
            resample_dt_s=self.config.reference_resample_dt_s,
            outlier_threshold_m=self.config.reference_outlier_threshold_m,
            smooth_window=self.config.reference_smooth_window,
        )

        # 把模板起点对齐到场景设定的起始位置（保留模板形态）
        start = extraction.samples[0]
        offset_east = self.config.target_start_east_m - start.east_m
        offset_north = self.config.target_start_north_m - start.north_m
        for sample in extraction.samples:
            sample.east_m += offset_east
            sample.north_m += offset_north

        extraction.provenance["alignment"] = {
            "template_start_east_m": round(start.east_m, 3),
            "template_start_north_m": round(start.north_m, 3),
            "target_start_east_m": self.config.target_start_east_m,
            "target_start_north_m": self.config.target_start_north_m,
            "offset_east_m": round(offset_east, 3),
            "offset_north_m": round(offset_north, 3),
            "note": (
                "模板轨迹整体平移以对齐场景起始位置；形态（采样间隔、速度变化、"
                "航向变化）来自实测轨迹，位置为场景设定值。"
            ),
        }
        extraction.provenance["scenario_id"] = self.config.scenario_id
        return extraction

    # -- 采样与扰动 ----------------------------------------------------------

    def _sample_times(self, sensor: SensorConfig, duration_s: float) -> List[float]:
        step = sensor.sample_dt_s
        count = int(duration_s / step) + 1
        return [index * step for index in range(count)]

    def _apply_noise(self, sensor: SensorConfig, value: float, std: float) -> float:
        if std <= 0:
            return value
        return value + self.random.gauss(0.0, std)

    def _quantize(self, sensor: SensorConfig, value: float) -> float:
        step = sensor.quantization_deg
        if step <= 0:
            return value
        return round(value / step) * step

    def _sample_sensor(
        self,
        sensor: SensorConfig,
        samples: Sequence[trajectory.ReferenceSample],
        duration_s: float,
    ) -> List[Observation]:
        """按传感器配置采样参考轨迹并施加该传感器自己的扰动。"""
        observations: List[Observation] = []
        dropout_until: Optional[float] = None
        index = 0

        for t_rel in self._sample_times(sensor, duration_s):
            reference = self._interpolate_reference(samples, t_rel)
            if reference is None:
                continue
            east, north, speed_mps, course_rad = reference

            # 状态时间（传感器自身时钟读数）= 真实时刻 + 时钟偏移
            state_seconds = (
                self.config.sim_start_time_of_day_s
                + t_rel
                + sensor.clock_offset_s
            )
            state_dt = self.sim_midnight + timedelta(seconds=state_seconds)
            # 接收时刻落在记录时钟上：真实时刻 + 接收延迟
            # （时钟偏移只影响传感器上报的状态时间，不影响到达时刻）
            capture_dt = self.capture_epoch + timedelta(
                seconds=t_rel + sensor.receive_delay_s
            )
            capture_unix = capture_dt.timestamp()

            observation = Observation(
                sensor=sensor.name,
                index=index,
                t_rel_s=t_rel,
                state_seconds_of_day=state_seconds,
                state_time_utc=state_dt.isoformat(),
                # 公共仿真时刻（不含传感器时钟偏移）——对齐模块修正偏移后
                # 应还原到这个时刻，评测据此检验偏移修正是否正确。
                truth_state_time_utc=(
                    self.sim_midnight
                    + timedelta(seconds=self.config.sim_start_time_of_day_s + t_rel)
                ).isoformat(),
                receive_time_utc=capture_dt.isoformat(),
                receive_time_unix=capture_unix,
                truth_east_m=east,
                truth_north_m=north,
                truth_latitude_deg=0.0,
                truth_longitude_deg=0.0,
                truth_speed_mps=speed_mps,
                truth_course_rad=course_rad,
            )
            lat, lon = self.origin.to_latlng(east, north)
            observation.truth_latitude_deg = lat
            observation.truth_longitude_deg = lon

            # 漏报判定
            if dropout_until is not None and t_rel < dropout_until:
                observation.dropped = True
                observation.drop_reason = "dropout_active"
            elif (
                sensor.dropout_duration_s > 0
                and sensor.dropout_probability > 0
                and self.random.random() < sensor.dropout_probability
            ):
                observation.dropped = True
                observation.drop_reason = "dropout_start"
                dropout_until = t_rel + sensor.dropout_duration_s

            if not observation.dropped:
                # 仅对该传感器施加位置误差
                obs_east = self._apply_noise(
                    sensor, east, sensor.position_noise_std_m
                )
                obs_north = self._apply_noise(
                    sensor, north, sensor.position_noise_std_m
                )
                # 量化在经纬度域上做（与真实数据一致）
                obs_lat, obs_lon = self.origin.to_latlng(obs_east, obs_north)
                obs_lat = self._quantize(sensor, obs_lat)
                obs_lon = self._quantize(sensor, obs_lon)

                obs_speed = self._apply_noise(
                    sensor, speed_mps, sensor.speed_noise_std_mps
                )
                obs_course = math.degrees(course_rad) % 360.0

                observation.obs_east_m = obs_east
                observation.obs_north_m = obs_north
                observation.obs_latitude_deg = obs_lat
                observation.obs_longitude_deg = obs_lon
                observation.obs_speed_kn = mps_to_knots(obs_speed)
                observation.obs_course_deg = obs_course

                # 雷达观测量：相对雷达站的距离与真方位
                offset_east, offset_north = self.origin.to_enu(obs_lat, obs_lon)
                observation.obs_range_nm = math.hypot(offset_east, offset_north) / 1852.0
                bearing_deg = math.degrees(
                    geo.enu_offset_to_bearing(offset_east, offset_north)
                )
                # 测角噪声作用在方位上：切向影响 = 方位残差 × 距离，
                # 与 MTDSP 实测结构一致（实测切向误差随距离放大）。
                if sensor.bearing_noise_std_deg > 0.0 or sensor.bearing_bias_deg != 0.0:
                    bearing_deg += sensor.bearing_bias_deg
                    if sensor.bearing_noise_std_deg > 0.0:
                        bearing_deg += self.random.gauss(0.0, sensor.bearing_noise_std_deg)
                observation.obs_bearing_deg = bearing_deg % 360.0

            observations.append(observation)
            index += 1
        return observations

    @staticmethod
    def _interpolate_reference(
        samples: Sequence[trajectory.ReferenceSample], t_rel: float
    ) -> Optional[Tuple[float, float, float, float]]:
        if not samples:
            return None
        if t_rel <= samples[0].t_rel_s:
            s = samples[0]
            return s.east_m, s.north_m, s.speed_mps, s.course_rad
        if t_rel >= samples[-1].t_rel_s:
            s = samples[-1]
            return s.east_m, s.north_m, s.speed_mps, s.course_rad
        low, high = 0, len(samples) - 1
        while high - low > 1:
            mid = (low + high) // 2
            if samples[mid].t_rel_s <= t_rel:
                low = mid
            else:
                high = mid
        a, b = samples[low], samples[high]
        span = b.t_rel_s - a.t_rel_s
        ratio = (t_rel - a.t_rel_s) / span if span > 0 else 0.0
        east = a.east_m + ratio * (b.east_m - a.east_m)
        north = a.north_m + ratio * (b.north_m - a.north_m)
        speed = a.speed_mps + ratio * (b.speed_mps - a.speed_mps)
        # 航向插值需处理环绕
        course = a.course_rad + ratio * _shortest_angle(a.course_rad, b.course_rad)
        return east, north, speed, course % (2 * math.pi)

    # -- 输出 ----------------------------------------------------------------

    def _build_radar_rows(
        self, observations: List[Observation]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """生成雷达 TTM 行与对应真值行。

        TTM 的日内时间取**观测状态时间**（仿真时钟），与真实数据语义一致。
        """
        rows: List[Dict[str, Any]] = []
        truth: List[Dict[str, Any]] = []
        for observation in observations:
            # 漏报的观测**不写入数据流**（真实传感器此时没有报文）。
            # 位置仍记入真值，便于评测确认此处应有观测而实际缺失。
            if observation.dropped:
                truth.append(
                    self._truth_row(observation, "radar", "", emitted=False)
                )
                continue
            state_seconds = observation.state_seconds_of_day
            time_of_day = nmea_encode.format_time_of_day(state_seconds)
            sentence = nmea_encode.encode_ttm(
                target_number=self.config.radar_target_number,
                distance_nm=observation.obs_range_nm,
                bearing_deg=observation.obs_bearing_deg,
                bearing_reference="T",
                speed_kn=observation.obs_speed_kn,
                course_deg=observation.obs_course_deg,
                course_reference="T",
                unit=self.config.radar_unit,
                target_name=self.config.radar_target_name,
                tracking_status="T",
                time_of_day=time_of_day,
                acquisition_type="A",
            )
            rows.append(
                {
                    "capture_time_unix": f"{observation.receive_time_unix:.6f}",
                    "capture_time_utc": observation.receive_time_utc,
                    "sentence_type": "TTM",
                    "checksum_valid": str(
                        nmea_encode.verify_sentence(sentence)
                    ).lower(),
                    "sentence": sentence,
                }
            )
            truth.append(self._truth_row(observation, "radar", time_of_day))
        return rows, truth

    def _build_ais_rows(
        self, observations: List[Observation]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """生成 AIS !AIVDM 行与对应真值行。

        AIS 报文的 `second` 是**仿真时钟**的分钟内秒数，与真实 RadarPWN
        数据语义一致。
        """
        rows: List[Dict[str, Any]] = []
        truth: List[Dict[str, Any]] = []
        for observation in observations:
            # 漏报的观测不写入数据流
            if observation.dropped:
                truth.append(
                    self._truth_row(observation, "ais", "", emitted=False)
                )
                continue
            state_seconds = observation.state_seconds_of_day
            second_of_minute = int(state_seconds % 60)
            sentence = nmea_encode.encode_ais_vdm(
                mmsi=self.config.sim_mmsi,
                latitude_deg=observation.obs_latitude_deg,
                longitude_deg=observation.obs_longitude_deg,
                speed_kn=observation.obs_speed_kn,
                course_deg=observation.obs_course_deg,
                heading_deg=int(round(self.config.sim_heading_deg)) % 360,
                second_of_minute=second_of_minute,
                status=0,
            )
            rows.append(
                {
                    "capture_time_unix": f"{observation.receive_time_unix:.6f}",
                    "capture_time_utc": observation.receive_time_utc,
                    "sentence_type": "VDM",
                    "checksum_valid": str(
                        nmea_encode.verify_sentence(sentence)
                    ).lower(),
                    "sentence": sentence,
                }
            )
            truth.append(
                self._truth_row(observation, "ais", nmea_encode.format_time_of_day(state_seconds))
            )
        return rows, truth

    def _build_context_rows(self, duration_s: float) -> List[Dict[str, Any]]:
        """生成 ZDA / RMC 时间与位置上下文行。

        仿真时钟与接收时钟分开：ZDA 声明仿真日期与仿真日内时间，
        接收时间戳用记录时钟。
        """
        rows: List[Dict[str, Any]] = []
        sim_start_tod = self.config.sim_start_time_of_day_s

        # RMC：本船位置与仿真起始时刻
        date_obj = self.sim_midnight
        date_ddmmyy = date_obj.strftime("%d%m%y")
        rmc = nmea_encode.encode_rmc(
            time_of_day=nmea_encode.format_time_of_day(sim_start_tod),
            latitude_deg=self.config.radar_latitude_deg,
            longitude_deg=self.config.radar_longitude_deg,
            speed_kn=0.0,
            course_deg=0.0,
            date_ddmmyy=date_ddmmyy,
        )
        capture_unix = self.capture_epoch.timestamp()
        rows.append(
            {
                "capture_time_unix": f"{capture_unix:.6f}",
                "capture_time_utc": self.capture_epoch.isoformat(),
                "sentence_type": "RMC",
                "checksum_valid": str(nmea_encode.verify_sentence(rmc)).lower(),
                "sentence": rmc,
            }
        )

        # ZDA：按雷达/ AIS 的接收间隔给出日期上下文
        zda_interval = max(
            self.config.radar_sensor.sample_dt_s,
            self.config.ais_sensor.sample_dt_s,
        )
        steps = int(duration_s / zda_interval) + 1
        for index in range(steps + 1):
            sim_seconds = sim_start_tod + index * zda_interval
            zda = nmea_encode.encode_zda(
                time_of_day=nmea_encode.format_time_of_day(sim_seconds),
                day=self.sim_midnight.day,
                month=self.sim_midnight.month,
                year=self.sim_midnight.year,
            )
            zda_capture = self.capture_epoch + timedelta(seconds=index * zda_interval)
            rows.append(
                {
                    "capture_time_unix": f"{zda_capture.timestamp():.6f}",
                    "capture_time_utc": zda_capture.isoformat(),
                    "sentence_type": "ZDA",
                    "checksum_valid": str(nmea_encode.verify_sentence(zda)).lower(),
                    "sentence": zda,
                }
            )
        return rows

    def _truth_row(
        self,
        observation: Observation,
        sensor: str,
        time_of_day: str,
        emitted: bool = True,
    ) -> Dict[str, Any]:
        """真值行：参考位置（ENU 与经纬度）、速度、目标编号。

        emitted=False 表示该时刻的观测被漏报，未出现在数据流中；
        真值仍保留，用于评测确认"此处应有观测而实际缺失"。
        """
        return {
            "sensor": sensor,
            "index": observation.index,
            "sim_time_of_day": time_of_day,
            "state_time_utc": observation.state_time_utc,
            "truth_state_time_utc": observation.truth_state_time_utc,
            "receive_time_utc": observation.receive_time_utc,
            "target_id": (
                self.config.radar_target_number
                if sensor == "radar"
                else str(self.config.sim_mmsi)
            ),
            "truth_east_m": round(observation.truth_east_m, 3),
            "truth_north_m": round(observation.truth_north_m, 3),
            "truth_latitude_deg": round(observation.truth_latitude_deg, 8),
            "truth_longitude_deg": round(observation.truth_longitude_deg, 8),
            "truth_speed_mps": round(observation.truth_speed_mps, 4),
            "truth_course_deg": round(math.degrees(observation.truth_course_rad) % 360.0, 3),
            "obs_latitude_deg": round(observation.obs_latitude_deg, 8),
            "obs_longitude_deg": round(observation.obs_longitude_deg, 8),
            "obs_speed_kn": round(observation.obs_speed_kn, 3),
            "obs_course_deg": round(observation.obs_course_deg, 3),
            "obs_range_nm": round(observation.obs_range_nm, 6),
            "obs_bearing_deg": round(observation.obs_bearing_deg, 4),
            "dropped": "true" if observation.dropped else "false",
            "drop_reason": observation.drop_reason or "",
            "emitted": "true" if emitted else "false",
        }

    def _t_rel_of(self, observation: Observation) -> float:
        """观测相对场景起点的秒数（不含传感器时钟偏移）。"""
        return observation.t_rel_s

    def generate(self, output_dir: str) -> Dict[str, Any]:
        """生成场景的全部文件，返回清单。"""
        os.makedirs(output_dir, exist_ok=True)
        extraction = self.build_reference()
        samples = extraction.samples
        duration_s = samples[-1].t_rel_s if samples else 0.0

        radar_obs = self._sample_sensor(self.config.radar_sensor, samples, duration_s)
        ais_obs = self._sample_sensor(self.config.ais_sensor, samples, duration_s)

        radar_rows, radar_truth = self._build_radar_rows(radar_obs)
        ais_rows, ais_truth = self._build_ais_rows(ais_obs)
        context_rows = self._build_context_rows(duration_s)

        files = {
            "radar_ttm": os.path.join(output_dir, "radar_ttm_with_capture_time.csv"),
            "ais_vdm": os.path.join(output_dir, "ais_with_capture_time.csv"),
            "context": os.path.join(output_dir, "all_incoming_nmea_with_capture_time.csv"),
            "truth": os.path.join(output_dir, "reference_truth.csv"),
            "mapping": os.path.join(output_dir, "target_mapping.json"),
            "manifest": os.path.join(output_dir, "scenario_manifest.json"),
        }

        _write_csv(files["radar_ttm"], radar_rows)
        _write_csv(files["ais_vdm"], ais_rows)
        _write_csv(files["context"], context_rows)
        _write_csv(files["truth"], radar_truth + ais_truth)

        # 对应关系：仅供评测
        mapping = {
            "scenario_id": self.config.scenario_id,
            "purpose": "仅供评测使用，不参与任何观测生成过程",
            "pairs": [
                {
                    "radar_source_id": "sim_radar",
                    "radar_local_track_id": self.config.radar_target_number,
                    "ais_source_id": "sim_ais",
                    "ais_local_track_id": str(self.config.sim_mmsi),
                    "sim_mmsi": self.config.sim_mmsi,
                }
            ],
        }
        _write_json(files["mapping"], mapping)

        manifest = {
            "scenario_id": self.config.scenario_id,
            "description": self.config.description,
            "config": _config_to_dict(self.config),
            "reference_trajectory": extraction.provenance,
            "counts": {
                "radar_observations": len(radar_rows),
                "radar_dropped": sum(1 for o in radar_obs if o.dropped),
                "ais_observations": len(ais_rows),
                "ais_dropped": sum(1 for o in ais_obs if o.dropped),
                "truth_rows": len(radar_truth) + len(ais_truth),
                "context_rows": len(context_rows),
            },
            "files": {key: os.path.basename(value) for key, value in files.items()},
        }
        _write_json(files["manifest"], manifest)

        return {
            "manifest": manifest,
            "files": files,
            "observations": {"radar": radar_obs, "ais": ais_obs},
        }


# -- 辅助函数 ---------------------------------------------------------------


def mps_to_knots(value: float) -> float:
    return value * 3600.0 / 1852.0


def _shortest_angle(a: float, b: float) -> float:
    delta = (b - a + math.pi) % (2 * math.pi) - math.pi
    return delta


def _write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def _config_to_dict(config: ScenarioConfig) -> Dict[str, Any]:
    data = asdict(config)
    return data
