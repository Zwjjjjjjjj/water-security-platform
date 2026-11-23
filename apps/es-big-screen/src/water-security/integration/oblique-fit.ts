/**
 * V2 固定斜视背景的坐标映射（共享）
 *
 * 【为什么抽出来】
 * `SceneObliqueV2.vue` 负责渲染背景图，`ChartSceneV2.vue` 负责在图上放标签。
 * 两者必须用**完全相同**的映射，否则文字会与背景里的建筑错位。
 * 把公式收敛到这里，两边共用一份实现，杜绝漂移。
 *
 * 【映射链路】
 *   ENU 米制  ──enuToImage()──▶  图片原生像素 (1536×1024)
 *   图片像素  ──本模块 r────▶  图表容器像素
 *
 *   其中 r = pxPerM / a
 *     · 1 米 ENU → a 个图片像素      （enuToImage 的 x 系数）
 *     · 1 米 ENU → pxPerM 个容器像素  （视图比例）
 *
 * 【铺满全屏 —— 背景锚点 vs 坐标轴绘图区】
 * 背景要铺满**整个中央显示区**，而 ECharts 坐标轴为了画刻度标签
 * 需要留出 GRID 边距（left 56 / right 18 / top 44 / bottom 40）。
 * 两者是不同的事：
 *
 *   · 背景层：锚在**容器**的 (0,0)，宽 = chartW，高 = chartH
 *     → 背景铺满整个面板，不留边
 *   · 坐标层：锚在**绘图区**的 (gridLeft, gridTop)
 *     → 刻度、距离环、航迹仍在原来的位置，行为不变
 *
 * 由于两者共用同一个 ENU↔图片↔容器 的仿射链，
 * 只要都用同一套 r，背景与航迹就必然对齐；
 * 差别仅在于「视口左上角落在容器的哪个像素上」。
 */
import { enuToImage, OBLIQUE_FIT } from './scenes.config.v2'

export interface ObliqueViewport {
  xMin: number
  xMax: number
  yMin: number
  yMax: number
  /** 背景层锚点（容器像素）—— 通常为 0,0，即铺满整个面板 */
  gridLeft: number
  gridTop: number
  /** 绘图区宽度（容器像素） */
  plotW: number
  /** 绘图区高度（容器像素） */
  plotH: number
  /**
   * 「视口左上角」在容器中的锚点像素。
   * 背景层用容器原点 (0,0)；坐标层用绘图区原点 (gridLeft, gridTop)。
   */
  anchorX?: number
  anchorY?: number
}

/** 斜视映射参数 */
export interface ObliqueFit {
  /** 图片像素 → 容器像素 的比率 */
  r: number
  /** 背景图片左上角在容器中的位置 */
  left: number
  top: number
  /** 背景图片在容器中的显示尺寸 */
  width: number
  height: number
}

/** 背景图的原生尺寸 */
export const OBLIQUE_NAT_W = 1536
export const OBLIQUE_NAT_H = 1024

/**
 * 计算斜视背景的映射参数。
 *
 * @param vp 当前 ENU 视口 + 绘图区尺寸
 */
export function computeObliqueFit(vp: ObliqueViewport): ObliqueFit {
  /* 当前视口四角在图片坐标系里的位置 */
  const [imgL, imgT] = enuToImage(vp.xMin, vp.yMax)

  /* 米→容器像素（两轴一致，等比例） */
  const pxPerM = Math.max(50, vp.plotW) / (vp.xMax - vp.xMin)

  /* 图片像素 → 容器像素 的比率。
     r = pxPerM / a，其中 a 是 ENU→图片 的像素系数。
     （写成乘法会让背景被缩小约 (a²) 倍。）
     取横向/纵向的较大者，避免图片被拉伸变形。 */
  const rW = pxPerM / OBLIQUE_FIT.a
  const rH = pxPerM / Math.abs(OBLIQUE_FIT.d)
  const r = Math.max(rW, rH)

  /* 锚点：视口左上角在容器里的位置。
     背景层用容器原点（铺满面板），坐标层用绘图区原点。 */
  const anchorX = vp.anchorX ?? vp.gridLeft
  const anchorY = vp.anchorY ?? vp.gridTop

  return {
    r,
    left: anchorX - imgL * r,
    top: anchorY - imgT * r,
    width: OBLIQUE_NAT_W * r,
    height: OBLIQUE_NAT_H * r,
  }
}

/**
 * 图片原生像素 → 图表容器像素。
 *
 * @param fit   映射参数（由 computeObliqueFit 得到）
 * @param imgX  图片 X 像素
 * @param imgY  图片 Y 像素
 */
export function obliqueImageToContainer(fit: ObliqueFit, imgX: number, imgY: number): [number, number] {
  return [fit.left + imgX * fit.r, fit.top + imgY * fit.r]
}

/**
 * 斜视模式下的**取景视口**。
 *
 * 【为什么需要单独的取景】
 * 正俯视模式按实测目标位置取景；但斜视背景是一张固定的概念图，
 * 若按实测目标取景，厂区（画面的主体）就会被裁到视口外。
 *
 * 这里改为：让**图片的有效内容区**恰好铺满**整个显示区**，
 * 厂区落在画面上部、海面在下部 —— 与素材包 preview 的目标布局一致。
 *
 * 【横向优先 —— 图片宽度铺满】
 * 显示区宽高比（1920×870 约 2.2:1）远大于图片内容区（1.5:1）。
 * 若按高度铺满，图片两侧会露出大片空白；
 * 因此**按宽度铺满**：图片整幅宽度铺满显示区，
 * 垂直方向按显示区比例裁切出一条带（上下各裁掉一部分）。
 * 裁切带以「内容区中心」为准，保证厂区与海面都保留在画面里。
 *
 * 【单位一致性 —— 这里最容易出错】
 *   · `spanE` 是**米**（参考窗口的示意米制跨度）
 *   · `imgSpanW` 是**图片像素**（内容区宽度）
 *   · `panelW` / `panelH` 是**容器像素**（显示区尺寸）
 * 三者不能直接相乘，必须先统一到「米」。
 *
 *   要让图片内容区铺满显示区宽度：
 *       容器像素/米 = panelW / spanE
 *       图片像素/米 = imgSpanW / spanE
 *       r（容器像素/图片像素） = (panelW/spanE) / (imgSpanW/spanE) = panelW / imgSpanW
 *
 *   视口米制跨度：
 *       xSpan = spanE                       （横向正好等于内容区）
 *       ySpan = xSpan * panelH / panelW     （等比例，1m(E)=1m(N)）
 *
 * 取景结果仍与 ENU 严格绑定（`enuToImage` 是双向映射），
 * 缩放/平移在此基础上进行，背景与航迹仍然同步。
 */
export function obliqueFramedRange(vp: { panelW: number; panelH: number }): {
  xMin: number
  xMax: number
  yMin: number
  yMax: number
} {
  const ref = OBLIQUE_FIT.ref
  const img = OBLIQUE_FIT.img

  /* 参考窗口的米制跨度 */
  const spanE = ref.eMax - ref.eMin
  /* 图片内容区的像素跨度 */
  const imgSpanW = img.x1 - img.x0

  /* 横向正好等于内容区：视口米制宽度 = 参考窗口米制宽度 */
  const xSpan = spanE
  /* 等比例：1 米(E) 与 1 米(N) 在屏幕上等长 */
  const ySpan = (xSpan * vp.panelH) / Math.max(1, vp.panelW)

  /* 视口中心对齐「图片内容区中心」
     （内容区中心对应 N = (ref.nMin + ref.nMax) / 2） */
  const cE = (ref.eMin + ref.eMax) / 2
  const cN = (ref.nMin + ref.nMax) / 2

  return {
    xMin: cE - xSpan / 2,
    xMax: cE + xSpan / 2,
    yMin: cN - ySpan / 2,
    yMax: cN + ySpan / 2,
  }
}

/**
 * 斜视背景的「铺满余量」系数。
 *
 * 【为什么需要余量】
 * 背景是一张**有限**的图。若它恰好与显示区等宽，
 * 那么水平方向一像素都不能平移 —— 用户拖不动，
 * 且稍微一动就会露出图外的空白。
 *
 * 让背景比显示区**宽一点点**（默认 8%），就有：
 *   · 水平可平移余量 ≈ 8% × 显示宽度
 *   · 垂直可平移余量（本来就有，因为图片更高）
 * 同时背景仍然**完全盖住**显示区，绝不会露白。
 *
 * 【实现方式】
 * 把视口米制宽度压缩 `COVER_MARGIN`：
 *   视口变窄 → 同样的图片映射到更宽的范围 → r 变大 → 背景超出显示区。
 * 缩放上限（1.5 倍）依然以这个取景为基准，行为一致。
 */
export const OBLIQUE_COVER_MARGIN = 0.08

/**
 * 带「铺满余量」的取景：让背景比显示区略大，四周都可小幅平移。
 *
 * @param vp 显示区尺寸（容器像素）
 */
export function obliqueCoverRange(vp: { panelW: number; panelH: number }): {
  xMin: number
  xMax: number
  yMin: number
  yMax: number
} {
  const base = obliqueFramedRange(vp)
  /* 视口米制宽度压缩 margin → 背景相对显示区放大 1/(1-margin) 倍 */
  const shrink = 1 - OBLIQUE_COVER_MARGIN
  const cx = (base.xMin + base.xMax) / 2
  const cy = (base.yMin + base.yMax) / 2
  const xSpan = (base.xMax - base.xMin) * shrink
  const ySpan = (base.yMax - base.yMin) * shrink

  return {
    xMin: cx - xSpan / 2,
    xMax: cx + xSpan / 2,
    yMin: cy - ySpan / 2,
    yMax: cy + ySpan / 2,
  }
}

/** 供外部确认映射系数（调试用） */
export const OBLIQUE_COEFF = OBLIQUE_FIT

/**
 * 侧栏遮挡区（容器像素）。
 *
 * 【问题】
 * 中央显示区铺满整个 1920px 宽的面板，
 * 但左右两侧各有一块浮层侧栏（各约 446px 宽）：
 *
 *   ┌──────────┬──────────────────────────────┬──────────┐
 *   │ 左侧栏   │        中央显示区            │ 右侧栏   │
 *   │  0~460  │                              │ 1460~1920│
 *   └──────────┴──────────────────────────────┴──────────┘
 *
 * 侧栏是**浮在场景之上**的（z-index 4），场景在其下方继续渲染。
 * 因此：
 *   · 目标落在 x > 1460 的区域 → 被右侧栏盖住，看不见；
 *   · 标签文字画到侧栏下方 → 同样被盖住。
 *
 * 【做法】
 * 不改布局（侧栏仍然浮在场景上，保持大屏观感），
 * 而是让**取景**与**标签避让**都知道这块遮挡区：
 *   · 取景时把「可见区」定义为左右侧栏之间的中段；
 *   · 标签落在遮挡区时，自动翻转到目标另一侧。
 */
export interface OcclusionArea {
  /** 左侧栏右边界（容器像素） */
  left: number
  /** 右侧栏左边界（容器像素） */
  right: number
}

/** 大屏布局的固定侧栏宽度（与 preview.css 的 --ws-left/right-width 一致） */
export const PANEL_WIDTH = 460
/** 侧栏与场景边缘的间距 */
export const PANEL_GAP = 14

/** 左右侧栏的遮挡边界（容器像素） */
export function panelOcclusion(panelW: number): OcclusionArea {
  return {
    left: PANEL_WIDTH + PANEL_GAP,
    right: Math.max(PANEL_WIDTH + PANEL_GAP + 100, panelW - PANEL_WIDTH - PANEL_GAP),
  }
}

/**
 * 按「可见区」重新居中取景。
 *
 * 【为什么不能直接用 panelW】
 * `obliqueCoverRange()` 用整个显示区宽度来铺满背景，
 * 背景确实盖满全屏（这是用户要的视觉效果）；
 * 但**内容居中**应该按可见区（两侧栏之间）居中，
 * 否则内容会被推到侧栏底下。
 *
 * 这里只平移视口中心，**不改变缩放** ——
 * 背景仍然铺满，缩放手感不变。
 *
 * @param range    原始取景（ENU 视口）
 * @param panelW   显示区宽度（容器像素）
 * @param toPx     ENU → 容器像素 的投影函数
 */
export function recenterForPanels(
  range: { xMin: number; xMax: number; yMin: number; yMax: number },
  panelW: number,
  toPx: (e: number, n: number) => [number, number],
): { xMin: number; xMax: number; yMin: number; yMax: number } {
  const occ = panelOcclusion(panelW)
  const visibleW = occ.right - occ.left
  if (visibleW < 120) return range

  /* 视口中心在容器中的位置 */
  const cx = (range.xMin + range.xMax) / 2
  const cy = (range.yMin + range.yMax) / 2
  const [pcx] = toPx(cx, cy)

  /* 可见区中心与当前内容中心的差（容器像素） */
  const visibleCenter = (occ.left + occ.right) / 2
  const dxPx = visibleCenter - pcx

  /* 把 dxPx 换算成 ENU 位移 */
  const [e0, n0] = toPx(cx, cy)
  const [e1] = toPx(cx + 1, cy)
  const mPerPx = 1 / Math.abs(e1 - e0)

  const shiftE = -dxPx * mPerPx
  return {
    xMin: range.xMin + shiftE,
    xMax: range.xMax + shiftE,
    yMin: range.yMin,
    yMax: range.yMax,
  }
}
