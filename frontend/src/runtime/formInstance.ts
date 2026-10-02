/** DOM lifetime facts; an instance token conveys no permission or application identity. */
const documents = new WeakMap<Document, {
  roots: Map<Element, string>, observer: MutationObserver, invalidate: (records: MutationRecord[]) => void,
}>()

export function formInstance(element: Element | null | undefined): string | null {
  const root = element?.closest?.('[role="dialog"],dialog') || element?.closest?.('form')
  if (!root?.isConnected) return null
  const document = root.ownerDocument
  let state = documents.get(document)
  if (!state) {
    const roots = new Map<Element, string>()
    const invalidate = (records: MutationRecord[]) => {
      for (const owner of roots.keys()) {
        if (!owner.isConnected || records.some(record => record.type === 'attributes'
          ? record.target === owner || (record.target instanceof Element && record.target.contains(owner))
          : Array.from(record.removedNodes).some(node => node === owner || node.contains(owner))
            || owner.contains(record.target))) roots.delete(owner)
      }
    }
    const observer = new MutationObserver(invalidate)
    observer.observe(document.documentElement, {subtree: true, childList: true,
      attributes: true, attributeFilter: ['hidden', 'inert', 'open', 'aria-hidden', 'class', 'style']})
    state = {roots, observer, invalidate}
    documents.set(document, state)
  }
  state.invalidate(state.observer.takeRecords())
  if (!state.roots.has(root)) {
    if (state.roots.size >= 128) return null
    state.roots.set(root, crypto.randomUUID())
  }
  return state.roots.get(root)!
}
