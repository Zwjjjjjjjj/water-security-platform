import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { writeFileSync, mkdirSync } from 'fs'

const pathResolve = (dir: string): string => {
  return resolve(__dirname, '.', dir)
}

/**
 * 开发期截图落地中间件（仅 dev 生效）。
 *
 * 用途：把浏览器 canvas 的截图（dataURL）POST 回本地磁盘，
 * 便于在远程/无头浏览器环境里做视觉核验。
 * 生产构建不会加载本插件 —— `apply: 'serve'` 保证只在 dev server 生效。
 */
function screenshotSink() {
  return {
    name: 'ws-screenshot-sink',
    apply: 'serve' as const,
    configureServer(server: any) {
      server.middlewares.use('/__shot', (req: any, res: any) => {
        if (req.method !== 'POST') {
          res.statusCode = 405
          res.end('POST only')
          return
        }
        let body = ''
        req.on('data', (c: Buffer) => (body += c))
        req.on('end', () => {
          try {
            const { name = 'shot.jpg', b64 = '' } = JSON.parse(body)
            const outDir = resolve(__dirname, '.dev-shots')
            mkdirSync(outDir, { recursive: true })
            const buf = Buffer.from(b64, 'base64')
            writeFileSync(resolve(outDir, name), buf)
            res.setHeader('Content-Type', 'text/plain')
            res.end(`ok ${buf.length}`)
          } catch (e: any) {
            res.statusCode = 500
            res.end(String(e))
          }
        })
      })

    },
  }
}
export default defineConfig(({ mode }) => {
  return {
    plugins: [vue(), screenshotSink()],
    base: mode === 'production' ? '/es-big-screen' : '',
    resolve: {
      alias: {
        '@': pathResolve('src'),
      },
    },
    server: {
      port: 3006,
    },
  }
})
