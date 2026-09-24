/**
 * The one keyboard shortcut the panel owns: open or put it away from anywhere on the page.
 *
 * ⌘J on a Mac, Ctrl+J elsewhere. Not ⌘K / Ctrl+K: Odoo opens its own command palette there,
 * and a host application's shortcut outranks an assistant's.
 */
export const ON_MAC = typeof navigator !== 'undefined'
  && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)

/** The shortcut as `aria-keyshortcuts` names it on this machine. */
export const SHORTCUT_ARIA = ON_MAC ? 'Meta+J' : 'Control+J'

export function isPanelShortcut(event: {
  key: string; metaKey: boolean; ctrlKey: boolean; altKey: boolean; shiftKey: boolean
}): boolean {
  if (event.key.toLowerCase() !== 'j' || event.altKey || event.shiftKey) return false
  return ON_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey
}
