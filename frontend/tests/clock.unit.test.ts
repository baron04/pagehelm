import {describe, expect, it} from 'vitest'
import {formatFullTime, formatTime} from '../src/state/clock'

// Local wall-clock dates, so the expectations hold in any time zone the suite runs in.
const at = (year: number, month: number, day: number, hour = 14, minute = 5) =>
  new Date(year, month - 1, day, hour, minute).getTime()
const NOW = at(2026, 9, 24, 16, 30) // a Thursday

describe('formatTime', () => {
  it('says only the time today, on a 24-hour clock', () => {
    expect(formatTime(at(2026, 9, 24, 9, 3), NOW, 'zh-CN')).toBe('09:03')
    expect(formatTime(at(2026, 9, 24, 0, 0), NOW, 'zh-CN')).toBe('00:00')
  })

  it('says yesterday by calendar day, not by 24 hours', () => {
    expect(formatTime(at(2026, 9, 23, 23, 59), NOW, 'zh-CN')).toBe('昨天 23:59')
    expect(formatTime(at(2026, 9, 23, 0, 1), NOW, 'zh-CN')).toBe('昨天 00:01')
  })

  it('names the weekday within the past week', () => {
    expect(formatTime(at(2026, 9, 22), NOW, 'zh-CN')).toBe('星期二 14:05')
    expect(formatTime(at(2026, 9, 18), NOW, 'zh-CN')).toBe('星期五 14:05')
  })

  it('gives the month and day this year, and the year before that', () => {
    expect(formatTime(at(2026, 9, 17), NOW, 'zh-CN')).toBe('9月17日 14:05')
    expect(formatTime(at(2026, 1, 2), NOW, 'zh-CN')).toBe('1月2日 14:05')
    expect(formatTime(at(2025, 12, 31), NOW, 'zh-CN')).toBe('2025年12月31日 14:05')
  })

  it('speaks the locale it is given', () => {
    expect(formatTime(at(2026, 9, 23), NOW, 'en-US')).toBe('yesterday 14:05')
  })

  it('says nothing for a moment it was not told', () => {
    expect(formatTime(0, NOW, 'zh-CN')).toBe('')
    expect(formatFullTime(0, 'zh-CN')).toBe('')
  })

  it('spells the whole moment out for the tooltip', () => {
    expect(formatFullTime(at(2026, 9, 24, 9, 3), 'zh-CN')).toBe('2026年9月24日星期四 09:03:00')
  })
})
