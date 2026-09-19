import {defineConfig} from 'vite'
import {fileURLToPath} from 'node:url'

const contract = fileURLToPath(new URL('../src/web_operation_agent/web_runtime', import.meta.url))

/**
 * The embedded Runtime: one ES module, no dependencies, served by the agent and imported
 * into the host application's page.
 *
 * It is left unminified. It runs inside someone else's application, and whoever integrates
 * it should be able to read what it does from the browser's own source view.
 */
export default defineConfig({
  resolve: {alias: {'@contract': contract}},
  build: {
    outDir: 'dist/runtime',
    emptyOutDir: true,
    target: 'es2022',
    minify: false,
    lib: {
      entry: fileURLToPath(new URL('./src/runtime/index.ts', import.meta.url)),
      formats: ['es'],
      fileName: () => 'embedded.js',
    },
    rollupOptions: {output: {inlineDynamicImports: true}},
  },
})
