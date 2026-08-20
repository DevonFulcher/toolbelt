"""Homegrown stack + worktree management (the git-town replacement).

Parent lineage is persisted in its own `toolbelt-stack.*` git-config namespace.
This is now the sole stacking implementation: `git save`/`git sync`/`git change`
and the worktree commands all drive it, and git-town is no longer used. See
docs/stack-workflow-plan.md.
"""
