# CLAUDE.md

## Git workflow

- This is a personal repo. Don't worry about branching — commit directly to the
  current branch (e.g. `main`) and push right after committing.

## Python

- The project targets Python 3.15 (`.python-version`, `requires-python`).
- Keep `tt` start-up fast: import heavy libraries (pandas, matplotlib, seaborn,
  boto3, and modules that pull in pydantic) with PEP 810 `lazy import` /
  `lazy from ... import`, at module level, and only where the name is used inside
  function bodies. `tests/test_cli_startup.py` and ruff's TID254/TID255 enforce
  this.
