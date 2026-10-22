# Posting PRs in Slack

With a `slack:` section in `~/.config/toolbelt/config.yaml`, `tt ui` follows a
PR through the review channel: **Post in Slack** when it is ready and nobody has
posted it, **Bump in Slack** when a day has passed and no person has looked.
This is what the `pr-publish` skill does by hand.

```yaml
slack:
  channel: C0A0TFP0XC6        # where posts go; omit to turn Slack off
  bump_after_hours: 24
  reviewers:                  # who may be asked: Slack display name and user ID
    - {name: will.james, id: U08129C20GZ}
    - {name: patrick.yost, id: U06GXCV7REV}
  assign:                     # who to ask: the first rule that matches
    - {repo: dbt-labs/ai-workflows, to: patrick.yost}
    - {min_lines: 500, to: will.james}
    - {max_lines: 150, rotate: [patrick.yost, will.james]}
  default: will.james
  user_id: U04460UT8GK        # your Slack user ID: whose posts an import reads
  workspace: fivetran         # the Slack subdomain (fivetran.slack.com): links to threads
```

A direct message works as the channel (give your own user ID), which is how to
try it without posting anywhere else. The channel comes only from this file: the
page never names one.

## What the card shows

- **Next**: `Post in Slack` / `Bump in Slack`, as a button.
- **Slack status**: `Not posted in Slack`, `Posted in Slack`, `Bumped in Slack`.
- **Post age**: time since the last post or bump.

`Post in Slack` is the next action for an open, not-approved PR that is not
posted. It comes after the rules that block a PR (conflicts, changes requested,
open threads, failing CI), so an agent still sees a failing CI first, and before
`Waiting on CI`, so a PR is posted while its checks run. `Bump in Slack` is
when the last post or bump is over `bump_after_hours` old and no person has
reviewed it, started a thread or approved it since the first post (bots do not
count). Drafts, merged and approved PRs are never posted.

## Posting

The button opens the message for you to read and edit; nothing is sent until you
press **Post**. Posting takes tens of seconds, so it runs in the background: the
dialog closes at once, the button reads "Posting in Slack…" and **Slack status**
says so, and when it is done the status reads posted and links to the thread. If
it failed, nothing was recorded and the card says why (the button is there to try
again); if Slack's copy lacked a link or mention, the card says which. A post covers the branch and the PRs of its stack that are
ready, bottom first and indented under the PR each is stacked on, with the
person the rules chose (change it in the dialog). A bump is a threaded reply
under the original post, for the PRs of that thread that are due, asking someone
other than who was asked before. The bump dialog links to the original post
(needs `workspace`).

The text is written the way the skill writes it, starting `🤖 AI-generated on
behalf of Devon:`. `@name` becomes a Slack mention when it is sent.

## How it is sent, and what it costs

Slack reaches Claude Code through the claude.ai connector, which only a model
session can call. Posting runs `claude -p --model haiku` allowed two tools,
`slack_send_message` and `slack_read_thread`; it is told to post the exact text
and then reads the message back. Toolbelt checks that Slack's copy has every PR
link and mention of what was sent and warns if not (Slack rewrites the
formatting, so the text itself is not compared). A send costs about $0.08.
Slack's connector adds a "Sent using Claude" line.

If the session fails or times out, nothing is recorded and the card says to check
Slack, since the message may have been sent.

## What it costs

Every session that posts, bumps or imports is recorded in `slack_sessions` with
the exact cost it reported. The spend view shows them in their own Slack section
(by window, and by what the session was for) and leaves them out of the other
figures, as it does the autonomous agents' runs; the agents' daily and monthly
caps do not count them. A session that fails before it answers has no recorded
cost and stays in the other figures like any other session.

## What is recorded

Toolbelt records each post (`slack_posts` in the stack DB): PR, message and
thread timestamps, kind, time and who was asked. That is the source of truth: the
status polling never reads Slack, so it costs nothing. A bump falling due is
announced by a watcher that checks once a minute. Posts made by hand outside
toolbelt are not seen until they are imported.

## Importing posts made by hand

`POST /api/slack/import` with `{"days": 5, "channel": null}` records your own
posts and thread replies of the last `days` days as posted, so PRs you posted
before toolbelt did read as posted and their post age starts from your post.
`channel` is the channel to read (`null`: the configured one). A session allowed
only the Slack read tools reads your messages (`user_id`); a top-level message
is a post of every PR it links and a reply is a bump of every PR it links, asking
the person mentioned on the PR's line (else the first one mentioned). It costs
about $0.06 and never posts. Running it again records each post once.

A bump replies in the thread of the last post, which must be in the configured
channel: a post imported from another channel is never replied to.
