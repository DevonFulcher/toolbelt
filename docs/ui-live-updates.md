# How `tt ui` learns what changed

The page never polls toolbelt: the server pushes every change over
`/api/events` (server-sent events). What the server itself waits on is GitHub,
and everything it asks GitHub goes through one client.

## One client for every GitHub request

`github_requests.py` (`GithubRequests`) makes every request: the PR lookups,
the ETag gate, the untracked-PR search, the aggregate history and the admin
check. It authenticates with the token of the signed-in `gh` and shares one
connection pool.

It reads each budget (`graphql` and `core`) off the response headers. When one
is used up, every caller that needs it stops at once, without sending anything,
until GitHub's reset time (or, without one, 60 s doubling up to 10 min). REST
and GraphQL are separate budgets: while GraphQL is exhausted the ETag gate keeps
working.

`GET /api/github-requests` shows each budget as last reported and how many
requests each caller (`status`, `gate`, `untracked`, `aggregate`, `admin`) has
made, with the number of 304s, rate-limited calls and failures.

## What a card shows when GitHub does not answer

Each branch's last good lookup (status, PR facts, conflict commit and the
gate's ETags) is kept in the stack DB (`pr_lookups`, see `lookup_cache.py`) and
loaded when the server starts. A failed lookup never replaces it: the card
keeps showing the last good data, the PR box counts its real age (`updated 612s
ago`, amber) and adds the reason (`GitHub graphql rate limit exceeded`). A
branch that was never looked up says `not read yet (…)`.

After a restart the saved ETags let the gate ask GitHub "did it change?" for
free, so unchanged PRs cost no GraphQL. A saved lookup whose full refresh is
overdue gets a random point inside the interval, so the full lookups spread out
instead of all running at once.

## Webhooks (repos you administer)

`gh webhook forward` registers a temporary webhook on a repo, receives its
events over a websocket and replays them as POSTs to `/api/github-webhook`.
It needs admin rights on the repo and the GitHub CLI extension:

```bash
gh extension install cli/gh-webhook
```

While `tt ui` serves, `webhook_forwarders.py` keeps one forwarder running for
each tracked repo where `permissions.admin` is true (checked once per repo per
server start), in its own process group, and restarts it when it exits.
Stopping the server ends each with Ctrl-C and GitHub removes the webhook.
Without the extension, or on a repo without admin rights, nothing is forwarded
and the repo falls back to the two ways below.

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
under a PR, so the full GraphQL lookup still runs every 10 minutes per PR
(`full_interval`). A change the gate does see shows up within one poll (20 s).

## Polling

Everything else is polled by the server only while a browser is watching:
branches without a PR (60 s, nothing to gate on), untracked PRs (180 s), Jira
(60 s), spend transcripts (60 s) and the aggregate history (15 min). A PR whose
state is merged or closed is looked up every 300 s.
