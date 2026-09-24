/**
 * The window's two modes. Floating: drag by the header, resize from four edges and four
 * corners. Drawer: the full height of the viewport at its right edge, resized from its left
 * edge, with the page made narrower to make room for it.
 *
 * Without an iframe this is the panel's own DOM, so `setPointerCapture` works: the pointer
 * can leave the panel, or the window, and events keep arriving. No overlay, no forwarding.
 *
 * Geometry is one viewer's habit, not session state, so it lives in `localStorage` keyed by
 * origin. That is a different thing from the timeline, which is deliberately *not* cached
 * locally any more -- the server replays it (F8a).
 */
import {useCallback, useEffect, useRef, useState} from 'react'
import presentation from '../presentation.json'

export const MIN_WIDTH = 320
export const MIN_HEIGHT = 360
export const DEFAULT_WIDTH = 420
export const DEFAULT_HEIGHT = 680
/**
 * How far the launcher sits from the viewport's right and bottom edges. The launcher, the
 * standing entry before it, and a window nobody has moved all take their corner from here.
 */
export const DOCK_INSET = presentation.layout.launcher_inset
export const DRAWER_DEFAULT_WIDTH = 420
export const DRAWER_MIN_WIDTH = 320
/** The largest share of the viewport a drawer may take: the page beside it has to stay usable. */
const DRAWER_MAX_SHARE = 0.6
/**
 * Below this viewport width a drawer covers the page instead of narrowing it: the page left
 * beside it would be too narrow to work in.
 */
export const DRAWER_PUSH_MIN_VIEWPORT = 900
/**
 * Below this viewport width, or this height, the window is the whole screen in either mode.
 * A phone held upright is narrower than a floating window and its margins; one held on its
 * side is too short for one. Neither has room for a window beside the page.
 */
export const FULL_SCREEN_MAX_WIDTH = 600
export const FULL_SCREEN_MAX_HEIGHT = 480
const STORAGE_KEY = 'woa.panel.geometry.v1'
const DRAWER_KEY = 'woa.panel.drawer.v1'
const MODE_KEY = 'woa.panel.mode.v1'

export type DockMode = 'float' | 'drawer'

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
let openInThisDocument = false

export function loadOpen(): boolean {
  return openInThisDocument
}

/** Remember a person's choice only while this document lives.

An Agent restart replaces the Runtime inside the same Odoo document and should restore the
window the person was using. A browser refresh evaluates this module again and deliberately
returns to closed, preserving the ordinary page-load behaviour described above.
*/
export function rememberOpen(value: boolean): void {
  openInThisDocument = Boolean(value)
}

function viewport(): {width: number; height: number} {
  if (typeof window === 'undefined') return {width: 1280, height: 800}
  // The page's own scrollbars are not the panel's to cover. The overlay the window lives in
  // is fixed to the viewport *inside* them, which is what `clientWidth` measures;
  // `innerWidth` counts the scrollbar too, and put the window's edge under it.
  const root = document.documentElement
  return {width: root.clientWidth || window.innerWidth, height: root.clientHeight || window.innerHeight}
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
 * Where a window nobody has moved yet opens: out of the launcher, its bottom-right corner
 * on the launcher's own.
 *
 * The window replaces the button that was pressed, so it should appear where that button
 * was, the way a menu opens from its trigger. The position is a function of the viewport
 * at the moment of opening, never of the viewport when the panel was mounted: a host that
 * mounts into a document still being laid out would otherwise pin the window to its
 * top-left corner for the rest of the session.
 */
export function defaultGeometry(): Geometry {
  const view = viewport()
  const width = Math.min(DEFAULT_WIDTH, Math.max(MIN_WIDTH, view.width - DOCK_INSET * 2))
  const height = Math.min(DEFAULT_HEIGHT, Math.max(MIN_HEIGHT, view.height - DOCK_INSET * 2))
  return clamp({
    left: view.width - width - DOCK_INSET,
    top: view.height - height - DOCK_INSET,
    width,
    height,
  }, {width, height})
}

/** A drawer width the viewport can hold, never so wide that the page beside it vanishes. */
export function clampDrawer(width: number): number {
  const view = viewport()
  const widest = Math.max(DRAWER_MIN_WIDTH, Math.floor(view.width * DRAWER_MAX_SHARE))
  return Math.min(Math.max(DRAWER_MIN_WIDTH, width), widest, view.width)
}

/** Whether this viewport is too small for a window beside the page. */
export function fillsScreen(): boolean {
  const view = viewport()
  return view.width < FULL_SCREEN_MAX_WIDTH || view.height < FULL_SCREEN_MAX_HEIGHT
}

/** Whether a drawer at this viewport narrows the page or, on a narrow one, covers it. */
export function drawerPushes(): boolean {
  return viewport().width >= DRAWER_PUSH_MIN_VIEWPORT
}

function read(key: string): unknown {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? JSON.parse(raw) as unknown : null
  } catch {
    return null
  }
}

function write(key: string, value: unknown): void {
  try {
    if (value === null) window.localStorage.removeItem(key)
    else window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // A viewer who blocks site data simply gets the default placement every time.
  }
}

function load(): Geometry | null {
  const parsed = read(STORAGE_KEY)
  if (typeof parsed !== 'object' || parsed === null) return null
  const value = parsed as Partial<Geometry>
  if (!['left', 'top', 'width', 'height'].every(
    key => typeof value[key as keyof Geometry] === 'number')) return null
  return value as Geometry
}

function loadDrawer(): number | null {
  const value = read(DRAWER_KEY)
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function loadMode(): DockMode {
  return read(MODE_KEY) === 'drawer' ? 'drawer' : 'float'
}

export interface WindowGeometry {
  mode: DockMode
  /** The viewport is too small for either mode: the window covers it, and neither moves nor resizes. */
  fullScreen: boolean
  geometry: Geometry
  /** True until the person moves or resizes the floating window: it then stays on the launcher. */
  docked: boolean
  /** The drawer's width, and whether it narrows the page (false: it covers a narrow one). */
  drawer: {width: number; pushes: boolean}
  dragging: Handle | null
  onPointerDown: (handle: Handle) => (event: React.PointerEvent) => void
  toggleMode: () => void
}

export function useWindowGeometry(enabled: boolean): WindowGeometry {
  const [mode, setMode] = useState<DockMode>(loadMode)
  // A placement the person chose is theirs to keep; without one the window is docked, and
  // its position is worked out afresh on every opening and every viewport change.
  const [placed, setPlaced] = useState<Geometry | null>(load)
  const [docked, setDocked] = useState<Geometry>(defaultGeometry)
  const [drawerWidth, setDrawerWidth] = useState(
    () => clampDrawer(loadDrawer() ?? DRAWER_DEFAULT_WIDTH))
  const [pushes, setPushes] = useState(drawerPushes)
  const [fullScreen, setFullScreen] = useState(fillsScreen)
  const [dragging, setDragging] = useState<Handle | null>(null)
  // What the user last asked for, so a temporarily narrow viewport does not shrink the
  // window permanently: on the way back out it returns to this size.
  const preferred = useRef(placed ? {width: placed.width, height: placed.height} : null)
  const preferredDrawer = useRef(loadDrawer() ?? DRAWER_DEFAULT_WIDTH)
  const frame = useRef(0)

  const settle = useCallback(() => {
    setDocked(defaultGeometry())
    setPlaced(current => current && clamp(current, preferred.current ?? current))
    setDrawerWidth(clampDrawer(preferredDrawer.current))
    setPushes(drawerPushes())
    setFullScreen(fillsScreen())
  }, [])

  // Opening is when the launcher's corner is known to be where the person just clicked.
  useEffect(() => {
    if (enabled) settle()
  }, [enabled, settle])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const onResize = () => {
      cancelAnimationFrame(frame.current)
      frame.current = requestAnimationFrame(settle)
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      cancelAnimationFrame(frame.current)
    }
  }, [settle])

  // Every switch starts the other mode from its defaults: what was set in one mode says
  // nothing about where the other should be.
  const toggleMode = useCallback(() => {
    const next: DockMode = mode === 'float' ? 'drawer' : 'float'
    preferred.current = null
    preferredDrawer.current = DRAWER_DEFAULT_WIDTH
    setPlaced(null)
    setDocked(defaultGeometry())
    setDrawerWidth(clampDrawer(DRAWER_DEFAULT_WIDTH))
    setPushes(drawerPushes())
    write(STORAGE_KEY, null)
    write(DRAWER_KEY, null)
    write(MODE_KEY, next)
    setMode(next)
  }, [mode])

  const geometry = placed ?? docked

  const onPointerDown = useCallback((handle: Handle) => (event: React.PointerEvent) => {
    if (!enabled || fullScreen || event.button !== 0) return
    // The title bar is both the drag handle and the home of the window's own controls.
    // Capturing the pointer here would suppress their click, so a press that started on a
    // control is left alone: the buttons have to keep working.
    if (handle === 'move' && (event.target as HTMLElement).closest?.('button')) return
    // A drawer is not moved, and is resized only from the edge that faces the page.
    if (mode === 'drawer' && handle !== 'w') return
    event.preventDefault()
    const target = event.currentTarget as HTMLElement
    target.setPointerCapture(event.pointerId)
    const start = {x: event.clientX, y: event.clientY}
    const base = geometry
    const baseDrawer = drawerWidth
    let nextDrawer: number | null = null
    let next: Geometry | null = null
    setDragging(handle)

    const onMove = (move: PointerEvent) => {
      const dx = move.clientX - start.x
      const dy = move.clientY - start.y
      if (mode === 'drawer') {
        nextDrawer = clampDrawer(baseDrawer - dx)
        setDrawerWidth(nextDrawer)
        return
      }
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
      setPlaced(next)
    }
    const onUp = () => {
      target.releasePointerCapture?.(event.pointerId)
      target.removeEventListener('pointermove', onMove)
      target.removeEventListener('pointerup', onUp)
      target.removeEventListener('pointercancel', onUp)
      setDragging(null)
      if (nextDrawer !== null) {
        preferredDrawer.current = nextDrawer
        write(DRAWER_KEY, nextDrawer)
      }
      // A press that never moved is a click on the title bar, not a placement.
      if (!next) return
      if (handle !== 'move' || !preferred.current) {
        preferred.current = {width: next.width, height: next.height}
      }
      write(STORAGE_KEY, next)
    }
    target.addEventListener('pointermove', onMove)
    target.addEventListener('pointerup', onUp)
    target.addEventListener('pointercancel', onUp)
  }, [enabled, fullScreen, mode, geometry, drawerWidth])

  return {
    mode, fullScreen, geometry, docked: placed === null,
    drawer: {width: drawerWidth, pushes},
    dragging, onPointerDown, toggleMode,
  }
}
