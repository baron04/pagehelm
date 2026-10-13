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
      // Against the card, or against the window when the card is the taller of the two.
      // A confirmation now sits in the conversation and has no height cap, so a long one
      // can be taller than the window it is in: asking for 90% of the card would make it
      // unapprovable, and asking for nothing would let a sliver of it count. What has to
      // be true either way is that the part of the screen it occupies is really showing
      // it -- so the ratio is measured against whichever is smaller.
      const box = record.boundingClientRect
      const view = record.rootBounds
      const shown = record.intersectionRect.width * record.intersectionRect.height
      let coverableHeight = view ? Math.min(box.height, view.height) : box.height
      let coverableWidth = view ? Math.min(box.width, view.width) : box.width
      // The conversation clips a long card inside the window. Measure the part
      // that can be shown through that scrollport, including its ancestor clips.
      for (let parent = node.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent)
        const clip = parent.getBoundingClientRect()
        if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) coverableHeight = Math.min(coverableHeight, clip.height)
        if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) coverableWidth = Math.min(coverableWidth, clip.width)
      }
      const coverable = coverableWidth * coverableHeight
      const ratio = coverable > 0 ? shown / coverable : 0
      const visible = record.isIntersecting && ratio >= minVisibleRatio
      if (visible && !visibleSince.current) visibleSince.current = performance.now()
      if (!visible) visibleSince.current = 0
    }, {threshold: [0, 0.25, 0.5, 0.75, minVisibleRatio, 1]})
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
