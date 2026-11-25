import {afterEach, describe, expect, it, vi} from 'vitest'

interface FakeStore {
  $subscribe(listener: () => void): () => void
  notify(): void
}
interface FakeUserStore extends FakeStore {
  isSetUser: boolean
  user: {id: number}
}
interface FakeAppStore extends FakeStore {
  pageLoading: boolean
}
interface FakeRouter {
  currentRoute: {value: {path: string; matched: unknown[]}}
  beforeEach(cb: (to: {path: string}) => void): () => void
  afterEach(cb: () => void): () => void
  navigate(path: string, matched?: unknown[]): void
}
interface FakeYudaoApp {
  appEl: HTMLDivElement
  userStore: FakeUserStore
  appStore: FakeAppStore
  router: FakeRouter
  mountVue(): void
  showSplash(): void
  showContent(): void
}

function makeStore<T extends object>(initial: T): T & FakeStore {
  const listeners = new Set<() => void>()
  return Object.assign(initial, {
    $subscribe(listener: () => void) {
      listeners.add(listener)
      return () => { listeners.delete(listener) }
    },
    notify() {
      for (const listener of listeners) listener()
    },
  })
}

function installFakeYudaoApp({
  path = '/index',
  isSetUser = true,
  pageLoading = false,
  matched = [{path: '/'}, {path}],
  splash = false,
  mounted = true,
}: {
  path?: string
  isSetUser?: boolean
  pageLoading?: boolean
  matched?: unknown[]
  splash?: boolean
  mounted?: boolean
} = {}): FakeYudaoApp {
  window.history.replaceState({}, '', path)
  const appEl = document.createElement('div')
  appEl.id = 'app'
  document.body.append(appEl)

  const userStore = makeStore({isSetUser, user: {id: isSetUser ? 1 : 0}})
  const appStore = makeStore({pageLoading})
  const beforeHooks = new Set<(to: {path: string}) => void>()
  const afterHooks = new Set<() => void>()
  const router: FakeRouter = {
    currentRoute: {value: {path, matched}},
    beforeEach(cb) { beforeHooks.add(cb); return () => { beforeHooks.delete(cb) } },
    afterEach(cb) { afterHooks.add(cb); return () => { afterHooks.delete(cb) } },
    navigate(nextPath, nextMatched = [{path: nextPath}]) {
      for (const cb of beforeHooks) cb({path: nextPath})
      window.history.replaceState({}, '', nextPath)
      router.currentRoute.value = {path: nextPath, matched: nextMatched}
      for (const cb of afterHooks) cb()
    },
  }
  const stores = new Map<string, unknown>([['admin-user', userStore], ['app', appStore]])
  const vueApp = {config: {globalProperties: {$pinia: {_s: stores}, $router: router}}}
  const showSplash = () => { appEl.innerHTML = '<div class="app-loading">芋道管理系统</div>' }
  const showContent = () => { appEl.innerHTML = '<section class="v-layout">工作台</section>' }
  const mountVue = () => {
    ;(appEl as unknown as {__vue_app__?: unknown}).__vue_app__ = vueApp
  }
  if (splash) showSplash()
  else showContent()
  if (mounted) mountVue()
  return {appEl, userStore, appStore, router, mountVue, showSplash, showContent}
}

const flush = async () => { for (let i = 0; i < 8; i += 1) await Promise.resolve() }

afterEach(() => {
  const handle = (window as unknown as {__WOA_YUDAO_LOADER__?: {dispose?: () => void}}).__WOA_YUDAO_LOADER__
  handle?.dispose?.()
  delete (window as unknown as {__WOA_YUDAO_LOADER__?: unknown}).__WOA_YUDAO_LOADER__
  delete (window as unknown as {__WOA_ASSISTANT__?: unknown}).__WOA_ASSISTANT__
  document.head.innerHTML = ''
  document.body.innerHTML = ''
  window.history.replaceState({}, '', '/')
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
    installFakeYudaoApp({path: '/index', isSetUser: true, pageLoading: false})
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

  it('gates mounting past splash/login loading, keeps normal nav, tears down on SPA logout, and remounts after re-login', async () => {
    // 1. Refresh with existing token: stay unmounted across `.app-loading` splash and pending Vue/user/route boot.
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('token-1')}))
    const yudao = installFakeYudaoApp({
      path: '/index', isSetUser: false, pageLoading: true, matched: [], splash: true, mounted: false,
    })
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    yudao.showContent()
    yudao.mountVue()
    ;(window as unknown as {__WOA_YUDAO_LOADER__: {sync(): void}}).__WOA_YUDAO_LOADER__.sync()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    yudao.userStore.isSetUser = true
    yudao.userStore.user = {id: 1}
    yudao.userStore.notify()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    yudao.appStore.pageLoading = false
    yudao.router.navigate('/index', [{path: '/'}, {path: '/index'}])
    yudao.appStore.notify()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).not.toBeNull()

    // 2. Normal authenticated route switch with transient `pageLoading` must NOT tear down.
    const destroy = vi.fn(() => { delete (window as unknown as {__WOA_ASSISTANT__?: unknown}).__WOA_ASSISTANT__ })
    ;(window as unknown as {__WOA_ASSISTANT__?: unknown}).__WOA_ASSISTANT__ = {destroy}
    yudao.appStore.pageLoading = true
    yudao.appStore.notify()
    yudao.router.navigate('/erp/sale/order', [{path: '/'}, {path: '/erp/sale/order'}])
    yudao.appStore.pageLoading = false
    yudao.appStore.notify()
    await flush()
    expect(destroy).not.toHaveBeenCalled()

    // 3. SPA logout clears token/user and returns to `/login`: tear down immediately.
    localStorage.clear()
    yudao.userStore.isSetUser = false
    yudao.userStore.user = {id: 0}
    yudao.userStore.notify()
    yudao.router.navigate('/login', [{path: '/login'}])
    await flush()
    expect(destroy).toHaveBeenCalledWith({clearSession: true})
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    // 4. Re-login in progress (`/login` + fullscreen loading mask + `pageLoading`) stays unmounted until mask closes.
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('token-2')}))
    const mask = document.createElement('div')
    mask.className = 'el-loading-mask is-fullscreen'
    mask.style.display = 'block'
    mask.style.opacity = '1'
    document.body.append(mask)
    yudao.appStore.pageLoading = true
    yudao.appStore.notify()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    yudao.userStore.isSetUser = true
    yudao.userStore.user = {id: 2}
    yudao.appStore.pageLoading = false
    yudao.router.navigate('/index', [{path: '/'}, {path: '/index'}])
    yudao.appStore.notify()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).toBeNull()

    mask.remove()
    await flush()
    expect(document.querySelector('script[data-woa-bootstrap]')).not.toBeNull()
  })

  it('does not remove a bootstrap tag already placed by the host page before the loader ran', async () => {
    const marker = document.createElement('script')
    marker.dataset.woaBootstrap = '/woa-agent-boot/bootstrap'
    document.head.append(marker)
    const yudao = installFakeYudaoApp({path: '/index', isSetUser: true, pageLoading: false})
    localStorage.setItem('ACCESS_TOKEN', JSON.stringify({v: JSON.stringify('live-token')}))
    vi.resetModules()
    // @ts-expect-error Application loader intentionally ships as framework-free JS.
    await import('../../examples/yudao/deploy/cloud/yudao-loader.js')
    const destroy = vi.fn()
    ;(window as unknown as {__WOA_ASSISTANT__?: unknown}).__WOA_ASSISTANT__ = {destroy}

    localStorage.clear()
    yudao.userStore.isSetUser = false
    yudao.userStore.notify()
    await flush()
    expect(destroy).not.toHaveBeenCalled()
    expect(marker.isConnected).toBe(true)
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
