/**
 * Typed view over the contract the server owns.
 *
 * Nothing here invents a value. Every state name, bound, label and mapping comes from
 * `panel_contract.json`, which the gateway validates and `test_panel_contract.py` pins.
 * The panel renders what the server decided; it does not keep a second copy of it.
 */
import raw from '@contract/panel_contract.json'
import presentation from '../presentation.json'

export type Tone = 'neutral' | 'ready' | 'active' | 'warning' | 'success' | 'error'
export type PanelState = keyof typeof raw.panel_states
export type EntryKind = 'goal' | 'progress' | 'action' | 'result' | 'approval' | 'notice'
export type LabelKey = keyof typeof raw.label_keys
export type Labels = Record<LabelKey, string>

export interface PanelLimits {
  composer_max_chars: number
  history_page_size: number
  effect_max_chars: number
  target_max_chars: number
  max_targets: number
  notice_max_chars: number
  label_max_chars: number
  goal_max_chars: number
  result_max_chars: number
}

export interface TimelineEntry {
  id: string
  sequence: number
  turn_id: string
  at: number
  kind: EntryKind
  tone: Tone
  text: string
  panel_state?: PanelState
  approval_id?: string
  authored?: 'model' | 'server'
  replayed?: boolean
  /** Stable ordering within a durable stream, independent of transport incarnation. */
  position?: {stream: string; sequence: number}
  step?: StepFacts
  /** Says only what is under way: shown while it is the newest step of a running turn. */
  transient?: boolean
}

/** What one timeline node can show beyond its sentence; the server bounds every field. */
export interface StepFacts {
  stage?: string
  tool?: string
  arguments?: string
  result?: string
}

export interface ApprovalRequest {
  schema_version: number
  approval_id: string
  effect: string
  targets: string[]
  expires_at: number
  principal_digest: string
  turn_id: string
  operation_id: string
  /** `operate` asks once per turn before the assistant works the page. */
  kind?: ApprovalKind
}

export type ApprovalKind = Exclude<keyof typeof presentation.approval_kinds, 'comment'>

/** Whether a card of this kind offers a decline -- the panel's own choice, not the server's. */
export function declinable(kind: ApprovalKind | undefined): boolean {
  return presentation.approval_kinds[kind ?? 'write'].declinable
}

export interface TimelinePage {
  schema_version: number
  protocol: number
  /** The channel that numbered these entries. Ids and sequences are its own. */
  incarnation?: string
  entries: TimelineEntry[]
  cursor: string
  oldest: string
  has_more: boolean
  /** More live entries remain after cursor; unrelated to older history. */
  has_newer?: boolean
  closed: boolean
  close_reason: string
  pending_approval: ApprovalRequest | null
}

/** Exactly what `RemoteSessionChannel._panel_configuration()` sends in the hello reply. */
export interface PanelConfiguration {
  protocol: number
  limits: PanelLimits
  labels: Labels
  notices: Record<string, string>
  panel_states: Record<PanelState, Tone>
  progress_states: Record<string, {panel_state: PanelState; tone: Tone; terminal: boolean}>
  terminal_error_codes: string[]
  terminal_panel_state_by_code: Record<string, PanelState>
  /** Why a session ended with nothing wrong; it comes back on the next message, unannounced. */
  lapse_codes: string[]
  session_expires_at: number
  principal_expires_at: number
}

// One generated integer, shared with the Runtime (`runtime/panelHost.ts`) so the two sides
// of the handshake can never state the protocol version differently.
export {PANEL_PROTOCOL} from './protocol.generated'
export const DEFAULT_CONFIGURATION: PanelConfiguration = {
  protocol: raw.protocol,
  limits: raw.limits as PanelLimits,
  labels: raw.label_keys as Labels,
  notices: raw.notices,
  panel_states: raw.panel_states as Record<PanelState, Tone>,
  progress_states: raw.progress_states as PanelConfiguration['progress_states'],
  terminal_error_codes: [...raw.terminal_error_codes],
  terminal_panel_state_by_code: raw.terminal_panel_state_by_code as Record<string, PanelState>,
  lapse_codes: [...raw.lapse_codes],
  session_expires_at: 0,
  principal_expires_at: 0,
}

export function toneOf(state: PanelState): Tone {
  return (raw.panel_states as Record<PanelState, Tone>)[state]
}

export function isPanelState(value: unknown): value is PanelState {
  return typeof value === 'string' && Object.hasOwn(raw.panel_states, value)
}

export function statusLabelKey(state: PanelState): LabelKey {
  const key = `status${state.replace(/(^|_)([a-z])/g, (_m, _s, c: string) => c.toUpperCase())}`
  if (!(key in raw.label_keys)) {
    throw new Error(`panel state ${state} has no status label in the contract`)
  }
  return key as LabelKey
}
