# Merge when ready

A PR whose next action is **Waiting on CI**, or **Blocked on** the PR it is
stacked on, shows a **Merge when ready** button next to it. Pressing it queues
the PR; once its next action is Merge (CI passed, approved, no comments or
conflicts, and any parent merged) `tt ui` merges it exactly as the Merge button
does (`toolbelt git merge`: squash-merge, then sync the stack), pinned to the
head commit that was queued (`--match-head-commit`), so nothing pushed after it
is merged.

## What can be queued

- A PR waiting on CI, whether or not it is approved yet: it stays queued through
  review and merges once it is approved and its CI has passed.
- A PR that is approved and blocked on a PR it is stacked on that is still open.
  It can be queued on any level of a stack. Each PR merges after its parents,
  one at a time.
- A PR that has just been published (**Publish PR** with the Slack post) is
  queued automatically, whether CI is running, still to start or already
  passed, and whether its Slack post or a review is next. One that is already
  ready to merge is left for the Merge button.

A branch an agent holds or has a question open for reads differently, so it
cannot be queued until it is free.

## What is never merged

A queued PR is taken off the queue, with a note on its card saying why, when:

- new commits were pushed after it was queued (what was approved is not what
  would be merged), so queue it again when it is ready. The one head change
  that is accepted is the sync after a parent merged: a merge commit whose first
  parent is the queued head and whose second parent is already on the trunk or
  the parent's branch. The queue then pins that new head;
- its CI failed, a comment or conflict appeared, changes were requested, or it
  became a draft again;
- it was closed without merging, or the PR it is stacked on was closed without
  merging;
- it was queued on the trunk but its PR is based on another branch: the queue
  only merges into the trunk.

A PR merged some other way, or no longer tracked, leaves the queue quietly. A PR
an agent holds stays queued until the agent is done. Waiting for CI, review or a
parent keeps it queued.

## Stacked PRs

Every queued PR is merged only into the trunk, never into another branch of a
stack. A queued PR remembers the PR it was stacked on, because `sync` forgets a merged
parent and reparents the branch onto the trunk. Once its next action is Merge it
is still held back until all of these hold:

- the parent PR is merged (GitHub says so);
- GitHub has retargeted it onto the trunk, so it never merges into the parent's
  old branch;
- its head contains the parent's squash commit, so it carries what the parent
  landed and is not merged ahead of the sync.

These are checked again right before the merge starts, after any merge ahead of
it in the queue has finished restacking.

If the merge itself fails (GitHub refuses, for instance), the PR leaves the queue
and the card shows the end of `toolbelt git merge`'s output. Queued merges run
one at a time, because each one restacks the stack it belonged to.

## How it works

The queue is the `merge_queue` table of the stack DB (the PR, its head commit
when it was queued, the PR it was stacked on, and when), so a restart keeps it.
It is checked every ten seconds against the PR lookups the page already keeps
current; a merge starts within that time of the PR being ready.
`POST /api/merge/queue` and `/api/merge/unqueue` (body
`{"id": "<repo id>:<branch>"}`) queue and cancel.

The Merge button uses the same background path: `POST /api/merge` answers `202`
at once, the card reads "Merging…", and if `toolbelt git merge` fails the card
shows its output ("The merge failed: …") until the next attempt.

## Paused in the evening and on weekends

The queue starts no merges from 16:00 (15:00 on Fridays) until 08:00 the next
morning, and none on Saturday and Sunday (until Monday 08:00), in this machine's
local time. A queued PR stays queued through it, its card says when the queue
resumes, and it merges once the pause is over and it is ready. The Merge button
is not affected by the pause. The hours are in
`src/toolbelt/ui/merge_queue_pause.py`.
