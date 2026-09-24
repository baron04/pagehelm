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
 * The server keys a conversation to this tab (the runtime instance id beside this record), so
 * a tab with no record here has no conversation to show.
 */
import type {PanelSnapshot} from '../bridge/host'

const KEY = 'woa.assistant.engagement.v1'

export type Engagement = 'none' | 'engaged' | 'active'

export interface EngagementRecord {
  read(): Engagement
  /** Update the record from what the panel shows; a tab never goes back to `none`. */
  note(snapshot: PanelSnapshot, connected: boolean): void
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

export function engagementRecord(storage: Storage | null = sessionStorageOrNull()): EngagementRecord {
  let current: Engagement = 'none'
  try {
    const stored = storage?.getItem(KEY)
    if (stored === 'engaged' || stored === 'active') current = stored
  } catch { /* a profile that blocks site data simply starts from nothing each time */ }
  return {
    read: () => current,
    note(snapshot, connected) {
      const next = engagementOf(snapshot, connected, current)
      if (next === current) return
      current = next
      try { storage?.setItem(KEY, next) } catch { /* as above */ }
    },
  }
}

function sessionStorageOrNull(): Storage | null {
  try {
    return typeof sessionStorage === 'undefined' ? null : sessionStorage
  } catch {
    return null
  }
}
