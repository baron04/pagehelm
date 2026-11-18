/**
 * Session handshake, expiry, lapse detection, and principal renewal for `EmbeddedWebRuntime`.
 */
import {DEFAULT_CONFIGURATION, type PanelConfiguration, type PanelState} from '../contract/panel'
import {later, type Cancel} from './contain'
import type {PanelHost} from './panelHost'
import type {SessionState} from './storage'
import {WIRE_VERSION, type JsonObject, type RuntimeConfig} from './types'

export interface LeaseSyncContext {
  readonly endpoint: string
  readonly sessionId: string
  readonly token: string
  readonly applicationId: string
  principalBinding: string
  readonly runtimeInstanceId: string
  readonly guardWindow: string
  config: RuntimeConfig
  leaseId: string
  panelConfiguration: PanelConfiguration | null
  readonly stored: SessionState
  endState: string
  stopped: boolean
  cancelExpiry: Cancel | null
  readonly eventAbortController: AbortController | null
  readonly panelHost: PanelHost | null
  post<T = JsonObject>(path: string, body: unknown, options?: {includeLease?: boolean; signal?: AbortSignal}): Promise<T>
  panelState(state: PanelState): void
  armExpiry(): void
  expire(code: string): void
  lapsed(code: string): boolean
}

export async function performHello(
  ctx: LeaseSyncContext,
  runtimeName: string,
  capabilities: readonly string[],
): Promise<void> {
  const reply = await ctx.post<{lease_id?: string; panel?: PanelConfiguration}>('/hello', {
    schema_version: WIRE_VERSION,
    session_id: ctx.sessionId,
    client_id: ctx.stored.clientId,
    runtime_instance_id: ctx.runtimeInstanceId,
    runtime: runtimeName,
    capabilities,
    page_origin: location.origin,
    application_id: ctx.applicationId,
    principal_digest: ctx.principalBinding,
    last_event_id: ctx.stored.lastEventId,
    ...(ctx.guardWindow ? {window_id: ctx.guardWindow} : {}),
  }, {includeLease: false})
  if (!reply.lease_id) throw new Error('hello response did not include a runtime lease')
  ctx.leaseId = String(reply.lease_id)
  if (!reply.panel || typeof reply.panel.protocol !== 'number') {
    throw new Error('hello response did not include the panel configuration')
  }
  ctx.panelConfiguration = reply.panel
  ctx.armExpiry()
}

/**
 * Stop pretending a session is live past its own deadline: it ends, and says which
 * deadline it was, rather than looking like a network fault that reconnects forever.
 */
export function armSessionExpiry(ctx: LeaseSyncContext): void {
  ctx.cancelExpiry?.()
  ctx.cancelExpiry = null
  const deadlines = ([
    [Number(ctx.config.principalExpiresAt || 0)
      || Number(ctx.panelConfiguration?.principal_expires_at || 0), 'principal_expired'],
    [Number(ctx.config.sessionExpiresAt || 0)
      || Number(ctx.panelConfiguration?.session_expires_at || 0), 'session_expired'],
  ] as Array<[number, string]>).filter(([at]) => at > 0)
  const soonest = deadlines.reduce<[number, string] | null>(
    (best, next) => (best && best[0] <= next[0] ? best : next), null)
  if (!soonest) return
  const [at, code] = soonest
  const delay = at - Date.now()
  if (delay <= 0) {
    ctx.expire(code)
    return
  }
  ctx.cancelExpiry = later('session expiry', delay, () => ctx.expire(code))
}

export function expireSession(ctx: LeaseSyncContext, code: string): void {
  if (ctx.stopped || ctx.lapsed(code)) return
  ctx.endState = 'ended'
  ctx.stopped = true
  ctx.eventAbortController?.abort()
  ctx.panelState('ended')
  ctx.panelHost?.raise(code, {retryable: false})
}

/**
 * End quietly when the session lapsed rather than failed -- its sign-in ran out while
 * nobody was using it, the page went quiet, the host restarted. The person's session comes
 * back as it was on their next message, so there is nothing to tell them.
 */
export function handleSessionLapse(ctx: LeaseSyncContext, code: string): boolean {
  const lapses = ctx.panelConfiguration?.lapse_codes ?? DEFAULT_CONFIGURATION.lapse_codes
  if (!lapses.includes(code)) return false
  if (!ctx.stopped) {
    ctx.endState = 'lapsed'
    ctx.stopped = true
    ctx.cancelExpiry?.()
    ctx.cancelExpiry = null
    ctx.eventAbortController?.abort()
  }
  return true
}

export function canRenewRuntimeSession(ctx: LeaseSyncContext): boolean {
  return !ctx.stopped && !ctx.stored.taskInFlight && !ctx.stored.inFlight
    && !ctx.panelHost?.snapshot().pendingApproval
}

export function renewRuntimeSession(ctx: LeaseSyncContext, config: RuntimeConfig): void {
  if (!canRenewRuntimeSession(ctx)) throw new Error('session renewal requires an idle runtime')
  const stable: Array<[string, string, string]> = [
    ['endpoint', ctx.endpoint, String(config?.endpoint || '').replace(/\/$/, '')],
    ['sessionId', ctx.sessionId, String(config?.sessionId || '')],
    ['clientToken', ctx.token, String(config?.clientToken || '')],
    ['sessionGeneration', String(ctx.config.sessionGeneration || ''),
      String(config?.sessionGeneration || '')],
    ['applicationId', ctx.applicationId, String(config?.applicationId || '')],
  ]
  const changed = stable.find(([, before, after]) => before !== after)
  if (changed) throw new Error(`session renewal changed ${changed[0]}`)
  const principalBinding = String(config?.principalBinding || '')
  const principalExpiresAt = Number(config?.principalExpiresAt || 0)
  const sessionExpiresAt = Number(config?.sessionExpiresAt || 0)
  if (!principalBinding || principalExpiresAt <= Date.now() || sessionExpiresAt <= Date.now()) {
    throw new Error('session renewal did not provide a live principal')
  }
  ctx.config = {...ctx.config, ...config}
  ctx.principalBinding = principalBinding
  if (ctx.panelConfiguration) {
    ctx.panelConfiguration = {
      ...ctx.panelConfiguration,
      principal_expires_at: principalExpiresAt,
      session_expires_at: sessionExpiresAt,
    }
  }
  ctx.armExpiry()
}
