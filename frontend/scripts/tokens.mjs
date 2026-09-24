/**
 * The design tokens, read once for every build script that turns them into CSS.
 *
 * `src/styles/design_tokens.json` is the frontend's own file: the palette and the scale
 * are how its pages look, which nothing on the server decides or reads.
 */
import {readFileSync} from 'node:fs'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
export const TOKENS_PATH = resolve(here, '../src/styles/design_tokens.json')

export function readTokens() {
  return JSON.parse(readFileSync(TOKENS_PATH, 'utf8'))
}

/** One `--woa-<name>: <value>;` declaration per token, each on its own indented line. */
export function customProperties(values, indent = '  ') {
  return Object.entries(values).map(([name, value]) => `${indent}--woa-${name}: ${value};`)
    .join('\n')
}
