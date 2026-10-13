import type { UsageLimit } from '../types'

// Pure helpers for the usage band: labels, pace, status colour and text. No engine calls, so every
// function takes `nowMs` explicitly and is tested with a fixed clock.

const HOUR_MS = 3_600_000
const DAY_MS = 24 * HOUR_MS

export type LimitStatus = 'calm' | 'over' | 'full' | 'unknown'

export type LimitRow = {
  kind: string
  label: string
  /** The real percentage, possibly past 100. */
  usedPercent: number
  /** Fill fraction of the bar, clamped to 0..1. */
  fill: number
  /** Where an even spread would be, 0..1, or null when it cannot be known. */
  pace: number | null
  status: LimitStatus
  /** "38%" */
  percentText: string
  /** "9% under pace", "on pace", or "" when there is no pace. */
  paceText: string
  /** "resets in 2h 10m", "resets Tue 14:00", or "" without `resetsAt`. */
  resetText: string
  /** Sentence for a tooltip. */
  detail: string
  /** Short clause for alt text and the status line. */
  summary: string
}

const KNOWN_LABELS: Record<string, string> = {
  five_hour: '5 hours',
  seven_day: '7 days',
  spend_limit: 'Spend',
}

export const labelFor = (kind: string): string => {
  const known = KNOWN_LABELS[kind]
  if (known !== undefined) return known
  const words = kind.replace(/_/g, ' ').trim()
  if (words === '') return 'Limit'
  return words.replace(/\b\w/g, c => c.toUpperCase())
}

const isMonthly = (kind: string): boolean => kind === 'spend_limit' || /month/i.test(kind)

/** The same instant one calendar month earlier (UTC), the day clamped to the shorter month. */
export const oneMonthBefore = (ms: number): number => {
  const d = new Date(ms)
  const year = d.getUTCFullYear()
  const month = d.getUTCMonth()
  const daysInPreviousMonth = new Date(Date.UTC(year, month, 0)).getUTCDate()
  return Date.UTC(
    year,
    month - 1,
    Math.min(d.getUTCDate(), daysInPreviousMonth),
    d.getUTCHours(),
    d.getUTCMinutes(),
    d.getUTCSeconds(),
    d.getUTCMilliseconds(),
  )
}

/** Window length in ms for a kind ending at `resetsAtMs`, or null when it is not known. */
export const windowLengthMs = (kind: string, resetsAtMs: number): number | null => {
  if (kind === 'five_hour') return 5 * HOUR_MS
  if (kind === 'seven_day') return 7 * DAY_MS
  if (isMonthly(kind)) return resetsAtMs - oneMonthBefore(resetsAtMs)
  return null
}

const clamp = (value: number, low: number, high: number): number =>
  Math.min(high, Math.max(low, value))

const parseTime = (iso: string | undefined): number | null => {
  if (iso === undefined) return null
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms : null
}

/**
 * Fraction of the window elapsed at `nowMs`, 0..1, or null when the window's start is unknown
 * (unknown kind, or no usable `resetsAt`). A `resetsAt` in the past is a window that has run out:
 * 1, never extrapolated.
 */
export const paceFraction = (kind: string, resetsAt: string | undefined, nowMs: number): number | null => {
  const end = parseTime(resetsAt)
  if (end === null) return null
  const length = windowLengthMs(kind, end)
  if (length === null || length <= 0) return null
  return clamp((nowMs - (end - length)) / length, 0, 1)
}

/** "38%", "7.5%", "112%". */
export const formatPercent = (value: number): string => {
  const rounded = Math.round(value * 10) / 10
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`
}

const pad2 = (n: number): string => String(n).padStart(2, '0')
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * "resets in 45m", "resets in 2h 10m", "resets Tue 14:00" (within a week, local time),
 * "resets 1 Nov 14:00" (further out), "resets now" when due or past, "" without a time.
 */
export const formatReset = (resetsAt: string | undefined, nowMs: number): string => {
  const end = parseTime(resetsAt)
  if (end === null) return ''
  const left = end - nowMs
  if (left <= 0) return 'resets now'
  if (left < 60_000) return 'resets in <1m'
  if (left < DAY_MS) {
    const minutes = Math.round(left / 60_000)
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    if (h === 0) return `resets in ${m}m`
    return m === 0 ? `resets in ${h}h` : `resets in ${h}h ${m}m`
  }
  const when = new Date(end)
  const clock = `${pad2(when.getHours())}:${pad2(when.getMinutes())}`
  if (left < 7 * DAY_MS) return `resets ${WEEKDAYS[when.getDay()]} ${clock}`
  return `resets ${when.getDate()} ${MONTHS[when.getMonth()]} ${clock}`
}

export const statusOf = (usedPercent: number, pace: number | null): LimitStatus => {
  if (usedPercent >= 100) return 'full'
  if (pace === null) return 'unknown'
  return usedPercent > pace * 100 ? 'over' : 'calm'
}

export const paceText = (usedPercent: number, pace: number | null): string => {
  if (pace === null) return ''
  const diff = Math.round(usedPercent - pace * 100)
  if (diff === 0) return 'on pace'
  return diff < 0 ? `${-diff}% under pace` : `${diff}% over pace`
}

/** A usable limit: a finite non-negative percentage. Anything else is not drawn. */
export const isUsable = (limit: unknown): limit is UsageLimit => {
  if (typeof limit !== 'object' || limit === null) return false
  const { kind, percentUsed, resetsAt } = limit as Record<string, unknown>
  return (
    typeof kind === 'string' &&
    typeof percentUsed === 'number' &&
    Number.isFinite(percentUsed) &&
    percentUsed >= 0 &&
    (resetsAt === undefined || typeof resetsAt === 'string')
  )
}

export const describeLimit = (limit: UsageLimit, nowMs: number): LimitRow => {
  const usedPercent = limit.percentUsed
  const pace = paceFraction(limit.kind, limit.resetsAt, nowMs)
  const label = labelFor(limit.kind)
  const percentText = formatPercent(usedPercent)
  const paceLabel = paceText(usedPercent, pace)
  const resetText = formatReset(limit.resetsAt, nowMs)

  const paceDetail =
    pace === null
      ? limit.resetsAt === undefined
        ? 'No pace line: the window reports no reset time.'
        : `No pace line: the length of the "${limit.kind}" window is not known.`
      : `Pace ${formatPercent(pace * 100)}: where an even spread of this window would be now (${paceLabel}).`
  const detail = [`${label}: ${percentText} used.`, paceDetail, resetText === '' ? '' : `${capitalize(resetText)}.`]
    .filter(part => part !== '')
    .join(' ')

  const summary = [
    `${label}: ${percentText} used`,
    pace === null ? null : `pace ${formatPercent(pace * 100)}`,
    resetText === '' ? null : resetText,
  ]
    .filter((part): part is string => part !== null)
    .join(', ')

  return {
    kind: limit.kind,
    label,
    usedPercent,
    fill: clamp(usedPercent / 100, 0, 1),
    pace,
    status: statusOf(usedPercent, pace),
    percentText,
    paceText: paceLabel,
    resetText,
    detail,
    summary,
  }
}

const capitalize = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1)

export const describeLimits = (limits: readonly unknown[], nowMs: number): LimitRow[] =>
  limits.filter(isUsable).map(limit => describeLimit(limit, nowMs))
