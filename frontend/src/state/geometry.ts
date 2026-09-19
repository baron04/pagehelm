/**
 * The floating window: drag by the header, resize from four edges and four corners.
 *
 * Without an iframe this is the panel's own DOM, so `setPointerCapture` works: the pointer
 * can leave the panel, or the window, and events keep arriving. No overlay, no forwarding.
 *
 * Geometry is one viewer's habit, not session state, so it lives in `localStorage` keyed by
 * origin. That is a different thing from the timeline, which is deliberately *not* cached
 * locally any more -- the server replays it (F8a).
 */
import {useCallback, useEffect, useRef, useState} from 'react'

export const MIN_WIDTH = 320
export const MIN_HEIGHT = 360
export const DEFAULT_WIDTH = 420
export const DEFAULT_HEIGHT = 560
const EDGE_INSET = 16
/** Where the window's own box sits between the top of the viewport and the bottom of it. */
const BELOW_CENTRE = 0.68
const STORAGE_KEY = 'woa.panel.geometry.v1'

export type Handle = 'move' | 'n' | 's' | 'e' | 'w' | 'nw' | 'ne' | 'sw' | 'se'

export interface Geometry {
  left: number
  top: number
  width: number
  height: number
}

/**
 * A page that loads starts with the panel closed.
 *
 * It used to reopen wherever it had been left, so that a page the assistant itself
 * navigated came back with its panel. The cost fell on every other load: a person who
 * refreshed their own page got the assistant reopened over their work, having asked for
 * nothing. What actually needs attention says so on its own -- the launcher carries the
 * working and awaiting states, and a confirmation opens the window by itself.
 */
export function loadOpen(): boolean {
  return false
}

function viewport(): {width: number; height: number} {
  if (typeof window === 'undefined') return {width: 1280, height: 800}
  return {width: window.innerWidth, height: window.innerHeight}
}

/** Keep the window inside the viewport without forgetting what the user asked for. */
export function clamp(geometry: Geometry, preferred: {width: number; height: number}): Geometry {
  const view = viewport()
  // Below the minimum size the viewport wins outright: a window wider than the screen is
  // worse than a cramped one, and the confirmation card has to stay reachable.
  const width = Math.min(Math.max(MIN_WIDTH, preferred.width), Math.max(240, view.width))
  const height = Math.min(Math.max(MIN_HEIGHT, preferred.height), Math.max(240, view.height))
  return {
    width,
    height,
    left: Math.min(Math.max(0, geometry.left), Math.max(0, view.width - width)),
    top: Math.min(Math.max(0, geometry.top), Math.max(0, view.height - height)),
  }
}

/**
 * Where a window nobody has moved yet opens: against the right edge, sitting below the
 * middle of the viewport.
 *
 * An assistant belongs at the side -- it is a companion to the application, not a modal
 * over it, and no product of this kind parks itself across the middle of the page the
 * user is working in. What was wrong before was the *vertical* placement, not the side:
 * pinned 16px off the floor, the one control a person has to find sat in the last sliver
 * of the viewport and was the first thing a shorter window clipped.
 */
export function defaultGeometry(): Geometry {
  const view = viewport()
  const width = Math.min(DEFAULT_WIDTH, Math.max(MIN_WIDTH, view.width - EDGE_INSET * 2))
  const height = Math.min(DEFAULT_HEIGHT, Math.max(MIN_HEIGHT, view.height - EDGE_INSET * 2))
  const room = view.height - height
  return clamp({
    left: Math.max(EDGE_INSET, view.width - width - EDGE_INSET),
    // Below centre, but never past the inset: on a viewport barely taller than the window
    // the fraction would push the composer off the bottom, which is what this fixes.
    top: Math.round(Math.max(0, Math.min(room * BELOW_CENTRE, room - EDGE_INSET))),
    width,
    height,
  }, {width, height})
}

function load(): Geometry | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const value = parsed as Partial<Geometry>
    if (!['left', 'top', 'width', 'height'].every(
      key => typeof value[key as keyof Geometry] === 'number')) return null
    return value as Geometry
  } catch {
    return null
  }
}

function save(geometry: Geometry): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(geometry))
  } catch {
    // A viewer who blocks site data simply gets the default position every time.
  }
}

export interface WindowGeometry {
  geometry: Geometry
  dragging: Handle | null
  onPointerDown: (handle: Handle) => (event: React.PointerEvent) => void
  reset: () => void
}

export function useWindowGeometry(enabled: boolean): WindowGeometry {
  const [geometry, setGeometry] = useState<Geometry>(() => {
    const stored = load()
    return stored ? clamp(stored, stored) : defaultGeometry()
  })
  const [dragging, setDragging] = useState<Handle | null>(null)
  // What the user last asked for, so a temporarily narrow viewport does not shrink the
  // window permanently: on the way back out it returns to this size.
  const preferred = useRef({width: geometry.width, height: geometry.height})
  const frame = useRef(0)

  useEffect(() => {
    if (typeof window === 'undefined') return
    const onResize = () => {
      cancelAnimationFrame(frame.current)
      frame.current = requestAnimationFrame(() => {
        setGeometry(current => clamp(current, preferred.current))
      })
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      cancelAnimationFrame(frame.current)
    }
  }, [])

  const onPointerDown = useCallback((handle: Handle) => (event: React.PointerEvent) => {
    if (!enabled || event.button !== 0) return
    // The title bar is both the drag handle and the home of the window's own controls.
    // Capturing the pointer here would suppress their click, so a press that started on a
    // control is left alone: the buttons have to keep working.
    if (handle === 'move' && (event.target as HTMLElement).closest?.('button')) return
    event.preventDefault()
    const target = event.currentTarget as HTMLElement
    target.setPointerCapture(event.pointerId)
    const start = {x: event.clientX, y: event.clientY}
    let next = geometry
    setDragging(handle)

    const onMove = (move: PointerEvent) => {
      const dx = move.clientX - start.x
      const dy = move.clientY - start.y
      const base = geometry
      let {left, top, width, height} = base
      if (handle === 'move') {
        left = base.left + dx
        top = base.top + dy
      } else {
        if (handle.includes('e')) width = base.width + dx
        if (handle.includes('s')) height = base.height + dy
        if (handle.includes('w')) {
          width = base.width - dx
          left = base.left + dx
        }
        if (handle.includes('n')) {
          height = base.height - dy
          top = base.top + dy
        }
        if (width < MIN_WIDTH && handle.includes('w')) left = base.left + base.width - MIN_WIDTH
        if (height < MIN_HEIGHT && handle.includes('n')) top = base.top + base.height - MIN_HEIGHT
      }
      next = clamp({left, top, width, height}, {width, height})
      setGeometry(next)
    }
    const onUp = () => {
      target.releasePointerCapture?.(event.pointerId)
      target.removeEventListener('pointermove', onMove)
      target.removeEventListener('pointerup', onUp)
      target.removeEventListener('pointercancel', onUp)
      setDragging(null)
      if (handle !== 'move') preferred.current = {width: next.width, height: next.height}
      save(next)
    }
    target.addEventListener('pointermove', onMove)
    target.addEventListener('pointerup', onUp)
    target.addEventListener('pointercancel', onUp)
  }, [enabled, geometry])

  const reset = useCallback(() => {
    const fresh = defaultGeometry()
    preferred.current = {width: fresh.width, height: fresh.height}
    setGeometry(fresh)
    save(fresh)
  }, [])

  return {geometry, dragging, onPointerDown, reset}
}
