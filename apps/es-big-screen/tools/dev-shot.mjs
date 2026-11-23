/**
 * 开发期截图落地工具（仅 dev 用）。
 *
 * 用途：把 CDP `Page.captureScreenshot` 返回的 base64 JPEG 写到 `.dev-shots/`，
 * 便于在远程浏览器环境里做视觉核验。
 *
 * 用法：node tools/dev-shot.mjs <name.jpg> <base64>
 */
import { writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const [, , name = 'shot.jpg', b64 = ''] = process.argv
if (!b64) {
  console.error('usage: node tools/dev-shot.mjs <name.jpg> <base64>')
  process.exit(1)
}
const outDir = resolve(__dirname, '..', '.dev-shots')
mkdirSync(outDir, { recursive: true })
const buf = Buffer.from(b64, 'base64')
writeFileSync(resolve(outDir, name), buf)
console.log(`ok ${buf.length} -> ${resolve(outDir, name)}`)
