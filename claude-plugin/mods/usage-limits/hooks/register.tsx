import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { UsageSnapshot } from '../types'
import {
  SVG_WIDTH,
  altText,
  buildSvg,
  statusText,
  svgHeight,
  terminalBar,
  terminalCells,
  terminalTail,
} from './bar-geometry'
import type { TerminalRun } from './bar-geometry'
import { describeLimits } from './window-pace'
import type { LimitStatus } from './window-pace'

// Draws the account's rate-limit windows above the prompt: one bar per window with a vertical
// pace line (where an even spread of the allowance would be now). Pure reads of
// `$.session.usage()` and drawing; nothing is spawned and nothing leaves the machine.

const REFRESH_MS = 60_000

const snapshot = atom({ plugin: 'toolbelt-usage', key: 'snapshot' } as const, {
  limits: [],
  nowMs: 0,
} as UsageSnapshot)

const THEME_COLOR: Record<LimitStatus, string> = {
  calm: 'success',
  unknown: 'suggestion',
  over: 'warning',
  full: 'error',
}

const debug = ($: EngineInterface, text: string): void => {
  try {
    void $.ui.log(`toolbelt-usage: ${text}`, { to: 'debug' })
  } catch {
    // Nothing to do: a log that fails must not take a hook down.
  }
}

const runColor = (run: TerminalRun, status: LimitStatus): string | undefined =>
  run.kind === 'fill' ? THEME_COLOR[status] : undefined

// Stores the windows with the time of the reading. Never throws.
const store = async ($: EngineInterface, limits: unknown, isStatusLineOn: boolean): Promise<void> => {
  try {
    const nowMs = await $.clock.now()
    const list = Array.isArray(limits) ? limits : []
    await update($, snapshot, () => ({ limits: list, nowMs }))
    if (isStatusLineOn) {
      const rows = describeLimits(list, nowMs)
      $.ui.status(rows.length === 0 ? undefined : statusText(rows))
    }
  } catch (error) {
    debug($, `store failed: ${String(error)}`)
  }
}

const refresh = async ($: EngineInterface, isStatusLineOn: boolean): Promise<void> => {
  try {
    const usage = await $.session.usage()
    await store($, usage.rateLimits, isStatusLineOn)
  } catch (error) {
    debug($, `usage failed: ${String(error)}`)
  }
}

export const register: Register = (on, options) => {
  const isStatusLineOn = options.statusLine === true

  on('session.start', async ($, e, next) => {
    await refresh($, isStatusLineOn)
    // The pace marker moves with time alone, so re-read on a timer too.
    $.clock.every(REFRESH_MS, () => {
      void refresh($, isStatusLineOn)
    })
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    await store($, e.rateLimits, isStatusLineOn)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    try {
      if (e.props.hasSurvey) return next(e)
      if (e.surface !== 'terminal' && e.surface !== 'desktop') return next(e)

      const { limits, nowMs } = await read($, snapshot)
      if (nowMs === 0) return next(e)
      const rows = describeLimits(limits, nowMs)
      if (rows.length === 0) return next(e)

      if (e.surface === 'desktop') {
        const { Box, Svg } = $.ui.resolve(e)
        return (
          <Box>
            <Svg
              source={buildSvg(rows)}
              alt={altText(rows)}
              width={SVG_WIDTH}
              height={svgHeight(rows)}
              isInteractive
            />
          </Box>
        )
      }

      const { Box, Text } = $.ui.resolve(e)
      const columns = e.props.bodyColumns
      const cells = terminalCells(columns)
      const labelWidth = Math.max(...rows.map(row => row.label.length))
      return (
        <Box flexDirection="column">
          {rows.map(row => (
            <Text key={row.kind} wrap="truncate">
              {row.label.padEnd(labelWidth)}{' '}
              {terminalBar(row, cells).map((run, i) => (
                <Text key={String(i)} color={runColor(run, row.status)} dimColor={run.kind === 'track'} bold={run.kind === 'pace'}>
                  {run.text}
                </Text>
              ))}{' '}
              {terminalTail(row, columns, labelWidth, cells)}
            </Text>
          ))}
        </Box>
      )
    } catch (error) {
      debug($, `render failed: ${String(error)}`)
      return next(e)
    }
  })
}
