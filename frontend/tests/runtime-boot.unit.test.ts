import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest'
import {
  BootFailure, CONNECTING_GRACE_MS, RENEW_RETRY_MS, RETRY_MS, STABLE_MS, Supervisor,
  endpointSource,
  inlineSource, type Bootstrap, type SessionSource, type Supervised,
} from '../src/runtime/boot'
import type {EntryFailure, StandingEntry} from '../src/runtime/entry'
import type {RuntimeConfig} from '../src/runtime/types'

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

class FakeEntry implements StandingEntry {
  log: string[] = []
  failure: EntryFailure | null = null
  retry: (() => void) | null = null
  connecting() { this.log.push('connecting') }
  unavailable(failure: EntryFailure, retry: () => void) {
    this.log.push(`unavailable:${failure.status}:${failure.retrying}`)
    this.failure = failure
    this.retry = retry
  }
  remove() { this.log.push('remove') }
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

function harness(src: SessionSource, runtimes: Array<FakeRuntime | null> = []) {
  const entry = new FakeEntry()
  const started: FakeRuntime[] = []
  const running: Supervised[] = []
  const supervisor = new Supervisor({
    source: src,
    entry,
    start: async () => {
      const next = runtimes.length ? runtimes.shift() : new FakeRuntime()
      if (next) started.push(next)
      return next ?? null
    },
    onRunning: runtime => { running.push(runtime) },
  })
  return {supervisor, entry, started, running}
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
  it('comes up without ever showing the entry when the first attempt is quick', async () => {
    const h = harness(source([{config: config()}]))
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.running).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(CONNECTING_GRACE_MS * 2)
    expect(h.entry.log).toEqual(['remove'])
  })

  it('says it is connecting when the first attempt outlasts the grace, then gets out of the way', async () => {
    let finish: (value: Bootstrap) => void = () => {}
    const slow = new Promise<Bootstrap>(resolve => { finish = resolve })
    const h = harness(source([slow]))
    h.supervisor.start()
    await vi.advanceTimersByTimeAsync(CONNECTING_GRACE_MS + 1)
    expect(h.entry.log).toEqual(['connecting'])
    finish({config: config()})
    await flush()
    expect(h.entry.log).toEqual(['connecting', 'remove'])
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
  it('climbs a capped ladder and keeps retrying for the life of the page', async () => {
    const src = source([new BootFailure('down', 503)])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state).toMatchObject({kind: 'retrying', attempt: 1})
    for (const delay of RETRY_MS) await vi.advanceTimersByTimeAsync(delay)
    expect(src.calls).toBe(RETRY_MS.length + 1)
    expect(h.supervisor.state.kind).toBe('retrying')
    expect(h.entry.log.at(-1)).toBe('unavailable:503:true')
    await vi.advanceTimersByTimeAsync(RETRY_MS.at(-1)!)
    expect(src.calls).toBe(RETRY_MS.length + 2)
  })

  it('does not retry on its own what another attempt cannot fix', async () => {
    const src = source([new BootFailure('refused', 403)])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state.kind).toBe('unavailable')
    expect(h.entry.log).toEqual(['unavailable:403:false'])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(src.calls).toBe(1)
  })

  it('treats a page held by another Runtime as a failed start, not as an assistant', async () => {
    const h = harness(source([{config: config()}]), [null])
    h.supervisor.start()
    await flush()
    expect(h.supervisor.state.kind).toBe('retrying')
    expect(h.running).toHaveLength(0)
    expect(h.entry.failure).toEqual({status: null, retrying: true})
  })

  it('starts again from the entry when the person asks, and comes up', async () => {
    const src = source([new BootFailure('refused', 403), {config: config()}])
    const h = harness(src)
    h.supervisor.start()
    await flush()
    h.entry.retry?.()
    await flush()
    expect(h.supervisor.state.kind).toBe('running')
    expect(h.entry.log.at(-1)).toBe('remove')
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
    expect(h.entry.log.at(-1)).toBe('unavailable:null:true')
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
