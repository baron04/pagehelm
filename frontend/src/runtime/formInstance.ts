/** DOM lifetime facts; an instance token conveys no permission or application identity. */
const documents = new WeakMap<Document, {
  roots: Map<Element, {token: string; controls: Set<Element>}>, observer: MutationObserver, invalidate: (records: MutationRecord[]) => void,
}>()

export function formInstance(element: Element | null | undefined): string | null {
  const root = element?.closest?.('[role="dialog"],dialog') || element?.closest?.('form')
  if (!root?.isConnected) return null
  const document = root.ownerDocument
  let state = documents.get(document)
  if (!state) {
    const roots = new Map<Element, {token: string; controls: Set<Element>}>()
    const controls = 'input,textarea,select,button,form,dialog,[role="dialog"],[role="textbox"],[role="combobox"],[contenteditable="true"]'
    const currentControls = (node: Node): Element[] => node instanceof Element
      ? [...(node.matches(controls) ? [node] : []), ...node.querySelectorAll(controls)] : []
    const changesName = (owner: Element, target: Node) => {
      if (target instanceof Element && target.closest('label,legend,[role="heading"]')) return true
      const ids = ['aria-labelledby'].flatMap(a =>
        (owner.getAttribute(a) || '').split(/\s+/).filter(Boolean))
      return ids.some(id => owner.ownerDocument.getElementById(id)?.contains(target))
    }
    const invalidate = (records: MutationRecord[]) => {
      for (const [owner, entry] of roots) {
        if (!owner.isConnected || records.some(record => record.type === 'attributes'
          ? record.target === owner || (record.target instanceof Element && record.target.contains(owner))
          : Array.from(record.removedNodes).some(node => node === owner || node.contains(owner))
            || owner.contains(record.target) && (changesName(owner, record.target) ||
              Array.from(record.removedNodes).some(node => [...entry.controls].some(c => node === c || node.contains(c))) ||
              Array.from(record.addedNodes).some(node => currentControls(node).some(c =>
                c.isConnected && !entry.controls.has(c)))))) roots.delete(owner)
      }
    }
    const observer = new MutationObserver(invalidate)
    observer.observe(document.documentElement, {subtree: true, childList: true,
      attributes: true, attributeFilter: ['hidden', 'inert', 'open', 'aria-hidden', 'aria-label', 'aria-labelledby', 'aria-describedby', 'class', 'style']})
    state = {roots, observer, invalidate}
    documents.set(document, state)
  }
  state.invalidate(state.observer.takeRecords())
  if (!state.roots.has(root)) {
    if (state.roots.size >= 128) return null
    const controls = new Set(root.querySelectorAll('input,textarea,select,button,form,dialog,[role="dialog"],[role="textbox"],[role="combobox"],[contenteditable="true"]'))
    if (controls.size > 200) return null
    state.roots.set(root, {token: crypto.randomUUID(), controls})
  }
  return state.roots.get(root)!.token
}
