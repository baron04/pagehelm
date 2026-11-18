/**
 * How far this tab has gone with the assistant, kept in `sessionStorage` so it survives the
 * page's own navigations and dies with the tab.
 *
 * It decides when a session is made. A tab that has said nothing makes none: the launcher is
 * on the page and nothing is connected until the person sends something. A tab that has a
 * conversation reconnects when the panel is opened, to show it. A tab in the middle of a turn
 * -- the agent opened another page, or a confirmation is waiting -- reconnects at once,
 * without being asked, since the turn cannot go on otherwise.
 *
 * The session identifier is the tab's alone and is never put in `localStorage`: each tab,
 * browser and device holds its own conversation, so one tab has nothing to hand another.
 * A key written by an older version is removed on sight rather than reused, since a session
 * remembered for the whole site is exactly the sharing this record no longer does.
 */
import type {PanelSnapshot} from '../bridge/host'

const KEY = 'woa.assistant.engagement.v1'
const SESSION_KEY = 'woa.assistant.session.v1'

type Engagement = 'none' | 'engaged' | 'active'

interface EngagementRecord {
  read(): Engagement
  sessionId(): string
  remember(sessionId: string): void
  /** Update the record from what the panel shows; a tab never goes back to `none`. */
  note(snapshot: PanelSnapshot, connected: boolean): void
  /** Forget the session this tab held, for a new conversation or a detected duplicate. */
  clear(): void
}

export function engagementOf(snapshot: PanelSnapshot, connected: boolean,
                             previous: Engagement): Engagement {
  // Only a connected session knows whether a turn is under way. A page the agent has just
  // opened shows nothing yet; reading `idle` off it would forget the turn it has to resume.
  if (!connected) {
    return previous === 'none' && (snapshot.entries.length || snapshot.optimistic)
      ? 'engaged' : previous
  }
  const {connection, pendingApproval} = snapshot
  return connection.busy || connection.stopping || pendingApproval ? 'active' : 'engaged'
}

export function engagementRecord(storage: Storage | null = storageOrNull('sessionStorage'),
                                 site: Storage | null = storageOrNull('localStorage')): EngagementRecord {
  let current: Engagement = 'none'
  let session = ''
  try {
    session = storage?.getItem(SESSION_KEY) || ''
    if (site?.getItem(SESSION_KEY)) site.removeItem(SESSION_KEY)
    const stored = storage?.getItem(KEY)
    if (session) current = stored === 'active' ? 'active' : 'engaged'
  } catch { /* a profile that blocks site data simply starts from nothing each time */ }
  return {
    read: () => current,
    sessionId: () => session,
    remember(sessionId) {
      session = sessionId
      if (current === 'none') current = 'engaged'
      try { storage?.setItem(SESSION_KEY, session) } catch { /* this page's copy holds */ }
    },
    note(snapshot, connected) {
      const next = engagementOf(snapshot, connected, current)
      if (next === current) return
      current = next
      try {
        storage?.setItem(KEY, next)
      } catch { /* as above */ }
    },
    clear() {
      current = 'none'
      session = ''
      try { storage?.removeItem(KEY) } catch { /* this page's copy holds */ }
      try { storage?.removeItem(SESSION_KEY) } catch { /* this page's copy holds */ }
      try { site?.removeItem(SESSION_KEY) } catch { /* a leftover site copy goes too */ }
    },
  }
}

function storageOrNull(name: 'sessionStorage' | 'localStorage'): Storage | null {
  try {
    return typeof globalThis[name] === 'undefined' ? null : globalThis[name]
  } catch {
    return null
  }
}
