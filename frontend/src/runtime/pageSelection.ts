/** A bounded presentation fact independent of PageController's action-ref budget.
 *
 * Large views can cause PageController to omit a visible toolbar entirely. Reading one
 * selected name from each visible exclusive button group keeps completion checks sound
 * without exposing more actionable elements or serializing the full page.
 */
import type {Redactor} from './redaction'
import browserContract from '@contract/browser_contract.json'

const limits = browserContract.observation

function visibleInViewport(group: Element, root: Document): boolean {
  if (group.closest('[hidden], [aria-hidden="true"]')) return false
  const rect = group.getBoundingClientRect()
  const view = root.defaultView
  if (!view || rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0 ||
      rect.top >= view.innerHeight || rect.left >= view.innerWidth) return false
  const x = Math.max(0, Math.min(view.innerWidth - 1, rect.left + rect.width / 2))
  const y = Math.max(0, Math.min(view.innerHeight - 1, rect.top + rect.height / 2))
  const top = root.elementFromPoint?.(x, y)
  return !top || group.contains(top)
}

function selected(button: HTMLButtonElement): boolean {
  if (['aria-selected', 'aria-pressed', 'aria-checked'].some(
    key => button.getAttribute(key)?.toLowerCase() === 'true')) return true
  return button.classList.contains('active')
}

function accessibleName(button: HTMLButtonElement): string {
  return (button.getAttribute('aria-label') || button.getAttribute('title') ||
    button.getAttribute('data-tooltip') || button.textContent || '').trim()
}

export function selectedControlsFromPage(root: Document, redactor: Redactor): string[] {
  const found: string[] = []
  const groups = root.querySelectorAll('nav, [role="group"], [role="toolbar"]')
  for (const group of Array.from(groups).slice(0, limits.max_selection_groups)) {
    if (!visibleInViewport(group, root)) continue
    const buttons = group.querySelectorAll('button')
    if (buttons.length < 2 || buttons.length > limits.max_group_buttons) continue
    const active = Array.from(buttons).filter(selected)
    if (active.length !== 1) continue
    const raw = accessibleName(active[0] as HTMLButtonElement)
    if (!raw || redactor.isSensitiveName(raw) ||
        redactor.isSensitiveName(raw.replace(/\s+/g, '_'))) continue
    const name = redactor.redactString(raw).slice(0, limits.max_selected_control_chars)
    if (name && !found.includes(name)) found.push(name)
    if (found.length === limits.max_selected_controls) break
  }
  return found
}
