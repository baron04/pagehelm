/** Browser-only draft protection for a user's click on an answer link. */

function visible(element: Element, doc: Document): boolean {
  for (let parent: Element | null = element; parent; parent = parent.parentElement) {
    if (parent.hasAttribute('hidden') || parent.getAttribute('aria-hidden') === 'true') return false
    const style = doc.defaultView?.getComputedStyle(parent)
    if (style?.display === 'none' || style?.visibility === 'hidden') return false
  }
  return true
}

export function hasLocalDraft(doc: Document = document): boolean {
  for (const dialog of doc.querySelectorAll('dialog[open], [role="dialog"], [aria-modal="true"]')) {
    if (visible(dialog, doc)) return true
  }
  for (const form of doc.querySelectorAll('form')) {
    if (visible(form, doc) && form.method.toLowerCase() !== 'get') return true
  }
  for (const element of doc.querySelectorAll('input, textarea, select, [contenteditable="true"]')) {
    if (!visible(element, doc)) continue
    if (element instanceof HTMLInputElement) {
      if (['hidden', 'submit', 'button', 'reset', 'image'].includes(element.type)) continue
      if (element.type === 'file' && element.files?.length) return true
      if (['checkbox', 'radio'].includes(element.type)) {
        if (element.checked !== element.defaultChecked) return true
      } else if (element.value !== element.defaultValue) return true
    } else if (element instanceof HTMLTextAreaElement) {
      if (element.value !== element.defaultValue) return true
    } else if (element instanceof HTMLSelectElement) {
      if ([...element.options].some(option => option.selected !== option.defaultSelected)) return true
    } else return true // A rich editor has no reliable native baseline.
  }
  return false
}

/** Return false to leave the anchor's native new-tab click in charge. */
export function tryOpenAnswerInPlace(url: string, opener: (url: string) => Promise<boolean>,
                                     doc: Document = document, view: Window = window): boolean {
  if (hasLocalDraft(doc)) return false
  // Reserve the fallback while the click still has a user gesture. An asynchronous SPA
  // refusal cannot reliably open a fresh tab after that gesture has expired.
  const fallback = view.open('about:blank', '_blank')
  if (!fallback) return false
  fallback.opener = null
  void opener(url).then(opened => {
    if (opened) fallback.close()
    else if (!fallback.closed) fallback.location.replace(url)
  }).catch(() => {
    if (!fallback.closed) fallback.location.replace(url)
  })
  return true
}
