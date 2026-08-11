import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        // 大依赖独立分包：框架层与组件库分开缓存（router 已有 chunk 404 自动刷新兜底）。
        // rolldown 的 manualChunks 会递归吞并依赖，改用 advancedChunks + priority
        // 保证 vue 运行时归 vendor-vue 而不是被 naive-ui 组吸走。
        advancedChunks: {
          groups: [
            {
              name: 'vendor-vue',
              // vue 生态（含 @vue/* 运行时包；注意精确匹配包名段，避免误吞 vueuc）
              test: /[\\/]node_modules[\\/](?:@vue|vue|vue-router|pinia)[\\/]/,
              priority: 20,
            },
            {
              name: 'vendor-naive',
              // naive-ui 及其 css-in-js / 运行时依赖
              test: /[\\/]node_modules[\\/](?:naive-ui|vueuc|css-render|@css-render|vooks|evtd|seemly|treemate|async-validator|date-fns|@vicons|highlight\.js)[\\/]/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7861',
        changeOrigin: true,
      },
    },
  },
})
