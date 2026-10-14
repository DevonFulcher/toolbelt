import { describe, expect, test } from 'claude-code/testing'

import {
  describeLimit,
  describeLimits,
  formatPercent,
  formatReset,
  labelFor,
  oneMonthBefore,
  paceFraction,
  paceText,
  statusOf,
} from '../hooks/window-pace'

const HOUR = 3_600_000
const DAY = 24 * HOUR
const iso = (ms: number): string => new Date(ms).toISOString()
const NOW = Date.UTC(2026, 0, 14, 12, 0, 0)

describe('labels', () => {
  test('known kinds and a fallback', () => {
    expect(labelFor('five_hour')).toBe('5 hours')
    expect(labelFor('seven_day')).toBe('7 days')
    expect(labelFor('spend_limit')).toBe('Spend')
    expect(labelFor('seven_day_opus')).toBe('Seven Day Opus')
    expect(labelFor('')).toBe('Limit')
  })
})

describe('pace', () => {
  test('five_hour at start, middle and end', () => {
    const end = NOW + 5 * HOUR
    expect(paceFraction('five_hour', iso(end), NOW)).toBe(0)
    expect(paceFraction('five_hour', iso(NOW + 2.5 * HOUR), NOW)).toBe(0.5)
    expect(paceFraction('five_hour', iso(NOW), NOW)).toBe(1)
  })

  test('seven_day middle', () => {
    expect(paceFraction('seven_day', iso(NOW + 3.5 * DAY), NOW)).toBe(0.5)
  })

  test('a reset in the past is a window that ran out; before the window is 0', () => {
    expect(paceFraction('five_hour', iso(NOW - HOUR), NOW)).toBe(1)
    expect(paceFraction('five_hour', iso(NOW + 9 * HOUR), NOW)).toBe(0)
  })

  test('no resetsAt, a bad one, or an unknown kind: no pace', () => {
    expect(paceFraction('five_hour', undefined, NOW)).toBeNull()
    expect(paceFraction('five_hour', 'soon', NOW)).toBeNull()
    expect(paceFraction('mystery', iso(NOW + HOUR), NOW)).toBeNull()
  })

  test('monthly assumes the calendar month ending at resetsAt', () => {
    // Window Jan 1 -> Feb 1 (31 days): halfway is Jan 16 12:00.
    const feb1 = Date.UTC(2026, 1, 1)
    expect(paceFraction('spend_limit', iso(feb1), Date.UTC(2026, 0, 16, 12))).toBe(0.5)
    // Window Feb 1 -> Mar 1 (28 days): halfway is Feb 15.
    expect(paceFraction('spend_limit', iso(Date.UTC(2026, 2, 1)), Date.UTC(2026, 1, 15))).toBe(0.5)
    // 30-day month (Apr 1 -> May 1), halfway Apr 16.
    expect(paceFraction('spend_limit', iso(Date.UTC(2026, 4, 1)), Date.UTC(2026, 3, 16))).toBe(0.5)
    // Year boundary: Dec 1 -> Jan 1 is 31 days.
    expect(oneMonthBefore(Date.UTC(2027, 0, 1))).toBe(Date.UTC(2026, 11, 1))
    // A kind that says month.
    expect(paceFraction('monthly_credits', iso(feb1), Date.UTC(2026, 0, 16, 12))).toBe(0.5)
  })

  test('month end days clamp to the shorter month', () => {
    expect(oneMonthBefore(Date.UTC(2026, 2, 31))).toBe(Date.UTC(2026, 1, 28))
    expect(oneMonthBefore(Date.UTC(2028, 2, 31))).toBe(Date.UTC(2028, 1, 29))
  })
})

describe('status and text', () => {
  test('status', () => {
    expect(statusOf(30, 0.5)).toBe('calm')
    expect(statusOf(50, 0.5)).toBe('calm')
    expect(statusOf(60, 0.5)).toBe('over')
    expect(statusOf(100, 0.99)).toBe('full')
    expect(statusOf(112, null)).toBe('full')
    expect(statusOf(40, null)).toBe('unknown')
  })

  test('pace text', () => {
    expect(paceText(38, 0.47)).toBe('9% under pace')
    expect(paceText(58, 0.5)).toBe('8% over pace')
    expect(paceText(50, 0.5)).toBe('on pace')
    expect(paceText(50, null)).toBe('')
  })

  test('percent formatting', () => {
    expect(formatPercent(38)).toBe('38%')
    expect(formatPercent(7.5)).toBe('7.5%')
    expect(formatPercent(112)).toBe('112%')
    expect(formatPercent(0)).toBe('0%')
  })

  test('reset formatting: minutes, hours, days, further out, due', () => {
    expect(formatReset(iso(NOW + 30_000), NOW)).toBe('resets in <1m')
    expect(formatReset(iso(NOW + 45 * 60_000), NOW)).toBe('resets in 45m')
    expect(formatReset(iso(NOW + 2 * HOUR + 10 * 60_000), NOW)).toBe('resets in 2h 10m')
    expect(formatReset(iso(NOW + 3 * HOUR), NOW)).toBe('resets in 3h')
    expect(formatReset(iso(NOW - 1), NOW)).toBe('resets now')
    expect(formatReset(undefined, NOW)).toBe('')
    // Local-time forms: build the instant from local fields so the test holds in any timezone.
    const tue = new Date(2026, 0, 20, 14, 0).getTime() // Tuesday
    expect(formatReset(iso(tue), new Date(2026, 0, 17, 9, 0).getTime())).toBe('resets Tue 14:00')
    const far = new Date(2026, 1, 1, 9, 5).getTime()
    expect(formatReset(iso(far), new Date(2026, 0, 14, 9, 0).getTime())).toBe('resets 1 Feb 09:05')
  })
})

describe('describeLimit', () => {
  test('under pace', () => {
    const row = describeLimit({ kind: 'five_hour', percentUsed: 38, resetsAt: iso(NOW + 2.65 * HOUR) }, NOW)
    expect(row.label).toBe('5 hours')
    expect(row.status).toBe('calm')
    expect(Math.abs((row.pace ?? 0) - 0.47)).toBeLessThan(0.005)
    expect(row.paceText).toBe('9% under pace')
    expect(row.fill).toBe(0.38)
    expect(row.summary).toContain('5 hours: 38% used, pace 47%')
  })

  test('over pace', () => {
    const row = describeLimit({ kind: 'seven_day', percentUsed: 80, resetsAt: iso(NOW + 3.5 * DAY) }, NOW)
    expect(row.status).toBe('over')
    expect(row.paceText).toBe('30% over pace')
  })

  test('past 100 fills the bar and reads real', () => {
    const row = describeLimit({ kind: 'spend_limit', percentUsed: 112, resetsAt: iso(NOW + DAY) }, NOW)
    expect(row.fill).toBe(1)
    expect(row.percentText).toBe('112%')
    expect(row.status).toBe('full')
  })

  test('no resetsAt: no pace, and the tooltip says so', () => {
    const row = describeLimit({ kind: 'five_hour', percentUsed: 10 }, NOW)
    expect(row.pace).toBeNull()
    expect(row.detail).toContain('No pace line')
    expect(row.resetText).toBe('')
  })

  test('unknown kind: no pace, labelled from the kind', () => {
    const row = describeLimit({ kind: 'weekly_extra', percentUsed: 10, resetsAt: iso(NOW + DAY) }, NOW)
    expect(row.pace).toBeNull()
    expect(row.label).toBe('Weekly Extra')
    expect(row.detail).toContain('"weekly_extra"')
  })

  test('unusable entries are dropped', () => {
    const rows = describeLimits(
      [
        { kind: 'five_hour', percentUsed: Number.NaN },
        { kind: 'five_hour', percentUsed: -3 },
        { kind: 7, percentUsed: 3 },
        null,
        { kind: 'five_hour', percentUsed: 3 },
      ],
      NOW,
    )
    expect(rows).toHaveLength(1)
  })
})
