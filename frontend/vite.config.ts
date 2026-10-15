/// <reference types="vitest/config" />
import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import {fileURLToPath} from 'node:url'
import {aiMotionAntialiasFix} from './ai-motion.vite.ts'

const contract = fileURLToPath(new URL('../src/web_operation_agent/web_runtime', import.meta.url))

/**
 * The dev page (`npm run dev`: `index.html` and `dev/`) and the unit tests. The product is
 * `assistant.js`, built by `vite.runtime.config.ts`; nothing here ships.
 */
export default defineConfig({
  resolve: {alias: {'@contract': contract}},
  plugins: [react(), aiMotionAntialiasFix()],
  optimizeDeps: {exclude: ['ai-motion']},
  test: {environment: 'jsdom', include: ['tests/**/*.unit.test.ts?(x)',
                                         'tests/**/*.unit.test.js'], globals: true},
})
