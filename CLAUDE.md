# CLAUDE.md

## Git workflow

- This is a personal repo. Don't worry about branching — commit directly to the
  current branch (e.g. `main`) and push right after committing.
- Commit and restart the live `tt ui` service
  (`launchctl kickstart -k gui/$(id -u)/com.user.toolbelt.ui`) as work
  progresses, without asking or waiting for a go-ahead. A restart kills a running
  agent run, so check `/api/agents` for one first and hold the restart until it
  ends. The pre-commit hook runs the whole test suite (several minutes) and
  stashes unstaged files, so leave the working tree alone while it runs. After a
  web change run `pnpm --dir web build` first: the service serves the built
  bundle.

## Python

- The project targets Python 3.15 (`.python-version`, `requires-python`).
- Keep `tt` start-up fast: import heavy libraries (pandas, matplotlib, seaborn,
  boto3, and modules that pull in pydantic) with PEP 810 `lazy import` /
  `lazy from ... import`, at module level, and only where the name is used inside
  function bodies. `tests/test_cli_startup.py` and ruff's TID254/TID255 enforce
  this.
