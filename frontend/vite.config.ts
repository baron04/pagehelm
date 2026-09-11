/// <reference types="vitest/config" />
import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import {fileURLToPath} from 'node:url'

const contract = fileURLToPath(new URL('../src/web_operation_agent/web_runtime', import.meta.url))

/**
 * One product: an ES module the Runtime imports into the host application's page.
 *
 * The stylesheet is bundled into the module as text and adopted as a constructed
 * stylesheet at mount, so a host page running `style-src 'self'` is never asked to allow
 * an injected `<style>` element.
 */
export default defineConfig({
  resolve: {alias: {'@contract': contract}},
  css: {preprocessorOptions: {scss: {api: 'modern-compiler'}}},
  // React is bundled, so it must be told it is in production; lib mode does not do this
  // for us and the development build is four times the size.
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  build: {
    outDir: 'dist/panel',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: false,
    lib: {
      entry: fileURLToPath(new URL('./src/index.ts', import.meta.url)),
      formats: ['es'],
      fileName: () => 'panel.js',
    },
    rollupOptions: {
      output: {inlineDynamicImports: true},
    },
  },
  plugins: [react()],
  test: {environment: 'jsdom', include: ['tests/**/*.unit.test.ts?(x)'], globals: true},
})
