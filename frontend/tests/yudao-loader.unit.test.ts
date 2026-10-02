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
})
