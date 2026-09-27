import {describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {backoffAfter, classifyTransportError, PanelHost} from '../src/runtime/panelHost'
import type {PanelActions} from '../src/runtime/panelHost'
import type {TimelineEntry} from '../src/contract/panel'

function actions(): PanelActions {
  return {
    submit: vi.fn(), cancel: vi.fn(), decide: vi.fn(),
    newConversation: vi.fn(), loadOlder: vi.fn(),
  }
}

function entry(overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    id: 'e1', sequence: 1, turn_id: 't1', at: Date.now(), kind: 'goal', tone: 'neutral',
    text: 'hello', ...overrides,
  }
}

describe('classifyTransportError reads the server own table, once', () => {
  it('never retries an approval the server can no longer accept a decision for', () => {
    const verdict = classifyTransportError({code: 'approval_expired'}, DEFAULT_CONFIGURATION)
    expect(verdict).toMatchObject({terminal: false, state: 'ready', approvalClosed: true})
  })

  it('treats 401/404/409/403 as terminal even with no code from the server', () => {
    expect(classifyTransportError({status: 401}, DEFAULT_CONFIGURATION).terminal).toBe(true)
    expect(classifyTransportError({status: 404}, DEFAULT_CONFIGURATION).state).toBe('ended')
    expect(classifyTransportError({status: 409}, DEFAULT_CONFIGURATION).state).toBe('replaced')
    expect(classifyTransportError({status: 403}, DEFAULT_CONFIGURATION).terminal).toBe(true)
  })

  it('falls back to retryable reconnecting for an ordinary network fault', () => {
    expect(classifyTransportError(null, DEFAULT_CONFIGURATION)).toMatchObject(
      {terminal: false, retryable: true, state: 'reconnecting'})
  })

  it('honours the server configured terminal codes over the built-in status table', () => {
    const configured = {
      ...DEFAULT_CONFIGURATION,
      terminal_error_codes: ['session_closed'],
      terminal_panel_state_by_code: {session_closed: 'ended' as const},
    }
    expect(classifyTransportError({code: 'session_closed'}, configured))
      .toMatchObject({terminal: true, state: 'ended'})
  })
})

describe('backoffAfter grows with jitter and stays bounded', () => {
  it('never returns 0 and never exceeds the ceiling by more than the jitter band', () => {
    let previous = 0
    for (let i = 0; i < 20; i += 1) {
      previous = backoffAfter(previous)
      expect(previous).toBeGreaterThan(0)
      expect(previous).toBeLessThanOrEqual(4000 * 1.2)
    }
  })
})

describe('PanelHost enforces the protocol and the required actions at construction', () => {
  it('refuses a configuration with no protocol number', () => {
    expect(() => new PanelHost({configuration: {} as never, actions: actions()})).toThrow()
  })

  it('refuses a protocol mismatch', () => {
    expect(() => new PanelHost(
      {configuration: {...DEFAULT_CONFIGURATION, protocol: 999}, actions: actions()}))
      .toThrow(/protocol/)
  })

  it('refuses actions missing a required verb', () => {
    const incomplete = {...actions(), submit: undefined} as unknown as PanelActions
    expect(() => new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: incomplete}))
      .toThrow(/submit/)
  })
})

describe('PanelHost.applyTimeline merges by event id and is idempotent', () => {
  it('does not duplicate an entry seen twice', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const page = {entries: [entry()], cursor: 'c1', has_more: false, closed: false,
      pending_approval: null}
    host.applyTimeline(page)
    host.applyTimeline(page)
    expect(host.snapshot().entries).toHaveLength(1)
  })

  it('prepends older pages without disturbing the newer entries', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({entries: [entry({id: 'e2', sequence: 2})], has_more: true})
    host.applyTimeline({entries: [entry({id: 'e1', sequence: 1})], has_more: false}, {prepend: true})
    expect(host.snapshot().entries.map(item => item.id)).toEqual(['e1', 'e2'])
    expect(host.snapshot().hasOlder).toBe(false)
  })

  it('retires a matching local echo once the server accepts the real message', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.setOptimistic({turnId: 't1', text: 'hi', failed: false})
    expect(host.snapshot().optimistic).not.toBeNull()
    host.applyTimeline({entries: [entry({kind: 'goal', turn_id: 't1'})]})
    expect(host.snapshot().optimistic).toBeNull()
  })

  it('drops the oldest entries once the render budget is exceeded', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const budget = DEFAULT_CONFIGURATION.limits.rendered_entry_budget
    const entries = Array.from({length: budget + 5},
      (_, i) => entry({id: `e${i}`, sequence: i + 1}))
    host.applyTimeline({entries})
    expect(host.snapshot().entries).toHaveLength(budget)
    expect(host.snapshot().entries[0]?.id).toBe(`e5`)
  })

  it('shows sequence-zero history recovered after a server restart', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({entries: [entry({id: 'h00000001', sequence: 0, replayed: true})]})
    expect(host.snapshot().entries.map(item => item.id)).toEqual(['h00000001'])
  })
})

describe('PanelHost.clearTimeline raises a floor instead of erasing server history', () => {
  it('drops the view but a later page under the floor does not come back', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({entries: [entry({id: 'e1', sequence: 1})]})
    host.clearTimeline()
    expect(host.snapshot().entries).toHaveLength(0)
    host.applyTimeline({entries: [entry({id: 'e1', sequence: 1})]})
    expect(host.snapshot().entries).toHaveLength(0)
  })

  it('does not bring recovered history back after starting a new conversation', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const recovered = entry({id: 'h00000001', sequence: 0, replayed: true})
    host.applyTimeline({entries: [recovered]})
    host.clearTimeline()
    host.applyTimeline({entries: [recovered]})
    expect(host.snapshot().entries).toHaveLength(0)
  })
})

describe('PanelHost verbs the panel drives', () => {
  it('submit clears the notice and forwards the trimmed intent', () => {
    const act = actions()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: act})
    host.raise('some_code')
    host.submit('go')
    expect(host.snapshot().notice).toBeNull()
    expect(act.submit).toHaveBeenCalledWith('go')
  })

  it('retrySubmit resends the failed optimistic message and clears its failed flag', () => {
    const act = actions()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: act})
    host.setOptimistic({turnId: 't1', text: 'go', failed: true})
    host.retrySubmit()
    expect(host.snapshot().optimistic?.failed).toBe(false)
    expect(act.submit).toHaveBeenCalledWith('go')
  })

  it('loadOlder is a no-op when there is nothing older or a load is already in flight', () => {
    const act = actions()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: act})
    host.loadOlder()
    expect(act.loadOlder).not.toHaveBeenCalled()
  })
})

describe('examples the page declares after the panel went up', () => {
  it('are shown once offered, and only the first offer counts', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const seen = vi.fn()
    host.subscribe(seen)
    expect(host.examples).toEqual([])
    host.offerExamples([])
    expect(seen).not.toHaveBeenCalled()
    host.offerExamples([{kind: 'query', text: '列出所有待开票的销售订单'}])
    expect(host.examples).toEqual([{kind: 'query', text: '列出所有待开票的销售订单'}])
    expect(seen).toHaveBeenCalledTimes(1)
    host.offerExamples([{kind: 'find', text: '在哪里新建报价单'}])
    expect(host.examples).toEqual([{kind: 'query', text: '列出所有待开票的销售订单'}])
  })

  it('never replace the ones the page declared at the start', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions(),
                                examples: [{kind: 'find', text: '在哪里新建报价单'}]})
    host.offerExamples([{kind: 'query', text: '列出所有待开票的销售订单'}])
    expect(host.examples).toEqual([{kind: 'find', text: '在哪里新建报价单'}])
  })
})
