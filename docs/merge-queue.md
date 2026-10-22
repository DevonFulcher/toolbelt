# Merge when CI passes

A PR that is approved and waiting on nothing but CI shows a **Merge when CI
passes** button next to its "Waiting on CI". Pressing it queues the PR; once its
CI has passed (so its next action is Merge) `tt ui` merges it exactly as the
Merge button does (`toolbelt git merge`: squash-merge, then sync the stack).

## What can be queued

The PR's CI is running and, once it passed, Merge would be its next action: it
is approved, open and not a draft, has no conflicts or unresolved comments, and
any PR it is stacked on has merged. No agent may hold it.

## What is never merged

A queued PR is taken off the queue, with a note on its card saying why, when:

- new commits were pushed after it was queued (what was approved is not what
  would be merged), so queue it again when it is ready;
- its CI failed, a comment or conflict appeared, changes were requested, its
  approval was removed, or it became a draft again;
- it was closed without merging.

A PR merged some other way, or no longer tracked, leaves the queue quietly. A PR
an agent holds stays queued until the agent is done.

If the merge itself fails (GitHub refuses, for instance), the PR leaves the queue
and the card shows the end of `toolbelt git merge`'s output. Queued merges run
one at a time, because each one restacks the stack it belonged to.

## How it works

The queue is the `merge_queue` table of the stack DB (the PR, its head commit
when it was queued, and when), so a restart keeps it. It is checked every ten
seconds against the PR lookups the page already keeps current; a merge starts
within that time of the CI passing. `POST /api/merge/queue` and
`/api/merge/unqueue` (body `{"id": "<repo id>:<branch>"}`) queue and cancel.
