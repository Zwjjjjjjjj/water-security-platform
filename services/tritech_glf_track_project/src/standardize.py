from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd


def _load_json(path: Path | None):
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def assign_event_labels(tracks: pd.DataFrame, summary: pd.DataFrame, profile: dict | None):
    """Assign optional experiment-event labels to candidate tracks.

    Important: these are external log-time associations, not embedded GLF labels.
    """
    if not profile or not profile.get("events") or tracks.empty or summary.empty:
        tracks = tracks.copy()
        tracks["target_label"] = "UNKNOWN"
        tracks["label_basis"] = "none"
        return tracks, {}

    s = summary.copy()
    s["start_time_utc"] = pd.to_datetime(s["start_time_utc"], utc=True, errors="coerce")
    s["end_time_utc"] = pd.to_datetime(s["end_time_utc"], utc=True, errors="coerce")
    s = s[s["selected_track_id"].astype(str).str.len() > 0].copy()
    used = set()
    mapping = {}
    label_counts = {}

    events = profile["events"]
    for idx, ev in enumerate(events):
        et = pd.to_datetime(ev["event_time_utc"], utc=True)
        max_delay = float(ev.get("max_start_delay_s", 30))
        end = et + pd.to_timedelta(max_delay, unit="s")
        count = int(ev.get("count", 1))
        candidates = s[
            (s["start_time_utc"] >= et) &
            (s["start_time_utc"] <= end) &
            (~s["selected_track_id"].isin(used))
        ].copy()
        if candidates.empty:
            continue
        # Prefer long, displaced, approaching tracks with high quality score.
        candidates["rank_score"] = (
            candidates["quality_score"].fillna(0) * 3.0 +
            np.minimum(candidates["net_displacement_m"].fillna(0), 30) / 10.0 +
            np.minimum(candidates["hits"].fillna(0), 100) / 100.0 +
            (candidates["range_change_m"].fillna(0) < -2.0).astype(float)
        )
        candidates = candidates.sort_values(["rank_score", "start_time_utc"], ascending=[False, True]).head(count)
        for _, row in candidates.iterrows():
            tid = str(row["selected_track_id"])
            used.add(tid)
            base = ev["label"]
            label_counts[base] = label_counts.get(base, 0) + 1
            if count > 1:
                label = f"{base}_{label_counts[base]}"
            else:
                label = base
            mapping[tid] = label

    out = tracks.copy()
    out["target_label"] = out["track_id"].map(mapping).fillna("UNKNOWN")
    out["label_basis"] = np.where(
        out["target_label"].ne("UNKNOWN"),
        profile.get("label_basis", "external_event_time_association"),
        "none"
    )
    return out, mapping


def standardize_tracks(raw_tracks_csv: Path, summary_csv: Path, out_csv: Path,
                       sensor_cfg: dict, profile_path: Path | None = None):
    tracks = pd.read_csv(raw_tracks_csv)
    summary = pd.read_csv(summary_csv)
    profile = _load_json(profile_path)
    tracks, mapping = assign_event_labels(tracks, summary, profile)

    tracks.insert(2, "sensor_id", sensor_cfg.get("sensor_id", "Gemini"))
    tracks["coordinate_frame"] = sensor_cfg.get("coordinate_frame", "SONAR_FAN")
    tracks["swath_orientation"] = sensor_cfg.get("swath_orientation", "Unknown")
    tracks["mount_location"] = sensor_cfg.get("mount_location", "Unknown")
    tracks["boresight_direction"] = sensor_cfg.get("boresight_direction", "Unknown")
    tracks["tilt_down_deg"] = sensor_cfg.get("tilt_down_deg", np.nan)
    tracks["sonar_depth_m"] = sensor_cfg.get("sonar_depth_m", np.nan)
    tracks["horizontal_xy_valid"] = bool(sensor_cfg.get("horizontal_xy_valid", False))
    tracks["enu_valid"] = bool(sensor_cfg.get("enu_valid", False))
    tracks["enu_invalid_reason"] = sensor_cfg.get("enu_invalid_reason", "")

    # A bounded quality/track-confidence value for downstream interfaces.
    hit_term = np.clip(tracks["hits"].astype(float) / 8.0, 0, 1)
    miss_term = np.exp(-tracks["misses"].astype(float) / 3.0)
    status_term = np.where(tracks["track_status"].eq("CONFIRMED"), 1.0, 0.5)
    base = tracks["track_quality_score"].fillna(0.0).astype(float)
    tracks["track_confidence"] = np.clip(0.50*base + 0.20*hit_term + 0.15*miss_term + 0.15*status_term, 0, 1)

    # Preserve clear ordering of interface fields.
    first = [
        "timestamp_utc", "frame_id", "sensor_id", "track_id", "target_label", "label_basis",
        "coordinate_frame", "swath_orientation", "mount_location", "boresight_direction",
        "tilt_down_deg", "sonar_depth_m", "measurement_available",
        "range_m", "bearing_deg", "fan_u_meas_m", "fan_v_meas_m",
        "fan_u_filt_m", "fan_v_filt_m", "du_mps", "dv_mps", "fan_speed_mps",
        "Puu_m2", "Puv_m2", "Pvv_m2", "Pdu_m2ps2", "Pdv_m2ps2",
        "track_confidence", "detection_score", "track_status", "hits", "misses", "age",
        "horizontal_xy_valid", "enu_valid", "enu_invalid_reason"
    ]
    cols = [c for c in first if c in tracks.columns] + [c for c in tracks.columns if c not in first]
    tracks = tracks[cols]
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    tracks.to_csv(out_csv, index=False)
    return tracks, mapping
