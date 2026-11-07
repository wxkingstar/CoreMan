import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    // /docs 与 /integration/ 下的两份说明由 API 渲染（源文件在 coreman/docs_site/pages/），开发时一并代理。
    proxy: Object.fromEntries(
      ['/api', '/health', '/docs', '/integration/api-tasks-agent-guide.md', '/integration/business-system-openapi-contract.md']
        .map((path) => [path, process.env.COREMAN_API_TARGET || 'http://127.0.0.1:8000']),
    ),
  },
  build: { outDir: 'dist', emptyOutDir: true },
  test: {
    environment: 'jsdom',
    server: { deps: { inline: ['element-plus'] } },
    testTimeout: 30000, // router.spec 动态加载 LoginView + Element Plus，空闲时约 2.6s，慢 CI 机上会超默认 5s
  },
})
