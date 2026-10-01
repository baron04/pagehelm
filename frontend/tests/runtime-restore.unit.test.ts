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
