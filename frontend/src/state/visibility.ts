/**
 * Refuse a confirmation the user could not actually have seen.
 *
 * With the panel in the host page's realm this cannot stop a script in that page from
 * forging a decision -- SECURITY.md says so plainly. What it does stop is *visual*
 * deception: something drawn over the card, or the window shrunk until the card is not
 * really on screen. Those are the cases a person would be tricked by, and they are worth
 * closing even though the stronger one is out of reach.
 */
import {useCallback, useEffect, useRef} from 'react'

export interface VisibilityGate {
  attach: (node: HTMLElement | null) => void
  /** '' when the gesture may proceed, otherwise the notice code explaining the refusal. */
  refusalFor: (event: {isTrusted: boolean}) => string
}

const CLIP = /(auto|scroll|hidden|clip)/

/**
 * How much of the panel's screen space the card really occupies, 0..1.
 *
 * A confirmation sits in the conversation and has no height cap, so a long one can be
 * taller than the window it is in: asking for 90% of the card would make it unapprovable,
 * and asking for nothing would let a sliver of it count. What has to be true either way is
 * that the part of the screen it occupies is really showing it -- so the denominator is
 * whichever is smaller: the card, the viewport, or the scrollport it scrolls inside.
 * The numerator is what shows right now: the observer's own clip when it gives one,
 * otherwise the card rect clamped by the viewport and every clipping ancestor.
 */
function coverage(node: HTMLElement, shownRect?: {width: number; height: number}): number {
  const box = node.getBoundingClientRect()
  const viewW = typeof window !== 'undefined' ? window.innerWidth : box.width
  const viewH = typeof window !== 'undefined' ? window.innerHeight : box.height
  let coverableWidth = Math.min(box.width, viewW)
  let coverableHeight = Math.min(box.height, viewH)
  let left = Math.max(box.left, 0)
  let top = Math.max(box.top, 0)
  let right = Math.min(box.right, viewW)
  let bottom = Math.min(box.bottom, viewH)
  for (let parent = node.parentElement; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent)
    const clip = parent.getBoundingClientRect()
    if (CLIP.test(style.overflowY)) {
      top = Math.max(top, clip.top)
      bottom = Math.min(bottom, clip.bottom)
      coverableHeight = Math.min(coverableHeight, clip.height)
    }
    if (CLIP.test(style.overflowX)) {
      left = Math.max(left, clip.left)
      right = Math.min(right, clip.right)
      coverableWidth = Math.min(coverableWidth, clip.width)
    }
  }
  const shown = shownRect ? shownRect.width * shownRect.height
    : Math.max(0, right - left) * Math.max(0, bottom - top)
  const coverable = coverableWidth * coverableHeight
  return coverable > 0 ? shown / coverable : 0
}

const THRESHOLDS = Array.from({length: 21}, (_, index) => index / 20)

export function useVisibilityGate(options: {
  minVisibleRatio: number
  minVisibleMs: number
}): VisibilityGate {
  const element = useRef<HTMLElement | null>(null)
  const mountedAt = useRef(0)
  const visibleSince = useRef(0)
  const reported = useRef(false)
  const observer = useRef<IntersectionObserver | null>(null)
  const cleanupListeners = useRef<(() => void) | null>(null)
  const {minVisibleRatio, minVisibleMs} = options

  const attach = useCallback((node: HTMLElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    cleanupListeners.current?.()
    cleanupListeners.current = null
    element.current = node
    mountedAt.current = node ? performance.now() : 0
    visibleSince.current = 0
    reported.current = false
    if (!node) return

    const syncCoverage = () => {
      if (!node.isConnected) {
        visibleSince.current = 0
        return
      }
      reported.current = true
      const visible = coverage(node) >= minVisibleRatio
      if (visible && !visibleSince.current) visibleSince.current = performance.now()
      if (!visible) visibleSince.current = 0
    }

    const targets: EventTarget[] = []
    if (typeof window !== 'undefined') {
      targets.push(window)
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent)
        if (CLIP.test(style.overflowY) || CLIP.test(style.overflowX)) targets.push(parent)
      }
      for (const target of targets) {
        target.addEventListener('scroll', syncCoverage, {passive: true})
        target.addEventListener('resize', syncCoverage, {passive: true})
      }
      cleanupListeners.current = () => {
        for (const target of targets) {
          target.removeEventListener('scroll', syncCoverage)
          target.removeEventListener('resize', syncCoverage)
        }
      }
    }

    if (typeof IntersectionObserver === 'undefined') return
    const thresholds = THRESHOLDS.includes(minVisibleRatio)
      ? THRESHOLDS
      : [...THRESHOLDS, minVisibleRatio].sort((a, b) => a - b)
    const created = new IntersectionObserver(records => {
      const record = records.at(-1)
      if (!record) return
      reported.current = true
      const ratio = coverage(node, record.intersectionRect)
      const visible = record.isIntersecting && (ratio >= minVisibleRatio || coverage(node) >= minVisibleRatio)
      if (visible && !visibleSince.current) visibleSince.current = performance.now()
      if (!visible) visibleSince.current = 0
    }, {threshold: thresholds})
    created.observe(node)
    observer.current = created
  }, [minVisibleRatio])

  useEffect(() => () => {
    observer.current?.disconnect()
    cleanupListeners.current?.()
  }, [])

  const refusalFor = useCallback((event: {isTrusted: boolean}) => {
    if (!event.isTrusted) return 'panel_not_visible'
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      return 'panel_not_visible'
    }
    // The click itself is the question: measure what it can see at this instant rather
    // than trusting observer records, which arrive on a lag a fast honest answer can
    // outrun. The dwell floor comes from the observer when it has spoken; only before
    // its first record does mount time stand in -- the earliest the card could have
    // been read -- so a card hidden until just now still fails the dwell.
    const node = element.current
    if (!node || !node.isConnected) return 'panel_not_visible'
    if (coverage(node) < minVisibleRatio) return 'panel_not_visible'
    const since = reported.current ? visibleSince.current : mountedAt.current
    if (!since || performance.now() - since < minVisibleMs) return 'panel_not_visible'
    return ''
  }, [minVisibleRatio, minVisibleMs])

  return {attach, refusalFor}
}
