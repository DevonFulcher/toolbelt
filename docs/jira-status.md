# Jira status follows the branch

With `tasks.tracker: jira` a branch's task moves through the board on its own:

| When | Status it moves to | From |
| --- | --- | --- |
| `tt g append --task KEY` creates the branch | `statuses.in_progress` | a "To Do" status only |
| `tt ui` sees the branch's PR open (not a draft) | `statuses.in_review` | a "To Do" status, or `in_progress` |
| `tt g merge` has merged the PR | `statuses.done` | anything not already done |

A task further along is never moved back (a ticket in QA stays in QA when its PR
opens). A task that is moved is also assigned to the account the API token
belongs to.

```yaml
tasks:
  jira:
    statuses:           # the defaults; name your board's own
      in_progress: In Progress
      in_review: In Review
      done: Done        # a project that ends in "Closed" says Closed
```

Every move is best-effort: a missing token, an unreachable ticket or a board
with no such transition is a warning, and the git command or the page carries on.
`tt ui` moves a PR's task once (kept in the stack DB's `task_status_moves`), so a
ticket someone moves back afterwards stays where they put it. It sweeps the
tracked PRs once a minute, so PRs that were already open when it started are
moved too, the first time it sees them.
