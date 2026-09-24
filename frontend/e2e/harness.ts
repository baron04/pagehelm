/**
 * The harness for the panel's Chromium checks: the built panel, mounted over a scripted host.
 *
 * Every state can be rendered without a live session this way: the states a real run
 * reaches rarely -- indeterminate, replaced, obscured confirmation -- are exactly the ones
 * worth pinning. The panel under test is the build (`dist/panel/panel.js`), not the source:
 * what ships is what is checked.
 */
import {existsSync, readFileSync} from 'node:fs'
import {createServer, type Server} from 'node:http'
import type {AddressInfo} from 'node:net'
import {resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {test as base, type Browser, type Page} from '@playwright/test'
import contract from '../../src/web_operation_agent/web_runtime/panel_contract.json' with {type: 'json'}
import {SAMPLE_SUGGESTIONS} from '../dev/application'
import type {Labels, PanelConfiguration} from '../src/contract/panel'

const here = fileURLToPath(new URL('.', import.meta.url))
export const PANEL_BUNDLE = resolve(here, '../dist/panel/panel.js')

/**
 * The hello reply's panel configuration, as `RemoteSessionChannel._panel_configuration()`
 * assembles it from the contract -- `test_panel_contract.py` pins that correspondence -- for
 * the one stand-in application the harness plays, which declares its own suggestions.
 */
export const CONFIGURATION = {
  protocol: contract.protocol,
  limits: contract.limits,
  labels: contract.label_keys as Labels,
  suggestions: SAMPLE_SUGGESTIONS,
  notices: contract.notices as Record<string, string>,
  panel_states: contract.panel_states,
  progress_states: contract.progress_states,
  terminal_error_codes: contract.terminal_error_codes,
  terminal_panel_state_by_code: contract.terminal_panel_state_by_code,
  session_expires_at: 0,
  principal_expires_at: 0,
} as PanelConfiguration
export const labels = CONFIGURATION.labels
export const notices = CONFIGURATION.notices

const DRIVER_HTML = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>panel harness</title>
<style>body{margin:0;height:2000px;font-family:system-ui}</style></head>
<body>
<script id="configuration" type="application/json">__CONFIGURATION__</script>
<script id="snapshot" type="application/json">__SNAPSHOT__</script>
<script type="module" src="/driver.js"></script>
</body></html>
`

// External on purpose: under `script-src 'self'` an inline module would be refused, and the
// refusal would be the harness's, not the panel's.
const DRIVER_JS = `
import {mountPanel} from './panel.js'

// If the panel installs its own global handler this stops being the page's.
const pageHandler = () => {}
window.onerror = pageHandler
window.__onerrorIsOurs = () => window.onerror === pageHandler

const configuration = JSON.parse(document.getElementById('configuration').textContent)
let snapshot = JSON.parse(document.getElementById('snapshot').textContent)
const listeners = new Set()
window.__calls = []

// A class, and every method reads \`this\` -- because the real \`PanelHost\` is a class and
// every one of its methods reads \`this\`. The double used to be an object literal of
// receiver-free arrows, so \`onClick={host.newConversation}\` worked here and threw in
// production: the panel handed React a bare function reference, the real host lost its
// receiver, and the TypeError escaped into the host application, which raised its own
// "something went wrong" dialog. A double more forgiving than the thing it stands in for
// does not test the thing it stands in for.
class ScriptedHost {
  constructor(configuration) { this.configuration = configuration }
  snapshot() { return this.read() }
  read() { return snapshot }
  subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) }
  submit(text) { this.note('submit', text) }
  retrySubmit() { this.note('retrySubmit') }
  cancel() { this.note('cancel') }
  decide(id, decision) { this.note('decide', id, decision) }
  // The real store clears the view here before it tells the server. A double that only
  // recorded the call would let the panel pass a test the product would fail.
  newConversation() {
    this.note('newConversation')
    window.__setSnapshot({entries: [], pendingApproval: null, notice: null, optimistic: null,
                          hasOlder: false})
  }
  loadOlder(before) { this.note('loadOlder', before) }
  dismissNotice() { this.note('dismissNotice') }
  reload() { this.note('reload') }
  note(...args) { window.__calls.push(args) }
}

const host = new ScriptedHost(configuration)
window.__setSnapshot = patch => {
  snapshot = {...snapshot, ...patch}
  for (const listener of listeners) listener(snapshot)
}
window.__host = host
window.__panel = mountPanel({host, allowedOrigins: [location.origin]})
`

export type Json = null | boolean | number | string | Json[] | {[key: string]: Json | undefined}
export type Snapshot = Record<string, Json>
export type Entry = Record<string, Json>

export function snapshot(patch: Snapshot = {}): Snapshot {
  return {
    connection: {state: 'ready', busy: false, stopping: false, online: true},
    entries: [],
    pendingApproval: null,
    notice: null,
    hasOlder: false,
    loadingOlder: false,
    closed: false,
    optimistic: null,
    ...patch,
  }
}

export function entry(id: string, kind: string, text: string, extra: Entry = {}): Entry {
  return {
    id, sequence: Number(id.slice(1)) || 1, turn_id: 'turn-1', at: 1_757_000_000_000,
    kind, tone: 'neutral', text, ...extra,
  }
}

// What a host application locking itself down would send. The panel must construct under
// it: no inline script, no injected <style>, no Trusted Types policy of its own.
export const STRICT_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; " +
  "connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; " +
  "form-action 'none'; require-trusted-types-for 'script'; trusted-types 'none'"

export const PENDING = {
  schema_version: 1, approval_id: 'a-1',
  effect: '确认报价单 S00002，使其成为销售订单',
  targets: ['sale.order · S00002 · Acme Corporation', 'state · draft → sale'],
  expires_at: 0, principal_digest: '', turn_id: 'turn-1', operation_id: 'op-1',
}

function serve(state: Snapshot, csp: string): Promise<Server> {
  const page = DRIVER_HTML
    .replace('__CONFIGURATION__', JSON.stringify(CONFIGURATION))
    .replace('__SNAPSHOT__', JSON.stringify(state))
  const bundle = readFileSync(PANEL_BUNDLE)
  const server = createServer((request, response) => {
    const reply = (body: string | Buffer, type: string) => {
      const headers: Record<string, string | number> = {
        'Content-Type': type, 'Content-Length': Buffer.byteLength(body),
      }
      if (csp && type.startsWith('text/html')) headers['Content-Security-Policy'] = csp
      response.writeHead(200, headers)
      response.end(body)
    }
    if (request.url === '/panel.js') return reply(bundle, 'text/javascript; charset=utf-8')
    if (request.url === '/driver.js') return reply(DRIVER_JS, 'text/javascript; charset=utf-8')
    if (request.url === '/' || request.url === '/index.html') {
      return reply(page, 'text/html; charset=utf-8')
    }
    response.writeHead(404)
    response.end()
  })
  return new Promise(done => server.listen(0, '127.0.0.1', () => done(server)))
}

export interface OpenOptions {
  width?: number
  height?: number
  reducedMotion?: 'reduce' | 'no-preference'
  colorScheme?: 'light' | 'dark'
  csp?: string
  deviceScaleFactor?: number
}

/** One mounted panel: the page, the uncaught errors it raised, and the host's record. */
export class Harness {
  readonly errors: string[] = []

  constructor(readonly page: Page) {
    page.on('pageerror', error => this.errors.push(String(error)))
  }

  /** What the panel asked of its host, in order. */
  calls(): Promise<Json[][]> {
    return this.page.evaluate(() => (window as unknown as {__calls: Json[][]}).__calls)
  }

  /** Replace part of the host's snapshot, as the session does when something arrives. */
  set(patch: Snapshot): Promise<void> {
    // Handed over as serialised JSON: the snapshot's recursive type is more than Playwright's
    // argument typing can unfold.
    return this.page.evaluate(value => {
      (window as unknown as {__setSnapshot: (patch: unknown) => void}).__setSnapshot(
        JSON.parse(value))
    }, JSON.stringify(patch))
  }

  /**
   * Let entrance animations finish, so geometry and pixels are the resting ones.
   *
   * A bounding box taken mid-arrival reads the window as translated and scaled by the
   * entrance -- sub-pixel lies the suite would otherwise chase under a bundler bump.
   */
  async settle(): Promise<void> {
    // An animation cut short -- a tooltip fading in on a control the click just replaced --
    // rejects its `finished`; it leaves nothing behind to measure, so it is not waited on.
    await this.page.evaluate(() => Promise.all(document.querySelector('#woa-assistant')!
      .shadowRoot!.getAnimations().map(animation => animation.finished.catch(() => null))))
  }

  /** Publish a confirmation the way the session does, after the panel is up. */
  async raiseApproval(overrides: Record<string, Json> = {}): Promise<void> {
    await this.page.evaluate(request => {
      (window as unknown as {__setSnapshot: (patch: unknown) => void}).__setSnapshot(
        {pendingApproval: {...request, expires_at: Date.now() + 120000}})
    }, {...PENDING, ...overrides})
    await this.page.locator('.woa-approval').waitFor({timeout: 5000})
  }

  /** The shadow root's focused element, read by the attribute or property named. */
  active(): Promise<{id: string; testid: string | null; text: string; label: string | null}> {
    return this.page.evaluate(() => {
      const element = document.querySelector('#woa-assistant')!.shadowRoot!.activeElement
      return {
        id: element?.id ?? '', testid: element?.getAttribute('data-testid') ?? null,
        text: element?.textContent ?? '', label: element?.getAttribute('aria-label') ?? null,
      }
    })
  }
}

/** `open(state, options)` mounts a fresh panel in its own browser context. */
export const test = base.extend<{open: (state: Snapshot, options?: OpenOptions) => Promise<Harness>}>({
  open: async ({browser}: {browser: Browser}, use) => {
    const cleanups: (() => Promise<unknown>)[] = []
    await use(async (state, options = {}) => {
      if (!existsSync(PANEL_BUNDLE)) {
        throw new Error(`the assistant panel is not built at ${PANEL_BUNDLE}; run npm run build`)
      }
      const server = await serve(state, options.csp ?? '')
      cleanups.push(() => new Promise(done => server.close(done)))
      const context = await browser.newContext({
        viewport: {width: options.width ?? 1280, height: options.height ?? 800},
        reducedMotion: options.reducedMotion ?? 'no-preference',
        colorScheme: options.colorScheme ?? 'light',
        deviceScaleFactor: options.deviceScaleFactor ?? 1,
      })
      cleanups.push(() => context.close())
      const page = await context.newPage()
      const harness = new Harness(page)
      const {port} = server.address() as AddressInfo
      await page.goto(`http://127.0.0.1:${port}/`, {waitUntil: 'load'})
      // Waiting on a locator rather than `waitForFunction`: the latter evaluates a string in
      // the page, which a strict CSP correctly refuses. A pending confirmation opens the
      // window straight away, so either surface counts as mounted.
      await page.locator('.woa-launcher, .woa-window').first().waitFor({timeout: 8000})
      return harness
    })
    for (const cleanup of cleanups.reverse()) await cleanup()
  },
})

export {expect} from '@playwright/test'
