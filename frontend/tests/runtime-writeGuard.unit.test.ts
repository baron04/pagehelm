import {afterEach, describe, expect, it, vi} from 'vitest'
import {urlDigest} from '../src/runtime/navigation'
import type {ActionEnvelope, ActionResultEnvelope, ObservationEnvelope} from '../src/runtime/types'
import {createRedactor} from '../src/runtime/redaction'
import {EmbeddedWebRuntime} from '../src/runtime/runtime'
import {
  WRITE_GUARD_TTL_S, closeWriteGuard, openWriteGuard, releaseWriteGuard,
  validWriteGuardPath,
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
    const expiresAt = Date.now() / 1000 + 120
    const fetcher = vi.fn(async () => reply(200, {active: true, expires_at: expiresAt}))
    expect(await openWriteGuard('/woa/page-guard', fetcher as typeof fetch)).toBe(expiresAt)
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
      async () => reply(200, {active: true, expires_at: Date.now() / 1000}),
      async () => reply(200, {active: true, expires_at: Date.now() / 1000 - 1}),
      async () => reply(200, {active: true, expires_at: 1e308}),
      async () => reply(200, null),
      async () => new Response('not json', {status: 200}),
      async () => { throw new TypeError('network down') },
    ]
    for (const answer of cases) {
      expect(await openWriteGuard('/woa/page-guard', vi.fn(answer) as typeof fetch)).toBeNull()
    }
  })

  it('never calls an invalid path', async () => {
    const fetcher = vi.fn()
    expect(await openWriteGuard('//evil.test/x', fetcher as typeof fetch)).toBeNull()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('closes with a best-effort request that cannot throw', async () => {
    const fetcher = vi.fn(async () => { throw new TypeError('offline') })
    await expect(closeWriteGuard('/woa/page-guard', fetcher as typeof fetch)).resolves.toBeUndefined()
    expect((fetcher.mock.calls[0] as unknown as [string])[0]).toBe('/woa/page-guard/end')
  })

  it('requires an explicit application acknowledgement before user handoff', async () => {
    for (const answer of [
      async () => reply(503, {active: false}),
      async () => reply(200, {active: true}),
      async () => reply(200, {}),
      async () => new Response('not json', {status: 200}),
      async () => { throw new TypeError('offline') },
    ]) {
      expect(await releaseWriteGuard('/woa/page-guard', vi.fn(answer) as typeof fetch))
        .toBe(false)
    }
    const fetcher = vi.fn(async () => reply(200, {active: false}))
    expect(await releaseWriteGuard('/woa/page-guard', fetcher as typeof fetch)).toBe(true)
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

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

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

  it('runs once the application confirms the window, and says the window held', async () => {
    const expiresAt = Date.now() / 1000 + 120
    const fetcher = vi.fn(async () => reply(200, {active: true, expires_at: expiresAt}))
    vi.stubGlobal('fetch', fetcher)
    const {runtime, executed, posts, action} = actingRuntime('/woa/page-guard')
    await runtime._handleAction(action as never)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(executed).toEqual(['click'])
    expect(posts.map(row => row.path)).toEqual(['/claims', '/results'])
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.write_guard_held).toBe(true)
  })

  it('does not claim or execute an already expired guard confirmation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => reply(200,
      {active: true, expires_at: Date.now() / 1000 - 1})))
    const {runtime, executed, posts, action} = actingRuntime('/woa/page-guard')
    await runtime._handleAction(action as never)
    expect(executed).toEqual([])
    expect(posts.map(row => row.path)).toEqual(['/results'])
    expect(posts[0]?.body.status).toBe('rejected')
    expect(posts[0]?.body.error_code).toBe('capability_refused')
  })

  it('does not say the window held when it had lapsed by the time the action settled', async () => {
    const now = Date.now()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(now)
    const fetcher = vi.fn(async () => reply(200, {active: true, expires_at: (now + 120_000) / 1000}))
    vi.stubGlobal('fetch', fetcher)
    const {runtime, posts, action} = actingRuntime('/woa/page-guard')
    const state = runtime as unknown as Record<string, unknown>
    state._execute = async () => {
      clock.mockReturnValue(now + 120_001)
      return {success: true}
    }
    await runtime._handleAction(action as never)
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.write_guard_held).toBe(false)
  })

  it('does not execute when the confirmed guard expires during the claim request', async () => {
    const now = Date.now()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(now)
    vi.stubGlobal('fetch', vi.fn(async () => reply(200,
      {active: true, expires_at: (now + 1000) / 1000})))
    const {runtime, executed, posts, action} = actingRuntime('/woa/page-guard')
    const state = runtime as unknown as Record<string, unknown>
    const post = state._post as (path: string, body: Record<string, unknown>) => Promise<unknown>
    state._post = async (path: string, body: Record<string, unknown>) => {
      if (path === '/claims') clock.mockReturnValue(now + 1000)
      return post(path, body)
    }
    await runtime._handleAction(action as never)
    expect(executed).toEqual([])
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.ok).toBe(false)
    expect(receipt.error_code).toBe('capability_refused')
    expect(receipt.write_guard_held).toBe(false)
  })

  it('does not execute when the action deadline expires during an unguarded claim', async () => {
    const now = Date.now()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(now)
    const {runtime, executed, posts, action} = actingRuntime()
    const state = runtime as unknown as Record<string, unknown>
    const post = state._post as (path: string, body: Record<string, unknown>) => Promise<unknown>
    state._post = async (path: string, body: Record<string, unknown>) => {
      if (path === '/claims') clock.mockReturnValue(action.deadline)
      return post(path, body)
    }
    await runtime._handleAction(action as never)
    expect(executed).toEqual([])
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.ok).toBe(false)
    expect(receipt.error_code).toBe('deadline_expired')
  })

  it('is untouched in an application without one', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    const {runtime, executed, posts, action} = actingRuntime()
    await runtime._handleAction(action as never)
    expect(fetcher).not.toHaveBeenCalled()
    expect(executed).toEqual(['click'])
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.write_guard_held).toBe(false)
  })

  it('releases the guard before highlighting and reports a completed handoff', async () => {
    const fetcher = vi.fn(async (url: string) => url.endsWith('/begin')
      ? reply(200, {active: true, expires_at: Date.now() / 1000 + 120})
      : reply(200, {active: false}))
    vi.stubGlobal('fetch', fetcher)
    const {runtime, posts, action} = actingRuntime('/woa/page-guard')
    const state = runtime as unknown as Record<string, unknown>
    const execute = vi.fn(() => {
      state.highlightHandle = {active: () => true, clear: vi.fn()}
      return Promise.resolve({success: true})
    })
    state._execute = execute
    action.command.action = 'highlight'
    await runtime._handleAction(action as never)
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      '/woa/page-guard/begin', '/woa/page-guard/end',
    ])
    expect(execute).toHaveBeenCalledOnce()
    expect((posts[1]?.body.receipt as Record<string, unknown>).ok).toBe(true)
  })

  it('does not highlight or report handoff if guard release is unacknowledged', async () => {
    const fetcher = vi.fn(async (url: string) => url.endsWith('/begin')
      ? reply(200, {active: true, expires_at: Date.now() / 1000 + 120})
      : reply(200, {active: true}))
    vi.stubGlobal('fetch', fetcher)
    const {runtime, posts, action} = actingRuntime('/woa/page-guard')
    action.command.action = 'highlight'
    await runtime._handleAction(action as never)
    const receipt = posts[1]?.body.receipt as Record<string, unknown>
    expect(receipt.ok).toBe(false)
    expect(receipt.error_code).toBe('capability_refused')
  })

  it('releases the guard only after an exact form readback and checks it again after release', async () => {
    const input = document.createElement('input')
    input.name = 'phone'
    input.value = 'new value'
    document.body.append(input)
    try {
      for (const changedAfterRelease of [false, true]) {
        input.value = 'new value'
        const fetcher = vi.fn(async (url: string) => {
          if (url.endsWith('/begin')) {
            return reply(200, {active: true, expires_at: Date.now() / 1000 + 120})
          }
          if (changedAfterRelease) input.value = 'application changed it'
          return reply(200, {active: false})
        })
        vi.stubGlobal('fetch', fetcher)
        const {runtime, posts, action} = actingRuntime('/woa/page-guard')
        const state = runtime as unknown as Record<string, unknown>
        const elements = [{ref: 'e2', role: 'textbox', name: 'Phone', tag: 'input',
          epoch: 1, attrs: {field: 'phone'}}]
        state.controller = {selectorMap: {}, getBrowserState: vi.fn(), clickElement: vi.fn(),
          inputText: vi.fn(), selectOption: vi.fn(), scroll: vi.fn(), dispose: vi.fn()}
        state.getElementByIndex = () => input
        state.currentObservation = {...state.currentObservation as object,
          observation: {url: 'https://app.test/odoo/sales', elements, render_state: 'ready'}}
        state.observationFingerprints = new Map([['e2', runtime.page.fingerprint('e2')]])
        state.observe = async (actionId: string) => ({session_id: 's', observation_id: 'o-2',
          page_epoch: 2, caused_by_action_id: actionId,
          observation: {url: 'https://app.test/odoo/sales', elements,
            render_state: 'ready'}})
        state._execute = EmbeddedWebRuntime.prototype._execute.bind(runtime)
        action.command.action = 'handoff_form';
        (action.command as {args: Record<string, unknown>}).args = {fields: [{ref: 'e2',
          field: 'phone', control: 'textbox', value: 'new value'}]}
        await runtime._handleAction(action as never)
        const receipt = posts[1]?.body.receipt as Record<string, unknown>
        expect(receipt.ok).toBe(!changedAfterRelease)
        expect(receipt.args_echo).toEqual({fields: [{ref: 'e2', field: 'phone',
          control: 'textbox', value: '[redacted]'}]})
        expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
          '/woa/page-guard/begin', '/woa/page-guard/end',
        ])
      }
    } finally { input.remove() }
  })

  it('does not release the guard if the form value already changed', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      reply(200, {active: true, expires_at: Date.now() / 1000 + 120})))
    const {runtime, posts, action} = actingRuntime('/woa/page-guard')
    const state = runtime as unknown as Record<string, unknown>
    state.controller = {selectorMap: {}, getBrowserState: vi.fn(), clickElement: vi.fn(),
      inputText: vi.fn(), selectOption: vi.fn(), scroll: vi.fn(), dispose: vi.fn()}
    state.getElementByIndex = () => ({tagName: 'INPUT', name: 'phone',
      value: 'wrong', isConnected: true})
    state.currentObservation = {...state.currentObservation as object,
      observation: {url: 'https://app.test/odoo/sales', render_state: 'ready', elements: [
        {ref: 'e2', role: 'textbox', name: 'Phone', tag: 'input', epoch: 1,
          attrs: {field: 'phone'}},
      ]}}
    state.observationFingerprints = new Map([['e2', runtime.page.fingerprint('e2')]])
    state._execute = EmbeddedWebRuntime.prototype._execute.bind(runtime)
    action.command.action = 'handoff_form';
    (action.command as {args: Record<string, unknown>}).args = {fields: [{ref: 'e2',
      field: 'phone', control: 'textbox', value: 'new value'}]}
    await runtime._handleAction(action as never)
    expect((posts[1]?.body.receipt as Record<string, unknown>).ok).toBe(false)
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.map(([url]) => url))
      .toEqual(['/woa/page-guard/begin'])
  })

  it.each(['ready', 'button-disabled', 'initially-disabled', 'button-hidden', 'field-reset', 'scope-replaced', 'release-refused'])(
    'hands over a scoped form with a highlight only when %s', async mode => {
      const form = document.createElement('form')
      form.id = 'handoff-editor'
      form.innerHTML = '<label for="draft-name">Name</label><input id="draft-name">' +
        '<button type="submit">Confirm</button>'
      document.body.append(form)
      const input = form.querySelector('input')!, button = form.querySelector('button')!
      input.value = 'Draft'
      button.disabled = mode === 'initially-disabled'
      button.getBoundingClientRect = () => ({width: 80, height: 20} as DOMRect)
      const clicked = vi.fn(); button.addEventListener('click', clicked)
      try {
        vi.stubGlobal('fetch', vi.fn(async (url: string) => {
          if (url.endsWith('/begin')) return reply(200, {active: true, expires_at: Date.now() / 1000 + 120})
          if (mode === 'release-refused') return reply(503, {})
          if (mode === 'button-disabled') button.disabled = true
          if (mode === 'button-hidden') button.hidden = true
          if (mode === 'field-reset') input.value = ''
          if (mode === 'scope-replaced') form.id = 'different'
          return reply(200, {active: false})
        }))
        const {runtime, posts, action} = actingRuntime('/woa/page-guard')
        const state = runtime as unknown as Record<string, unknown>
        state.controller = {selectorMap: {}, getBrowserState: vi.fn(), clickElement: vi.fn(),
          inputText: vi.fn(), selectOption: vi.fn(), scroll: vi.fn(), dispose: vi.fn()}
        state.getElementByIndex = (_map: unknown, index: number) => index === 1 ? input : button
        const read = () => runtime.page.describe({url: 'https://app.test/odoo/sales', title: 'Editor',
          content: '[1]<input type=text />\n[2]<button>Confirm />'}, 1).observation
        state.currentObservation = {session_id: 's', observation_id: 'o-1', page_epoch: 1,
          observation: {...read(), render_state: 'ready'}}
        state.observe = async (actionId: string) => ({session_id: 's', observation_id: 'o-2',
          page_epoch: 2, caused_by_action_id: actionId, observation: {...read(), render_state: 'ready'}})
        state._execute = EmbeddedWebRuntime.prototype._execute.bind(runtime)
        action.command.action = 'handoff_form'
        ;(action.command as {args: Record<string, unknown>}).args = {fields: [{ref: 'e1',
          field: 'name', label: 'Name', binding: 'label', scope: 'form:handoff-editor',
          control: 'textbox', value: 'Draft'}],
          submit: {ref: 'e2', label: 'Confirm', scope: 'form:handoff-editor'}}
        await runtime._handleAction(action as never)
        const handedOver = ['ready', 'button-disabled', 'initially-disabled'].includes(mode)
        expect((posts[1]?.body.receipt as Record<string, unknown>).ok).toBe(handedOver)
        expect(button.style.outline !== '').toBe(handedOver)
        expect(clicked).not.toHaveBeenCalled()
        if (handedOver) (state.highlightHandle as {clear(): void}).clear()
      } finally { form.remove() }
    })

  it('never holds back, swallows or stops on a person\'s own gesture', () => {
    const {runtime} = actingRuntime()
    const state = runtime as unknown as Record<string, unknown>
    state.stored = {taskInFlight: true, pageWorked: true, inFlight: {action_id: 'a-1'}, cachedResult: null}
    state.cancelTask = vi.fn(async () => {})
    state.highlightHandle = {clear: vi.fn(), active: () => true}
    const preventDefault = vi.fn(), stopImmediatePropagation = vi.fn()
    const event = {isTrusted: true, cancelable: true, composedPath: () => [document.body],
      preventDefault, stopImmediatePropagation} as unknown as Event
    runtime._userActed(event)
    expect(preventDefault).not.toHaveBeenCalled()
    expect(stopImmediatePropagation).not.toHaveBeenCalled()
    expect(state.cancelTask).not.toHaveBeenCalled()
    // The assistant's marker goes; the turn itself carries on.
    expect((state.highlightHandle as unknown)).toBeNull()
  })

  it('closes the assistant\'s read-only window the moment the person acts on the page', () => {
    const {runtime} = actingRuntime()
    const state = runtime as unknown as Record<string, unknown>
    const calls: string[] = []
    const original = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input)); return new Response('{"active":false}', {status: 200})
    }) as typeof fetch
    try {
      state.config = {...(state.config as object), pageWriteGuard: '/woa/page-guard'}
      state.writeGuardUntil = Date.now() + 60_000
      const event = {isTrusted: true, cancelable: true, composedPath: () => [document.body],
        preventDefault: vi.fn(), stopImmediatePropagation: vi.fn()} as unknown as Event
      runtime._userActed(event)
      // Asked for synchronously, before the application's own handler for the gesture runs.
      expect(calls).toEqual(['/woa/page-guard/end'])
      expect(state.writeGuardUntil).toBe(0)
      // A synthetic event, or one inside the assistant's own panel, closes nothing.
      state.writeGuardUntil = Date.now() + 60_000
      runtime._userActed({...event, isTrusted: false} as unknown as Event)
      expect(calls.length).toBe(1)
    } finally { globalThis.fetch = original }
  })
})

describe('a confirmed write guard across document navigation', () => {
  it('durably correlates the claimed action before its dispatch can end the document', async () => {
    const {runtime, action} = actingRuntime()
    const state = runtime as unknown as Record<string, unknown>
    const saved: ActionEnvelope[] = []
    state._save = () => {
      const inFlight = runtime.stored.inFlight
      if (inFlight) saved.push(structuredClone(inFlight))
    }
    state._execute = async () => {
      const last = saved.at(-1)!
      expect(last.action_id).toBe(action.action_id)
      expect(last.phase).toBe('claimed')
      expect(last.dispatched_at).toBeTypeOf('number')
      expect(last.dispatched_at).toBeLessThanOrEqual(Date.now())
      return {success: true}
    }
    await runtime._handleAction(action as ActionEnvelope)
  })

  it.each(['exact-load', 'wrong-load', 'reload', 'redirect'])(
    'recovers a canonicalized destination only from its exact document load: %s', async mode => {
      const requested = 'https://app.test/projects', current = requested + '?group=mine'
      const dispatchedAt = Date.now() - 1000
      vi.stubGlobal('location', {href: current})
      vi.stubGlobal('performance', {timeOrigin: dispatchedAt + 10,
        getEntriesByType: () => [{entryType: 'navigation', startTime: 0,
          name: mode === 'wrong-load' ? current : requested,
          type: mode === 'reload' ? 'reload' : 'navigate',
          redirectCount: mode === 'redirect' ? 1 : 0}]})
      const {runtime, action, posts} = actingRuntime('/woa/page-guard')
      const state = runtime as unknown as Record<string, unknown>
      state.documentId = 'new-document'
      const saved = {...action, phase: 'claimed', navigation_kind: 'goto', document_id: 'old-document',
        dispatched_at: dispatchedAt, expected_url_digest: await urlDigest(requested),
        started_url_digest: await urlDigest('https://app.test/home'),
        started_url_display: 'https://app.test/home',
        write_guard_path: '/woa/page-guard', write_guard_expires_at_ms: Date.now() + 60_000,
        command: {...action.command, action: 'goto', args: {url: requested}}}
      state.stored = {inFlight: saved}
      const execute = vi.fn()
      state._execute = execute
      const observation = {session_id: 's', observation_id: 'o-2', page_epoch: 2,
        caused_by_action_id: action.action_id,
        observation: {url: current, elements: [], render_state: 'ready'}} as unknown as ObservationEnvelope
      await runtime._recoverAfterNavigation(observation)
      expect(execute).not.toHaveBeenCalled()
      expect(posts.map(row => row.path)).toEqual(['/results'])
      const result = posts[0]!.body as unknown as ActionResultEnvelope
      expect(result.action_id).toBe(action.action_id)
      expect(result.command_digest).toBe(action.command_digest)
      expect(result.based_on_observation_id).toBe(action.based_on_observation_id)
      expect(result.observation_after?.caused_by_action_id).toBe(action.action_id)
      expect(result.status).toBe(mode === 'exact-load' ? 'completed' : 'indeterminate')
      if (mode === 'exact-load') {
        expect(result.receipt?.write_guard_held).toBe(true)
        expect(result.receipt?.url_after).toBe(current)
        expect(result.receipt?.args_echo).toEqual({url: requested})
      } else {
        expect(result.receipt).toBeNull()
        expect(result.error_code).toBe('execution_state_unknown')
      }
      expect(JSON.stringify(result)).not.toContain('dispatched_at')
    })

  it('persists only the confirmed window before dispatch, including a prepared replay', async () => {
    const {runtime, action} = actingRuntime('/woa/page-guard')
    const state = runtime as unknown as Record<string, unknown>
    state.writeGuardUntil = Date.now() + 60_000
    state.redactorInstance = createRedactor()
    const prepare = state._prepare as (a: ActionEnvelope, o: ObservationEnvelope,
      n: {url: string; kind: string}) => Promise<ActionEnvelope>
    const observation = state.currentObservation as ObservationEnvelope
    for (const expiry of [Date.now() + 60_000, Date.now() + 120_000]) {
      state.writeGuardUntil = expiry
      await prepare.call(runtime, action as ActionEnvelope, observation,
        {url: 'https://app.test/next', kind: 'link'})
      const wal = (state.stored as {inFlight: ActionEnvelope}).inFlight
      expect(wal.write_guard_path).toBe('/woa/page-guard')
      expect(wal.write_guard_expires_at_ms).toBe(expiry)
      expect(wal.started_url_raw).toBeUndefined()
    }
  })

  it('restores guard proof only for claimed navigation within the same unexpired window', async () => {
    vi.stubGlobal('location', {href: 'https://app.test/next'})
    for (const [path, expiry, phase, proven] of [
      ['/woa/page-guard', Date.now() + 60_000, 'claimed', true],
      ['/woa/other', Date.now() + 60_000, 'claimed', false],
      ['/woa/page-guard', Date.now() - 1, 'claimed', false],
      ['/woa/page-guard', undefined, 'claimed', false],
      ['/woa/page-guard', Number.POSITIVE_INFINITY, 'claimed', false],
      ['/woa/page-guard', Date.now() + 60_000, 'prepared', false],
    ] as const) {
      const {runtime, action} = actingRuntime('/woa/page-guard')
      const state = runtime as unknown as Record<string, unknown>
      state.redactorInstance = createRedactor()
      const saved = {...action, phase, navigation_kind: 'link',
        expected_url_digest: await urlDigest(location.href), started_url_digest: 'different',
        write_guard_path: path, write_guard_expires_at_ms: expiry}
      state.stored = {inFlight: saved}
      let result!: ActionResultEnvelope
      state._deliver = async (value: ActionResultEnvelope) => {result = value}
      await runtime._recoverAfterNavigation({session_id: 's', observation_id: 'o-2', page_epoch: 2,
        observation: {url: location.href, elements: []}} as unknown as ObservationEnvelope)
      expect(result.receipt?.write_guard_held === true).toBe(proven)
      expect(JSON.stringify(result)).not.toContain('write_guard_expires_at_ms')
      expect(JSON.stringify(result)).not.toContain('write_guard_path')
      if (phase === 'prepared') expect(result.status).toBe('rejected')
    }
  })
})
