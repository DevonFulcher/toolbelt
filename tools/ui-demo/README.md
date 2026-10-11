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
| `history.py` | Generates the aggregate history: about 12 weeks of PRs and the archived task facts of their branches. |
| `gh` | A fake `gh` that answers the UI's single-PR GraphQL lookup, the untracked-PR search and the aggregate view's history search from canned scenarios. Timestamps in `SCENARIOS` are relative to now; the history's are absolute (written at seed time). |
| `run.sh` | Seeds, puts the fake `gh` first on `PATH`, and runs this checkout's `toolbelt ui`. |

Scenarios covered by the fake `gh` (branch -> state): approved and waiting to
merge, requested review with unresolved threads and a failed-then-fixed CI,
draft with CI running, changes requested with failing CI, merged, merge
conflicts, ready for review with CI running, and branches with no PR.

## The aggregate view's history

`?view=aggregate` is built from two sources, both seeded here:

* **GitHub** (the fake `gh`): about 35 merged or closed PRs over 12 weeks in
  `DEST/history.json`, with varied lead times (quick and slow reviews,
  changes requested, failing CI, drafts, a few closed unmerged, two stacked
  pairs whose child is approved before its parent merges), plus the open PRs
  of `SCENARIOS`. The two searches the view makes (`updated:>=...` and
  `is:open`) are answered from them.
* **The stack DB**: `removed_branches` holds the archived task facts (task
  start, removal, whether it landed) of the cleaned-up branches, and
  `archive_info.since` is set 9 weeks back, so older PRs show "no task start
  recorded": the unknown intervals the view must not invent. Three tasks
  never got a PR (worked on, then dropped).

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
