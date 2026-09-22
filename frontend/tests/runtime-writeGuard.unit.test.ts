import {afterEach, describe, expect, it, vi} from 'vitest'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import {
  WRITE_GUARD_TTL_S, closeWriteGuard, openWriteGuard, validWriteGuardPath,
} from '../src/runtime/writeGuard'

function reply(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}})
}

describe('the page write guard path', () => {
  it('accepts a same-origin absolute path and nothing else', () => {
    expect(validWriteGuardPath('/woa/page-guard')).toBe(true)
    for (const bad of ['', 'woa/page-guard', '//evil.test/x', 'https://evil.test/x', '/a?b',
      '/a/../b', '/a/./b', '/a b', '/' + 'x'.repeat(128), 7, null]) {
      expect(validWriteGuardPath(bad)).toBe(false)
    }
  })
})

describe('opening the guard', () => {
  it('asks for a bounded window and accepts only an explicit confirmation', async () => {
    const fetcher = vi.fn(async () => reply(200, {active: true, expires_at: 1790000000.5}))
    expect(await openWriteGuard('/woa/page-guard', fetcher as typeof fetch)).toBe(true)
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/woa/page-guard/begin')
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('same-origin')
    expect(JSON.parse(String(init.body))).toEqual({ttl_s: WRITE_GUARD_TTL_S})
  })

  it('refuses on anything short of a confirmation', async () => {
    const cases: Array<() => Promise<Response>> = [
      async () => reply(404, {}),
      async () => reply(503, {active: true, expires_at: 1}),
      async () => reply(200, {active: false, expires_at: 1}),
      async () => reply(200, {active: 'yes', expires_at: 1}),
      async () => reply(200, {active: true}),
      async () => reply(200, null),
      async () => new Response('not json', {status: 200}),
      async () => { throw new TypeError('network down') },
    ]
    for (const answer of cases) {
      expect(await openWriteGuard('/woa/page-guard', vi.fn(answer) as typeof fetch)).toBe(false)
    }
  })

  it('never calls an invalid path', async () => {
    const fetcher = vi.fn()
    expect(await openWriteGuard('//evil.test/x', fetcher as typeof fetch)).toBe(false)
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('closes with a best-effort request that cannot throw', async () => {
    const fetcher = vi.fn(async () => { throw new TypeError('offline') })
    await expect(closeWriteGuard('/woa/page-guard', fetcher as typeof fetch)).resolves.toBeUndefined()
    expect((fetcher.mock.calls[0] as unknown as [string])[0]).toBe('/woa/page-guard/end')
  })
})

type Posted = {path: string; body: Record<string, unknown>}

function actingRuntime(pageWriteGuard?: string) {
  const runtime = Object.create(EmbeddedWebRuntime.prototype) as EmbeddedWebRuntime
  const state = runtime as unknown as Record<string, unknown>
  state.config = {endpoint: 'https://agent.test', sessionId: 's', clientToken: 't',
    sessionGeneration: 'g', pageControllerModule: 'https://app.test/pc.js',
    ...(pageWriteGuard === undefined ? {} : {pageWriteGuard})}
  state.runtimeInstanceId = 'runtime-guard'
  state.stopped = false
  state.stored = {inFlight: null, cachedResult: null}
  state._save = () => {}
  state._panelState = () => {}
  const url = 'https://app.test/odoo/sales'
  state.currentObservation = {session_id: 's', observation_id: 'o-1', page_epoch: 1,
    observation: {url, elements: []}}
  state.observe = async (actionId: string) => ({session_id: 's', observation_id: 'o-2',
    page_epoch: 2, caused_by_action_id: actionId, observation: {url, elements: []}})
  state._actionFreshness = () => ({fresh: true, reason: ''})
  const executed: string[] = []
  state._execute = async () => { executed.push('click'); return {success: true} }
  const posts: Posted[] = []
  state._post = async (path: string, body: Record<string, unknown>) => {
    posts.push({path, body})
    return {claimed: true}
  }
  const action = {schema_version: 1, session_id: 's', turn_id: 't', action_id: 'a-1',
    based_on_observation_id: 'o-1', page_epoch: 1, deadline: Date.now() + 5000,
    command_digest: '0'.repeat(64),
    command: {command_id: 'a-1', action: 'click', args: {ref: 'e0'}, expect_navigation: false}}
  return {runtime, executed, posts, action}
}

afterEach(() => { vi.unstubAllGlobals() })

describe('a page action in an application that names a write guard', () => {
  it('is refused, before anything is claimed, when the guard cannot be opened', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => reply(404, {})))
    const {runtime, executed, posts, action} = actingRuntime('/woa/page-guard')
    await runtime._handleAction(action as never)
    expect(executed).toEqual([])
    expect(posts.map(row => row.path)).toEqual(['/results'])
    expect(posts[0]?.body.status).toBe('rejected')
    expect(posts[0]?.body.error_code).toBe('capability_refused')
    expect(String(posts[0]?.body.error_detail)).toMatch(/^page_write_guard_unavailable/)
  })

  it('runs once the application confirms the window', async () => {
    const fetcher = vi.fn(async () => reply(200, {active: true, expires_at: 1790000000}))
    vi.stubGlobal('fetch', fetcher)
    const {runtime, executed, posts, action} = actingRuntime('/woa/page-guard')
    await runtime._handleAction(action as never)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(executed).toEqual(['click'])
    expect(posts.map(row => row.path)).toEqual(['/claims', '/results'])
  })

  it('is untouched in an application without one', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const {runtime, executed, action} = actingRuntime()
    await runtime._handleAction(action as never)
    expect(fetcher).not.toHaveBeenCalled()
    expect(executed).toEqual(['click'])
  })
})
