/**
 * What the Runtime reports about an action: a receipt of what happened on the page, inside
 * a result that says whether the server may count it.
 *
 * Every result carries the same header -- the action it answers, the observation it was
 * based on, the command digest -- which three builders and one inline literal used to
 * write out separately. They share it now.
 */
import {summarizeObservation} from './pageSnapshot'
import type {Redactor} from './redaction'
import browserContract from '@contract/browser_contract.json'
import {WIRE_VERSION, type ActionCommand, type ActionEnvelope,
  type ActionReceipt, type ActionResultEnvelope, type JsonObject,
  type ObservationEnvelope, type ExecutionTrace, type PageObservation} from './types'

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
  beforeObservation?: PageObservation | null
  observationSummary?: JsonObject
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
  appliedFields?: JsonObject
  rejectedFields?: JsonObject
  previousFields?: JsonObject
  restrictedFields?: string[]
}

function boundedFieldMap(raw: JsonObject | undefined, redactor: Redactor): JsonObject | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  const entries = Object.entries(raw)
  if (!entries.length) return undefined
  const out: JsonObject = {}
  for (const [key, val] of entries.slice(0, 64)) {
    const cleanKey = redactor.redactString(key).slice(0, browserContract.form_handoff.max_label_chars)
    if (!cleanKey || redactor.isSensitiveName(cleanKey)) continue
    if (typeof val === 'string') {
      out[cleanKey] = redactor.redactString(val).slice(0, 300)
    } else if (val && typeof val === 'object' && !Array.isArray(val)) {
      const obj = val as Record<string, unknown>
      const cleaned: JsonObject = {}
      if (typeof obj.reason === 'string') cleaned.reason = redactor.redactString(obj.reason).slice(0, 120)
      if (Array.isArray(obj.candidates)) {
        cleaned.candidates = obj.candidates
          .filter((c): c is string => typeof c === 'string' && c.trim().length > 0)
          .slice(0, browserContract.choice_failure.max_candidates)
          .map(c => redactor.redactString(c).slice(0, browserContract.choice_failure.max_label_chars))
      }
      if (Object.keys(cleaned).length) out[cleanKey] = cleaned
    } else if (typeof val === 'number' || typeof val === 'boolean' || val === null) {
      out[cleanKey] = val
    }
  }
  if (!Object.keys(out).length) return undefined
  try {
    if (JSON.stringify(out).length > browserContract.form_model.max_bytes) return undefined
  } catch {
    return undefined
  }
  return out
}

function boundedRestrictedFields(raw: string[] | undefined, redactor: Redactor): string[] | undefined {
  if (!Array.isArray(raw) || !raw.length) return undefined
  const out: string[] = []
  const seen = new Set<string>()
  for (const label of raw) {
    if (typeof label !== 'string') continue
    const clean = redactor.redactString(label).replace(/\s+/g, ' ').trim()
      .slice(0, browserContract.form_handoff.max_label_chars)
    if (!clean || seen.has(clean)) continue
    seen.add(clean)
    out.push(clean)
    if (out.length >= browserContract.form_handoff.max_choice_options) break
  }
  return out.length ? out : undefined
}

export function receipt(action: ActionEnvelope, facts: ReceiptFacts, redactor: Redactor): ActionReceipt {
  const execution_trace = executionTraceOf(facts, action.command.action)
  const observation_summary = facts.observationSummary ?? (
    ['click', 'select', 'goto'].includes(action.command.action)
      ? summarizeObservation(facts.beforeObservation, facts.observation.observation, redactor)
      : undefined
  )
  const applied_fields = boundedFieldMap(facts.appliedFields, redactor)
  const rejected_fields = boundedFieldMap(facts.rejectedFields, redactor)
  const previous_fields = boundedFieldMap(facts.previousFields, redactor)
  const restricted_fields = boundedRestrictedFields(facts.restrictedFields, redactor)
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
    ...(observation_summary ? {observation_summary} : {}),
    ...(applied_fields ? {applied_fields} : {}),
    ...(rejected_fields ? {rejected_fields} : {}),
    ...(previous_fields ? {previous_fields} : {}),
    ...(restricted_fields ? {restricted_fields} : {}),
    observation: facts.observation.observation,
  }
}
