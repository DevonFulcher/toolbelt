# toolbelt-claims

A Claude Code mod (function hooks) that tells toolbelt which tracked
branches and worktrees an agent session is working on. It does this by writing
**claim files**; `tt` reads them. The files are the only interface: the mod
never spawns `tt` or any other process, because `tt` takes about two seconds to
start and the mod runs on the tool-call path.

The file format is contract v1, documented in `docs/task-claims.md`. A JSON
Schema for one file is in `schema/claim.schema.json`.

## What it records

One file per holder at `$TOOLBELT_HOME/claims/<holder>.json` (default
`~/.toolbelt/claims`). The holder is the session id, plus `.<agent_id>` for a
subagent.

| Signal | Claim |
| --- | --- |
| `Edit`, `Write`, `NotebookEdit` of a file under `$GIT_PROJECTS_WORKDIR/wt/<repo>/<name>/` | worktree `<root>/<repo>/<name>` |
| `Bash` whose working directory (or leading `cd`, or `git -C`) is in such a worktree | the same worktree |
| the `task_claim` tool, `refs: string[]` | one `ref` claim per ref |

Reads claim nothing. Paths outside `$GIT_PROJECTS_WORKDIR/wt` claim nothing
(if the variable is not visible to the mod, the first `/wt/` path segment is
used instead).

State and lifetime:

- `working` while a turn runs: `ttl_seconds` 180, rewritten every 30 s.
- `idle` when the turn ends (also when it fails or is interrupted): ttl 1800,
  claims kept.
- `blocked` on a permission prompt or input dialog: ttl 1800. The next tool
  result puts it back to `working`.
- Claims are turn-scoped: every turn starts with an empty list.
- A subagent gets its own file, released when it stops (and all subagent files
  are released when the main turn ends).
- Before each write the mod reads `claims/revoked/<holder>.json` (written by
  `tt`) and drops any claim touched at or before its revocation time.
- The status line shows `claimed: N` while working and something is claimed.

Files are written only on a state change, the first touch of a new worktree or
ref in a turn, and the heartbeat, never per tool call. Write errors are swallowed
(debug log only).

## Install

```sh
claude-plugin/install.sh
```

That is `claude plugin marketplace add <this folder>` followed by
`claude plugin install toolbelt-claims@toolbelt --scope user`, made idempotent
(a second run updates). The dotfiles `bootstrap.sh` runs it. It exits 0 with a
message when `claude` is not on PATH, and honours `CLAUDE_CONFIG_DIR`.

Start a new session afterwards; the mod loads at session start. A user-scope
install is also what makes it load in the desktop app's Code tab.

## Uninstall

```sh
claude plugin uninstall toolbelt-claims@toolbelt
claude plugin marketplace remove toolbelt
```

Leftover claim files expire by themselves.

## When the process is killed

Nothing runs at a kill, so the file stays. It expires at `updated_at +
ttl_seconds` (180 s if it died mid-turn, up to 1800 s if idle). Nothing needs
recovering: claims are turn-scoped.

## Known limits

- The function-hooks API is early access and moves between releases. The events
  used are: `session.start`,
  `turn.start`, `turn.complete`, `session.end`, `tool.call`, and the classic
  `SubagentStart`, `SubagentStop`, `PermissionRequest`, `Notification`,
  `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`, `CwdChanged`.
- The mod API has no delete or rename. Session end therefore rewrites the file
  already expired (`ttl_seconds` 0, no claims) instead of deleting it, and
  writes are a plain overwrite rather than temp file plus rename. Readers must
  skip a file that fails to parse and may garbage-collect expired files.
- Bash is judged by the tracked working directory, a leading `cd <path> &&`,
  and `git -C <path>`. A path computed inside the command is not seen.
- Loaded per session: sessions that started before the install do not have it.
- The `.claude/worktrees/<name>` layout is not recognised, only `wt/<repo>/<name>`.

## Development

```sh
claude plugin validate claude-plugin
claude plugin test claude-plugin
```

Tests use the engine's test kit (`claude plugin test` needs Claude Code 2.1.295
or newer). `tests/claims.test.ts` carries an inline copy of
`schema/claim.schema.json` because a test cannot import a `.json` file; keep
them identical.
