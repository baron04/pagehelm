/**
 * The one boundary between the embedded Runtime and this panel.
 *
 * The Runtime owns every network call, the session lease and the page; the panel owns
 * nothing but what it is handed. The shape is checked once, here, when the panel is
 * mounted -- not probed for at each call site. That rule is the same one the server
 * applies at its own assembly points, and it is why `setPanelState`'s two-layer
 * `state() ?? status()` probe does not survive into this contract.
 */
import type {
  ApprovalRequest, PanelConfiguration, PanelState, TimelineEntry, TimelinePage,
} from '../contract/panel'

export interface ConnectionState {
  /** What the Runtime knows and the server cannot: the state of this page's connection. */
  state: PanelState
  busy: boolean
  stopping: boolean
  online: boolean
}

export interface HostNotice {
  code: string
  retryable: boolean
  /**
   * The words, when they cannot be the server's: a start that failed happened before any
   * server spoke, so its sentence is the panel's own (`presentation.json`).
   */
  message?: string
  /** Set when the failure belongs to one message rather than the session. */
  turnId?: string
}

/** What a new conversation offers to start from: one kind of request, and one example of it. */
export type ExampleKind = 'query' | 'find' | 'operate'
export interface PanelExample {
  kind: ExampleKind
  /** A request the application has been shown to answer; picking it fills the composer. */
  text: string
}

export interface PanelSnapshot {
  /** Browser-only viewport point of a dispatched page action; never sent to the server. */
  activityPoint?: {x: number; y: number; at: number} | null
  connection: ConnectionState
  entries: TimelineEntry[]
  pendingApproval: ApprovalRequest | null
  notice: HostNotice | null
  hasOlder: boolean
  loadingOlder: boolean
  historyError?: boolean
  closed: boolean
  /** A locally echoed message that has not yet been accepted by the server. */
  optimistic: {turnId: string; text: string; failed: boolean} | null
}

export interface PanelHost {
  readonly configuration: PanelConfiguration
  /**
   * The application's own examples, declared by the page that hosts the panel -- not by the
   * server, which a new conversation has not spoken to yet. None offers none.
   */
  readonly examples?: readonly PanelExample[]
  snapshot(): PanelSnapshot
  subscribe(listener: (snapshot: PanelSnapshot) => void): () => void
  submit(text: string): void
  retrySubmit(): void
  cancel(): void
  decide(approvalId: string, decision: 'approved' | 'denied'): void
  newConversation(): void
  loadOlder(): void
  dismissNotice(): void
  reload(): void
  /** Report a view-only visibility change to the host, which may clear page markers. */
  panelVisibility?(open: boolean): void
}

const REQUIRED_METHODS = [
  'snapshot', 'subscribe', 'submit', 'retrySubmit', 'cancel', 'decide',
  'newConversation', 'loadOlder', 'dismissNotice', 'reload',
] as const

export function requirePanelHost(candidate: unknown): PanelHost {
  if (typeof candidate !== 'object' || candidate === null) {
    throw new TypeError('panel host must be an object')
  }
  const host = candidate as Record<string, unknown>
  for (const name of REQUIRED_METHODS) {
    if (typeof host[name] !== 'function') {
      throw new TypeError(`panel host is missing ${name}()`)
    }
  }
  const configuration = host.configuration as PanelConfiguration | undefined
  if (!configuration || typeof configuration.protocol !== 'number') {
    throw new TypeError('panel host carries no server configuration')
  }
  if (!configuration.labels || !configuration.limits) {
    throw new TypeError('panel host configuration carries no labels or limits')
  }
  return candidate as PanelHost
}

export type {ApprovalRequest, PanelConfiguration, TimelineEntry, TimelinePage}
