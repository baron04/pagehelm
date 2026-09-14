/**
 * Typed view over the contract the server owns.
 *
 * Nothing here invents a value. Every state name, bound, label and mapping comes from
 * `panel_contract.json`, which the gateway validates and `test_panel_contract.py` pins.
 * The panel renders what the server decided; it does not keep a second copy of it.
 */
import raw from '@contract/panel_contract.json'

export type Tone = 'neutral' | 'ready' | 'active' | 'warning' | 'success' | 'error'
export type PanelState = keyof typeof raw.panel_states
export type EntryKind = 'goal' | 'progress' | 'action' | 'result' | 'approval' | 'notice'
export type LabelKey = keyof typeof raw.label_keys
export type Labels = Record<LabelKey, string>

export interface PanelLimits {
  composer_max_chars: number
  history_page_size: number
  rendered_entry_budget: number
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
  source_details?: string
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
}

export interface TimelinePage {
  schema_version: number
  protocol: number
  entries: TimelineEntry[]
  cursor: string
  oldest: string
  has_more: boolean
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
  layout: {overlay_z_index: number}
  approval_visibility: {min_visible_ratio: number; min_visible_ms: number}
  terminal_error_codes: string[]
  terminal_panel_state_by_code: Record<string, PanelState>
  session_expires_at: number
  principal_expires_at: number
}

export const PANEL_PROTOCOL: number = raw.protocol
export const DEFAULT_CONFIGURATION: PanelConfiguration = {
  protocol: raw.protocol,
  limits: raw.limits as PanelLimits,
  labels: raw.label_keys as Labels,
  notices: raw.notices,
  panel_states: raw.panel_states as Record<PanelState, Tone>,
  progress_states: raw.progress_states as PanelConfiguration['progress_states'],
  layout: raw.layout,
  approval_visibility: raw.approval_visibility,
  terminal_error_codes: [...raw.terminal_error_codes],
  terminal_panel_state_by_code: raw.terminal_panel_state_by_code as Record<string, PanelState>,
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
