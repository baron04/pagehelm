import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'

const runtimes: EmbeddedWebRuntime[] = []

function runtime(): EmbeddedWebRuntime {
  const made = new EmbeddedWebRuntime({
    endpoint: 'https://agent.test', sessionId: crypto.randomUUID(),
    clientToken: 'token', sessionGeneration: 'generation', panel: false,
  })
  made.panelConfiguration = DEFAULT_CONFIGURATION
  runtimes.push(made)
  return made
}

afterEach(() => {
  for (const made of runtimes.splice(0)) made.stop()
})

describe('a session that lapsed ends without a word', () => {
  it('says nothing when its sign-in runs out on the page', () => {
    const made = runtime()
    const raise = vi.fn()
    made.panelHost = {raise, setConnection: vi.fn()} as never
    made._expire('principal_expired')
    expect(made.stopped).toBe(true)
    expect(made.endState).toBe('lapsed')
    expect(raise).not.toHaveBeenCalled()
  })

  it('says nothing when the server ended it for a lapse', async () => {
    const made = runtime()
    const raise = vi.fn()
    const setConnection = vi.fn()
    made.panelHost = {raise, setConnection} as never
    await made._handleEvent({kind: 'terminal', payload: {reason: 'host_stopped'}})
    expect(made.endState).toBe('lapsed')
    expect(raise).not.toHaveBeenCalled()
    expect(setConnection).not.toHaveBeenCalled()
  })

  it('still says why when the session ended for a reason the person must know', () => {
    const made = runtime()
    const raise = vi.fn()
    made.panelHost = {raise, setConnection: vi.fn()} as never
    made._raise({code: 'principal_mismatch', status: 403})
    expect(made.endState).not.toBe('lapsed')
    expect(raise).toHaveBeenCalledWith('principal_mismatch', {retryable: false})
  })
})
