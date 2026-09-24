/**
 * Build the trace replay page: `trace/trace_replay.html` with the design tokens inlined.
 *
 * The replay is a single self-contained file that also opens from a local file URL, so it
 * can link to nothing: the palette goes into its own `<style>`. The server's part is only
 * the data -- `traceview.py` fills the bundle and the product name into the built page --
 * which is why the page's source, like its look, lives here.
 */
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {customProperties, readTokens} from './tokens.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const source = resolve(here, '../trace/trace_replay.html')
const target = resolve(here, '../dist/trace/trace_replay.html')
const MARKER = '/*__WOA_DESIGN_TOKENS__*/'

const tokens = readTokens()
const template = readFileSync(source, 'utf8')
if (template.split(MARKER).length !== 2) {
  throw new Error(`${source} must contain exactly one ${MARKER}`)
}
const css = [
  ':root {',
  customProperties(tokens.scale, '      '),
  customProperties(tokens.themes.light, '      '),
  '      color-scheme: light;',
  '    }',
  '    @media (prefers-color-scheme: dark) {',
  '      :root {',
  customProperties(tokens.themes.dark, '        '),
  '        color-scheme: dark;',
  '      }',
  '    }',
].join('\n')
mkdirSync(dirname(target), {recursive: true})
writeFileSync(target, template.replace(MARKER, css))
process.stdout.write(`trace replay -> ${target}\n`)
