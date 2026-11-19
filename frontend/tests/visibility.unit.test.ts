import {createElement} from 'react'
import {createRoot} from 'react-dom/client'
import {act} from 'react'
import {describe, expect, it, vi} from 'vitest'
import {useVisibilityGate, type VisibilityGate} from '../src/state/visibility'

describe('useVisibilityGate tall approval card coverage', () => {
  it('unlocks approval after scrolling a card taller than 1.33x the panel scrollport into view', () => {
    const ioState: {cb: ((records: IntersectionObserverEntry[]) => void) | null; thresholds: readonly number[]} = {
      cb: null,
      thresholds: [],
    }
    const OriginalIO = globalThis.IntersectionObserver
    class MockIO {
      readonly thresholds: readonly number[]
      constructor(cb: (records: IntersectionObserverEntry[]) => void, opts?: IntersectionObserverInit) {
        ioState.cb = cb
        const t = opts?.threshold
        this.thresholds = Array.isArray(t) ? t : typeof t === 'number' ? [t] : [0]
        ioState.thresholds = this.thresholds
      }
      observe() {}
      unobserve() {}
      disconnect() {}
      takeRecords(): IntersectionObserverEntry[] { return [] }
    }
    globalThis.IntersectionObserver = MockIO as unknown as typeof IntersectionObserver

    let now = 1_000
    const nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => now)
    Object.defineProperty(window, 'innerWidth', {value: 1280, configurable: true})
    Object.defineProperty(window, 'innerHeight', {value: 800, configurable: true})

    const scrollport = document.createElement('div')
    scrollport.style.overflowY = 'auto'
    scrollport.getBoundingClientRect = () => ({
      left: 880, top: 100, right: 1240, bottom: 500, width: 360, height: 400, x: 880, y: 100, toJSON: () => ({}),
    } as DOMRect)
    const card = document.createElement('div')
    // Card height is 700px (> 1.33x the 400px scrollport -> max intersectionRatio = 400/700 = 0.571 < 0.75)
    let cardTop = 460
    card.getBoundingClientRect = () => ({
      left: 890, top: cardTop, right: 1230, bottom: cardTop + 700, width: 340, height: 700, x: 890, y: cardTop, toJSON: () => ({}),
    } as DOMRect)
    scrollport.append(card)
    document.body.append(scrollport)

    let gate: VisibilityGate | null = null
    function Probe() {
      gate = useVisibilityGate({minVisibleRatio: 0.75, minVisibleMs: 250})
      return createElement('div')
    }

    const host = document.createElement('div')
    document.body.append(host)
    const root = createRoot(host)
    try {
      act(() => { root.render(createElement(Probe)) })
      expect(gate).not.toBeNull()
      act(() => { gate!.attach(card) })
      expect(ioState.thresholds.length).toBeGreaterThanOrEqual(21)

      // Initially only 40px of the 700px card shows at the bottom of the 400px scrollport (40/400 = 0.10 < 0.75)
      ioState.cb?.([{
        isIntersecting: true,
        intersectionRatio: 40 / 700,
        intersectionRect: {width: 340, height: 40} as DOMRectReadOnly,
      } as IntersectionObserverEntry])
      now = 1_500
      expect(gate!.refusalFor({isTrusted: true})).toBe('insufficient_coverage')

      // User scrolls the tall card so it fills the entire 400px scrollport (coverage = 400/400 = 1.0 >= 0.75)
      cardTop = 50
      scrollport.dispatchEvent(new Event('scroll'))
      // Immediately before dwell elapses, click is still refused
      now = 1_600
      expect(gate!.refusalFor({isTrusted: true})).toBe('insufficient_dwell')
      // Once minVisibleMs (250ms) elapses, approval succeeds!
      now = 1_800
      expect(gate!.refusalFor({isTrusted: true})).toBe('')
    } finally {
      act(() => { root.unmount() })
      host.remove()
      scrollport.remove()
      nowSpy.mockRestore()
      globalThis.IntersectionObserver = OriginalIO
    }
  })
})
