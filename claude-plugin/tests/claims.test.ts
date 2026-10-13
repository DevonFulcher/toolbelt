import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

const HOME = '/home/dev/.toolbelt'
const CLAIMS = `${HOME}/claims`
const WORK = '/g'
const SESSION = 'sess-1'
const TASK_CLAIM = 'mcp__toolbelt-claims__task_claim'
const T0 = Date.parse('2026-01-01T00:00:00.000Z')

// Copy of schema/claim.schema.json (a test cannot import a .json file; the Python side validates
// against the file). Keep the two identical.
const claimSchema = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://toolbelt.local/schemas/claim.v1.schema.json",
  "title": "toolbelt claim file, contract v1",
  "description": "One file per holder at $TOOLBELT_HOME/claims/<holder>.json. A holder is live iff now < updated_at + ttl_seconds. ttl_seconds 0 is how the writer ends a holder it cannot delete (an immediately expired tombstone with no claims).",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "version",
    "holder",
    "kind",
    "session_id",
    "agent_id",
    "link",
    "state",
    "started_at",
    "updated_at",
    "ttl_seconds",
    "claims"
  ],
  "properties": {
    "version": {
      "const": 1
    },
    "holder": {
      "type": "string",
      "pattern": "^[A-Za-z0-9._-]+$"
    },
    "kind": {
      "const": "agent"
    },
    "session_id": {
      "type": [
        "string",
        "null"
      ]
    },
    "agent_id": {
      "type": [
        "string",
        "null"
      ]
    },
    "link": {
      "type": [
        "string",
        "null"
      ]
    },
    "state": {
      "enum": [
        "working",
        "idle",
        "blocked"
      ]
    },
    "started_at": {
      "$ref": "#/$defs/timestamp"
    },
    "updated_at": {
      "$ref": "#/$defs/timestamp"
    },
    "ttl_seconds": {
      "type": "integer",
      "minimum": 0
    },
    "claims": {
      "type": "array",
      "items": {
        "oneOf": [
          {
            "$ref": "#/$defs/worktree_claim"
          },
          {
            "$ref": "#/$defs/ref_claim"
          }
        ]
      }
    }
  },
  "$defs": {
    "timestamp": {
      "type": "string",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?Z$"
    },
    "worktree_claim": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "kind",
        "path",
        "touched_at"
      ],
      "properties": {
        "kind": {
          "const": "worktree"
        },
        "path": {
          "type": "string",
          "pattern": "^/.+"
        },
        "touched_at": {
          "$ref": "#/$defs/timestamp"
        }
      }
    },
    "ref_claim": {
      "type": "object",
      "additionalProperties": false,
      "required": [
        "kind",
        "ref",
        "touched_at"
      ],
      "properties": {
        "kind": {
          "const": "ref"
        },
        "ref": {
          "type": "string",
          "minLength": 1
        },
        "touched_at": {
          "$ref": "#/$defs/timestamp"
        }
      }
    }
  }
}

// ---- a JSON schema validator for the subset the contract schema uses ----

type Schema = Record<string, unknown>

function validate(schema: Schema, value: unknown, root: Schema, path = '$'): string[] {
  const ref = schema['$ref']
  if (typeof ref === 'string') {
    const target = ref.replace('#/', '').split('/').reduce<unknown>((node, key) => (node as Schema)[key], root)
    return validate(target as Schema, value, root, path)
  }
  const errors: string[] = []
  const kindOf = (v: unknown): string =>
    v === null ? 'null' : Array.isArray(v) ? 'array' : Number.isInteger(v) ? 'integer' : typeof v
  if ('const' in schema && value !== schema['const']) errors.push(`${path}: not const ${String(schema['const'])}`)
  if ('enum' in schema && !(schema['enum'] as unknown[]).includes(value)) errors.push(`${path}: not in enum`)
  if ('type' in schema) {
    const types = ([] as string[]).concat(schema['type'] as string | string[])
    const kind = kindOf(value)
    const matches = types.includes(kind) || (kind === 'integer' && types.includes('number'))
    if (!matches) errors.push(`${path}: type ${kind} not in ${types.join('|')}`)
  }
  if (typeof value === 'string') {
    if (typeof schema['pattern'] === 'string' && !new RegExp(schema['pattern']).test(value)) {
      errors.push(`${path}: ${value} does not match ${schema['pattern']}`)
    }
    if (typeof schema['minLength'] === 'number' && value.length < schema['minLength']) errors.push(`${path}: too short`)
  }
  if (typeof value === 'number' && typeof schema['minimum'] === 'number' && value < schema['minimum']) {
    errors.push(`${path}: below minimum`)
  }
  if (Array.isArray(value) && schema['items'] !== undefined) {
    value.forEach((item, i) => errors.push(...validate(schema['items'] as Schema, item, root, `${path}[${i}]`)))
  }
  if (Array.isArray(schema['oneOf'])) {
    const passing = (schema['oneOf'] as Schema[]).filter(s => validate(s, value, root, path).length === 0)
    if (passing.length !== 1) errors.push(`${path}: matches ${passing.length} of oneOf`)
  }
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    const object = value as Record<string, unknown>
    const props = (schema['properties'] ?? {}) as Record<string, Schema>
    for (const key of (schema['required'] ?? []) as string[]) {
      if (!(key in object)) errors.push(`${path}: missing ${key}`)
    }
    for (const [key, item] of Object.entries(object)) {
      const sub = props[key]
      if (sub !== undefined) errors.push(...validate(sub, item, root, `${path}.${key}`))
      else if (schema['additionalProperties'] === false) errors.push(`${path}: unexpected ${key}`)
    }
  }
  return errors
}

// ---- a world beneath the plugin: env, clock, fs, session, ui ----

type Doc = {
  holder: string
  session_id: string | null
  agent_id: string | null
  link: string | null
  state: string
  started_at: string
  updated_at: string
  ttl_seconds: number
  claims: { kind: string; path?: string; ref?: string; touched_at: string }[]
}

type World = {
  files: Map<string, Doc>
  writes: Doc[]
  statuses: (string | undefined)[]
  clock: ReturnType<typeof mock.clock>
  revoke: (holder: string, entries: unknown[]) => void
  failWrites: () => void
}

const EDIT = (file_path: string, agentId?: string) =>
  ({ tool: 'Edit', file_path, old_string: 'a', new_string: 'b', agentId }) as never

function world(
  on: On,
  env: Record<string, string> = {
    TOOLBELT_HOME: HOME,
    GIT_PROJECTS_WORKDIR: WORK,
    CLAUDE_CODE_HOST_SESSION_ID: 'local_abc',
    HOME: '/home/dev',
  },
  cwd = '/g/toolbelt',
): World {
  const files = new Map<string, Doc>()
  const writes: Doc[] = []
  const statuses: (string | undefined)[] = []
  const revoked = new Map<string, string>()
  let isFailing = false
  const clock = mock.clock(on, { now: T0 })
  mock.env(on, env)
  on('session.id', () => ({ value: SESSION }))
  on('session.cwd', () => ({ value: cwd }))
  on('tool.register', () => ({ value: { tool: TASK_CLAIM } }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.status', (_$, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('fs.write', (_$, e) => {
    if (isFailing) return { deny: 'EROFS' }
    const doc = JSON.parse(e.text) as Doc
    expect(validate(claimSchema as Schema, doc, claimSchema as Schema)).toEqual([])
    files.set(e.path, doc)
    writes.push(doc)
    return { value: undefined }
  })
  on('fs.read', (_$, e) => {
    const text = revoked.get(e.path)
    return text === undefined ? { deny: 'ENOENT' } : { value: text }
  })
  on('tool.call', () => ({ result: 'ok' }))
  on('session.start', () => ({ cwd }))
  on('session.end', (_$, e) => ({ sessionId: e.sessionId }))
  on('turn.start', (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  for (const name of [
    'classic.PermissionRequest',
    'classic.Notification',
    'classic.SubagentStart',
    'classic.SubagentStop',
    'classic.PostToolUse',
    'classic.PostToolUseFailure',
    'classic.PermissionDenied',
    'classic.CwdChanged',
  ] as const) {
    on(name, () => ({}) as never)
  }
  return {
    files,
    writes,
    statuses,
    clock,
    revoke: (holder, entries) =>
      revoked.set(`${CLAIMS}/revoked/${holder}.json`, JSON.stringify({ version: 1, holder, revoked: entries })),
    failWrites: () => {
      isFailing = true
    },
  }
}

const mainDoc = (w: World): Doc => {
  const doc = w.files.get(`${CLAIMS}/${SESSION}.json`)
  if (doc === undefined) throw new Error(`no main claim file; wrote ${[...w.files.keys()].join(', ')}`)
  return doc
}

const claimed = (doc: Doc): string[] => doc.claims.map(c => c.path ?? c.ref ?? '').sort()

async function startTurn($: Engine, w: World): Promise<void> {
  await $.session.start({ cwd: '/g/toolbelt' } as never)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await w.clock.settle()
}

async function endTurn($: Engine, w: World, reason: 'answer' | 'error' | 'aborted' = 'answer'): Promise<void> {
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: reason === 'aborted', turnId: 't1', reason } as never)
  await w.clock.settle()
}

describe('worktree resolution', () => {
  test('claims <root>/<repo>/<name> for writes at any depth, nothing outside', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/feature/src/deep/a.ts'))
    await $.tool.call(EDIT('/g/wt/repo/feature/README.md'))
    await $.tool.call(EDIT('/g/wt/repo/other/a.ts'))
    await $.tool.call(EDIT('/g/toolbelt/src/a.ts'))
    await $.tool.call(EDIT('/g/wt-extra/repo/feature/a.ts'))
    await $.tool.call(EDIT('/g/wtrepo/feature/a.ts'))
    await $.tool.call(EDIT('/g/wt/repo/lonely.txt'))
    await $.tool.call(EDIT('relative/path.ts'))
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/feature', '/g/wt/repo/other'])
  })

  test('a repo whose name starts like another is its own worktree root', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo-other/x/a.ts'))
    await $.tool.call(EDIT('/g/wt/repo/x/a.ts'))
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo-other/x', '/g/wt/repo/x'])
  })

  test('dot segments cannot climb out of a worktree', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/feature/../../../toolbelt/a.ts'))
    await w.clock.settle()
    expect(w.files.size).toBe(1)
    expect(claimed(mainDoc(w))).toEqual([])
  })

  test('falls back to the first /wt/ segment without GIT_PROJECTS_WORKDIR', async ($, on) => {
    const w = world(on, { TOOLBELT_HOME: HOME })
    await startTurn($, w)
    await $.tool.call(EDIT('/Users/dev/code/wt/repo/feature/a.ts'))
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/Users/dev/code/wt/repo/feature'])
  })

  test('NotebookEdit claims, Read does not', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call({ tool: 'NotebookEdit', notebook_path: '/g/wt/repo/nb/a.ipynb', new_source: 'x' } as never)
    await $.tool.call({ tool: 'Read', file_path: '/g/wt/repo/read-only/a.ts' } as never)
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/nb'])
  })

  test('Bash claims its working directory and a leading cd', async ($, on) => {
    const w = world(on, undefined, '/g/wt/repo/feature/sub')
    await startTurn($, w)
    await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
    await $.tool.call({ tool: 'Bash', command: 'cd /g/wt/repo/second && make test' } as never)
    await $.tool.call({ tool: 'Bash', command: 'git -C /g/wt/repo/third status' } as never)
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/feature', '/g/wt/repo/second', '/g/wt/repo/third'])
  })

  test('Bash in the main checkout claims nothing', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call({ tool: 'Bash', command: 'ls' } as never)
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual([])
  })
})

describe('turn scope and state', () => {
  test('working then idle keeps claims, the next turn starts empty', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    expect(mainDoc(w)).toMatchObject({ state: 'working', ttl_seconds: 180, claims: [] })
    await $.tool.call(EDIT('/g/wt/repo/a/f.ts'))
    await w.clock.settle()
    expect(mainDoc(w)).toMatchObject({ state: 'working', ttl_seconds: 180 })
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/a'])

    await endTurn($, w)
    expect(mainDoc(w)).toMatchObject({ state: 'idle', ttl_seconds: 1800 })
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/a'])

    await $.turn.start({ text: 'again', turnId: 't2' })
    await w.clock.settle()
    expect(mainDoc(w)).toMatchObject({ state: 'working', ttl_seconds: 180, claims: [] })
    await $.tool.call(EDIT('/g/wt/repo/b/f.ts'))
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/b'])
  })

  test('a failed or aborted turn is a turn end', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await endTurn($, w, 'error')
    expect(mainDoc(w).state).toBe('idle')
    await $.turn.start({ text: 'again', turnId: 't2' })
    await w.clock.settle()
    await endTurn($, w, 'aborted')
    expect(mainDoc(w)).toMatchObject({ state: 'idle', ttl_seconds: 1800 })
  })

  test('a permission prompt blocks and the next tool result resumes', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.classic.PermissionRequest({ tool_name: 'Bash', tool_input: {}, permission_suggestions: [] } as never)
    await w.clock.settle()
    expect(mainDoc(w)).toMatchObject({ state: 'blocked', ttl_seconds: 1800 })
    await $.classic.PostToolUse({ tool_name: 'Bash', tool_input: {}, tool_response: {}, tool_use_id: 'u' } as never)
    await w.clock.settle()
    expect(mainDoc(w)).toMatchObject({ state: 'working', ttl_seconds: 180 })
  })

  test('a permission notification blocks while working only', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.classic.Notification({ message: 'm', notification_type: 'permission_prompt' } as never)
    await w.clock.settle()
    expect(mainDoc(w).state).toBe('blocked')
    await $.classic.PermissionDenied({ tool_name: 'Bash', tool_input: {}, tool_use_id: 'u', reason: 'r' } as never)
    await w.clock.settle()
    expect(mainDoc(w).state).toBe('working')
    await endTurn($, w)
    await $.classic.Notification({ message: 'm', notification_type: 'idle_prompt' } as never)
    await w.clock.settle()
    expect(mainDoc(w).state).toBe('idle')
  })

  test('the file carries the contract fields', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    expect(mainDoc(w)).toMatchObject({
      holder: SESSION,
      session_id: SESSION,
      agent_id: null,
      link: 'claude://claude.ai/epitaxy/local_abc',
    })
    expect(Date.parse(mainDoc(w).updated_at)).toBeGreaterThanOrEqual(Date.parse(mainDoc(w).started_at))
  })

  test('session end leaves an already expired, empty file', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/a/f.ts'))
    await w.clock.settle()
    await $.session.end({ reason: 'clear', sessionId: SESSION, resume: { id: SESSION } } as never)
    expect(mainDoc(w)).toMatchObject({ ttl_seconds: 0, claims: [] })
  })
})

describe('heartbeat', () => {
  test('rewrites a working file every 30 seconds and not an idle one', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    const before = w.writes.length
    const first = mainDoc(w).updated_at
    await w.clock.advance(29_000)
    expect(w.writes.length).toBe(before)
    await w.clock.advance(1_000)
    expect(w.writes.length).toBe(before + 1)
    expect(Date.parse(mainDoc(w).updated_at)).toBeGreaterThan(Date.parse(first))
    await w.clock.advance(30_000)
    expect(w.writes.length).toBe(before + 2)

    await endTurn($, w)
    const idle = w.writes.length
    await w.clock.advance(120_000)
    expect(w.writes.length).toBe(idle)
  })

  test('repeat touches of a claimed worktree write nothing', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/a/f.ts'))
    await w.clock.settle()
    const count = w.writes.length
    for (let i = 0; i < 20; i += 1) await $.tool.call(EDIT(`/g/wt/repo/a/f${i}.ts`))
    await w.clock.settle()
    expect(w.writes.length).toBe(count)
  })
})

describe('revocation', () => {
  test('drops a claim touched at or before the revocation, keeps a later one', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/gone/f.ts'))
    await $.tool.call(EDIT('/g/wt/repo/kept/f.ts'))
    await w.clock.settle()
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/gone', '/g/wt/repo/kept'])
    w.revoke(SESSION, [
      { kind: 'worktree', path: '/g/wt/repo/gone', at: '2999-01-01T00:00:00Z', by: 'tt' },
      { kind: 'worktree', path: '/g/wt/repo/kept', at: '2000-01-01T00:00:00Z', by: 'tt' },
    ])
    await w.clock.advance(30_000)
    expect(claimed(mainDoc(w))).toEqual(['/g/wt/repo/kept'])
  })

  test('revokes refs too, and an emptied revocation list revokes nothing', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call({ tool: TASK_CLAIM, refs: ['T-1', 'T-2'] } as never)
    await w.clock.settle()
    w.revoke(SESSION, [{ kind: 'ref', ref: 'T-1', at: '2999-01-01T00:00:00Z', by: 'tt' }])
    await w.clock.advance(30_000)
    expect(claimed(mainDoc(w))).toEqual(['T-2'])
    w.revoke(SESSION, [])
    await w.clock.advance(30_000)
    expect(claimed(mainDoc(w))).toEqual(['T-2'])
  })
})

describe('subagents and the claim tool', () => {
  test('a subagent has its own holder file, released when it stops', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.classic.SubagentStart({ agent_id: 'ag/1', agent_type: 'general-purpose' } as never)
    await w.clock.settle()
    await $.tool.call(EDIT('/g/wt/repo/sub/f.ts', 'ag/1'))
    await w.clock.settle()
    const sub = w.files.get(`${CLAIMS}/${SESSION}.ag_1.json`)
    expect(sub).toMatchObject({
      holder: `${SESSION}.ag_1`,
      session_id: SESSION,
      agent_id: 'ag/1',
      state: 'working',
    })
    expect(claimed(sub as Doc)).toEqual(['/g/wt/repo/sub'])
    expect(claimed(mainDoc(w))).toEqual([])

    await $.classic.SubagentStop({ agent_id: 'ag/1', agent_type: 'general-purpose', stop_hook_active: false } as never)
    await w.clock.settle()
    expect(w.files.get(`${CLAIMS}/${SESSION}.ag_1.json`)).toMatchObject({ ttl_seconds: 0, claims: [] })
  })

  test('task_claim adds ref claims and the status shows the distinct count', async ($, on) => {
    const w = world(on)
    await startTurn($, w)
    await $.tool.call(EDIT('/g/wt/repo/a/f.ts'))
    const result = await $.tool.call({ tool: TASK_CLAIM, refs: ['feature/x', ' T-9 ', '', 7] } as never)
    await w.clock.settle()
    expect(JSON.stringify(result)).toContain('feature/x')
    expect(mainDoc(w).claims.filter(c => c.kind === 'ref').map(c => c.ref)).toEqual(['feature/x', 'T-9'])
    expect(w.statuses.at(-1)).toBe('claimed: 3')
    await endTurn($, w)
    expect(w.statuses.at(-1)).toBeUndefined()
  })
})

describe('resilience', () => {
  test('a failing write never fails or slows the tool call', async ($, on) => {
    const w = world(on)
    w.failWrites()
    await startTurn($, w)
    const result = await $.tool.call(EDIT('/g/wt/repo/a/f.ts'))
    await w.clock.advance(60_000)
    expect(JSON.stringify(result)).toContain('ok')
    expect(w.writes.length).toBe(0)
  })

  test('without TOOLBELT_HOME the files go to ~/.toolbelt/claims', async ($, on) => {
    const w = world(on, { HOME: '/home/dev', GIT_PROJECTS_WORKDIR: WORK })
    await startTurn($, w)
    expect([...w.files.keys()]).toEqual([`/home/dev/.toolbelt/claims/${SESSION}.json`])
  })
})
