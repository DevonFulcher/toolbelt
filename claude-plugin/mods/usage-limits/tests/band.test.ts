import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const PLUGIN = 'toolbelt-usage'
const NOW = Date.UTC(2026, 0, 14, 12, 0, 0)
const HOUR = 3_600_000
const DAY = 24 * HOUR
const iso = (ms: number): string => new Date(ms).toISOString()

type Limit = { kind: string; percentUsed: number; resetsAt?: string }

const THREE: Limit[] = [
  { kind: 'five_hour', percentUsed: 38, resetsAt: iso(NOW + 2.65 * HOUR) },
  { kind: 'seven_day', percentUsed: 80, resetsAt: iso(NOW + 3.5 * DAY) },
  { kind: 'spend_limit', percentUsed: 112, resetsAt: iso(Date.UTC(2026, 1, 1)) },
]

const props = { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100 } as never

const START = { cwd: '/w', surface: 'terminal', isInteractive: true } as never

// `claude plugin test` on the marketplace folder runs every *.test.ts beneath it against the
// claims plugin too. The engine stamps the plugin's folder as the classic envelope's cwd, so a
// test can tell whether it is running against this mod; against another plugin it returns early.
let ownFolder = ''
const isOwnFolder = async ($: Engine): Promise<boolean> => {
  ownFolder = ''
  await $.classic.SessionStart({ source: 'startup' })
  return ownFolder.endsWith('/usage-limits')
}

// The bottom of the chain: what the engine would answer under the plugin.
const bottom = (on: On, limits: unknown) => {
  const statuses: (string | undefined)[] = []
  on('classic.SessionStart', (_$, e) => {
    ownFolder = e.cwd
    return {} as never
  })
  on('session.usage', () => {
    if (limits instanceof Error) throw limits
    return { value: { startedAt: NOW, context: {}, rateLimits: limits } } as never
  })
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.measure', (_$, e) => ({ changed: e.changed }))
  on('ui.status', (_$, e) => {
    statuses.push(e.text)
    return { value: undefined } as never
  })
  on('ui.log', () => ({ value: undefined }) as never)
  // Stands for the engine's own band: the plugin drew nothing when only this marker is in the tree.
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['engine band'] }) as never)
  return statuses
}

const setup = (on: On, limits: unknown) => {
  mock.clock(on, { now: NOW })
  return bottom(on, limits)
}

const feed = (engine: Engine, limits: unknown) =>
  engine.session.measure({ context: {}, rateLimits: limits, changed: ['rateLimits'] } as never)

describe('band', () => {
  for (const surface of ['terminal', 'desktop'] as const) {
    test(`${surface}: no limits draws nothing`, async ($, on) => {
      setup(on, [])
      if (!(await isOwnFolder($))) return
      await feed($, [])
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'AbovePrompt', props })
      expect(await ui.findAll({ type: 'Svg' })).toHaveLength(0)
      expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['engine band'])
    })

    test(`${surface}: bad data does not throw and draws nothing`, async ($, on) => {
      setup(on, 'nonsense')
      if (!(await isOwnFolder($))) return
      await feed($, [{ kind: 'five_hour', percentUsed: Number.NaN }, null, { kind: 3 }])
      const ui = await $.ui.mount({ plugin: PLUGIN, surface, component: 'AbovePrompt', props })
      expect(await ui.findAll({ type: 'Svg' })).toHaveLength(0)
      expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['engine band'])
    })

    test(`${surface}: yields to a survey`, async ($, on) => {
      setup(on, THREE)
      if (!(await isOwnFolder($))) return
      await feed($, THREE)
      const ui = await $.ui.mount({
        plugin: PLUGIN,
        surface,
        component: 'AbovePrompt',
        props: { ...(props as object), hasSurvey: true } as never,
      })
      expect(await ui.findAll({ type: 'Svg' })).toHaveLength(0)
      expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['engine band'])
    })
  }

  test('desktop: one Svg with every window in the alt text and the pace lines', async ($, on) => {
    setup(on, THREE)
    if (!(await isOwnFolder($))) return
    await feed($, THREE)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', component: 'AbovePrompt', props })
    const svgs = await ui.findAll({ type: 'Svg' })
    expect(svgs).toHaveLength(1)
    const alt = String(svgs[0].props.alt)
    expect(alt).toContain('5 hours: 38% used, pace 47%, resets in 2h 39m')
    expect(alt).toContain('7 days: 80% used, pace 50%')
    expect(alt).toContain('Spend: 112% used')
    const source = String(svgs[0].props.source)
    expect(source.match(/<line /g)).toHaveLength(3)
    expect(source).toContain('112%')
    expect(source).toContain('30% over pace')
  })

  test('desktop: a limit with no reset time draws no pace line and says so', async ($, on) => {
    const limits = [{ kind: 'five_hour', percentUsed: 10 }]
    setup(on, limits)
    if (!(await isOwnFolder($))) return
    await feed($, limits)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', component: 'AbovePrompt', props })
    const source = String((await ui.find({ type: 'Svg' }))?.props.source)
    expect(source).not.toContain('<line ')
    expect(source).toContain('No pace line')
  })

  test('terminal: a row per window with bar characters and a pace marker', async ($, on) => {
    setup(on, THREE)
    if (!(await isOwnFolder($))) return
    await feed($, THREE)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props })
    const rows = await ui.findAll({ type: 'Text', text: /used|%/ })
    const text = rows.map(row => row.text).join('\n')
    expect(text).toContain('5 hours')
    expect(text).toContain('7 days')
    expect(text).toContain('Spend')
    expect(text).toContain('█')
    expect(text).toContain('░')
    expect(text).toContain('│')
    expect(text).toContain('112%')
    expect(text).toContain('9% under pace')
  })

  test('a refresh on the timer moves the pace with time alone', async ($, on) => {
    const clock = mock.clock(on, { now: NOW })
    bottom(on, [{ kind: 'five_hour', percentUsed: 38, resetsAt: iso(NOW + 2.5 * HOUR) }])
    if (!(await isOwnFolder($))) return
    await $.session.start(START)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', component: 'AbovePrompt', props })
    expect(String((await ui.find({ type: 'Svg' }))?.props.alt)).toContain('pace 50%')
    await clock.advance(30 * 60_000)
    const after = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', component: 'AbovePrompt', props })
    expect(String((await after.find({ type: 'Svg' }))?.props.alt)).toContain('pace 60%')
  })

  test('a usage() that rejects does not break the session start', async ($, on) => {
    mock.clock(on, { now: NOW })
    bottom(on, new Error('no usage'))
    if (!(await isOwnFolder($))) return
    await $.session.start(START)
    const ui = await $.ui.mount({ plugin: PLUGIN, surface: 'desktop', component: 'AbovePrompt', props })
    expect(await ui.findAll({ type: 'Svg' })).toHaveLength(0)
    expect((await ui.findAll({ type: 'Text' })).map(t => t.text)).toEqual(['engine band'])
  })

  test('statusLine option keeps a one-line summary', { options: { statusLine: true } }, async ($, on) => {
    const statuses = setup(on, THREE)
    if (!(await isOwnFolder($))) return
    await feed($, THREE)
    expect(statuses.at(-1)).toBe('5 hours 38% (pace 47%) · 7 days 80% (pace 50%) · Spend 112% (pace 44%)')
  })
})
