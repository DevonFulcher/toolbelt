# Toolbelt Codex integration

This local Codex plugin writes the same task-claim and activity records used by
the Claude Code integration. It also records Codex model-usage events when the
hook reports token usage. The Toolbelt UI reads these files from
`$TOOLBELT_HOME` (default `~/.toolbelt`).

## Install in Codex

From this repository, add the local marketplace:

```sh
codex plugin marketplace add /path/to/toolbelt/codex-plugin
```

Then open the Codex desktop app's Plugins Directory, choose the Toolbelt
marketplace, install **Toolbelt**, and review and trust its local hooks. The
plugin's scripts must remain available at the path the marketplace references.
Restart Codex or start a new session after installing.

The Codex hooks receive event data through standard input. Toolbelt stores only
tool names, a limited command/path summary, timing, exit status, session IDs,
and reported token usage. It does not persist prompts, tool responses, or the
session transcript. Codex documents `transcript_path` as a convenience field
whose format is not a stable hook interface, so this integration does not parse
it.

At each prompt, the plugin also supplies the Codex session ID and
`codex://threads/<session_id>` link as concise additional context. When calling
Toolbelt Decisions, Codex can pass the explicit `session_id` argument so the
decision links back to the right session.

## Claims and cleanup

Codex has no periodic heartbeat hook. An active claim therefore uses a
30-minute TTL and is refreshed on hook events. A completed turn becomes idle;
`SessionEnd` and `SubagentStop` write an expired claim with no claims. A killed
or disconnected Codex process may leave a claim visible until its TTL expires.
Manual `tt task release` revocations are checked on every claim write and are
preserved. Subagent hooks share the parent session ID and add the Codex agent ID
to the holder name.

## Stored records

- Claims: `$TOOLBELT_HOME/claims/<session>[.<agent>].json`, contract v1.
- Activity: `$TOOLBELT_HOME/activity/<holder>.<turn>.<seq>.json`, contract v1.
- Model usage: `$TOOLBELT_HOME/usage/codex/<session>.jsonl`, one event per
  `PostModelRequest` that includes `token_usage`.

The usage event retains Codex's raw `token_usage` object. The Toolbelt Codex
provider maps its token fields when ingesting it.

## Development

Run the standalone hook tests with:

```sh
uv run python -m unittest discover -s codex-plugin/plugin/tests
```
