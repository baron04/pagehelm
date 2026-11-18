import {afterEach, describe, expect, it, vi} from 'vitest'
import {DEFAULT_CONFIGURATION} from '../src/contract/panel'
import {PanelHost} from '../src/runtime/panelHost'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import type {RuntimeConfig} from '../src/runtime/types'

const runtimes: EmbeddedWebRuntime[] = []

function makeRuntime({withPanel = true} = {}) {
  const sessionId = crypto.randomUUID()
  const config: RuntimeConfig = {
    endpoint: 'https://agent.test',
    sessionId,
    clientToken: 'token-1',
    sessionGeneration: 'gen-1',
    panel: false,
  }
  const runtime = new EmbeddedWebRuntime(config)
  runtime.panelConfiguration = DEFAULT_CONFIGURATION
  runtime.leaseId = 'lease-1'
  if (withPanel) {
    runtime.panelHost = new PanelHost({
      configuration: DEFAULT_CONFIGURATION,
      actions: {
        submit() {}, cancel() {}, decide() {},
        newConversation() {}, loadOlder() {},
      },
    })
    runtime._attachPanel()
  }
  runtimes.push(runtime)
  return runtime
}

afterEach(() => {
  for (const runtime of runtimes.splice(0)) runtime.stop()
  sessionStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('BUG-01: optimistic echo appears synchronously before slow renewal', () => {
  it('shows optimistic echo and observing state before beforeSubmit resolves, then submits with the same messageId', async () => {
    const runtime = makeRuntime()
    let resolveRenewal!: () => void
    runtime.beforeSubmit = () => new Promise<void>(resolve => { resolveRenewal = resolve })
    const observe = vi.spyOn(runtime, 'observe').mockResolvedValue({
      schema_version: 1, session_id: runtime.sessionId, observation_id: 'c:1', page_epoch: 1,
      observation: {url: 'https://app.test/orders', title: 'Orders', elements: []},
      events: [], caused_by_action_id: '',
    } as never)
    const post = vi.spyOn(runtime, '_post').mockResolvedValue({accepted: true})

    const sending = runtime.submitTask('  查询今天的新增客户  ')

    // Synchronously visible before beforeSubmit() settles.
    const snap = runtime.panelHost!.snapshot()
    expect(snap.optimistic).toMatchObject({
      text: '查询今天的新增客户',
      failed: false,
    })
    expect(snap.optimistic!.turnId).toMatch(/^[0-9a-f-]{36}$/i)
    expect(snap.connection).toMatchObject({busy: true, stopping: false, state: 'observing'})
    expect(observe).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()

    const mintedId = snap.optimistic!.turnId
    resolveRenewal()
    await sending

    expect(post).toHaveBeenCalledWith('/messages', {
      message_id: mintedId,
      text: '查询今天的新增客户',
      page_url: 'https://app.test/orders',
    }, expect.objectContaining({signal: expect.any(AbortSignal)}))
    expect(runtime.panelHost!.snapshot().connection.state).toBe('queued')
  })

  it('marks optimistic failed when observation or /messages fails before acceptance without losing the message', async () => {
    const runtime = makeRuntime()
    vi.spyOn(runtime, 'observe').mockRejectedValue(new Error('dom unavailable'))

    await expect(runtime.submitTask('失败回填测试')).rejects.toThrow('dom unavailable')

    const snap = runtime.panelHost!.snapshot()
    expect(snap.optimistic).toMatchObject({text: '失败回填测试', failed: true})
    expect(snap.optimistic!.turnId).not.toBe('')
    expect(snap.connection.busy).toBe(false)
    expect(runtime.stored.taskInFlight).toBe(false)
    expect(runtime.stopped).toBe(false)
  })
})

describe('BUG-03: cancel during preparation/observation aborts locally and keeps session alive', () => {
  it('aborts slow beforeSubmit locally without calling observe or /cancellations', async () => {
    const runtime = makeRuntime()
    runtime.beforeSubmit = () => new Promise<void>(() => {})
    const observe = vi.spyOn(runtime, 'observe')
    const post = vi.spyOn(runtime, '_post')

    const sending = runtime.submitTask('在续期时取消')
    expect(runtime.panelHost!.snapshot().optimistic?.failed).toBe(false)

    await runtime.cancelTask()
    await expect(sending).resolves.toEqual({cancelled: true})

    expect(observe).not.toHaveBeenCalled()
    expect(post).not.toHaveBeenCalled()
    expect(runtime.stopped).toBe(false)
    expect(runtime.stored.taskInFlight).toBe(false)
    expect(runtime.panelHost!.snapshot()).toMatchObject({
      optimistic: null,
      notice: null,
      connection: {busy: false, stopping: false, state: 'ready'},
    })
  })

  it('aborts pending observe() immediately without sending /observations, /messages, or /cancellations', async () => {
    const runtime = makeRuntime()
    vi.spyOn(runtime, 'observe').mockImplementation(() => new Promise(() => {}))
    const post = vi.spyOn(runtime, '_post')

    const sending = runtime.submitTask('在观察时取消')
    await Promise.resolve()
    await runtime.cancelTask()
    await expect(sending).resolves.toEqual({cancelled: true})

    expect(post).not.toHaveBeenCalled()
    expect(runtime.stopped).toBe(false)
    expect(runtime.stored.taskInFlight).toBe(false)
    expect(runtime.panelHost!.snapshot()).toMatchObject({
      optimistic: null,
      notice: null,
      connection: {busy: false, stopping: false, state: 'ready'},
    })
  })

  it('aborts in-flight /observations fetch via AbortSignal and never sends /messages', async () => {
    const runtime = makeRuntime()
    vi.spyOn(runtime, 'observe').mockResolvedValue({
      schema_version: 1, session_id: runtime.sessionId, observation_id: 'c:1', page_epoch: 1,
      observation: {url: 'https://app.test/', title: 'App', elements: []},
      events: [], caused_by_action_id: '',
    } as never)
    let observedSignal: AbortSignal | null | undefined
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/observations')) {
        observedSignal = init?.signal
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            const err = new Error('Aborted')
            err.name = 'AbortError'
            reject(err)
          })
        })
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetcher)

    const sending = runtime.submitTask('上传观察时取消')
    await Promise.resolve()
    await Promise.resolve()
    expect(observedSignal).toBeDefined()
    expect(observedSignal?.aborted).toBe(false)

    await runtime.cancelTask()
    await expect(sending).resolves.toEqual({cancelled: true})

    expect(observedSignal?.aborted).toBe(true)
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(runtime.stopped).toBe(false)
    expect(runtime.panelHost!.snapshot()).toMatchObject({
      optimistic: null,
      connection: {busy: false, stopping: false, state: 'ready'},
    })
  })

  it('handles /cancellations 200 pre_cancelled and 404 unknown_message without ending the session', async () => {
    const runtime = makeRuntime()
    vi.spyOn(runtime, 'observe').mockResolvedValue({
      schema_version: 1, session_id: runtime.sessionId, observation_id: 'c:1', page_epoch: 1,
      observation: {url: 'https://app.test/', title: 'App', elements: []},
      events: [], caused_by_action_id: '',
    } as never)
    const fetcher = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/observations')) {
        return Promise.resolve(Response.json({accepted: true}))
      }
      if (url.endsWith('/messages')) {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            const err = new Error('Aborted')
            err.name = 'AbortError'
            reject(err)
          })
        })
      }
      if (url.endsWith('/cancellations')) {
        return Promise.resolve(Response.json({accepted: true, duplicate: false, pre_cancelled: true}))
      }
      throw new Error(`unexpected fetch ${url}`)
    })
    vi.stubGlobal('fetch', fetcher)

    const sending = runtime.submitTask('消息发送瞬间取消')
    await new Promise(resolve => setTimeout(resolve, 10))
    await runtime.cancelTask()
    await expect(sending).resolves.toEqual({cancelled: true})

    expect(runtime.stopped).toBe(false)
    expect(runtime.panelHost!.snapshot()).toMatchObject({
      optimistic: null,
      connection: {busy: false, stopping: false, state: 'ready'},
    })

    // A per-turn 404 unknown_message on /cancellations also keeps the session alive.
    runtime.stored.taskInFlight = true
    runtime.stored.activeMessageId = 'missing-turn'
    fetcher.mockImplementationOnce(async () => Response.json(
      {error: 'unknown message', code: 'unknown_message'}, {status: 404},
    ))
    await runtime.cancelTask()
    expect(runtime.stopped).toBe(false)
    expect(runtime.panelHost!.snapshot().connection).toMatchObject({
      busy: false, stopping: false, state: 'ready',
    })
  })
})

describe('UX-06: PanelHost.reconcile calls /reconciliations and updates timeline state without retrying writes', () => {
  it('posts ReconciliationRequest and updates the indeterminate timeline entry for completed/failed/unknown', async () => {
    const runtime = makeRuntime()
    const digest = 'b'.repeat(64)
    runtime.panelHost!.applyTimeline({
      entries: [
        {
          id: 'res-1', sequence: 2, turn_id: 'turn-1', at: 1000, kind: 'result',
          tone: 'warning', panel_state: 'indeterminate', text: '结果不确定',
          proposal_digest: digest,
        },
      ],
    })
    const fetcher = vi.fn()
      .mockResolvedValueOnce(Response.json({outcome: 'completed', settled: true, message: '记录已创建'}))
      .mockResolvedValueOnce(Response.json({reconciliation: {outcome: 'unknown', settled: false, message: '请人工核对'}}))
    vi.stubGlobal('fetch', fetcher)

    const first = await runtime.panelHost!.reconcile!('turn-1', digest)
    expect(first).toEqual({outcome: 'completed', settled: true, message: '记录已创建'})
    expect(fetcher).toHaveBeenCalledTimes(1)
    const [url, init] = fetcher.mock.calls[0]!
    expect(String(url)).toBe(`https://agent.test/v1/sessions/${runtime.sessionId}/reconciliations`)
    expect(JSON.parse(String(init?.body))).toEqual({
      schema_version: 1,
      session_id: runtime.sessionId,
      turn_id: 'turn-1',
      proposal_digest: digest,
    })
    expect(runtime.panelHost!.snapshot().entries[0]?.reconciliation).toEqual(first)

    const second = await runtime.panelHost!.reconcile!('turn-1')
    expect(second).toEqual({outcome: 'unknown', settled: false, message: '请人工核对'})
    expect(JSON.parse(String(fetcher.mock.calls[1]![1]?.body))).toEqual({
      schema_version: 1,
      session_id: runtime.sessionId,
      turn_id: 'turn-1',
    })
    expect(runtime.panelHost!.snapshot().entries[0]?.reconciliation).toEqual(second)
  })

  it('never retries /reconciliations on transport failure and preserves session on non-terminal error', async () => {
    const runtime = makeRuntime()
    const fetcher = vi.fn().mockResolvedValue(
      Response.json({error: 'busy', code: 'gateway_error'}, {status: 503}),
    )
    vi.stubGlobal('fetch', fetcher)

    await expect(runtime.panelHost!.reconcile!('turn-1')).rejects.toMatchObject({
      code: 'gateway_error', status: 503,
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(runtime.stopped).toBe(false)
    expect(runtime.panelHost!.snapshot().notice).toMatchObject({code: 'gateway_error', retryable: false})
  })
})
