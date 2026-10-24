# Conflicts between open tasks

`tt ui` tells a card when its branch would conflict with another open task of
the same repo. It only detects: nothing is merged, rebased or fixed for you.

## What is tried

Every pair of tracked branches of a repo whose PR is not merged or closed,
except a branch and its ancestors in the stack (one chain is `tt g sync`'s job).
Siblings, cousins and unrelated stacks are all tried. Ancestry is only used to
leave pairs out; a conflict is never decided from it (a squash merge makes
ancestry lie), it comes only from merging the two trees.

`git merge-tree --write-tree --name-only` merges two commits in memory, with
their natural merge base (no `--merge-base`), and lists the conflicted files.
One `git merge-tree --stdin` runs per repo and pass (`sibling_conflicts.py`).

Nothing is ignored: lockfiles and generated files are reported like any other.

## Where the heads come from, and "unknown"

A branch's head is its local branch ref, else its PR's head commit. The UI never
fetches. A head that is not in the local repo (a PR pushed from another machine
and not fetched) is **unknown**: the pair is shown as "unknown, head not
fetched", never as clean. A task whose own head is unknown says so on its card
when it has other tasks to be compared with.

## Caching

The answer is a pure function of the two commits, so it is stored for good in
the `sibling_conflicts` table by (repo, oid, oid) with the oids sorted, and rows
whose commits are no longer a head are dropped. A pass reads every head (one
`git for-each-ref`), looks every pair up and tries only the ones it has not
seen: when a head moves, only that branch's pairs (n-1 of them) are new. Cost on a
50,000-file repo is about 8 ms per pair one by one and well under 1 ms in a batch;
30 tasks are at most 435 pairs.

## When it runs

* While a browser is connected, every 10 seconds (heads move with a local commit
  that GitHub does not know about).
* `app.state.sibling_conflicts.fresh_conflicts_for(repo_id, branch)` forces a
  fresh pass for the repo and answers for the branch: call it right before a
  personal review starts and right before a merge. It returns
  `SiblingConflictsInfo`, or `None` when the branch merges cleanly.

## Inherited conflicts

Two cousins can conflict only because their parents do. When the parents' heads
conflict in every file the pair conflicts in, the pair is **inherited**: the card
says "conflicts with X through their parents".

## On the card

A chip next to Next (`api.SiblingConflictsInfo`, a field of the branch row next
to `merge_queue`, sent again in `status` events when it changes):
"conflicts with <task>: file1, file2, file3 (+n)", at most ten files kept and
three shown; more than three tasks collapse into "conflicts with N tasks". It is
red for a task whose PR is open and not a draft (in review or ready to merge),
and gray for the rest. It is not a Next kind and not a timeline lane: nothing is
recorded (`SiblingConflictState` is in `NOT_TIMELINE_STATUS`).

## What cannot be seen

* Conflicts are textual. A rename of a function the other side still calls merges
  cleanly. Distant hunks and renames merge cleanly too.
* Three-way interactions are not tried: A, B and C may each merge with the
  others and still not all three. For text merges a conflict needs two
  overlapping or adjoining changes, which a pair shows; a triple can only
  conflict through rename and directory-rename edge cases.
* "Inherited" only looks at the two branches' direct parents.
