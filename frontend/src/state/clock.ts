/**
 * When something was said, the way a chat on a phone says it: the time alone today,
 * "yesterday" and the time the day before, the weekday within the past week, the month
 * and day within this year, and the full date before that.
 *
 * Every word comes from `Intl` in the locale the server's copy is written in, so the panel
 * declares no calendar words of its own and a panel in another language needs no change here.
 */
const DAY_MS = 86_400_000

function startOfDay(at: number): number {
  const day = new Date(at)
  day.setHours(0, 0, 0, 0)
  return day.getTime()
}

/** Whole calendar days from `at` to `now`: 0 today, 1 yesterday. */
function daysBefore(at: number, now: number): number {
  return Math.round((startOfDay(now) - startOfDay(at)) / DAY_MS)
}

export function formatTime(at: number, now: number, locale: string): string {
  if (!(at > 0)) return ''
  const time = new Intl.DateTimeFormat(locale, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
    .format(at)
  const days = daysBefore(at, now)
  if (days <= 0) return time
  if (days === 1) {
    const yesterday = new Intl.RelativeTimeFormat(locale, {numeric: 'auto'}).format(-1, 'day')
    return `${yesterday} ${time}`
  }
  const date = new Date(at)
  const day: Intl.DateTimeFormatOptions = days < 7 ? {weekday: 'long'}
    : date.getFullYear() === new Date(now).getFullYear() ? {month: 'long', day: 'numeric'}
    : {year: 'numeric', month: 'long', day: 'numeric'}
  return `${new Intl.DateTimeFormat(locale, day).format(at)} ${time}`
}

/** The whole moment, for a tooltip: the short form leaves the rest out. */
export function formatFullTime(at: number, locale: string): string {
  if (!(at > 0)) return ''
  return new Intl.DateTimeFormat(locale, {dateStyle: 'full', timeStyle: 'medium', hourCycle: 'h23'})
    .format(at)
}
