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
export default defineConfig(({command}) => ({
  resolve: {alias: {'@contract': contract}},
  // React is bundled, so the build must be told it is in production: lib mode does not do
  // it, and the development build is four times the size. Only on `build`, though --
  // forcing it during `serve` hands Fast Refresh a production React and the dev page dies
  // on `RefreshRuntime.injectIntoGlobalHook is not a function`.
  ...(command === 'build'
    ? {define: {'process.env.NODE_ENV': JSON.stringify('production')}}
    : {}),
  build: {
    outDir: 'dist/panel',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: false,
    // The stylesheet ships as a string inside the module and the browser adopts it whole,
    // so the suite can assert that the exact palette literals it reads back are the ones
    // declared in design_tokens.json. lightningcss -- the Vite 8 default -- shortens
    // `#ffffff` to `#fff` and quietly re-writes rules; esbuild minifies whitespace without
    // touching any of that, and costs nothing on top.
    cssMinify: 'esbuild',
    lib: {
      entry: fileURLToPath(new URL('./src/index.ts', import.meta.url)),
      formats: ['es'],
      fileName: () => 'panel.js',
    },
    rolldownOptions: {
      output: {inlineDynamicImports: true},
    },
  },
  plugins: [react()],
  // `index.html` and `dev/` are the harness for `npm run dev`; `lib` above is the
  // product, and it takes `src/index.ts` as its only entry, so neither ships.
  test: {environment: 'jsdom', include: ['tests/**/*.unit.test.ts?(x)',
                                         'tests/**/*.unit.test.js'], globals: true},
}))
