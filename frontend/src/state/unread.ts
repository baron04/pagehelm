/**
 * What the launcher's number counts: answers that arrived while the panel was put away.
 *
 * It used to count every line added to the timeline -- the person's own message, each step,
 * the passing "正在思考" -- so one turn finished behind the launcher showed 4. And it counted
 * from what the document had when it loaded, so a refresh, which starts collapsed, counted
 * the whole conversation the session handed back as new.
 *
 * So it counts answers, and only those after the last one the person had the panel open
 * for. That mark is the server's own time on the entry, kept in `sessionStorage` so it
 * survives the page's navigations and dies with the tab, as the conversation does.
 */
import type {TimelineEntry} from '../contract/panel'

const KEY = 'woa.panel.seen.v1'

function isAnswer(entry: TimelineEntry): boolean {
  return entry.kind === 'result' && !entry.transient
}

/** When the newest answer here arrived, or 0 when there is none. */
export function latestAnswer(entries: readonly TimelineEntry[]): number {
  let latest = 0
  for (const entry of entries) if (isAnswer(entry) && entry.at > latest) latest = entry.at
  return latest
}

export function unreadAnswers(entries: readonly TimelineEntry[], seen: number): number {
  let count = 0
  for (const entry of entries) if (isAnswer(entry) && entry.at > seen) count += 1
  return count
}

export function loadSeen(): number {
  try {
    const value = Number(sessionStorage.getItem(KEY))
    return Number.isFinite(value) && value > 0 ? value : 0
  } catch {
    return 0
  }
}

export function rememberSeen(value: number): void {
  try {
    sessionStorage.setItem(KEY, String(value))
  } catch { /* storage refused: the mark lasts as long as this document */ }
}
