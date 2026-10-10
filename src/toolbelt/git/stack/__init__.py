"""Homegrown stack + worktree management (the git-town replacement).

Parent lineage is persisted in a SQLite DB (see `store.py`).
This is now the sole stacking implementation: `git save`/`git sync`/`git change`
and the worktree commands all drive it, and git-town is no longer used. See
docs/stack-workflow-plan.md.
"""
