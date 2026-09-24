import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import {fileURLToPath} from 'node:url'

const contract = fileURLToPath(new URL('../src/web_operation_agent/web_runtime', import.meta.url))

/**
 * `assistant.js`: the one file a host page includes to have the assistant -- the boot
 * supervisor, the Runtime and the panel, served by the agent. Nothing is fetched after it:
 * no second module, no stylesheet. The panel's stylesheet ships inside as text and is
 * adopted as a constructed stylesheet at mount, so a host page running `style-src 'self'`
 * is never asked to allow an injected `<style>` element.
 *
 * It is minified: it carries React, and it loads on every page of the host application.
 */
export default defineConfig({
  resolve: {alias: {'@contract': contract}},
  // React is bundled, so the build must be told it is in production: lib mode does not do
  // it, and the development build is four times the size.
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  build: {
    outDir: 'dist/runtime',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: false,
    // The stylesheet ships as a string inside the module and the browser adopts it whole,
    // so the suite can assert that the exact palette literals it reads back are the ones
    // declared in design_tokens.json. lightningcss -- the Vite 8 default -- shortens
    // `#ffffff` to `#fff` and quietly re-writes rules; esbuild minifies whitespace without
    // touching any of that.
    cssMinify: 'esbuild',
    lib: {
      entry: fileURLToPath(new URL('./src/runtime/index.ts', import.meta.url)),
      formats: ['es'],
      fileName: () => 'assistant.js',
    },
    rolldownOptions: {output: {inlineDynamicImports: true}},
  },
  plugins: [react()],
})
