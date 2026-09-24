import {describe, expect, it} from 'vitest'
import {narrowPage} from '../src/state/pagePush'

describe('narrowing the host page', () => {
  it('narrows the root by the drawer and leaves no trace when it gives the width back', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 420)
    expect(root.style.getPropertyValue('width')).toBe('calc(100% - 420px)')
    expect(root.style.getPropertyPriority('width')).toBe('important')
    restore()
    expect(root.hasAttribute('style')).toBe(false)
  })

  it('puts back a width the page had set itself', () => {
    const root = document.createElement('html')
    root.style.setProperty('width', '100%')
    narrowPage(root, 300)()
    expect(root.style.getPropertyValue('width')).toBe('100%')
    expect(root.style.getPropertyPriority('width')).toBe('')
  })

  it('leaves a width the application changed afterwards to the application', () => {
    const root = document.createElement('html')
    const restore = narrowPage(root, 300)
    root.style.setProperty('width', '50%')
    restore()
    expect(root.style.getPropertyValue('width')).toBe('50%')
  })
})
