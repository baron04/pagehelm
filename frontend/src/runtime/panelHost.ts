/**
 * The Runtime's side of the panel contract.
 *
 * The panel is a view with no transport, no lease and no session state of its own. This
 * module is the only thing that talks to both: it holds the snapshot the panel renders,
 * turns the panel's intents into authenticated requests, and classifies every transport
 * failure once so that no call site has to guess whether a code is terminal.
 *
 * It lives as long as the page, not as long as a session: the panel is up from the moment the
 * script runs, and a Runtime that connects binds its verbs to this host, and unbinds them when
 * it stops. Until one does, the page's own verbs answer -- start a session, then pass the
 * intent on.
 *
 * Two rules it exists to enforce:
 *   * the timeline is whatever the server projects, never anything this file accumulated
 *     locally -- a full page navigation replays it from `/timeline` (F8a);
 *   * every bound and every user-visible string is the server's contract (F8b): the one
 *     compiled into this script, checked against the hello reply's protocol.
 */
import type {
  ConnectionState, HostNotice, PanelExample, PanelHost as PanelHostContract, PanelSnapshot,
} from '../bridge/host'
import type {
  ApprovalRequest, PanelConfiguration, PanelState, TimelineEntry, TimelinePage,
} from '../contract/panel'
// Not a hand-written `1` any more: `scripts/build-tokens.mjs` lifts it out of the server's
// own `panel_contract.json`, the same source `contract/panel.ts` re-exports for the panel.
import {PANEL_PROTOCOL} from '../contract/protocol.generated'

const RETRY_FLOOR_MS = 250
const RETRY_CEILING_MS = 4000

/** What a failed request carries, whatever threw it. */
export interface TransportFailure {
  code?: unknown
  status?: unknown
}

export interface TransportVerdict {
  terminal: boolean
  code: string
  state: PanelState
  retryable: boolean
  approvalClosed?: true
}

//: A confirmation the server will not take an answer for any more: it expired, it was
//: already answered, or another one replaced it. The decision endpoint reports these as 409,
//: which the rules below would read as "another page took over this session" -- so the
//: runtime stopped, the card stayed on screen with dead buttons, the composer stayed locked,
//: and a reload replayed the same doomed decision and never mounted the panel (P7.5 CR-38).
const APPROVAL_CLOSED = new Set([
  'approval_expired', 'approval_not_pending', 'unknown_approval', 'approval_conflict'])

/**
 * How one failed request ends: is the session over, and what should the panel show?
 *
 * The codes and their panel states come from the server's own configuration, so this
 * cannot drift from `panel_contract.json` the way two hand-written tables would.
 */
export function classifyTransportError(
  error: TransportFailure | null | undefined,
  configuration: Pick<PanelConfiguration,
    'terminal_error_codes' | 'terminal_panel_state_by_code'> | null | undefined,
): TransportVerdict {
  const code = String(error?.code || '')
  if (APPROVAL_CLOSED.has(code)) {
    // The session is fine; only this one card can no longer be answered. The hand-written
    // runtime said `'idle'` here, a state the contract has never had.
    return {terminal: false, code, state: 'ready', retryable: false, approvalClosed: true}
  }
  const status = Number(error?.status || 0)
  const terminalCodes = configuration?.terminal_error_codes ?? []
  const byCode = configuration?.terminal_panel_state_by_code ?? {}
  if (terminalCodes.includes(code)) {
    return {terminal: true, code, state: byCode[code] ?? 'error', retryable: false}
  }
  // A 401 or a 404 on a session endpoint is not a hiccup: the credential or the session is
  // gone, and retrying only turns a finished UI into a reconnect loop.
  if (status === 401) {
    return {terminal: true, code: code || 'unauthorized', state: 'error', retryable: false}
  }
  if (status === 404) {
    return {terminal: true, code: code || 'not_found', state: 'ended', retryable: false}
  }
  if (status === 409) {
    return {terminal: true, code: code || 'runtime_replaced', state: 'replaced', retryable: false}
  }
  if (status === 403) {
    return {terminal: true, code: code || 'origin_refused', state: 'error', retryable: false}
  }
  return {terminal: false, code: code || 'submit_failed', state: 'reconnecting', retryable: true}
}

export function backoffAfter(previous: number): number {
  const base = Math.min(previous ? previous * 2 : RETRY_FLOOR_MS, RETRY_CEILING_MS)
  // Jitter keeps every reconnecting tab from returning in the same millisecond.
  return Math.round(base * (0.8 + Math.random() * 0.4))
}

/** The Runtime's own verbs, which the panel's intents turn into. */
export interface PanelActions {
  submit(text: string): void
  cancel(): void
  decide(approvalId: string, decision: 'approved' | 'denied'): void
  newConversation(): void
  loadOlder(before: string): void
  panelVisibility?(open: boolean): void
}

const REQUIRED_ACTIONS = ['submit', 'cancel', 'decide', 'newConversation', 'loadOlder'] as const

type Optimistic = NonNullable<PanelSnapshot['optimistic']>

export class PanelHost implements PanelHostContract {
  private current: PanelConfiguration
  /** The page's own verbs, answering while no Runtime is bound. */
  private readonly actions: PanelActions
  /** The connected Runtime's verbs, while one is. */
  private bound: PanelActions | null = null
  /** What Retry on a failed start does; without one, Retry reloads the page. */
  private retryStart: (() => void) | null = null
  private readonly listeners = new Set<(snapshot: PanelSnapshot) => void>()
  private state: PanelSnapshot = {
    connection: {state: 'connecting', busy: false, stopping: false, online: true},
    entries: [],
    pendingApproval: null,
    notice: null,
    hasOlder: false,
    loadingOlder: false,
    closed: false,
    optimistic: null,
  }
  private readonly byId = new Map<string, TimelineEntry>()
  /** Sequence below which projected entries belong to a conversation the user ended. */
  private floor = 0
  /** Recovered entries use sequence zero because they belong to an earlier incarnation. */
  private recoveredHistoryVisible = true

  readonly examples: readonly PanelExample[]

  constructor({configuration, actions, examples = []}: {
    configuration: PanelConfiguration
    actions: PanelActions
    examples?: readonly PanelExample[]
  }) {
    requireProtocol(configuration)
    requireActions(actions)
    this.current = configuration
    this.actions = actions
    this.examples = examples
  }

  get configuration(): PanelConfiguration { return this.current }

  private get verbs(): PanelActions { return this.bound ?? this.actions }

  /** A Runtime takes the panel's intents. Returns the release, which undoes only its own bind. */
  bind(actions: PanelActions): () => void {
    requireActions(actions)
    this.bound = actions
    return () => { if (this.bound === actions) this.bound = null }
  }

  /**
   * The server's side of the contract, from a hello reply. The copy and the bounds are the
   * ones compiled in; what the server adds is when this session's identity and the session
   * itself expire.
   */
  adoptServerConfiguration(server: PanelConfiguration): void {
    requireProtocol(server)
    this.current = {
      ...this.current,
      session_expires_at: server.session_expires_at,
      principal_expires_at: server.principal_expires_at,
    }
    this.commit({})
  }

  /** What Retry does for a start that failed; `null` once a session is up. */
  setRetryStart(retry: (() => void) | null): void { this.retryStart = retry }

  snapshot(): PanelSnapshot { return this.state }

  panelVisibility(open: boolean): void { this.verbs.panelVisibility?.(open) }

  subscribe(listener: (snapshot: PanelSnapshot) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private commit(patch: Partial<PanelSnapshot>): void {
    this.state = {...this.state, ...patch}
    for (const listener of this.listeners) listener(this.state)
  }

  // -- fed by the Runtime ----------------------------------------------------------
  setConnection(patch: Partial<ConnectionState>): void {
    this.commit({connection: {...this.state.connection, ...patch}})
  }

  /** Merge one projected page. Identity is the server's event id, so this is idempotent. */
  applyTimeline(page: Partial<TimelinePage> | null | undefined,
                {prepend = false}: {prepend?: boolean} = {}): void {
    const incoming = Array.isArray(page?.entries) ? page.entries : []
    const fresh = incoming.filter(entry => !this.byId.has(entry.id) && (
      entry.replayed ? this.recoveredHistoryVisible : entry.sequence > this.floor))
    for (const entry of fresh) this.byId.set(entry.id, entry)
    let entries = this.state.entries
    if (fresh.length) {
      entries = prepend ? [...fresh, ...entries] : [...entries, ...fresh]
    }
    const budget = this.configuration.limits.rendered_entry_budget
    if (entries.length > budget) {
      const dropped = entries.slice(0, entries.length - budget)
      for (const entry of dropped) this.byId.delete(entry.id)
      entries = entries.slice(entries.length - budget)
    }
    // The server's own `message_accepted` is what retires the local echo. Leaving both in
    // place showed the user their message twice for as long as the round trip took.
    const echo = this.state.optimistic
    const accepted = echo !== null && fresh.some(
      entry => entry.kind === 'goal' && entry.turn_id === echo.turnId)
    this.commit({
      entries,
      optimistic: accepted ? null : echo,
      pendingApproval: page?.pending_approval ?? null,
      hasOlder: prepend ? Boolean(page?.has_more) : this.state.hasOlder || Boolean(page?.has_more),
      loadingOlder: false,
      closed: Boolean(page?.closed) || this.state.closed,
    })
  }

  /**
   * No session, and none needed: what a session said about itself -- its state, a notice, a
   * card it held, a conversation it closed -- no longer applies. The conversation on screen
   * stays; the next message makes a new session.
   */
  rest(): void {
    this.commit({
      connection: {...this.state.connection, state: 'ready', busy: false, stopping: false},
      notice: null, pendingApproval: null, closed: false,
    })
  }

  setPendingApproval(request: ApprovalRequest | null | undefined): void {
    this.commit({pendingApproval: request ?? null})
  }

  /**
   * Drop everything above a new conversation.
   *
   * The panel's *view* is what clears. The server's event log is untouched -- it is the
   * record of what this session did, and a person changing topic does not unmake it, nor
   * could the panel delete it if they wanted to. The floor stops the next projected page
   * from paging the old turns straight back in.
   */
  clearTimeline(): void {
    const latest = this.state.entries.at(-1)
    this.floor = Math.max(this.floor, latest ? latest.sequence : 0)
    this.recoveredHistoryVisible = false
    this.byId.clear()
    this.commit({entries: [], hasOlder: false, loadingOlder: false, optimistic: null})
  }

  setOptimistic(optimistic: Optimistic | null): void { this.commit({optimistic}) }

  failOptimistic(): void {
    if (!this.state.optimistic) return
    this.commit({optimistic: {...this.state.optimistic, failed: true}})
  }

  clearOptimisticOnce(turnId: string): void {
    if (this.state.optimistic?.turnId === turnId) this.commit({optimistic: null})
  }

  raise(code: string, {retryable = true, turnId = '', message = ''}:
        {retryable?: boolean; turnId?: string; message?: string} = {}): void {
    const notice: HostNotice = {code, retryable}
    if (turnId) notice.turnId = turnId
    if (message) notice.message = message
    this.commit({notice})
  }

  clearNotice(): void { this.commit({notice: null}) }

  setLoadingOlder(value: boolean): void { this.commit({loadingOlder: Boolean(value)}) }

  // -- driven by the panel ---------------------------------------------------------
  submit(text: string): void {
    this.clearNotice()
    this.verbs.submit(String(text || ''))
  }

  retrySubmit(): void {
    const pending = this.state.optimistic
    if (!pending) return
    this.commit({optimistic: {...pending, failed: false}})
    this.verbs.submit(pending.text)
  }

  cancel(): void { this.verbs.cancel() }

  decide(approvalId: string, decision: 'approved' | 'denied'): void {
    this.verbs.decide(String(approvalId), decision)
  }

  newConversation(): void {
    this.clearTimeline()
    this.clearNotice()
    this.verbs.newConversation()
  }

  loadOlder(): void {
    if (this.state.loadingOlder || !this.state.hasOlder) return
    const oldest = this.state.entries[0]
    this.setLoadingOlder(true)
    this.verbs.loadOlder(oldest ? oldest.id : '')
  }

  dismissNotice(): void { this.clearNotice() }

  reload(): void {
    if (this.retryStart) {
      this.clearNotice()
      this.retryStart()
      return
    }
    if (typeof location !== 'undefined') location.reload()
  }
}

function requireProtocol(configuration: PanelConfiguration | null | undefined): void {
  if (!configuration || typeof configuration.protocol !== 'number') {
    throw new Error('panel host requires the server panel configuration')
  }
  if (configuration.protocol !== PANEL_PROTOCOL) {
    throw new Error(
      `runtime speaks panel protocol ${PANEL_PROTOCOL}, server sent ${configuration.protocol}`)
  }
}

function requireActions(actions: PanelActions | null | undefined): void {
  for (const name of REQUIRED_ACTIONS) {
    if (typeof actions?.[name] !== 'function') {
      throw new Error(`panel host actions are missing ${name}()`)
    }
  }
}
