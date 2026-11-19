/**
 * Approval decision persistence, idempotent replay, and incoming approval requests.
 */
import type {ApprovalRequest, PanelConfiguration, PanelState} from '../contract/panel'
import {classifyTransportError, type PanelHost} from './panelHost'
import type {SessionState} from './storage'
import {WIRE_VERSION, type JsonObject} from './types'

export interface ApprovalContext {
  readonly sessionId: string
  readonly stored: SessionState
  readonly panelHost: PanelHost | null
  readonly panelConfiguration: PanelConfiguration | null
  save(): void
  panelState(state: PanelState): void
  post<T = JsonObject>(path: string, body: unknown, options?: {includeLease?: boolean; signal?: AbortSignal}): Promise<T>
  postApproval(request: ApprovalRequest, decision: 'approved' | 'denied' | 'prefill'): Promise<void>
}

/** Record the decision before dispatching it, so a reload replays rather than re-asks. */
export async function postApprovalDecision(
  ctx: ApprovalContext,
  request: ApprovalRequest,
  decision: 'approved' | 'denied' | 'prefill',
): Promise<void> {
  ctx.stored.approvals[request.approval_id] = decision
  ctx.save()
  try {
    await ctx.post('/approvals', {
      schema_version: WIRE_VERSION,
      session_id: ctx.sessionId,
      approval_id: request.approval_id,
      decision,
      principal_digest: String(request.principal_digest || ''),
      turn_id: String(request.turn_id || ''),
      operation_id: String(request.operation_id || ''),
    })
  } catch (error) {
    const verdict = classifyTransportError(
      error as {code?: unknown; status?: unknown}, ctx.panelConfiguration)
    if (!verdict.approvalClosed) throw error
    // Nothing can be answered here any more. Forget the decision -- replaying it after a
    // reload fails the same way -- take the card down, and say what happened. The
    // hand-written Runtime set the panel to `'idle'` here, a state the contract never had.
    delete ctx.stored.approvals[request.approval_id]
    ctx.save()
    ctx.panelHost?.setPendingApproval(null)
    ctx.panelHost?.raise(verdict.code, {retryable: false})
    ctx.panelState('ready')
    return
  }
  ctx.panelHost?.setPendingApproval(null)
  ctx.panelState(decision === 'denied' ? 'cancelled' : 'acting')
}

/** Report a panel visibility-gate refusal for server audit diagnostics without consuming the card. */
export async function postApprovalRefusal(
  ctx: ApprovalContext,
  request: ApprovalRequest,
  code: string,
): Promise<void> {
  if (!code) return
  try {
    await ctx.post('/approval_refusals', {
      schema_version: WIRE_VERSION,
      session_id: ctx.sessionId,
      approval_id: request.approval_id,
      code: String(code),
      turn_id: String(request.turn_id || ''),
    })
  } catch {
    // Diagnostic-only telemetry: never consume the pending card or raise a panel notice.
  }
}

export async function handleApprovalRequiredEvent(
  ctx: ApprovalContext,
  payload: JsonObject,
): Promise<void> {
  ctx.panelState('awaiting_confirmation')
  const request = payload as unknown as ApprovalRequest
  const decided = ctx.stored.approvals[request.approval_id] as 'approved' | 'denied' | 'prefill' | undefined
  if (decided) {
    // A document that reloaded between deciding and posting finishes the POST rather
    // than asking again: the decision is already recorded and idempotent.
    await ctx.postApproval(request, decided)
    return
  }
  if (!ctx.panelHost) {
    // No panel means nobody can be asked, and an unanswered write must not proceed.
    await ctx.postApproval(request, 'denied')
    return
  }
  ctx.panelHost.setPendingApproval(request)
}
