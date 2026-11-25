import {describe, expect, it} from 'vitest'
import {narrowPage} from '../src/state/pagePush'

describe('narrowing the host page', () => {
  it('narrows the root by the drawer, sets --woa-drawer-offset and data-woa-drawer-push, and leaves no trace when it gives the width back', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 420)
    expect(root.style.getPropertyValue('width')).toBe('calc(100% - 420px)')
    expect(root.style.getPropertyPriority('width')).toBe('important')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('420px')
    expect(root.hasAttribute('data-woa-drawer-push')).toBe(true)
    restore()
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('')
    expect(root.hasAttribute('style')).toBe(false)
    expect(root.hasAttribute('data-woa-drawer-push')).toBe(false)
  })

  it('puts back a width, drawer offset, and push attribute the page had set itself', () => {
    const root = document.createElement('html')
    root.style.setProperty('width', '100%')
    root.style.setProperty('--woa-drawer-offset', '12px')
    root.setAttribute('data-woa-drawer-push', 'custom')
    narrowPage(root, 300)()
    expect(root.style.getPropertyValue('width')).toBe('100%')
    expect(root.style.getPropertyPriority('width')).toBe('')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('12px')
    expect(root.getAttribute('data-woa-drawer-push')).toBe('custom')
  })

  it('leaves a width or push attribute the application changed afterwards to the application while cleaning up its offset', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 300)
    root.style.setProperty('width', '50%')
    root.setAttribute('data-woa-drawer-push', 'app-owned')
    restore()
    expect(root.style.getPropertyValue('width')).toBe('50%')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('')
    expect(root.getAttribute('data-woa-drawer-push')).toBe('app-owned')
  })
})
