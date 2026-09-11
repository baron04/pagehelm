import {describe, expect, it} from 'vitest'
import {
  DEFAULT_CONFIGURATION, PANEL_PROTOCOL, isPanelState, statusLabelKey, toneOf,
} from '../src/contract/panel'
import {requirePanelHost} from '../src/bridge/host'

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
})
