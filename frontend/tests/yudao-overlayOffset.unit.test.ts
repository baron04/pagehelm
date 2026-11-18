import {afterEach, describe, expect, it} from 'vitest'
import {narrowPage} from '../src/state/pagePush'
// @ts-expect-error Application module ships as framework-free JS.
import {installOverlayOffset} from '../src/integrations/yudao/form-surface.js'

const cleanups: Array<() => void> = []

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  document.head.innerHTML = ''
  document.documentElement.removeAttribute('style')
  if ('adoptedStyleSheets' in document && Array.isArray(document.adoptedStyleSheets)) {
    document.adoptedStyleSheets = []
  }
})

describe('Yudao Element Plus overlay drawer offset', () => {
  it('installs the --woa-drawer-offset rule for .el-overlay and .el-drawer__wrapper and cleans up on dispose', () => {
    const restoreNarrow = narrowPage(document.documentElement, 380)
    cleanups.push(restoreNarrow)
    const cleanup = installOverlayOffset(document)
    cleanups.push(cleanup)

    expect(document.documentElement.style.getPropertyValue('--woa-drawer-offset')).toBe('380px')
    const styleEl = document.getElementById('woa-yudao-overlay-offset')
    const sheetCss = Array.isArray(document.adoptedStyleSheets)
      ? document.adoptedStyleSheets.map(sheet => Array.from(sheet.cssRules).map(r => r.cssText).join('\n')).join('\n')
      : ''
    const appliedCss = styleEl?.textContent ?? sheetCss
    expect(appliedCss).toContain('.el-overlay, .el-drawer__wrapper')
    expect(appliedCss).toContain('right: var(--woa-drawer-offset, 0px)')

    cleanup()
    expect(document.getElementById('woa-yudao-overlay-offset')).toBeNull()
    if (Array.isArray(document.adoptedStyleSheets)) {
      expect(document.adoptedStyleSheets).toHaveLength(0)
    }
    restoreNarrow()
    expect(document.documentElement.style.getPropertyValue('--woa-drawer-offset')).toBe('')
  })

  it('refcounts multiple installers and tears down on pagehide', () => {
    const first = installOverlayOffset(document)
    const second = installOverlayOffset(document)
    cleanups.push(first, second)

    first()
    expect(document.getElementById('woa-yudao-overlay-offset')).not.toBeNull()

    window.dispatchEvent(new Event('pagehide'))
    expect(document.getElementById('woa-yudao-overlay-offset')).toBeNull()
  })

  it('propagates a CSP nonce onto fallback style tags when constructable stylesheets are unavailable', () => {
    const original = globalThis.CSSStyleSheet
    // Simulate an environment without constructable CSSStyleSheet support.
    Object.defineProperty(globalThis, 'CSSStyleSheet', {value: undefined, configurable: true})
    const meta = document.createElement('meta')
    meta.setAttribute('name', 'csp-nonce')
    meta.setAttribute('content', 'nonce-123')
    document.head.appendChild(meta)
    try {
      const cleanup = installOverlayOffset(document)
      cleanups.push(cleanup)
      const styleEl = document.getElementById('woa-yudao-overlay-offset') as HTMLStyleElement | null
      expect(styleEl).not.toBeNull()
      expect(styleEl?.nonce).toBe('nonce-123')
      expect(styleEl?.textContent).toContain('.el-overlay, .el-drawer__wrapper')
    } finally {
      Object.defineProperty(globalThis, 'CSSStyleSheet', {value: original, configurable: true})
    }
  })
})
