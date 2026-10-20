import {afterEach, describe, expect, it, vi} from 'vitest'

afterEach(() => {
  delete (window as unknown as {__WOA_YUDAO_LOADER__?: boolean}).__WOA_YUDAO_LOADER__
  document.head.innerHTML = ''
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Yudao identity for the server-declared native guard', () => {
  it('attaches the browser identity only to bootstrap and exact same-origin guard POSTs', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('synthetic-token')}))
    localStorage.setItem('tenantId', JSON.stringify({v: JSON.stringify('1')}))
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({config: {
      pageWriteGuard: '/test-native-guard'}}), {status: 200, headers: {'Content-Type': 'application/json'}}))
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})
    await window.fetch('/test-native-guard/begin', {method: 'POST'})
    await window.fetch('/test-native-guard/end', {method: 'POST'})
    await window.fetch('/test-native-guard/begin', {method: 'GET'})
    await window.fetch('/test-native-guard/begin?other=1', {method: 'POST'})
    await window.fetch('https://other.test/test-native-guard/begin', {method: 'POST'})
    await window.fetch('/admin-api/crm/customer/update', {method: 'POST'})
    const headers = fetcher.mock.calls.map(([, init]) => new Headers(init?.headers).get('Authorization'))
    expect(headers).toEqual(['Bearer synthetic-token', 'Bearer synthetic-token', 'Bearer synthetic-token',
      null, null, null, null])
    expect(new Headers(fetcher.mock.calls[1]![1]?.headers).get('tenant-id')).toBe('1')
  })

  it('renews an expired bootstrap token once and retries with the new token', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('expired-token')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('refresh-token')}))
    const fetcher = vi.fn().mockImplementation(async (input: string, init?: RequestInit) => {
      const url = new URL(input, location.href)
      if (url.pathname === '/admin-api/system/auth/refresh-token') {
        return new Response(JSON.stringify({code: 0, data: {
          accessToken: 'renewed-token', refreshToken: 'renewed-refresh'}}), {status: 200})
      }
      const token = new Headers(init?.headers).get('Authorization')
      return new Response(JSON.stringify(token === 'Bearer renewed-token'
        ? {config: {}} : {error: 'sign_in_required'}),
      {status: token === 'Bearer renewed-token' ? 200 : 401})
    })
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    const response = await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})
    expect(response.status).toBe(200)
    expect(fetcher.mock.calls.map(([, init]) => new Headers(init?.headers).get('Authorization')))
      .toEqual(['Bearer expired-token', null, 'Bearer renewed-token'])
    expect(JSON.parse(JSON.parse(localStorage.getItem('ACCESS_TOKEN')!).v)).toBe('renewed-token')
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('does not treat an upstream 503 as an expired token', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('synthetic-token')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('refresh-token')}))
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({error: 'assistant_unavailable'}),
      {status: 503}))
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(503)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('keeps the bootstrap 401 when the application also refuses refresh', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('expired-token')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('expired-refresh')}))
    const fetcher = vi.fn().mockImplementation(async () => {
      return new Response(JSON.stringify({error: 'sign_in_required'}), {status: 401,
        headers: {'Content-Type': 'application/json'}})
    })
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(401)
    expect(fetcher.mock.calls.map(([input]) => new URL(input, location.href).pathname))
      .toEqual(['/woa-agent-boot/bootstrap', '/admin-api/system/auth/refresh-token'])
  })

  it('starts from a surviving refresh token and replaces expired storage metadata after renewal', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({e: Date.now() - 1, v: JSON.stringify('expired')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('refresh')}))
    const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
      if (input.startsWith('/admin-api/')) return Response.json({code: 0, data: {accessToken: 'new'}})
      return Response.json({config: {}}, {status:
        new Headers(init?.headers).get('Authorization') === 'Bearer new' ? 200 : 401})
    })
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect(document.querySelector('script[data-woa-bootstrap]')).not.toBeNull()
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(200)
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(200)
    expect(fetcher.mock.calls.filter(([input]) => input.startsWith('/admin-api/'))).toHaveLength(1)
  })

  it('reuses a token the page refreshed while bootstrap was awaiting its 401', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('old')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('refresh')}))
    const fetcher = vi.fn(async (_input: string, init?: RequestInit) => {
      if (new Headers(init?.headers).get('Authorization') === 'Bearer old') {
        localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('page-new')}))
        return Response.json({}, {status: 401})
      }
      return Response.json({config: {}})
    })
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(200)
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(fetcher.mock.calls.map(([, init]) => new Headers(init?.headers).get('Authorization')))
      .toEqual(['Bearer old', 'Bearer page-new'])
  })

  it('does not overwrite a sign-out while refresh is in flight', async () => {
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('old')}))
    localStorage.setItem('REFRESH_TOKEN', JSON.stringify({v: JSON.stringify('refresh')}))
    const fetcher = vi.fn(async (input: string) => {
      if (input.startsWith('/admin-api/')) {
        localStorage.clear()
        return Response.json({code: 0, data: {accessToken: 'must-not-install', refreshToken: 'new-refresh'}})
      }
      return Response.json({}, {status: 401})
    })
    vi.stubGlobal('fetch', fetcher)
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect((await window.fetch('/woa-agent-boot/bootstrap', {method: 'POST'})).status).toBe(401)
    expect(localStorage.getItem('ACCESS_TOKEN')).toBeNull()
    expect(localStorage.getItem('REFRESH_TOKEN')).toBeNull()
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
