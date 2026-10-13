# Task claims

A *claim* says "this agent session is working on this task right now". `tt ui`
shows a claimed task as **With an agent** instead of **Needs me**, so what the
agents are on is not mistaken for work waiting on you.

Claims are **files**. A Claude Code plugin (`claude-plugin/`, a TypeScript
mod) *writes* them; `tt` only *reads* them (the UI, `tt task status`) and
offers manual commands. **The plugin never runs `tt`: the file contract below
is the whole interface.**

## Who does what

| | plugin (writer) | `tt` (reader) |
| --- | --- | --- |
| Claim files `<holder>.json` | writes, heartbeats, rewrites each turn | reads; writes only `human.*` ones (`tt task claim`) |
| Revocations `revoked/<holder>.json` | reads its own before each write, drops what was revoked | writes (`tt task release`), deletes orphans (`tt task gc`) |
| Expired files | stops refreshing | ignores; `tt task gc` deletes |
| Which task a claim is about | names a worktree path or a ref | resolves it against the stack store and `git worktree list` |

## Contract v1

Directory: `$TOOLBELT_HOME/claims/` (`TOOLBELT_HOME` is the override the stack
store already uses, default `~/.toolbelt`; this is DATA, not config).

One JSON file per holder: `<holder>.json`. The holder is the Claude session
id, plus `.<agent_id>` for a subagent; human/CLI holders are `human.<name>`.
Holder characters are `[A-Za-z0-9._-]` only. The `holder` field must equal the
file name without `.json` (a file that disagrees is ignored).

```json
{
  "version": 1,
  "holder": "<holder>",
  "kind": "agent",                // "agent" | "human"
  "session_id": "<claude session id or null>",
  "agent_id": null,               // subagent id or null
  "link": "claude://claude.ai/epitaxy/<host session id>",   // or null
  "state": "working",             // "working" | "idle" | "blocked"
  "started_at": "2026-10-12T20:00:00Z",
  "updated_at": "2026-10-12T20:00:30Z",
  "ttl_seconds": 180,
  "claims": [
    {"kind": "worktree", "path": "/Users/devon/git/wt/toolbelt/my-branch", "touched_at": "2026-10-12T20:00:10Z"},
    {"kind": "ref", "ref": "PLAT-412", "touched_at": "2026-10-12T20:00:12Z"}
  ]
}
```

- **Live** iff `now < updated_at + ttl_seconds`. The writer picks the ttl:
  working 180 (heartbeat every 30s), idle 1800, blocked 1800. Expired files
  are ignored by readers and deleted by `tt task gc`.
- **Semantics.** `working` live claims make the task "With an agent". `idle` =
  the agent's turn ended and it waits for the user; `blocked` = it waits on a
  permission prompt/question. idle/blocked live holders do NOT change the
  task's status; the UI shows a chip ("agent idle" / "agent blocked on you")
  with the session link. Claims are TURN-SCOPED: at a new turn the writer
  starts with an empty claims list.
- **Writes** were specified as atomic (a temp file in the same directory,
  then rename), but the plugin's mod API only has a plain overwrite: see
  "Amendments" below. Readers tolerate empty/unreadable/partial/unknown-version
  files either way: they are ignored for that read, and listed under
  `tt task status --verbose`.
- **Revocation.** `$TOOLBELT_HOME/claims/revoked/<holder>.json` =
  `{"version":1,"holder":"<holder>","revoked":[{"kind":"worktree","path":"...","at":"<ISO>","by":"tt task release"},{"kind":"ref","ref":"...","at":"<ISO>","by":"..."}]}`,
  written ONLY by `tt`. The writer reads its own revocation file before each
  write and drops claims whose `touched_at <= at`; a claim touched after the
  revocation stands. (This is how a force-release of a slow-but-alive holder
  sticks without write races.)
- **Resolution (tt side).** A `worktree` claim maps to the tracked branch
  whose worktree path it is (worktrees live at
  `$GIT_PROJECTS_WORKDIR/wt/<repo>/<name>`; resolved robustly through
  `git worktree list --porcelain` of each registered repo, cached per read,
  rather than by string layout alone); a `ref` claim resolves by branch name,
  task ref (the stored ticket) or task id. Unresolvable claims are ignored
  (and shown in verbose status).

### Amendments from the writer (binding)

The plugin's mod API has no delete, rename or atomic write, so:

1. **No delete on session end.** At session end (and when a subagent stops)
   the writer *rewrites its file already dead*: `state: "idle"`,
   `ttl_seconds: 0`, `claims: []`. `ttl_seconds` is therefore `>= 0`; a file
   with `ttl_seconds == 0`, like any expired file, is dead/released: never
   shown, deleted by `tt task gc`.
2. **Writes are a plain overwrite, not atomic.** A reader may see an empty or
   partial file. It skips that file for that read (the next poll sees the
   whole file) and must never error or flap the UI on one bad read. A file is
   *reported* (verbose status, `ClaimsFeed.unreadable`) only when it is
   invalid on two consecutive reads: `tt ui` keeps a holder's last good state
   across one unreadable poll, and `tt task status` re-reads 0.2 s later.
   (`tt` itself still writes `human.*` files and revocations atomically.)
7. **`tt` deletes what is dead.** The mod never deletes or renames. `tt ui`
   deletes a holder file once it has been expired (or a `ttl_seconds: 0`
   tombstone) for 10 seconds (the grace keeps a heartbeat that races the
   deletion from losing a claim; the file is re-read right before it is
   removed), and a revocation file whose holder file is gone.
   `tt task gc` does the same on demand, without the grace.
3. `link` is `null` when `CLAUDE_CODE_HOST_SESSION_ID` is unset, otherwise
   `claude://claude.ai/epitaxy/<that id>`.
4. **Machine-readable schema**: `claude-plugin/schema/claim.schema.json`
   (`additionalProperties: false` at the top level and on claims,
   `ttl_seconds >= 0`). `tests/test_task_claims.py` validates files produced
   by the test helpers, and `tt`'s own `human.*` files (apart from `kind`),
   against a copy of it (`tests/claim_schema/`).
5. Agent worktrees made by the Claude harness under
   `<repo>/.claude/worktrees/<name>` are not tracked toolbelt worktrees and
   are not claimed by the plugin; only `<GIT_PROJECTS_WORKDIR>/wt/<repo>/<name>`
   is. A worktree claim that resolves to nothing stays ignored (listed by
   `--verbose`).
6. Subagent holders are named `<session>.<agent_id>`, and are released (a
   ttl-0 rewrite) when the subagent stops or the main turn ends.

### Details this implementation fixes (clarifications of v1)

These are not new fields; they pin down what the contract left open.

- **Temp files** are named so they cannot be mistaken for a claim file: start
  with `.` and end with `.tmp` (e.g. `.<holder>.json.<pid>.tmp`) if a writer
  ever uses one. Readers only consider `<holder>.json` and skip dotfiles.
- **Worktree claims match exactly**: the `path` must be the worktree's root
  (what `git worktree list` prints; symlinks are resolved on both sides), not
  a subdirectory of it.
- **Ref claims** match a tracked branch name exactly, a task ref
  case-insensitively, and a task id exactly. One ref may match several
  branches (a ticket on a whole stack); each is claimed.
- **Timestamps** are ISO 8601 with an explicit offset (`Z` for UTC). A
  timestamp without one makes the file unreadable.
- **Stale**: a `working` holder that has not written for 90 seconds (three
  heartbeats) is *stale*: still live by its ttl (so still "With an agent"),
  flagged in `tt task status`, and revocable without `--force`.
- **Unknown extra fields** in a v1 file are ignored (so a writer may add
  fields without a version bump); an unknown `version` is unreadable.
- **A human holder counts as a holder**: a live `working` `human.*` claim
  also makes the task "With an agent" (it is how an agent without the plugin
  is marked).

## Commands

`tt task` (the claim machinery is only imported when you run it, so it adds
nothing to `tt`'s start-up):

```sh
tt task claim PLAT-412 devon/auth-api   # human claim: human.<user>.json, working, 8h
tt task claim . --ttl 30m               # a worktree directory works too
tt task status                          # who holds what, state, age, ttl left, STALE
tt task status --verbose                # + expired holders, revoked and unresolved claims, ignored files
tt task status --json                   # the same, machine-readable
tt task release PLAT-412                # a human claim is removed; an agent's is revoked
tt task release --holder <holder>       # all of one holder's claims
tt task release --all
tt task release PLAT-412 --force        # also an agent that is working and heartbeating
tt task gc                              # delete expired files, orphaned revocations, dead temp files
```

`release` of an agent's claim never edits the agent's file: it records a
revocation the writer honours (see above). An agent that is `working` and
heartbeating is refused without `--force`; a stale, idle or blocked one is
not. `release` exits 1 when it released nothing or refused something.

## In `tt ui`

- **Next** gains a value: `With an agent` (`who: "agent"`, `kind: "claimed"`),
  shown when a live `working` holder claims the branch. It carries the action
  it replaced (`would_be`, "would be: Resolve comments"). A merged/closed PR
  stays *Done* (the claim is ignored there, but still shown as a chip).
- A child that is approved but waits on a parent that reads "With an agent"
  is "Blocked on <parent>", waiting on *others* (the agent has the parent).
- `idle` / `blocked` holders do not change the status: the card gets a chip
  ("agent idle", "agent blocked on you") linking the session; a stale working
  holder is flagged.
- Grouped and Priorities have a **With an agent** section after **Needs me**;
  the Next filter has the value; a rolled-up stacked card counts them.
- **Live**: while a browser is connected the server re-reads the directory
  about once a second (the same gating and grace as the GitHub polling),
  re-evaluates TTL expiry on each tick, and pushes a `claims` event listing
  the branches whose holders changed *and* the branches stacked on them with
  their re-derived status. `git worktree list` is asked only when the set of
  claims changed (or every 15 s), never on a heartbeat.
- The **aggregate view** is unchanged: claims have no history. Its notes say
  so.
- The snapshot's `Branch.claim.holders[]` has `holder`, `kind`, `state`,
  `link`, `age` and `stale`; only live holders are sent.

## Failure modes

- **The agent died without cleaning up** (no ttl-0 rewrite): its file expires within `ttl`
  (180s for working); the task goes back to "Needs me" by itself.
- **The agent is hung but its file is still live**: `tt task status` shows it
  `STALE`; `tt task release <ref>` revokes its claims at once; `tt task gc`
  removes the file when it expires.
- **A claim is wrong or a slow-but-alive holder re-claims**: a revocation
  only drops claims touched at or before it, so a deliberate re-claim after a
  release stands.
- **Clock skew**: liveness compares the writer's `updated_at` with this
  machine's clock; both are the same machine, so there is none in practice.
- **A claim matches no tracked branch**: ignored by the UI, listed by
  `tt task status --verbose`.
- **Partial or future-version files**: ignored, listed by `--verbose`, never
  deleted by `gc` (a writer may be mid-way, or newer than this `tt`).
