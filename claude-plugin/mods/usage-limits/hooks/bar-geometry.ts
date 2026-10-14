import type { LimitRow, LimitStatus } from './window-pace'

// Turns LimitRows into an SVG document (desktop) or runs of block characters (terminal). Pure.

export const SVG_WIDTH = 440
const ROW_HEIGHT = 15
const LABEL_X = 0
const BAR_X = 58
const BAR_WIDTH = 120
const BAR_HEIGHT = 6
const TEXT_X = 188

// Mid-tone fills read on a light and a dark theme. Text takes its colour from the stylesheet
// (dark text on a light theme, light text on a dark one); the attribute is the mid-grey fallback
// where the stylesheet is not honoured.
const FILL: Record<LimitStatus, string> = {
  calm: '#2f9e6b',
  unknown: '#3b82c4',
  over: '#d98e04',
  full: '#d64550',
}

const STYLE =
  '.t{fill:#555}.p{stroke:#555}@media (prefers-color-scheme:dark){.t{fill:#d4d4d4}.p{stroke:#e5e5e5}}'

const escapeXml = (text: string): string =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

const trimNumber = (n: number): string => String(Math.round(n * 10) / 10)

const rightText = (row: LimitRow): string =>
  [row.percentText, row.paceText, row.resetText].filter(part => part !== '').join('  ·  ')

export const svgHeight = (rows: readonly LimitRow[]): number => rows.length * ROW_HEIGHT + 4

export const buildSvg = (rows: readonly LimitRow[]): string => {
  const height = svgHeight(rows)
  const body = rows.map((row, i) => {
    const top = 2 + i * ROW_HEIGHT
    const mid = top + ROW_HEIGHT / 2
    const barTop = mid - BAR_HEIGHT / 2
    const fillWidth = row.fill * BAR_WIDTH
    const paceX = row.pace === null ? null : BAR_X + row.pace * BAR_WIDTH
    return [
      `<g>`,
      `<title>${escapeXml(row.detail)}</title>`,
      `<rect x="0" y="${top}" width="${SVG_WIDTH}" height="${ROW_HEIGHT}" fill="transparent"/>`,
      `<text class="t" fill="#888" x="${LABEL_X}" y="${mid + 4}" font-size="11" font-family="system-ui,sans-serif">${escapeXml(row.label)}</text>`,
      `<rect x="${BAR_X}" y="${barTop}" width="${BAR_WIDTH}" height="${BAR_HEIGHT}" rx="1.5" fill="#808080" fill-opacity="0.28"/>`,
      fillWidth > 0
        ? `<rect x="${BAR_X}" y="${barTop}" width="${trimNumber(fillWidth)}" height="${BAR_HEIGHT}" rx="1.5" fill="${FILL[row.status]}"/>`
        : '',
      paceX === null
        ? ''
        : `<line class="p" stroke="#888" x1="${trimNumber(paceX)}" x2="${trimNumber(paceX)}" y1="${barTop - 2}" y2="${barTop + BAR_HEIGHT + 2}" stroke-width="2"/>`,
      `<text class="t" fill="#888" x="${TEXT_X}" y="${mid + 4}" font-size="11" font-family="system-ui,sans-serif">${escapeXml(rightText(row))}</text>`,
      `</g>`,
    ].join('')
  })
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${SVG_WIDTH}" height="${height}" viewBox="0 0 ${SVG_WIDTH} ${height}">` +
    `<style>${STYLE}</style>${body.join('')}</svg>`
  )
}

export const altText = (rows: readonly LimitRow[]): string =>
  rows.map(row => row.summary).join('; ')

export type TerminalRun = { text: string; kind: 'fill' | 'track' | 'pace' }

const FULL = '█'
const EMPTY = '░'
const MARK = '│'

/** The bar as runs of one kind each: filled cells, empty cells, and the pace marker cell. */
export const terminalBar = (row: LimitRow, cells: number): TerminalRun[] => {
  const filled = Math.round(row.fill * cells)
  const marker = row.pace === null ? -1 : Math.min(cells - 1, Math.floor(row.pace * cells))
  const runs: TerminalRun[] = []
  for (let i = 0; i < cells; i += 1) {
    const kind: TerminalRun['kind'] = i === marker ? 'pace' : i < filled ? 'fill' : 'track'
    const text = kind === 'pace' ? MARK : kind === 'fill' ? FULL : EMPTY
    const last = runs[runs.length - 1]
    if (last !== undefined && last.kind === kind) last.text += text
    else runs.push({ text, kind })
  }
  return runs
}

export const terminalTail = (row: LimitRow, columns: number, labelWidth: number, cells: number): string => {
  const room = columns - labelWidth - cells - 4
  const full = rightText(row)
  if (full.length <= room) return full
  const short = [row.percentText, row.paceText].filter(part => part !== '').join('  ·  ')
  return short.length <= room ? short : row.percentText
}

export const terminalCells = (columns: number): number => Math.max(8, Math.min(24, Math.floor(columns / 4)))

/** One line of text for the status line. */
export const statusText = (rows: readonly LimitRow[]): string =>
  rows
    .map(row => `${row.label} ${row.percentText}${row.pace === null ? '' : ` (pace ${Math.round(row.pace * 100)}%)`}`)
    .join(' · ')
