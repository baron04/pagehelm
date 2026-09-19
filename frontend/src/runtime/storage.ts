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

export function readStored(sessionId: string): StoredState {
  try {
    const raw = sessionStorage.getItem(storageKey(sessionId))
    const parsed: unknown = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? parsed as StoredState : {}
  } catch {
    return {}
  }
}

export function writeStored(sessionId: string, value: StoredState): void {
  sessionStorage.setItem(storageKey(sessionId), JSON.stringify(value))
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
    activeMessageId: String(stored.activeMessageId || ''),
    cancellationPending: Boolean(stored.cancellationPending),
  }
  writeStored(sessionId, state)
  return state
}
