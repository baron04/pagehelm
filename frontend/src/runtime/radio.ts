/** A bounded, DOM-owned single-choice group; no application selectors or plans. */
import type {ElementLike} from './pageModel'
import browserContract from '@contract/browser_contract.json'

export function radioGroup(element: ElementLike | null | undefined): HTMLElement | null {
  if (typeof HTMLElement === 'undefined' || !(element instanceof HTMLElement)) return null
  if (element.getAttribute('role') !== 'radiogroup' &&
      !(element instanceof HTMLInputElement && element.type === 'radio') &&
      element.getAttribute('role') !== 'radio') return null
  const group = element.closest('[role="radiogroup"]')
  return group instanceof HTMLElement && group.isConnected &&
    group.getAttribute('aria-disabled') !== 'true' ? group : null
}

export function radioChoices(group: HTMLElement): HTMLElement[] {
  const candidates = [...group.querySelectorAll<HTMLElement>('input[type="radio"],[role="radio"]')]
    .filter(e => e.closest('[role="radiogroup"]') === group)
    // A wrapper with role=radio around a native input is one option, not two.
    .filter(e => e instanceof HTMLInputElement || !e.querySelector('input[type="radio"]'))
  if (!candidates.length || candidates.length > browserContract.form_handoff.max_choice_options) return []
  const native = candidates.filter((e): e is HTMLInputElement => e instanceof HTMLInputElement)
  if (native.length && (native.length !== candidates.length ||
      new Set(native.map(e => e.name)).size !== 1 || new Set(native.map(e => e.form)).size !== 1)) return []
  return candidates
}

export function radioLabel(element: HTMLElement): string | null {
  const text = element instanceof HTMLInputElement
    ? element.labels?.length === 1 ? element.labels[0]!.textContent : null
    : element.getAttribute('aria-label') || element.textContent
  const label = (text || '').replace(/\s+/g, ' ').trim()
  return label || null
}

export function radioEnabled(element: HTMLElement): boolean {
  return element.isConnected && !(element instanceof HTMLInputElement && element.disabled) &&
    !(element instanceof HTMLButtonElement && (element.disabled || element.type !== 'button')) &&
    !element.matches('a[href],input:not([type="radio"])') &&
    !element.closest('[aria-disabled="true"],fieldset[disabled]')
}

export function radioChecked(element: HTMLElement): boolean {
  return element instanceof HTMLInputElement ? element.checked : element.getAttribute('aria-checked') === 'true'
}

export function selectedRadio(group: HTMLElement): string | null {
  const options = radioChoices(group), selected = options.filter(radioChecked)
  const label = selected.length === 1 && radioEnabled(selected[0]!) ? radioLabel(selected[0]!) : null
  return label && options.filter(e => radioLabel(e) === label).length === 1 ? label : null
}

export function radioSelectionMatches(group: HTMLElement, label: string, value: string): boolean {
  if (selectedRadio(group) !== label) return false
  const selected = radioChoices(group).find(radioChecked)
  return !(selected instanceof HTMLInputElement) || selected.value === value
}
