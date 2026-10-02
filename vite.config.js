import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
const proxy = { target: 'http://127.0.0.1:8091', changeOrigin: false }
export default defineConfig({ plugins: [vue()], server: { proxy: { '/api': proxy, '/agent': proxy } } })
