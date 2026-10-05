/** DOM lifetime facts; an instance token conveys no permission or application identity. */
type RemovalWindow = {anchor: Element; protected: Set<Element>}
type Entry = {token: string; controls: Set<Element>; removal?: RemovalWindow}
const documents = new WeakMap<Document, {
  roots: Map<Element, Entry>, observer: MutationObserver, invalidate: (records: MutationRecord[]) => void,
}>()

const formRoot = (element: Element | null | undefined) =>
  element?.closest?.('[role="dialog"],dialog') || element?.closest?.('form')

export function formInstance(element: Element | null | undefined): string | null {
  const root = formRoot(element)
  if (!root?.isConnected) return null
  const document = root.ownerDocument
  let state = documents.get(document)
  if (!state) {
    const roots = new Map<Element, Entry>()
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
              Array.from(record.removedNodes).some(node => [...entry.controls].some(c => {
                if (node !== c && !node.contains(c)) return false
                const window = entry.removal
                return !window || c.isConnected || !window.anchor.isConnected ||
                  node === window.anchor || node.contains(window.anchor) ||
                  [...window.protected].some(p => node === p || node.contains(p))
              })) ||
              Array.from(record.addedNodes).some(node => currentControls(node).some(c =>
                c.isConnected && !entry.controls.has(c)))))) roots.delete(owner)
        else if (entry.removal) {
          // Only detached, unprotected controls can leave during this fixed action.
          // New controls, replacements, editor naming and lifetime changes still revoke.
          if ([...entry.removal.protected].some(control => !control.isConnected || !owner.contains(control))) {
            roots.delete(owner)
          } else for (const control of entry.controls) if (!control.isConnected) entry.controls.delete(control)
        }
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

/** Tolerate removal-only layout changes during one verified local choice action.
 * This is DOM continuity, never permission or application identity. The server must
 * still revalidate its complete form descriptor and actor after the action receipt.
 */
export function localChoiceRemoval(anchor: Element, protectedControls: Iterable<Element>): () => void {
  const token = formInstance(anchor), root = formRoot(anchor)
  const state = root && documents.get(root.ownerDocument), entry = root && state?.roots.get(root)
  if (!token || !entry || !state || entry.removal) throw new Error('execution_failed: form continuity is unavailable')
  const removal = {anchor, protected: new Set([anchor, ...protectedControls])}
  entry.removal = removal
  return () => {
    state.invalidate(state.observer.takeRecords())
    if (entry.removal === removal) delete entry.removal
  }
}
