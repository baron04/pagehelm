import {afterEach, describe, expect, it, vi} from 'vitest'

type Shell = {destroy(): void}

afterEach(() => {
  ;(window as unknown as {__WOA_BOOTSTRAP_SHELL__?: Shell})
    .__WOA_BOOTSTRAP_SHELL__?.destroy()
  document.head.innerHTML = ''
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the Odoo-owned assistant shell', () => {
  it('keeps an entry collapsed until the person opens a missing-runtime error', async () => {
    const tag = document.createElement('script')
    tag.dataset.woaRuntime = '/assistant/v1/runtime/assistant.js'
    tag.dataset.woaBootstrap = '/assistant/bootstrap'
    document.head.append(tag)

    class Sheet {
      replaceSync(css: string) { void css }
    }
    vi.stubGlobal('CSSStyleSheet', Sheet)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', {status: 404})))

    // @ts-expect-error The Odoo shell is deliberately framework-free JavaScript.
    await import('../../examples/odoo/deploy/addons/woa_runtime/static/src/loader.js')

    await vi.waitFor(() => {
      const host = document.getElementById('woa-assistant-bootstrap-shell')
      expect(host).not.toBeNull()
      expect(host!.shadowRoot!.querySelector<HTMLElement>('.panel')!.hidden).toBe(true)
      expect(host!.shadowRoot!.querySelector('.message')!.textContent).toContain('HTTP 404')
      host!.shadowRoot!.querySelector<HTMLButtonElement>('.launcher')!.click()
      expect(host!.shadowRoot!.querySelector<HTMLElement>('.panel')!.hidden).toBe(false)
    })
  })
})
