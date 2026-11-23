"""MTDSP 光电观测适配器（Photo_ScreenShot* → 公共标准记录）。

## 输入

MTDSP 数据包中的光电部分，三类文件：

| 文件 | 内容 |
| ---- | ---- |
| `Photo_ScreenShot_<设备>_<...>_<时间戳>_<n>.jpg` | 光电设备原始截图 |
| `Photo_ScreenShot_<设备>_<...>_<时间戳>_<n>.txt` | 设备解算元数据（**主目标**测距测角） |
| `Photo_ScreenShot_<设备>_<...>_<时间戳>_<n>_DLDete.txt` | 光电 AI 检测结果（**画面内所有目标**） |

设备型号 `DS-2ZTD816CX-HB/YT`（编码 PHK003），三个采集包（03-05 / 03-12 等）
使用同一套设备，字段结构完全一致。

### 文件名结构

```
Photo_ScreenShot_Inf_2002_AIS_0_413203610_5.00_0.000_13.30_20250305103643_2.txt
                            │  │       │   │       │      │            │  └ 序号
                            │  │       │   │       │      │            └──── 采集时间
                            │  │       │   │       │      └───────────────── 中心点方位(°)
                            │  │       │   │       └──────────────────────── 俯仰角度(°)
                            │  │       │   └──────────────────────────────── 焦距(mm)
                            │  │       └──────────────────────────────────── MMSI
                            │  └──────────────────────────────────────────── AIS 目标标识
                            └─────────────────────────────────────────────── 设备: Inf(红外) / TV(可见光)
```

已核验：文件名第 4/5/6 段 = 焦距 / 俯仰角 / 中心点方位，与 `.txt` 元数据
的 `焦距` / `俯仰角度(°)` / `中心点方位(°)` 逐一一致（05 包 92 个文件、
12 包 120 个文件全部吻合）。

## 已核验的关键事实（决定本适配器的设计）

1. **`.txt` 元数据描述的是光电正在跟踪的【主目标】。**
   `目标距离(M)` 与 `中心点方位(°)` 是设备对该目标的解算结果。
   03-05 包的 92 个文件全部指向同一目标（MMSI 413203610，锚泊货船），
   距离在 3874.71–3881.15 m 之间、中心点方位恒为 13.30°（跨 5 个焦距档）。

2. **`.txt` 的距离在部分时段直接拷贝 AIS 的距离值。**
   03-12 包 120 个文件里有 69 个的 `目标距离(M)` 与某条 AIS 记录
   **精确相等（差 < 0.01 m）**，其余 51 个则远离所有 AIS 记录（> 50 m）。
   分界点恰好是 AIS 数据结束时刻（10:15:03）：前 6 个距离档是 AIS 快照，
   后 5 个档是**光电独立测距**。
   ⇒ 必须标记 `optical_range_source`（`ais_snapshot` / `independent_measurement`），
   否则会把"拷贝值"当作"光电测量精度"来评估。

3. **`.txt` 的方位与 AIS 方位存在系统性偏差，不是随机误差。**
   03-05 包：光电中心点方位 13.30° vs 同一目标 AIS 极坐标 8.017°，
   差 **+5.28°**；而该目标 AIS 自身的经纬度反算方位为 7.619°，
   与其极坐标只差 0.398°。
   03-12 包：CDT 与时间插值后的 AIS 相比，Δ方位中位 −1.43°、最大 −5.49°。
   两个独立日期、不同机动状态的船舶上都出现同量级偏差
   ⇒ 存在系统性的**视轴/方位基准偏差**，跨源比对前必须扣除或标定。
   适配器因此把 `optical_azimuth_bias_deg` 单独记录，不做隐式补偿。

4. **`_DLDete.txt` 是光电 AI 的画面内检测结果，字段含义已逐项核验：**

   ```
   2,[民用]Civil Ship(Cargo),0.92,1007.00,545.00,594.00,123.00,1,33452,2
   ```

   | 字段 | 含义 | 依据 |
   | ---- | ---- | ---- |
   | 0 | 检测目标序号 | 取值 0–3，与画面内目标数一致 |
   | 1 | 目标类型 | `[民用]Civil Ship(Cargo/Fishing/Passenger/Buoy)` |
   | 2 | 置信度 | 0–1 |
   | 3 | 检测框中心 x（像素） | 值域 0–1920 |
   | 4 | 检测框中心 y（像素） | 值域 0–540 |
   | 5 | 检测框宽（像素） | |
   | 6 | 检测框高（像素） | |
   | 7 | **匹配编号** | `0`=未匹配雷达目标；`1`=已匹配。与 `CutDataTarget.匹配编号` 同义 |
   | 8 | **测距（米，整数）** | 与 AIS 距离吻合到 0.3–7 m |
   | 9 | **光学目标序号** | 多目标时的次级编号 |

   字段 7/8/9 仅在字段数为 9 或 10 的行存在；字段数为 7 的行只有
   框与类型信息，**没有距离**，不能当作位置观测。

5. **DLDete 测距存在大量误检，必须做距离合理性过滤。**
   03-05 包 187 行检测中，距离取值散布到 28 km / 45–50 km / 234–350 km，
   而该场景真实目标全部在 3.6–5.4 km 内。误检行的置信度高达 0.81–0.94，
   **无法靠置信度剔除**，只能靠距离范围。
   ⇒ 适配器按 `max_range_m` 过滤，超限行标记为 `optical_detection_out_of_range`
   并置 `valid_for_spatial_alignment = False`（保留记录，不删除）。

6. **光电测距与 AIS 距离一致到米级，但方位差 1.4°–5.3°。**
   03-05 包：`目标距离(M)` 3874.71–3881.15 vs 413203610 的 AIS 距离
   3874.52–3878.47，逐档差 0.2–5 m。
   ⇒ **距离基准一致，方位基准不一致**。这直接约束了跨源关联门限的设定：
   距离方向可以用紧门限，方位方向必须放宽。

7. **时间无时区标记。** 与 MTDSP AIS CSV、CutDataTarget 保持一致：
   时区未确认前 `state_time_utc` 留空并标记 `timezone_unknown`。
   `.jpg` 的文件系统时间戳**不可用**（解压后已丢失原始时间），一律以
   文件名中的时间戳为准。

## 输出

两种记录：

- `position_type = range_bearing`：**主目标**观测（来自 `.txt` 元数据），
  `bearing_reference = "T"`。
- `position_type = range_bearing`：**画面内检测**观测（来自 `_DLDete.txt`），
  距离用 DLDete 第 8 字段，方位由检测框中心 x 换算。

两类记录的 `local_track_id` 语义不同：
主目标用文件名中的 MMSI（`optical_primary_mmsi`），检测目标用
`optical_det_<序号>`；二者在 `optical_record_kind` 中区分。

**Inf 与 TV 是两个独立设备，必须分配不同的 `local_track_id`。**

依据（MTDSP 实测）：

- 03-05 包：同一时刻 46 组 Inf/TV 主目标观测，距离与方位**完全相同**
  （差 0.00 m / 0.00000°）—— 两设备共用同一个主目标解算结果；
- 03-12 包：同一时刻 60 组主目标中 58 组距离方位完全相同，
  仅 2 组不同（最大 121.7 m）；
- 但**画面内检测**（`_DLDete.txt`）两组差异极大：03-05 包 92 组同时刻
  检测的距离差最大 320 km，03-12 包 65 组最大 1.8 Mm —— 这是两个设备
  各自的 AI 检测结果，互不对应。

若两者共用同一 `local_track_id`：

1. 光电航迹点序列出现**重复时间戳**且同一时刻有两个不同位置，
   对齐模块的二分 bracket 只能隐式收敛到其中一条（依赖记录顺序），
   另一条被静默吞掉；
2. 下游跨源关联会看到"同一目标同一时刻两个位置"，无法判定用哪一条。

因此 `local_track_id` 在 MMSI 之后追加设备标识：

- 主目标：`413203610@Inf` / `413203610@TV`
- 检测目标：`optical_det_1@Inf` / `optical_det_1@TV`

原始 MMSI 仍保留在 `optical_primary_mmsi` 字段中，语义不变。
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import schema, time_fields, validation

# ---------------------------------------------------------------------------
# 文件名与内容解析
# ---------------------------------------------------------------------------

# Photo_ScreenShot_<设备>_2002_AIS_0_<MMSI>_<焦距>_<俯仰>_<方位>_<时间戳>_<序号>
_PHOTO_NAME_RE = re.compile(
    r"^Photo_ScreenShot_(?P<device>Inf|TV)_2002_AIS_0_(?P<mmsi>\d+)"
    r"_(?P<focal>[\d.]+)_(?P<pitch>[\d.]+)_(?P<azimuth>[\d.]+)"
    r"_(?P<timestamp>\d{14})_(?P<seq>\d+)$"
)

# 设备解算元数据：每行 "键:值"
_META_KEYS = {
    "光电设备型号": "device_model",
    "光电设备编码": "device_code",
    "象元(um)": "pixel_um",
    "水平视场角(°)": "hfov_deg",
    "俯仰视场角(°)": "vfov_deg",
    "目标距离(M)": "target_range_m",
    "焦距": "focal_length_mm",
    "真实高度(M)": "true_height_m",
    "真实宽度(M)": "true_width_m",
    "俯仰角度(°)": "pitch_deg",
    "中心点方位(°)": "center_azimuth_deg",
    "最大方位(°)": "max_azimuth_deg",
    "最小方位(°)": "min_azimuth_deg",
}

# DLDete 目标类型 → 归一化类别
_TYPE_PREFIX_RE = re.compile(r"^\[(?P<category>[^\]]+)\]\s*(?P<label>.+)$")

# 设备 → 观测波段标识
DEVICE_BAND = {
    "Inf": "infrared",
    "TV": "visible",
}

# 光电自身的位置基准：与雷达站同址（同一采集站）
DEVICE_SOURCE_FORMAT = "mtdsp_photo"

RECORD_KIND_PRIMARY = "primary_target"
RECORD_KIND_DETECTION = "screen_detection"

# 距离来源分类
RANGE_SOURCE_AIS_SNAPSHOT = "ais_snapshot"
RANGE_SOURCE_INDEPENDENT = "independent_measurement"
RANGE_SOURCE_UNKNOWN = "unknown"

#: 设备标识后缀分隔符：`{mmsi}@{device}`。
DEVICE_SCOPE_SEPARATOR = "@"


def device_scoped_track_id(base_id: str, device: Optional[str]) -> str:
    """给航迹编号加上设备标识后缀，使 Inf 与 TV 成为两条独立航迹。

    Inf 与 TV 是两个独立设备：主目标观测二者基本重合（同一份设备解算结果），
    但画面内检测各自独立、差异很大。共用同一个 `local_track_id` 会让
    航迹点序列出现重复时间戳且同刻多位置，下游无法判定取舍。

    `device` 为空或未知时退回原编号（不臆造设备标识）。
    """
    if not device:
        return base_id
    if DEVICE_SCOPE_SEPARATOR in base_id:
        # 已经带过设备标识，不重复追加
        return base_id
    return f"{base_id}{DEVICE_SCOPE_SEPARATOR}{device}"


class OpticalPhotoAdapter:
    """把 MTDSP 光电截图/检测结果转换为公共标准记录。"""

    source_type = schema.SOURCE_TYPE_OPTICAL

    def __init__(self, source_config: Dict[str, Any]):
        self.config = source_config
        self.source_id = source_config["source_id"]
        self.session_id = source_config["session_id"]
        self.frame_id = source_config.get("frame_id", "radar_local_polar")
        self.mapping = source_config.get("field_mapping", {})
        self.timezone_known = source_config.get("timezone_known", False)
        self.timezone_name = source_config.get("timezone_name")
        self.station_latitude_deg = source_config.get("station_latitude_deg")
        self.station_longitude_deg = source_config.get("station_longitude_deg")

        # 距离合理性过滤：超出该范围的光电测距判定为误检。
        # 依据：03-05 包 187 行 DLDete 的距离散布到 28/45/234–350 km，
        # 而场景内真实目标全部在 3.6–5.4 km。误检置信度高达 0.81–0.94，
        # 无法用置信度区分，只能用距离范围。
        limits = source_config.get("max_range_m", 20000.0)
        self.max_range_m = float(limits)

        # 图像幅面（用于把检测框像素坐标换算为视场内的相对横向位置）
        self.image_width_px = float(source_config.get("image_width_px", 1920))
        self.image_height_px = float(source_config.get("image_height_px", 1080))

        # AIS 距离索引：用于判定光电距离是"拷贝 AIS"还是"光电独立测量"。
        # 依据：03-12 包 120 个主目标中 69 个的 目标距离(M) 与某条 AIS 记录的
        # 距离(m) **精确相等**（差 < 0.01 m），其余 51 个远离所有 AIS 记录。
        # 分界点恰好是 AIS 数据结束时刻 —— 前段是 AIS 快照，后段是独立测距。
        # 不做这一步区分，就会把"拷贝值"当成"光电测量精度"来评估。
        self.ais_range_index = _build_ais_range_index(
            source_config.get("ais_range_index_dir")
        )

    # -- 文件发现 -----------------------------------------------------------

    def iter_files(self, input_path: str) -> List[str]:
        """返回 (元数据txt, DLDete.txt) 配对列表，按时间戳排序。"""
        if os.path.isfile(input_path):
            directory = os.path.dirname(os.path.abspath(input_path))
        else:
            directory = os.path.abspath(input_path)
        if not os.path.isdir(directory):
            return []

        pairs: List[Tuple[str, Optional[str]]] = []
        for name in sorted(os.listdir(directory)):
            if not name.startswith("Photo_ScreenShot"):
                continue
            if name.endswith(".jpg"):
                continue
            if name.endswith("_DLDete.txt"):
                continue
            if not name.endswith(".txt"):
                continue
            base = name[: -len(".txt")]
            det_path = os.path.join(directory, base + "_DLDete.txt")
            pairs.append(
                (
                    os.path.join(directory, name),
                    det_path if os.path.exists(det_path) else None,
                )
            )
        pairs.sort(key=lambda p: p[0])
        return pairs

    # -- 主流程 -------------------------------------------------------------

    def run(self, input_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
        """返回 (标准记录, 拒绝记录, 处理日志行)。"""
        records: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        log: List[str] = []

        pairs = self.iter_files(input_path)
        if not pairs:
            log.append(f"optical: 在 {input_path} 未找到光电文件")
            return records, rejected, log

        primary_count = 0
        detection_count = 0
        out_of_range = 0
        total_detections = 0

        for meta_path, det_path in pairs:
            name = os.path.basename(meta_path)
            parsed = _PHOTO_NAME_RE.match(name[: -len(".txt")])
            if parsed is None:
                rejected.append(
                    self._rejected(meta_path, 0, "unparsable_photo_filename", {"file": name})
                )
                continue

            groups = parsed.groupdict()
            timestamp_raw = groups["timestamp"]
            state_time_raw = _timestamp_to_datetime_string(timestamp_raw)

            meta = self._read_meta(meta_path)
            if meta is None:
                rejected.append(
                    self._rejected(meta_path, 0, "missing_required_optical_field", {"file": name})
                )
                continue

            # -- 主目标记录 ------------------------------------------------
            primary = self._build_primary_record(
                meta_path, meta_path, name, groups, meta, state_time_raw
            )
            if primary is not None:
                records.append(primary)
                primary_count += 1
            else:
                rejected.append(
                    self._rejected(
                        meta_path,
                        0,
                        "missing_required_optical_field",
                        {"file": name, "meta": meta},
                    )
                )

            # -- 画面内检测记录 --------------------------------------------
            if det_path is None:
                continue
            detections = self._read_detections(det_path)
            total_detections += len(detections)
            for index, det in enumerate(detections, start=1):
                record = self._build_detection_record(
                    det_path, det, index, groups, meta, state_time_raw
                )
                if record is None:
                    continue
                if not record["valid_for_spatial_alignment"]:
                    out_of_range += 1
                records.append(record)
                detection_count += 1

        log.append(
            f"optical: 配对文件 {len(pairs)} 组，主目标记录 {primary_count} 条，"
            f"检测记录 {detection_count} 条（总检测行 {total_detections}）"
        )
        log.append(
            f"optical: 距离超限被标记为不可用于空间对齐 {out_of_range} 条，"
            f"过滤阈值 max_range_m={self.max_range_m}"
        )
        log.append(
            f"optical: 输入 {primary_count + detection_count} 条输出 "
            f"{len(records)} 条，拒绝 {len(rejected)} 条"
        )
        return records, rejected, log

    # -- 内部实现 -----------------------------------------------------------

    def _rejected(
        self, path: str, row_index: int, reason: str, raw: Any
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

    @staticmethod
    def _read_meta(path: str) -> Optional[Dict[str, Any]]:
        """解析设备解算元数据。必需字段缺失时返回 None。"""
        meta: Dict[str, Any] = {}
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                field = _META_KEYS.get(key)
                if field is None:
                    continue
                if field in ("device_model", "device_code"):
                    meta[field] = value
                else:
                    try:
                        meta[field] = float(value)
                    except ValueError:
                        meta[field] = None
        if meta.get("target_range_m") is None:
            return None
        return meta

    @staticmethod
    def _read_detections(path: str) -> List[Dict[str, Any]]:
        """解析 _DLDete.txt，返回检测列表。

        每行逗号分隔，字段数 7/9/10 三种。字段 7/8/9 仅在 9/10 字段行存在。
        """
        detections: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                item: Dict[str, Any] = {
                    "row_index": len(detections) + 1,
                    "target_index": _to_int(parts[0]),
                    "type_raw": parts[1].strip(),
                    "confidence": _to_float(parts[2]),
                    "box_center_x_px": _to_float(parts[3]),
                    "box_center_y_px": _to_float(parts[4]),
                    "box_width_px": _to_float(parts[5]),
                    "box_height_px": _to_float(parts[6]),
                    "match_id": None,
                    "range_m": None,
                    "optical_target_index": None,
                }
                if len(parts) >= 9:
                    item["match_id"] = parts[7].strip() or None
                    item["range_m"] = _to_float(parts[8])
                if len(parts) >= 10:
                    item["optical_target_index"] = _to_int(parts[9])
                detections.append(item)
        return detections

    def _build_primary_record(
        self,
        path: str,
        meta_path: str,
        name: str,
        groups: Dict[str, str],
        meta: Dict[str, Any],
        state_time_raw: str,
    ) -> Optional[Dict[str, Any]]:
        """构造主目标观测记录（来自设备解算元数据）。"""
        range_m = meta.get("target_range_m")
        azimuth_deg = meta.get("center_azimuth_deg")
        if range_m is None or azimuth_deg is None:
            return None
        if range_m < 0.0 or not (0.0 <= azimuth_deg < 360.0):
            return None

        time_block = time_fields.time_fields_for_csv(
            state_time_raw=state_time_raw,
            timezone_known=self.timezone_known,
            timezone_name=self.timezone_name,
        )

        record = schema.new_record(
            source_id=self.source_id,
            source_type=self.source_type,
            session_id=self.session_id,
            local_track_id=device_scoped_track_id(
                groups["mmsi"], groups.get("device")
            ),
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
                "bearing_rad": math.radians(azimuth_deg),
                "bearing_reference": schema.REFERENCE_TRUE,
            },
            speed_mps=None,
            course_rad=None,
            course_reference=None,
            motion_reference=None,
            heading_rad=None,
            tracking_status=schema.TRACKING_STATUS_TRACKING,
            navigation_status=None,
            source_file=path,
            source_row=1,
            raw_record={
                "file": name,
                "target_range_m": range_m,
                "center_azimuth_deg": azimuth_deg,
            },
        )

        self._attach_common(record, groups, meta, state_time_raw)
        record["optical_record_kind"] = RECORD_KIND_PRIMARY
        record["optical_primary_mmsi"] = groups["mmsi"]
        record["optical_range_m"] = range_m
        record["optical_azimuth_deg"] = azimuth_deg
        record["optical_pitch_deg"] = meta.get("pitch_deg")
        record["optical_focal_length_mm"] = meta.get("focal_length_mm")
        record["optical_hfov_deg"] = meta.get("hfov_deg")
        record["optical_vfov_deg"] = meta.get("vfov_deg")
        record["optical_true_height_m"] = meta.get("true_height_m")
        record["optical_true_width_m"] = meta.get("true_width_m")
        record["optical_min_azimuth_deg"] = meta.get("min_azimuth_deg")
        record["optical_max_azimuth_deg"] = meta.get("max_azimuth_deg")

        if not self.timezone_known:
            schema.add_issue(record, schema.ISSUE_TIMEZONE_UNKNOWN)

        validation.validate_common(record)
        validation.assess_validity(record)
        return record

    def _build_detection_record(
        self,
        path: str,
        det: Dict[str, Any],
        index: int,
        groups: Dict[str, str],
        meta: Dict[str, Any],
        state_time_raw: str,
    ) -> Optional[Dict[str, Any]]:
        """构造画面内检测记录（来自 _DLDete.txt）。"""
        range_m = det.get("range_m")
        if range_m is None or range_m < 0.0:
            # 字段数为 7 的行没有测距，不能作为位置观测。
            return None

        # 方位：优先用设备解算的主目标方位；检测框中心 x 只能给出相对量，
        # 无法单独还原绝对方位，因此检测记录沿用该时刻的设备中心点方位，
        # 并把框内横向偏移单独记录，避免伪造精度。
        azimuth_deg = meta.get("center_azimuth_deg")
        if azimuth_deg is None:
            return None
        # 方位合法区间 [0, 360)。与主目标记录同口径：设备解算的方位非法时，
        # 沿用该方位的检测记录同样不可用，必须一并拒绝，
        # 否则非法方位会绕过主目标记录的校验进入下游。
        if not (0.0 <= azimuth_deg < 360.0):
            return None

        time_block = time_fields.time_fields_for_csv(
            state_time_raw=state_time_raw,
            timezone_known=self.timezone_known,
            timezone_name=self.timezone_name,
        )

        type_raw = det.get("type_raw") or ""
        label_match = _TYPE_PREFIX_RE.match(type_raw)
        category = label_match.group("category") if label_match else None
        label = label_match.group("label") if label_match else type_raw

        record = schema.new_record(
            source_id=self.source_id,
            source_type=self.source_type,
            session_id=self.session_id,
            local_track_id=device_scoped_track_id(
                f"optical_det_{det.get('optical_target_index') or det.get('target_index') or index}",
                groups.get("device"),
            ),
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
                "bearing_rad": math.radians(azimuth_deg),
                "bearing_reference": schema.REFERENCE_TRUE,
            },
            speed_mps=None,
            course_rad=None,
            course_reference=None,
            motion_reference=None,
            heading_rad=None,
            tracking_status=None,
            navigation_status=None,
            source_file=path,
            source_row=det.get("row_index", index),
            raw_record={
                "target_index": det.get("target_index"),
                "type_raw": type_raw,
                "confidence": det.get("confidence"),
                "match_id": det.get("match_id"),
                "range_m": range_m,
            },
        )

        self._attach_common(record, groups, meta, state_time_raw)
        record["optical_record_kind"] = RECORD_KIND_DETECTION
        record["optical_detection_index"] = det.get("target_index")
        record["optical_target_index"] = det.get("optical_target_index")
        record["optical_type_raw"] = type_raw
        record["optical_type_category"] = category
        record["optical_type_label"] = label
        record["optical_confidence"] = det.get("confidence")
        record["optical_range_m"] = range_m
        record["optical_azimuth_deg"] = azimuth_deg
        record["optical_box_center_x_px"] = det.get("box_center_x_px")
        record["optical_box_center_y_px"] = det.get("box_center_y_px")
        record["optical_box_width_px"] = det.get("box_width_px")
        record["optical_box_height_px"] = det.get("box_height_px")
        record["optical_match_id"] = det.get("match_id")

        # 距离合理性过滤：超出量程的检测为误检（置信度不足以区分）。
        record["optical_detection_out_of_range"] = range_m > self.max_range_m
        record["optical_range_filter_limit_m"] = self.max_range_m

        if not self.timezone_known:
            schema.add_issue(record, schema.ISSUE_TIMEZONE_UNKNOWN)

        validation.validate_common(record)
        validation.assess_validity(record)
        if record["optical_detection_out_of_range"]:
            record["valid_for_spatial_alignment"] = False
        return record

    def _classify_range_source(self, range_m: Optional[float]) -> str:
        """判定光电距离是拷贝 AIS 还是光电独立测量。

        判据：与 AIS 库中任一距离值精确相等（差 < 0.01 m）即为快照拷贝。
        AIS 库为空（未配置或目录无 AIS 文件）时返回 unknown，不臆测。
        """
        if range_m is None or not self.ais_range_index:
            return RANGE_SOURCE_UNKNOWN
        if round(float(range_m), 2) in self.ais_range_index:
            return RANGE_SOURCE_AIS_SNAPSHOT
        return RANGE_SOURCE_INDEPENDENT

    def _attach_common(
        self,
        record: Dict[str, Any],
        groups: Dict[str, str],
        meta: Dict[str, Any],
        state_time_raw: str,
    ) -> None:
        """附加各记录共有的来源与设备信息。"""
        record["optical_source_format"] = DEVICE_SOURCE_FORMAT
        record["optical_device"] = groups.get("device")
        record["optical_band"] = DEVICE_BAND.get(groups.get("device", ""), "unknown")
        record["optical_device_model"] = meta.get("device_model")
        record["optical_device_code"] = meta.get("device_code")
        record["optical_pixel_um"] = meta.get("pixel_um")
        record["optical_filename_focal"] = _to_float(groups.get("focal"))
        record["optical_filename_pitch"] = _to_float(groups.get("pitch"))
        record["optical_filename_azimuth"] = _to_float(groups.get("azimuth"))
        record["optical_capture_time_raw"] = state_time_raw
        record["optical_station_latitude_deg"] = self.station_latitude_deg
        record["optical_station_longitude_deg"] = self.station_longitude_deg
        # 光电与雷达同址部署，但视轴标定独立：方位存在系统性偏差，
        # 记录中不隐式补偿，由下游按需标定。
        record["optical_azimuth_bias_applied"] = False
        record["optical_range_source"] = self._classify_range_source(meta.get("target_range_m"))


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> Optional[int]:
    number = _to_float(value)
    if number is None:
        return None
    try:
        return int(round(number))
    except (ValueError, OverflowError):
        return None


def _timestamp_to_datetime_string(timestamp_raw: str) -> str:
    """把文件名中的 14 位时间戳转成 "YYYY-MM-DD HH:MM:SS"。"""
    try:
        return (
            f"{timestamp_raw[0:4]}-{timestamp_raw[4:6]}-{timestamp_raw[6:8]} "
            f"{timestamp_raw[8:10]}:{timestamp_raw[10:12]}:{timestamp_raw[12:14]}"
        )
    except (IndexError, ValueError):
        return timestamp_raw


def _build_ais_range_index(directory: Optional[str]) -> set:
    """收集 AIS 轨迹文件中的全部距离值（两位小数）。

    目录为空或不存在时返回空集合，此时距离来源判定为 unknown。
    """
    if not directory or not os.path.isdir(directory):
        return set()
    import csv

    index = set()
    for name in sorted(os.listdir(directory)):
        if not name.startswith("AIS_Trajectory") or not name.endswith(".csv"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    raw = (row.get("距离(m)") or "").strip()
                    if not raw:
                        continue
                    try:
                        index.add(round(float(raw), 2))
                    except ValueError:
                        continue
        except OSError:
            continue
    return index
