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

describe('PanelHost keeps one conversation across the channels that carry it', () => {
  // The agent restarted, or the session ended and the next message made another, while this
  // page stayed open. The new channel numbers its events from one again.
  const first = [
    entry({id: 'e00000002', sequence: 2, turn_id: 't1', kind: 'goal', text: 'first question'}),
    entry({id: 'e00000003', sequence: 3, turn_id: 't1', kind: 'result', text: 'first answer'}),
  ]

  it('shows a new channel\'s lines although an earlier one used the same ids', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({incarnation: 'a', entries: first})
    host.setOptimistic({turnId: 't2', text: 'second question', failed: false})
    host.applyTimeline({incarnation: 'b', entries: [
      entry({id: 'e00000002', sequence: 2, turn_id: 't2', kind: 'goal', text: 'second question'}),
      entry({id: 'e00000003', sequence: 3, turn_id: 't2', kind: 'result', text: 'second answer'}),
    ]})
    expect(host.snapshot().entries.map(item => item.text)).toEqual(
      ['first question', 'first answer', 'second question', 'second answer'])
    expect(host.snapshot().optimistic).toBeNull()
  })

  it('still takes a line it was given twice by the same channel for one line', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({incarnation: 'a', entries: first})
    host.applyTimeline({incarnation: 'a', entries: first})
    expect(host.snapshot().entries).toHaveLength(2)
  })

  it('does not add a second time what the new channel replays of a turn on screen', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({incarnation: 'a', entries: first})
    const replay = [
      entry({id: 'h00000001', sequence: 0, replayed: true, turn_id: 't0', kind: 'goal', text: 'before this page'}),
      entry({id: 'h00000002', sequence: 0, replayed: true, turn_id: 't1', kind: 'goal', text: 'first question'}),
      entry({id: 'h00000003', sequence: 0, replayed: true, turn_id: 't1', kind: 'result', text: 'first answer'}),
    ]
    host.applyTimeline({incarnation: 'b', entries: replay})
    host.applyTimeline({incarnation: 'b', entries: replay}, {prepend: true})
    expect(host.snapshot().entries.map(item => item.text)).toEqual(
      ['first question', 'first answer', 'before this page'])
  })

  it('does not hold a new channel\'s lines under the place an old conversation was cleared', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({incarnation: 'a', entries: [entry({id: 'e00000050', sequence: 50})]})
    host.clearTimeline()
    host.applyTimeline({incarnation: 'a', entries: [entry({id: 'e00000050', sequence: 50})]})
    expect(host.snapshot().entries).toHaveLength(0)
    host.applyTimeline({incarnation: 'b', entries: [
      entry({id: 'h00000001', sequence: 0, replayed: true, turn_id: 't0', text: 'cleared'}),
      entry({id: 'e00000002', sequence: 2, turn_id: 't2', text: 'asked after'}),
    ]})
    expect(host.snapshot().entries.map(item => item.text)).toEqual(['asked after'])
  })

  it('changes nothing for a server that does not say which channel a page is from', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({entries: first})
    host.applyTimeline({entries: first})
    expect(host.snapshot().entries).toHaveLength(2)
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
