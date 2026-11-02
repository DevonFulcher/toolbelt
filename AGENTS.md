# AGENTS.md

## Git workflow

- For the dotfiles and toolbelt repositories, work and commit directly on `main`;
  do not create task branches or pull requests unless explicitly requested. Use
  `toolbelt git save` to commit and push verified changes, pushing immediately
  after committing in this repo.
- Commit and restart the live `tt ui` service
  (`launchctl kickstart -k gui/$(id -u)/com.user.toolbelt.ui`) as work
  progresses, without asking or waiting for a go-ahead. A restart kills a running
  agent run, so check `/api/agents` for one first and hold the restart until it
  ends. The pre-commit hook runs the whole test suite (several minutes) and
  stashes unstaged files, so leave the working tree alone while it runs. After a
  web change run `pnpm --dir web build` first: the service serves the built
  bundle.

## Shared instructions

- Keep repository instructions in `AGENTS.md`. The dotfiles manifest manages
  `CLAUDE.md` as an ignored compatibility symlink; do not maintain a second copy.

## Delegation

- Handle simple, single-shot requests directly. For medium or high complexity
  work, use Codex subagents when available.
- Split work across agents when independent tracks have clear deliverables. Keep
  tightly coupled work together and avoid overlapping file ownership.
- Give each agent a scoped brief with the goal, relevant context and files,
  deliverable, constraints, dependencies, and whether it may edit files. Preserve
  and attribute user-provided decisions and wording.
- For sustained work, get an agent's plan before implementation. Treat agent
  findings and diffs as unverified until reviewed against the request and repo
  guidance.
- Use Codex's available multi-agent tools for dispatch and follow-up; do not rely
  on Claude-specific `Agent` or `SendMessage` tools. If subagents are unavailable
  or cannot start, proceed directly and say delegation was unavailable.

## Web UI

- Every action responds at once and shows it is in progress: a disabled button
  that says what it is doing ("Starting…", "Posting in Slack…"), never a click
  that leaves the user wondering whether anything is loading.
- Anything that takes more than a moment (git, GitHub, Jira, Slack, starting an
  agent, a merge) runs in the background on the server. The endpoint checks what
  it cheaply can, answers `202` and does the work after; the outcome (done, or
  why it failed) reaches the card through the live event stream, not through the
  request, and a failure is shown on the thing it concerns. See
  `docs/async-ui.md`.

## Python

- The project targets Python 3.15 (`.python-version`, `requires-python`).
- Keep `tt` start-up fast: import heavy libraries (pandas, matplotlib, seaborn,
  boto3, and modules that pull in pydantic) with PEP 810 `lazy import` /
  `lazy from ... import`, at module level, and only where the name is used inside
  function bodies. `tests/test_cli_startup.py` and ruff's TID254/TID255 enforce
  this.
