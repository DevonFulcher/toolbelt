import type { EngineInterface, Register } from 'claude-code'

// Writes claim files (contract v1, docs/task-claims.md) recording which tracked
// worktrees and refs this session is working on. The files are the only
// interface to toolbelt: this module never spawns `tt` or any process, and the
// per-tool-call hooks only test a string and update a map. Files are written
// from a timer, never from the call itself.

type ClaimKind = 'worktree' | 'ref'
type HolderState = 'working' | 'idle' | 'blocked'

type Claim = { kind: ClaimKind; value: string; touchedAt: number }

type Holder = {
  agentId: string | null
  state: HolderState
  startedAt: number
  // last hook event seen for this holder; a subagent quiet for SUBAGENT_QUIET_MS is dropped
  lastActiveAt: number
  cwd: string | null
  claims: Map<string, Claim>
  isDirty: boolean
}

type Config = {
  claimsDir: string
  worktreesRoot: string | null
  link: string | null
}

const TTL_SECONDS: Record<HolderState, number> = { working: 180, idle: 1800, blocked: 1800 }
const HEARTBEAT_MS = 30_000
// A subagent's claim lives from SubagentStart to SubagentStop. If SubagentStop never
// arrives (the subagent was killed) while the Claude process lives, the heartbeat
// would keep the claim fresh forever, so a subagent with no hook event for this long
// is released. Its next event claims again.
const SUBAGENT_QUIET_MS = 2 * 60 * 60 * 1000
const MAX_REFS_PER_CALL = 50
const MAX_REF_LENGTH = 200
const TASK_CLAIM_TOOL = 'mcp__toolbelt-claims__task_claim'
const MAIN = ''

const HOLDER_UNSAFE = /[^A-Za-z0-9._-]/g

// Module state is per load. Claims are turn-scoped, so a hot reload losing it
// costs nothing that a file's TTL does not already cover.
let config: Config | null = null
let sessionId: string | null = null
let sessionCwd: string | null = null
let clockOffset = 0
let timer: { cancel: () => void } | null = null
let isFlushScheduled = false
let shownCount = -1
const holders = new Map<string, Holder>()

const now = (): number => Date.now() + clockOffset

const iso = (ms: number): string => new Date(ms).toISOString()

const holderName = (agentId: string): string => {
  const base = (sessionId ?? 'unknown').replace(HOLDER_UNSAFE, '_')
  return agentId === MAIN ? base : `${base}.${agentId.replace(HOLDER_UNSAFE, '_')}`
}

const debug = ($: EngineInterface, text: string): void => {
  try {
    void $.ui.log(`toolbelt-claims: ${text}`, { to: 'debug' })
  } catch {
    // logging must never fail a hook
  }
}

const loadConfig = async ($: EngineInterface): Promise<void> => {
  if (config !== null) return
  const explicit = await $.env.get('TOOLBELT_HOME')
  const home = await $.env.get('HOME')
  const workdir = await $.env.get('GIT_PROJECTS_WORKDIR')
  const host = await $.env.get('CLAUDE_CODE_HOST_SESSION_ID')
  const base = explicit ?? (home === undefined ? undefined : `${home}/.toolbelt`)
  if (base === undefined) return
  config = {
    claimsDir: `${base.replace(/\/+$/, '')}/claims`,
    worktreesRoot: workdir ? `${workdir.replace(/\/+$/, '')}/wt` : null,
    link: host ? `claude://claude.ai/epitaxy/${host}` : null,
  }
}

const syncClock = async ($: EngineInterface): Promise<void> => {
  clockOffset = (await $.clock.now()) - Date.now()
}

const ensureSession = async ($: EngineInterface): Promise<void> => {
  await loadConfig($)
  sessionId = await $.session.id()
  if (sessionCwd === null) sessionCwd = await $.session.cwd()
  await syncClock($)
}

const holderFor = (agentId: string | undefined): Holder => {
  const key = agentId ?? MAIN
  let holder = holders.get(key)
  if (holder === undefined) {
    holder = {
      agentId: agentId ?? null,
      state: 'working',
      startedAt: now(),
      lastActiveAt: now(),
      cwd: null,
      claims: new Map(),
      isDirty: true,
    }
    holders.set(key, holder)
  }
  return holder
}

// ---- path to worktree (pure, no fs) ----

const normalize = (path: string): string => {
  const out: string[] = []
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') out.pop()
    else out.push(part)
  }
  return `/${out.join('/')}`
}

// <root>/<repo>/<name> for any absolute path at or below a worktree; the
// fallback (no GIT_PROJECTS_WORKDIR) is the first `/wt/` path segment. A file
// path needs a segment below the worktree, else it is a file of the repo dir.
const worktreeOf = (path: string, isFile: boolean): string | null => {
  if (!path.startsWith('/')) return null
  const clean = normalize(path)
  let base: string
  let rest: string
  if (config !== null && config.worktreesRoot !== null) {
    const root = normalize(config.worktreesRoot)
    if (!clean.startsWith(`${root}/`)) return null
    base = root
    rest = clean.slice(root.length + 1)
  } else {
    const match = /^(.*?\/wt)\/(.+)$/.exec(clean)
    if (match === null) return null
    base = match[1] as string
    rest = match[2] as string
  }
  const [repo, name, ...below] = rest.split('/')
  if (!repo || !name || (isFile && below.length === 0)) return null
  return `${base}/${repo}/${name}`
}

// ---- touches: string test + map update, then a scheduled flush ----

const scheduleFlush = ($: EngineInterface): void => {
  if (isFlushScheduled) return
  isFlushScheduled = true
  $.clock.after(0, () => {
    void flush($)
  })
}

const touch = ($: EngineInterface, agentId: string | undefined, kind: ClaimKind, value: string): void => {
  const holder = holderFor(agentId)
  const key = `${kind}:${value}`
  const isNew = !holder.claims.has(key)
  holder.claims.set(key, { kind, value, touchedAt: now() })
  holder.lastActiveAt = now()
  if (holder.state === 'working' && timer === null) startTimer($)
  if (isNew) {
    holder.isDirty = true
    scheduleFlush($)
  }
}

const touchPath = ($: EngineInterface, agentId: string | undefined, path: string, isFile: boolean): void => {
  const worktree = worktreeOf(path, isFile)
  if (worktree !== null) touch($, agentId, 'worktree', worktree)
}

const bashDirectories = (command: string, cwd: string | null): string[] => {
  const dirs: string[] = []
  const resolve = (path: string): string | null =>
    path.startsWith('/') ? path : cwd === null ? null : `${cwd}/${path}`
  const lead = /^\s*cd\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|"']+))\s*(?:&&|;)/.exec(command)
  const target = lead === null ? null : (lead[1] ?? lead[2] ?? lead[3] ?? null)
  const entry = target === null ? cwd : resolve(target)
  if (entry !== null) dirs.push(entry)
  const git = /\bgit\s+-C\s+(?:"([^"]+)"|'([^']+)'|(\S+))/.exec(command)
  const gitTarget = git === null ? null : (git[1] ?? git[2] ?? git[3] ?? null)
  const gitDir = gitTarget === null ? null : resolve(gitTarget)
  if (gitDir !== null) dirs.push(gitDir)
  return dirs
}

// ---- files ----

const dropRevoked = async ($: EngineInterface, name: string, holder: Holder): Promise<void> => {
  if (config === null) return
  let text: string
  try {
    const read = await $.fs.read(`${config.claimsDir}/revoked/${name}.json`)
    if (typeof read !== 'string') return
    text = read
  } catch {
    return // no revocation file
  }
  try {
    const parsed: unknown = JSON.parse(text)
    const list = (parsed as { revoked?: unknown }).revoked
    if (!Array.isArray(list)) return
    for (const item of list) {
      const entry = item as { kind?: unknown; path?: unknown; ref?: unknown; at?: unknown }
      const at = typeof entry.at === 'string' ? Date.parse(entry.at) : Number.NaN
      if (Number.isNaN(at)) continue
      const value = entry.kind === 'worktree' ? entry.path : entry.kind === 'ref' ? entry.ref : undefined
      if (typeof value !== 'string') continue
      const key = `${String(entry.kind)}:${value}`
      const claim = holder.claims.get(key)
      if (claim !== undefined && claim.touchedAt <= at) holder.claims.delete(key)
    }
  } catch {
    // a corrupt revocation file revokes nothing
  }
}

const writeHolder = async ($: EngineInterface, holder: Holder, isEnded: boolean): Promise<void> => {
  if (config === null || sessionId === null) return
  const name = holderName(holder.agentId ?? MAIN)
  if (!isEnded) await dropRevoked($, name, holder)
  const at = iso(now())
  const doc = {
    version: 1,
    holder: name,
    kind: 'agent',
    session_id: sessionId,
    agent_id: holder.agentId,
    link: config.link,
    state: isEnded ? 'idle' : holder.state,
    started_at: iso(holder.startedAt),
    updated_at: at,
    // contract has no delete here: an ended holder is written already expired
    ttl_seconds: isEnded ? 0 : TTL_SECONDS[holder.state],
    claims: isEnded
      ? []
      : [...holder.claims.values()].map(claim =>
          claim.kind === 'worktree'
            ? { kind: 'worktree', path: claim.value, touched_at: iso(claim.touchedAt) }
            : { kind: 'ref', ref: claim.value, touched_at: iso(claim.touchedAt) },
        ),
  }
  await $.fs.write(`${config.claimsDir}/${name}.json`, JSON.stringify(doc))
}

const writeAll = async ($: EngineInterface, only: (h: Holder) => boolean): Promise<void> => {
  for (const holder of holders.values()) {
    if (!only(holder)) continue
    holder.isDirty = false
    try {
      await writeHolder($, holder, false)
    } catch (error) {
      debug($, `claim write failed: ${String(error)}`)
    }
  }
}

const refreshStatus = ($: EngineInterface): void => {
  const distinct = new Set<string>()
  let isWorking = false
  for (const holder of holders.values()) {
    if (holder.state !== 'working') continue
    isWorking = true
    for (const key of holder.claims.keys()) distinct.add(key)
  }
  const count = isWorking ? distinct.size : 0
  if (count === shownCount) return
  shownCount = count
  void $.ui.status(count > 0 ? `claimed: ${count}` : undefined)
}

const flush = async ($: EngineInterface): Promise<void> => {
  isFlushScheduled = false
  try {
    await ensureSession($)
    await writeAll($, holder => holder.isDirty)
    refreshStatus($)
  } catch (error) {
    debug($, `flush failed: ${String(error)}`)
  }
}

const stopTimerIfIdle = (): void => {
  if (timer === null) return
  for (const holder of holders.values()) if (holder.state === 'working') return
  timer.cancel()
  timer = null
}

const startTimer = ($: EngineInterface): void => {
  if (timer !== null) return
  timer = $.clock.every(HEARTBEAT_MS, () => {
    void (async () => {
      try {
        await ensureSession($)
        await releaseQuietSubagents($)
        await writeAll($, holder => holder.state === 'working')
        stopTimerIfIdle()
      } catch (error) {
        debug($, `heartbeat failed: ${String(error)}`)
      }
    })()
  })
}

const releaseQuietSubagents = async ($: EngineInterface): Promise<void> => {
  for (const [key, holder] of [...holders]) {
    if (key !== MAIN && now() - holder.lastActiveAt > SUBAGENT_QUIET_MS) await release($, key)
  }
}

const release = async ($: EngineInterface, agentId: string): Promise<void> => {
  const holder = holders.get(agentId)
  if (holder === undefined) return
  holders.delete(agentId)
  try {
    await writeHolder($, holder, true)
  } catch (error) {
    debug($, `release failed: ${String(error)}`)
  }
}

const setState = ($: EngineInterface, agentId: string | undefined, state: HolderState): void => {
  const holder = holderFor(agentId)
  if (holder.state === state) return
  holder.state = state
  holder.isDirty = true
  if (state === 'working') startTimer($)
  else stopTimerIfIdle()
  scheduleFlush($)
}

const resume = ($: EngineInterface, agentId: string | undefined): void => {
  const holder = holders.get(agentId ?? MAIN)
  if (holder !== undefined) holder.lastActiveAt = now()
  if (holder !== undefined && holder.state === 'blocked') setState($, agentId, 'working')
}

export const register: Register = on => {
  // ---- lifecycle ----

  on('session.start', async ($, e, next) => {
    try {
      await ensureSession($)
      await $.tool.register({
        name: 'task_claim',
        description:
          'Record that you are working on a task, ticket or branch without editing files in its ' +
          'worktree. Call it with the branch names, ticket ids or task ids you are working on so ' +
          'other agents and the user can see the work is taken. Edits and shell commands inside a ' +
          'tracked worktree are claimed automatically; use this for everything else (research, ' +
          'review, planning, waiting on CI).',
        inputSchema: {
          type: 'object',
          properties: {
            refs: {
              type: 'array',
              items: { type: 'string' },
              description: 'Branch names, ticket ids or task ids being worked on.',
            },
          },
          required: ['refs'],
        },
        isDeferred: false,
      })
    } catch (error) {
      debug($, `session.start failed: ${String(error)}`)
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    try {
      await ensureSession($)
      const holder = holderFor(undefined)
      holder.claims.clear()
      holder.state = 'working'
      holder.isDirty = true
      shownCount = -1
      startTimer($)
      scheduleFlush($)
    } catch (error) {
      debug($, `turn.start failed: ${String(error)}`)
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // a subagent's own turns carry agentId; only the main loop's end is ours. Subagent
    // holders are not touched: a background subagent keeps working after the main turn
    // ends, so its claim lives until SubagentStop or session end.
    if (e.agentId === undefined) {
      try {
        await ensureSession($)
        const main = holders.get(MAIN)
        if (main !== undefined) {
          main.state = 'idle'
          main.isDirty = true
        }
        stopTimerIfIdle()
        scheduleFlush($)
      } catch (error) {
        debug($, `turn.complete failed: ${String(error)}`)
      }
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    try {
      // the id of the session that is ending (after /clear the process goes on under another)
      sessionId = e.sessionId
      await loadConfig($)
      await syncClock($)
      for (const key of [...holders.keys()]) await release($, key)
      if (timer !== null) {
        timer.cancel()
        timer = null
      }
      shownCount = -1
      void $.ui.status(undefined)
      sessionId = null
      sessionCwd = null
    } catch (error) {
      debug($, `session.end failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.SubagentStart', ($, e, next) => {
    try {
      const holder = holderFor(e.agent_id)
      holder.state = 'working'
      holder.lastActiveAt = now()
      holder.isDirty = true
      startTimer($)
      scheduleFlush($)
    } catch (error) {
      debug($, `SubagentStart failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.SubagentStop', async ($, e, next) => {
    try {
      await ensureSession($)
      await release($, e.agent_id)
      stopTimerIfIdle()
      refreshStatus($)
    } catch (error) {
      debug($, `SubagentStop failed: ${String(error)}`)
    }
    return next(e)
  })

  // ---- blocked / resumed ----

  on('classic.PermissionRequest', ($, e, next) => {
    try {
      setState($, e.agent_id, 'blocked')
    } catch (error) {
      debug($, `PermissionRequest failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.Notification', ($, e, next) => {
    try {
      const kind = e.notification_type
      if (kind === 'permission_prompt' || kind === 'elicitation_dialog') {
        const holder = holders.get(MAIN)
        if (holder !== undefined && holder.state === 'working') setState($, undefined, 'blocked')
      }
    } catch (error) {
      debug($, `Notification failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.PostToolUse', ($, e, next) => {
    try {
      resume($, e.agent_id)
      // cwd after the command ran: a `cd` the command made is seen here
      if (e.tool_name === 'Bash') touchPath($, e.agent_id, e.cwd, false)
    } catch (error) {
      debug($, `PostToolUse failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.PostToolUseFailure', ($, e, next) => {
    try {
      resume($, e.agent_id)
    } catch (error) {
      debug($, `PostToolUseFailure failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.PermissionDenied', ($, e, next) => {
    try {
      resume($, e.agent_id)
    } catch (error) {
      debug($, `PermissionDenied failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.CwdChanged', ($, e, next) => {
    try {
      holderFor(e.agent_id).cwd = e.new_cwd
      if (e.agent_id === undefined) sessionCwd = e.new_cwd
    } catch (error) {
      debug($, `CwdChanged failed: ${String(error)}`)
    }
    return next(e)
  })

  // ---- touches from tool calls ----

  on('tool.call', { tool: ['Edit', 'Write'] }, ($, e, next) => {
    try {
      touchPath($, e.agentId, e.file_path, true)
    } catch (error) {
      debug($, `touch failed: ${String(error)}`)
    }
    return next(e)
  })

  on('tool.call', { tool: 'NotebookEdit' }, ($, e, next) => {
    try {
      touchPath($, e.agentId, e.notebook_path, true)
    } catch (error) {
      debug($, `touch failed: ${String(error)}`)
    }
    return next(e)
  })

  on('tool.call', { tool: 'Bash' }, ($, e, next) => {
    try {
      const cwd = holders.get(e.agentId ?? MAIN)?.cwd ?? sessionCwd
      for (const dir of bashDirectories(e.command, cwd)) touchPath($, e.agentId, dir, false)
    } catch (error) {
      debug($, `touch failed: ${String(error)}`)
    }
    return next(e)
  })

  on('tool.call', { tool: TASK_CLAIM_TOOL }, ($, e) => {
    const input = e as unknown as { refs?: unknown; agentId?: string }
    const refs = Array.isArray(input.refs) ? input.refs : []
    const accepted: string[] = []
    for (const ref of refs.slice(0, MAX_REFS_PER_CALL)) {
      if (typeof ref !== 'string') continue
      const value = ref.trim()
      if (value === '' || value.length > MAX_REF_LENGTH) continue
      touch($, input.agentId, 'ref', value)
      accepted.push(value)
    }
    return {
      result: accepted.length > 0 ? `Claimed: ${accepted.join(', ')}` : 'Nothing claimed: refs must be non-empty strings.',
    }
  })
}
