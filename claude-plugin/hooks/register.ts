import type { EngineInterface, Register } from 'claude-code'

// Writes claim files (contract v1, docs/task-claims.md) recording which tracked
// worktrees and refs this session is working on. The files are the only
// interface to toolbelt: this module never spawns `tt`, and the per-tool-call
// hooks only test a string and update a map. Files are written from a timer,
// never from the call itself. The one process it runs is a single `sh`/`ps`
// at load to learn the Claude process's pid and start time (see processInfo).

type ClaimKind = 'worktree' | 'ref'
type HolderState = 'working' | 'idle' | 'blocked'

type Claim = { kind: ClaimKind; value: string; touchedAt: number }

type Holder = {
  agentId: string | null
  state: HolderState
  startedAt: number
  cwd: string | null
  claims: Map<string, Claim>
  isDirty: boolean
}

type Config = {
  claimsDir: string
  activityDir: string
  worktreesRoot: string | null
  link: string | null
}

// What a tool call did, kept per turn (see "activity" below).
type Call = {
  tool: string
  detail: string
  startedAt: number
  endedAt: number | null
  isOk: boolean | null
  isFailed: boolean
  worktree: string | null
  isBackground: boolean
  isDelegated: boolean
}

type Interval = [number, number]

type Turn = {
  id: string
  startedAt: number
  // chunk files written so far for this turn
  seq: number
  lastChunkAt: number
  // finished calls not yet written, and the ones in flight
  calls: Call[]
  open: Call[]
  lastEnded: Call | null
  // every call's interval (kept after its chunk is written: they give the turn's totals)
  intervals: Interval[]
  delegated: Interval[]
  blocked: Interval[]
  blockedSince: number | null
}

const TTL_SECONDS: Record<HolderState, number> = { working: 180, idle: 1800, blocked: 1800 }
const HEARTBEAT_MS = 30_000
const MAX_REFS_PER_CALL = 50
const MAX_REF_LENGTH = 200
const TASK_CLAIM_TOOL = 'mcp__toolbelt-claims__task_claim'
const MAIN = ''
const MAX_DETAIL = 500
const LIVE_DETAIL = 200
// a long turn writes its finished calls this often, so they are not lost to a kill
const CHUNK_AFTER_CALLS = 200
const CHUNK_AFTER_MS = 5 * 60_000
// tools that start a subagent: their wait is "delegated", not tool time
const DELEGATING_TOOLS = new Set(['Task', 'Agent'])

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
// The Claude process this module runs in, written into every claim file so a reader
// can tell at once that the writer died. undefined: not looked up yet; null: could
// not be determined (the fields are then left out).
let processInfo: { pid: number; startedAt: string } | null | undefined
const holders = new Map<string, Holder>()
const turns = new Map<string, Turn>()

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
    activityDir: `${base.replace(/\/+$/, '')}/activity`,
    worktreesRoot: workdir ? `${workdir.replace(/\/+$/, '')}/wt` : null,
    link: host ? `claude://claude.ai/epitaxy/${host}` : null,
  }
}

// `ps -o lstart=` prints the start time in the local zone with no zone name, as
// "Tue Oct 21 10:00:00 2026"; TZ=UTC makes it UTC. Seconds are its resolution.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const parseLstart = (text: string): string | null => {
  const match = /^\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})$/.exec(text.trim())
  if (match === null) return null
  const month = MONTHS.indexOf(match[1] as string)
  if (month < 0) return null
  const [day, hour, minute, second, year] = [match[2], match[3], match[4], match[5], match[6]].map(Number)
  return iso(Date.UTC(year as number, month, day as number, hour as number, minute as number, second as number))
}

// The pid of the process that started this `sh` is the Claude process; its start time
// is what lets a reader tell a dead writer from a reused pid. Looked up once; on any
// failure both fields are omitted rather than written wrong.
const resolveProcess = async ($: EngineInterface): Promise<void> => {
  if (processInfo !== undefined) return
  processInfo = null
  try {
    const { exitCode, stdout } = await $.process.run(
      ['sh', '-c', 'echo $PPID; ps -o lstart= -p $PPID'],
      { env: { TZ: 'UTC', LC_ALL: 'C' }, timeoutMs: 5000 },
    )
    if (exitCode !== 0) return
    const [pidLine, startLine] = stdout.split('\n')
    const pid = Number(pidLine?.trim())
    const startedAt = parseLstart(startLine ?? '')
    if (Number.isInteger(pid) && pid > 1 && startedAt !== null) processInfo = { pid, startedAt }
  } catch (error) {
    debug($, `process lookup failed: ${String(error)}`)
  }
}

const syncClock = async ($: EngineInterface): Promise<void> => {
  clockOffset = (await $.clock.now()) - Date.now()
}

const ensureSession = async ($: EngineInterface): Promise<void> => {
  await loadConfig($)
  await resolveProcess($)
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

// ---- activity: what each tool call did ----
//
// Every tool call is timed (the hook wraps it) and kept in memory for the turn;
// the finished calls are written as one chunk file per turn (more for a long
// one) to $TOOLBELT_HOME/activity, which `tt` reads and removes. Nothing is
// written from the call itself.

const turnFor = (agentId: string | undefined, id: string | null = null): Turn => {
  const key = agentId ?? MAIN
  let turn = turns.get(key)
  if (turn === undefined) {
    const startedAt = now()
    turn = {
      id: id ?? `${key === MAIN ? 'main' : key}-${startedAt}`,
      startedAt,
      seq: 0,
      lastChunkAt: startedAt,
      calls: [],
      open: [],
      lastEnded: null,
      intervals: [],
      delegated: [],
      blocked: [],
      blockedSince: null,
    }
    turns.set(key, turn)
  }
  return turn
}

const noteBlocked = (agentId: string | undefined, isBlocked: boolean): void => {
  const turn = turnFor(agentId)
  if (isBlocked) {
    if (turn.blockedSince === null) turn.blockedSince = now()
  } else if (turn.blockedSince !== null) {
    turn.blocked.push([turn.blockedSince, now()])
    turn.blockedSince = null
  }
}

// -- intervals: merged, measured, subtracted (all in ms)

const merged = (intervals: Interval[]): Interval[] => {
  const sorted = intervals.filter(([a, b]) => b > a).sort((x, y) => x[0] - y[0])
  const out: Interval[] = []
  for (const [a, b] of sorted) {
    const last = out[out.length - 1]
    if (last !== undefined && a <= last[1]) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}

const measure = (intervals: Interval[]): number => intervals.reduce((sum, [a, b]) => sum + (b - a), 0)

// a minus b, for two merged lists
const minus = (a: Interval[], b: Interval[]): Interval[] => {
  const out: Interval[] = []
  for (const [start, end] of a) {
    let from = start
    for (const [x, y] of b) {
      if (y <= from || x >= end) continue
      if (x > from) out.push([from, x])
      from = Math.max(from, y)
    }
    if (from < end) out.push([from, end])
  }
  return out
}

const within = (intervals: Interval[], lo: number, hi: number): Interval[] =>
  intervals.map(([a, b]): Interval => [Math.max(a, lo), Math.min(b, hi)]).filter(([a, b]) => b > a)

// -- the shell: words, chains and where a command runs

// Splits a command into the commands of its chain (&&, ||, ;, |, &, newlines and
// subshell parentheses), then each into words, honouring quotes. Not a shell:
// substitutions, heredoc bodies and expansions are left as they are.
const shellCommands = (command: string): string[][] => {
  const commands: string[][] = []
  let words: string[] = []
  let word = ''
  let hasWord = false
  let quote: string | null = null
  const endWord = (): void => {
    if (hasWord) words.push(word)
    word = ''
    hasWord = false
  }
  const endCommand = (): void => {
    endWord()
    if (words.length > 0) commands.push(words)
    words = []
  }
  for (let i = 0; i < command.length; i++) {
    const c = command[i] as string
    if (quote !== null) {
      if (c === quote) quote = null
      else if (c === '\\' && quote === '"' && i + 1 < command.length) word += command[++i]
      else word += c
    } else if (c === '"' || c === "'") {
      quote = c
      hasWord = true
    } else if (c === '\\' && i + 1 < command.length) {
      word += command[++i]
      hasWord = true
    } else if (c === ' ' || c === '\t') {
      endWord()
    } else if (';|&\n()'.includes(c)) {
      endCommand()
    } else {
      word += c
      hasWord = true
    }
  }
  endCommand()
  return commands
}

const DIRECTORY_OPTIONS = new Set(['-C', '--dir', '--directory', '--project', '--cwd', '--prefix', '--workdir'])

// Directories a Bash command works in or names: where each command of the chain
// runs (a `cd` anywhere in it moves the rest), the targets of options like
// `git -C`, `pnpm --dir` and `uv run --project`, and absolute paths among the
// words. Explicit ones first, then where it runs.
const bashActivityDirectories = (command: string, start: string | null): string[] => {
  let cwd = start
  const explicit: string[] = []
  const context: string[] = []
  const resolve = (path: string): string | null => {
    if (path.startsWith('/')) return normalize(path)
    if (path.startsWith('~') || path.includes('$') || path.includes('`')) return null
    return cwd === null ? null : normalize(`${cwd}/${path}`)
  }
  for (const all of shellCommands(command)) {
    let words = all
    while (words.length > 0 && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0] as string)) words = words.slice(1)
    const [program, first] = words
    if ((program === 'cd' || program === 'pushd') && first !== undefined) {
      const target = resolve(first)
      if (target !== null) cwd = target
      continue
    }
    if (program === undefined) continue
    for (let i = 1; i < words.length; i++) {
      const word = words[i] as string
      const equals = word.indexOf('=')
      const option = equals > 0 ? word.slice(0, equals) : word
      if (DIRECTORY_OPTIONS.has(option)) {
        const value = equals > 0 ? word.slice(equals + 1) : words[i + 1]
        const target = value === undefined ? null : resolve(value)
        if (target !== null) explicit.push(target)
      } else if (word.startsWith('/')) {
        explicit.push(normalize(word))
      }
    }
    if (cwd !== null) context.push(cwd)
  }
  return [...explicit, ...context]
}

// The paths a tool call names, by the field each tool uses.
const callDetail = (e: Record<string, unknown>): string => {
  for (const key of ['command', 'file_path', 'notebook_path', 'path', 'pattern', 'url', 'query', 'text', 'description', 'prompt']) {
    const value = e[key]
    if (typeof value === 'string' && value !== '') return value.slice(0, MAX_DETAIL)
  }
  return ''
}

// The worktree a call was about: a path it names, else where its command ran,
// else where the session is. Unlike a claim this also counts reads.
const callWorktree = (tool: string, e: Record<string, unknown>, agentId: string | undefined): string | null => {
  const start = holders.get(agentId ?? MAIN)?.cwd ?? sessionCwd
  const directories: string[] = []
  for (const key of ['file_path', 'notebook_path', 'path']) {
    const value = e[key]
    if (typeof value === 'string') directories.push(value)
  }
  if (tool === 'Bash' && typeof e['command'] === 'string') {
    directories.push(...bashActivityDirectories(e['command'], start))
  } else if (start !== null) {
    directories.push(start)
  }
  for (const directory of directories) {
    const worktree = worktreeOf(directory, false)
    if (worktree !== null) return worktree
  }
  return null
}

const beginCall = (e: Record<string, unknown>, agentId: string | undefined): Call => {
  const tool = typeof e['tool'] === 'string' ? e['tool'] : 'unknown'
  const call: Call = {
    tool,
    detail: callDetail(e),
    startedAt: now(),
    endedAt: null,
    isOk: null,
    isFailed: false,
    worktree: callWorktree(tool, e, agentId),
    isBackground: e['run_in_background'] === true,
    isDelegated: DELEGATING_TOOLS.has(tool),
  }
  turnFor(agentId).open.push(call)
  return call
}

const endCall = (call: Call, agentId: string | undefined, isOk: boolean): void => {
  if (call.endedAt !== null) return
  const turn = turnFor(agentId)
  call.endedAt = now()
  call.isOk = isOk && !call.isFailed
  turn.open = turn.open.filter(open => open !== call)
  turn.calls.push(call)
  turn.lastEnded = call
  turn.intervals.push([call.startedAt, call.endedAt])
  if (call.isDelegated) turn.delegated.push([call.startedAt, call.endedAt])
}

// What the tool failure hook says: the call in flight, or the one that just ended.
const failCall = (agentId: string | undefined): void => {
  const turn = turns.get(agentId ?? MAIN)
  if (turn === undefined) return
  const open = turn.open[turn.open.length - 1]
  if (open !== undefined) open.isFailed = true
  else if (turn.lastEnded !== null && now() - (turn.lastEnded.endedAt ?? 0) < 5000) turn.lastEnded.isOk = false
}

// The call a claim file shows as what the holder is running now: the one in
// flight, else the last that ended.
const liveActivity = (agentId: string | null): { tool: string; detail: string; since: string; open: boolean } | null => {
  const turn = turns.get(agentId ?? MAIN)
  if (turn === undefined) return null
  const open = turn.open[turn.open.length - 1]
  const call = open ?? turn.lastEnded
  if (call === undefined || call === null) return null
  return { tool: call.tool, detail: call.detail.slice(0, LIVE_DETAIL), since: iso(call.startedAt), open: open !== undefined }
}

// Where the turn's unattributed calls go: the one worktree it claimed, when there is just one.
const claimedWorktree = (agentId: string | null): string | null => {
  const holder = holders.get(agentId ?? MAIN)
  if (holder === undefined) return null
  const worktrees = [...holder.claims.values()].filter(claim => claim.kind === 'worktree')
  return worktrees.length === 1 ? (worktrees[0] as Claim).value : null
}

type Totals = { wall_ms: number; tool_ms: number; delegated_ms: number; blocked_ms: number; model_ms: number }

// The turn's wall time split into parts that add up to it: tool time (calls,
// less the time blocked on you and the time waiting on a subagent), delegated
// time (waiting on subagents), blocked time (permission prompts and dialogs)
// and model time (the rest: thinking, streaming, API latency).
const turnTotals = (turn: Turn, endedAt: number, wallMs: number | null): Totals => {
  const lo = turn.startedAt
  const hi = Math.max(endedAt, lo)
  const blocked = merged(
    within([...turn.blocked, ...(turn.blockedSince === null ? [] : [[turn.blockedSince, hi] as Interval])], lo, hi),
  )
  const tools = merged(within(turn.intervals, lo, hi))
  const delegated = merged(within(turn.delegated, lo, hi))
  const busy = merged([...tools, ...blocked])
  const wall = Math.max(wallMs ?? hi - lo, measure(busy))
  return {
    wall_ms: Math.round(wall),
    tool_ms: Math.round(measure(minus(minus(tools, delegated), blocked))),
    delegated_ms: Math.round(measure(minus(delegated, blocked))),
    blocked_ms: Math.round(measure(blocked)),
    model_ms: Math.round(wall - measure(busy)),
  }
}

const callDocument = (call: Call, endedAt: number, fallback: string | null, blocked: Interval[]) => {
  const end = call.endedAt ?? endedAt
  return {
    tool: call.tool,
    detail: call.detail,
    started_at: iso(call.startedAt),
    duration_ms: Math.max(Math.round(end - call.startedAt), 0),
    blocked_ms: Math.round(measure(merged(within(blocked, call.startedAt, end)))),
    ok: call.endedAt === null ? null : call.isOk,
    unended: call.endedAt === null,
    worktree: call.worktree ?? fallback,
    background: call.isBackground,
    delegated: call.isDelegated,
  }
}

const writeChunk = async (
  $: EngineInterface,
  agentId: string | undefined,
  isFinal: boolean,
  endedAt: number,
  extra: { wallMs: number | null; reason: string | null },
): Promise<void> => {
  const turn = turns.get(agentId ?? MAIN)
  if (turn === undefined || config === null || sessionId === null) return
  const name = holderName(agentId ?? MAIN)
  if (isFinal) {
    // calls still in flight end with the turn
    for (const call of turn.open) turn.intervals.push([call.startedAt, endedAt])
    turn.calls.push(...turn.open)
    turn.open = []
  } else if (turn.calls.length === 0) {
    return
  }
  const fallback = claimedWorktree(agentId ?? null)
  const blocked = merged([
    ...turn.blocked,
    ...(turn.blockedSince === null ? [] : [[turn.blockedSince, endedAt] as Interval]),
  ])
  const doc = {
    version: 1,
    holder: name,
    session_id: sessionId,
    agent_id: agentId ?? null,
    turn_id: turn.id,
    seq: turn.seq,
    final: isFinal,
    started_at: iso(turn.startedAt),
    ended_at: isFinal ? iso(endedAt) : null,
    reason: isFinal ? extra.reason : null,
    totals: isFinal ? turnTotals(turn, endedAt, extra.wallMs) : null,
    calls: turn.calls.map(call => callDocument(call, endedAt, fallback, blocked)),
  }
  turn.calls = []
  turn.seq += 1
  turn.lastChunkAt = endedAt
  if (isFinal) turns.delete(agentId ?? MAIN)
  const safeTurn = turn.id.replace(HOLDER_UNSAFE, '_')
  await $.fs.write(`${config.activityDir}/${name}.${safeTurn}.${doc.seq}.json`, JSON.stringify(doc))
}

// The time a turn ends at: now, but never before something it recorded (the
// clock offset is re-read from the engine at each flush, so now() can step back).
const endOf = (turn: Turn | undefined): number => {
  const latest = turn === undefined ? 0 : Math.max(0, ...turn.intervals.map(([, end]) => end), ...turn.blocked.map(([, end]) => end))
  return Math.max(now(), latest)
}

const finishTurn = async (
  $: EngineInterface,
  agentId: string | undefined,
  reason: string,
  wallMs: number | null = null,
): Promise<void> => {
  try {
    await writeChunk($, agentId, true, endOf(turns.get(agentId ?? MAIN)), { wallMs, reason })
  } catch (error) {
    debug($, `activity write failed: ${String(error)}`)
  }
}

// A long turn's finished calls are written part way, so a kill loses little.
const flushLongTurns = async ($: EngineInterface): Promise<void> => {
  for (const [key, turn] of [...turns.entries()]) {
    if (turn.calls.length < CHUNK_AFTER_CALLS && !(turn.calls.length > 0 && now() - turn.lastChunkAt >= CHUNK_AFTER_MS)) continue
    try {
      await writeChunk($, key === MAIN ? undefined : key, false, now(), { wallMs: null, reason: null })
    } catch (error) {
      debug($, `activity write failed: ${String(error)}`)
    }
  }
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
    // the writer's process (both fields or neither); a reader treats a dead or reused pid as dead
    ...(processInfo === null || processInfo === undefined
      ? {}
      : { pid: processInfo.pid, process_started_at: processInfo.startedAt }),
    ...(isEnded || liveActivity(holder.agentId) === null ? {} : { activity: liveActivity(holder.agentId) }),
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
        await writeAll($, holder => holder.state === 'working')
        await flushLongTurns($)
        stopTimerIfIdle()
      } catch (error) {
        debug($, `heartbeat failed: ${String(error)}`)
      }
    })()
  })
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
  noteBlocked(agentId, state === 'blocked')
  holder.isDirty = true
  if (state === 'working') startTimer($)
  else stopTimerIfIdle()
  scheduleFlush($)
}

const resume = ($: EngineInterface, agentId: string | undefined): void => {
  const holder = holders.get(agentId ?? MAIN)
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
      // a turn that never completed (the engine skipped it) ends where the next starts
      if (turns.has(MAIN)) await finishTurn($, undefined, 'superseded')
      turnFor(undefined, typeof e.turnId === 'string' ? e.turnId : null)
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
    try {
      await ensureSession($)
      await finishTurn(
        $,
        e.agentId,
        typeof e.reason === 'string' ? e.reason : 'complete',
        typeof e.durationMs === 'number' ? e.durationMs : null,
      )
      if (e.agentId === undefined) {
        // the main loop's end. Subagent holders are not touched: a background subagent keeps
        // working after the main turn ends, so its claim lives until its own turn ends.
        const main = holders.get(MAIN)
        if (main !== undefined) {
          main.state = 'idle'
          main.isDirty = true
        }
        stopTimerIfIdle()
        scheduleFlush($)
      } else {
        // a subagent's own turn ended, so it has stopped. classic.SubagentStop is the usual
        // signal for this but is not always delivered (the engine can pass it over: see
        // docs/task-claims.md), so the claim is also released here. A tool call it makes
        // afterwards claims again.
        await release($, e.agentId)
        stopTimerIfIdle()
        refreshStatus($)
      }
    } catch (error) {
      debug($, `turn.complete failed: ${String(error)}`)
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    try {
      // the id of the session that is ending (after /clear the process goes on under another)
      sessionId = e.sessionId
      await loadConfig($)
      await syncClock($)
      for (const key of [...turns.keys()]) await finishTurn($, key === MAIN ? undefined : key, 'session_end')
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
      await finishTurn($, e.agent_id, 'subagent_stop')
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
      failCall(e.agent_id)
      resume($, e.agent_id)
    } catch (error) {
      debug($, `PostToolUseFailure failed: ${String(error)}`)
    }
    return next(e)
  })

  on('classic.PermissionDenied', ($, e, next) => {
    try {
      failCall(e.agent_id)
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

  // Times every tool call. Registered first, so it wraps the rest; it never
  // changes what the call does or returns.
  on('tool.call', async ($, e, next) => {
    let call: Call | null = null
    try {
      call = beginCall(e as unknown as Record<string, unknown>, e.agentId)
    } catch (error) {
      debug($, `activity begin failed: ${String(error)}`)
    }
    try {
      const result = await next(e)
      // a call that failed or was refused (a blocking hook, say) still returns, with isError set
      const isError = typeof result === 'object' && result !== null && (result as { isError?: unknown }).isError === true
      if (call !== null) endCall(call, e.agentId, !isError)
      return result
    } catch (error) {
      if (call !== null) endCall(call, e.agentId, false)
      throw error
    }
  })

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
