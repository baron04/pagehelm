/**
 * Embedded Web Runtime for the server-owned Web Operation Agent: one session, one page.
 *
 * The browser owns DOM observation and generic actions only. Task history, planning,
 * source knowledge, model credentials and the control plan stay on the server. A result is
 * written to sessionStorage before it is posted, so a reconnect can resend it without
 * repeating the page action. The panel it mounts renders the server's own projection of
 * the timeline, which is why a full page navigation does not empty it.
 *
 * What survives across sessions -- starting one, noticing it ended, starting the next,
 * and saying so when that fails -- is not this class's job. That is `boot.ts`.
 *
 * Method names prefixed with `_` are this class's internal steps. Several are also the
 * seams the conformance tests reach through, so they stay methods on the instance.
 */
import {contained, detach, later, listen, sleep, soon, type Cancel} from './contain'
import browserContract from '@contract/browser_contract.json'
import {
  executeCommand, mayNavigate, navigationExpectation, waitForNavigation,
  type CommandOutcome, type NavigationExpectation,
} from './executor'
import {
  absoluteUrl, armChildFrameNavigation, canonicalUrl, navigationWal, recoveryProvesNavigation,
} from './navigation'
import {Page, loadPageController, formFieldMatches, type FormHandoffField,
  type FormSubmitTarget, type PageControllerApi, type PageFacetReader,
  type PageHighlight, type PageRecordRouteReader, type PageBlockerReader,
  type ActiveViewReader, type FacetCandidateReader, type FacetApplier,
  type PageRouteReader, type PageQueryReader, type ElementNavigationReader} from './page'
import type {ElementLookup} from './pageModel'
import {createShell, type PanelShell} from './shell'
import {DEFAULT_CONFIGURATION} from '../contract/panel'
import {backoffAfter, classifyTransportError, type PanelHost, type TransportVerdict} from './panelHost'
import {createRedactor, type Redactor} from './redaction'
import * as results from './results'
import {renderState, type PendingView} from './renderState'
import {clearStored, loadSession, writeStored, type SessionState} from './storage'
import {SessionClient, sseBatches} from './transport'
import {closeWriteGuard, openWriteGuard, releaseWriteGuard} from './writeGuard'
import type {ApprovalRequest, PanelConfiguration, PanelState} from '../contract/panel'
import {WIRE_VERSION, type ActionCommand, type ActionEnvelope, type ActionResultEnvelope,
  type JsonObject, type ObservationEnvelope, type RuntimeConfig} from './types'

// The build this Runtime is qualified against. It is stated once in
// `policies/page_controller.json`; `test_page_controller.py` fails if this string drifts.
const RUNTIME_NAME = 'woa-embedded/page-controller-1.12.4'
const CAPABILITIES = [
  'dom.observe', 'dom.click', 'dom.fill', 'dom.select', 'dom.submit',
  'page.goto', 'page.back', 'page.scroll', 'page.wait', 'page.apply_facet',
  'page.highlight', 'page.handoff_form', 'page.reveal',
  browserContract.form_handoff.scoped_capability,
]

/** Used where a Runtime was assembled without its constructor -- in conformance tests. */
const defaultRedactor = createRedactor()

/** One event on the server's stream. */
export interface WireEvent {
  kind: string
  payload: JsonObject
  turn_id?: string
}

interface Freshness { fresh: boolean; reason: string }

/** Commands that only look at the page: they leave it as the person has it. */
const PASSIVE_ACTIONS = new Set(['observe', 'wait_for', 'scroll'])

export class EmbeddedWebRuntime {
  config!: RuntimeConfig
  endpoint = ''
  sessionId = ''
  token = ''
  applicationId = ''
  principalBinding = ''
  documentId = ''
  runtimeInstanceId = ''
  leaseId = ''
  stored!: SessionState
  /**
   * Why this Runtime stopped, once it has: a terminal panel state from the server's table,
   * `ended` for a session that is over, or `suspended` for a page restored from the
   * back-forward cache. Whoever supervises the Runtime decides from this what comes next.
   */
  endState = ''
  private stoppedFlag = false
  private stopListeners: Set<() => void> | undefined
  currentObservation: ObservationEnvelope | null = null
  observationFingerprints = new Map<string, string>()
  observedUrl = ''
  private pendingView: PendingView | null = null
  eventAbortController: AbortController | null = null
  /** Set once PageController is loaded; conformance tests set both directly. */
  controller: PageControllerApi | null = null
  getElementByIndex: ElementLookup | null = null
  readPageFacets: PageFacetReader | undefined
  readFacetCandidates: FacetCandidateReader | undefined
  applyPageFacet: FacetApplier | undefined
  readPageRecordRoute: PageRecordRouteReader | undefined
  readPageBlocker: PageBlockerReader | undefined
  readActiveView: ActiveViewReader | undefined
  readPageRoute: PageRouteReader | undefined
  readPageQuery: PageQueryReader | undefined
  readElementNavigation: ElementNavigationReader | undefined
  panelEnabled = true
  panelContainer: HTMLDivElement | null = null
  panelHost: PanelHost | null = null
  panelConfiguration: PanelConfiguration | null = null
  panelCursor = ''
  /** The shell this Runtime made for itself, when none was handed to it; it goes with it. */
  private ownShell: PanelShell | null = null
  /** Gives the panel's intents back to the shell. */
  private releasePanel: (() => void) | null = null
  private cancelExpiry: Cancel | null = null
  private stopListening: Cancel[] = []
  private redactorInstance: Redactor | null = null
  /** When the application's read-only window expires, in epoch milliseconds; 0 if none. */
  private writeGuardUntil = 0
  private highlightHandle: PageHighlight | null = null
  private userIntervened = false
  private handoffGranted = false
  private clientInstance: SessionClient | null = null
  /** A message the person sent in place of an unanswered confirmation. */
  private replacementText = ''

  constructor(config: RuntimeConfig, shell: PanelShell | null = null) {
    if (!config?.endpoint || !config?.sessionId || !config?.clientToken) {
      throw new Error('endpoint, sessionId and clientToken are required')
    }
    this.config = config
    this.redactorInstance = createRedactor(config.sensitiveQueryKeys || [])
    this.endpoint = String(config.endpoint).replace(/\/$/, '')
    this.sessionId = String(config.sessionId)
    this.token = String(config.clientToken)
    this.applicationId = String(config.applicationId || '')
    this.principalBinding = String(config.principalBinding || '')
    if (Boolean(this.applicationId) !== Boolean(this.principalBinding)) {
      throw new Error('applicationId and principalBinding must be supplied together')
    }
    window.__WOA_DOCUMENT_ID__ ||= crypto.randomUUID()
    this.documentId = window.__WOA_DOCUMENT_ID__
    this.runtimeInstanceId = crypto.randomUUID()
    // The stable session id names replayable progress. A freshly issued capability names a
    // new execution incarnation: old cursors, actions and approvals must never cross it.
    this.stored = loadSession(this.sessionId, String(config.sessionGeneration || ''), this.redactor)
    this.panelEnabled = config.panel !== false
    if (this.panelEnabled && typeof document !== 'undefined') {
      // The page's shell when it has one; otherwise this Runtime's own. Either way it exists
      // before PageController, so the agent's view of the page can exclude it.
      if (!shell) this.ownShell = shell = createShell()
      this.panelContainer = shell.container
      this.panelHost = shell.host
    }
    this.stopListening = [
      listen('pageshow', window, 'pageshow', (event: PageTransitionEvent) => {
        if (!event?.persisted || this.stopped) return
        // A back-forward cache restore resumes a heap whose lease the intervening document
        // may have retired. This Runtime cannot vouch for it, so it stops; whoever supervises
        // it starts the next one in the same document -- reloading could resubmit a POST.
        this.endState = 'suspended'
        this.stop()
      }),
      listen('online', window, 'online', () => {
        this.panelHost?.setConnection({online: true})
        this.panelHost?.clearNotice()
      }),
      listen('offline', window, 'offline', () => {
        this.panelHost?.setConnection({online: false})
        this.panelHost?.raise('offline', {retryable: false})
      }),
      listen('user pointer', document, 'pointerdown', event => this._userTookControl(event), true),
      listen('user keyboard', document, 'keydown', event => this._userTookControl(event), true),
      listen('user wheel', document, 'wheel', event => this._userTookControl(event),
        {capture: true, passive: false}),
      listen('route change', window, 'popstate', () => this._clearHighlight()),
      listen('fragment change', window, 'hashchange', () => this._clearHighlight()),
    ]
  }

  // -- stopping, as others observe it -----------------------------------------------------

  /** Once true, never false again for a live Runtime; tests reset it on bare instances. */
  get stopped(): boolean { return Boolean(this.stoppedFlag) }

  set stopped(value: boolean) {
    const was = Boolean(this.stoppedFlag)
    this.stoppedFlag = value
    if (!value || was) return
    // A session the server will not speak for again has nothing left to recover with, and
    // its stored state can hold a full page observation -- say goodbye to it here, the one
    // place every stop passes. `suspended` and `replaced` keep theirs: a successor on this
    // very page reads it, which is the whole point of those two states.
    if (this.sessionId && this.endState
        && this.endState !== 'suspended' && this.endState !== 'replaced') {
      clearStored(this.sessionId)
    }
    // Told after the current task: the Runtime finishes stopping before anyone reacts.
    for (const listener of [...(this.stopListeners ?? [])]) soon('stop listener', listener)
  }

  /** Hear once when this Runtime stops, for whatever reason. */
  onStopped(listener: () => void): Cancel {
    const listeners = (this.stopListeners ??= new Set())
    listeners.add(listener)
    if (this.stopped) soon('stop listener', listener)
    return () => { listeners.delete(listener) }
  }

  // -- the pieces, reachable even on an instance assembled without its constructor ---------

  get redactor(): Redactor { return this.redactorInstance ?? defaultRedactor }

  private get client(): SessionClient {
    this.clientInstance ??= new SessionClient({
      endpoint: this.endpoint, sessionId: this.sessionId, token: this.token,
      lease: () => this.leaseId,
    })
    return this.clientInstance
  }

  /** The loaded page. Asking before PageController is loaded is a programming error. */
  get page(): Page {
    if (!this.controller || !this.getElementByIndex) {
      throw new Error('the page is not loaded yet')
    }
    if (this.heldPage?.controller === this.controller && this.heldPage.lookup === this.getElementByIndex) {
      return this.heldPage.page
    }
    const page = new Page(this.controller, this.getElementByIndex, this.redactor,
      this.readPageFacets, this.readFacetCandidates, this.applyPageFacet,
      this.readPageRecordRoute, this.readPageBlocker, this.readActiveView, this.readPageRoute, this.readPageQuery,
      () => this.userIntervened, this.readElementNavigation)
    this.heldPage = {controller: this.controller, lookup: this.getElementByIndex, page}
    return page
  }
  private heldPage: {controller: PageControllerApi; lookup: ElementLookup; page: Page} | undefined

  _save(): void { writeStored(this.sessionId, this.stored) }

  _panelState(state: PanelState): void { this.panelHost?.setConnection({state}) }

  _headers(extra: Record<string, string> = {}, includeLease = true): Record<string, string> {
    return this.client.headers(extra, includeLease)
  }

  _post<T = JsonObject>(path: string, body: unknown,
                        options: {includeLease?: boolean} = {}): Promise<T> {
    return this.client.post<T>(path, body, options)
  }

  /** One place where a failed request becomes a panel state and a stop-or-retry decision. */
  _raise(error: unknown, fallback = 'submit_failed'): TransportVerdict {
    const failure = error as {code?: unknown; status?: unknown} | null | undefined
    const verdict = classifyTransportError(failure, this.panelConfiguration)
    if (verdict.terminal && this._lapsed(verdict.code)) return verdict
    if (verdict.terminal) {
      this.endState = verdict.state
      this.stopped = true
      this.eventAbortController?.abort()
      this._panelState(verdict.state)
      this.panelHost?.raise(verdict.code, {retryable: false})
    } else {
      this._panelState(verdict.state)
      this.panelHost?.raise(String(failure?.code || fallback), {retryable: true})
    }
    return verdict
  }

  // -- starting ------------------------------------------------------------------------

  async start(): Promise<this> {
    const {controller, lookup, readPageFacets, readFacetCandidates, applyPageFacet,
      readPageRecordRoute, readPageBlocker, readActiveView, readPageRoute, readPageQuery,
      readElementNavigation} = await loadPageController(
      this.config.pageControllerModule, this.panelContainer ? [this.panelContainer] : [])
    this.controller = controller
    this.getElementByIndex = lookup
    this.readPageFacets = readPageFacets
    this.readFacetCandidates = readFacetCandidates
    this.applyPageFacet = applyPageFacet
    this.readPageRecordRoute = readPageRecordRoute
    this.readPageBlocker = readPageBlocker
    this.readActiveView = readActiveView
    this.readPageRoute = readPageRoute
    this.readPageQuery = readPageQuery
    this.readElementNavigation = readElementNavigation
    return this._startConnectedRuntime()
  }

  async _startConnectedRuntime(): Promise<this> {
    await this._hello()
    // The panel takes this session's intents as soon as the server has spoken, before the
    // steps below: a failed startup step then becomes a panel state and a notice instead of
    // an assistant that is simply not there.
    this._attachPanel()
    // A definite result cached before a reload must reach the server before a newer page
    // observation, or the older observation it carries would be rejected as stale and the
    // server could not tell whether the action ran.
    let resultDeferred = false
    try {
      if (this.stored.cachedResult) {
        try { await this._sendResult(this.stored.cachedResult) } catch { resultDeferred = true }
      }
      if (!resultDeferred) {
        const observation = await this.observe(this.stored.inFlight?.action_id || '')
        await this._post('/observations', observation)
        if (!this.stored.cachedResult) {
          try { await this._recoverAfterNavigation(observation) } catch (error) {
            // Recovery writes the outbox before posting. A transport failure after that
            // write is recoverable here; failures before a result exists stay startup errors.
            if (!this.stored.cachedResult) throw error
            resultDeferred = true
          }
        }
      }
    } catch (error) {
      this._raise(error, 'submit_failed')
      throw error
    }
    this.panelHost?.setConnection({
      busy: this.stored.taskInFlight,
      stopping: this.stored.cancellationPending,
      online: typeof navigator === 'undefined' || navigator.onLine !== false,
    })
    // A connected transport does not mean an empty conversation. Restore its history
    // before releasing the loading state; a failed fetch is not a new chat.
    try {
      await this._refreshTimeline()
    } catch (error) {
      this._raise(error, 'reconnecting')
      throw error
    }
    this._panelState(resultDeferred ? 'reconnecting'
      : (this.stored.cancellationPending ? 'stopping' : 'ready'))
    if (this.stored.cancellationPending) {
      detach('resume stop', this.cancelTask({retry: true}))
    }
    detach('event loop', resultDeferred ? this._resumeAfterDeferredResult() : this._eventLoop())
    return this
  }

  async _hello(): Promise<void> {
    const reply = await this._post<{lease_id?: string; panel?: PanelConfiguration}>('/hello', {
      schema_version: WIRE_VERSION,
      session_id: this.sessionId,
      client_id: this.stored.clientId,
      runtime_instance_id: this.runtimeInstanceId,
      runtime: RUNTIME_NAME,
      capabilities: CAPABILITIES,
      page_origin: location.origin,
      application_id: this.applicationId,
      principal_digest: this.principalBinding,
      last_event_id: this.stored.lastEventId,
    }, {includeLease: false})
    if (!reply.lease_id) throw new Error('hello response did not include a runtime lease')
    this.leaseId = String(reply.lease_id)
    if (!reply.panel || typeof reply.panel.protocol !== 'number') {
      throw new Error('hello response did not include the panel configuration')
    }
    this.panelConfiguration = reply.panel
    this._armExpiry()
  }

  _attachPanel(): void {
    if (!this.panelHost || this.releasePanel) return
    if (!this.panelConfiguration) throw new Error('panel host requires the server panel configuration')
    this.panelHost.adoptServerConfiguration(this.panelConfiguration)
    this.panelHost.setRetryStart(null)
    this.releasePanel = this.panelHost.bind({
      submit: text => { detach('submit', this.submitOrReplaceTask(text)) },
      cancel: () => { detach('cancel', this.cancelTask()) },
      decide: (approvalId, decision) => {
        const request = this.panelHost?.snapshot().pendingApproval
        if (!request || request.approval_id !== approvalId) return
        detach('decide', this._postApproval(request, decision).catch(error => this._raise(error)))
      },
      newConversation: () => {
        detach('new conversation', this._post('/conversation', {action: 'new'})
          .then(() => this._refreshTimeline())
          .catch(error => this._raise(error)))
      },
      loadOlder: before => {
        detach('load older', this._loadOlder(before).catch(error => this._raise(error)))
      },
      panelVisibility: open => { if (!open) this._clearHighlight() },
    })
  }

  // -- how long this session may live ---------------------------------------------------

  /**
   * Stop pretending a session is live past its own deadline: it ends, and says which
   * deadline it was, rather than looking like a network fault that reconnects forever.
   */
  _armExpiry(): void {
    this.cancelExpiry?.()
    this.cancelExpiry = null
    const deadlines = ([
      [Number(this.config.principalExpiresAt || 0)
        || Number(this.panelConfiguration?.principal_expires_at || 0), 'principal_expired'],
      [Number(this.config.sessionExpiresAt || 0)
        || Number(this.panelConfiguration?.session_expires_at || 0), 'session_expired'],
    ] as Array<[number, string]>).filter(([at]) => at > 0)
    const soonest = deadlines.reduce<[number, string] | null>(
      (best, next) => (best && best[0] <= next[0] ? best : next), null)
    if (!soonest) return
    const [at, code] = soonest
    const delay = at - Date.now()
    if (delay <= 0) {
      this._expire(code)
      return
    }
    this.cancelExpiry = later('session expiry', delay, () => this._expire(code))
  }

  _expire(code: string): void {
    if (this.stopped || this._lapsed(code)) return
    this.endState = 'ended'
    this.stopped = true
    this.eventAbortController?.abort()
    this._panelState('ended')
    this.panelHost?.raise(code, {retryable: false})
  }

  /**
   * End quietly when the session lapsed rather than failed -- its sign-in ran out while
   * nobody was using it, the page went quiet, the host restarted. The person's session comes
   * back as it was on their next message, so there is nothing to tell them.
   */
  _lapsed(code: string): boolean {
    const lapses = this.panelConfiguration?.lapse_codes ?? DEFAULT_CONFIGURATION.lapse_codes
    if (!lapses.includes(code)) return false
    if (!this.stopped) {
      this.endState = 'lapsed'
      this.stopped = true
      this.cancelExpiry?.()
      this.cancelExpiry = null
      this.eventAbortController?.abort()
    }
    return true
  }

  canRenewSession(): boolean {
    return !this.stopped && !this.stored.taskInFlight && !this.stored.inFlight
      && !this.panelHost?.snapshot().pendingApproval
  }

  renewSession(config: RuntimeConfig): this {
    if (!this.canRenewSession()) throw new Error('session renewal requires an idle runtime')
    const stable: Array<[string, string, string]> = [
      ['endpoint', this.endpoint, String(config?.endpoint || '').replace(/\/$/, '')],
      ['sessionId', this.sessionId, String(config?.sessionId || '')],
      ['clientToken', this.token, String(config?.clientToken || '')],
      ['sessionGeneration', String(this.config.sessionGeneration || ''),
        String(config?.sessionGeneration || '')],
      ['applicationId', this.applicationId, String(config?.applicationId || '')],
    ]
    const changed = stable.find(([, before, after]) => before !== after)
    if (changed) throw new Error(`session renewal changed ${changed[0]}`)
    const principalBinding = String(config?.principalBinding || '')
    const principalExpiresAt = Number(config?.principalExpiresAt || 0)
    const sessionExpiresAt = Number(config?.sessionExpiresAt || 0)
    if (!principalBinding || principalExpiresAt <= Date.now() || sessionExpiresAt <= Date.now()) {
      throw new Error('session renewal did not provide a live principal')
    }
    this.config = {...this.config, ...config}
    this.principalBinding = principalBinding
    if (this.panelConfiguration) {
      this.panelConfiguration = {
        ...this.panelConfiguration,
        principal_expires_at: principalExpiresAt,
        session_expires_at: sessionExpiresAt,
      }
    }
    this._armExpiry()
    return this
  }

  // -- stopping --------------------------------------------------------------------------

  stop({removePanel = true}: {removePanel?: boolean} = {}): void {
    this._clearHighlight()
    this.stopped = true
    this.cancelExpiry?.()
    this.cancelExpiry = null
    this.eventAbortController?.abort()
    this.eventAbortController = null
    for (const stop of this.stopListening?.splice(0) ?? []) stop()
    // Stopping twice -- once by the Runtime, once by its supervisor tearing it down -- must
    // not dispose PageController twice.
    const controller = this.controller
    this.controller = null
    this.heldPage = undefined
    controller?.dispose()
    if (removePanel) {
      // The panel's intents go back to the page's shell, which stays; a shell of this
      // Runtime's own goes with it.
      this.releasePanel?.()
      this.releasePanel = null
      this.ownShell?.destroy()
      this.ownShell = null
      this.panelHost = null
      this.panelConfiguration = null
      this.panelCursor = ''
      this.panelContainer = null
      // Stopped for good: the page is free for the next Runtime.
      this._releasePage()
    }
  }

  /** Give up this Runtime's claim on the page, and only its own. */
  _releasePage(): void {
    if (typeof window !== 'undefined' && window.__WOA_EMBEDDED_RUNTIME_STARTED__ === this) {
      window.__WOA_EMBEDDED_RUNTIME_STARTED__ = false
    }
  }

  // -- the person's intents ---------------------------------------------------------------

  async submitOrReplaceTask(text: string): Promise<JsonObject> {
    const goal = String(text || '').trim()
    if (!goal || goal.length > 2000) throw new Error('task must contain 1-2000 characters')
    if (!this.stored.taskInFlight) return this.submitTask(goal)

    // The composer exposes this only when an unanswered card arrived after the person had
    // already written a draft. Queue the new intent first, then cancel the old turn. The
    // terminal progress event drains the queue; the server therefore never has two active
    // messages, and the unanswered approval can never authorize the replacement.
    this.replacementText = goal
    try {
      await this.cancelTask()
    } catch (error) {
      this.replacementText = ''
      throw error
    }
    return {replacement_queued: true}
  }

  async submitTask(text: string): Promise<JsonObject> {
    if (this.stopped) throw new Error('embedded runtime is stopped')
    if (this.stored.taskInFlight) throw new Error('another task is still running')
    const goal = String(text || '').trim()
    if (!goal || goal.length > 2000) throw new Error('task must contain 1-2000 characters')
    this._clearHighlight()
    this.userIntervened = false
    this.handoffGranted = false
    const messageId = crypto.randomUUID()
    this.stored.taskInFlight = true
    this.stored.pageWorked = false
    this.stored.activeMessageId = messageId
    this.stored.cancellationPending = false
    this._save()
    // A completed cancellation belongs to the preceding message. Reset both flags at the
    // new-message boundary as well as at the terminal event, so a delayed projection
    // cannot leave the new stop control disabled.
    this.panelHost?.setConnection({busy: true, stopping: false})
    this._panelState('observing')
    // Echo the message now; the server's own `message_accepted` event replaces it.
    this.panelHost?.setOptimistic({turnId: messageId, text: goal, failed: false})
    try {
      // A first-party SPA can change route without replacing this Runtime. Bind every new
      // task to a fresh observation, so the server does not plan from a stale route.
      const observation = await this.observe()
      await this._post('/observations', observation)
      const response = await this._post('/messages', {
        message_id: messageId, text: this.redactor.redactString(goal),
        page_url: observation.observation.url,
      })
      this._panelState('queued')
      return response
    } catch (error) {
      this.stored.taskInFlight = false
      this.stored.activeMessageId = ''
      this.stored.cancellationPending = false
      this._save()
      this.panelHost?.setConnection({busy: false, stopping: false})
      this.panelHost?.failOptimistic()
      this._raise(error)
      throw error
    }
  }

  async cancelTask({retry = false}: {retry?: boolean} = {}): Promise<void> {
    if (this.stopped || !this.stored.taskInFlight || !this.stored.activeMessageId) return
    if (this.stored.cancellationPending && !retry) return
    this._panelState('stopping')
    this.stored.cancellationPending = true
    this._save()
    this.panelHost?.setConnection({stopping: true})
    try {
      await this._post('/cancellations', {
        schema_version: WIRE_VERSION,
        session_id: this.sessionId,
        message_id: this.stored.activeMessageId,
      })
    } catch (error) {
      this.stored.cancellationPending = false
      this._save()
      this.panelHost?.setConnection({stopping: false})
      this._panelState('error')
      this.panelHost?.raise('cancel_failed', {retryable: true})
      throw error
    }
  }

  /** Record the decision before dispatching it, so a reload replays rather than re-asks. */
  async _postApproval(request: ApprovalRequest, decision: 'approved' | 'denied'): Promise<void> {
    this.stored.approvals[request.approval_id] = decision
    this._save()
    try {
      await this._post('/approvals', {
        schema_version: WIRE_VERSION,
        session_id: this.sessionId,
        approval_id: request.approval_id,
        decision,
        principal_digest: String(request.principal_digest || ''),
        turn_id: String(request.turn_id || ''),
        operation_id: String(request.operation_id || ''),
      })
    } catch (error) {
      const verdict = classifyTransportError(
        error as {code?: unknown; status?: unknown}, this.panelConfiguration)
      if (!verdict.approvalClosed) throw error
      // Nothing can be answered here any more. Forget the decision -- replaying it after a
      // reload fails the same way -- take the card down, and say what happened. The
      // hand-written Runtime set the panel to `'idle'` here, a state the contract never had.
      delete this.stored.approvals[request.approval_id]
      this._save()
      this.panelHost?.setPendingApproval(null)
      this.panelHost?.raise(verdict.code, {retryable: false})
      this._panelState('ready')
      return
    }
    this.panelHost?.setPendingApproval(null)
    this._panelState(decision === 'approved' ? 'acting' : 'cancelled')
  }

  // -- the timeline the panel shows ------------------------------------------------------

  async _timeline(query: {after?: string; before?: string} = {}) {
    return this.client.timeline<Parameters<PanelHost['applyTimeline']>[0]>(query)
  }

  async _refreshTimeline(): Promise<void> {
    if (!this.panelHost) return
    const page = await this._timeline({after: this.panelCursor})
    this.panelCursor = String(page?.cursor || '') || this.panelCursor
    this.panelHost.applyTimeline(page)
  }

  async _loadOlder(before: string): Promise<void> {
    const host = this.panelHost
    if (!host) return
    try {
      host.applyTimeline(await this._timeline({before}), {prepend: true})
    } finally {
      host.setLoadingOlder(false)
    }
  }

  // -- the server's event stream ---------------------------------------------------------

  async _eventLoop(): Promise<void> {
    let backoff = 0
    while (!this.stopped) {
      const abortController = new AbortController()
      this.eventAbortController = abortController
      try {
        const stream = await this.client.events(this.stored.lastEventId, abortController.signal)
        backoff = 0
        this.panelHost?.clearNotice()
        await this._readEventStream(stream)
      } catch (error) {
        if (this.stopped) return
        if (this._raise(error, 'reconnecting').terminal) return
        backoff = backoffAfter(backoff)
        await sleep(backoff)
      } finally {
        if (this.eventAbortController === abortController) this.eventAbortController = null
      }
    }
  }

  async _readEventStream(stream: ReadableStream<Uint8Array>): Promise<void> {
    for await (const batch of sseBatches(stream, () => this.stopped)) {
      for (const frame of batch) {
        // Frames that were parsed before a stop may still sit in the current batch. Do not
        // handle them, and above all never `_save` after a terminal stop: the session's
        // storage was just cleared, and saving would resurrect what it said goodbye to.
        // Breaking (not returning) still lets the batch's final timeline fetch run -- that
        // is how the panel shows the last message of a session that ended mid-batch.
        if (this.stopped) break
        const event = JSON.parse(frame.data) as WireEvent
        await this._handleEvent(event)
        if (this.stopped) break
        this.stored.lastEventId = frame.id
        this._save()
        if (event.kind === 'action') {
          const actionId = event.payload.action_id
          if (this.stored.cachedResult?.action_id === actionId) this.stored.cachedResult = null
          if (this.stored.inFlight?.action_id === actionId) this.stored.inFlight = null
          this._save()
        }
      }
      // One projection fetch per delivered batch, not per event: the panel renders the
      // server's timeline, so it has to come from the server.
      await this._refreshTimeline().catch(() => {})
    }
  }

  async _handleEvent(event: WireEvent): Promise<void> {
    const turnId = String(event.turn_id || event.payload?.turn_id || this.stored.activeMessageId || '')
    switch (event.kind) {
      case 'action':
        await this._handleAction(event.payload as unknown as ActionEnvelope)
        return
      case 'progress': {
        const mapped = this.panelConfiguration?.progress_states?.[String(event.payload.state || '')]
        // A session may be open on several pages. While this page waits on a turn of its
        // own, another page's turn moving or ending is that page's, not this one's.
        const own = this.stored.activeMessageId
        if (own && turnId !== own) return
        if (mapped?.terminal) {
          this._finishTurn(turnId)
          // A confirmation whose turn is over can never be answered. Leaving it up is how a
          // person ends up pressing a dead button with the composer locked behind it.
          const stale = this.panelHost?.snapshot().pendingApproval
          if (stale && (!stale.turn_id || stale.turn_id === turnId)) {
            delete this.stored.approvals[stale.approval_id]
            this._save()
            this.panelHost?.setPendingApproval(null)
          }
        }
        // The panel state each progress state maps to is the server's table; an unmapped
        // state means the contract moved and must be noticed, not papered over here.
        this._panelState(mapped ? mapped.panel_state : 'acting')
        return
      }
      case 'terminal':
        if (this._lapsed(String(event.payload?.reason || ''))) return
        this._finishTurn(turnId)
        this._panelState('ended')
        // The terminal cursor is durable once this returns. A closed server session has no
        // future events, and another GET would only turn the final UI into a reconnect loop.
        this.endState = 'ended'
        this.stopped = true
        return
      case 'approval_required': {
        this._panelState('awaiting_confirmation')
        const request = event.payload as unknown as ApprovalRequest
        const decided = this.stored.approvals[request.approval_id] as 'approved' | 'denied' | undefined
        if (decided) {
          // A document that reloaded between deciding and posting finishes the POST rather
          // than asking again: the decision is already recorded and idempotent.
          await this._postApproval(request, decided)
          return
        }
        if (!this.panelHost) {
          // No panel means nobody can be asked, and an unanswered write must not proceed.
          await this._postApproval(request, 'denied')
          return
        }
        this.panelHost.setPendingApproval(request)
      }
    }
  }

  private _finishTurn(turnId: string): void {
    const guard = this.config?.pageWriteGuard
    if (guard) detach('close page write guard', closeWriteGuard(guard))
    this.stored.taskInFlight = false
    this.stored.activeMessageId = ''
    this.stored.cancellationPending = false
    this._save()
    this.panelHost?.setConnection({busy: false, stopping: false})
    this.panelHost?.clearOptimisticOnce(turnId)
    const replacement = this.replacementText
    this.replacementText = ''
    if (replacement && !this.stopped) {
      soon('submit replacement', () => {
        if (!this.stopped) detach('submit replacement', this.submitTask(replacement))
      })
    }
  }

  private _clearHighlight(): void {
    this.highlightHandle?.clear()
    this.highlightHandle = null
  }

  /** A person's real page gesture fences this turn before another command can dispatch. */
  _userTookControl(event: Event): void {
    if (!event.isTrusted) return
    if (event.composedPath().includes(this.panelContainer as EventTarget)) return
    this._clearHighlight()
    if (!this.stored?.taskInFlight || this.userIntervened) return
    // While the assistant only reads and thinks, the page is the person's: a click or a
    // scroll there takes nothing from the assistant, and stopping the answer for it -- and
    // swallowing the click -- was the bug. It is taking control only once the turn has
    // worked the page or a command is running on it; a later command is checked against
    // a fresh observation by the server either way.
    if (!this.stored.pageWorked && !this.stored.inFlight) return
    this.userIntervened = true
    if (this.handoffGranted) return
    // The first gesture stops the assistant, not the application. In particular it must
    // not reach an app whose read-only window is still closing and be replayed later.
    if (event.cancelable) event.preventDefault()
    event.stopImmediatePropagation()
    detach('user takeover', this.cancelTask().catch(() => {}))
  }

  // -- observing the page ------------------------------------------------------------------

  async observe(causedByActionId = ''): Promise<ObservationEnvelope> {
    const page = this.page
    const state = await page.read()
    const epoch = ++this.stored.epoch
    this._save()
    const reading = page.describe(state, epoch)
    const applicationState = page.readiness()
    const readiness = renderState(this.currentObservation?.observation ?? null,
      reading.observation, this.pendingView, applicationState.blocked, applicationState.pending)
    this.pendingView = readiness.pendingView
    reading.observation.render_state = readiness.state
    const envelope: ObservationEnvelope = {
      schema_version: WIRE_VERSION,
      session_id: this.sessionId,
      observation_id: `${this.stored.clientId}:${epoch}`,
      page_epoch: epoch,
      observation: reading.observation,
      events: [],
      caused_by_action_id: causedByActionId,
    }
    this.currentObservation = envelope
    this.observationFingerprints = reading.fingerprints
    this.observedUrl = reading.canonicalUrl
    return envelope
  }

  /**
   * Whether an action is still about the page the server planned it against: the same
   * route, and -- for anything that touches an element -- the same element, by everything
   * its meaning depends on.
   */
  _actionFreshness(command: ActionCommand): Freshness {
    if (command?.action === 'observe') return {fresh: true, reason: ''}
    const observedUrl = this.observedUrl || canonicalUrl(
      this.currentObservation?.observation?.url || location.href)
    if (canonicalUrl(location.href) !== observedUrl) {
      return {fresh: false, reason: 'the page URL changed after the last observation'}
    }
    if (command?.action === 'apply_facet') {
      const ref = command.args?.ref
      const expected = this.currentObservation?.observation?.facet_candidates
        ?.find(item => item.ref === ref)
      let actual: typeof expected
      try { actual = this.page.facetCandidate(ref) } catch { /* unavailable stays stale */ }
      if (!expected || !actual || expected.kind !== actual.kind ||
          expected.name !== actual.name || expected.active !== actual.active) {
        return {fresh: false, reason: 'the page facet changed after the last observation'}
      }
      return {fresh: true, reason: ''}
    }
    if (command?.action === 'handoff_form') {
      const fields = command.args?.fields
      if (!Array.isArray(fields) || !fields.length) {
        return {fresh: false, reason: 'the form handoff fields are invalid'}
      }
      for (const field of fields) {
        const ref = String((field as {ref?: unknown})?.ref || '')
        if (!ref) {
          if (!this.page.verifyFormFields([field], this.currentObservation?.observation.elements || [])) {
            return {fresh: false, reason: 'an entered form control changed or lost its current instance'}
          }
          continue
        }
        const expected = this.observationFingerprints.get(ref)
        if (expected === undefined || this.page.fingerprint(ref) !== expected) {
          return {fresh: false, reason: 'a form field changed after the last observation'}
        }
      }
      const submit = command.args?.submit as FormSubmitTarget | undefined
      if (submit) {
        const expected = this.observationFingerprints.get(submit.ref)
        if (expected === undefined || this.page.fingerprint(submit.ref) !== expected) {
          return {fresh: false, reason: 'the form submit control changed after observation'}
        }
      }
      return {fresh: true, reason: ''}
    }
    if (!['click', 'fill', 'select', 'submit', 'highlight', 'reveal'].includes(command?.action)) {
      return {fresh: true, reason: ''}
    }
    // Old constructed test shells have no map. Every real Runtime replaces this map during
    // observe(), before it can receive an action.
    if (!(this.observationFingerprints instanceof Map)) return {fresh: true, reason: ''}
    const ref = String(command?.args?.ref || '')
    const expected = this.observationFingerprints.get(ref)
    let actual = 'missing'
    try { actual = this.page.fingerprint(ref) } catch { /* missing stays fail-closed */ }
    if (expected === undefined || actual !== expected) {
      return {fresh: false, reason: 'the target element changed after the last observation'}
    }
    return {fresh: true, reason: ''}
  }

  // -- one action, from the server's request to the result it counts -------------------

  async _handleAction(action: ActionEnvelope): Promise<void> {
    if (await this._replayOrRecover(action)) return
    if (await this._rejectIfIntervened(action)) return
    if (await this._rejectIfNotCurrent(action)) return
    if (await this._rejectWithoutWriteGuard(action)) return
    if (await this._rejectIfIntervened(action)) return
    const before = this.currentObservation
    if (!before) return
    const navigation = this._navigationExpectation(action.command)
    const executing = await this._prepare(action, before, navigation)
    if (await this._rejectIfIntervened(action)) return
    if (!await this._claim(action, executing)) return
    if (await this._rejectIfIntervened(action)) return
    const outcome = await this._dispatch(action, executing, navigation)
    await this._settle(executing, before, outcome)
  }

  private async _rejectIfIntervened(action: ActionEnvelope): Promise<boolean> {
    if (!this.userIntervened) return false
    await this._reject(action, 'user_intervened',
      'The person took control of the page before this action was dispatched.')
    return true
  }

  /** A result already written for this action is resent; an interrupted one is recovered. */
  private async _replayOrRecover(action: ActionEnvelope): Promise<boolean> {
    if (this.stored.cachedResult?.action_id === action.action_id) {
      await this._sendResult(this.stored.cachedResult)
      return true
    }
    const inFlight = this.stored.inFlight
    if (inFlight?.action_id === action.action_id
        && (inFlight.phase !== 'prepared' || inFlight.runtime_instance_id !== this.runtimeInstanceId)) {
      const observation = await this.observe(action.action_id)
      await this._post('/observations', observation)
      await this._recoverAfterNavigation(observation)
      return true
    }
    return false
  }

  /** Refuse an action planned against a page that has since changed, or whose time is up. */
  private async _rejectIfNotCurrent(action: ActionEnvelope): Promise<boolean> {
    const current = this.currentObservation
    const expired = Date.now() > action.deadline
    if (!current || action.based_on_observation_id !== current.observation_id
        || action.page_epoch !== current.page_epoch || expired) {
      await this._reject(action, expired ? 'deadline_expired' : 'stale_observation',
        'Action was not executed because its page observation is no longer current.')
      return true
    }
    const freshness = this._actionFreshness(action.command)
    if (!freshness.fresh) {
      await this._reject(action, 'stale_observation',
        `Action was not executed because ${freshness.reason}.`)
      return true
    }
    return false
  }

  /**
   * An application that names a page write guard gets no page action without the guard
   * open: the action is refused, before anything is written ahead or claimed, rather than
   * run on a session that could still change data.
   */
  private async _rejectWithoutWriteGuard(action: ActionEnvelope): Promise<boolean> {
    const path = this.config?.pageWriteGuard
    if (path === undefined || path === '') return false
    this.writeGuardUntil = 0
    const expiresAt = await openWriteGuard(path)
    if (expiresAt !== null) {
      // Kept so the receipt can say whether the window still covered the action when it
      // settled. A window that lapsed mid-action proves nothing about what the action did.
      this.writeGuardUntil = expiresAt * 1000
      return false
    }
    await this._reject(action, 'capability_refused',
      'page_write_guard_unavailable: the application could not make this session read-only, '
      + 'so the action was not executed.')
    return true
  }

  private async _reject(action: ActionEnvelope, code: string, detail: string): Promise<void> {
    const observation = await this.observe(action.action_id)
    await this._deliver(results.rejected(action, observation, code, detail))
  }

  /**
   * Write the action ahead of running it: everything after this may destroy the JavaScript
   * context, and the next document recovers from exactly what is written here.
   */
  private async _prepare(action: ActionEnvelope, before: ObservationEnvelope,
                         navigation: NavigationExpectation): Promise<ActionEnvelope> {
    const prior = this.stored.inFlight?.action_id === action.action_id ? this.stored.inFlight : null
    const startedUrl = absoluteUrl(location.href)
    const wal = prior || await navigationWal(startedUrl, navigation.url, navigation.kind, this.redactor)
    const executing: ActionEnvelope = {...action, phase: 'prepared',
      started_at: prior?.started_at || Date.now(),
      started_url_raw: startedUrl,
      started_url_digest: wal.started_url_digest,
      started_url_display: prior?.started_url_display || before.observation.url,
      expected_url_digest: wal.expected_url_digest,
      expected_url_display: wal.expected_url_display,
      navigation_kind: wal.navigation_kind || navigation.kind,
      document_id: prior?.document_id || this.documentId,
      runtime_instance_id: this.runtimeInstanceId}
    if (!prior) {
      const durable: Partial<ActionEnvelope> = {...executing}
      delete durable.started_url_raw
      this.stored.inFlight = {
        ...durable,
        command: {...executing.command, args: this.redactor.safeArgs(executing.command.args)},
      } as ActionEnvelope
      this._save()
    }
    return executing
  }

  /** Take the action for this Runtime; one another Runtime took can no longer be vouched for. */
  private async _claim(action: ActionEnvelope, executing: ActionEnvelope): Promise<boolean> {
    const claim = await this._post<{claimed?: boolean}>('/claims', {action_id: action.action_id})
    if (!claim.claimed) {
      const observation = await this.observe(action.action_id)
      await this._post('/observations', observation)
      await this._deliver(results.indeterminate(action, observation,
        'Another runtime claimed this action; its execution state cannot be trusted after takeover.'))
      return false
    }
    executing.phase = 'claimed'
    if (this.stored.inFlight) this.stored.inFlight.phase = 'claimed'
    this._save()
    this._panelState('acting')
    return true
  }

  /** Run the command, and wait for the navigation it was expected to cause. */
  private async _dispatch(action: ActionEnvelope, executing: ActionEnvelope,
                          navigation: NavigationExpectation,
  ): Promise<{ok: boolean; code: string; detail: string}> {
    if (action.command.action === 'highlight' && this.config?.pageWriteGuard) {
      if (!await releaseWriteGuard(this.config.pageWriteGuard)) {
        return {ok: false, code: 'capability_refused',
          detail: 'the application did not acknowledge release of its read-only window'}
      }
      this.writeGuardUntil = 0
    }
    if (this.userIntervened) {
      return {ok: false, code: 'user_intervened',
        detail: 'the person took control before the command was dispatched'}
    }
    if (!PASSIVE_ACTIONS.has(action.command.action) && !this.stored.pageWorked) {
      this.stored.pageWorked = true
      this._save()
    }
    // A native submit can commit a child-frame navigation before execution returns. Arm the
    // stable iframe element first so a fast server response cannot escape observation.
    const signal = armChildFrameNavigation(navigation.frameElement)
    try {
      const result = await this._execute(action.command) as CommandOutcome | null
      if (result?.native_navigation_expected || this._mayNavigate(action.command)) {
        // What the expectation did not name is left to the wait's defaults: the top-level
        // window and document.
        const outcome = await waitForNavigation({
          urlBefore: navigation.startedUrl || String(executing.started_url_raw || ''),
          deadline: action.deadline, guardMs: this.config?.navigationGuardMs, signal,
          ...(navigation.context === undefined ? {} : {context: navigation.context}),
          ...(navigation.document === undefined ? {} : {documentBefore: navigation.document}),
          ...(navigation.frameElement === undefined ? {} : {frameElement: navigation.frameElement}),
        })
        if (!outcome.navigated) {
          throw new Error('navigation_timeout: expected navigation did not commit before deadline')
        }
      }
      const ok = result?.success !== false
      if (ok && action.command.action === 'handoff_form' && this.config?.pageWriteGuard) {
        if (!await releaseWriteGuard(this.config.pageWriteGuard)) {
          return {ok: false, code: 'capability_refused',
            detail: 'the application did not acknowledge release of its read-only window'}
        }
        this.writeGuardUntil = 0
      }
      return {
        ok,
        code: ok ? '' : 'element_not_actionable',
        detail: ok ? '' : results.safeErrorDetail(result?.message || '', action.command, this.redactor),
      }
    } catch (error) {
      const detail = results.safeErrorDetail(
        String((error as Error)?.message || error), action.command, this.redactor)
      const code = ['origin_refused', 'bad_arguments', 'navigation_timeout']
        .find(prefix => detail.startsWith(prefix)) ?? 'page_error'
      return {ok: false, code, detail}
    } finally {
      signal?.dispose()
    }
  }

  /** Observe what the action left behind and report it. */
  private async _settle(executing: ActionEnvelope, before: ObservationEnvelope,
                        outcome: {ok: boolean; code: string; detail: string}): Promise<void> {
    const observation = await this.observe(executing.action_id)
    if (executing.command.action === 'highlight' && outcome.ok &&
        (this.userIntervened || !this.highlightHandle?.active() ||
         before.observation.url !== observation.observation.url)) {
      this._clearHighlight()
      this.handoffGranted = false
      outcome = {ok: false, code: this.userIntervened ? 'user_intervened' : 'stale_observation',
        detail: 'the highlighted control was no longer current at handoff'}
    }
    if (executing.command.action === 'handoff_form' && outcome.ok) {
      const fields = executing.command.args?.fields
      const rebound = Array.isArray(fields) ? fields.map((field: unknown) => {
        const binding = field as FormHandoffField
        const matches = observation.observation.elements.filter(element =>
          formFieldMatches(element, binding))
        return matches.length === 1 ? {...binding, ref: matches[0]!.ref}
          : matches.length === 0 ? {...binding, ref: undefined} : null
      }) : []
      const submit = executing.command.args?.submit as FormSubmitTarget | undefined
      const buttons = submit ? observation.observation.elements.filter(element =>
        element.role === 'button' && element.name === submit.label &&
        element.attrs.form_scope === submit.scope) : []
      const submitReady = !submit || (buttons.length === 1 &&
        this.highlightHandle?.active() && this.page.verifyFormSubmit(
          {...submit, ref: buttons[0]!.ref}, rebound, observation.observation.elements))
      if (this.userIntervened || before.observation.url !== observation.observation.url ||
          before.observation.record_route !== observation.observation.record_route ||
          observation.observation.render_state !== 'ready' ||
          rebound.some(field => field === null) || !submitReady ||
          !this.page.verifyFormFields(rebound, observation.observation.elements)) {
        outcome = {ok: false, code: this.userIntervened ? 'user_intervened' : 'stale_observation',
          detail: 'the form fields were no longer ready at handoff'}
      } else {
        this.handoffGranted = true
      }
    }
    if (executing.command.action === 'handoff_form' && !outcome.ok) {
      this._clearHighlight()
      this.handoffGranted = false
    }
    // `navigated` is a change in the observed top-level URL. A child-frame commit satisfies
    // the action guard, and its new content travels in the fresh observation instead.
    const receipt = results.receipt(executing, {
      ok: outcome.ok, code: outcome.code, detail: outcome.detail, observation,
      navigated: before.observation.url !== observation.observation.url,
      elapsedMs: Date.now() - Number(executing.started_at || 0),
      urlBefore: this.currentObservation?.observation.url || '',
      writeGuardHeld: this.writeGuardUntil > Date.now(),
    }, this.redactor)
    await this._deliver(results.completed(executing, receipt, observation))
  }

  _execute(command: ActionCommand): Promise<unknown> {
    if (command.action === 'highlight') {
      this._clearHighlight()
      this.highlightHandle = this.page.highlight(command.args?.ref)
      this.handoffGranted = true
      return Promise.resolve({success: true, message: 'control highlighted for the person'})
    }
    if (command.action === 'handoff_form') {
      if (!this.currentObservation || !this.page.verifyFormFields(
        command.args?.fields, this.currentObservation.observation.elements)) {
        throw new Error('bad_arguments: form fields do not match the current page')
      }
      if (command.args?.submit) {
        if (!this.page.verifyFormSubmit(command.args.submit, command.args.fields,
          this.currentObservation.observation.elements)) {
          throw new Error('bad_arguments: form submit control does not match the current page')
        }
        this._clearHighlight()
        this.highlightHandle = this.page.highlight(command.args.submit.ref)
      }
      return Promise.resolve({success: true, message: 'form fields verified for handoff'})
    }
    if (command.action === 'apply_facet' && !this._actionFreshness(command).fresh) {
      throw new Error('bad_arguments: page facet changed before dispatch')
    }
    return executeCommand(command, () => this.page, this.config ?? {})
  }

  /** Whether `command` can leave the page. Kept as a method: it is a conformance seam. */
  _mayNavigate(command: ActionCommand): boolean {
    return mayNavigate(command, () => this.page)
  }

  /** Where `command` should take the page, decided before it runs. */
  _navigationExpectation(command: ActionCommand): NavigationExpectation {
    return navigationExpectation(command, () => this.page)
  }

  // -- results ---------------------------------------------------------------------------

  /** Cache a result durably, then send it: a crash in between resends, never re-executes. */
  private async _deliver(result: ActionResultEnvelope): Promise<void> {
    this.stored.cachedResult = result
    this._save()
    await this._sendResult(result)
  }

  async _sendResult(result: ActionResultEnvelope): Promise<void> {
    await this._post('/results', result)
    // Keep both records until the SSE cursor is durably advanced. A crash after the POST
    // response but before that write would otherwise replay and execute again.
    const after = result.observation_after
    if (after && (!this.currentObservation || after.page_epoch >= this.currentObservation.page_epoch)) {
      this.currentObservation = after
    }
    this._panelState('observing')
  }

  async _recoverAfterNavigation(observation: ObservationEnvelope): Promise<void> {
    const action = this.stored.inFlight
    if (!action || action.page_epoch >= observation.page_epoch) return
    let result: ActionResultEnvelope
    if (action.phase === 'prepared') {
      result = results.rejected(action, observation, 'runtime_replaced',
        'The previous runtime ended before the action was dispatched.')
    } else if (await recoveryProvesNavigation(action, location.href, this.documentId)) {
      const receipt = results.receipt(action, {
        ok: true, code: '', detail: '', observation,
        navigated: action.started_url_display !== observation.observation.url,
        elapsedMs: Date.now() - Number(action.started_at || 0),
        urlBefore: this.currentObservation?.observation.url || '',
      }, this.redactor)
      result = results.completed(action, receipt, observation)
    } else {
      result = results.indeterminate(action, observation,
        'The prior page ended without a trustworthy receipt; the action will not be retried.')
    }
    await this._deliver(result)
  }

  async _retryCachedResult(): Promise<boolean> {
    let backoff = 0
    while (!this.stopped && this.stored.cachedResult) {
      try {
        await this._sendResult(this.stored.cachedResult)
        return true
      } catch (error) {
        if (this._raise(error, 'reconnecting').terminal) return false
        await sleep(backoff)
        backoff = backoffAfter(backoff)
      }
    }
    return false
  }

  async _resumeAfterDeferredResult(): Promise<void> {
    if (!await this._retryCachedResult() || this.stopped) return
    // The cached result can describe the document that just navigated away. Refresh the
    // selector map and server observation before consuming another action here.
    let backoff = 0
    while (!this.stopped) {
      try {
        const observation = await this.observe(this.stored.inFlight?.action_id || '')
        await this._post('/observations', observation)
        this._panelState('ready')
        if (!this.stopped) detach('event loop', this._eventLoop())
        return
      } catch (error) {
        if (this._raise(error, 'reconnecting').terminal) return
        await sleep(backoff)
        backoff = backoffAfter(backoff)
      }
    }
  }
}

/**
 * Start the one Runtime this page may have, or return `null` if a live one already exists.
 *
 * The page-wide latch holds the Runtime that owns it, so the Runtime can release exactly
 * its own claim when it is stopped for good. A start that fails gives the panel back: the
 * page's shell stays and says why (`boot.ts`), and a shell of the Runtime's own goes with it,
 * since one left behind would be a second assistant root the next attempt cannot see.
 */
export async function startEmbeddedRuntime(config: RuntimeConfig,
                                           shell: PanelShell | null = null): Promise<EmbeddedWebRuntime | null> {
  if (window.__WOA_EMBEDDED_RUNTIME_STARTED__) return null
  const runtime = new EmbeddedWebRuntime(config, shell)
  window.__WOA_EMBEDDED_RUNTIME_STARTED__ = runtime
  try {
    return await runtime.start()
  } catch (error) {
    contained('failed start', () => runtime.stop())()
    runtime._releasePage()
    throw error
  }
}
