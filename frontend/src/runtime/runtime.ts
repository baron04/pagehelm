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
import browserContract from '@contract/browser_contract.json'
import type {ApprovalRequest, PanelConfiguration, PanelState, ReconciliationStatus} from '../contract/panel'
import {postApprovalDecision, type ApprovalContext} from './approvalController'
import {contained, detach, listen, soon, type Cancel} from './contain'
import {
  mayNavigate, navigationExpectation, type NavigationExpectation,
} from './executor'
import {
  armSessionExpiry, canRenewRuntimeSession, expireSession, handleSessionLapse,
  performHello, renewRuntimeSession, type LeaseSyncContext,
} from './leaseSync'
import {
  Page, loadPageController, type ActiveViewReader, type ElementNavigationReader,
  type FacetApplier, type FacetCandidateReader, type PageBlockerReader,
  type PageControllerApi, type PageFacetReader, type PageHighlight, type PageOpener,
  type PageQueryReader, type PageRecordRouteReader, type PageRouteReader,
} from './page'
import type {ElementLookup} from './pageModel'
import type {PanelHost, TransportVerdict} from './panelHost'
import {createRedactor, type Redactor} from './redaction'
import type {PendingView} from './renderState'
import {createShell, type PanelShell} from './shell'
import {clearStored, loadSession, writeStored, type SessionState} from './storage'
import {IdleHandshakeClosed, SessionClient, TransportError} from './transport'
import {
  consumeEventStream, deliverResultEnvelope, loadOlderTimeline, postWithLeaseRecovery,
  raiseTransportError, refreshTimelineLoop, resumeDeferredResultLoop, retryCachedResultLoop,
  runEventLoop, sendResultEnvelope, type TransportClientContext, type WireEvent,
} from './transportClient'
import {
  bindAnswerOpener, cancelTurn, checkActionFreshness, clearPageHighlight, dispatchAction,
  executeActionCommand, executePageCommand, handleActionEnvelope, handleUserActed,
  handleWireEvent, observePage, openInApplication, prepareAction, reconcileRuntimeTurn,
  recoverActionAfterNavigation, settleAction, submitOrReplaceTurn, submitTurn,
  type DispatchOutcome, type Freshness, type TurnControllerContext,
} from './turnController'
import type {
  ActionCommand, ActionEnvelope, ActionResultEnvelope, JsonObject,
  ObservationEnvelope, RuntimeConfig,
} from './types'

// The build this Runtime is qualified against. It is stated once in
// `policies/page_controller.json`; `test_page_controller.py` fails if this string drifts.
const RUNTIME_NAME = 'woa-embedded/page-controller-1.12.4'
const CAPABILITIES = [
  'approval.details.v1',
  'dom.observe', 'dom.click', 'dom.fill', 'dom.select', 'dom.submit',
  'page.goto', 'page.back', 'page.scroll', 'page.wait', 'page.apply_facet',
  'page.highlight', 'page.handoff_form', 'page.reveal',
  browserContract.form_model.capability, browserContract.form_submit.capability,
  browserContract.form_handoff.scoped_capability,
]

/** Used where a Runtime was assembled without its constructor -- in conformance tests. */
const defaultRedactor = createRedactor()

function leaseContextOf(runtime: EmbeddedWebRuntime): LeaseSyncContext {
  return {
    get endpoint() { return runtime.endpoint },
    get sessionId() { return runtime.sessionId },
    get token() { return runtime.token },
    get applicationId() { return runtime.applicationId },
    get principalBinding() { return runtime.principalBinding },
    set principalBinding(v) { runtime.principalBinding = v },
    get runtimeInstanceId() { return runtime.runtimeInstanceId },
    get guardWindow() { return runtime.guardWindowValue() },
    get config() { return runtime.config },
    set config(v) { runtime.config = v },
    get leaseId() { return runtime.leaseId },
    set leaseId(v) { runtime.leaseId = v },
    get panelConfiguration() { return runtime.panelConfiguration },
    set panelConfiguration(v) { runtime.panelConfiguration = v },
    get stored() { return runtime.stored },
    get endState() { return runtime.endState },
    set endState(v) { runtime.endState = v },
    get stopped() { return runtime.stopped },
    set stopped(v) { runtime.stopped = v },
    get cancelExpiry() { return runtime.cancelExpiryHandle },
    set cancelExpiry(v) { runtime.cancelExpiryHandle = v },
    get eventAbortController() { return runtime.eventAbortController },
    get panelHost() { return runtime.panelHost },
    post: (path, body, options) => runtime._post(path, body, options),
    panelState: state => runtime._panelState(state),
    armExpiry: () => runtime._armExpiry(),
    expire: code => runtime._expire(code),
    lapsed: code => runtime._lapsed(code),
  }
}

function approvalContextOf(runtime: EmbeddedWebRuntime): ApprovalContext {
  return {
    get sessionId() { return runtime.sessionId },
    get stored() { return runtime.stored },
    get panelHost() { return runtime.panelHost },
    get panelConfiguration() { return runtime.panelConfiguration },
    save: () => runtime._save(),
    panelState: state => runtime._panelState(state),
    post: (path, body, options) => runtime._post(path, body, options),
    postApproval: (request, decision) => runtime._postApproval(request, decision),
  }
}

function transportContextOf(runtime: EmbeddedWebRuntime): TransportClientContext {
  return {
    get client() { return runtime.sessionClient() },
    get stored() { return runtime.stored },
    get panelHost() { return runtime.panelHost },
    get panelConfiguration() { return runtime.panelConfiguration },
    get panelCursor() { return runtime.panelCursor },
    set panelCursor(v) { runtime.panelCursor = v },
    get timelineRefresh() { return runtime.timelineRefreshHandle },
    set timelineRefresh(v) { runtime.timelineRefreshHandle = v },
    get currentObservation() { return runtime.currentObservation },
    set currentObservation(v) { runtime.currentObservation = v },
    get endState() { return runtime.endState },
    set endState(v) { runtime.endState = v },
    get stopped() { return runtime.stopped },
    set stopped(v) { runtime.stopped = v },
    get eventAbortController() { return runtime.eventAbortController },
    set eventAbortController(v) { runtime.eventAbortController = v },
    save: () => runtime._save(),
    panelState: state => runtime._panelState(state),
    lapsed: code => runtime._lapsed(code),
    raise: (error, fallback) => runtime._raise(error, fallback),
    post: (path, body, options) => runtime._post(path, body, options),
    timeline: query => runtime._timeline(query),
    refreshTimeline: () => runtime._refreshTimeline(),
    handleEvent: event => runtime._handleEvent(event),
    sendResult: result => runtime._sendResult(result),
    observe: (causedByActionId, signal) => runtime.observe(causedByActionId, signal),
    eventLoop: () => runtime._eventLoop(),
  }
}

function turnContextOf(runtime: EmbeddedWebRuntime): TurnControllerContext {
  return {
    ...approvalContextOf(runtime),
    get config() { return runtime.config },
    get documentId() { return runtime.documentId },
    get runtimeInstanceId() { return runtime.runtimeInstanceId },
    get redactor() { return runtime.redactor },
    get page() { return runtime.page },
    get openPage() { return runtime.openPage },
    get panelContainer() { return runtime.panelContainer },
    get beforeSubmit() { return runtime.beforeSubmit },
    get confirmBoxReader() { return runtime.confirmBoxReader },
    get confirmButtonPresser() { return runtime.confirmButtonPresser },
    get submitBusyReader() { return runtime.submitBusyReader },
    get restoreElementFormModel() { return runtime.restoreElementFormModel },
    get guardWindow() { return runtime.guardWindowValue() },
    get stopped() { return runtime.stopped },
    set stopped(v) { runtime.stopped = v },
    get endState() { return runtime.endState },
    set endState(v) { runtime.endState = v },
    get preparingSubmission() { return Boolean(runtime.preparingSubmission) },
    set preparingSubmission(v) { runtime.preparingSubmission = v },
    get preparingMessageId() { return String(runtime.preparingMessageId || '') },
    set preparingMessageId(v) { runtime.preparingMessageId = v },
    get submitAbortController() { return runtime.submitAbortController ?? null },
    set submitAbortController(v) { runtime.submitAbortController = v },
    get messagePostStarted() { return Boolean(runtime.messagePostStarted) },
    set messagePostStarted(v) { runtime.messagePostStarted = v },
    get replacementText() { return String(runtime.replacementText || '') },
    set replacementText(v) { runtime.replacementText = v },
    get writeGuardUntil() { return Number(runtime.writeGuardUntil || 0) },
    set writeGuardUntil(v) { runtime.writeGuardUntil = v },
    get highlightHandle() { return runtime.highlightHandle ?? null },
    set highlightHandle(v) { runtime.highlightHandle = v },
    get pendingView() { return runtime.pendingView ?? null },
    set pendingView(v) { runtime.pendingView = v },
    get currentObservation() { return runtime.currentObservation },
    set currentObservation(v) { runtime.currentObservation = v },
    get observationFingerprints() { return runtime.observationFingerprints },
    set observationFingerprints(v) { runtime.observationFingerprints = v },
    get observedUrl() { return runtime.observedUrl },
    set observedUrl(v) { runtime.observedUrl = v },
    raise: (error, fallback) => runtime._raise(error, fallback),
    lapsed: code => runtime._lapsed(code),
    observe: (causedByActionId, signal) => runtime.observe(causedByActionId, signal),
    submitTask: text => runtime.submitTask(text),
    cancelTask: options => runtime.cancelTask(options),
    actionFreshness: command => runtime._actionFreshness(command),
    mayNavigate: command => runtime._mayNavigate(command),
    navigationExpectation: command => runtime._navigationExpectation(command),
    prepare: (action, before, navigation) => runtime._prepare(action, before, navigation),
    execute: command => runtime._execute(command),
    executePageCommand: command => typeof runtime._executePageCommand === 'function'
      ? runtime._executePageCommand(command)
      : executePageCommand(turnContextOf(runtime), command),
    deliver: result => runtime._deliver(result),
    sendResult: result => runtime._sendResult(result),
    recoverAfterNavigation: observation => runtime._recoverAfterNavigation(observation),
    openInApplication: url => runtime._openInApplication(url),
  }
}

export class EmbeddedWebRuntime {
  /** The session host reauthenticates while idle, before any observation or message is sent. */
  beforeSubmit?: () => Promise<void>
  /** @internal */
  preparingSubmission = false
  /** @internal */
  preparingMessageId = ''
  /** @internal */
  submitAbortController: AbortController | null = null
  /** @internal */
  messagePostStarted = false
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
  /** @internal */
  pendingView: PendingView | null = null
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
  readPageRead: PageQueryReader | undefined
  readElementNavigation: ElementNavigationReader | undefined
  readElementPageControl: ElementNavigationReader | undefined
  readElementFormModel: ElementNavigationReader | undefined
  restoreElementFormModel: ((element: unknown, request: unknown) => Promise<boolean>) | undefined
  readElementRowRecord: ElementNavigationReader | undefined
  openPage: PageOpener | undefined
  /** The application's own form/confirm/settle readers; absent means none exist. */
  acceptReadSource: ((source: string, kind: 'form' | 'row') => boolean) | undefined
  confirmBoxReader: ((doc: Document) => unknown) | undefined
  confirmButtonPresser: ((label: string, doc: Document) => boolean) | undefined
  submitBusyReader: ((doc: Document) => boolean) | undefined
  panelEnabled = true
  panelContainer: HTMLDivElement | null = null
  panelHost: PanelHost | null = null
  panelConfiguration: PanelConfiguration | null = null
  panelCursor = ''
  private timelineRefresh: Promise<void> | null = null
  /** The shell this Runtime made for itself, when none was handed to it; it goes with it. */
  private ownShell: PanelShell | null = null
  /** Gives the panel's intents back to the shell. */
  private releasePanel: (() => void) | null = null
  private cancelExpiry: Cancel | null = null
  private stopListening: Cancel[] = []
  private redactorInstance: Redactor | null = null
  /** When the application's read-only window expires, in epoch milliseconds; 0 if none. */
  writeGuardUntil = 0
  /** @internal */
  highlightHandle: PageHighlight | null = null
  private clientInstance: SessionClient | null = null
  /** A message the person sent in place of an unanswered confirmation. */
  replacementText = ''
  /** The guard window name this page's assistant work carries; '' until resolved. */
  private guardWindow = ''

  constructor(config: RuntimeConfig, shell: PanelShell | null = null,
              private readonly hooks: RuntimeHooks = {}) {
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
      listen('user pointer', document, 'pointerdown', event => this._userActed(event), true),
      listen('user keyboard', document, 'keydown', event => this._userActed(event), true),
      listen('user wheel', document, 'wheel', event => this._userActed(event),
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
      () => false, this.readElementNavigation, this.readPageRead, this.readElementPageControl,
      this.panelContainer ?? undefined)
    page.formModelReader = this.readElementFormModel
    page.rowRecordReader = this.readElementRowRecord
    page.sourceAccepter = this.acceptReadSource
    this.heldPage = {controller: this.controller, lookup: this.getElementByIndex, page}
    return page
  }
  private heldPage: {controller: PageControllerApi; lookup: ElementLookup; page: Page} | undefined

  /** @internal */
  sessionClient(): SessionClient {
    return this.client
  }

  /** @internal */
  guardWindowValue(): string {
    return this.guardWindow
  }

  /** @internal */
  get cancelExpiryHandle(): Cancel | null { return this.cancelExpiry }
  set cancelExpiryHandle(value: Cancel | null) { this.cancelExpiry = value }

  /** @internal */
  get timelineRefreshHandle(): Promise<void> | null { return this.timelineRefresh }
  set timelineRefreshHandle(value: Promise<void> | null) { this.timelineRefresh = value }

  _save(): void { writeStored(this.sessionId, this.stored) }

  _panelState(state: PanelState): void { this.panelHost?.setConnection({state}) }

  _headers(extra: Record<string, string> = {}, includeLease = true): Record<string, string> {
    return this.client.headers(extra, includeLease)
  }

  _post<T = JsonObject>(path: string, body: unknown,
                        options: {includeLease?: boolean; signal?: AbortSignal} = {}): Promise<T> {
    return postWithLeaseRecovery<T>(this.client, path, body, options, () => this._hello())
  }

  /** One place where a failed request becomes a panel state and a stop-or-retry decision. */
  _raise(error: unknown, fallback = 'submit_failed'): TransportVerdict {
    return raiseTransportError(transportContextOf(this), error, fallback)
  }

  // -- starting ------------------------------------------------------------------------

  async start(): Promise<this> {
    const {controller, lookup, readPageFacets, readFacetCandidates, applyPageFacet,
      readPageRecordRoute, readPageBlocker, readActiveView, readPageRoute, readPageQuery,
      readElementNavigation, readPageRead, readElementPageControl, readElementFormModel, readElementRowRecord, restoreElementFormModel,
      openPage, acceptReadSource, readConfirmBox: confirmBoxReader, pressConfirmButton: confirmButtonPresser,
      readSubmitBusy: submitBusyReader} = await loadPageController(
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
    this.readPageRead = readPageRead
    this.readElementPageControl = readElementPageControl
    this.readElementFormModel = readElementFormModel
    this.restoreElementFormModel = restoreElementFormModel
    this.readElementRowRecord = readElementRowRecord
    this.openPage = openPage
    this.acceptReadSource = acceptReadSource
    this.confirmBoxReader = confirmBoxReader
    this.confirmButtonPresser = confirmButtonPresser
    this.submitBusyReader = submitBusyReader
    this.guardWindow = await this.hooks.windowToken?.() ?? ''
    return this._startConnectedRuntime()
  }

  async _startConnectedRuntime(): Promise<this> {
    try {
      await this._hello()
    } catch (error) {
      // A fresh document may be the one that exhausts the old session's bounded
      // runtime registry. Only an idle, pre-hello failure can ask the supervisor
      // for another session: never discard an in-flight task or cached receipt.
      if (error instanceof TransportError && error.status === 410
          && !this.stored.taskInFlight && !this.stored.activeMessageId
          && !this.stored.inFlight && !this.stored.cachedResult
          && !this.stored.cancellationPending && !Object.keys(this.stored.approvals).length) {
        throw new IdleHandshakeClosed(error.message, error.code, error.status)
      }
      throw error
    }
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
    return performHello(leaseContextOf(this), RUNTIME_NAME, CAPABILITIES)
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
        detach('new conversation', (async () => {
          // The page may start a fresh session entirely: this Runtime's session keeps
          // its history on the server, and the next message opens a new one.
          if (this.hooks.onNewConversation?.()) return
          await this._post('/conversation', {action: 'new'})
          this.panelHost?.clearTimeline()
          this.panelHost?.clearNotice()
          await this._refreshTimeline()
        })().catch(error => this._raise(error)))
      },
      loadOlder: before => {
        detach('load older', this._loadOlder(before).catch(error => this._raise(error)))
      },
      reconcile: (turnId, proposalDigest) => this.reconcileTurn(turnId, proposalDigest),
      openAnswerTarget: bindAnswerOpener(this.openPage, target => this._openInApplication(target)),
      panelVisibility: open => { if (!open) this._clearHighlight() },
    })
  }

  // -- how long this session may live ---------------------------------------------------

  _armExpiry(): void { armSessionExpiry(leaseContextOf(this)) }

  _expire(code: string): void { expireSession(leaseContextOf(this), code) }

  _lapsed(code: string): boolean { return handleSessionLapse(leaseContextOf(this), code) }

  canRenewSession(): boolean { return canRenewRuntimeSession(leaseContextOf(this)) }

  renewSession(config: RuntimeConfig): this {
    renewRuntimeSession(leaseContextOf(this), config)
    return this
  }

  // -- stopping --------------------------------------------------------------------------

  stop({removePanel = true}: {removePanel?: boolean} = {}): void {
    this._clearHighlight()
    this.stopped = true
    this.cancelExpiry?.()
    this.cancelExpiry = null
    this.submitAbortController?.abort()
    this.submitAbortController = null
    this.preparingSubmission = false
    this.preparingMessageId = ''
    this.messagePostStarted = false
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

  submitOrReplaceTask(text: string): Promise<JsonObject> {
    return submitOrReplaceTurn(turnContextOf(this), text)
  }

  submitTask(text: string): Promise<JsonObject> {
    return submitTurn(turnContextOf(this), text)
  }

  cancelTask(options: {retry?: boolean} = {}): Promise<void> {
    return cancelTurn(turnContextOf(this), options)
  }

  /** Trigger a read-only reconciliation check for an indeterminate write turn. Never retries writes. */
  reconcileTurn(turnId: string, proposalDigest?: string): Promise<ReconciliationStatus> {
    return reconcileRuntimeTurn(turnContextOf(this), turnId, proposalDigest)
  }

  /** Record the decision before dispatching it, so a reload replays rather than re-asks. */
  _postApproval(request: ApprovalRequest, decision: 'approved' | 'denied' | 'prefill'): Promise<void> {
    return postApprovalDecision(approvalContextOf(this), request, decision)
  }

  // -- the timeline the panel shows ------------------------------------------------------

  async _timeline(query: {after?: string; before?: string} = {}) {
    return this.client.timeline<Parameters<PanelHost['applyTimeline']>[0]>(query)
  }

  _refreshTimeline(): Promise<void> {
    return refreshTimelineLoop(transportContextOf(this))
  }

  _loadOlder(before: string): Promise<void> {
    return loadOlderTimeline(transportContextOf(this), before)
  }

  // -- the server's event stream ---------------------------------------------------------

  _eventLoop(): Promise<void> {
    return runEventLoop(transportContextOf(this))
  }

  _readEventStream(stream: ReadableStream<Uint8Array>): Promise<void> {
    return consumeEventStream(transportContextOf(this), stream)
  }

  _handleEvent(event: WireEvent): Promise<void> {
    return handleWireEvent(turnContextOf(this), event)
  }

  private _clearHighlight(): void {
    clearPageHighlight(turnContextOf(this))
  }

  _userActed(event: Event): void {
    handleUserActed(turnContextOf(this), event)
  }

  // -- observing the page ------------------------------------------------------------------

  observe(causedByActionId = '', signal?: AbortSignal): Promise<ObservationEnvelope> {
    return observePage(turnContextOf(this), causedByActionId, signal)
  }

  _actionFreshness(command: ActionCommand): Freshness {
    return checkActionFreshness(turnContextOf(this), command)
  }

  // -- one action, from the server's request to the result it counts -------------------

  _handleAction(action: ActionEnvelope): Promise<void> {
    return handleActionEnvelope(turnContextOf(this), action)
  }

  _prepare(action: ActionEnvelope, before: ObservationEnvelope,
           navigation: NavigationExpectation): Promise<ActionEnvelope> {
    return prepareAction(turnContextOf(this), action, before, navigation)
  }

  _dispatch(action: ActionEnvelope, executing: ActionEnvelope,
            navigation: NavigationExpectation): Promise<DispatchOutcome> {
    return dispatchAction(turnContextOf(this), action, executing, navigation)
  }

  _settle(executing: ActionEnvelope, before: ObservationEnvelope,
          outcome: DispatchOutcome): Promise<void> {
    return settleAction(turnContextOf(this), executing, before, outcome)
  }

  _execute(command: ActionCommand): Promise<unknown> {
    return executeActionCommand(turnContextOf(this), command)
  }

  /** @internal */
  _executePageCommand(command: ActionCommand): Promise<unknown> {
    return executePageCommand(turnContextOf(this), command)
  }

  _openInApplication(url: string): Promise<boolean> {
    return openInApplication(this.openPage, url)
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

  _deliver(result: ActionResultEnvelope): Promise<void> {
    return deliverResultEnvelope(transportContextOf(this), result)
  }

  _sendResult(result: ActionResultEnvelope): Promise<void> {
    return sendResultEnvelope(transportContextOf(this), result)
  }

  _recoverAfterNavigation(observation: ObservationEnvelope): Promise<void> {
    return recoverActionAfterNavigation(turnContextOf(this), observation)
  }

  _retryCachedResult(): Promise<boolean> {
    return retryCachedResultLoop(transportContextOf(this))
  }

  _resumeAfterDeferredResult(): Promise<void> {
    return resumeDeferredResultLoop(transportContextOf(this))
  }
}

/**
 * What the page around a Runtime supplies when it knows more than the Runtime does.
 *
 * `windowToken` names the page's window to the application's write guard, so two pages
 * of one person hold -- and end -- theirs independently. `onNewConversation` lets the
 * page switch to a fresh session entirely; it answers whether it did, and a `false`
 * falls back to asking the session to renew its conversation in place.
 */
interface RuntimeHooks {
  windowToken?: () => Promise<string>
  onNewConversation?: () => boolean
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
                                           shell: PanelShell | null = null,
                                           hooks: RuntimeHooks = {}): Promise<EmbeddedWebRuntime | null> {
  if (window.__WOA_EMBEDDED_RUNTIME_STARTED__) return null
  const runtime = new EmbeddedWebRuntime(config, shell, hooks)
  window.__WOA_EMBEDDED_RUNTIME_STARTED__ = runtime
  try {
    return await runtime.start()
  } catch (error) {
    contained('failed start', () => runtime.stop())()
    runtime._releasePage()
    throw error
  }
}
