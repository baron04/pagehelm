/**
 * Bundle Yudao page-integration modules (`src/integrations/yudao/`) for browser serving
 * into gitignored `dist/integrations/yudao/`, inlining shared workspace modules (e.g. `element-plus/`)
 * while keeping sibling `/woa/` imports external.
 */
import {mkdirSync} from 'node:fs'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {build} from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const srcDir = resolve(here, '../src/integrations/yudao')
const outDir = resolve(here, '../dist/integrations/yudao')

mkdirSync(outDir, {recursive: true})

const entryPoints = [
  resolve(srcDir, 'page-facts.js'),
  resolve(srcDir, 'yudao-page-controller.js'),
  resolve(srcDir, 'form-surface.js'),
]

await build({
  entryPoints,
  outdir: outDir,
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2022',
  // Sibling modules under `/woa/` are loaded independently over HTTP at runtime.
  external: ['./page-controller.js', './page-facts.js', './form-surface.js'],
  logLevel: 'silent',
})

process.stdout.write(`yudao integrations -> ${outDir}\n`)
