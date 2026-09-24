/**
 * Put text on the clipboard from inside the host page.
 *
 * The asynchronous clipboard exists only in a secure context, and a host application is
 * often served over plain HTTP on an internal address. There the older copy command still
 * works while the press that asked for it is being handled, so it is the fallback: a
 * throwaway text area in the panel's own root, selected, copied, removed.
 */
export async function copyText(text: string, from: Element): Promise<boolean> {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // Refused (no permission, or the document lost focus): try the older way below.
  }
  const root = from.getRootNode() as Document | ShadowRoot
  const holder = document.createElement('textarea')
  holder.value = text
  holder.setAttribute('readonly', '')
  holder.style.position = 'fixed'
  holder.style.opacity = '0'
  holder.style.pointerEvents = 'none'
  const parent = root instanceof ShadowRoot ? root : document.body
  parent.appendChild(holder)
  try {
    holder.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    holder.remove()
  }
}
