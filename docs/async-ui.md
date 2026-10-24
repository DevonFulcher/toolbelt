# Async UI

An action in the web page responds at once. Nothing makes the user wait on a
request to know whether something is happening.

- **Immediate feedback.** The button that was pressed is disabled and says what
  it is doing ("Starting…", "Posting in Slack…", "Merging…").
- **Slow work runs on the server, in the background.** Git, GitHub, Jira, Slack,
  starting an agent and merging all take seconds. The endpoint validates what is
  cheap to validate (the thing exists, it is allowed), marks it in progress and
  answers `202`; the work happens after the answer.
- **The outcome arrives through the live stream.** The card reads in progress,
  then done, or carries a note saying why it failed (and the button to try
  again). The request's own answer is never where the result is.
- **State that must survive a reload is on the server**, so a page opened in the
  middle of an action shows it in progress.

## How each slow action does it

| Action | In progress | Failure |
| --- | --- | --- |
| Post or bump in Slack | `posting` on the PR's Slack info | `notice` on the PR's Slack info |
| Run an agent by hand | `starting` on the task's agent info | `start_error` on the same |
| Validate a task | `starting` on the task's agent info | `start_error` on the same |
| Approve a branch (opens its PR) | `opening_pr` on the task's validation info | `notice` on the same |
| Merge, and merge when CI passes | `merging` on the PR's merge-queue info | `notice` on the same |

Quick actions (queueing a merge, taking it off the queue, the Agents switches)
answer in a few milliseconds and need no background work, but still disable their
button while the request is out.
