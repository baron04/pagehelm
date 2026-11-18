/**
 * Transport client coordination: lease recovery, error classification, SSE event stream,
 * timeline pagination/refresh, and durable result delivery/retry loops.
 */
import type {PanelConfiguration, PanelState} from '../contract/panel'
import {detach, sleep} from './contain'
import {backoffAfter, classifyTransportError, type PanelHost, type TransportVerdict} from './panelHost'
import type {SessionState} from './storage'
import {sseBatches, type SessionClient} from './transport'
import type {ActionResultEnvelope, JsonObject, ObservationEnvelope} from './types'

/** One event on the server's stream. */
export interface WireEvent {
  kind: string
  payload: JsonObject
  turn_id?: string
}

export interface TransportClientContext {
  readonly client: SessionClient
  readonly stored: SessionState
  readonly panelHost: PanelHost | null
  readonly panelConfiguration: PanelConfiguration | null
  panelCursor: string
  timelineRefresh: Promise<void> | null
  currentObservation: ObservationEnvelope | null
  endState: string
  stopped: boolean
  eventAbortController: AbortController | null
  save(): void
  panelState(state: PanelState): void
  lapsed(code: string): boolean
  raise(error: unknown, fallback?: string): TransportVerdict
  post<T = JsonObject>(path: string, body: unknown, options?: {includeLease?: boolean; signal?: AbortSignal}): Promise<T>
  timeline(query?: {after?: string; before?: string}): Promise<Parameters<PanelHost['applyTimeline']>[0]>
  refreshTimeline(): Promise<void>
  handleEvent(event: WireEvent): Promise<void>
  sendResult(result: ActionResultEnvelope): Promise<void>
  observe(causedByActionId?: string, signal?: AbortSignal): Promise<ObservationEnvelope>
  eventLoop(): Promise<void>
}

export function createAbortError(): Error {
  const error = new Error('The operation was aborted.')
  error.name = 'AbortError'
  return error
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

export function postWithLeaseRecovery<T = JsonObject>(
  client: SessionClient,
  path: string,
  body: unknown,
  options: {includeLease?: boolean; signal?: AbortSignal},
  onLeaseEvicted: () => Promise<void>,
): Promise<T> {
  return client.post<T>(path, body, options).catch(async (error: unknown) => {
    if (options.signal?.aborted || isAbortError(error)) throw error
    // Registry pressure may evict a quiet page's lease where a taken-over one is
    // terminal: the session is still this page's, so hello again for a fresh lease
    // and retry the request once.
    if ((error as {code?: unknown})?.code !== 'lease_evicted'
        || options.includeLease === false) throw error
    await onLeaseEvicted()
    return client.post<T>(path, body, options)
  })
}

/** One place where a failed request becomes a panel state and a stop-or-retry decision. */
export function raiseTransportError(
  ctx: TransportClientContext,
  error: unknown,
  fallback = 'submit_failed',
): TransportVerdict {
  const failure = error as {code?: unknown; status?: unknown} | null | undefined
  const verdict = classifyTransportError(failure, ctx.panelConfiguration)
  if (verdict.terminal && ctx.lapsed(verdict.code)) return verdict
  if (verdict.terminal) {
    ctx.endState = verdict.state
    ctx.stopped = true
    ctx.eventAbortController?.abort()
    ctx.panelState(verdict.state)
    ctx.panelHost?.raise(verdict.code, {retryable: false})
  } else {
    ctx.panelState(verdict.state)
    ctx.panelHost?.raise(String(failure?.code || fallback), {retryable: verdict.retryable})
  }
  return verdict
}

export async function refreshTimelineLoop(ctx: TransportClientContext): Promise<void> {
  if (ctx.timelineRefresh) return ctx.timelineRefresh
  const refresh = async () => {
    const host = ctx.panelHost
    if (!host) return
    let more = true
    while (more && !ctx.stopped && ctx.panelHost === host) {
      const version = host.timelineVersion
      const page = await ctx.timeline({after: ctx.panelCursor})
      if (ctx.stopped || ctx.panelHost !== host
          || !host.applyTimeline(page, {version})) return
      const cursor = String(page?.cursor || '')
      more = Boolean(page?.has_newer) && cursor !== ctx.panelCursor
      ctx.panelCursor = cursor || ctx.panelCursor
    }
  }
  ctx.timelineRefresh = refresh()
  try {
    await ctx.timelineRefresh
  } finally {
    ctx.timelineRefresh = null
  }
}

export async function loadOlderTimeline(ctx: TransportClientContext, before: string): Promise<void> {
  const host = ctx.panelHost
  if (!host) return
  const version = host.timelineVersion
  try {
    const page = await ctx.timeline({before})
    if (!ctx.stopped && ctx.panelHost === host) {
      host.applyTimeline(page, {prepend: true, version})
    }
  } catch (error) {
    if (!ctx.stopped && ctx.panelHost === host && version === host.timelineVersion) {
      const verdict = classifyTransportError(
        error as {code?: unknown; status?: unknown}, ctx.panelConfiguration)
      if (verdict.terminal) ctx.raise(error)
      else host.setHistoryError(true)
    }
  } finally {
    if (version === host.timelineVersion) host.setLoadingOlder(false)
  }
}

export async function runEventLoop(ctx: TransportClientContext): Promise<void> {
  let backoff = 0
  while (!ctx.stopped) {
    const abortController = new AbortController()
    ctx.eventAbortController = abortController
    try {
      const stream = await ctx.client.events(ctx.stored.lastEventId, abortController.signal)
      backoff = 0
      ctx.panelHost?.clearNotice()
      await consumeEventStream(ctx, stream)
    } catch (error) {
      if (ctx.stopped) return
      if (ctx.raise(error, 'reconnecting').terminal) return
      backoff = backoffAfter(backoff)
      await sleep(backoff)
    } finally {
      if (ctx.eventAbortController === abortController) ctx.eventAbortController = null
    }
  }
}

export async function consumeEventStream(
  ctx: TransportClientContext,
  stream: ReadableStream<Uint8Array>,
): Promise<void> {
  for await (const batch of sseBatches(stream, () => ctx.stopped)) {
    for (const frame of batch) {
      // Frames that were parsed before a stop may still sit in the current batch. Do not
      // handle them, and above all never `_save` after a terminal stop: the session's
      // storage was just cleared, and saving would resurrect what it said goodbye to.
      // Breaking (not returning) still lets the batch's final timeline fetch run -- that
      // is how the panel shows the last message of a session that ended mid-batch.
      if (ctx.stopped) break
      const event = JSON.parse(frame.data) as WireEvent
      await ctx.handleEvent(event)
      if (ctx.stopped) break
      ctx.stored.lastEventId = frame.id
      ctx.save()
      if (event.kind === 'action') {
        const actionId = event.payload.action_id
        if (ctx.stored.cachedResult?.action_id === actionId) ctx.stored.cachedResult = null
        if (ctx.stored.inFlight?.action_id === actionId) ctx.stored.inFlight = null
        ctx.save()
      }
    }
    // One projection fetch per delivered batch, not per event: the panel renders the
    // server's timeline, so it has to come from the server.
    await ctx.refreshTimeline().catch(() => {})
  }
}

/** Cache a result durably, then send it: a crash in between resends, never re-executes. */
export async function deliverResultEnvelope(
  ctx: TransportClientContext,
  result: ActionResultEnvelope,
): Promise<void> {
  ctx.stored.cachedResult = result
  ctx.save()
  await ctx.sendResult(result)
}

export async function sendResultEnvelope(
  ctx: TransportClientContext,
  result: ActionResultEnvelope,
): Promise<void> {
  await ctx.post('/results', result)
  // Keep both records until the SSE cursor is durably advanced. A crash after the POST
  // response but before that write would otherwise replay and execute again.
  const after = result.observation_after
  if (after && (!ctx.currentObservation || after.page_epoch >= ctx.currentObservation.page_epoch)) {
    ctx.currentObservation = after
  }
  ctx.panelState('observing')
}

export async function retryCachedResultLoop(ctx: TransportClientContext): Promise<boolean> {
  let backoff = 0
  while (!ctx.stopped && ctx.stored.cachedResult) {
    try {
      await ctx.sendResult(ctx.stored.cachedResult)
      return true
    } catch (error) {
      if (ctx.raise(error, 'reconnecting').terminal) return false
      await sleep(backoff)
      backoff = backoffAfter(backoff)
    }
  }
  return false
}

export async function resumeDeferredResultLoop(ctx: TransportClientContext): Promise<void> {
  if (!await retryCachedResultLoop(ctx) || ctx.stopped) return
  // The cached result can describe the document that just navigated away. Refresh the
  // selector map and server observation before consuming another action here.
  let backoff = 0
  while (!ctx.stopped) {
    try {
      const observation = await ctx.observe(ctx.stored.inFlight?.action_id || '')
      await ctx.post('/observations', observation)
      ctx.panelState('ready')
      if (!ctx.stopped) detach('event loop', ctx.eventLoop())
      return
    } catch (error) {
      if (ctx.raise(error, 'reconnecting').terminal) return
      await sleep(backoff)
      backoff = backoffAfter(backoff)
    }
  }
}
