/**
 * V2 场景素材索引（写实航拍正俯视风格）
 *
 * 与 V1 的区别：
 *  1. 素材来自独立的 `scenes-v2/manifest.json`，与 V1 完全隔离，互不影响；
 *  2. **逐个素材探测存在性** —— manifest 里列出的素材可能还没提供，
 *     缺失的会被过滤掉，渲染层自动回退到矢量绘制。
 *     这样「先搭框架、后补素材」可以无缝衔接，不需要改代码；
 *  3. 提供 `missingAssets()` 供界面提示「还缺哪些素材」。
 *
 * 素材约定：
 *   - 正俯视贴图（campus_aerial / sea_surface）需要提供 ENU 世界范围，
 *     才能与实测航迹精确配准；缺失时按设计坐标锚定（精度略低但可用）；
 *   - 船体贴图提供 length_m / beam_m 用于换算真实水线尺寸。
 */

/** 素材类别 */
export type SceneCategoryV2 = 'ships' | 'npp' | 'sprites' | 'terrain'

export interface SceneAssetV2 {
  id: string
  category: SceneCategoryV2
  label: string
  view: 'top' | 'iso' | 'texture'
  url: string
  /** 是否为本版本必需（仅用于提示，不影响加载） */
  required?: boolean
  width_px: number
  height_px: number
  transparent: boolean
  /** 示意模板船长（米），仅 ships 有 */
  length_m?: number
  /** 示意模板船宽（米），仅 ships 有 */
  beam_m?: number
  /** 非透明轮廓像素范围 [x0,y0,x1,y1] */
  opaque_bbox_px?: [number, number, number, number]
  /** 锚点归一化坐标 */
  anchor_uv?: [number, number]
  /**
   * 地面（底座）锚点归一化坐标 —— 斜俯视立牌素材专用。
   *
   * 【与 `anchor_uv` 的区别】
   * `anchor_uv` 是「图片内容的中心/参考点」，
   * 而 `ground_anchor_uv` 是「设备**底座落地点**」——
   * 也就是素材与地面接触的那一点。
   *
   * 斜俯视立牌必须用底座锚点与场景地面对齐，
   * 若用整图中心或顶点点位，设备就会「陷进地里」或「浮在空中」。
   */
  ground_anchor_uv?: [number, number]
  /**
   * 建议显示高度（容器像素）—— 斜俯视立牌素材专用。
   *
   * 素材包给出的建议：AIS 岸台 70~100px、雷达站 80~110px、光电转塔 40~60px。
   * 这里取区间中值作为默认值，实际渲染还会按视口缩放做微调。
   */
  display_height_px?: number
  /** 素材是否自带扫描动画（斜俯视立牌均为静态，需由代码单独绘制扫描效果） */
  contains_scan_animation?: boolean
  /** 船艏朝向：image_up 表示图片上方为船艏 */
  bow_direction?: string
  /** 贴图覆盖的 ENU 世界范围 [xMin, yMin, xMax, yMax]（米） */
  world_bounds_enu?: [number, number, number, number] | null
  /** 可平铺（用于海面纹理） */
  tileable?: boolean
}

/** manifest 原始结构 */
interface ManifestV2 {
  version?: string
  assets: SceneAssetV2[]
  ships?: SceneAssetV2[]
}

let cache: SceneAssetV2[] | null = null
/** 已被探测为「文件不存在」的素材 id */
const missing = new Set<string>()
/** 所有 manifest 条目（含缺失的），用于提示 */
let allEntries: SceneAssetV2[] = []

/**
 * 探测单个素材文件是否存在。
 *
 * 用 HEAD 请求而非直接 <img> 加载，是因为：
 *   - <img> 的 onerror 是异步的，首帧会先画出破图占位；
 *   - HEAD 能同步拿到 404 状态码，便于在首帧前就决定「用贴图还是回退矢量」。
 *
 * 代价是每个素材多一次请求。素材数量少（<20），且会被缓存，可接受。
 */
async function probeAsset(a: SceneAssetV2): Promise<boolean> {
  try {
    const res = await fetch(a.url, { method: 'HEAD' })
    if (!res.ok) return false
    const len = Number(res.headers.get('content-length') || '0')
    // 某些静态服务器对不存在的文件也返回 200 + 空体
    return len > 0
  } catch {
    return false
  }
}

/**
 * 加载 V2 素材（带缓存）。
 *
 * 失败时返回空数组，场景回退到内置矢量示意图。
 */
export async function loadSceneAssetsV2(): Promise<SceneAssetV2[]> {
  if (cache) return cache
  try {
    const res = await fetch('/water-security/assets/scenes-v2/manifest.json')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as ManifestV2
    const entries: SceneAssetV2[] = [
      ...(Array.isArray(data.assets) ? data.assets : []),
      ...(Array.isArray(data.ships) ? data.ships : []),
    ]
    allEntries = entries
    // 并行探测，逐个决定是否可用
    const flags = await Promise.all(entries.map((a) => probeAsset(a)))
    cache = entries.filter((_, i) => {
      if (flags[i]) return true
      missing.add(entries[i].id)
      return false
    })
    if (missing.size) {
      console.info(
        `[scenes-v2] 以下素材尚未提供，已回退到矢量绘制：`,
        Array.from(missing).join(', '),
      )
    }
  } catch (err) {
    console.warn('[scenes-v2] 素材 manifest 加载失败，回退内置矢量示意图', err)
    cache = []
  }
  return cache
}

/** 同步读取（供已加载后使用） */
export function getSceneAssetsV2(): SceneAssetV2[] {
  return cache || []
}

/** 按 id 取素材 */
export function assetByIdV2(id: string): SceneAssetV2 | undefined {
  return getSceneAssetsV2().find((a) => a.id === id)
}

/** 按类别取素材 */
export function assetsByCategoryV2(cat: SceneCategoryV2): SceneAssetV2[] {
  return getSceneAssetsV2().filter((a) => a.category === cat)
}

/** 当前缺失的素材 id 列表 */
export function missingAssets(): string[] {
  return Array.from(missing)
}

/** manifest 中全部条目（含缺失的），用于「缺什么素材」的提示清单 */
export function allManifestEntries(): SceneAssetV2[] {
  return allEntries
}

/** 船型素材 id 列表（顺序即配置表顺序） */
export const SHIP_ASSET_IDS_V2 = [
  'cargo_90m_top',
  'tanker_140m_top',
  'container_160m_top',
  'bulk_180m_top',
  'roro_180m_top',
  'tug_32m_top',
  'fishing_24m_top',
  'engineering_60m_top',
] as const

export type ShipAssetIdV2 = (typeof SHIP_ASSET_IDS_V2)[number]
