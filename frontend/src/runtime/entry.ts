/**
 * The standing entry: what the page shows where the assistant will be, before a panel exists.
 *
 * It appears while the first connection is still being made and whenever starting failed,
 * and it goes the moment a panel is mounted. It sits exactly where the panel's own launcher
 * will, at the same size, so the swap does not move anything under the person's pointer.
 * Its words are the contract's (`entry` in `panel_contract.json`), not this file's, and so
 * are its colours (`design_tokens.json`, through the palette this file imports).
 */
import {entry as contractEntry} from '@contract/panel_contract.json'
import {listen, type Cancel} from './contain'
import {ENTRY_DARK, ENTRY_LIGHT} from './entry-palette.generated'

export const ENTRY_ID = 'woa-assistant-entry'

export type EntryText = Omit<typeof contractEntry, 'comment'>
// Imported by name, so the bundle carries this section of the contract and not the rest.
export const ENTRY_TEXT: EntryText = contractEntry

/** Why starting failed: an HTTP status when the server answered, `null` when it did not. */
export interface EntryFailure {
  status: number | null
  /** Whether another attempt is already scheduled. */
  retrying: boolean
}

/** The sentence for a failure: its cause, then what happens next, then the status. */
export function failureSentence(failure: EntryFailure, text: EntryText = ENTRY_TEXT): string {
  const key = failure.status === null ? 'default' : String(failure.status)
  const causes = text.causes as Record<string, string>
  const cause = causes[key] ?? text.causes.default
  const final = failure.status !== null && text.final_statuses.includes(failure.status)
  const next = final ? '' : `，${failure.retrying ? text.retrying : text.stopped}`
  const status = failure.status === null ? '' : `（${failure.status}）`
  return `${cause}${next}${status}。`
}

export interface StandingEntry {
  /** Show that the assistant is on its way. */
  connecting(): void
  /** Show why it is not here, and a way to try again. */
  unavailable(failure: EntryFailure, retry: () => void): void
  /** The panel is up: go. */
  remove(): void
}

/** The roles the entry's own markup uses; the generator writes exactly these keys. */
interface EntryPalette {
  readonly ink: string
  readonly muted: string
  readonly card: string
  readonly line: string
  readonly lineStrong: string
  readonly accent: string
  readonly button: string
  readonly warning: string
  readonly shadow: string
}

// Annotating the generated literals: a renamed role fails the build here rather than
// interpolating `undefined` into a colour and rendering it as none.
const LIGHT: EntryPalette = ENTRY_LIGHT
const DARK: EntryPalette = ENTRY_DARK

const STYLE = `
  :host { all: initial }
  .entry {
    position: fixed; right: 20px; bottom: 20px;
    display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
    font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: ${LIGHT.ink};
  }
  .badge {
    position: relative; width: 52px; height: 52px; padding: 0; border-radius: 50%;
    border: 1px solid ${LIGHT.line}; background: ${LIGHT.card}; color: ${LIGHT.muted};
    box-shadow: ${LIGHT.shadow}; cursor: pointer; font: inherit;
  }
  .badge:focus-visible { outline: 2px solid ${LIGHT.accent}; outline-offset: 2px }
  .ring {
    position: absolute; inset: -4px; border-radius: 50%;
    border: 2px solid transparent; border-top-color: ${LIGHT.accent};
    animation: spin 1s linear infinite; display: none;
  }
  .entry[data-state='connecting'] .ring { display: block }
  .dot {
    position: absolute; top: 2px; right: 2px; width: 10px; height: 10px; border-radius: 50%;
    background: ${LIGHT.warning}; box-shadow: 0 0 0 2px ${LIGHT.card}; display: none;
  }
  .entry[data-state='unavailable'] .dot { display: block }
  .note {
    max-width: 260px; padding: 10px 12px; border-radius: 10px; background: ${LIGHT.card};
    border: 1px solid ${LIGHT.line}; box-shadow: ${LIGHT.shadow};
  }
  .note p { margin: 0 }
  .note button {
    margin-top: 8px; font: inherit; padding: 4px 10px; border-radius: 6px; cursor: pointer;
    border: 1px solid ${LIGHT.lineStrong}; background: ${LIGHT.button}; color: inherit;
  }
  .note button[hidden] { display: none }
  @keyframes spin { to { transform: rotate(360deg) } }
  @media (prefers-reduced-motion: reduce) { .ring { animation: none; border-color: ${LIGHT.accent} } }
  @media (prefers-color-scheme: dark) {
    .entry { color: ${DARK.ink} }
    .badge, .note { background: ${DARK.card}; border-color: ${DARK.line}; color: ${DARK.muted} }
    .badge:focus-visible { outline-color: ${DARK.accent} }
    .ring { border-top-color: ${DARK.accent} }
    .dot { background: ${DARK.warning}; box-shadow: 0 0 0 2px ${DARK.card} }
    .note button { background: ${DARK.button}; border-color: ${DARK.lineStrong} }
  }`

/**
 * A constructed stylesheet, as the panel uses: a host page running `style-src 'self'` would
 * refuse an injected `<style>` element, and then the entry would render unstyled.
 */
function adoptStyle(shadow: ShadowRoot): void {
  if (typeof CSSStyleSheet === 'undefined' || !('adoptedStyleSheets' in shadow)) return
  const sheet = new CSSStyleSheet()
  if (typeof sheet.replaceSync !== 'function') return
  sheet.replaceSync(STYLE)
  shadow.adoptedStyleSheets = [sheet]
}

interface Parts {
  host: HTMLElement
  entry: HTMLElement
  badge: HTMLButtonElement
  note: HTMLElement
  sentence: HTMLParagraphElement
  again: HTMLButtonElement
  stops: Cancel[]
}

/** The page's standing entry, created on first use and removed with `remove()`. */
export function standingEntry(doc: Document = document, text: EntryText = ENTRY_TEXT): StandingEntry {
  let parts: Parts | null = null
  let onRetry: () => void = () => {}

  const build = (): Parts => {
    if (parts) return parts
    const host = doc.createElement('div')
    host.id = ENTRY_ID
    host.setAttribute('data-page-agent-ignore', 'true')
    host.style.setProperty('z-index', '2147483000', 'important')
    const shadow = host.attachShadow({mode: 'open'})
    adoptStyle(shadow)
    const entry = doc.createElement('div')
    entry.className = 'entry'
    const note = doc.createElement('div')
    note.className = 'note'
    note.hidden = true
    note.setAttribute('role', 'status')
    const sentence = doc.createElement('p')
    const again = doc.createElement('button')
    again.type = 'button'
    again.textContent = text.retry
    note.append(sentence, again)
    const badge = doc.createElement('button')
    badge.type = 'button'
    badge.className = 'badge'
    badge.textContent = text.badge
    const ring = doc.createElement('span')
    ring.className = 'ring'
    const dot = doc.createElement('span')
    dot.className = 'dot'
    badge.append(ring, dot)
    entry.append(note, badge)
    shadow.append(entry)
    const stops = [
      listen('entry badge', badge, 'click', () => { note.hidden = !note.hidden }),
      listen('entry retry', again, 'click', () => {
        again.disabled = true
        onRetry()
      }),
    ]
    ;(doc.body || doc.documentElement).appendChild(host)
    parts = {host, entry, badge, note, sentence, again, stops}
    return parts
  }

  return {
    connecting() {
      const view = build()
      view.entry.dataset.state = 'connecting'
      view.badge.setAttribute('aria-label', text.label_connecting)
      view.sentence.textContent = text.connecting
      view.again.hidden = true
    },
    unavailable(failure, retry) {
      const view = build()
      onRetry = retry
      view.entry.dataset.state = 'unavailable'
      view.badge.setAttribute('aria-label', text.label_unavailable)
      view.sentence.textContent = failureSentence(failure, text)
      view.again.hidden = false
      view.again.disabled = false
    },
    remove() {
      if (!parts) return
      for (const stop of parts.stops) stop()
      parts.host.remove()
      parts = null
    },
  }
}
