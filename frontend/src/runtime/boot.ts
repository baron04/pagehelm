/**
 * The one owner of the assistant's life on a page, from the first attempt to the last.
 *
 * A Runtime lives for one session. Sessions end for reasons the page can simply undo -- they
 * expire, the agent restarts, a quiet tab is reaped, the page comes back from the back-forward
 * cache -- and for one it must not: the assistant was taken over by another page, and
 * reconnecting here would have the two pages taking it from each other in turn. Deciding
 * which is which, starting the next session, renewing the identity behind the current one,
 * and saying so whenever there is no assistant to show: that is this file, and only it.
 *
 * Two invariants hold throughout. Nothing here reaches the host page's error handlers (I1):
 * every callback goes through `contain.ts`. And the assistant is never silently absent (I2):
 * the panel (`shell.ts`) is up from the moment this script runs, before and between sessions,
 * and says what is happening when one is being made or could not be.
 */
import {DEFAULT_CONFIGURATION} from '../contract/panel'
import {detach, later, listen, report, type Cancel} from './contain'
import {engagementRecord} from './engagement'
import {examplesFromPage, watchForExamples} from './examples'
import {startEmbeddedRuntime} from './runtime'
import {IdleHandshakeClosed} from './transport'
import {createShell} from './shell'
import {START_TEXT, panelStartStatus, type StartStatus} from './startStatus'
import type {RuntimeConfig} from './types'

/** A capped ladder: connection faults retry for the life of the page without request storms. */
export const RETRY_MS: readonly number[] = [1000, 3000, 8000]
/**
 * How long a session with nothing to do is kept before it is let go. The next thing the
 * person sends makes a new one; the conversation is the server's and comes back with it.
 */
export const KEEP_ALIVE_MS = 5 * 60_000
/** How soon to ask again when a renewal could not happen yet. */
export const RENEW_RETRY_MS = 5000
/**
 * A session that ends sooner than this after it started did not really start. Reconnecting
 * at once would turn a session that is refused every time into a request storm against the
 * server, so it climbs the same capped retry ladder as a failed start.
 */
export const STABLE_MS = 10_000

const INSTANCE_KEY = 'woa.runtime.instance.v1'

/** What a bootstrap endpoint answers: a Runtime configuration, and when to renew it. */
export interface Bootstrap {
  config: RuntimeConfig
  renew_at?: number
}

/** A start that did not produce an assistant. `status` is the server's, when it answered. */
export class BootFailure extends Error {
  constructor(message: string, readonly status: number | null) {
    super(message)
    this.name = 'BootFailure'
  }

  static from(error: unknown): BootFailure {
    if (error instanceof BootFailure) return error
    const status = (error as {status?: unknown} | null)?.status
    return new BootFailure(String((error as Error)?.message ?? error),
      typeof status === 'number' && status > 0 ? status : null)
  }

  /** Another attempt cannot fix it: the person has to sign in again or be granted access. */
  get final(): boolean {
    return this.status !== null && START_TEXT.final_statuses.includes(this.status)
  }
}

/** Where sessions come from. Only a renewable source can be asked for the next one. */
export interface SessionSource {
  readonly renewable: boolean
  obtain(): Promise<Bootstrap>
}

function isBootstrap(value: unknown): value is Bootstrap {
  const config = (value as {config?: unknown} | null)?.config
  return Boolean(config && typeof config === 'object'
    && typeof (config as RuntimeConfig).endpoint === 'string')
}

/**
 * Sessions from the host application's own endpoints, which turn the person's application
 * sign-in into an agent session. With `identity`, the application first issues a signed
 * assertion and the agent's bootstrap consumes it (Odoo); without, one endpoint does both.
 */
export function endpointSource({bootstrap, identity = null, instanceId,
                                fetchImpl = (...args) => globalThis.fetch(...args)}: {
  bootstrap: string
  identity?: string | null
  instanceId: string
  fetchImpl?: typeof fetch
}): SessionSource {
  const post = async (url: string, body: unknown): Promise<unknown> => {
    let response: Response
    try {
      response = await fetchImpl(url, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      })
    } catch (error) {
      throw new BootFailure(`${url} did not answer: ${String(error)}`, null)
    }
    if (!response.ok) throw new BootFailure(`${url} answered ${response.status}`, response.status)
    try {
      return await response.json()
    } catch {
      throw new BootFailure(`${url} answered something that is not JSON`, null)
    }
  }
  return {
    renewable: true,
    async obtain() {
      const request = {runtime_instance_id: instanceId}
      const payload = await post(bootstrap, identity ? await post(identity, request) : request)
      if (!isBootstrap(payload)) {
        throw new BootFailure('the bootstrap carried no Runtime configuration', null)
      }
      return payload
    },
  }
}

/** A session handed to the page whole, as a demo harness does; it cannot be renewed. */
export function inlineSource(config: RuntimeConfig): SessionSource {
  return {renewable: false, obtain: async () => ({config})}
}

/** The part of a Runtime the supervisor drives. `EmbeddedWebRuntime` is one. */
export interface Supervised {
  readonly stopped: boolean
  readonly endState: string
  canRenewSession(): boolean
  renewSession(config: RuntimeConfig): unknown
  stop(): void
  onStopped(listener: () => void): Cancel
}

export type BootState =
  | {kind: 'idle'}
  | {kind: 'starting'; attempt: number}
  | {kind: 'running'; runtime: Supervised}
  | {kind: 'retrying'; attempt: number; failure: BootFailure}
  | {kind: 'unavailable'; failure: BootFailure}
  /** Over, with the panel left as the Runtime left it; it is the one that says why. */
  | {kind: 'settled'; reason: 'replaced' | 'not_renewable'}

export interface SupervisorDeps {
  source: SessionSource
  start(config: RuntimeConfig): Promise<Supervised | null>
  status: StartStatus
  /**
   * Whether to make or keep a session without the person asking: while a turn is under way,
   * or for a session handed to the page whole. Otherwise a failed start waits for Retry and
   * a session that ends is not replaced until the person next sends something.
   */
  eager?(): boolean
  /** Told of every Runtime that comes up. */
  onRunning?(runtime: Supervised): void
}

export class Supervisor {
  private current: BootState = {kind: 'idle'}
  private pending: Cancel | null = null
  private renewal: Cancel | null = null
  private unwatch: Cancel | null = null
  private runningSince = 0
  /** Consecutive sessions that ended before they were stable: the rung of the ladder. */
  private flaps = 0

  constructor(private readonly deps: SupervisorDeps) {}

  get state(): BootState { return this.current }

  /** Begin, or begin again after a failure. Ignored while starting, running or settled. */
  start(attempt = 0): void {
    const kind = this.current.kind
    if (kind === 'starting' || kind === 'running' || kind === 'settled') return
    this.cancel('pending')
    // The panel decides what to show while this takes: nothing at first, then a skeleton.
    if (kind === 'idle') this.deps.status.connecting()
    this.current = {kind: 'starting', attempt}
    detach('start', this.attempt(attempt))
  }

  /** The person asked: start over from the bottom of the ladder. */
  retry(): void {
    this.flaps = 0
    this.start(0)
  }

  /** Something needs a session -- a message sent, a conversation opened: make one if none is up. */
  connect(): void {
    const kind = this.current.kind
    if (kind === 'idle' || kind === 'retrying' || kind === 'unavailable') this.retry()
  }

  /**
   * Let an idle session go. The Runtime stops without its session ending on the server, and
   * the panel stays as it is, ready for the next message to make a new one.
   */
  rest(): void {
    if (this.current.kind !== 'running') return
    const {runtime} = this.current
    if (!runtime.canRenewSession()) return
    this.current = {kind: 'idle'}
    this.cancel('renewal')
    this.cancel('unwatch')
    try { runtime.stop() } catch (error) { report('rest', error) }
    this.deps.status.rested()
  }

  private get eager(): boolean { return this.deps.eager?.() ?? true }

  private async attempt(attempt: number): Promise<void> {
    let runtime: Supervised | null
    let bootstrap: Bootstrap
    try {
      bootstrap = await this.deps.source.obtain()
      try {
        runtime = await this.deps.start(bootstrap.config)
      } catch (error) {
        if (!(error instanceof IdleHandshakeClosed) || !this.deps.source.renewable) throw error
        // The server may close its bounded runtime registry at hello. The failed
        // Runtime has been torn down; obtain a fresh capability once, without
        // sending or replaying a message, approval, action, or cached result.
        bootstrap = await this.deps.source.obtain()
        runtime = await this.deps.start(bootstrap.config)
      }
    } catch (error) {
      this.failed(BootFailure.from(error), attempt)
      return
    }
    // `null` means another Runtime holds the page. It is not one to watch or renew, and it
    // is not a reason to show nothing either: it is a failed start like any other.
    if (!runtime) {
      this.failed(new BootFailure('the assistant Runtime did not start', null), attempt)
      return
    }
    this.running(runtime, bootstrap)
  }

  private running(runtime: Supervised, bootstrap: Bootstrap): void {
    this.deps.status.started()
    this.current = {kind: 'running', runtime}
    this.runningSince = Date.now()
    this.unwatch = runtime.onStopped(() => this.stopped(runtime))
    this.scheduleRenewal(runtime, bootstrap)
    this.deps.onRunning?.(runtime)
  }

  private failed(failure: BootFailure, attempt: number): void {
    report('start', failure)
    // Authentication and authorization refusals need a new sign-in or grant. Everything
    // else keeps trying while a turn depends on it; a transient outage must not strand a turn
    // the person is not watching. With nothing under way, the person is the one who asked,
    // and Retry beside the reason is theirs to press.
    const retrying = !failure.final && this.eager
    this.current = retrying
      ? {kind: 'retrying', attempt: attempt + 1, failure}
      : {kind: 'unavailable', failure}
    this.deps.status.unavailable({status: failure.status, retrying}, () => this.retry())
    const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)] ?? 1000
    if (retrying) {
      this.pending = later('retry', delay, () => this.start(attempt + 1))
    }
  }

  private stopped(runtime: Supervised): void {
    if (this.current.kind !== 'running' || this.current.runtime !== runtime) return
    this.cancel('renewal')
    this.cancel('unwatch')
    if (runtime.endState === 'replaced') {
      this.current = {kind: 'settled', reason: 'replaced'}
      return
    }
    // An inline session cannot be asked for again; only a page restored from the
    // back-forward cache may reuse it, because the session itself never ended.
    if (!this.deps.source.renewable && runtime.endState !== 'suspended') {
      this.current = {kind: 'settled', reason: 'not_renewable'}
      return
    }
    try { runtime.stop() } catch (error) { report('stop', error) }
    const early = runtime.endState !== 'suspended'
      && Date.now() - this.runningSince < STABLE_MS
    if (early) {
      const rung = this.flaps
      this.flaps += 1
      this.failed(new BootFailure(
        `the assistant session ended (${runtime.endState || 'stopped'}) as soon as it started`,
        null), rung)
      return
    }
    this.flaps = 0
    this.current = {kind: 'idle'}
    // With a turn under way the page reconnects on its own; otherwise the next message does.
    if (this.eager) this.start(0)
    else this.deps.status.rested()
  }

  private scheduleRenewal(runtime: Supervised, bootstrap: Bootstrap, delay?: number): void {
    this.cancel('renewal')
    if (!this.deps.source.renewable) return
    if (delay === undefined && !bootstrap.renew_at) return
    const wait = delay ?? Math.max(1000, Number(bootstrap.renew_at) - Date.now())
    this.renewal = later('renewal', wait, async () => {
      const live = () => this.current.kind === 'running' && this.current.runtime === runtime
        && !runtime.stopped
      if (!live()) return
      if (!runtime.canRenewSession()) {
        this.scheduleRenewal(runtime, bootstrap, RENEW_RETRY_MS)
        return
      }
      try {
        const renewed = await this.deps.source.obtain()
        if (!live()) return
        runtime.renewSession(renewed.config)
        this.scheduleRenewal(runtime, renewed)
      } catch (error) {
        // The Runtime keeps its own expiry and ends when its identity does; a renewal that
        // failed is tried again rather than turned into anything the host would hear.
        report('renewal', error)
        if (live()) this.scheduleRenewal(runtime, bootstrap, RENEW_RETRY_MS)
      }
    })
  }

  private cancel(which: 'pending' | 'renewal' | 'unwatch'): void {
    this[which]?.()
    this[which] = null
  }
}

/** A stable id for this tab, so a reload rejoins its own session rather than starting another. */
export function runtimeInstanceId(): string {
  const fresh = () => crypto.randomUUID().replaceAll('-', '')
  try {
    const known = sessionStorage.getItem(INSTANCE_KEY)
    if (known) return known
    const id = fresh()
    sessionStorage.setItem(INSTANCE_KEY, id)
    return id
  } catch {
    return fresh()
  }
}

/** A URL the page declared, accepted only on the page's own origin. */
function sameOrigin(value: string | undefined): string | null {
  if (!value) return null
  const url = new URL(value, location.href)
  if (url.origin !== location.origin) {
    report('boot', new Error(`refusing a bootstrap endpoint on another origin: ${url.origin}`))
    return null
  }
  return url.href
}

/**
 * How this page asked for the assistant. A whole configuration a harness placed in
 * `window.__WOA_EMBED_CONFIG__` wins: it is a deliberate override, and the application page
 * underneath may well carry its own tag. Otherwise a `<script data-woa-bootstrap>` tag names
 * the application's endpoints, which must be on the page's own origin.
 */
export function sourceFromPage(doc: Document = document, win: Window = window): SessionSource | null {
  if (win.__WOA_EMBED_CONFIG__) return inlineSource(win.__WOA_EMBED_CONFIG__)
  const tag = doc.querySelector<HTMLScriptElement>('script[data-woa-bootstrap]')
  if (!tag) return null
  const bootstrap = sameOrigin(tag.dataset.woaBootstrap)
  const declaredIdentity = tag.dataset.woaIdentity
  const identity = declaredIdentity === undefined ? null : sameOrigin(declaredIdentity)
  if (!bootstrap || (declaredIdentity !== undefined && !identity)) return null
  return endpointSource({bootstrap, identity, instanceId: runtimeInstanceId()})
}

/**
 * Boot the assistant this page asked for, once per page. `window.__WOA_RUNTIME_READY__`
 * resolves with the first Runtime that comes up and is never rejected, so no outage of the
 * assistant can reach the host's `unhandledrejection` handler.
 *
 * The panel goes up at once; a session does not. One is made when the person sends
 * something, or opens a conversation this tab already has, or -- without anyone asking --
 * when a turn is under way (`engagement.ts`). A session handed to the page whole, as a demo
 * or a test does, is made at once and kept: whoever handed it over is driving it. A session
 * with nothing to do is let go after `KEEP_ALIVE_MS`.
 */
export function autoBoot(win: Window = window): Supervisor | null {
  if (win.top !== win || win.__WOA_ASSISTANT__) return win.__WOA_ASSISTANT__ ?? null
  const source = sourceFromPage(win.document, win)
  if (!source) return null
  let settle: (runtime: unknown) => void = () => {}
  win.__WOA_RUNTIME_READY__ = new Promise(resolve => { settle = resolve })
  const handedOver = !source.renewable
  const engagement = engagementRecord()
  const eager = () => handedOver || engagement.read() === 'active'
  // What the person sent while no session could take it: it goes to the first one that does.
  let waiting = ''
  // The panel's verbs while no Runtime is bound. Sending makes a session, and opening the
  // panel on a conversation this tab has makes one to show it; the rest wait for one.
  const shell = createShell({
    submit(text) {
      waiting = text
      shell.host.setOptimistic({turnId: '', text, failed: false})
      supervisor.connect()
    },
    panelVisibility(open) {
      if (open && engagement.read() !== 'none') supervisor.connect()
    },
    cancel() {}, decide() {}, newConversation() {}, loadOlder() {},
  }, DEFAULT_CONFIGURATION,
  examplesFromPage(win.document, DEFAULT_CONFIGURATION.limits.goal_max_chars))
  // Loaded ahead of the page -- as a harness injects it -- the tag that declares the examples
  // is not parsed yet; watch for it instead of guessing when it is likely there.
  watchForExamples(win.document, DEFAULT_CONFIGURATION.limits.goal_max_chars,
    examples => shell.host.offerExamples(examples))
  const supervisor: Supervisor = new Supervisor({
    source,
    start: async config => {
      const runtime = await startEmbeddedRuntime(config, shell)
      if (runtime) engagement.remember(config.sessionId)
      return runtime
    },
    status: panelStartStatus(shell.host),
    eager,
    onRunning: runtime => {
      settle(runtime)
      if (!waiting) return
      const text = waiting
      waiting = ''
      shell.host.submit(text)
    },
  })
  win.__WOA_ASSISTANT__ = supervisor
  // Keep the tab's record current, and let a session go once it has had nothing to do for a
  // while: every change the panel shows while it is idle starts the wait again.
  let resting: Cancel | null = null
  shell.host.subscribe(snapshot => {
    // Only a live session speaks for the turn. One that is ending says it is no longer busy,
    // which is not the same as the turn being over: the next session may have to resume it.
    const state = supervisor.state
    const running = state.kind === 'running' && !state.runtime.stopped
    engagement.note(snapshot, running)
    resting?.()
    resting = null
    if (!running || handedOver || engagement.read() === 'active') return
    resting = later('keep alive', KEEP_ALIVE_MS, () => {
      resting = null
      supervisor.rest()
    })
  })
  // The network coming back is the moment a failed start is worth trying again.
  listen('boot online', win, 'online', () => {
    const kind = supervisor.state.kind
    if (eager() && (kind === 'retrying' || kind === 'unavailable')) supervisor.retry()
  })
  if (!eager()) {
    // This identifier hints at history; the server still authenticates the current actor.
    // Keep the greeting hidden until that actor's timeline has arrived.
    if (!engagement.sessionId()) shell.host.rest()
    return supervisor
  }
  if (win.document.readyState === 'loading') {
    listen('boot', win.document, 'DOMContentLoaded', () => supervisor.start(), {once: true})
  } else {
    supervisor.start()
  }
  return supervisor
}
