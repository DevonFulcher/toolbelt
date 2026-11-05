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

## Tests

- Run pytest in parallel (`.venv/bin/python -m pytest -n 6 ...`, as the pre-commit
  hook does); a serial full run is several times slower. Most of the suite's time
  is tests that build real git repos.
- While iterating, run only the tests for the files you touched (pass paths or
  `-k`), plus `ruff check` and `ruff format` on them. Run the whole suite once,
  when the work is finished and before committing, not after every edit. The
  pre-commit hook runs it again on commit, so do not run it by hand right before
  `toolbelt git save`.
- The web suite is fast (`pnpm --dir web vitest run`, a few seconds): run all of
  it, not only the file you edited, because some tests list every card slot or
  API field (`timeline.test.ts`) and fail when one is added elsewhere. Also run
  `pnpm --dir web typecheck` and `pnpm --dir web check:types`.
- Run anything that may take minutes in the background with its full output sent
  to a file, and read the file when it ends. A foreground command is killed at ten
  minutes, and piping through `tail` loses the line that says what failed.

## Python

- The project targets Python 3.15 (`.python-version`, `requires-python`).
- Keep `tt` start-up fast: import heavy libraries (pandas, matplotlib, seaborn,
  boto3, and modules that pull in pydantic) with PEP 810 `lazy import` /
  `lazy from ... import`, at module level, and only where the name is used inside
  function bodies. `tests/test_cli_startup.py` and ruff's TID254/TID255 enforce
  this.
