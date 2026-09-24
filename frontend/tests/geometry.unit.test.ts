import {describe, expect, it, vi} from 'vitest'
import {
  DOCK_INSET, DRAWER_MIN_WIDTH, MIN_HEIGHT, MIN_WIDTH, clamp, clampDrawer, defaultGeometry,
  drawerPushes, loadOpen, rememberOpen,
} from '../src/state/geometry'

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

describe('the open window survives only a same-document Runtime replacement', () => {
  it('remembers the state in module memory', () => {
    rememberOpen(false)
    expect(loadOpen()).toBe(false)
    rememberOpen(true)
    expect(loadOpen()).toBe(true)
    rememberOpen(false)
  })
})

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

describe('a window nobody has moved yet', () => {
  it('opens with its bottom-right corner on the launcher', () => {
    withViewport(1440, 900, () => {
      const {left, top, width, height} = defaultGeometry()
      expect(1440 - (left + width)).toBe(DOCK_INSET)
      expect(900 - (top + height)).toBe(DOCK_INSET)
    })
  })

  it('is placed by the viewport at the time it is asked, not an earlier one', () => {
    // A panel mounted while the document was still being laid out must not keep the
    // top-left corner that a zero-sized viewport clamped it to.
    withViewport(0, 0, () => {
      expect(defaultGeometry().left).toBe(0)
    })
    withViewport(1280, 800, () => {
      const {left, width} = defaultGeometry()
      expect(1280 - (left + width)).toBe(DOCK_INSET)
    })
  })

  it('keeps the composer on screen when the viewport is barely taller than the window', () => {
    withViewport(1024, 600, () => {
      const {top, height} = defaultGeometry()
      expect(top).toBeGreaterThanOrEqual(0)
      expect(top + height).toBeLessThanOrEqual(600)
    })
  })
})

describe('a drawer', () => {
  it('stays between its minimum and a share of the viewport that leaves the page usable', () => {
    withViewport(1440, 900, () => {
      expect(clampDrawer(100)).toBe(DRAWER_MIN_WIDTH)
      expect(clampDrawer(5000)).toBeLessThanOrEqual(1440 * 0.6)
    })
  })

  it('narrows the page only on a viewport wide enough to share', () => {
    withViewport(1440, 900, () => expect(drawerPushes()).toBe(true))
    withViewport(800, 600, () => expect(drawerPushes()).toBe(false))
  })
})
