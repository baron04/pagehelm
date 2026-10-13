import {soon} from './contain'

/** Ignore only our panel's hit targets during the controller's synchronous DOM sample. */
export function suspendPanelHitTargets(container: HTMLElement | undefined): () => void {
  if (!container?.isConnected) return () => {}
  const elements = new Set<HTMLElement>()
  const visit = (element: Element) => {
    if (element instanceof HTMLElement) elements.add(element)
    for (const child of element.children) visit(child)
    if (element.shadowRoot) for (const child of element.shadowRoot.children) visit(child)
  }
  visit(container)
  const saved = [...elements].map(element => ({element,
    value: element.style.getPropertyValue('pointer-events'),
    priority: element.style.getPropertyPriority('pointer-events')}))
  let restored = false
  const restore = () => {
    if (restored) return
    restored = true
    for (const {element, value, priority} of saved) {
      if (value) element.style.setProperty('pointer-events', value, priority)
      else element.style.removeProperty('pointer-events')
    }
  }
  try {
    for (const element of elements) element.style.setProperty('pointer-events', 'none', 'important')
  } catch (error) {
    restore()
    throw error
  }
  // A controller that yields or fails before afterUpdate cannot leave the panel
  // transparent to a later user event. The pinned extractor samples synchronously.
  soon('restore panel hit targets', restore)
  return restore
}
