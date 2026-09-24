/**
 * Make the host page narrower by the drawer's width, and give its width back afterwards.
 *
 * The root element is narrowed rather than given a margin, because applications set
 * `html { width: 100% }` -- Odoo does -- and a margin beside a full width only overflows.
 * Elements the page positions against the viewport (`position: fixed`) are not narrowed:
 * a dialog or a notification pinned to the right edge can sit partly under the drawer.
 * Narrowing those as well would mean making the body their containing block, which changes
 * how every fixed element of the application is placed; that is not this panel's to do.
 */
import {useEffect} from 'react'

export function narrowPage(root: HTMLElement, width: number): () => void {
  const hadStyle = root.hasAttribute('style')
  const previous = {
    value: root.style.getPropertyValue('width'),
    priority: root.style.getPropertyPriority('width'),
  }
  const applied = `calc(100% - ${Math.round(width)}px)`
  root.style.setProperty('width', applied, 'important')
  return () => {
    // An application that changed the width after the drawer did owns its new value.
    if (root.style.getPropertyValue('width') !== applied) return
    if (previous.value) root.style.setProperty('width', previous.value, previous.priority)
    else root.style.removeProperty('width')
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
