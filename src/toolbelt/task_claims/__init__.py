"""Task claims: which agent session (or person) is working on which task.

The writer side is a Claude Code plugin that drops one JSON file per holder
into ``$TOOLBELT_HOME/claims/``; this package only reads those files (and
writes ``human.*`` files and revocations on behalf of ``tt task``). The file
contract is documented in ``docs/task-claims.md``.
"""
