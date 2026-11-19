import {afterEach, beforeEach, describe, expect, it} from 'vitest'
import {targetView} from '../src/handoff/visibility'

describe('handoff targetView without outside clip check', () => {
  let root: HTMLDivElement
  let panel: HTMLDivElement
  let scrollHost: HTMLDivElement
  let button: HTMLButtonElement

  beforeEach(() => {
    for (const el of [document.documentElement, document.body]) {
      el.style.visibility = 'visible'
      el.style.opacity = '1'
    }
    root = document.createElement('div')
    root.style.visibility = 'visible'
    root.style.opacity = '1'
    panel = document.createElement('div')
    scrollHost = document.createElement('div')
    scrollHost.style.overflow = 'auto'
    scrollHost.style.visibility = 'visible'
    scrollHost.style.opacity = '1'
    scrollHost.getBoundingClientRect = () => ({
      left: 0, top: 0, right: 600, bottom: 400, width: 600, height: 400, x: 0, y: 0, toJSON: () => ({}),
    })
    button = document.createElement('button')
    button.style.visibility = 'visible'
    button.style.opacity = '1'
    button.textContent = '确定'
    button.getBoundingClientRect = () => ({
      left: 300, top: 480, right: 380, bottom: 516, width: 80, height: 36, x: 300, y: 480, toJSON: () => ({}),
    })
    scrollHost.append(button)
    root.append(panel, scrollHost)
    document.body.append(root)
    document.elementFromPoint = () => button
  })

  afterEach(() => {
    root.remove()
  })

  it('keeps target visible even when extending past an ancestor overflow container', () => {
    const view = targetView(button, panel)
    expect(view.kind).toBe('visible')
  })

  it('still detects hidden, disabled, assistant-covered, and element-covered targets', () => {
    button.setAttribute('aria-hidden', 'true')
    expect(targetView(button, panel)).toEqual({kind: 'hidden'})

    button.removeAttribute('aria-hidden')
    button.disabled = true
    expect(targetView(button, panel)).toEqual({kind: 'disabled'})

    button.disabled = false
    document.elementFromPoint = () => panel
    expect(targetView(button, panel)).toEqual({kind: 'assistant'})

    const other = document.createElement('div')
    root.append(other)
    document.elementFromPoint = () => other
    expect(targetView(button, panel)).toEqual({kind: 'covered'})
  })
})
