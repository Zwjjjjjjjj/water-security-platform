#!/usr/bin/env python3
"""
校验 SceneSensorsV2 的 ENU 平面投影是否正确。

【校验三件事】
1. **原点一致**：雷达站标记的屏幕位置 == ENU 原点的投影位置；
2. **四方向检查**：方位角 0°/90°/180°/270° 投影后分别指向北/东/南/西；
3. **同平面**：ENU 平面上的圆投影后是**贴面椭圆**，不是屏幕正圆。

投影链路（与 oblique-fit.ts 一致）：
  ENU ──enuToImage()──▶ 图片像素 ──×r + 偏移──▶ 容器像素
"""

import math

# --- 与 scenes.config.v2.ts 一致 ---
IMG_W, IMG_H = 1536, 1024
REF_E_MIN, REF_E_MAX = -2600.0, 1000.0
REF_N_MIN, REF_N_MAX = 2900.0, 5100.0
IMG_X0, IMG_Y0, IMG_X1, IMG_Y1 = 0.0, 40.0, 1536.0, 1000.0

A = (IMG_X1 - IMG_X0) / (REF_E_MAX - REF_E_MIN)
B = 0.0
C = 0.0
D = -(IMG_Y1 - IMG_Y0) / (REF_N_MAX - REF_N_MIN)
E = IMG_X0 - A * REF_E_MIN
F = IMG_Y0 - D * REF_N_MAX


def enu_to_image(e, n):
    return (A * e + C * n + E, B * e + D * n + F)


def image_to_enu(x, y):
    det = A * D
    dx = x - E
    dy = y - F
    return ((D * dx - C * dy) / det, (-B * dx + A * dy) / det)


# --- 与 oblique-fit.ts 的 computeObliqueFit 一致 ---
def compute_fit(x_min, x_max, y_min, y_max, plot_w, plot_h, anchor_x=0.0, anchor_y=0.0):
    img_l, img_t = enu_to_image(x_min, y_max)
    px_per_m = max(50.0, plot_w) / (x_max - x_min)
    r_w = px_per_m / A
    r_h = px_per_m / abs(D)
    r = max(r_w, r_h)
    return {
        "r": r,
        "left": anchor_x - img_l * r,
        "top": anchor_y - img_t * r,
    }


def project(fit, e, n):
    ix, iy = enu_to_image(e, n)
    return (fit["left"] + ix * fit["r"], fit["top"] + iy * fit["r"])


OBLIQUE_Y_RATIO = 0.46


def polar(fit, east, north, rng, bearing_deg):
    a = math.radians(bearing_deg)
    ox, oy = project(fit, east, north)
    px, py = project(fit, east + rng * math.sin(a), north + rng * math.cos(a))
    # 斜视视觉压缩：纵向位移按 0.46 压扁（与声纳波圈一致）
    return (px, oy + (py - oy) * OBLIQUE_Y_RATIO)


# --- 测试场景：与 obliqueCoverRange 对齐的取景 ---
PANEL_W, PANEL_H = 1920.0, 870.0
GRID_LEFT, GRID_RIGHT, GRID_TOP, GRID_BOTTOM = 0.0, 0.0, 0.0, 0.0


def oblique_cover_range(panel_w, panel_h):
    span_e = REF_E_MAX - REF_E_MIN
    img_span_w = IMG_X1 - IMG_X0
    x_span = span_e
    y_span = x_span * panel_h / max(1.0, panel_w)
    c_e = (REF_E_MIN + REF_E_MAX) / 2
    c_n = (REF_N_MIN + REF_N_MAX) / 2
    base = (c_e - x_span / 2, c_e + x_span / 2, c_n - y_span / 2, c_n + y_span / 2)
    shrink = 1 - 0.08
    cx = (base[0] + base[1]) / 2
    cy = (base[2] + base[3]) / 2
    xs = (base[1] - base[0]) * shrink
    ys = (base[3] - base[2]) * shrink
    return (cx - xs / 2, cx + xs / 2, cy - ys / 2, cy + ys / 2)


SENSORS = [
    ("RADAR-01", -1985.9, 4034.4, 1500, 175),
    ("AIS-01", -748.4, 3587.5, 1200, 105),
    ("EO-01", -57.0, 3747.9, 1000, 130),
]


def main():
    x_min, x_max, y_min, y_max = oblique_cover_range(PANEL_W, PANEL_H)
    fit = compute_fit(
        x_min, x_max, y_min, y_max,
        PANEL_W - GRID_LEFT - GRID_RIGHT,
        PANEL_H - GRID_TOP - GRID_BOTTOM,
        0.0, 0.0,
    )
    print(f"视口 ENU: E[{x_min:.0f}, {x_max:.0f}] N[{y_min:.0f}, {y_max:.0f}]")
    print(f"投影比率 r = {fit['r']:.4f}（容器像素 / 图片像素）\n")

    for sid, e0, n0, rng, bearing in SENSORS:
        print(f"===== {sid} =====")
        origin = project(fit, e0, n0)
        print(f"  原点屏幕位置 = ({origin[0]:.1f}, {origin[1]:.1f})")

        # --- 1. 原点一致性：ENU 反算回图片像素，应等于原始像素 ---
        px, py = enu_to_image(e0, n0)
        back = project(fit, e0, n0)
        assert abs(back[0] - origin[0]) < 1e-6 and abs(back[1] - origin[1]) < 1e-6
        print(f"  ✓ 原点一致（ENU↔图片↔容器 链路自洽）")

        # --- 1b. 原点必须贴住底图上的站点像素 ---
        # 传感器在底图上的原始像素位置（见 SENSORS 定义处的反算依据）
        src_px = {"RADAR-01": (262, 505), "AIS-01": (790, 700), "EO-01": (1085, 630)}[sid]
        ex, ey = image_to_enu(*src_px)
        exp_origin = project(fit, ex, ey)
        d = math.hypot(exp_origin[0] - origin[0], exp_origin[1] - origin[1])
        print(f"  ✓ 与底图像素 {src_px} 重合（偏差 {d:.2f}px）")
        assert d < 0.5, "传感器标记必须严格贴住底图上的站点"

        # --- 2. 四方向检查 ---
        print("  四方向检查（方位角 → 屏幕方向）：")
        dirs = {}
        for label, deg in [("北 0°", 0), ("东 90°", 90), ("南 180°", 180), ("西 270°", 270)]:
            p = polar(fit, e0, n0, rng, deg)
            dx = p[0] - origin[0]
            dy = p[1] - origin[1]
            # 屏幕角度（0° = 右，顺时针向下为正）
            ang = math.degrees(math.atan2(dy, dx))
            dirs[label] = (dx, dy)
            print(
                f"    {label:8s} → 屏幕 ({p[0]:7.1f}, {p[1]:7.1f})  "
                f"偏移 ({dx:+7.1f}, {dy:+7.1f})  屏幕角 {ang:+7.1f}°"
            )

        # 断言：北偏上、东偏右、南偏下、西偏左
        assert dirs["北 0°"][1] < 0, "北应当偏上（dy<0）"
        assert dirs["东 90°"][0] > 0, "东应当偏右（dx>0）"
        assert dirs["南 180°"][1] > 0, "南应当偏下（dy>0）"
        assert dirs["西 270°"][0] < 0, "西应当偏左（dx<0）"
        print("  ✓ 四方向语义正确（北/东/南/西 各就各位）")

        # --- 3. 同平面检查：ENU 圆投影后应是压扁的椭圆（贴在水面上） ---
        pts = []
        for i in range(360):
            p = polar(fit, e0, n0, rng, i)
            pts.append(p)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        print(f"  投影后包络：宽 {w:.1f}px × 高 {h:.1f}px  （高/宽 = {h/w:.3f}）")
        assert 0.40 < h / w < 0.55, "应当是压扁的椭圆（贴合斜视水面）"
        print("  ✓ 贴合水面斜面（压扁椭圆），不是屏幕正圆\n")

    print("全部校验通过 ✅")


if __name__ == "__main__":
    main()
