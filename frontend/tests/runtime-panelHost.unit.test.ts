import {describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {postApprovalRefusal, type ApprovalContext} from '../src/runtime/approvalController'
import {backoffAfter, classifyTransportError, PanelHost} from '../src/runtime/panelHost'
import type {PanelActions} from '../src/runtime/panelHost'
import type {ApprovalRequest, TimelineEntry} from '../src/contract/panel'

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
  it('uses the server state for a request conflict without ending the session', () => {
    const configured = {...DEFAULT_CONFIGURATION, nonterminal_error_states: {turn_in_flight: 'ready' as const}}
    expect(classifyTransportError({code: 'turn_in_flight', status: 409}, configured))
      .toMatchObject({terminal: false, state: 'ready', retryable: false})
  })
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

  it('keeps the session alive on per-turn 404 errors while ending only on session-level 404s', () => {
    expect(classifyTransportError({status: 404, code: 'unknown_message'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: false, code: 'unknown_message', state: 'ready', retryable: false})
    expect(classifyTransportError({status: 404, code: 'unknown_action'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: false, code: 'unknown_action', state: 'ready', retryable: false})
    expect(classifyTransportError({status: 404, code: 'unknown_approval'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: false, code: 'unknown_approval', state: 'ready', approvalClosed: true})
    expect(classifyTransportError({status: 404, code: 'unknown_session'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: true, code: 'unknown_session', state: 'ended', retryable: false})
    expect(classifyTransportError({status: 404, code: 'session_closed'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: true, code: 'session_closed', state: 'ended', retryable: false})
    expect(classifyTransportError({status: 404, code: 'not_found'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: true, code: 'not_found', state: 'ended', retryable: false})
  })

  it('falls back to retryable reconnecting for an ordinary network fault', () => {
    expect(classifyTransportError(null, DEFAULT_CONFIGURATION)).toMatchObject(
      {terminal: false, retryable: true, state: 'reconnecting'})
  })

  it('adopts server read_only_mode from the hello configuration', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    expect(host.configuration.read_only_mode).toBe(false)
    host.adoptServerConfiguration({...DEFAULT_CONFIGURATION, read_only_mode: true})
    expect(host.configuration.read_only_mode).toBe(true)
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

describe('retryable only offers a resend when repeating can help', () => {
  it('hides retry for refused credentials and decided refusals without a status', () => {
    expect(classifyTransportError({code: 'unauthorized'}, DEFAULT_CONFIGURATION).retryable).toBe(false)
    expect(classifyTransportError({code: 'unreviewed'}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: false, retryable: false})
    expect(classifyTransportError({status: 400}, DEFAULT_CONFIGURATION).retryable).toBe(false)
  })

  it('offers retry for rate limits, exhausted servers and transient statuses', () => {
    expect(classifyTransportError({status: 429}, DEFAULT_CONFIGURATION))
      .toMatchObject({terminal: false, retryable: true})
    expect(classifyTransportError({status: 503}, DEFAULT_CONFIGURATION).retryable).toBe(true)
    expect(classifyTransportError({status: 502}, DEFAULT_CONFIGURATION).retryable).toBe(true)
    expect(classifyTransportError({code: 'rate_limited'}, DEFAULT_CONFIGURATION).retryable).toBe(true)
  })

  it('keeps retry for a transport fault that never reached a server answer', () => {
    expect(classifyTransportError({code: 'submit_failed'}, DEFAULT_CONFIGURATION).retryable).toBe(true)
    expect(classifyTransportError({}, DEFAULT_CONFIGURATION).retryable).toBe(true)
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

describe('PanelHost accepts only same-origin structured answer targets', () => {
  it('refuses another origin and query-bearing routes before asking the runtime to open', () => {
    const openAnswerTarget = vi.fn(() => true)
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION,
      actions: {...actions(), openAnswerTarget}})
    const target = {kind: 'record' as const, address: '/contacts/grace', label: 'Grace',
      identity: 'contacts:grace', read_ref: 'read#1'}
    expect(host.openAnswerTarget(target)).toBe(true)
    expect(openAnswerTarget).toHaveBeenCalledWith(new URL(target.address, location.href).href)
    for (const address of ['//elsewhere.test/path', 'https://elsewhere.test/path',
      '/contacts/grace?secret=1', '/contacts/grace#edit']) {
      expect(host.openAnswerTarget({...target, address})).toBe(false)
    }
    expect(openAnswerTarget).toHaveBeenCalledOnce()
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

  it('retains loaded history beyond the former render budget', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const count = 2005
    const entries = Array.from({length: count},
      (_, i) => entry({id: `e${i}`, sequence: i + 1}))
    host.applyTimeline({entries})
    expect(host.snapshot().entries).toHaveLength(count)
    expect(host.snapshot().entries[0]?.id).toBe('e0')
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

  it('merges replay by stable ID, including a partially loaded turn', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const id = (n: number) => `h0123456789abcdef:${String(n).padStart(8, '0')}`
    host.applyTimeline({incarnation: 'a', entries: [
      entry({id: id(3), position: {stream: 'history', sequence: 3}, sequence: 3, turn_id: 't1', kind: 'result', text: 'first answer'}),
    ]})
    const replay = [
      entry({id: id(1), position: {stream: 'history', sequence: 1}, sequence: 0, replayed: true, turn_id: 't0', kind: 'goal', text: 'before this page'}),
      entry({id: id(2), position: {stream: 'history', sequence: 2}, sequence: 0, replayed: true, turn_id: 't1', kind: 'goal', text: 'first question'}),
      entry({id: id(3), position: {stream: 'history', sequence: 3}, sequence: 0, replayed: true, turn_id: 't1', kind: 'result', text: 'first answer'}),
    ]
    host.applyTimeline({incarnation: 'b', entries: replay})
    host.applyTimeline({incarnation: 'b', entries: replay}, {prepend: true})
    expect(host.snapshot().entries.map(item => item.text)).toEqual(
      ['before this page', 'first question', 'first answer'])
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


describe('history paging state is independent of the live stream', () => {
  it('keeps a history request in flight when a live page arrives', () => {
    const act = actions()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: act})
    host.applyTimeline({entries: [entry({id: 'e100', sequence: 100})], oldest: 'opaque', has_more: true})
    host.loadOlder()
    host.applyTimeline({entries: [entry({id: 'e101', sequence: 101})], has_more: false})
    host.loadOlder()
    expect(act.loadOlder).toHaveBeenCalledExactlyOnceWith('opaque')
    expect(host.snapshot().loadingOlder).toBe(true)
    host.applyTimeline({entries: [], oldest: 'older-opaque', has_more: true}, {prepend: true})
    host.loadOlder()
    expect(act.loadOlder).toHaveBeenLastCalledWith('older-opaque')
  })

  it('rejects late history responses after clearing the conversation', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    const version = host.timelineVersion
    host.clearTimeline()
    expect(host.applyTimeline({entries: [entry()], has_more: true}, {prepend: true, version})).toBe(false)
    expect(host.snapshot().entries).toEqual([])
    expect(host.snapshot().hasOlder).toBe(false)
  })

  it('rejects history from a channel replaced during the request', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({incarnation: 'a', entries: [entry()]})
    const version = host.timelineVersion
    host.applyTimeline({incarnation: 'b', entries: []})
    expect(host.applyTimeline({incarnation: 'a', entries: [entry()]}, {prepend: true, version})).toBe(false)
  })
})

describe('PanelHost.reconcile updates indeterminate outcome entries in place', () => {
  it('delegates to bound reconcile verb and updates the matching timeline entry', async () => {
    const reconcile = vi.fn().mockResolvedValue({
      outcome: 'completed' as const, settled: true, message: '已确认生效',
    })
    const host = new PanelHost({
      configuration: DEFAULT_CONFIGURATION,
      actions: {...actions(), reconcile},
    })
    host.applyTimeline({
      entries: [
        entry({id: 'e1', sequence: 1, turn_id: 't1', kind: 'goal', text: '创建客户'}),
        entry({
          id: 'e2', sequence: 2, turn_id: 't1', kind: 'result',
          panel_state: 'indeterminate', proposal_digest: 'a'.repeat(64),
          text: '状态不确定',
        }),
      ],
    })
    const listener = vi.fn()
    host.subscribe(listener)

    const result = await host.reconcile('t1', 'a'.repeat(64))

    expect(reconcile).toHaveBeenCalledWith('t1', 'a'.repeat(64))
    expect(result).toEqual({outcome: 'completed', settled: true, message: '已确认生效'})
    expect(host.snapshot().entries[1]?.reconciliation).toEqual(result)
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('merges updated reconciliation status when applyTimeline replays an existing entry id', () => {
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.applyTimeline({
      entries: [
        entry({id: 'e2', sequence: 2, turn_id: 't1', kind: 'result', panel_state: 'indeterminate'}),
      ],
    })
    host.applyTimeline({
      entries: [
        entry({
          id: 'e2', sequence: 2, turn_id: 't1', kind: 'result', panel_state: 'indeterminate',
          reconciliation: {outcome: 'failed', settled: true, message: '未生效'},
        }),
      ],
    })
    expect(host.snapshot().entries).toHaveLength(1)
    expect(host.snapshot().entries[0]?.reconciliation).toEqual({
      outcome: 'failed', settled: true, message: '未生效',
    })
  })
})

describe('approval click refusal reporting leaves pending card untouched', () => {
  it('PanelHost.reportApprovalRefused forwards only to bound runtime actions', () => {
    const bound = actions()
    const reportApprovalRefused = vi.fn()
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    host.reportApprovalRefused('ap-1', 'insufficient_coverage')
    host.bind({...bound, reportApprovalRefused})
    host.reportApprovalRefused('ap-1', 'insufficient_coverage')
    expect(reportApprovalRefused).toHaveBeenCalledExactlyOnceWith('ap-1', 'insufficient_coverage')
  })

  it('postApprovalRefusal posts /approval_refusals without consuming approval or changing panel state', async () => {
    const request: ApprovalRequest = {
      schema_version: 1,
      approval_id: 'ap-1',
      effect: '更新联系人',
      targets: ['张三'],
      expires_at: Date.now() + 60_000,
      principal_digest: '',
      turn_id: 't-1',
      operation_id: 'op-1',
      kind: 'write',
    }
    const panelHost = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: actions()})
    panelHost.setPendingApproval(request)
    const post = vi.fn().mockResolvedValue({accepted: true, count: 1, last_code: 'insufficient_coverage'})
    const save = vi.fn()
    const panelState = vi.fn()
    const ctx: ApprovalContext = {
      sessionId: 'sess-1',
      stored: {approvals: {}} as ApprovalContext['stored'],
      panelHost,
      panelConfiguration: DEFAULT_CONFIGURATION,
      save,
      panelState,
      post,
      postApproval: vi.fn(),
    }
    await postApprovalRefusal(ctx, request, 'insufficient_coverage')
    expect(post).toHaveBeenCalledWith('/approval_refusals', {
      schema_version: 1,
      session_id: 'sess-1',
      approval_id: 'ap-1',
      code: 'insufficient_coverage',
      turn_id: 't-1',
    })
    expect(ctx.stored.approvals).toEqual({})
    expect(panelHost.snapshot().pendingApproval).toEqual(request)
    expect(save).not.toHaveBeenCalled()
    expect(panelState).not.toHaveBeenCalled()
  })
})

describe('PanelHost.reload retries connected sessions without full page reload', () => {
  it('routes failed optimistic message notices to retrySubmit and clears notice', () => {
    const page = actions()
    const bound = {...actions(), retryNotice: vi.fn()}
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: page})
    host.bind(bound)
    host.setOptimistic({turnId: 't1', text: '查询客户', failed: true})
    host.raise('submit_failed', {retryable: true, turnId: 't1'})

    host.reload()

    expect(host.snapshot().notice).toBeNull()
    expect(host.snapshot().optimistic).toEqual({turnId: 't1', text: '查询客户', failed: false})
    expect(bound.submit).toHaveBeenCalledExactlyOnceWith('查询客户')
    expect(bound.retryNotice).not.toHaveBeenCalled()
  })

  it('routes connected session notices without failed optimistic message to bound.retryNotice', () => {
    const page = actions()
    const bound = {...actions(), retryNotice: vi.fn()}
    const host = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: page})
    host.bind(bound)
    host.raise('timeline_failed', {retryable: true})

    host.reload()

    expect(host.snapshot().notice).toBeNull()
    expect(bound.retryNotice).toHaveBeenCalledOnce()
    expect(bound.submit).not.toHaveBeenCalled()
  })
})
