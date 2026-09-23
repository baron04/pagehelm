/**
 * What the Runtime reports about an action: a receipt of what happened on the page, inside
 * a result that says whether the server may count it.
 *
 * Every result carries the same header -- the action it answers, the observation it was
 * based on, the command digest -- which three builders and one inline literal used to
 * write out separately. They share it now.
 */
import type {Redactor} from './redaction'
import {WIRE_VERSION, type ActionCommand, type ActionEnvelope,
  type ActionReceipt, type ActionResultEnvelope, type ObservationEnvelope} from './types'

export type ResultStatus = 'completed' | 'rejected' | 'indeterminate'

/** An error message with credentials and the value the action typed removed, and bounded. */
export function safeErrorDetail(detail: unknown, command: ActionCommand | null | undefined,
                                redactor: Redactor): string {
  let out = redactor.redactString(detail)
  const hideValues = (input: unknown): void => {
    if (!input || typeof input !== 'object') return
    for (const [key, value] of Object.entries(input)) {
      if (key === 'value' && value !== undefined && value !== null && String(value)) {
        out = out.split(String(value)).join('[redacted]')
      } else if (Array.isArray(value)) {
        value.forEach(hideValues)
      } else if (value && typeof value === 'object') {
        hideValues(value)
      }
    }
  }
  hideValues(command?.args)
  return out.slice(0, 800)
}

export function resultEnvelope(action: ActionEnvelope, status: ResultStatus,
                               observation: ObservationEnvelope | null,
                               {receipt = null, code = '', detail = ''}:
                               {receipt?: ActionReceipt | null; code?: string; detail?: string} = {},
): ActionResultEnvelope {
  return {
    schema_version: WIRE_VERSION, session_id: action.session_id, turn_id: action.turn_id,
    action_id: action.action_id, based_on_observation_id: action.based_on_observation_id,
    page_epoch: action.page_epoch, command_digest: action.command_digest,
    status, observation_after: observation, receipt, error_code: code, error_detail: detail,
  }
}

export function rejected(action: ActionEnvelope, observation: ObservationEnvelope | null,
                         code: string, detail: string): ActionResultEnvelope {
  return resultEnvelope(action, 'rejected', observation, {code, detail})
}

export function indeterminate(action: ActionEnvelope, observation: ObservationEnvelope | null,
                              detail: string): ActionResultEnvelope {
  return resultEnvelope(action, 'indeterminate', observation,
    {code: 'execution_state_unknown', detail})
}

export function completed(action: ActionEnvelope, receipt: ActionReceipt,
                          observation: ObservationEnvelope): ActionResultEnvelope {
  return resultEnvelope(action, 'completed', observation, {receipt})
}

export interface ReceiptFacts {
  ok: boolean
  code: string
  detail: unknown
  observation: ObservationEnvelope
  navigated: boolean
  elapsedMs: number
  /** Where the page was before, if the in-flight record does not say. */
  urlBefore: string
  /** The application's read-only window covered this whole action. */
  writeGuardHeld?: boolean
}

export function receipt(action: ActionEnvelope, facts: ReceiptFacts, redactor: Redactor): ActionReceipt {
  return {
    schema_version: WIRE_VERSION,
    command_id: action.action_id,
    action: action.command.action,
    args_echo: redactor.safeArgs(action.command.args),
    ok: facts.ok,
    error_code: facts.code,
    error_detail: safeErrorDetail(facts.detail, action.command, redactor),
    url_before: String(action.started_url_display || facts.urlBefore || ''),
    url_after: facts.observation.observation.url,
    navigated: facts.navigated,
    epoch_before: action.page_epoch,
    epoch_after: facts.observation.page_epoch,
    elapsed_ms: Math.max(0, Math.round(facts.elapsedMs || 0)),
    relogin: false,
    write_guard_held: facts.writeGuardHeld === true,
    observation: facts.observation.observation,
  }
}
