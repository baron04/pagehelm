import type {PanelSnapshot} from '../bridge/host'

/** Read only geometry; opaque, hidden or offscreen targets have no cursor. */
export function activityPoint(element: Element | null | undefined): PanelSnapshot['activityPoint'] {
  try {
    if (!element?.isConnected || element.getClientRects().length === 0) return null
    const rect = element.getBoundingClientRect()
    let context = element.ownerDocument.defaultView
    if (!context || rect.width <= 0 || rect.height <= 0 ||
        context.getComputedStyle(element).visibility === 'hidden') return null
    let x = (Math.max(0, rect.left) + Math.min(context.innerWidth, rect.right)) / 2
    let y = (Math.max(0, rect.top) + Math.min(context.innerHeight, rect.bottom)) / 2
    if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= context.innerWidth ||
        rect.top >= context.innerHeight) return null
    while (context !== window) {
      const frame: Element | null = context.frameElement
      if (!frame) return null
      const box = frame.getBoundingClientRect()
      // Account for a scaled same-origin frame as well as its content border.
      const scaleX = box.width / (frame as HTMLElement).offsetWidth
      const scaleY = box.height / (frame as HTMLElement).offsetHeight
      x = box.left + (frame.clientLeft + x) * scaleX
      y = box.top + (frame.clientTop + y) * scaleY
      context = frame.ownerDocument.defaultView
      if (!context || x < 0 || y < 0 || x >= context.innerWidth || y >= context.innerHeight) return null
    }
    return Number.isFinite(x) && Number.isFinite(y) ? {x, y, at: Date.now()} : null
  } catch {
    return null
  }
}
