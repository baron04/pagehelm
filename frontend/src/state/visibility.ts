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

export function useVisibilityGate(options: {
  minVisibleRatio: number
  minVisibleMs: number
}): VisibilityGate {
  const visibleSince = useRef(0)
  const observer = useRef<IntersectionObserver | null>(null)
  const {minVisibleRatio, minVisibleMs} = options

  const attach = useCallback((node: HTMLElement | null) => {
    observer.current?.disconnect()
    observer.current = null
    visibleSince.current = 0
    if (!node || typeof IntersectionObserver === 'undefined') return
    const created = new IntersectionObserver(records => {
      const record = records.at(-1)
      if (!record) return
      const visible = record.isIntersecting && record.intersectionRatio >= minVisibleRatio
      if (visible && !visibleSince.current) visibleSince.current = performance.now()
      if (!visible) visibleSince.current = 0
    }, {threshold: [0, minVisibleRatio, 1]})
    created.observe(node)
    observer.current = created
  }, [minVisibleRatio])

  useEffect(() => () => observer.current?.disconnect(), [])

  const refusalFor = useCallback((event: {isTrusted: boolean}) => {
    if (!event.isTrusted) return 'panel_not_visible'
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
      return 'panel_not_visible'
    }
    if (typeof IntersectionObserver === 'undefined') return ''
    if (!visibleSince.current) return 'panel_not_visible'
    if (performance.now() - visibleSince.current < minVisibleMs) return 'panel_not_visible'
    return ''
  }, [minVisibleMs])

  return {attach, refusalFor}
}
