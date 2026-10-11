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
| `seed.py` | Two empty git repos (`demo-app`, `demo-search`) registered in a temp `TOOLBELT_HOME` with stacks, task refs, session deeplinks and backdated task start times. |
| `gh` | A fake `gh` that answers the UI's single-PR GraphQL lookup and the untracked-PR search from canned scenarios. Timestamps are relative to now. |
| `run.sh` | Seeds, puts the fake `gh` first on `PATH`, and runs `toolbelt ui`. |

Scenarios covered by the fake `gh` (branch -> state): approved and waiting to
merge, requested review with unresolved threads and a failed-then-fixed CI,
draft with CI running, changes requested with failing CI, merged, merge
conflicts, ready for review with CI running, and branches with no PR.

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
