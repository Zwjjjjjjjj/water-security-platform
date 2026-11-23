"""MTDSP 雷达检测结果适配器（CutDataTarget.csv → 公共标准记录）。

## 输入

MTDSP 数据包中的 `CutDataTarget.csv`（522 行 × 20 列）。这是雷达信号处理链
与光电联动软件的**输出汇总表**，同一张表里混用三类目标语义：

| 目标类型 | 含义 | `目标方位`/`目标距离` 的来源 |
| -------- | ---- | ------------------------------ |
| `AT`     | 已关联到 AIS 的目标（`目标编号` = MMSI） | **继承自 AIS 位置** |
| `CDT`    | 雷达检测目标（`目标编号` = 900xxx 内部号） | **雷达实测** |
| `RT`     | 雷达航迹（`目标编号` = 1 等内部号） | **雷达实测** |

## 已核验的关键事实（决定了本适配器的设计）

1. **`AT` 行的方位/距离不是雷达测量值。**
   逐点比对 `AIS_Trajectory_<MMSI>.csv` 的 `方位角(°)`/`距离(m)`：
   残差精确到 0.0000°/0.000 m —— 是同一份数值的两处拷贝。
   因此 `AT` 行**不能**用于评估雷达测角测距精度。

   **补充核验（第二轮）**：拷贝的**是快照而非逐时刻刷新**。
   `AT` 行的极坐标在数分钟内**冻结不变**（412331720 的 16 行完全相同），
   挂靠的是该目标**最后一次 AIS 报告**的值，与 `AT` 行自身的时间戳
   可相差 3215 s（412331720）、3730 s（413301480）。
   ⇒ `AT` 行的时间戳**不代表**其所挂靠 AIS 位置的时刻。
   下游做跨源时序比对时，必须先用极坐标反查对应的 AIS 记录时刻，
   不能直接用 `AT` 行的时间戳。

2. **`CDT` 行的外接矩形是雷达检测框。**
   目标方位落在矩形角度区间的比例 271/271 = 100%，距离同理。
   而 `AT`/`RT` 行的该比例为 0/144 与 0/107 —— 它们的"外接矩形"是
   **光电视场范围**（光电从动于雷达，指向固定在 347–348°）。

   另：`x_center`/`y_center`/`w`/`h` 是**归一化图像坐标（0–1）**，
   不是米；`x_center` 与目标方位严格单调对应，是方位的编码，
   不含额外的空间信息。

3. **`目标编号` 不是稳定航迹号。**
   `900001` 在 3333 m 与 4057 m 之间整块跳变，跳变块位置与 `900002`/`900003`
   重合 —— 同一物理目标在多个内部编号间切换。跨帧关联必须用
   (方位, 距离) 聚类 + 时间，不能用编号做主键。

4. **`目标方位(°)` 是真方位。**
   用 AIS 自带的极坐标反算站址，残差 1.3/14.0/52.0 m；误加
   `角度修正(°)=344`（天线安装方位）后残差恶化到 173/400/1080 m。
   因此 `角度修正` **不参与**方位换算，只作记录。

5. **时间无时区标记。**
   与 MTDSP AIS CSV 保持一致：时区未确认前 `state_time_utc` 留空。

6. **AIS CSV 的经纬度列量化很粗，不能用来做米级几何比对。**
   经度只保留 0.001°（≈88.3 m），纬度 0.0001°（≈11.1 m）。
   413203610 的 84 条记录里，经度**只有 1 个唯一值** 121.426。
   ⇒ 用经纬度反算的方位本身就带 ±1.3° 的量化噪声，
   任何基于经纬度的"站址标定"都会被量化主导。
   做几何自洽检验必须走**极坐标**路径。

7. **不存在单一站址能让所有目标的自洽残差同时归零。**
   逐目标反算站址的结果彼此相差数百米（东向 −559 m ~ +547 m）。
   ⇒ 各目标之间存在**独立**的系统偏差（疑似各船 AIS 天线与雷达站之间的
   杆差），不是一个可以全局标定的常数。

8. **CDT 列与 AIS 列之间存在约 159 m 的系统性【坐标基准差异】，不是随机误差。**
   证据链（全部可复现）：

   | 证据 | 结果 |
   | ---- | ---- |
   | `IF_2003_AT_413203610.csv` 帧头（123 帧经纬度列完全一致） | 雷达自记站址 **(37.53297, 121.42020)**，权威 |
   | AIS 行内（经纬度+方位角+距离）三元组反解参考站 | **121.41990**（东向 −27 m，5 个目标一致） |
   | 同文件内 CDT 行 vs AT 行（同一船，光电确认关联） | Δ方位 **−2.42°**（std 0.15°），Δ距离 **+20 m** |
   | 同期光电确认关联的 46 行（`匹配编号=1`） | 同样给出 −2.42°，排除配对偶发 |
   | 用 AIS 经纬度列 + CDT 极坐标反解所需站址 | 东移 **+132 m**（散布仅 11 m） |
   | 跨目标 Δ方位随方位角变号（北向簇负、南向簇正） | 站址偏移特征，**排除**统一旋转零点偏差 |

   ⇒ CDT 极坐标的参考点比 AIS 极坐标的参考点偏东约 159 m。
   在 3.9 km 处，159 m 的横向差对应约 2.4° 的方位差 —— 与实测吻合。

   **这改变了对 165 m 的定性**：它不是"检测框的固有偏移"，也不是数据错误，
   而是**两套坐标基准的固定差异**。跨源比对前必须统一基准（或扣除该系统差）；
   当作随机噪声处理是错的，会让关联门限整体失效。

9. **`x_center`/`y_center`/`w`/`h` 是归一化图像坐标（值域 0–1），不是米。**
   `x_center` 与目标方位严格单调对应（方位 349° → 0.9385；方位 357° → 0.5900），
   是方位的编码，不含额外空间信息。
   ⇒ 不存在"检测框中心相对 AIS 位置偏 165 m"这种说法；
   165 m 是极坐标换算到 ENU 后的平面差。

10. **CDT 的"目标方位/目标距离"是检测框的中心，不是角点。**
    271/271 条的目标方位落在外接矩形角度区间内、目标距离落在距离区间内，
    且相对位置中位 0.51（≈区间中心）。
    ⇒ 检测框本身是自洽的，问题出在**参考点基准**，不在框的几何。

## 输出

标准记录，`position_type = range_bearing`，`bearing_reference = "T"`。
额外字段 `radar_target_type` / `radar_measurement_kind` /
`radar_detection_box` 用于区分"雷达实测"与"AIS 继承"，避免在误差评估中
把继承值误当作雷达精度。
"""

from __future__ import annotations

import csv
import math
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import schema, time_fields, validation

# 目标类型 → 测量来源分类
# radar_detected   : 雷达实测，可用于评估雷达测量精度
# ais_associated   : 方位/距离继承自 AIS，不是雷达独立测量
# radar_tracked    : 雷达航迹（内部航迹号）
TARGET_TYPE_MEASUREMENT_KIND = {
    "AT": "ais_associated",
    "CDT": "radar_detected",
    "RT": "radar_tracked",
}

# 目标类型 → 跟踪状态
TARGET_TYPE_TRACKING_STATUS = {
    "AT": schema.TRACKING_STATUS_TRACKING,
    "CDT": None,
    "RT": schema.TRACKING_STATUS_TRACKING,
}


class RadarCutDataAdapter:
    """把 MTDSP CutDataTarget.csv 转换为公共标准记录。"""

    source_type = schema.SOURCE_TYPE_RADAR

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.source_id = source_config["source_id"]
        self.session_id = source_config["session_id"]
        self.frame_id = source_config.get("frame_id", "radar_local_polar")
        self.mapping = source_config.get("field_mapping", {})
        self.timezone_known = source_config.get("timezone_known", False)
        self.timezone_name = source_config.get("timezone_name")
        # 站址与天线安装方位：仅用于记录与追溯，不参与方位换算
        self.station_latitude_deg = source_config.get("station_latitude_deg")
        self.station_longitude_deg = source_config.get("station_longitude_deg")
        self.install_heading_deg = source_config.get("install_heading_deg")
        self.beamwidth_deg = source_config.get("antenna_beamwidth_deg")
        self.range_resolution_m = source_config.get("range_resolution_m")

    # -- 读取 ---------------------------------------------------------------

    def iter_files(self, input_path: str) -> List[str]:
        if os.path.isdir(input_path):
            candidate = os.path.join(input_path, "CutDataTarget.csv")
            return [candidate] if os.path.exists(candidate) else []
        return [input_path]

    def iter_rows(self, path: str) -> Iterator[Tuple[int, Dict[str, str]]]:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, start=1):
                yield index, row

    # -- 主流程 -------------------------------------------------------------

    def run(self, input_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """返回 (标准记录, 拒绝记录, 处理日志行)。"""
        records: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        log: List[str] = []

        for path in self.iter_files(input_path):
            if not os.path.exists(path):
                log.append(f"radar_cutdata: 找不到 {path}")
                continue
            file_records = 0
            file_rejected = 0
            for row_index, row in self.iter_rows(path):
                record = self._build_record(path, row_index, row)
                if record is None:
                    rejected.append(
                        self._rejected(
                            path,
                            row_index,
                            "invalid_or_missing_field_value",
                            {k: v for k, v in row.items() if v},
                        )
                    )
                    file_rejected += 1
                    continue
                records.append(record)
                file_records += 1
            log.append(
                f"radar_cutdata: {os.path.basename(path)} 记录 {file_records} 拒绝 {file_rejected}"
            )

        log.append(
            f"radar_cutdata: 输入行 {len(records) + len(rejected)}，输出 {len(records)}，拒绝 {len(rejected)}"
        )
        return records, rejected, log

    # -- 内部实现 -----------------------------------------------------------

    def _rejected(
        self,
        path: str,
        row_index: int,
        reason: str,
        raw: Any,
    ) -> Dict[str, Any]:
        from .validation import reject_reason_to_issue

        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "session_id": self.session_id,
            "record_id": schema.make_record_id(
                self.source_id, path, row_index, session_id=self.session_id
            ),
            "source_file": path,
            "source_row": row_index,
            "reason": reason,
            "issue": reject_reason_to_issue(reason),
            "raw_record": raw,
        }

    def _field(self, row: Dict[str, str], key: str, default: str) -> Optional[str]:
        column = self.mapping.get(key, default)
        value = (row.get(column) or "").strip()
        return value or None

    @staticmethod
    def _to_float(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _build_record(
        self,
        path: str,
        row_index: int,
        row: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        target_type = self._field(row, "target_type", "目标类型")
        target_id = self._field(row, "target_id", "目标编号")
        bearing_deg = self._to_float(self._field(row, "bearing", "目标方位(°)"))
        range_m = self._to_float(self._field(row, "range", "目标距离(米)"))
        start_time = self._field(row, "start_time", "起始时间(帧)")
        end_time = self._field(row, "end_time", "截止时间(帧)")

        if target_type is None or target_id is None:
            return None
        if bearing_deg is None or range_m is None:
            return None
        if start_time is None:
            return None
        if range_m < 0.0:
            return None

        # 方位合法区间 [0, 360)
        if not (0.0 <= bearing_deg < 360.0):
            return None

        time_block = time_fields.time_fields_for_csv(
            state_time_raw=start_time,
            timezone_known=self.timezone_known,
            timezone_name=self.timezone_name,
        )

        record = schema.new_record(
            source_id=self.source_id,
            source_type=self.source_type,
            session_id=self.session_id,
            local_track_id=target_id,
            state_time_raw=time_block["state_time_raw"],
            state_time_utc=time_block["state_time_utc"],
            time_basis=time_block["time_basis"],
            time_domain=time_block["time_domain"],
            time_precision=time_block["time_precision"],
            receive_time_utc=None,
            receive_time_source=None,
            position_type=schema.POSITION_TYPE_RANGE_BEARING,
            frame_id=self.frame_id,
            position={
                "range_m": range_m,
                "bearing_rad": math.radians(bearing_deg),
                "bearing_reference": schema.REFERENCE_TRUE,
            },
            speed_mps=None,
            course_rad=None,
            course_reference=None,
            motion_reference=None,
            heading_rad=None,
            tracking_status=TARGET_TYPE_TRACKING_STATUS.get(target_type),
            navigation_status=None,
            source_file=path,
            source_row=row_index,
            raw_record={k: v for k, v in row.items() if v},
        )

        # -- 扩展字段 --------------------------------------------------------
        record["radar_target_type"] = target_type
        record["radar_measurement_kind"] = TARGET_TYPE_MEASUREMENT_KIND.get(
            target_type, "unknown"
        )
        # 记录来源格式。source_type 保持 "radar"（与真值评测的 sensor 分桶一致），
        # 用本字段区分具体来源，便于跨数据集追溯。
        record["radar_source_format"] = "mtdsp_cutdata"
        record["radar_bearing_deg"] = bearing_deg
        record["radar_range_m"] = range_m
        record["radar_frame_index"] = self._to_float(
            self._field(row, "frame_index", "帧序号")
        )
        record["radar_frame_start_time_raw"] = start_time
        record["radar_frame_end_time_raw"] = end_time
        # 帧率降采样说明：
        # CutDataTarget 的帧号与 IF/IQ 脉冲头元数据的帧号同源，但两者帧率不同
        # （IF 帧间隔 2-3 s，Cut 帧间隔 2-5 s）。实测 Cut 的 123 帧只对应
        # 107 个唯一时间戳，其中 16 组为**相邻两帧共享同一时间戳且内容完全
        # 相同** —— 这是雷达帧率高于检测输出率导致的正常降采样，不是数据错误。
        # 因此同一 (目标, 时间戳) 出现两条记录属正常，frame_index 区分二者。
        record["radar_frame_time_shared"] = True

        # 站址与安装方位（仅记录，不参与换算）
        record["radar_station_latitude_deg"] = self.station_latitude_deg
        record["radar_station_longitude_deg"] = self.station_longitude_deg
        record["radar_install_heading_deg"] = self.install_heading_deg
        record["radar_angle_correction_deg"] = self._to_float(
            self._field(row, "angle_correction", "角度修正(°)")
        )
        record["radar_range_correction_m"] = self._to_float(
            self._field(row, "range_correction", "距离修正(米)")
        )
        record["radar_antenna_beamwidth_deg"] = self.beamwidth_deg
        record["radar_range_resolution_m"] = self.range_resolution_m

        # 检测框（外接矩形）
        box = self._detection_box(row, target_type)
        if box:
            record["radar_detection_box"] = box

        # 光电匹配
        optical_match_id = self._field(row, "optical_match_id", "匹配编号")
        optical_match_time = self._field(row, "optical_match_time", "光电匹配时间")
        record["optical_match_id"] = optical_match_id
        record["optical_match_time_raw"] = optical_match_time
        record["is_slice_target"] = self._field(row, "is_slice_target", "是否为切片目标")

        # 角度修正仅记录，不叠加（已核验：叠加会使站址反算残差从 14 m 恶化到 400 m）
        record["radar_angle_correction_applied"] = False

        if not self.timezone_known:
            schema.add_issue(record, schema.ISSUE_TIMEZONE_UNKNOWN)

        validation.validate_common(record)
        validation.assess_validity(record)
        return record

    def _detection_box(
        self, row: Dict[str, str], target_type: str
    ) -> Optional[Dict[str, Any]]:
        """外接矩形。

        `CDT` 行的外接矩形是**雷达检测框**（方位/距离 100% 落在框内）；
        `AT`/`RT` 行的是**光电视场范围**（方位从不落在框内）。两者语义不同，
        因此必须随目标类型标注，避免下游误用。
        """
        bearing_min = self._to_float(
            self._field(row, "box_bearing_min", "[外接矩形]起始角度(°)")
        )
        bearing_max = self._to_float(
            self._field(row, "box_bearing_max", "[外接矩形]结束角度(°)")
        )
        range_min = self._to_float(
            self._field(row, "box_range_min", "[外接矩形]起始距离(米)")
        )
        range_max = self._to_float(
            self._field(row, "box_range_max", "[外接矩形]结束距离(米)")
        )
        if None in (bearing_min, bearing_max, range_min, range_max):
            return None
        return {
            "bearing_min_deg": bearing_min,
            "bearing_max_deg": bearing_max,
            "range_min_m": range_min,
            "range_max_m": range_max,
            "kind": "radar_detection" if target_type == "CDT" else "optical_field_of_view",
        }
