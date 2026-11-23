/**
 * 场景素材索引
 *
 * 数据来源：/water-security/assets/scenes/manifest.json（由素材包提供）
 * 说明：manifest 中 length_m / beam_m 为「示意模板尺寸」，非数据集目标真实船长；
 *      opaque_bbox_px 为 alpha≥250 估算的船体轮廓范围，用于换算真实水线尺寸，
 *      不能按整张 PNG 尺寸计算（图片左右有透明留白）。
 */

export interface SceneAsset {
  id: string
  category: 'ships' | 'npp' | 'sprites' | 'terrain'
  label: string
  view: 'top' | 'iso' | 'texture'
  url: string
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
  /** 船艏朝向：image_up 表示图片上方为船艏 */
  bow_direction?: string
}

/** 素材包 manifest 原始结构（只取用得到的字段） */
interface Manifest {
  assets: SceneAsset[]
}

let cache: SceneAsset[] | null = null

/** 加载 manifest（带缓存）。失败时返回空数组，场景回退到内置 SVG 示意图。 */
export async function loadSceneAssets(): Promise<SceneAsset[]> {
  if (cache) return cache
  try {
    const res = await fetch('/water-security/assets/scenes/manifest.json')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = (await res.json()) as Manifest
    cache = Array.isArray(data.assets) ? data.assets : []
  } catch (err) {
    console.warn('[scenes] 素材 manifest 加载失败，回退内置示意图', err)
    cache = []
  }
  return cache
}

/** 同步读取（供已加载后使用） */
export function getSceneAssets(): SceneAsset[] {
  return cache || []
}

/** 按 id 取素材 */
export function assetById(id: string): SceneAsset | undefined {
  return getSceneAssets().find((a) => a.id === id)
}

/** 按类别取素材 */
export function assetsByCategory(cat: SceneAsset['category']): SceneAsset[] {
  return getSceneAssets().filter((a) => a.category === cat)
}

/** 船型素材 id 列表（顺序即配置表顺序） */
export const SHIP_ASSET_IDS = [
  'cargo_90m_top',
  'tanker_140m_top',
  'container_160m_top',
  'bulk_180m_top',
  'roro_180m_top',
  'tug_32m_top',
  'fishing_24m_top',
  'engineering_60m_top',
] as const

export type ShipAssetId = (typeof SHIP_ASSET_IDS)[number]
