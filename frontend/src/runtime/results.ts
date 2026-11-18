/**
 * What the Runtime reports about an action: a receipt of what happened on the page, inside
 * a result that says whether the server may count it.
 *
 * Every result carries the same header -- the action it answers, the observation it was
 * based on, the command digest -- which three builders and one inline literal used to
 * write out separately. They share it now.
 */
import type {Redactor} from './redaction'
import browserContract from '@contract/browser_contract.json'
import {WIRE_VERSION, type ActionCommand, type ActionEnvelope,
  type ActionReceipt, type ActionResultEnvelope, type ObservationEnvelope, type ExecutionTrace} from './types'

/** Read only the diagnostic whitelist. A getter or malformed collector cannot
 * replace a valid business result or its original failure. */
export function executionTraceOf(result: unknown, action: string): ExecutionTrace | undefined {
  try {
    const limits = browserContract.execution_trace
    if (!limits.operations.includes(action) || !result || typeof result !== 'object') return undefined
    const raw = (result as {execution_trace?: unknown}).execution_trace
    if (!raw || typeof raw !== 'object') return undefined
    const {trace_id, events} = raw as {trace_id?: unknown; events?: unknown}
    if (typeof trace_id !== 'string' || !trace_id.length || trace_id.length > limits.max_trace_id_chars
        || !new RegExp(limits.trace_id_pattern).test(trace_id)
        || !Array.isArray(events) || !events.length || events.length > limits.max_events) return undefined
    let previous = 0
    const clean: ExecutionTrace['events'] = []
    for (const event of events) {
      if (!event || typeof event !== 'object') return undefined
      const {sequence, operation, path} = event as {sequence?: unknown; operation?: unknown; path?: unknown}
      if (typeof sequence !== 'number' || !Number.isSafeInteger(sequence) || sequence <= previous
          || operation !== action || typeof path !== 'string' || !limits.paths.includes(path)) return undefined
      clean.push({sequence, operation: action, path})
      previous = sequence
    }
    return {trace_id, events: clean}
  } catch { return undefined }
}

type ResultStatus = 'completed' | 'rejected' | 'indeterminate'

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

function resultEnvelope(action: ActionEnvelope, status: ResultStatus,
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

interface ReceiptFacts {
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
  /** Bounded labels from the owned list, for a recoverable no-effect refusal. */
  candidateLabels?: string[]
  noEffect?: boolean
  execution_trace?: unknown
  form_data?: unknown
}

export function receipt(action: ActionEnvelope, facts: ReceiptFacts, redactor: Redactor): ActionReceipt {
  const execution_trace = executionTraceOf(facts, action.command.action)
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
    ...(facts.candidateLabels?.length ? {candidate_labels: facts.candidateLabels
      .slice(0, browserContract.choice_failure.max_candidates)
      .map(label => redactor.redactString(label).slice(0, browserContract.choice_failure.max_label_chars))
      .filter(Boolean)} : {}),
    ...(facts.noEffect === true ? {no_effect: true} : {}),
    ...(execution_trace ? {execution_trace} : {}),
    ...(facts.form_data && action.command.action === 'form_model'
      ? {form_data: facts.form_data as NonNullable<ActionReceipt['form_data']>} : {}),
    observation: facts.observation.observation,
  }
}
