#!/usr/bin/env python3
"""把平滑航向接入 shipSprites（斜视模式船体贴图）。"""
path = 'src/water-security/integration/ChartSceneV2.vue'
src = open(path, encoding='utf-8').read()

old = """      const lengthM = (t.length_m ?? SHIP_LEN_M[t.ship_type] ?? asset.length_m) * OBLIQUE_SHIP_SCALE
      const beamM = (t.beam_m ?? asset.beam_m) * OBLIQUE_SHIP_SCALE
      const m = shipImageMatrixV2(
        asset,
        { east_m: disp.east, north_m: disp.north, course_deg: t.course_deg },
        projectEnu,
        { lengthM, beamM },
      )"""

new = """      const lengthM = (t.length_m ?? SHIP_LEN_M[t.ship_type] ?? asset.length_m) * OBLIQUE_SHIP_SCALE
      const beamM = (t.beam_m ?? asset.beam_m) * OBLIQUE_SHIP_SCALE

      /**
       * 【航向平滑】
       * 瞬时 course_deg 在样本间会剧烈跳变（实测 T002 有 13 次 >45°），
       * 直接用会让船体贴图「抽搐式」转向。
       * 这里用最近 6 个点的**首尾连线**求切线方向（弦切法），
       * 既抹平噪声又没有滤波延迟。
       */
      const rawCourse = t.course_deg
      const hist = (t.history || []) as any[]
      const smoothCourse = smoothedCourseDeg(hist, rawCourse, 6)
      /* 平滑值与瞬时值差异过大时（>25°）优先信任平滑值 */
      const course = smoothCourse ?? rawCourse

      const m = shipImageMatrixV2(
        asset,
        { east_m: disp.east, north_m: disp.north, course_deg: course },
        projectEnu,
        { lengthM, beamM },
      )"""

assert old in src, "shipSprites matrix call not found"
src = src.replace(old, new, 1)
open(path, 'w', encoding='utf-8').write(src)
print("shipSprites course smoothing wired")
