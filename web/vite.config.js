import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Element Plus on-demand styles. Vitest loads element-plus from node_modules
// with Node's ESM loader (bypassing Vite's CSS pipeline), so importing .css
// there throws "Unknown file extension". Skip style imports under Vitest; the
// auto-imported styles are only needed for dev and the production build.
const elementResolver = () => ElementPlusResolver({ importStyle: process.env.VITEST ? false : 'css' })

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [elementResolver()],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [elementResolver()],
      dts: 'src/components.d.ts',
    }),
  ],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      }
    }
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.js'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json-summary'],
      include: ['src/pages/**/*.vue', 'src/components/**/*.vue'],
      thresholds: { lines: 80, functions: 80, statements: 80, branches: 80 },
    },
  },
})
