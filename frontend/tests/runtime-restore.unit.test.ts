import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {PanelHost} from '../src/runtime/panelHost'
import {loadPageController} from '../src/runtime/page'
import type * as PageModule from '../src/runtime/page'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import {IdleHandshakeClosed, SessionClient, TransportError} from '../src/runtime/transport'

vi.mock('../src/runtime/page', async importOriginal => ({
  ...await importOriginal<typeof PageModule>(),
  loadPageController: vi.fn(),
}))

const runtimes: EmbeddedWebRuntime[] = []
function setup() {
  const runtime = new EmbeddedWebRuntime({endpoint: 'https://agent.test',
    sessionId: crypto.randomUUID(), clientToken: 'token', sessionGeneration: 'generation',
    panel: false})
  runtimes.push(runtime)
  runtime.panelConfiguration = DEFAULT_CONFIGURATION
  const host = new PanelHost({configuration: DEFAULT_CONFIGURATION,
    actions: {submit() {}, cancel() {}, decide() {}, newConversation() {}, loadOlder() {}}})
  runtime.panelHost = host
  vi.spyOn(runtime, '_hello').mockResolvedValue()
  vi.spyOn(runtime, 'observe').mockResolvedValue({} as never)
  vi.spyOn(runtime, '_post').mockResolvedValue({})
  vi.spyOn(runtime, '_recoverAfterNavigation').mockResolvedValue()
  vi.spyOn(runtime, '_eventLoop').mockResolvedValue()
  return {runtime, host}
}
afterEach(() => { for (const runtime of runtimes.splice(0)) runtime.stop() })

describe('restoring a conversation before exposing an empty greeting', () => {
  it('marks only an idle first-hello410 as eligible for a fresh bootstrap', async () => {
    const {runtime} = setup()
    vi.mocked(runtime._hello).mockRejectedValue(new TransportError('closed', 'session_closed', 410))
    await expect(runtime._startConnectedRuntime()).rejects.toBeInstanceOf(IdleHandshakeClosed)
    expect(runtime._post).not.toHaveBeenCalled()
    expect(runtime._recoverAfterNavigation).not.toHaveBeenCalled()
  })

  it('retires settled approval decisions before a later idle handshake expires', async () => {
    const {runtime} = setup()
    Object.assign(runtime.stored, {taskInFlight: true, activeMessageId: 'completed-turn',
      approvals: {consumed: 'approved'}})
    await runtime._handleEvent({kind: 'progress', turn_id: 'other-turn', payload: {state: 'finished'}})
    expect(runtime.stored.approvals).toEqual({consumed: 'approved'})
    await runtime._handleEvent({kind: 'progress', turn_id: 'completed-turn', payload: {state: 'finished'}})
    expect(runtime.stored.approvals).toEqual({})
    expect(runtime.stored.taskInFlight).toBe(false)
    vi.mocked(runtime._hello).mockRejectedValue(new TransportError('closed', 'session_closed', 410))
    await expect(runtime._startConnectedRuntime()).rejects.toBeInstanceOf(IdleHandshakeClosed)
  })

  it.each([
    {taskInFlight: true}, {activeMessageId: 'accepted-message'}, {cancellationPending: true},
    {approvals: {decision: 'approved'}}, {inFlight: {action_id: 'held-action'} as never},
    {cachedResult: {action_id: 'acknowledged-action'} as never},
  ])('does not replace a handshake while recovery state is held: %j', async held => {
    const {runtime} = setup(), error = new TransportError('closed', 'session_closed', 410)
    Object.assign(runtime.stored, held)
    vi.mocked(runtime._hello).mockRejectedValue(error)
    await expect(runtime._startConnectedRuntime()).rejects.toBe(error)
    expect(runtime._post).not.toHaveBeenCalled()
    expect(runtime._recoverAfterNavigation).not.toHaveBeenCalled()
  })

  it('retains history when another page has a turn and new conversation is refused', async () => {
    const {runtime, host} = setup()
    runtime._attachPanel()
    host.applyTimeline({entries: [{id: 'e1', sequence: 1, turn_id: 't1', at: 1,
      kind: 'goal', tone: 'neutral', text: 'previous question'}]})
    vi.mocked(runtime._post).mockRejectedValue({code: 'turn_in_flight', status: 409})
    host.newConversation()
    await vi.waitFor(() => expect(host.snapshot().notice).not.toBeNull())
    expect(host.snapshot().entries[0]?.text).toBe('previous question')
  })

  it('clears history only after the server accepts new conversation', async () => {
    const {runtime, host} = setup()
    runtime._attachPanel()
    host.applyTimeline({entries: [{id: 'e1', sequence: 1, turn_id: 't1', at: 1,
      kind: 'goal', tone: 'neutral', text: 'previous question'}]})
    let accepted!: (value: unknown) => void
    vi.mocked(runtime._post).mockImplementation(() => new Promise(done => {accepted = done}))
    vi.spyOn(runtime, '_timeline').mockResolvedValue({entries: []} as never)
    host.newConversation()
    expect(host.snapshot().entries).toHaveLength(1)
    accepted({})
    await vi.waitFor(() => expect(host.snapshot().entries).toHaveLength(0))
  })
  it('keeps connecting through a delayed timeline and shows history before ready', async () => {
    const {runtime, host} = setup()
    let resolve!: (value: never) => void
    vi.spyOn(runtime, '_timeline').mockImplementation(() => new Promise(done => { resolve = done }))
    const seen: Array<{state: string; count: number}> = []
    host.subscribe(s => seen.push({state: s.connection.state, count: s.entries.length}))
    const starting = runtime._startConnectedRuntime()
    await vi.waitFor(() => expect(resolve).toBeDefined())
    expect(host.snapshot().connection.state).toBe('connecting')
    resolve({entries: [{id: 'e1', sequence: 1, turn_id: 't1', at: 1, kind: 'goal',
      tone: 'neutral', text: 'previous question'}], cursor: 'e1'} as never)
    await starting
    expect(host.snapshot().connection.state).toBe('ready')
    expect(host.snapshot().entries[0]?.text).toBe('previous question')
    expect(seen.some(s => s.state === 'ready' && s.count === 0)).toBe(false)
  })

  it('does not silently present a new conversation when history cannot be fetched', async () => {
    const {runtime, host} = setup()
    vi.spyOn(runtime, '_timeline').mockRejectedValue(new Error('network unavailable'))
    await expect(runtime._startConnectedRuntime()).rejects.toThrow('network unavailable')
    expect(host.snapshot().connection.state).not.toBe('ready')
    expect(host.snapshot().notice).not.toBeNull()
  })
})

describe('bounded live catch-up and independent history requests', () => {
  it('drains live pages in order and shares concurrent refresh requests', async () => {
    const {runtime, host} = setup()
    const timeline = vi.spyOn(runtime, '_timeline')
    timeline.mockResolvedValueOnce({entries: [], cursor: 'e100', has_newer: true} as never)
      .mockResolvedValueOnce({entries: [], cursor: 'e200', has_newer: true} as never)
      .mockResolvedValueOnce({entries: [], cursor: 'e235', has_newer: false} as never)
    await Promise.all([runtime._refreshTimeline(), runtime._refreshTimeline()])
    expect(timeline.mock.calls).toEqual([[{after: ''}], [{after: 'e100'}], [{after: 'e200'}]])
    expect(runtime.panelCursor).toBe('e235')
    expect(host.snapshot().notice).toBeNull()
  })

  it('offers history retry without putting the connection into reconnecting', async () => {
    const {runtime, host} = setup()
    host.setConnection({state: 'ready'})
    host.setLoadingOlder(true)
    const timeline = vi.spyOn(runtime, '_timeline').mockRejectedValueOnce(new Error('offline'))
    await runtime._loadOlder('older')
    expect(host.snapshot()).toMatchObject({historyError: true, loadingOlder: false,
      connection: {state: 'ready'}, notice: null})
    timeline.mockResolvedValueOnce({entries: [], has_more: false} as never)
    await runtime._loadOlder('older')
    expect(host.snapshot().historyError).toBe(false)
  })

  it('ignores a history page that completes after new conversation', async () => {
    const {runtime, host} = setup()
    let resolve!: (value: never) => void
    vi.spyOn(runtime, '_timeline').mockImplementation(() => new Promise(done => {resolve = done}))
    const pending = runtime._loadOlder('older')
    host.clearTimeline()
    resolve({entries: [{id: 'h00000001', sequence: 0, replayed: true,
      turn_id: 'old', text: 'old question', kind: 'goal'}], has_more: true} as never)
    await pending
    expect(host.snapshot().entries).toEqual([])
    expect(host.snapshot().hasOlder).toBe(false)
  })
})

describe('the page names its channel presence by its guard window', () => {
  afterEach(() => { vi.restoreAllMocks() })
  it('starts with the window name as its runtime instance id', async () => {
    const runtime = new EmbeddedWebRuntime({endpoint: 'https://agent.test',
      sessionId: 's1', clientToken: 'token', sessionGeneration: 'g',
      panel: false}, null, {windowToken: async () => 'tab-1.3'})
    runtimes.push(runtime)
    runtime.panelConfiguration = DEFAULT_CONFIGURATION
    const controllerStub = new Proxy({controller: {dispose: vi.fn()}} as Record<PropertyKey, unknown>,
      {get: (target: Record<PropertyKey, unknown>, prop) =>
        prop === 'then' ? undefined : (target[prop] ??= vi.fn())})
    vi.mocked(loadPageController).mockResolvedValue(controllerStub as never)
    vi.spyOn(runtime, '_startConnectedRuntime').mockResolvedValue(runtime)
    await runtime.start()
    expect(runtime.runtimeInstanceId).toBe('tab-1.3')
  })

  it('sends that name as the hello runtime instance id', async () => {
    const runtime = new EmbeddedWebRuntime({endpoint: 'https://agent.test',
      sessionId: 's1', clientToken: 'token', sessionGeneration: 'g',
      panel: false})
    runtimes.push(runtime)
    runtime.panelConfiguration = DEFAULT_CONFIGURATION
    runtime.runtimeInstanceId = 'tab-1.0'
    const post = vi.spyOn(SessionClient.prototype, 'post')
      .mockResolvedValue({lease_id: 'L1', panel: DEFAULT_CONFIGURATION} as never)
    await runtime._hello()
    expect(vi.mocked(post).mock.calls[0]?.[1])
      .toMatchObject({runtime_instance_id: 'tab-1.0'})
  })
})

describe('a lease evicted to make room recovers where a taken-over one cannot', () => {
  afterEach(() => { vi.restoreAllMocks() })
  it('re-hellos for a fresh lease and retries the request once', async () => {
    const runtime = new EmbeddedWebRuntime({endpoint: 'https://agent.test',
      sessionId: 's1', clientToken: 'token', sessionGeneration: 'g',
      panel: false})
    runtimes.push(runtime)
    runtime.panelConfiguration = DEFAULT_CONFIGURATION
    const post = vi.spyOn(SessionClient.prototype, 'post')
    post.mockRejectedValueOnce(new TransportError('evicted', 'lease_evicted', 409))
      .mockResolvedValueOnce({lease_id: 'L2', panel: DEFAULT_CONFIGURATION} as never)
      .mockResolvedValueOnce({done: true} as never)
    await expect(runtime._post('/observations', {x: 1})).resolves.toEqual({done: true})
    expect(post.mock.calls.map(call => call[0]))
      .toEqual(['/observations', '/hello', '/observations'])
    expect(runtime.leaseId).toBe('L2')
  })

  it('never re-hellos from inside a hello', async () => {
    const runtime = new EmbeddedWebRuntime({endpoint: 'https://agent.test',
      sessionId: 's1', clientToken: 'token', sessionGeneration: 'g',
      panel: false})
    runtimes.push(runtime)
    const post = vi.spyOn(SessionClient.prototype, 'post')
      .mockRejectedValue(new TransportError('evicted', 'lease_evicted', 409))
    await expect(runtime._post('/hello', {}, {includeLease: false}))
      .rejects.toMatchObject({code: 'lease_evicted'})
    expect(post).toHaveBeenCalledOnce()
  })
})
