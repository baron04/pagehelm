/**
 * The Runtime's side of the panel contract.
 *
 * The panel is a view with no transport, no lease and no session state of its own. This
 * module is the only thing that talks to both: it holds the snapshot the panel renders,
 * turns the panel's intents into authenticated requests, and classifies every transport
 * failure once so that no call site has to guess whether a code is terminal.
 *
 * Two rules it exists to enforce:
 *   * the timeline is whatever the server projects, never anything this file accumulated
 *     locally -- a full page navigation replays it from `/timeline` (F8a);
 *   * every bound and every user-visible string comes from the hello reply (F8b).
 */

export const PANEL_PROTOCOL = 1

const RETRY_FLOOR_MS = 250
const RETRY_CEILING_MS = 4000

/**
 * How one failed request ends: is the session over, and what should the panel show?
 *
 * The codes and their panel states come from the server's own configuration, so this
 * cannot drift from `panel_contract.json` the way two hand-written tables would.
 */
export function classifyTransportError(error, configuration) {
  const code = String(error?.code || '')
  const status = Number(error?.status || 0)
  const terminalCodes = configuration?.terminal_error_codes || []
  const byCode = configuration?.terminal_panel_state_by_code || {}
  if (terminalCodes.includes(code)) {
    return {terminal: true, code, state: byCode[code] || 'error', retryable: false}
  }
  // A 401 or a 404 on a session endpoint is not a hiccup: the credential or the session is
  // gone, and retrying only turns a finished UI into a reconnect loop.
  if (status === 401) return {terminal: true, code: code || 'unauthorized', state: 'error', retryable: false}
  if (status === 404) return {terminal: true, code: code || 'not_found', state: 'ended', retryable: false}
  if (status === 409) {
    return {terminal: true, code: code || 'runtime_replaced', state: 'replaced', retryable: false}
  }
  if (status === 403) return {terminal: true, code: code || 'origin_refused', state: 'error', retryable: false}
  return {terminal: false, code: code || 'submit_failed', state: 'reconnecting', retryable: true}
}

export function backoffAfter(previous) {
  const base = Math.min(previous ? previous * 2 : RETRY_FLOOR_MS, RETRY_CEILING_MS)
  // Jitter keeps every reconnecting tab from returning in the same millisecond.
  return Math.round(base * (0.8 + Math.random() * 0.4))
}

export class PanelHost {
  /**
   * @param {object} options
   * @param {object} options.configuration the `panel` block of the hello reply
   * @param {object} options.actions the Runtime's own verbs; see EmbeddedWebRuntime
   */
  constructor({configuration, actions}) {
    if (!configuration || typeof configuration.protocol !== 'number') {
      throw new Error('panel host requires the server panel configuration')
    }
    if (configuration.protocol !== PANEL_PROTOCOL) {
      throw new Error(
        `runtime speaks panel protocol ${PANEL_PROTOCOL}, server sent ${configuration.protocol}`)
    }
    for (const name of ['submit', 'cancel', 'decide', 'newConversation', 'loadOlder']) {
      if (typeof actions?.[name] !== 'function') {
        throw new Error(`panel host actions are missing ${name}()`)
      }
    }
    this.configuration = configuration
    this._actions = actions
    this._listeners = new Set()
    this._state = {
      connection: {state: 'connecting', busy: false, stopping: false, online: true},
      entries: [],
      pendingApproval: null,
      notice: null,
      hasOlder: false,
      loadingOlder: false,
      closed: false,
      optimistic: null,
    }
    this._byId = new Map()
  }

  snapshot() { return this._state }

  subscribe(listener) {
    this._listeners.add(listener)
    return () => this._listeners.delete(listener)
  }

  _commit(patch) {
    this._state = {...this._state, ...patch}
    for (const listener of this._listeners) listener(this._state)
  }

  // -- fed by the Runtime ----------------------------------------------------------
  setConnection(patch) {
    this._commit({connection: {...this._state.connection, ...patch}})
  }

  /** Merge one projected page. Identity is the server's event id, so this is idempotent. */
  applyTimeline(page, {prepend = false} = {}) {
    const incoming = Array.isArray(page?.entries) ? page.entries : []
    const fresh = incoming.filter(entry => !this._byId.has(entry.id))
    for (const entry of fresh) this._byId.set(entry.id, entry)
    let entries = this._state.entries
    if (fresh.length) {
      entries = prepend ? [...fresh, ...entries] : [...entries, ...fresh]
    }
    const budget = this.configuration.limits.rendered_entry_budget
    if (entries.length > budget) {
      const dropped = entries.slice(0, entries.length - budget)
      for (const entry of dropped) this._byId.delete(entry.id)
      entries = entries.slice(entries.length - budget)
    }
    // The server's own `message_accepted` is what retires the local echo. Leaving both in
    // place showed the user their message twice for as long as the round trip took.
    const echo = this._state.optimistic
    const accepted = echo !== null && fresh.some(
      entry => entry.kind === 'goal' && entry.turn_id === echo.turnId)
    this._commit({
      entries,
      optimistic: accepted ? null : echo,
      pendingApproval: page?.pending_approval ?? null,
      hasOlder: prepend ? Boolean(page?.has_more) : this._state.hasOlder || Boolean(page?.has_more),
      loadingOlder: false,
      closed: Boolean(page?.closed) || this._state.closed,
    })
  }

  setPendingApproval(request) { this._commit({pendingApproval: request ?? null}) }

  setOptimistic(optimistic) { this._commit({optimistic}) }

  failOptimistic() {
    if (!this._state.optimistic) return
    this._commit({optimistic: {...this._state.optimistic, failed: true}})
  }

  clearOptimisticOnce(turnId) {
    if (this._state.optimistic?.turnId === turnId) this._commit({optimistic: null})
  }

  raise(code, {retryable = true, turnId = ''} = {}) {
    this._commit({notice: {code, retryable, ...(turnId ? {turnId} : {})}})
  }

  clearNotice() { this._commit({notice: null}) }

  setLoadingOlder(value) { this._commit({loadingOlder: Boolean(value)}) }

  // -- driven by the panel ---------------------------------------------------------
  submit(text) {
    this.clearNotice()
    this._actions.submit(String(text || ''))
  }

  retrySubmit() {
    const pending = this._state.optimistic
    if (!pending) return
    this._commit({optimistic: {...pending, failed: false}})
    this._actions.submit(pending.text)
  }

  cancel() { this._actions.cancel() }

  decide(approvalId, decision) { this._actions.decide(String(approvalId), decision) }

  newConversation() { this._actions.newConversation() }

  loadOlder() {
    if (this._state.loadingOlder || !this._state.hasOlder) return
    const oldest = this._state.entries[0]
    this.setLoadingOlder(true)
    this._actions.loadOlder(oldest ? oldest.id : '')
  }

  dismissNotice() { this.clearNotice() }

  reload() {
    if (typeof location !== 'undefined') location.reload()
  }
}
