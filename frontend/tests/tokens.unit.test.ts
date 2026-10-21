/**
 * The design tokens are one file, and these are the checks that keep it honest.
 *
 * The palette is checkable, so a colour cannot be made inaccessible unnoticed; the
 * handwritten stylesheet quotes the scale rather than restating it; and the generated
 * sources are generated from this file rather than from a copy of it.
 */
import {readFileSync} from 'node:fs'
import {resolve} from 'node:path'
import {describe, expect, it} from 'vitest'
import tokens from '../src/styles/design_tokens.json'

const root = resolve(__dirname, '..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

function linear(hex: string): number {
  const scaled = parseInt(hex, 16) / 255
  return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4
}

function luminance(colour: string): number {
  const value = colour.replace('#', '')
  return 0.2126 * linear(value.slice(0, 2)) + 0.7152 * linear(value.slice(2, 4))
    + 0.0722 * linear(value.slice(4, 6))
}

function contrast(foreground: string, background: string): number {
  const first = luminance(foreground)
  const second = luminance(background)
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

/** Comments are blanked, keeping line numbers: prose names the hex it argues against. */
function withoutComments(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, match => '\n'.repeat(match.split('\n').length - 1))
    .replace(/^\s*\/\/.*$/gm, '')
}

type Palette = Record<string, string>
const themes = tokens.themes as Record<'light' | 'dark', Palette>

describe('the palette', () => {
  it('meets its declared contrast in both themes', () => {
    const failures: string[] = []
    for (const [theme, palette] of Object.entries(themes)) {
      for (const {foreground, background, minimum} of tokens.contrast_requirements) {
        const ratio = contrast(palette[foreground] ?? '', palette[background] ?? '')
        if (ratio < minimum) {
          failures.push(`${theme}: ${foreground} on ${background} is ${ratio.toFixed(2)}, ` +
                        `needs ${minimum}`)
        }
      }
    }
    expect(failures).toEqual([])
  })

  it('declares the same tokens in both themes', () => {
    expect(Object.keys(themes.dark).sort()).toEqual(Object.keys(themes.light).sort())
  })
})

describe('the generated sources', () => {
  it('carry the palette of this file into the panel stylesheet, both themes', () => {
    const generated = read('src/styles/_tokens.generated.scss')
    for (const [name, value] of Object.entries(themes.light)) {
      expect(generated).toContain(`--woa-${name}: ${value};`)
    }
    for (const [name, value] of Object.entries(themes.dark)) {
      expect(generated).toContain(`--woa-${name}: ${value};`)
    }
    for (const [name, value] of Object.entries(tokens.scale)) {
      expect(generated).toContain(`--woa-${name}: ${value};`)
    }
  })
})

describe('the handwritten styles', () => {
  it('reach only for tokens that are declared', () => {
    // A reference to a token nobody declares falls back to nothing, quietly.
    const declared = new Set([
      ...Object.keys(tokens.scale), ...Object.keys(themes.light),
    ].map(name => `--woa-${name}`))
    const used = [...withoutComments(read('src/styles/panel.scss'))
      .matchAll(/var\((--woa-[a-z0-9-]+)/g)].map(match => match[1] ?? '')
    expect([...new Set(used)].filter(name => !declared.has(name))).toEqual([])
  })
})
