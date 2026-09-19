/**
 * The Runtime's wire and state shapes, mirroring `web_operation_agent/schemas.py`.
 *
 * These were implicit while the Runtime was hand-written JavaScript: every field was
 * whatever the code happened to read, and a value that could be `null` was read as though
 * it never was. Naming them here is what lets the compiler refuse that.
 */

/** What `EmbeddedGateway.bootstrap_config` hands the page. */
export interface RuntimeConfig {
  endpoint: string
  sessionId: string
  clientToken: string
  sessionGeneration: string
  pageControllerModule?: string
  navigationMode?: 'document' | 'spa' | string
  navigationGuardMs?: number
  applicationId?: string
  principalBinding?: string
  principalExpiresAt?: number
  sessionExpiresAt?: number
  sensitiveQueryKeys?: string[]
  panel?: boolean
  panelModule?: string
}

export type JsonObject = Record<string, unknown>

export interface ElementRef {
  ref: string
  role: string
  name: string
  tag: string
  epoch: number
  attrs: Record<string, string>
}

export interface PageObservation {
  schema_version: number
  epoch: number
  url: string
  title: string
  landmarks: string[]
  elements: ElementRef[]
  content: string
  messages: string[]
  truncated: boolean
}

export interface ObservationEnvelope {
  schema_version: number
  session_id: string
  observation_id: string
  page_epoch: number
  observation: PageObservation
  events: JsonObject[]
  caused_by_action_id: string
}

export interface ActionCommand {
  command_id: string
  action: string
  args: JsonObject
  expect_navigation: boolean
}

export interface ActionEnvelope {
  schema_version: number
  session_id: string
  turn_id: string
  action_id: string
  based_on_observation_id: string
  page_epoch: number
  deadline: number
  command_digest: string
  command: ActionCommand
  /** Navigation bookkeeping the Runtime keeps while a command is in flight. */
  [extra: string]: unknown
}

export interface ActionReceipt {
  schema_version: number
  command_id: string
  action: string
  args_echo: JsonObject
  ok: boolean
  error_code: string
  error_detail: string
  url_before: string
  url_after: string
  navigated: boolean
  epoch_before: number
  epoch_after: number
  elapsed_ms: number
  relogin: boolean
  observation: PageObservation | null
}

export interface ActionResultEnvelope {
  schema_version: number
  session_id: string
  turn_id: string
  action_id: string
  based_on_observation_id: string
  page_epoch: number
  command_digest: string
  status: string
  observation_after: ObservationEnvelope | null
  receipt: ActionReceipt | null
  error_code: string
  error_detail: string
}

/** What survives a full page navigation, in sessionStorage. */
export interface StoredState {
  sessionGeneration?: string
  clientId?: string
  epoch?: number
  lastEventId?: string
  inFlight?: ActionEnvelope | null
  cachedResult?: ActionResultEnvelope | null
  approvals?: Record<string, unknown>
  taskInFlight?: boolean
  activeMessageId?: string
  cancellationPending?: boolean
}

/** A failure carrying the server's code and HTTP status, as `_post` raises it. */
export interface RuntimeError extends Error {
  code?: string
  status?: number
}

declare global {
  interface Window {
    __WOA_DOCUMENT_ID__?: string
    /**
     * The one Runtime this page may have. It holds the owning Runtime rather than a flag
     * so that a Runtime can release exactly its own claim (P7.5 J5.05).
     */
    __WOA_EMBEDDED_RUNTIME_STARTED__?: unknown
    __WOA_EMBED_CONFIG__?: RuntimeConfig
    __WOA_RUNTIME_READY__?: Promise<unknown>
  }
}
