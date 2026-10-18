import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {PanelHost} from '../src/runtime/panelHost'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

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
