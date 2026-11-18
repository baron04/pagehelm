import {describe, expect, it} from 'vitest'
import {narrowPage} from '../src/state/pagePush'

describe('narrowing the host page', () => {
  it('narrows the root by the drawer, sets --woa-drawer-offset, and leaves no trace when it gives the width back', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 420)
    expect(root.style.getPropertyValue('width')).toBe('calc(100% - 420px)')
    expect(root.style.getPropertyPriority('width')).toBe('important')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('420px')
    restore()
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('')
    expect(root.hasAttribute('style')).toBe(false)
  })

  it('puts back a width and drawer offset the page had set itself', () => {
    const root = document.createElement('html')
    root.style.setProperty('width', '100%')
    root.style.setProperty('--woa-drawer-offset', '12px')
    narrowPage(root, 300)()
    expect(root.style.getPropertyValue('width')).toBe('100%')
    expect(root.style.getPropertyPriority('width')).toBe('')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('12px')
  })

  it('leaves a width the application changed afterwards to the application while cleaning up its offset', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 300)
    root.style.setProperty('width', '50%')
    restore()
    expect(root.style.getPropertyValue('width')).toBe('50%')
    expect(root.style.getPropertyValue('--woa-drawer-offset')).toBe('')
  })
})
