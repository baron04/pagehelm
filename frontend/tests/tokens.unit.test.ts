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

  it('keeps the brand disc the same object in both themes', () => {
    // The launcher disc is one brand moment; a theme must not quietly fork it.
    for (const name of ['brand-top', 'brand-mid', 'brand-bottom', 'brand-ink']) {
      expect(themes.dark[name], name).toBe(themes.light[name])
    }
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
  /*
   * P3 put every size, radius and colour onto the scale; this keeps them there. Four rules,
   * each earned by a defect: font-size and line-height are token references, border-radius
   * too (a percentage is allowed only for a true circle); no solid colour is written inline
   * anywhere, the shadow rgba surviving because a shadow is light, not colour; the white
   * that lights a highlight comes from the sheen tokens, except inside the brand disc -- the
   * one object allowed its own fixed light, which the test above pins as equal across
   * themes; and the entry, which cannot inherit a token file because it renders outside any
   * panel, states no colour in its own words, only in the palette generated for it.
   */
  it('quote the scale rather than restate it', () => {
    const disc = /\.woa-(launcher__disc|mark)\b/
    const problems: string[] = []
    let selector = ''
    withoutComments(read('src/styles/panel.scss')).split('\n').forEach((line, index) => {
      const at = `panel.scss:${index + 1}`
      const opened = /^([^{};]*)\{/.exec(line)
      if (opened) selector = opened[1] ?? ''
      if (!line.trim()) return
      for (const [property, prefix] of [['font-size', '--woa-font-size'],
                                        ['line-height', '--woa-line-height']]) {
        const value = new RegExp(`\\b${property}\\s*:\\s*([^;]+)`).exec(line)
        if (value?.[1] !== undefined && !value[1].startsWith(`var(${prefix}`)) {
          problems.push(`${at} ${property}: ${value[1]}`)
        }
      }
      const radius = /\bborder-radius\s*:\s*([^;]+)/.exec(line)
      if (radius?.[1] !== undefined && !radius[1].split(/\s+/).every(
        token => token.startsWith('var(--woa-radius') || token === '50%')) {
        problems.push(`${at} border-radius: ${radius[1]}`)
      }
      if (/#[0-9a-fA-F]{3,8}\b/.test(line)) problems.push(`${at} colour literal: ${line.trim()}`)
      if (/rgba\(\s*255,\s*255,\s*255/.test(line) && !disc.test(selector)) {
        problems.push(`${at} white highlight outside the brand disc`)
      }
    })
    if (/#[0-9a-fA-F]{3,8}\b/.test(withoutComments(read('src/runtime/entry.ts')))) {
      problems.push('entry.ts states a colour of its own')
    }
    expect(problems).toEqual([])
  })

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
