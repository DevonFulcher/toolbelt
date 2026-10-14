# tt ui demo

Self-contained data for looking at, developing and checking `tt ui` without a
GitHub account, a network, or any of your real repos.

```bash
tools/ui-demo/run.sh                 # http://127.0.0.1:8771
tools/ui-demo/run.sh --deep 14       # adds a 14-deep linear stack (depth cap, re-rooting)
tools/ui-demo/run.sh --port 8800 --dir /tmp/my-demo
```

## What is in it

| Piece | What it is |
|---|---|
| `seed.py` | Two git repos (`demo-app`, `demo-search`, each with an `origin` URL) registered in a temp `TOOLBELT_HOME` with stacks, task refs, session deeplinks and backdated task start times, plus the aggregate view's history (below). |
| `seed.py` (claims) | Task claim files in `home/claims/` (see below). |
| `history.py` | Generates the aggregate history: about 12 weeks of PRs and the archived task facts of their branches. |
| `gh` | A fake `gh` that answers the UI's single-PR GraphQL lookup, the untracked-PR search and the aggregate view's history search from canned scenarios. Timestamps in `SCENARIOS` are relative to now; the history's are absolute (written at seed time). |
| `run.sh` | Seeds, puts the fake `gh` first on `PATH`, and runs this checkout's `toolbelt ui`. |

Scenarios covered by the fake `gh` (branch -> state): approved and waiting to
merge, requested review with unresolved threads and a failed-then-fixed CI,
draft with CI running, changes requested with failing CI, merged, merge
conflicts, ready for review with CI running, and branches with no PR.

## Task claims

`seed.py` writes claim files (`docs/task-claims.md`) as a Claude Code session
would, so the UI's "With an agent" status can be looked at without one:

| Holder | State | On | What it shows |
|---|---|---|---|
| `sess-working` | working | `devon/auth-docs` (failing CI) | "With an agent", "would be: Fix / re-run CI", a session link, and the card in the *With an agent* section |
| `auto.demo-run` | working, autonomous (contract v2) | `devon/cache-metrics` (merge conflicts) | "Autonomous agent", "would be: Resolve conflicts", and the card in the *Autonomous agent* section |
| `sess-idle` | idle | `devon/cache-layer` | an "agent idle" chip; the status is unchanged |
| `sess-blocked` | blocked | `devon/auth-ui` | an "agent blocked on you" chip |
| `sess-expired` | working, expired 2h ago | `devon/search-index` | nothing: ignored, then deleted by `tt ui` after a grace |
| `sess-revoked` | working, claim revoked | `devon/cache-metrics` | nothing: the revocation is newer than the claim |

The live holders have an 8-hour ttl so they outlast a look at the demo (a
plugin's is 3 minutes with a heartbeat). Nothing heartbeats them, so the
working holder shows as "stale" after 90 seconds: that is the stale flag. Try the commands against the demo with
`TOOLBELT_HOME=<dir>/home tt task status --verbose`, `tt task release
devon/auth-docs --force` (the card moves back to "Needs me" within a second)
and `tt task claim devon/search-ranking`.

## The aggregate view's history

`?view=aggregate` is built from two sources, both seeded here:

* **GitHub** (the fake `gh`): about 35 merged or closed PRs over 12 weeks in
  `DEST/history.json`, with varied lead times (quick and slow reviews,
  changes requested, failing CI, drafts, a few closed unmerged, two stacked
  pairs whose child is approved before its parent merges), plus the open PRs
  of `SCENARIOS`. The two searches the view makes (`updated:>=...` and
  `is:open`) are answered from them. The server warms this history in the
  background and the page reads its cache.
* **The stack DB**: `removed_branches` holds the archived task facts (task
  id, PR number and node id when recorded, task start, removal, whether it
  landed) of the cleaned-up branches: about two thirds carry their PR's
  identity (joined by id), the rest do not (matched by branch name and
  flagged), and one branch name is used by two tasks. Some tracked branches
  have a recorded PR link too. `archive_info.since` is set 9 weeks back, and
  so older PRs show "no task start recorded": the unknown intervals the view
  must not invent. Three tasks never got a PR (worked on, then dropped).

The fake `gh` gives every PR an `id`, `url` and `updatedAt` and answers the
`updated:>=<time>` search the background refresh makes, which re-queries only
the open PRs and what changed. Commits carry check suite
completion times (`suite=<minutes>` in `commit(...)`), so a card whose last
commit failed (`devon/auth-docs`) shows "Waiting on CI" and then "CI failing".

The history comes from a fixed random seed (`history.py`) and is placed
relative to the time you seed. To see the empty state, run `tt ui` against a
fresh `TOOLBELT_HOME` with a `gh` that finds nothing.

## Adding a scenario

Add a `pr(...)` entry to `SCENARIOS` in `gh` keyed by branch name, and a row
in `STACKS` in `seed.py`. Branches without an entry have no PR.

## Not covered here

States that need a second GitHub user (an approval, a requested change, a
requested reviewer from someone else) are only reproducible with the fake `gh`.
`docs/ui-validation-fixture.md` describes the real private GitHub repo used to
check everything else against live data.

How this fits with the other checks, and how to measure layout across widths:
[`docs/ui-testing.md`](../../docs/ui-testing.md).
