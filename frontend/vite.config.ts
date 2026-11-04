import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// dev 模式下把 /api 代理到 FastAPI(8000)，前端代码里统一用相对路径
export default defineConfig({
  plugins: [react()],
  server: {
    // 显式绑定 IPv4 回环。
    // 缺省时 Vite 会把 localhost 解析成 ::1，只监听 [::1]:5173，
    // 导致内置浏览器/工具访问 127.0.0.1:5173 时一直 SYN_SENT（刷新白屏）。
    host: '127.0.0.1',
    port: 5173,
    // 端口被占用时直接报错，而不是悄悄漂移到 5174。
    // 否则浏览器还停在旧地址，表现就是"页面刷新不出来"。
    strictPort: true,
    proxy: {
      '/api': {
        // 用 127.0.0.1 而非 localhost，避免 Windows 上解析到 ::1 后连不通 uvicorn
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
