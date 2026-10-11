# `tt ui` validation fixture

`DevonFulcher/toolbelt-gh-stack-validation` (private) is a standing fixture for
checking `tt ui` against real GitHub: the GraphQL payload parser
(`src/toolbelt/ui/pr_fetch.py`), the next-action rules and the callout of
untracked PRs. Only that repo is ever written to. For the other checks (unit tests, the fake-`gh`
demo, layout measurement) see [`ui-testing.md`](ui-testing.md).

## What exists in it

| Branch (base)              | PR   | State it exercises                                       |
| -------------------------- | ---- | -------------------------------------------------------- |
| `a` (main), `b` (a)        | #1, #2 | a two-branch stack; `b` is stacked on `a`              |
| `c` (develop), `d` (c)     | #3, #4 | a stack whose root is on a base other than `main`      |
| `val/draft` (main)         | #7   | a draft PR                                               |
| `val/ci-fail` (main)       | #8   | commit status `failure` (`ci/test`): CI failed, 1 failure |
| `val/ci-pass` (main)       | #9   | commit status `success`                                  |
| `val/ci-pend` (main)       | #10  | commit status `pending` (switch to `success` to watch a card move from "Waiting on CI" to "Needs me") |
| `val/threads` (main)       | #11  | one unresolved inline review comment (a review thread)   |
| `val/merged` (val/base)    | #12  | a merged PR (merging also records a ClosedEvent)         |
| `val/conflict` (val/base)  | #13  | conflicts with its base (`shared.txt` edited on both)    |
| `val/untracked` (main)     | #14  | an open PR that is *not* registered in the stack DB (the callout) |
| `val/nopr`                 | none | a pushed branch without a PR                             |

`val/base` exists so the conflict and merge scenarios never touch `main`.

Not reproducible alone (they need a second GitHub user): approved, changes
requested, a requested reviewer. Those stay covered by the fake `gh` /
canned payloads in `tests/test_pr_fetch.py` and `tests/test_ui*.py`.

CI is faked through the commit status API; the token has no `workflow` scope,
so no workflow files can be pushed:

```sh
gh api repos/DevonFulcher/toolbelt-gh-stack-validation/statuses/<sha> \
  -f state=failure|success|pending -f context=ci/test
```

## Reproducing the scenarios

In a clone of the repo (each `mk` makes a branch with one commit):

```sh
REPO=DevonFulcher/toolbelt-gh-stack-validation
mk() { git checkout -q -B "$1" "origin/$2"; echo "$1" > "$(echo "$1" | tr / -).txt"
       git add -A; git commit -qm "add $1"; git push -q -u origin "$1"; }

git checkout -q -B val/base origin/main; echo base > shared.txt
git add -A; git commit -qm "val/base: shared file"; git push -q -u origin val/base

mk val/draft main;   gh pr create --draft --base main --head val/draft --title "validate: draft PR" --body fixture
for s in ci-fail:failure ci-pass:success ci-pend:pending; do
  mk "val/${s%%:*}" main
  gh pr create --base main --head "val/${s%%:*}" --title "validate: ${s%%:*}" --body fixture
  gh api "repos/$REPO/statuses/$(git rev-parse HEAD)" -f state="${s##*:}" -f context=ci/test
done
mk val/threads main; gh pr create --base main --head val/threads --title "validate: unresolved thread" --body fixture
gh api "repos/$REPO/pulls/<its number>/comments" -f body="inline comment" \
  -f commit_id="$(git rev-parse HEAD)" -f path=val-threads.txt -F line=1 -f side=RIGHT
mk val/merged val/base; gh pr create --base val/base --head val/merged --title "validate: merged" --body fixture
gh pr merge val/merged --merge
git checkout -q -B val/conflict origin/val/base; echo "conflict side" > shared.txt
git add -A; git commit -qm "val/conflict edits shared.txt"; git push -q -u origin val/conflict
gh pr create --base val/base --head val/conflict --title "validate: conflicts" --body fixture
git checkout -q val/base; git pull -q origin val/base; echo "base side" > shared.txt
git add -A; git commit -qm "val/base edits shared.txt"; git push -q origin val/base
mk val/untracked main; gh pr create --base main --head val/untracked --title "validate: untracked" --body fixture
```

## Pointing `tt ui` at it

Register the clone's tracked branches in a throwaway `TOOLBELT_HOME` (not the
real one), then run the real `tt ui` with the real `gh`. `tt g set-parent`
reparents the *current* branch and syncs, so the stack DB is written directly:

```python
# TOOLBELT_HOME=$(mktemp -d) uv run python seed.py <clone>
import sys
from pathlib import Path
from toolbelt.git.stack import lineage

parents = {"a": "main", "b": "a", "c": "develop", "d": "c",
           "val/draft": "main", "val/ci-fail": "main", "val/ci-pass": "main",
           "val/ci-pend": "main", "val/threads": "main",
           "val/merged": "val/base", "val/conflict": "val/base", "val/nopr": "main"}
for branch, parent in parents.items():
    lineage.set_parent(branch, parent, root=Path(sys.argv[1]))
```

```sh
TOOLBELT_HOME=<the same dir> tt ui
```

`val/untracked` is deliberately left out, so it is the one entry of the
"Open PRs not tracked by toolbelt" callout (plus, on an account whose token is
not authorized for a SAML-protected organization, the "N open PRs could not be
read" line).

## What real GitHub taught the parser

* Merging a PR also records a `ClosedEvent` at the same instant as the
  `MergedEvent`; it is dropped (`parse_snapshot`), or a merged PR's timeline
  showed a spurious "closed without merging" marker.
* A search across organizations answers what it may and reports the rest as
  `FORBIDDEN` errors (a SAML-protected organization): `gh` exits non-zero with
  partial data. `gh_search` keeps the partial result and counts the hidden PRs.
* The commit status API's latest state per context wins; GraphQL reflects a new
  status within one or two polls.
