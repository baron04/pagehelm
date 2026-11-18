/**
 * Turn lifecycle, observation, action freshness/dispatch/settlement, and navigation recovery
 * for `EmbeddedWebRuntime`.
 */
import {activityPoint} from './activityPoint'
import {tryOpenAnswerInPlace} from './answerNavigation'
import {handleApprovalRequiredEvent, type ApprovalContext} from './approvalController'
import {ChoiceUnavailable} from './combobox'
import {detach, later, listen, sleep, soon} from './contain'
import {
  executeCommand, waitForNavigation,
  type CommandOutcome, type NavigationExpectation,
} from './executor'
import type {FormFillTracker} from './formFillTracker'
import {pressConfirmButton, readConfirmBox, sameControlText, waitForSubmitToSettle} from './formModel'
import {
  absoluteUrl, armChildFrameNavigation, canonicalUrl, documentNavigation, navigationWal,
  recoveryProvesNavigation,
} from './navigation'
import {
  FormClosedOrReadonlyError, formFieldMatches, type FormHandoffField, type FormSubmitTarget,
  type Page, type PageHighlight, type PageOpener,
} from './page'
import {summarizeObservation} from './pageSnapshot'
import {classifyTransportError, type TransportVerdict} from './panelHost'
import type {Redactor} from './redaction'
import {renderState, type PendingView} from './renderState'
import * as results from './results'
import {createAbortError, isAbortError, type WireEvent} from './transportClient'
import {
  WIRE_VERSION, type ActionCommand, type ActionEnvelope, type ActionResultEnvelope,
  type JsonObject, type ObservationEnvelope, type RuntimeConfig,
} from './types'
import {closeWriteGuard, openWriteGuard, releaseWriteGuard} from './writeGuard'
import type {ReconciliationStatus} from '../contract/panel'

export interface Freshness {
  fresh: boolean
  reason: string
}

/** Commands that only look at the page: they leave it as the person has it. */
const PASSIVE_ACTIONS = new Set(['observe', 'wait_for', 'scroll', 'form_model'])

/** How long an application is given to open one of its own pages before it is loaded. */
const APPLICATION_OPEN_MS = 6_000

function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise
  if (signal.aborted) return Promise.reject(createAbortError())
  return new Promise<T>((resolve, reject) => {
    const cancel = listen('submit abort', signal, 'abort', () => {
      reject(createAbortError())
    }, {once: true})
    promise.then(
      value => { cancel(); resolve(value) },
      error => { cancel(); reject(error) },
    )
  })
}

export interface TurnControllerContext extends ApprovalContext {
  readonly config: RuntimeConfig
  readonly documentId: string
  readonly runtimeInstanceId: string
  readonly redactor: Redactor
  readonly page: Page
  readonly openPage: PageOpener | undefined
  readonly panelContainer: HTMLDivElement | null
  readonly beforeSubmit?: (() => Promise<void>) | undefined
  readonly confirmBoxReader: ((doc: Document) => unknown) | undefined
  readonly confirmButtonPresser: ((label: string, doc: Document) => boolean) | undefined
  readonly submitBusyReader: ((doc: Document) => boolean) | undefined
  readonly restoreElementFormModel: ((element: unknown, request: unknown) => Promise<boolean>) | undefined
  readonly formFillTracker?: FormFillTracker | undefined
  readonly guardWindow: string
  stopped: boolean
  endState: string
  preparingSubmission: boolean
  preparingMessageId: string
  submitAbortController: AbortController | null
  messagePostStarted: boolean
  replacementText: string
  writeGuardUntil: number
  highlightHandle: PageHighlight | null
  pendingView: PendingView | null
  currentObservation: ObservationEnvelope | null
  observationFingerprints: Map<string, string>
  observedUrl: string
  raise(error: unknown, fallback?: string): TransportVerdict
  lapsed(code: string): boolean
  observe(causedByActionId?: string, signal?: AbortSignal): Promise<ObservationEnvelope>
  submitTask(text: string): Promise<JsonObject>
  cancelTask(options?: {retry?: boolean}): Promise<void>
  actionFreshness(command: ActionCommand): Freshness
  mayNavigate(command: ActionCommand): boolean
  navigationExpectation(command: ActionCommand): NavigationExpectation
  prepare(action: ActionEnvelope, before: ObservationEnvelope, navigation: NavigationExpectation): Promise<ActionEnvelope>
  execute(command: ActionCommand): Promise<unknown>
  executePageCommand(command: ActionCommand): Promise<unknown>
  deliver(result: ActionResultEnvelope): Promise<void>
  sendResult(result: ActionResultEnvelope): Promise<void>
  recoverAfterNavigation(observation: ObservationEnvelope): Promise<void>
  openInApplication(url: string): Promise<boolean>
}

export function clearPageHighlight(ctx: Pick<TurnControllerContext, 'highlightHandle'>): void {
  ctx.highlightHandle?.clear()
  ctx.highlightHandle = null
}

export async function submitOrReplaceTurn(ctx: TurnControllerContext, text: string): Promise<JsonObject> {
  const goal = String(text || '').trim()
  if (!goal || goal.length > 2000) throw new Error('task must contain 1-2000 characters')
  if (!ctx.stored.taskInFlight) return ctx.submitTask(goal)

  // The composer exposes this only when an unanswered card arrived after the person had
  // already written a draft. Queue the new intent first, then cancel the old turn. The
  // terminal progress event drains the queue; the server therefore never has two active
  // messages, and the unanswered approval can never authorize the replacement.
  ctx.replacementText = goal
  try {
    await ctx.cancelTask()
  } catch (error) {
    ctx.replacementText = ''
    throw error
  }
  return {replacement_queued: true}
}

function cleanupAbortedSubmit(ctx: TurnControllerContext, messageId: string): void {
  ctx.stored.taskInFlight = false
  if (ctx.stored.activeMessageId === messageId) ctx.stored.activeMessageId = ''
  ctx.stored.cancellationPending = false
  ctx.save()
  if (ctx.stopped) return
  ctx.panelHost?.clearOptimisticOnce(messageId)
  ctx.panelHost?.setConnection({busy: false, stopping: false, state: 'ready'})
}

export async function submitTurn(ctx: TurnControllerContext, text: string): Promise<JsonObject> {
  if (ctx.stopped) throw new Error('embedded runtime is stopped')
  if (ctx.stored.taskInFlight) throw new Error('another task is still running')
  if (ctx.preparingSubmission) throw new Error('a task is being prepared')
  const goal = String(text || '').trim()
  if (!goal || goal.length > 2000) throw new Error('task must contain 1-2000 characters')
  clearPageHighlight(ctx)
  const messageId = crypto.randomUUID()
  const abortController = new AbortController()
  ctx.preparingSubmission = true
  ctx.preparingMessageId = messageId
  ctx.submitAbortController = abortController
  ctx.messagePostStarted = false
  // Show the optimistic echo and observing/sending state synchronously before any renewal
  // or observation round-trip, while keeping stored.taskInFlight false so renewal stays allowed.
  ctx.panelHost?.setConnection({busy: true, stopping: false, state: 'observing'})
  ctx.panelHost?.setOptimistic({turnId: messageId, text: goal, failed: false})
  try {
    if (ctx.beforeSubmit) {
      await abortable(ctx.beforeSubmit(), abortController.signal)
    }
    if (abortController.signal.aborted) {
      cleanupAbortedSubmit(ctx, messageId)
      return {cancelled: true}
    }
    if (ctx.stopped || ctx.stored.taskInFlight) throw new Error('runtime is no longer idle')
  } catch (error) {
    if (ctx.submitAbortController === abortController) {
      ctx.preparingSubmission = false
      ctx.preparingMessageId = ''
    }
    if (abortController.signal.aborted || isAbortError(error)) {
      if (ctx.submitAbortController === abortController) {
        ctx.submitAbortController = null
        ctx.messagePostStarted = false
      }
      cleanupAbortedSubmit(ctx, messageId)
      return {cancelled: true}
    }
    if (ctx.submitAbortController === abortController) {
      ctx.submitAbortController = null
      ctx.messagePostStarted = false
    }
    ctx.panelHost?.setConnection({busy: false, stopping: false})
    if (ctx.panelHost?.snapshot().optimistic?.turnId === messageId) {
      ctx.panelHost.failOptimistic()
    } else {
      ctx.panelHost?.setOptimistic({turnId: messageId, text: goal, failed: true})
    }
    ctx.raise(error)
    throw error
  }
  if (ctx.submitAbortController === abortController) {
    ctx.preparingSubmission = false
    ctx.preparingMessageId = ''
  }
  ctx.stored.taskInFlight = true
  ctx.stored.pageWorked = false
  ctx.stored.activeMessageId = messageId
  ctx.stored.cancellationPending = false
  ctx.save()
  try {
    // A first-party SPA can change route without replacing this Runtime. Bind every new
    // task to a fresh observation, so the server does not plan from a stale route.
    const observation = await abortable(
      ctx.observe('', abortController.signal),
      abortController.signal,
    )
    if (abortController.signal.aborted) {
      if (!ctx.stored.cancellationPending) cleanupAbortedSubmit(ctx, messageId)
      return {cancelled: true}
    }
    await ctx.post('/observations', observation, {signal: abortController.signal})
    if (abortController.signal.aborted) {
      if (!ctx.stored.cancellationPending) cleanupAbortedSubmit(ctx, messageId)
      return {cancelled: true}
    }
    ctx.messagePostStarted = true
    const response = await ctx.post('/messages', {
      message_id: messageId, text: ctx.redactor.redactString(goal),
      page_url: observation.observation.url,
    }, {signal: abortController.signal})
    if (!ctx.stopped && !abortController.signal.aborted) {
      ctx.panelState('queued')
    }
    return response
  } catch (error) {
    if (abortController.signal.aborted || isAbortError(error)) {
      if (!ctx.stored.cancellationPending) cleanupAbortedSubmit(ctx, messageId)
      return {cancelled: true}
    }
    ctx.stored.taskInFlight = false
    ctx.stored.activeMessageId = ''
    ctx.stored.cancellationPending = false
    ctx.save()
    ctx.panelHost?.setConnection({busy: false, stopping: false})
    ctx.panelHost?.failOptimistic()
    ctx.raise(error)
    throw error
  } finally {
    if (ctx.submitAbortController === abortController) {
      ctx.submitAbortController = null
      ctx.messagePostStarted = false
    }
  }
}

export async function cancelTurn(ctx: TurnControllerContext, {retry = false}: {retry?: boolean} = {}): Promise<void> {
  if (ctx.stopped) return
  const controller = ctx.submitAbortController
  const messageWasDispatched = ctx.messagePostStarted
  const targetMessageId = ctx.stored.activeMessageId || ctx.preparingMessageId
  if (controller && !messageWasDispatched) {
    controller.abort()
    ctx.preparingSubmission = false
    ctx.preparingMessageId = ''
    ctx.submitAbortController = null
    ctx.messagePostStarted = false
    if (targetMessageId) cleanupAbortedSubmit(ctx, targetMessageId)
    return
  }
  if (!ctx.stored.taskInFlight || !ctx.stored.activeMessageId) return
  if (ctx.stored.cancellationPending && !retry) return
  const wasAborted = Boolean(controller)
  if (controller) controller.abort()
  const messageId = ctx.stored.activeMessageId
  ctx.panelState('stopping')
  ctx.stored.cancellationPending = true
  ctx.save()
  ctx.panelHost?.setConnection({stopping: true})
  try {
    const reply = await ctx.post<{pre_cancelled?: boolean}>('/cancellations', {
      schema_version: WIRE_VERSION,
      session_id: ctx.sessionId,
      message_id: messageId,
    })
    if (reply?.pre_cancelled) {
      cleanupAbortedSubmit(ctx, messageId)
    }
  } catch (error) {
    const verdict = classifyTransportError(
      error as {code?: unknown; status?: unknown}, ctx.panelConfiguration)
    if (verdict.terminal) {
      ctx.stored.cancellationPending = false
      ctx.save()
      ctx.panelHost?.setConnection({stopping: false})
      ctx.raise(error, 'cancel_failed')
      throw error
    }
    if (verdict.code === 'unknown_message' || (wasAborted && verdict.code !== 'submit_failed')) {
      cleanupAbortedSubmit(ctx, messageId)
      return
    }
    ctx.stored.cancellationPending = false
    ctx.save()
    ctx.panelHost?.setConnection({stopping: false})
    ctx.panelState('error')
    ctx.panelHost?.raise('cancel_failed', {retryable: true})
    throw error
  }
}

/** Trigger a read-only reconciliation check for an indeterminate write turn. Never retries writes. */
export async function reconcileRuntimeTurn(
  ctx: TurnControllerContext,
  turnId: string,
  proposalDigest?: string,
): Promise<ReconciliationStatus> {
  if (ctx.stopped) throw new Error('embedded runtime is stopped')
  const cleanTurnId = String(turnId || '').trim()
  const cleanDigest = String(proposalDigest || '').trim()
  if (!cleanTurnId && !cleanDigest) {
    throw new Error('reconciliation requires turnId or proposalDigest')
  }
  try {
    const raw = await ctx.post<Record<string, unknown>>('/reconciliations', {
      schema_version: WIRE_VERSION,
      session_id: ctx.sessionId,
      ...(cleanTurnId ? {turn_id: cleanTurnId} : {}),
      ...(cleanDigest ? {proposal_digest: cleanDigest} : {}),
    })
    const source = (raw?.reconciliation && typeof raw.reconciliation === 'object'
      ? raw.reconciliation : raw) as Record<string, unknown>
    const rawOutcome = source?.outcome
    const outcome: ReconciliationStatus['outcome'] =
      rawOutcome === 'completed' || rawOutcome === 'failed' || rawOutcome === 'unknown'
        ? rawOutcome : 'unknown'
    const settled = typeof source?.settled === 'boolean'
      ? source.settled : outcome !== 'unknown'
    const message = typeof source?.message === 'string' && source.message.trim()
      ? source.message.trim() : undefined
    const status: ReconciliationStatus = {
      outcome,
      settled,
      ...(message !== undefined ? {message} : {}),
    }
    ctx.panelHost?.setReconciliation(cleanTurnId, status, cleanDigest || undefined)
    return status
  } catch (error) {
    const verdict = classifyTransportError(
      error as {code?: unknown; status?: unknown}, ctx.panelConfiguration)
    const failure = error as {code?: unknown} | null | undefined
    if (failure?.code === 'unknown_reconciliation' && !verdict.terminal) {
      const status: ReconciliationStatus = {outcome: 'unknown', settled: false}
      ctx.panelHost?.setReconciliation(cleanTurnId, status, cleanDigest || undefined)
      return status
    }
    if (verdict.terminal) {
      ctx.raise(error)
    } else {
      ctx.panelHost?.raise(String(failure?.code || verdict.code || 'unexpected'), {retryable: false})
    }
    throw error
  }
}

function finishTurn(ctx: TurnControllerContext, turnId: string): void {
  const guard = ctx.config?.pageWriteGuard
  if (guard && ctx.writeGuardUntil > 0 && ctx.stored.activeMessageId === turnId) {
    ctx.writeGuardUntil = 0
    detach('close page write guard', closeWriteGuard(guard, fetch, ctx.guardWindow))
  }
  ctx.stored.taskInFlight = false
  ctx.stored.activeMessageId = ''
  ctx.stored.cancellationPending = false
  // A terminal receipt settles this turn's decisions too. Keeping consumed
  // approvals would make every later closed handshake look like live recovery.
  // Unfinished turns retain their decisions until the server settles them.
  ctx.stored.approvals = {}
  ctx.save()
  ctx.panelHost?.setConnection({busy: false, stopping: false})
  ctx.panelHost?.clearOptimisticOnce(turnId)
  const replacement = ctx.replacementText
  ctx.replacementText = ''
  if (replacement && !ctx.stopped) {
    soon('submit replacement', () => {
      if (!ctx.stopped) detach('submit replacement', ctx.submitTask(replacement))
    })
  }
}

export async function handleWireEvent(ctx: TurnControllerContext, event: WireEvent): Promise<void> {
  const turnId = String(event.turn_id || event.payload?.turn_id || ctx.stored.activeMessageId || '')
  switch (event.kind) {
    case 'action':
      await handleActionEnvelope(ctx, event.payload as unknown as ActionEnvelope)
      return
    case 'progress': {
      const mapped = ctx.panelConfiguration?.progress_states?.[String(event.payload.state || '')]
      // A session may be open on several pages. While this page waits on a turn of its
      // own, another page's turn moving or ending is that page's, not this one's.
      const own = ctx.stored.activeMessageId
      if (own && turnId !== own) return
      if (mapped?.terminal) {
        finishTurn(ctx, turnId)
        // A confirmation whose turn is over can never be answered. Leaving it up is how a
        // person ends up pressing a dead button with the composer locked behind it.
        const stale = ctx.panelHost?.snapshot().pendingApproval
        if (stale && (!stale.turn_id || stale.turn_id === turnId)) {
          delete ctx.stored.approvals[stale.approval_id]
          ctx.save()
          ctx.panelHost?.setPendingApproval(null)
        }
      }
      // The panel state each progress state maps to is the server's table; an unmapped
      // state means the contract moved and must be noticed, not papered over here.
      ctx.panelState(mapped ? mapped.panel_state : 'acting')
      return
    }
    case 'terminal':
      if (ctx.lapsed(String(event.payload?.reason || ''))) return
      finishTurn(ctx, turnId)
      ctx.panelState('ended')
      // The terminal cursor is durable once this returns. A closed server session has no
      // future events, and another GET would only turn the final UI into a reconnect loop.
      ctx.endState = 'ended'
      ctx.stopped = true
      return
    case 'approval_required':
      await handleApprovalRequiredEvent(ctx, event.payload)
  }
}

/**
 * A person's real gesture on the page is theirs: it is never held back, never swallowed
 * and never stops the assistant. Two things follow from it. The marker the assistant
 * left on a control is cleared; a form guide instead follows scrolling and review. The
 * read-only window the assistant opened for its own page work is closed at once, before
 * the application handles the gesture, so what the person does is not refused as if
 * the assistant had done it. The assistant opens
 * the window again before its next action, and the server checks that action against a
 * fresh observation either way.
 */
export function handleUserActed(ctx: TurnControllerContext, event: Event): void {
  if (!event.isTrusted) return
  if (event.composedPath().includes(ctx.panelContainer as EventTarget)) return
  if (!ctx.highlightHandle?.preserveOnInteraction) clearPageHighlight(ctx)
  const guard = ctx.config?.pageWriteGuard
  if (guard && ctx.writeGuardUntil > Date.now()) {
    ctx.writeGuardUntil = 0
    detach('close page write guard for the person', closeWriteGuard(guard, fetch, ctx.guardWindow))
  }
}

export async function observePage(
  ctx: TurnControllerContext,
  causedByActionId = '',
  signal?: AbortSignal,
): Promise<ObservationEnvelope> {
  if (signal?.aborted) throw createAbortError()
  const page = ctx.page
  const state = await abortable(page.read(), signal)
  if (signal?.aborted) throw createAbortError()
  const epoch = ++ctx.stored.epoch
  ctx.save()
  const reading = page.describe(state, epoch)
  const applicationState = page.readiness()
  const readiness = renderState(ctx.currentObservation?.observation ?? null,
    reading.observation, ctx.pendingView, applicationState.blocked, applicationState.pending)
  ctx.pendingView = readiness.pendingView
  reading.observation.render_state = readiness.state
  const envelope: ObservationEnvelope = {
    schema_version: WIRE_VERSION,
    session_id: ctx.sessionId,
    observation_id: `${ctx.stored.clientId}:${epoch}`,
    page_epoch: epoch,
    observation: reading.observation,
    events: [],
    caused_by_action_id: causedByActionId,
  }
  ctx.currentObservation = envelope
  ctx.observationFingerprints = reading.fingerprints
  ctx.observedUrl = reading.canonicalUrl
  return envelope
}

/**
 * Whether an action is still about the page the server planned it against: the same
 * route, and -- for anything that touches an element -- the same element, by everything
 * its meaning depends on.
 */
export function checkActionFreshness(ctx: TurnControllerContext, command: ActionCommand): Freshness {
  if (command?.action === 'observe') return {fresh: true, reason: ''}
  const observedUrl = ctx.observedUrl || canonicalUrl(
    ctx.currentObservation?.observation?.url || location.href)
  if (canonicalUrl(location.href) !== observedUrl) {
    return {fresh: false, reason: 'the page URL changed after the last observation'}
  }
  if (command?.action === 'apply_facet') {
    const ref = command.args?.ref
    const expected = ctx.currentObservation?.observation?.facet_candidates
      ?.find(item => item.ref === ref)
    let actual: typeof expected
    try { actual = ctx.page.facetCandidate(ref) } catch { /* unavailable stays stale */ }
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
        if (!ctx.page.verifyFormFields([field], ctx.currentObservation?.observation.elements || [])) {
          return {fresh: false, reason: 'an entered form control changed or lost its current instance'}
        }
        continue
      }
      const expected = ctx.observationFingerprints.get(ref)
      if (expected === undefined || ctx.page.fingerprint(ref) !== expected) {
        return {fresh: false, reason: 'a form field changed after the last observation'}
      }
    }
    const submit = command.args?.submit as FormSubmitTarget | undefined
    if (submit) {
      const expected = ctx.observationFingerprints.get(submit.ref)
      if (expected === undefined || ctx.page.fingerprint(submit.ref) !== expected) {
        return {fresh: false, reason: 'the form submit control changed after observation'}
      }
    }
    return {fresh: true, reason: ''}
  }
  if (!['click', 'fill', 'select', 'submit', 'highlight', 'reveal', 'form_model', 'form_submit']
      .includes(command?.action)) {
    return {fresh: true, reason: ''}
  }
  // Old constructed test shells have no map. Every real Runtime replaces this map during
  // observe(), before it can receive an action.
  if (!(ctx.observationFingerprints instanceof Map)) return {fresh: true, reason: ''}
  const ref = String(command?.args?.ref || '')
  const expected = ctx.observationFingerprints.get(ref)
  let actual = 'missing'
  try { actual = ctx.page.fingerprint(ref) } catch { /* missing stays fail-closed */ }
  if (expected === undefined || actual !== expected) {
    return {fresh: false, reason: 'the target element changed after the last observation'}
  }
  return {fresh: true, reason: ''}
}

export async function handleActionEnvelope(ctx: TurnControllerContext, action: ActionEnvelope): Promise<void> {
  if (await replayOrRecoverAction(ctx, action)) return
  if (await rejectIfNotCurrent(ctx, action)) return
  if (await rejectWithoutWriteGuard(ctx, action)) return
  const before = ctx.currentObservation
  if (!before) return
  const navigation = ctx.navigationExpectation(action.command)
  const executing = await ctx.prepare(action, before, navigation)
  if (!await claimAction(ctx, action, executing)) return
  const outcome = await dispatchAction(ctx, action, executing, navigation)
  await settleAction(ctx, executing, before, outcome)
}

/** A result already written for this action is resent; an interrupted one is recovered. */
async function replayOrRecoverAction(ctx: TurnControllerContext, action: ActionEnvelope): Promise<boolean> {
  if (ctx.stored.cachedResult?.action_id === action.action_id) {
    await ctx.sendResult(ctx.stored.cachedResult)
    return true
  }
  const inFlight = ctx.stored.inFlight
  if (inFlight?.action_id === action.action_id
      && (inFlight.phase !== 'prepared' || inFlight.runtime_instance_id !== ctx.runtimeInstanceId)) {
    const observation = await ctx.observe(action.action_id)
    await ctx.post('/observations', observation)
    await ctx.recoverAfterNavigation(observation)
    return true
  }
  return false
}

/** Refuse an action planned against a page that has since changed, or whose time is up. */
async function rejectIfNotCurrent(ctx: TurnControllerContext, action: ActionEnvelope): Promise<boolean> {
  const current = ctx.currentObservation
  const expired = Date.now() > action.deadline
  if (!current || action.based_on_observation_id !== current.observation_id
      || action.page_epoch !== current.page_epoch || expired) {
    await rejectAction(ctx, action, expired ? 'deadline_expired' : 'stale_observation',
      'Action was not executed because its page observation is no longer current.')
    return true
  }
  const freshness = ctx.actionFreshness(action.command)
  if (!freshness.fresh) {
    await rejectAction(ctx, action, 'stale_observation',
      `Action was not executed because ${freshness.reason}.`)
    return true
  }
  return false
}

/**
 * An application that names a page write guard gets no page interaction without the guard
 * open: the action is refused, before anything is written ahead or claimed, rather than
 * run on a session that could still change data.
 */
async function rejectWithoutWriteGuard(ctx: TurnControllerContext, action: ActionEnvelope): Promise<boolean> {
  // Observation only reads the controller's projection. Opening a write window for it
  // made a successful API query fail whenever that unrelated endpoint was unavailable.
  if (action.command.action === 'observe' || action.command.action === 'form_model') return false
  const path = ctx.config?.pageWriteGuard
  if (path === undefined || path === '') return false
  ctx.writeGuardUntil = 0
  const expiresAt = await openWriteGuard(path, fetch, ctx.guardWindow)
  if (expiresAt !== null) {
    // Kept so the receipt can say whether the window still covered the action when it
    // settled. A window that lapsed mid-action proves nothing about what the action did.
    ctx.writeGuardUntil = expiresAt * 1000
    return false
  }
  await rejectAction(ctx, action, 'capability_refused',
    'page_write_guard_unavailable: the application could not make this session read-only, '
    + 'so the action was not executed.')
  return true
}

async function rejectAction(
  ctx: TurnControllerContext,
  action: ActionEnvelope,
  code: string,
  detail: string,
): Promise<void> {
  const observation = await ctx.observe(action.action_id)
  await ctx.deliver(results.rejected(action, observation, code, detail))
}

/**
 * Write the action ahead of running it: everything after this may destroy the JavaScript
 * context, and the next document recovers from exactly what is written here.
 */
export async function prepareAction(
  ctx: TurnControllerContext,
  action: ActionEnvelope,
  before: ObservationEnvelope,
  navigation: NavigationExpectation,
): Promise<ActionEnvelope> {
  const prior = ctx.stored.inFlight?.action_id === action.action_id ? ctx.stored.inFlight : null
  const startedUrl = absoluteUrl(location.href)
  const wal = prior || await navigationWal(startedUrl, navigation.url, navigation.kind, ctx.redactor)
  const executing: ActionEnvelope = {...action, phase: 'prepared',
    started_at: prior?.started_at || Date.now(),
    started_url_raw: startedUrl,
    started_url_digest: wal.started_url_digest,
    started_url_display: prior?.started_url_display || before.observation.url,
    expected_url_digest: wal.expected_url_digest,
    expected_url_display: wal.expected_url_display,
    navigation_kind: wal.navigation_kind || navigation.kind,
    write_guard_path: ctx.config?.pageWriteGuard || '',
    write_guard_expires_at_ms: ctx.writeGuardUntil,
    document_id: prior?.document_id || ctx.documentId,
    runtime_instance_id: ctx.runtimeInstanceId}
  // Refresh a same-runtime prepared replay with the window confirmed before this dispatch.
  const durable: Partial<ActionEnvelope> = {...executing}
  delete durable.started_url_raw
  ctx.stored.inFlight = {
    ...durable,
    command: {...executing.command, args: ctx.redactor.safeArgs(executing.command.args)},
  } as ActionEnvelope
  ctx.save()
  return executing
}

/** Take the action for this Runtime; one another Runtime took can no longer be vouched for. */
async function claimAction(
  ctx: TurnControllerContext,
  action: ActionEnvelope,
  executing: ActionEnvelope,
): Promise<boolean> {
  const claim = await ctx.post<{claimed?: boolean}>('/claims', {action_id: action.action_id})
  if (!claim.claimed) {
    const observation = await ctx.observe(action.action_id)
    await ctx.post('/observations', observation)
    await ctx.deliver(results.indeterminate(action, observation,
      'Another runtime claimed this action; its execution state cannot be trusted after takeover.'))
    return false
  }
  executing.phase = 'claimed'
  if (ctx.stored.inFlight) ctx.stored.inFlight.phase = 'claimed'
  ctx.save()
  ctx.panelState('acting')
  return true
}

export interface DispatchOutcome {
  ok: boolean
  code: string
  detail: string
  candidate_labels?: string[]
  no_effect?: boolean
  execution_trace?: unknown
  form_data?: unknown
}

/** Run the command, and wait for the navigation it was expected to cause. */
export async function dispatchAction(
  ctx: TurnControllerContext,
  action: ActionEnvelope,
  executing: ActionEnvelope,
  navigation: NavigationExpectation,
): Promise<DispatchOutcome> {
  // Claiming is a network round trip. Neither the action deadline nor the application's
  // confirmed window may have lapsed while it was pending; reject before page dispatch.
  if (Date.now() >= action.deadline) {
    return {ok: false, code: 'deadline_expired',
      detail: 'the action deadline expired before page dispatch'}
  }
  // Reading the form data or the confirmation box changes nothing, so it needs no window either.
  if (action.command.action !== 'observe' && action.command.action !== 'form_model' && ctx.config?.pageWriteGuard
      && !(ctx.writeGuardUntil > Date.now())) {
    return {ok: false, code: 'capability_refused',
      detail: 'page_write_guard_unavailable: the confirmed window expired before page dispatch'}
  }
  if (action.command.action === 'highlight' && ctx.config?.pageWriteGuard) {
    if (!await releaseWriteGuard(ctx.config.pageWriteGuard, fetch, ctx.guardWindow)) {
      return {ok: false, code: 'capability_refused',
        detail: 'the application did not acknowledge release of its read-only window'}
    }
    ctx.writeGuardUntil = 0
  }
  if (!PASSIVE_ACTIONS.has(action.command.action) && !ctx.stored.pageWorked) {
    ctx.stored.pageWorked = true
    ctx.save()
  }
  // A native submit can commit a child-frame navigation before execution returns. Arm the
  // stable iframe element first so a fast server response cannot escape observation.
  const signal = armChildFrameNavigation(navigation.frameElement)
  const tracker = ctx.formFillTracker
  const turnId = executing.turn_id || ctx.stored?.activeMessageId || ''
  const isFieldAction = action.command.action === 'fill' || action.command.action === 'select'
  let targetElement: ReturnType<Page['element']> | undefined
  let beforeFieldValue: {value: string; checked?: boolean} | undefined
  let fieldKey = ''
  if (isFieldAction && tracker) {
    try {
      targetElement = ctx.page.element(action.command.args?.ref)
      fieldKey = tracker.resolveFieldKey(targetElement, String(action.command.args?.ref || ''))
      beforeFieldValue = tracker.captureCurrentValue(targetElement)
      if (targetElement) tracker.scanFormContainer(turnId, targetElement)
    } catch {
      // Missing/unloaded page fails in execution below.
    }
  }
  try {
    // The successor document must have started after this exact claimed command reached
    // dispatch. Persist before execution, which can destroy this JavaScript context.
    executing.dispatched_at = Date.now()
    if (ctx.stored.inFlight?.action_id === executing.action_id) {
      ctx.stored.inFlight.dispatched_at = executing.dispatched_at
    }
    ctx.save()
    const result = await ctx.execute(action.command) as CommandOutcome | null
    if (result?.native_navigation_expected || ctx.mayNavigate(action.command)) {
      // What the expectation did not name is left to the wait's defaults: the top-level
      // window and document.
      const outcome = await waitForNavigation({
        urlBefore: navigation.startedUrl || String(executing.started_url_raw || ''),
        deadline: action.deadline, guardMs: ctx.config?.navigationGuardMs, signal,
        ...(navigation.context === undefined ? {} : {context: navigation.context}),
        ...(navigation.document === undefined ? {} : {documentBefore: navigation.document}),
        ...(navigation.frameElement === undefined ? {} : {frameElement: navigation.frameElement}),
      })
      if (!outcome.navigated) {
        throw new Error('navigation_timeout: expected navigation did not commit before deadline')
      }
    }
    const ok = result?.success !== false
    if (isFieldAction && tracker && turnId && fieldKey) {
      if (ok) {
        const appliedValue = action.command.action === 'select'
          ? String(action.command.args?.option_label ?? action.command.args?.value ?? '')
          : tracker.captureCurrentValue(targetElement).value || String(action.command.args?.value ?? '')
        tracker.recordApplied(turnId, fieldKey, targetElement, beforeFieldValue ?? {value: ''}, appliedValue)
      } else {
        tracker.recordRejected(turnId, fieldKey, 'element_not_actionable', undefined, targetElement)
      }
    }
    const execution_trace = results.executionTraceOf(result, action.command.action)
    if (ok && action.command.action === 'handoff_form' && ctx.config?.pageWriteGuard) {
      if (!await releaseWriteGuard(ctx.config.pageWriteGuard, fetch, ctx.guardWindow)) {
        return {ok: false, code: 'capability_refused',
          detail: 'the application did not acknowledge release of its read-only window'}
      }
      ctx.writeGuardUntil = 0
    }
    if (action.command.action === 'form_submit') {
      await waitForSubmitToSettle(document, sleep,
        {readSubmitBusy: ctx.submitBusyReader, readConfirmBox: ctx.confirmBoxReader})
    }
    if (action.command.action === 'form_submit' && ctx.config?.pageWriteGuard) {
      // The permit covered this one click. Ending it is what returns the page to its
      // ordinary, read-only window; an application that does not confirm is reported.
      const released = await releaseWriteGuard(ctx.config.pageWriteGuard, fetch, ctx.guardWindow)
      ctx.writeGuardUntil = 0
      if (!released) {
        return {ok: false, code: 'capability_refused',
          detail: 'the application did not acknowledge the end of the approved click'}
      }
    }
    const formData = (result as {form_data?: unknown} | null)?.form_data
    return {
      ok,
      code: ok ? '' : 'element_not_actionable',
      detail: ok ? '' : results.safeErrorDetail(result?.message || '', action.command, ctx.redactor),
      ...(execution_trace ? {execution_trace} : {}),
      ...(formData ? {form_data: formData} : {}),
    }
  } catch (error) {
    if (error instanceof ChoiceUnavailable) {
      if (isFieldAction && tracker && turnId && fieldKey) {
        tracker.recordRejected(turnId, fieldKey, error.code, error.candidate_labels, targetElement)
      }
      return {ok: false, code: error.code, detail: error.message,
        no_effect: error.no_effect,
        candidate_labels: error.candidate_labels}
    }
    if (error instanceof FormClosedOrReadonlyError) {
      const detail = results.safeErrorDetail(error.message, action.command, ctx.redactor)
      if (isFieldAction && tracker && turnId && fieldKey) {
        tracker.recordRejected(turnId, fieldKey, error.code, undefined, targetElement)
      }
      return {ok: false, code: error.code, detail}
    }
    const detail = results.safeErrorDetail(
      String((error as Error)?.message || error), action.command, ctx.redactor)
    const code = [
      'origin_refused', 'bad_arguments', 'navigation_timeout', 'option_unavailable',
      'form_closed_or_readonly',
    ].find(prefix => detail.startsWith(prefix)) ?? 'page_error'
    if (isFieldAction && tracker && turnId && fieldKey) {
      tracker.recordRejected(turnId, fieldKey, code, undefined, targetElement)
    }
    return {ok: false, code, detail}
  } finally {
    signal?.dispose()
  }
}

/** Observe what the action left behind and report it. */
export async function settleAction(
  ctx: TurnControllerContext,
  executing: ActionEnvelope,
  before: ObservationEnvelope,
  outcome: DispatchOutcome,
): Promise<void> {
  const observation = await ctx.observe(executing.action_id)
  if (executing.command.action === 'highlight' && outcome.ok &&
      (!ctx.highlightHandle?.active() ||
       before.observation.url !== observation.observation.url)) {
    clearPageHighlight(ctx)
    outcome = {ok: false, code: 'stale_observation',
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
      ctx.highlightHandle?.active() && ctx.page.verifyFormSubmit(
        {...submit, ref: buttons[0]!.ref}, rebound, observation.observation.elements))
    if (before.observation.url !== observation.observation.url ||
        before.observation.record_route !== observation.observation.record_route ||
        observation.observation.render_state !== 'ready' ||
        rebound.some(field => field === null) || !submitReady ||
        !ctx.page.verifyFormFields(rebound, observation.observation.elements)) {
      let closedError: FormClosedOrReadonlyError | null = null
      try {
        ctx.page.ensureHandoffOpenAndWritable(fields, observation.observation.elements)
      } catch (err) {
        if (err instanceof FormClosedOrReadonlyError) closedError = err
      }
      outcome = closedError
        ? {ok: false, code: 'form_closed_or_readonly', detail: closedError.message}
        : {ok: false, code: 'stale_observation', detail: 'the form fields were no longer ready at handoff'}
    }
  }
  if (executing.command.action === 'handoff_form' && !outcome.ok) {
    clearPageHighlight(ctx)
  }
  const turnId = executing.turn_id || ctx.stored?.activeMessageId || ''
  const trackerFields = ['fill', 'select', 'handoff_form'].includes(executing.command.action)
    ? (ctx.formFillTracker?.receiptFields(turnId) ?? {})
    : {}
  const observationSummary = ['click', 'select', 'goto'].includes(executing.command.action)
    ? summarizeObservation(
      before.observation,
      observation.observation,
      ctx.redactor,
      typeof document !== 'undefined' ? document : null,
      ctx.panelContainer ?? undefined,
    )
    : undefined
  // `navigated` is a change in the observed top-level URL. A child-frame commit satisfies
  // the action guard, and its new content travels in the fresh observation instead.
  const receipt = results.receipt(executing, {
    ok: outcome.ok, code: outcome.code, detail: outcome.detail, observation,
    beforeObservation: before.observation,
    ...(observationSummary ? {observationSummary} : {}),
    ...trackerFields,
    navigated: before.observation.url !== observation.observation.url,
    elapsedMs: Date.now() - Number(executing.started_at || 0),
    urlBefore: ctx.currentObservation?.observation.url || '',
    writeGuardHeld: ctx.writeGuardUntil > Date.now(),
    ...(outcome.candidate_labels ? {candidateLabels: outcome.candidate_labels} : {}),
    ...(outcome.no_effect ? {noEffect: true} : {}),
    ...(outcome.execution_trace ? {execution_trace: outcome.execution_trace} : {}),
    ...(outcome.form_data ? {form_data: outcome.form_data} : {}),
  }, ctx.redactor)
  await ctx.deliver(results.completed(executing, receipt, observation))
}

export function executeActionCommand(ctx: TurnControllerContext, command: ActionCommand): Promise<unknown> {
  const showTarget = () => {
    if (!ctx.panelHost) return
    try {
      const ref = command.action === 'click' ? command.args?.ref : undefined
      ctx.panelHost.setActivityPoint(ref === undefined ? null
        : activityPoint(ctx.page.element(ref) as Element | null))
    } catch { /* A visual hint must never change a command's outcome. */ }
  }
  showTarget()
  // Keep synchronous validation failures synchronous. PageController can scroll the
  // target into view during execution, so refresh geometry when its promise settles.
  return ctx.executePageCommand(command).finally(showTarget)
}

export function executePageCommand(ctx: TurnControllerContext, command: ActionCommand): Promise<unknown> {
  if (command.action === 'highlight') {
    clearPageHighlight(ctx)
    ctx.highlightHandle = ctx.page.highlight(command.args?.ref,
      ctx.stored?.inFlight?.turn_id ?? ctx.stored?.activeMessageId ?? '')
    return Promise.resolve({success: true, message: 'control highlighted for the person'})
  }
  if (command.action === 'handoff_form') {
    if (ctx.currentObservation) {
      ctx.page.ensureHandoffOpenAndWritable(
        command.args?.fields, ctx.currentObservation.observation.elements)
    }
    if (!ctx.currentObservation || !ctx.page.verifyFormFields(
      command.args?.fields, ctx.currentObservation.observation.elements)) {
      throw new Error('bad_arguments: form fields do not match the current page')
    }
    if (command.args?.submit) {
      if (!ctx.page.verifyFormSubmit(command.args.submit, command.args.fields,
        ctx.currentObservation.observation.elements)) {
        throw new Error('bad_arguments: form submit control does not match the current page')
      }
      clearPageHighlight(ctx)
      ctx.highlightHandle = ctx.page.guideSubmit(command.args.submit.ref,
        ctx.stored?.inFlight?.turn_id ?? ctx.stored?.activeMessageId ?? '')
    }
    const turnId = ctx.stored?.inFlight?.turn_id ?? ctx.stored?.activeMessageId ?? ''
    if (ctx.formFillTracker && turnId && Array.isArray(command.args?.fields)) {
      for (const rawField of command.args.fields) {
        const ref = (rawField as FormHandoffField | null)?.ref
        if (!ref) continue
        try {
          const el = ctx.page.element(ref)
          if (el) {
            ctx.formFillTracker.scanFormContainer(turnId, el)
            const key = ctx.formFillTracker.resolveFieldKey(el, ref)
            const cur = ctx.formFillTracker.captureCurrentValue(el)
            ctx.formFillTracker.recordApplied(turnId, key, el, cur, cur.value)
          }
        } catch {
          // Ignore stale ref during optional restricted-control scan.
        }
      }
      ctx.formFillTracker.focusFirstRestricted(turnId)
    }
    return Promise.resolve({success: true, message: 'form fields verified for handoff'})
  }
  if (command.action === 'form_model') {
    // Kind `row` reads the table row a control belongs to, `confirm` the application's
    // own confirmation box; anything else reads the form.
    if (command.args?.kind === 'confirm') {
      if (!ctx.confirmBoxReader) {
        return Promise.resolve({success: false, message: 'confirmation box reading unavailable'})
      }
      return Promise.resolve({success: true,
        form_data: readConfirmBox(document, {readConfirmBox: ctx.confirmBoxReader}),
        message: 'confirmation box read'})
    }
    if (command.args?.kind === 'restore' || command.args?.kind === 'validate') {
      const element = ctx.page.element(command.args?.ref)
      if (!ctx.restoreElementFormModel) throw new Error('bad_arguments: form restoration unavailable')
      const validating = command.args?.kind === 'validate'
      const request = validating ? {expected: ctx.page.formModel(command.args?.ref)?.model, values: {}} : command.args
      return ctx.restoreElementFormModel(element, request).then(ok => {
        if (!ok && !validating) throw new Error('bad_arguments: form restoration or validation failed')
        const form = ctx.page.formModel(command.args?.ref)
        if (!form) throw new Error('bad_arguments: restored form unreadable')
        return {success: true, form_data: {...form, validation: ok}, message: validating ? 'application validation checked' : 'page opening changes restored'}
      })
    }
    if (command.args?.kind === 'row') {
      const row = ctx.page.rowRecord(command.args?.ref)
      if (!row) throw new Error('bad_arguments: the table row is not readable')
      return Promise.resolve({success: true, form_data: row, message: 'row record read'})
    }
    const form = ctx.page.formModel(command.args?.ref)
    if (!form) throw new Error(`bad_arguments: the form data is not readable (${ctx.page.formUnreadable || 'unknown'})`)
    return Promise.resolve({success: true, form_data: form, message: 'form data read'})
  }
  if (command.action === 'form_submit') {
    // The one click an approved card allows. What was approved must still be what is
    // on the page: the same form data, or the same table row.
    if (command.args?.kind === 'confirm') {
      if (!pressConfirmButton(String(command.args?.label ?? ''), document,
          {pressConfirmButton: ctx.confirmButtonPresser})) {
        throw new Error('bad_arguments: the confirmation box does not offer that button')
      }
      return sleep(300).then(() => ({success: true, message: 'confirmation button pressed'}))
    }
    // The control itself must still be the one the card named, by its own text.
    const target = ctx.page.element(command.args?.ref) as {textContent?: string | null} | null | undefined
    if (!sameControlText(command.args?.label, target?.textContent)) {
      throw new Error('bad_arguments: the control is no longer the one that was approved')
    }
    if (command.args?.kind === 'row') {
      const row = ctx.page.rowRecord(command.args?.ref)
      if (row?.record_id !== String(command.args?.record_id ?? '') || row?.check !== command.args?.row_check) {
        throw new Error('bad_arguments: the table row changed after approval')
      }
    } else if (ctx.page.formModel(command.args?.ref)?.check !== String(command.args?.check ?? '')) {
      throw new Error('bad_arguments: the form data changed after approval')
    }
    return executeCommand({...command, action: 'click'}, () => ctx.page, ctx.config ?? {},
      ctx.openPage ? url => ctx.openInApplication(url) : undefined)
  }
  if (command.action === 'apply_facet' && !ctx.actionFreshness(command).fresh) {
    throw new Error('bad_arguments: page facet changed before dispatch')
  }
  const agentEndpoint = ctx.config?.endpoint ? String(ctx.config.endpoint).replace(/\/$/, '') : ''
  return executeCommand(command, () => ctx.page, ctx.config ?? {},
    ctx.openPage ? url => ctx.openInApplication(url) : undefined, {
      panelContainer: ctx.panelContainer,
      isBusy: () => {
        try { return ctx.page.readiness().pending } catch { return false }
      },
      ignoreUrl: (url: string) => Boolean(agentEndpoint && url.startsWith(agentEndpoint)) ||
        /\/woa\/|\/events(?:$|\?)/.test(url),
    })
}

/**
 * Ask the application to open one of its own pages in this document. An application
 * that throws, answers anything but true, or does not answer in time has not opened it:
 * the page is then loaded, which is right whatever the application went on to do.
 */
export async function openInApplication(opener: PageOpener | undefined, url: string): Promise<boolean> {
  if (!opener) return false
  let stopWaiting: (() => void) | undefined
  try {
    // The losing side of the race must not leave its timer running: a cancelled wait
    // does not hold the host's event loop (or a Node VM contract) for the full timeout.
    return await new Promise<boolean>(resolve => {
      stopWaiting = later('application open', APPLICATION_OPEN_MS, () => resolve(false))
      Promise.resolve(opener(url)).then(
        opened => resolve(opened === true),
        () => resolve(false))
    })
  } catch {
    return false
  } finally {
    stopWaiting?.()
  }
}

export async function recoverActionAfterNavigation(
  ctx: TurnControllerContext,
  observation: ObservationEnvelope,
): Promise<void> {
  const action = ctx.stored.inFlight
  if (!action || action.page_epoch >= observation.page_epoch) return
  let result: ActionResultEnvelope
  if (action.phase === 'prepared') {
    result = results.rejected(action, observation, 'runtime_replaced',
      'The previous runtime ended before the action was dispatched.')
  } else if (await recoveryProvesNavigation(
    action, location.href, ctx.documentId, documentNavigation())) {
    const guardExpiry = action.write_guard_expires_at_ms
    const observationSummary = ['click', 'select', 'goto'].includes(action.command.action)
      ? summarizeObservation(
        null,
        observation.observation,
        ctx.redactor,
        typeof document !== 'undefined' ? document : null,
        ctx.panelContainer ?? undefined,
      )
      : undefined
    const receipt = results.receipt(action, {
      ok: true, code: '', detail: '', observation,
      beforeObservation: null,
      ...(observationSummary ? {observationSummary} : {}),
      navigated: action.started_url_display !== observation.observation.url,
      elapsedMs: Date.now() - Number(action.started_at || 0),
      urlBefore: ctx.currentObservation?.observation.url || '',
      writeGuardHeld: Boolean(ctx.config?.pageWriteGuard &&
        action.write_guard_path === ctx.config.pageWriteGuard &&
        typeof guardExpiry === 'number' && Number.isFinite(guardExpiry) &&
        guardExpiry > Date.now()),
    }, ctx.redactor)
    result = results.completed(action, receipt, observation)
  } else {
    result = results.indeterminate(action, observation,
      'The prior page ended without a trustworthy receipt; the action will not be retried.')
  }
  await ctx.deliver(result)
}

export function bindAnswerOpener(
  openPage: PageOpener | undefined,
  openInApp: (url: string) => Promise<boolean>,
): (url: string) => boolean {
  return url => Boolean(openPage) && tryOpenAnswerInPlace(url, openInApp)
}
