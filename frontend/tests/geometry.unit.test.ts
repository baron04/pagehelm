import {describe, expect, it, vi} from 'vitest'
import {MIN_HEIGHT, MIN_WIDTH, clamp} from '../src/state/geometry'

function withViewport(width: number, height: number, run: () => void) {
  const original = {width: window.innerWidth, height: window.innerHeight}
  vi.stubGlobal('innerWidth', width)
  vi.stubGlobal('innerHeight', height)
  try {
    run()
  } finally {
    vi.stubGlobal('innerWidth', original.width)
    vi.stubGlobal('innerHeight', original.height)
  }
}

describe('the window stays reachable', () => {
  it('pulls a window that would hang off the edge back inside', () => {
    withViewport(1280, 800, () => {
      const result = clamp({left: 2000, top: 2000, width: 376, height: 560},
                           {width: 376, height: 560})
      expect(result.left + result.width).toBeLessThanOrEqual(1280)
      expect(result.top + result.height).toBeLessThanOrEqual(800)
    })
  })

  it('never shrinks below the minimum while the viewport can still hold it', () => {
    withViewport(1280, 800, () => {
      const result = clamp({left: 0, top: 0, width: 100, height: 100},
                           {width: 100, height: 100})
      expect(result.width).toBe(MIN_WIDTH)
      expect(result.height).toBe(MIN_HEIGHT)
    })
  })

  it('gives a viewport narrower than the minimum the whole viewport', () => {
    withViewport(300, 300, () => {
      const result = clamp({left: 40, top: 40, width: 376, height: 560},
                           {width: 376, height: 560})
      expect(result.left).toBe(0)
      expect(result.top).toBe(0)
      expect(result.width).toBe(300)
      expect(result.height).toBe(300)
    })
  })

  it('remembers what the user asked for rather than the size a narrow viewport forced', () => {
    // A window squeezed by a temporarily narrow viewport comes back when there is room.
    withViewport(400, 400, () => {
      const squeezed = clamp({left: 0, top: 0, width: 376, height: 560},
                             {width: 900, height: 700})
      expect(squeezed.width).toBe(400)
    })
    withViewport(1600, 1000, () => {
      const restored = clamp({left: 0, top: 0, width: 400, height: 400},
                             {width: 900, height: 700})
      expect(restored.width).toBe(900)
      expect(restored.height).toBe(700)
    })
  })
})
