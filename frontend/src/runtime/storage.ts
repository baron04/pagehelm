/**
 * What survives a full page navigation: the session's cursor, the action in flight, the
 * result not yet delivered, and the decisions already made.
 *
 * It lives in sessionStorage, keyed by the stable session id. A freshly issued capability
 * is a new execution incarnation, so a changed generation discards everything stored
 * under the old one: old cursors, actions and approvals must never cross it.
 */
import type {Redactor} from './redaction'
import {scrubLegacyNavigationUrls} from './navigation'
import type {StoredState} from './types'

/** Every field present, with the type the rest of the Runtime relies on. */
export type SessionState = Required<Omit<StoredState, 'inFlight' | 'cachedResult'>> &
  Pick<StoredState, 'inFlight' | 'cachedResult'>

function storageKey(sessionId: string): string {
  return `woa.remote.v1.${sessionId}`
}

/**
 * Storage the page can actually lose.
 *
 * Safari in private mode throws on `setItem` and a full quota throws on any origin -- and
 * boot used to call this on its way through the constructor, so one throw was an
 * "Oops!" in the host application over a page nothing had gone wrong on. The mirror keeps
 * the current document self-consistent when the real storage refuses: every read answers
 * from it once this document has written the session, because a write the storage refused
 * must not leave reads serving the stale copy behind. Only survival across a full
 * navigation degrades -- the mirror dies with the document, and a page that never lets
 * storage be written simply starts fresh every time.
 */
const mirror = new Map<string, StoredState>()

export function readStored(sessionId: string): StoredState {
  const kept = mirror.get(sessionId)
  if (kept) return kept
  try {
    const raw = sessionStorage.getItem(storageKey(sessionId))
    const parsed: unknown = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? parsed as StoredState : {}
  } catch {
    return {}
  }
}

export function writeStored(sessionId: string, value: StoredState): void {
  mirror.set(sessionId, value)
  try {
    sessionStorage.setItem(storageKey(sessionId), JSON.stringify(value))
  } catch {
    // The mirror above holds it. Nothing here may throw: see the note on `mirror`.
  }
}

/**
 * Drop a session's durable state for good.
 *
 * The stored state carries cached results of up to the observation ceiling, and the key is
 * named by the session id -- so without this, every expired session left a copy of a page
 * read behind in sessionStorage of every tab that had ever opened the panel. The Runtime
 * calls it once the server says the session is over, when nothing can replay from it.
 */
export function clearStored(sessionId: string): void {
  mirror.delete(sessionId)
  try {
    sessionStorage.removeItem(storageKey(sessionId))
  } catch {
    // A storage that throws on read throws on remove; there is nothing left to do.
  }
}

/** The stored state for this incarnation, normalized and written back. */
export function loadSession(sessionId: string, sessionGeneration: string,
                            redactor: Redactor): SessionState {
  if (!sessionGeneration) throw new Error('sessionGeneration is required')
  let stored = readStored(sessionId)
  if (stored.sessionGeneration !== sessionGeneration) stored = {sessionGeneration}
  const state: SessionState = {
    sessionGeneration,
    clientId: stored.clientId || crypto.randomUUID(),
    epoch: Number(stored.epoch || 0),
    lastEventId: stored.lastEventId || '',
    inFlight: scrubLegacyNavigationUrls(stored.inFlight || null, redactor),
    cachedResult: stored.cachedResult || null,
    approvals: stored.approvals || {},
    taskInFlight: Boolean(stored.taskInFlight),
    pageWorked: Boolean(stored.pageWorked),
    activeMessageId: String(stored.activeMessageId || ''),
    cancellationPending: Boolean(stored.cancellationPending),
  }
  writeStored(sessionId, state)
  return state
}
