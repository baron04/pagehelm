/** The shipped loader and Runtime must share refreshed browser identity before dispatch. */
import {fileURLToPath} from 'node:url'
import {test, expect, snapshot} from './harness'
import type {EmbeddedWebRuntime} from '../src/runtime/runtime'
import type {RuntimeConfig} from '../src/runtime/types'
import type * as RuntimeModule from '../src/runtime/index'

type AuthWindow = Window & {__authRuntime: EmbeddedWebRuntime}

for (const refused of [false, true]) {
  test(`expired application token ${refused ? 'refuses dispatch when refresh fails' : 'refreshes before dispatch'}`, async ({open}) => {
    const h = await open(snapshot())
    const calls: string[] = []
    let expired = false
    let issued = 0
    const origin = new URL(h.page.url()).origin
    await h.page.route('**/woa-agent-boot/bootstrap', async route => {
      calls.push('bootstrap')
      const allowed = !expired || route.request().headers().authorization === 'Bearer renewed'
      await route.fulfill({status: allowed ? 200 : 401, json: allowed ? {config: {
        endpoint: origin + '/agent', sessionId: 'browser-auth-test', clientToken: 'capability',
        sessionGeneration: 'generation', applicationId: 'test-app', principalBinding: `identity-${++issued}`,
        principalExpiresAt: Date.now() + 600_000, sessionExpiresAt: Date.now() + 600_000, panel: false,
      }} : {}})
    })
    await h.page.route('**/admin-api/system/auth/refresh-token?*', async route => {
      calls.push('refresh')
      await route.fulfill({json: refused ? {code: 401} : {
        code: 0, data: {accessToken: 'renewed', refreshToken: 'new-refresh'},
      }})
    })
    await h.page.route('**/agent/v1/sessions/browser-auth-test/*', async route => {
      calls.push(new URL(route.request().url()).pathname.split('/').at(-1)!)
      await route.fulfill({json: {accepted: true}})
    })
    await h.page.evaluate(() => {
      for (const [key, token] of [['ACCESS_TOKEN', 'expired'], ['REFRESH_TOKEN', 'refresh']]) {
        localStorage.setItem(key!, JSON.stringify({v: JSON.stringify(token)}))
      }
      // The test drives the exported Supervisor; prevent the application loader auto-starting another one.
      const marker = document.createElement('script')
      marker.dataset.woaBootstrap = '/woa-agent-boot/bootstrap'
      document.head.append(marker)
    })
    await h.page.addScriptTag({path: fileURLToPath(new URL('../../examples/yudao/deploy/cloud/yudao-loader.js', import.meta.url))})
    await h.page.evaluate(async () => {
      const moduleUrl = '/assistant.js'
      const {Supervisor, EmbeddedWebRuntime} = await import(moduleUrl) as typeof RuntimeModule
      let runtime: EmbeddedWebRuntime
      const supervisor = new Supervisor({
        source: {renewable: true, async obtain() {
          const response = await fetch('/woa-agent-boot/bootstrap', {method: 'POST'})
          if (!response.ok) throw Object.assign(new Error('bootstrap refused'), {status: response.status})
          return response.json()
        }},
        async start(config: RuntimeConfig) {
          runtime = new EmbeddedWebRuntime(config)
          runtime.observe = async () => ({observation: {url: location.href}} as Awaited<ReturnType<EmbeddedWebRuntime['observe']>>)
          ;(window as unknown as AuthWindow).__authRuntime = runtime
          return runtime
        },
        status: {connecting() {}, unavailable() {}, started() {}, rested() {}},
        eager: () => false,
      })
      supervisor.start()
      await new Promise<void>(resolve => {
        const timer = setInterval(() => {
          if (supervisor.state.kind === 'running') {clearInterval(timer); resolve()}
        }, 10)
      })
    })
    expired = true
    const outcome = await h.page.evaluate(async () => {
      const runtime = (window as unknown as AuthWindow).__authRuntime
      try { await runtime.submitTask('unchanged question'); return 'sent' }
      catch (error) { return (error as {code: string}).code }
    })
    expect(outcome).toBe(refused ? 'business_authentication' : 'sent')
    expect(calls).toEqual(refused
      ? ['bootstrap', 'bootstrap', 'refresh']
      : ['bootstrap', 'bootstrap', 'refresh', 'bootstrap', 'observations', 'messages'])
    expect(h.errors).toEqual([])
  })
}
