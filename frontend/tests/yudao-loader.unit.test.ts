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
})
