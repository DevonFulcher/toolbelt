# Agent activity

What an agent does in a turn, not only that it is running (a claim says that).
The Claude Code plugin (`claude-plugin/`) records every tool call; `tt ui`
reads it into the stack store and the aggregate view shows it.

## What is recorded

The plugin wraps each `tool.call` (it times the call and passes it on
unchanged) and keeps, per turn:

| Field | |
| --- | --- |
| `tool`, `detail` | the tool and its command (Bash) or path / pattern / URL / query / text / description / prompt (the first the call has), cut to 500 characters |
| `started_at`, `duration_ms` | when and how long, from the same clock as the claim files |
| `blocked_ms` | the part of the call spent blocked on a permission prompt or dialog |
| `ok` | `false` when the failure hook reported it, the user denied it, or its result came back as an error (it failed, or a hook refused it); `null` when it never ended |
| `worktree` | the worktree it was about: a path it names, else where its command ran (a `cd` anywhere in the chain moves the rest; `-C`, `--dir`, `--project`, `--cwd` and absolute paths among the words are read), else where the session is; failing that, the one worktree the turn claimed. Reads are attributed too, although they claim nothing |
| `background`, `delegated` | a background Bash call; a call that starts a subagent (`Task`) |

A turn's wall time is the engine's `durationMs` for the turn, split in parts
that add up to it:

- **tool**: the union of the calls' intervals (parallel calls count once), less
  blocked and delegated time;
- **delegated**: waiting on subagents (the subagent's own calls are its own
  holder's, `<session>.<agent>`);
- **blocked**: permission prompts and dialogs, from the holder's `blocked` state;
- **model**: the rest: thinking, streaming, API latency.

A call still running when the turn ends is closed with it and marked `unended`.

## What else the plugin can see

Seen in real events (captured with a throwaway probe) but not recorded yet:

- `classic.PostToolUse` and `PostToolUseFailure` carry the engine's own
  `duration_ms` for the call (a cross-check on the plugin's timing),
  `tool_use_id`, `tool_response` (for Bash: `stdout`, `stderr`, `interrupted`)
  and, for a failure, `error` and `is_interrupt`; both also carry `agent_id`,
  `agent_type`, `permission_mode`, `effort.level`, `prompt_id` (groups the turns
  of one user prompt) and `transcript_path`.
- The `tool.call` result is `{ref, result, text}` plus `isError` for a failed or
  refused call and `isReadOnly` for a read-only one.
- A call refused by a hook fires no `PostToolUse` or `PostToolUseFailure`; it is
  only visible as `isError` on the result, which is what `ok` reads.

## The files

`$TOOLBELT_HOME/activity/<holder>.<turn>.<seq>.json` (JSON; one chunk per
file, never rewritten): `version` 1, `holder`, `session_id`, `agent_id`,
`turn_id`, `seq`, `final`, `started_at`, `ended_at`, `reason`, `totals` (final
only) and `calls`. A long turn writes a chunk every five minutes or 200 calls,
so a kill loses little; the last one is `final` and carries the totals. `tt`
removes a file once it is stored; an unreadable one is kept while it may still
be being written and dropped after ten minutes. The claim file's optional
`activity` field (see `task-claims.md`) says what the holder is running now.

## Autonomous runs

A run loads only the project's settings, not the user's plugins, so the plugin
records nothing for it. When a run has ended, `tt ui` reads its Claude Code
transcript instead (`agent_activity/run_transcripts.py`, every minute, for the
runs of the last 60 days that are not stored): each `tool_use` and its
`tool_result` give a call and its duration, a call with no result when the run
ended is `unended`, and the entries made while the run was running (a resumed
run's session holds the runs before it) are the turn. The turn is named
`run-<run id>` and its time is split like the plugin's, with no blocked time:
tool time is the union of the calls, subagent calls (`Task`) are delegated, and
the rest is the model's. The calls are classified and tied to a task by the
`cwd` the transcript records, then stored and shown like the plugin's. A run
whose session the plugin also recorded is left to the plugin.

## In the store

`agent_tool_calls`: the raw calls, classified (`agent_activity/shell.py`: the
main program of a command, its subcommand, and a category: edit, read, search,
files, vcs, github, test, build, lint, deps, run, network, infra, toolbelt,
subagent, web, mcp, other) and tied to the task whose worktree they were about.
**Kept 60 days**; classification can be improved and re-run over them.

`agent_turns`: one row per turn, **kept**: the totals and, by tool and by
category, the calls and the call time summed over them (so parallel calls add
up past the tool time). A turn whose writer died is closed from its calls after
an hour (`reason: unfinished`; tool time is then the summed call time, not the
union).

## In the aggregate view

An "Agent activity" section (`Aggregate.activity`, hourly sums in UTC that the
page makes local days or weeks): agent time by kind (model, tools, subagents,
blocked on you), call time by category, and the tools by call time. It is
activity, not status, so it is not a lane (see `timeline-lanes.md`) and not on
the card timeline. Recording begins when the plugin is updated; earlier turns
are unknown, not zero. A turn is tied to a repo by the worktree most of its
calls were about; turns with none are only in the all-repos view.

## Updating

`claude-plugin/install.sh`, then start a new session: the mod is loaded per
session. The plugin version is 0.2.2.
