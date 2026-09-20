import {describe, expect, it} from 'vitest'
import raw from '@contract/panel_contract.json'
import rawTokens from '@contract/design_tokens.json'
import {
  DEFAULT_CONFIGURATION, PANEL_PROTOCOL, isPanelState, statusLabelKey, toneOf,
} from '../src/contract/panel'
import {requirePanelHost} from '../src/bridge/host'
import {ENTRY_DARK, ENTRY_LIGHT} from '../src/runtime/entry-palette.generated'

const actions = {
  snapshot: () => ({}), subscribe: () => () => undefined, submit: () => undefined,
  retrySubmit: () => undefined, cancel: () => undefined, decide: () => undefined,
  newConversation: () => undefined, loadOlder: () => undefined,
  dismissNotice: () => undefined, reload: () => undefined,
}

describe('the contract is the server\'s, not a copy', () => {
  it('gives every state a tone and a status label', () => {
    for (const state of Object.keys(DEFAULT_CONFIGURATION.panel_states)) {
      expect(isPanelState(state)).toBe(true)
      expect(toneOf(state as never)).toBeTruthy()
      expect(DEFAULT_CONFIGURATION.labels[statusLabelKey(state as never)]).toBeTruthy()
    }
  })

  it('maps every progress state onto a state it also knows', () => {
    for (const mapping of Object.values(DEFAULT_CONFIGURATION.progress_states)) {
      expect(isPanelState(mapping.panel_state)).toBe(true)
    }
  })

  it('refuses an invented state rather than rendering a blank pill', () => {
    expect(isPanelState('invented')).toBe(false)
    expect(() => statusLabelKey('invented' as never)).toThrow()
  })

  it('offers the server\'s suggestions, and gives the composer limit words', () => {
    expect(DEFAULT_CONFIGURATION.suggestions.length).toBeGreaterThan(0)
    for (const item of DEFAULT_CONFIGURATION.suggestions) {
      expect(item.text.trim()).toBeTruthy()
      expect(item.kind.trim()).toBeTruthy()
    }
    expect(DEFAULT_CONFIGURATION.labels.composerAtLimit).toBeTruthy()
  })
})

describe('the host contract is checked where it is assembled', () => {
  it('accepts a complete host', () => {
    expect(() => requirePanelHost({
      ...actions, configuration: DEFAULT_CONFIGURATION,
    })).not.toThrow()
  })

  it('names the missing member instead of probing for it later', () => {
    const incomplete: Record<string, unknown> = {...actions}
    delete incomplete.cancel
    expect(() => requirePanelHost({...incomplete, configuration: DEFAULT_CONFIGURATION}))
      .toThrow(/missing cancel\(\)/)
  })

  it('refuses a host with no server configuration', () => {
    expect(() => requirePanelHost({...actions})).toThrow(/no server configuration/)
    expect(PANEL_PROTOCOL).toBe(DEFAULT_CONFIGURATION.protocol)
  })

  // The panel and the Runtime both check the server's `protocol`; both read it from the
  // file `scripts/build-tokens.mjs` generates. This fails if that script stops being run
  // -- a stale integer in a generated file is exactly the drift the two hand-written
  // copies used to invite.
  it('states the protocol as the contract states it, not as the generator last ran', () => {
    expect(PANEL_PROTOCOL).toBe(raw.protocol)
  })

  const entryRoles: Array<[keyof typeof ENTRY_LIGHT, keyof typeof rawTokens.themes.light]> = [
    ['ink', 'ink'], ['muted', 'ink-muted'], ['card', 'surface-raised'], ['line', 'line'],
    ['lineStrong', 'line-strong'], ['accent', 'accent'], ['button', 'surface-muted'],
    ['warning', 'warning'], ['shadow', 'shadow-2'],
  ]

  // The standing entry renders before the panel's shadow root exists, so it cannot inherit
  // the custom properties and carries generated literals instead. This fails when someone
  // edits `design_tokens.json` and forgets the build -- the drift the hand-copied palette
  // (`#2557dd` next to an accent of `#1d4ed8`) used to hide.
  it('gives the entry the palette as the tokens state it now, not as the generator last ran', () => {
    for (const [entry, token] of entryRoles) {
      expect(ENTRY_LIGHT[entry]).toBe(rawTokens.themes.light[token])
      expect(ENTRY_DARK[entry]).toBe(rawTokens.themes.dark[token])
    }
  })
})
