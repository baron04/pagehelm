import {afterEach, describe, expect, it, vi} from 'vitest'
import {Supervisor, endpointSource, RENEW_RETRY_MS} from '../src/runtime/boot'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {PanelHost} from '../src/runtime/panelHost'
import type {RuntimeConfig} from '../src/runtime/types'

const runtimes: EmbeddedWebRuntime[] = []
afterEach(() => {
  for (const runtime of runtimes.splice(0)) runtime.stop()
  delete (window as unknown as {__WOA_YUDAO_LOADER__?: boolean}).__WOA_YUDAO_LOADER__
  document.head.innerHTML = ''
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve() }
const cache = (key: string, value: string) => localStorage.setItem(key, JSON.stringify({v: JSON.stringify(value)}))

async function harness({refused = false, scheduled = false} = {}) {
  cache('ACCESS_TOKEN', 'initial-token')
  cache('REFRESH_TOKEN', 'refresh-token')
  let tokenValid = true
  let binding = 0
  const config: RuntimeConfig = {
    endpoint: location.origin + '/agent', sessionId: crypto.randomUUID(), clientToken: 'capability',
    sessionGeneration: 'generation', panel: false, applicationId: 'test-app', principalBinding: 'initial',
    principalExpiresAt: Date.now() + 600_000, sessionExpiresAt: Date.now() + 600_000,
  }
  let hold: Promise<void> | null = null
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), location.href).pathname
    if (path === '/admin-api/system/auth/refresh-token') {
      if (hold) await hold
      return Response.json(refused ? {code: 401} : {code: 0, data: {
        accessToken: 'renewed-token', refreshToken: 'renewed-refresh',
      }})
    }
    if (path !== '/woa-agent-boot/bootstrap') throw new Error('unexpected fetch ' + path)
    const token = new Headers(init?.headers).get('Authorization')
    if (!tokenValid && token !== 'Bearer renewed-token') return Response.json({}, {status: 401})
    binding++
    return Response.json({config: {...config, principalBinding: 'binding-' + binding},
      renew_at: scheduled ? Date.now() + RENEW_RETRY_MS : undefined})
  })
  vi.stubGlobal('fetch', fetcher)
  vi.resetModules()
  // @ts-expect-error Application loader ships as framework-free JS.
  await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
  const runtime = new EmbeddedWebRuntime(config)
  runtime.panelConfiguration = DEFAULT_CONFIGURATION
  runtimes.push(runtime)
  const observe = vi.spyOn(runtime, 'observe').mockResolvedValue({observation: {url: location.href}} as never)
  const post = vi.spyOn(runtime, '_post').mockResolvedValue({accepted: true})
  vi.spyOn(runtime, '_raise').mockReturnValue({terminal: false, code: '', state: 'error', retryable: false})
  const supervisor = new Supervisor({
    source: endpointSource({bootstrap: '/woa-agent-boot/bootstrap', instanceId: 'tab'}),
    start: async () => runtime,
    status: {connecting() {}, unavailable() {}, started() {}, rested() {}},
    eager: () => false,
  })
  supervisor.start()
  await flush()
  expect(supervisor.state.kind).toBe('running')
  return {runtime, supervisor, fetcher, observe, post,
    expire() { tokenValid = false }, holdRefresh(value: Promise<void>) { hold = value }}
}

describe('application identity before submitting a new task', () => {
  it('renews an expired application token even though the agent principal is still live, then sends once', async () => {
    const h = await harness()
    h.expire()
    await h.runtime.submitTask('my original question')
    expect(h.fetcher.mock.calls.map(([input]) => new URL(String(input), location.href).pathname))
      .toEqual(['/woa-agent-boot/bootstrap', '/woa-agent-boot/bootstrap',
        '/admin-api/system/auth/refresh-token', '/woa-agent-boot/bootstrap'])
    expect(h.runtime.principalBinding).toBe('binding-2')
    expect(h.post.mock.calls.map(([path]) => path)).toEqual(['/observations', '/messages'])
    expect(h.post.mock.calls[1]![1]).toMatchObject({text: 'my original question'})
    expect(h.fetcher.mock.invocationCallOrder.at(-1)!).toBeLessThan(h.observe.mock.invocationCallOrder[0]!)
  })

  it('sends no observation or task when the refresh token is refused; a new user submission can recover', async () => {
    const h = await harness({refused: true})
    vi.mocked(h.runtime._raise).mockRestore()
    h.runtime.panelHost = new PanelHost({configuration: DEFAULT_CONFIGURATION, actions: {
      submit() {}, cancel() {}, decide() {}, newConversation() {}, loadOlder() {},
    }})
    h.expire()
    await expect(h.runtime.submitTask('preserve this question')).rejects.toMatchObject({
      code: 'business_authentication', status: 401,
    })
    expect(h.post).not.toHaveBeenCalled()
    expect(h.observe).not.toHaveBeenCalled()
    expect(h.runtime.stored.taskInFlight).toBe(false)
    expect(h.runtime.stopped).toBe(false)
    expect(h.runtime.panelHost.snapshot().notice).toMatchObject({
      code: 'business_authentication', retryable: false,
    })
    expect(h.runtime.panelHost.snapshot().optimistic).toMatchObject({text: 'preserve this question', failed: true})
    cache('ACCESS_TOKEN', 'renewed-token')
    await flush()
    expect(h.post).not.toHaveBeenCalled()
    await h.runtime.submitTask('preserve this question')
    expect(h.post.mock.calls.filter(([path]) => path === '/messages')).toHaveLength(1)
  })

  it('shares scheduled renewal with submission, blocks double send and dispatches only after renewal finishes', async () => {
    vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'Date']})
    const h = await harness({scheduled: true})
    h.expire()
    let finish!: () => void
    h.holdRefresh(new Promise(resolve => { finish = resolve }))
    await vi.advanceTimersByTimeAsync(RENEW_RETRY_MS)
    const sending = h.runtime.submitTask('one question')
    await flush()
    await expect(h.runtime.submitTask('duplicate')).rejects.toThrow('being prepared')
    expect(h.post).not.toHaveBeenCalled()
    finish()
    await sending
    expect(h.fetcher.mock.calls.filter(([input]) => String(input).startsWith('/admin-api/'))).toHaveLength(1)
    expect(h.post.mock.calls.filter(([path]) => path === '/messages')).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(RENEW_RETRY_MS)
    // An active task is never reauthenticated or replayed by the timer.
    expect(h.fetcher).toHaveBeenCalledTimes(4)
    expect(h.post).toHaveBeenCalledTimes(2)
  })

  it('never resends a task after an uncertain message transport failure', async () => {
    const h = await harness()
    h.post.mockImplementation(async path => {
      if (path === '/messages') throw new Error('connection lost after dispatch')
      return {accepted: true}
    })
    await expect(h.runtime.submitTask('do something')).rejects.toThrow('connection lost')
    await flush()
    expect(h.post.mock.calls.filter(([path]) => path === '/messages')).toHaveLength(1)
  })
})
