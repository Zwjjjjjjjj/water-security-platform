"""航迹关联单元测试：评分、修正、指派、加载规则。"""

from __future__ import annotations

import csv
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from association import associate as A


def _trk(tid, sensor, pts, speed=None, course=None):
    return A.AssocTrack(
        track_id=tid,
        sensor=sensor,
        points=list(pts),
        mean_speed=speed,
        mean_course=course,
        point_count=len(pts),
        raw_point_count=len(pts),
    )


def _line(t0, n, dt, e0, de, n0, dn):
    return [(t0 + i * dt, e0 + i * de, n0 + i * dn) for i in range(n)]


def test_rotated_points_identity_and_roundtrip():
    pts = [(0.0, 100.0, 200.0), (1.0, 110.0, 205.0)]
    assert A._rotated_points(pts, 0.0) == pts
    fwd = A._rotated_points(pts, 5.5)
    back = A._rotated_points(fwd, -5.5)
    for p, q in zip(pts, back):
        assert abs(p[1] - q[1]) < 1e-9
        assert abs(p[2] - q[2]) < 1e-9


def test_pair_score_identical_tracks_zero_median():
    pts = _line(0.0, 6, 5.0, 500.0, 1.0, 3800.0, 0.5)
    first = _trk("ais:1", "ais", pts, speed=4.0, course=100.0)
    second = _trk("ais:2", "ais", pts, speed=4.1, course=101.0)
    score = A.pair_score(first, second)
    assert score["ok"]
    assert score["median_m"] == 0.0
    assert score["course_diff_deg"] == pytest.approx(1.0)


def test_pair_score_insufficient_overlap():
    first = _trk("a", "ais", _line(0.0, 2, 5.0, 0.0, 1.0, 0.0, 1.0))
    second = _trk("b", "ais", _line(1000.0, 2, 5.0, 0.0, 1.0, 0.0, 1.0))
    score = A.pair_score(first, second)
    assert not score["ok"]
    assert score["reason"] == "insufficient_overlap"


def test_bias_correction_moves_score():
    # 同一几何，光电实测方位整体偏 +5.5°（bearing 域）：
    # ENU 数学旋转下实测 = 真值旋转 −5.5°（方位增 = 数学角减），
    # 修正（+5.5°）后应回到真值。不修正远、修正近。
    base = _line(0.0, 6, 5.0, 500.0, 0.0, 3800.0, 0.0)
    shifted = A._rotated_points(base, -5.5)
    opt = _trk("o", "optical", shifted)
    ais = _trk("a", "ais", base)
    raw = A.pair_score(opt, ais, apply_bias_correction=False)
    fixed = A.pair_score(opt, ais, apply_bias_correction=True)
    assert raw["ok"] and fixed["ok"]
    assert fixed["median_m"] < 1.0
    assert fixed["median_m"] < raw["median_m"]


def test_course_gate_skipped_when_anchored():
    pts_a = _line(0.0, 4, 5.0, 0.0, 0.5, 0.0, 0.2)
    pts_b = _line(0.0, 4, 5.0, 1.0, 0.5, 1.0, 0.2)
    slow = _trk("s", "ais", pts_a, speed=0.03, course=110.0)
    other = _trk("o", "ais", pts_b, speed=0.04, course=200.0)
    acc, _ = A.associate_pair([slow], [other], gate_m=100.0)
    assert len(acc) == 1  # 航向差 90° 但锚泊，不否决


def test_course_gate_applies_when_moving():
    pts_a = _line(0.0, 4, 5.0, 0.0, 2.0, 0.0, 1.0)
    pts_b = _line(0.0, 4, 5.0, 1.0, 2.0, 1.0, 1.0)
    fast_a = _trk("a", "ais", pts_a, speed=4.0, course=90.0)
    fast_b = _trk("b", "ais", pts_b, speed=4.0, course=200.0)
    acc, _ = A.associate_pair([fast_a], [fast_b], gate_m=100.0)
    assert acc == []  # 航向差 110° 否决


def test_many_to_one_allows_shared_target():
    ais = _trk("ais:x", "ais", _line(0.0, 6, 5.0, 0.0, 1.0, 0.0, 1.0))
    inf = _trk("optical:x@Inf", "optical", _line(0.0, 6, 5.0, 0.5, 1.0, 0.5, 1.0))
    tv = _trk("optical:x@TV", "optical", _line(0.0, 6, 5.0, 0.6, 1.0, 0.4, 1.0))
    acc, _ = A.associate_pair(
        [inf, tv], [ais], gate_m=10.0, apply_bias_correction=False,
        allow_many_to_one=True,
    )
    assert {a["track_a"] for a in acc} == {"optical:x@Inf", "optical:x@TV"}


def test_load_candidates_skips_ais_snapshots(tmp_path):
    # 最小对齐目录：index + ais/radar csv；雷达混合轨含快照拷贝
    tracks = tmp_path / "tracks"
    (tracks / "ais").mkdir(parents=True)
    (tracks / "radar").mkdir(parents=True)
    with open(tracks / "track_index.csv", "w", encoding="utf-8", newline="") as h:
        w = csv.writer(h)
        w.writerow(["track_id", "sensor_type", "local_track_id",
                    "track_stability", "point_count",
                    "mean_speed_mps", "mean_course_deg"])
        w.writerow(["ais:1", "ais", "1", "stable", 4, 1.0, 90.0])
        w.writerow(["radar:1", "radar", "1", "stable", 4, 1.0, 90.0])
    with open(tracks / "ais" / "1.csv", "w", encoding="utf-8", newline="") as h:
        w = csv.writer(h)
        w.writerow(["time_utc", "east_m", "north_m"])
        for i in range(4):
            w.writerow([f"2025-03-05 10:00:0{i}.000", 100 + i, 200 + i])
    with open(tracks / "radar" / "1.csv", "w", encoding="utf-8", newline="") as h:
        w = csv.writer(h)
        w.writerow(["time_utc", "east_m", "north_m", "radar_measurement_kind"])
        for i in range(2):  # 快照拷贝应被剔除
            w.writerow([f"2025-03-05 10:00:0{i}.000", 100 + i, 200 + i,
                        "ais_associated"])
        for i in range(2, 5):  # 纯测量保留 3 点
            w.writerow([f"2025-03-05 10:00:0{i}.000", 100 + i, 200 + i,
                        "radar_detected"])
    cands = A.load_candidates(str(tmp_path), min_points=3,
                              use_reconstructed=False)
    assert [t.track_id for t in cands["radar"]] == ["radar:1"]
    assert cands["radar"][0].point_count == 3
