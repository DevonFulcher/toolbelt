# How `tt ui` learns what changed

The page never polls toolbelt: the server pushes every change over
`/api/events` (server-sent events). What the server itself waits on is
GitHub, and it learns about GitHub in three ways, cheapest first.

## Webhooks (repos you administer)

`gh webhook forward` registers a temporary webhook on a repo, receives its
events over a websocket and replays them as POSTs to `/api/github-webhook`.
It needs admin rights on the repo and the GitHub CLI extension:

```bash
gh extension install cli/gh-webhook
```

While `tt ui` serves, `webhook_forwarders.py` keeps one forwarder running for
each tracked repo where `permissions.admin` is true (checked once per repo per
server start), and restarts it when it exits. Stopping the server ends each
forwarder with Ctrl-C so it removes its webhook. Without the extension, or on a
repo without admin rights, nothing is forwarded and the repo falls back to the
two ways below.

A webhook is only a hint (`github_webhooks.py`): it names the branches and PR
numbers it touched, and the status refresher looks those up again at once,
skipping the gate. The payload is never used as data. A repo whose forwarder
has stayed up for a few seconds is polled only every 300 s, as a safety net.

## ETag gate (every repo)

GraphQL has no conditional requests, but REST does, and a `304 Not Modified`
does not count against the rate limit. For each PR the gate (`pr_gate.py`)
remembers the ETag of the pull request, the check runs of its head commit and
the statuses of its head commit. A poll that finds all three unchanged reuses
the previous lookup; the card's `updated … ago` still restarts, because GitHub
was just asked.

The gate does not see a review thread being resolved or the base branch moving
under a PR, so the full GraphQL lookup still runs every 120 s per PR
(`full_interval`). A change the gate does see shows up within one poll (20 s).

## Polling

Everything else is polled by the server only while a browser is watching:
untracked PRs (180 s), Jira (60 s), spend transcripts (60 s) and the aggregate
history (15 min). A PR whose state is merged or closed is looked up every
300 s.
