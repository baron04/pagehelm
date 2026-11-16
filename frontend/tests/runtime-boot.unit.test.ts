import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {
  BootFailure, RENEW_RETRY_MS, RETRY_MS, STABLE_MS, Supervisor,
  endpointSource,
  inlineSource, type Bootstrap, type SessionSource, type Supervised,
} from '../src/runtime/boot'
import type {StartFailure, StartStatus} from '../src/runtime/startStatus'
import type {RuntimeConfig} from '../src/runtime/types'
import {IdleHandshakeClosed, TransportError} from '../src/runtime/transport'

function config(sessionId = 's1'): RuntimeConfig {
  return {endpoint: 'https://agent.test', sessionId, clientToken: 't', sessionGeneration: 'g'}
}

class FakeRuntime implements Supervised {
  stopped = false
  endState = ''
  renewable = true
  renewals: RuntimeConfig[] = []
  stopCalls = 0
  private listeners = new Set<() => void>()
  canRenewSession() { return this.renewable }
  renewSession(next: RuntimeConfig) { this.renewals.push(next) }
  stop() { this.stopCalls += 1; this.end() }
  onStopped(listener: () => void) {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  /** Stop the way the real Runtime does: flag first, listeners after the current task. */
  end(endState = '') {
    if (this.stopped) return
    this.endState ||= endState
    this.stopped = true
    for (const listener of this.listeners) queueMicrotask(listener)
  }
}

class FakeStatus implements StartStatus {
  log: string[] = []
  failure: StartFailure | null = null
  retry: (() => void) | null = null
  connecting() { this.log.push('connecting') }
  unavailable(failure: StartFailure, retry: () => void) {
    this.log.push(`unavailable:${failure.status}:${failure.retrying}`)
    this.failure = failure
    this.retry = retry
  }
  started() { this.log.push('started') }
  rested() { this.log.push('rested') }
}

type Outcome = Bootstrap | BootFailure | Promise<Bootstrap>

function source(outcomes: Outcome[], renewable = true): SessionSource & {calls: number} {
  const queue = [...outcomes]
  return {
    renewable,
    calls: 0,
    async obtain() {
      this.calls += 1
      const next = queue.length > 1 ? queue.shift() : queue[0]
      if (next instanceof BootFailure) throw next
      if (next === undefined) throw new Error('no outcome queued')
      return next
    },
  }
}

function harness(src: SessionSource, runtimes: Array<FakeRuntime | null | Error> = [], eager = true) {
  const status = new FakeStatus()
  const started: FakeRuntime[] = []
  const running: Supervised[] = []
  const reset = vi.fn()
  const supervisor = new Supervisor({
    source: src,
    status,
    eager: () => eager,
    start: async () => {
      const next = runtimes.length ? runtimes.shift() : new FakeRuntime()
      if (next instanceof Error) throw next
      if (next) started.push(next)
      return next ?? null
    },
    onRunning: runtime => { running.push(runtime) },
    resetConversation: reset,
  })
  return {supervisor, status, started, running, reset}
}

/** Let promises and microtasks settle without moving the clock. */
const flush = async () => { for (let i = 0; i < 10; i += 1) await Promise.resolve() }

// Only the clock: the Runtime reports stopping on a microtask, and a faked queueMicrotask
// would hold that back until the clock moved.
beforeEach(() => {
  vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date']})
})
afterEach(() => { vi.useRealTimers() })

describe('starting', () => {
  it('says it is connecting at once, and leaves what to show meanwhile to the panel', async () => {
    let finish: (value: Bootstrap) => void = () => {}
    const slow = new Promise<Bootstrap>(resolve => { finish = resolve })
    const h = harness(source([slow]))
    h.supervisor.start()
    // The panel shows nothing for the first moments and a skeleton after; it is told now.
    expect(h.status.log).toEqual(['connecting'])
    finish({config: config()})
    await flush()
    expect(h.status.log).toEqual(['connecting', 'started'])
    expect(h.supervisor.state.kind).toBe('running')
  })

  it('ignores a second start while one is in flight', async () => {
    const src = source([{config: config()}])
    const h = harness(src)
    h.supervisor.start()
    h.supervisor.start()
    h.supervisor.retry()
    await flush()
    expect(src.calls).toBe(1)
    expect(h.started).toHaveLength(1)
  })
})

describe('failing', () => {
  it('obtains one fresh session for an idle closed handshake even without an active turn', async () => {
    const src = source([{config: config('old')}, {config: config('new')}])
    const h = harness(src, [new IdleHandshakeClosed('closed', 'session_closed', 410), new FakeRuntime()], false)
    h.supervisor.start(); await flush()
    expect(src.calls).toBe(2)
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.running).toHaveLength(1)
    expect(h.status.log).toEqual(['connecting', 'started'])
  })

  it('does not loop when the replacement handshake is also closed', async () => {
    const src = source([{config: config()}])
    const h = harness(src, [new IdleHandshakeClosed('first', 'session_closed', 410),
      new IdleHandshakeClosed('second', 'session_closed', 410)], false)
    h.supervisor.start(); await flush(); await vi.advanceTimersByTimeAsync(60_000)
    expect(src.calls).toBe(2)
    expect(h.supervisor.state.kind).toBe('unavailable')
  })

  it('cannot renew an inline capability after a closed handshake', async () => {
    const src = source([{config: config()}], false)
    const h = harness(src, [new IdleHandshakeClosed('closed', 'session_closed', 410)], false)
    h.supervisor.start(); await flush()
    expect(src.calls).toBe(1)
    expect(h.supervisor.state.kind).toBe('unavailable')
  })

  it.each([401, 409, 410])('does not treat an ordinary %s failure as an idle handshake closure', async status => {
    const src = source([{config: config()}])
    const h = harness(src, [new TransportError('capacity exhausted', 'server_error', status)], false)
    h.supervisor.start(); await flush()
    expect(src.calls).toBe(1)
    expect(h.supervisor.state.kind).toBe('unavailable')
  })

  it('climbs a capped ladder and keeps retrying for the life of the page', async () => {
    const src = source([new BootFailure('down', 503)])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state).toMatchObject({kind: 'retrying', attempt: 1})
    for (const delay of RETRY_MS) await vi.advanceTimersByTimeAsync(delay)
    expect(src.calls).toBe(RETRY_MS.length + 1)
    expect(h.supervisor.state.kind).toBe('retrying')
    expect(h.status.log.at(-1)).toBe('unavailable:503:true')
    await vi.advanceTimersByTimeAsync(RETRY_MS.at(-1)!)
    expect(src.calls).toBe(RETRY_MS.length + 2)
  })

  it('does not retry on its own what another attempt cannot fix', async () => {
    const src = source([new BootFailure('refused', 403)])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state.kind).toBe('unavailable')
    expect(h.status.log).toEqual(['connecting', 'unavailable:403:false'])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(src.calls).toBe(1)
  })

  it('treats a page held by another Runtime as a failed start, not as an assistant', async () => {
    const h = harness(source([{config: config()}]), [null])
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state.kind).toBe('retrying')
    expect(h.running).toHaveLength(0)
    expect(h.status.failure).toEqual({status: null, retrying: true})
  })

  it('starts again from the panel when the person asks, and comes up', async () => {
    const src = source([new BootFailure('refused', 403), {config: config()}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    h.status.retry?.()
    await flush()
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.status.log.at(-1)).toBe('started')
  })

  it('carries the status of a transport error thrown by the Runtime itself', () => {
    expect(BootFailure.from(Object.assign(new Error('gone'), {status: 404})).status).toBe(404)
    expect(BootFailure.from(new Error('odd')).status).toBeNull()
    expect(new BootFailure('x', 401).final).toBe(true)
    expect(new BootFailure('x', 409).final).toBe(false)
  })
})

describe('when the session ends under the page', () => {
  it('tears the old Runtime down and connects a new session, without asking', async () => {
    const src = source([{config: config('first')}, {config: config('second')}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    await vi.advanceTimersByTimeAsync(STABLE_MS)
    const first = h.started[0]!
    first.end('ended')
    await flush()
    expect(first.stopCalls).toBe(1)
    expect(src.calls).toBe(2)
    expect(h.running).toHaveLength(2)
    expect(h.supervisor.state.kind).toBe('running')
  })

  it('keeps a capped reconnect cadence when every session ends as soon as it starts', async () => {
    const src = source([{config: config()}])
    const runtimes = Array.from({length: 10}, () => new FakeRuntime())
    const h = harness(src, [...runtimes])
    h.supervisor.start()
    for (let round = 0; round < 8; round += 1) {
      await flush()
      h.started.at(-1)?.end('error')
      await flush()
      await vi.advanceTimersByTimeAsync(10_000)
    }
    expect(h.started.length).toBeGreaterThan(RETRY_MS.length)
    h.started.at(-1)?.end('error')
    await flush()
    expect(h.supervisor.state.kind).toBe('retrying')
    expect(h.status.log.at(-1)).toBe('unavailable:null:true')
  })

  it('reconnects at once again after a session that ran long enough to be trusted', async () => {
    const src = source([{config: config()}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    h.started[0]!.end('error')
    await flush()
    await vi.advanceTimersByTimeAsync(RETRY_MS[0]!)
    expect(h.started).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(STABLE_MS)
    h.started[1]!.end('ended')
    await flush()
    expect(h.started).toHaveLength(3)
  })

  it('leaves a session another page took over to that page', async () => {
    const src = source([{config: config()}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    h.started[0]!.end('replaced')
    await flush()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.supervisor.state).toEqual({kind: 'settled', reason: 'replaced'})
    expect(h.started[0]!.stopCalls).toBe(0)
    expect(src.calls).toBe(1)
  })

  it('cannot renew an inline session, but restarts it after a back-forward cache restore', async () => {
    const ended = harness(inlineSource(config()))
    ended.supervisor.start()
    await flush()
    ended.started[0]!.end('ended')
    await flush()
    expect(ended.supervisor.state).toEqual({kind: 'settled', reason: 'not_renewable'})

    const restored = harness(inlineSource(config()))
    restored.supervisor.start()
    await flush()
    restored.started[0]!.end('suspended')
    await flush()
    expect(restored.running).toHaveLength(2)
  })
})

describe('renewing the identity behind a running session', () => {
  it('keeps a standalone capability when sending instead of obtaining another session', async () => {
    const src = source([{config: config()}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    const runtime: Supervised = h.started[0]!
    await runtime.beforeSubmit?.()
    expect(src.calls).toBe(1)
    expect(h.started[0]!.renewals).toHaveLength(0)
  })

  it('renews at the time the bootstrap named, then again at the next one', async () => {
    const now = Date.now()
    const src = source([
      {config: config(), renew_at: now + 60_000},
      {config: {...config(), principalBinding: 'renewed'}, renew_at: now + 120_000},
    ])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.started[0]!.renewals).toHaveLength(1)
    expect(h.started[0]!.renewals[0]!.principalBinding).toBe('renewed')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.started[0]!.renewals).toHaveLength(2)
  })

  it('waits while the Runtime is busy, and retries a renewal that failed', async () => {
    const now = Date.now()
    const src = source([{config: config(), renew_at: now + 10_000}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    const runtime = h.started[0]!
    runtime.renewable = false
    await vi.advanceTimersByTimeAsync(10_000)
    expect(src.calls).toBe(1)
    runtime.renewable = true
    await vi.advanceTimersByTimeAsync(RENEW_RETRY_MS)
    expect(runtime.renewals).toHaveLength(1)
  })

  it('never renews what the bootstrap did not say to renew, nor an inline session', async () => {
    const plain = harness(source([{config: config()}]))
    plain.supervisor.start()
    const inline = harness(inlineSource(config()))
    inline.supervisor.start()
    await flush()
    await vi.advanceTimersByTimeAsync(3_600_000)
    expect(plain.started[0]!.renewals).toHaveLength(0)
    expect(inline.started[0]!.renewals).toHaveLength(0)
  })
})

describe('endpointSource', () => {
  const ok = (body: unknown) => new Response(JSON.stringify(body), {status: 200})

  it('exchanges the application sign-in for an assertion, and the assertion for a session', async () => {
    const calls: Array<{url: string; body: unknown}> = []
    const fetchImpl = (async (url: string, init?: RequestInit) => {
      calls.push({url, body: JSON.parse(String(init?.body))})
      return url.endsWith('/identity') ? ok({signed: 'assertion'}) : ok({config: config()})
    }) as unknown as typeof fetch
    const src = endpointSource({bootstrap: '/bootstrap', identity: '/identity',
      instanceId: 'tab-1', fetchImpl})
    await expect(src.obtain()).resolves.toMatchObject({config: {sessionId: 's1'}})
    expect(calls).toEqual([
      {url: '/identity', body: {runtime_instance_id: 'tab-1'}},
      {url: '/bootstrap', body: {signed: 'assertion'}},
    ])
  })

  it('names the status of a refusal, and none for a network failure or a bad payload', async () => {
    const refusing = endpointSource({bootstrap: '/b', instanceId: 'x',
      fetchImpl: (async () => new Response('{}', {status: 429})) as unknown as typeof fetch})
    await expect(refusing.obtain()).rejects.toMatchObject({status: 429})
    const offline = endpointSource({bootstrap: '/b', instanceId: 'x',
      fetchImpl: (async () => { throw new TypeError('Failed to fetch') }) as unknown as typeof fetch})
    await expect(offline.obtain()).rejects.toMatchObject({status: null})
    const empty = endpointSource({bootstrap: '/b', instanceId: 'x',
      fetchImpl: (async () => ok({})) as unknown as typeof fetch})
    await expect(empty.obtain()).rejects.toBeInstanceOf(BootFailure)
  })
})

describe('with nothing under way, a session is made when asked for and let go when idle', () => {
  it('leaves a failed start to the Retry beside its reason', async () => {
    const src = source([new BootFailure('down', 503), {config: config()}])
    const h = harness(src, [], false)
    h.supervisor.connect()
    await flush()
    expect(h.supervisor.state.kind).toBe('unavailable')
    expect(h.status.log.at(-1)).toBe('unavailable:503:false')
    await vi.advanceTimersByTimeAsync(60_000)
    expect(src.calls).toBe(1)
    h.status.retry?.()
    await flush()
    expect(h.supervisor.state.kind).toBe('running')
  })

  it('does not replace a session that ended; the next message makes a new one', async () => {
    const src = source([{config: config('first')}, {config: config('second')}])
    const h = harness(src, [], false)
    h.supervisor.connect()
    await flush()
    await vi.advanceTimersByTimeAsync(STABLE_MS)
    h.started[0]!.end('ended')
    await flush()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.supervisor.state.kind).toBe('idle')
    expect(h.status.log.at(-1)).toBe('rested')
    expect(src.calls).toBe(1)
    h.supervisor.connect()
    await flush()
    expect(src.calls).toBe(2)
    expect(h.supervisor.state.kind).toBe('running')
  })

  it('lets an idle session go without replacing it, and never one that is busy', async () => {
    const h = harness(source([{config: config()}]), [], false)
    h.supervisor.connect()
    await flush()
    const runtime = h.started[0]!
    runtime.renewable = false
    h.supervisor.rest()
    expect(h.supervisor.state.kind).toBe('running')
    expect(runtime.stopCalls).toBe(0)
    runtime.renewable = true
    h.supervisor.rest()
    await flush()
    expect(h.supervisor.state.kind).toBe('idle')
    expect(runtime.stopCalls).toBe(1)
    expect(h.status.log.at(-1)).toBe('rested')
    expect(h.started).toHaveLength(1)
  })

  it('ignores a request for a session while one is up or on its way', async () => {
    const src = source([{config: config()}])
    const h = harness(src, [], false)
    h.supervisor.connect()
    h.supervisor.connect()
    await flush()
    h.supervisor.connect()
    await flush()
    expect(src.calls).toBe(1)
  })
})

describe('a fresh conversation', () => {
  it('lets the running session go, forgets it and rests the panel', async () => {
    const src = source([{config: config('old')}, {config: config('fresh')}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    const runtime = h.started[0]!
    expect(h.supervisor.newConversation()).toBe(true)
    expect(h.supervisor.state.kind).toBe('idle')
    expect(runtime.stopCalls).toBe(1)
    expect(h.reset).toHaveBeenCalledOnce()
    expect(h.status.log.at(-1)).toBe('rested')
    // What the person sends next opens a new session, never rejoins the old one.
    h.supervisor.connect()
    await flush()
    expect(src.calls).toBe(2)
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.started).toHaveLength(2)
  })

  it('refuses while a turn is under way: the session is stopped explicitly first', async () => {
    const h = harness(source([{config: config()}]))
    h.supervisor.start()
    await flush()
    h.started[0]!.renewable = false
    expect(h.supervisor.newConversation()).toBe(false)
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.started[0]!.stopCalls).toBe(0)
    expect(h.reset).not.toHaveBeenCalled()
  })

  it('abandons a start still in flight: its session belongs to nobody', async () => {
    let finish: (value: Bootstrap) => void = () => {}
    const slow = new Promise<Bootstrap>(resolve => { finish = resolve })
    const src = source([slow, {config: config('fresh')}])
    const h = harness(src)
    h.supervisor.start()
    expect(h.supervisor.newConversation()).toBe(true)
    expect(h.supervisor.state.kind).toBe('idle')
    finish({config: config('late')})
    await flush()
    // The landing Runtime is let go, not presented as the new conversation's session.
    expect(h.supervisor.state.kind).toBe('idle')
    expect(h.started).toHaveLength(1)
    expect(h.started[0]!.stopCalls).toBe(1)
    h.supervisor.connect()
    await flush()
    expect(src.calls).toBe(2)
    expect(h.supervisor.state.kind).toBe('running')
  })

  it('never adopts the abandoned start once the new conversation connected its own', async () => {
    let finish: (value: Bootstrap) => void = () => {}
    const slow = new Promise<Bootstrap>(resolve => { finish = resolve })
    const src = source([slow, {config: config('fresh')}])
    const h = harness(src)
    h.supervisor.start()
    expect(h.supervisor.newConversation()).toBe(true)
    h.supervisor.connect()
    await flush()
    // The new conversation's own start is running before the abandoned one lands.
    expect(h.supervisor.state.kind).toBe('running')
    finish({config: config('old')})
    await flush()
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.started).toHaveLength(2)
    // The successor keeps running; the abandoned Runtime is let go, never presented.
    expect(h.started[0]!.stopCalls).toBe(0)
    expect(h.started[1]!.stopCalls).toBe(1)
    expect(src.calls).toBe(2)
    h.supervisor.connect()
    await flush()
    expect(src.calls).toBe(2)
  })

  it('forgets an idle session pointer and leaves the rested page ready', () => {
    const h = harness(source([{config: config()}]), [], false)
    expect(h.supervisor.newConversation()).toBe(true)
    expect(h.reset).toHaveBeenCalledOnce()
    expect(h.status.log.at(-1)).toBe('rested')
  })
})
