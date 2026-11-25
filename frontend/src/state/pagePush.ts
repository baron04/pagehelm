/**
 * Make the host page narrower by the drawer's width, and give its width back afterwards.
 *
 * The root element is narrowed rather than given a margin, because host applications may set
 * `html { width: 100% }`, and a margin beside a full width only overflows.
 * `--woa-drawer-offset` and `data-woa-drawer-push` are maintained on the root element so
 * application integrations can offset viewport-fixed overlays and clamp wide dialogs/drawers
 * (`position: fixed`) away from the pushed drawer without altering closed-panel layout.
 */
import {useEffect} from 'react'

export function narrowPage(root: HTMLElement, width: number): () => void {
  const hadStyle = root.hasAttribute('style')
  const hadPush = root.hasAttribute('data-woa-drawer-push')
  const previousPush = root.getAttribute('data-woa-drawer-push')
  const previous = {
    value: root.style.getPropertyValue('width'),
    priority: root.style.getPropertyPriority('width'),
  }
  const previousOffset = {
    value: root.style.getPropertyValue('--woa-drawer-offset'),
    priority: root.style.getPropertyPriority('--woa-drawer-offset'),
  }
  const offset = `${Math.round(width)}px`
  const applied = `calc(100% - ${offset})`
  root.style.setProperty('width', applied, 'important')
  root.style.setProperty('--woa-drawer-offset', offset)
  root.setAttribute('data-woa-drawer-push', '')
  return () => {
    // An application that changed a property after the drawer did owns its new value.
    if (root.getAttribute('data-woa-drawer-push') === '') {
      if (hadPush && previousPush !== null) {
        root.setAttribute('data-woa-drawer-push', previousPush)
      } else {
        root.removeAttribute('data-woa-drawer-push')
      }
    }
    if (root.style.getPropertyValue('--woa-drawer-offset') === offset) {
      if (previousOffset.value) {
        root.style.setProperty('--woa-drawer-offset', previousOffset.value, previousOffset.priority)
      } else {
        root.style.removeProperty('--woa-drawer-offset')
      }
    }
    if (root.style.getPropertyValue('width') === applied) {
      if (previous.value) root.style.setProperty('width', previous.value, previous.priority)
      else root.style.removeProperty('width')
    }
    if (!hadStyle && root.getAttribute('style') === '') root.removeAttribute('style')
  }
}

/** Narrow the page while `width` is a number; restore it when it is null or on unmount. */
export function usePageNarrowing(width: number | null): void {
  useEffect(() => {
    if (width === null || typeof document === 'undefined') return
    return narrowPage(document.documentElement, width)
  }, [width])
}
