import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import type {PanelConfiguration} from '../src/contract/panel'
import type {PanelActions} from '../src/runtime/panelHost'
import {autoBoot} from '../src/runtime/boot'
import {engagementRecord} from '../src/runtime/engagement'
import {createShell} from '../src/runtime/shell'
import {startEmbeddedRuntime, type EmbeddedWebRuntime} from '../src/runtime/runtime'
import type * as RuntimeModule from '../src/runtime/runtime'

vi.mock('../src/runtime/shell', async () => {
  const {PanelHost} = await import('../src/runtime/panelHost')
  return {createShell: vi.fn((actions: PanelActions, configuration: PanelConfiguration) => ({
    host: new PanelHost({actions, configuration}), container: document.createElement('div'), destroy: vi.fn(),
  }))}
})

vi.mock('../src/runtime/runtime', async importOriginal => {
  const actual = await importOriginal<typeof RuntimeModule>()
  return {
    ...actual,
    startEmbeddedRuntime: vi.fn(actual.startEmbeddedRuntime),
  }
})

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  delete window.__WOA_ASSISTANT__
  document.body.innerHTML = '<script data-woa-bootstrap="/bootstrap"></script>'
  vi.clearAllMocks()
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})
afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
  delete window.__WOA_ASSISTANT__
})

describe('the first panel state depends on a locally saved session', () => {
  it('shows the default immediately and opening creates no session without an ID', async () => {
    const supervisor = autoBoot()
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    // What the tab is settles first; the panel states itself once it has.
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(host.snapshot().connection.state).toBe('ready')
    host.panelVisibility(true)
    expect(supervisor?.state.kind).toBe('idle')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('keeps the greeting hidden while opening restores a saved session', async () => {
    engagementRecord().remember('actor-session')
    const supervisor = autoBoot()
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(host.snapshot().connection.state).toBe('connecting')
    expect(fetch).not.toHaveBeenCalled()
    host.panelVisibility(true)
    expect(supervisor?.state.kind).toBe('starting')
    expect(host.snapshot().connection.state).toBe('connecting')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('cancels cold-start queued waiting text on shell.cancel and clears waiting on unavailable start', async () => {
    const startGate: {resolve?: (rt: EmbeddedWebRuntime) => void} = {}
    const submittedAfterBoot: string[] = []
    vi.mocked(startEmbeddedRuntime).mockImplementation(async (_cfg, shell) => {
      return await new Promise<EmbeddedWebRuntime | null>(resolve => {
        startGate.resolve = rt => {
          shell?.host.bind({
            submit: text => { submittedAfterBoot.push(text) },
            cancel: vi.fn(),
            decide: vi.fn(),
            newConversation: vi.fn(),
            loadOlder: vi.fn(),
          })
          resolve(rt)
        }
      })
    })
    const fakeRuntime = (): EmbeddedWebRuntime => ({
      stopped: false, endState: '', canRenewSession: () => true,
      renewSession: vi.fn(), stop: vi.fn(), onStopped: () => () => undefined,
    } as unknown as EmbeddedWebRuntime)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      config: {
        endpoint: 'https://agent.test', sessionId: 's-boot', clientToken: 'tok',
        sessionGeneration: 'g1', pageControllerModule: 'http://localhost/pc.js',
      },
    }), {status: 200, headers: {'Content-Type': 'application/json'}})))

    const supervisor = autoBoot()!
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    await new Promise(resolve => setTimeout(resolve, 0))

    // Submit while cold-starting, then immediately click Stop/Cancel before runtime binds
    host.submit('不要发送的冷启动消息')
    expect(host.snapshot().optimistic?.text).toBe('不要发送的冷启动消息')
    host.cancel()
    expect(host.snapshot().optimistic).toBeNull()
    expect(host.snapshot().connection.state).toBe('ready')

    // When start completes, canceled waiting text is NOT sent
    await new Promise(resolve => setTimeout(resolve, 0))
    startGate.resolve?.(fakeRuntime())
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(submittedAfterBoot).toEqual([])
    expect(supervisor.state.kind).toBe('running')

    // Now test unavailable cold-start clears waiting so a later start() does not replay it
    delete window.__WOA_ASSISTANT__
    vi.clearAllMocks()
    let failBootstrap = true
    vi.stubGlobal('fetch', vi.fn(async () => failBootstrap
      ? new Response('forbidden', {status: 403})
      : new Response(JSON.stringify({
        config: {
          endpoint: 'https://agent.test', sessionId: 's-boot-2', clientToken: 'tok',
          sessionGeneration: 'g2', pageControllerModule: 'http://localhost/pc.js',
        },
      }), {status: 200, headers: {'Content-Type': 'application/json'}})))

    const sup2 = autoBoot()!
    const host2 = vi.mocked(createShell).mock.results[0]!.value.host
    await new Promise(resolve => setTimeout(resolve, 0))
    host2.submit('失败的冷启动消息')
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(sup2.state.kind).toBe('unavailable')
    expect(host2.snapshot().optimistic).toEqual({turnId: '', text: '失败的冷启动消息', failed: true})

    failBootstrap = false
    sup2.start()
    await new Promise(resolve => setTimeout(resolve, 10))
    startGate.resolve?.(fakeRuntime())
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(submittedAfterBoot).toEqual([])
  })

  it('resends failed cold-start message when the person clicks inline retry', async () => {
    const startGate: {resolve?: (rt: EmbeddedWebRuntime) => void} = {}
    const submittedAfterBoot: string[] = []
    vi.mocked(startEmbeddedRuntime).mockImplementation(async (_cfg, shell) => {
      return await new Promise<EmbeddedWebRuntime | null>(resolve => {
        startGate.resolve = rt => {
          shell?.host.bind({
            submit: text => { submittedAfterBoot.push(text) },
            cancel: vi.fn(),
            decide: vi.fn(),
            newConversation: vi.fn(),
            loadOlder: vi.fn(),
          })
          resolve(rt)
        }
      })
    })
    const fakeRuntime = (): EmbeddedWebRuntime => ({
      stopped: false, endState: '', canRenewSession: () => true,
      renewSession: vi.fn(), stop: vi.fn(), onStopped: () => () => undefined,
    } as unknown as EmbeddedWebRuntime)
    let failBootstrap = true
    vi.stubGlobal('fetch', vi.fn(async () => failBootstrap
      ? new Response('unavailable', {status: 503})
      : new Response(JSON.stringify({
        config: {
          endpoint: 'https://agent.test', sessionId: 's-boot-3', clientToken: 'tok',
          sessionGeneration: 'g3', pageControllerModule: 'http://localhost/pc.js',
        },
      }), {status: 200, headers: {'Content-Type': 'application/json'}})))

    const supervisor = autoBoot()!
    const host = vi.mocked(createShell).mock.results[0]!.value.host
    await new Promise(resolve => setTimeout(resolve, 0))
    host.submit('列出订单')
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(supervisor.state.kind).toBe('unavailable')
    expect(host.snapshot().optimistic).toEqual({turnId: '', text: '列出订单', failed: true})

    failBootstrap = false
    host.reload()
    await new Promise(resolve => setTimeout(resolve, 10))
    startGate.resolve?.(fakeRuntime())
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(submittedAfterBoot).toEqual(['列出订单'])
  })

  it('destroys the shell, clears tab session state on sign-out, and lets autoBoot mount cleanly again', async () => {
    engagementRecord().remember('actor-a-session')
    sessionStorage.setItem('woa.remote.state', JSON.stringify({taskInFlight: true}))
    sessionStorage.setItem('woa.assistant.epoch.v1', '2')

    const first = autoBoot()!
    const firstShell = vi.mocked(createShell).mock.results[0]!.value as {destroy: ReturnType<typeof vi.fn>}
    expect(window.__WOA_ASSISTANT__).toBe(first)

    first.destroy({clearSession: true})
    expect(first.state).toEqual({kind: 'settled', reason: 'destroyed'})
    expect(firstShell.destroy).toHaveBeenCalledOnce()
    expect(window.__WOA_ASSISTANT__).toBeUndefined()
    expect(engagementRecord().read()).toBe('none')
    expect(engagementRecord().sessionId()).toBe('')
    expect(sessionStorage.getItem('woa.remote.state')).toBeNull()
    expect(sessionStorage.getItem('woa.assistant.epoch.v1')).toBeNull()

    // Subsequent lifecycle calls on the destroyed supervisor are no-ops.
    first.start()
    first.connect()
    expect(first.newConversation()).toBe(false)
    expect(first.state).toEqual({kind: 'settled', reason: 'destroyed'})

    // Re-login in the same document can cleanly mount a brand-new supervisor and shell.
    const second = autoBoot()!
    expect(second).not.toBe(first)
    expect(window.__WOA_ASSISTANT__).toBe(second)
    expect(vi.mocked(createShell)).toHaveBeenCalledTimes(2)
    second.destroy()
  })
})
