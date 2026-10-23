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
  workspace: fivetran         # the Slack subdomain (fivetran.slack.com): links to threads
```

A direct message works as the channel (give your own user ID), which is how to
try it without posting anywhere else. The channel comes only from this file: the
page never names one.

## What the card shows

- **Next**: `Post in Slack` / `Bump in Slack`, as a button.
- **Slack status**: `Not posted in Slack`, `Posted in Slack`, `Bumped in Slack`.
- **Post age**: time since the last post or bump.
- **Assigned Review**: who the latest post or bump asked to review the PR (it
  replaces the card's "Reviewers" slot, which showed GitHub's requested reviewers).

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
person the rules chose (change it in the dialog).

When the stack has several PRs the dialog lists each with a checkbox, who is
asked about it and a line on where it stands (`+added/-deleted`, CI, review, open
comments, conflicts). Unchecking a PR leaves it out of the message; the PRs of
the stack that an agent holds (they read "With an agent" or "Autonomous agent"
because the post is what they would do next) are listed unchecked, with that
note, and can be checked to include them. Checking or unchecking writes the
message again on the server, so edits to the text are replaced. A bump is a threaded reply
under the original post, for the PRs of that thread that are due, asking someone
other than who was asked before. The bump dialog links to the original post
(needs `workspace`).

**Reassign.** A posted PR that is still open has a **Reassign** button beside its
Assigned Review, available any time rather than only once a bump is due. It
opens a threaded reply under the original post that moves that one PR to
someone who has not been asked about it yet (change them in the dialog): "@new,
would you mind if I reassign [repo#number](link) to you? Thanks!". Sending it
records a `reassign` row, so Assigned Review follows to the new person, the
card reads "Reassigned in Slack", and the 24-hour window for a bump starts
again from the reassignment.

A bump names each PR as `[repo#number](link)` followed by its `+added/-deleted`
lines, as the post does, and asks "would you mind taking a look?".

The text is written the way the skill writes it, with no line saying how it was
written (the messages are canned and you confirm each one). `@name` becomes a
Slack mention when it is sent.

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

Every session that posts or bumps is recorded in `slack_sessions` with
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
toolbelt are not seen until they are linked with `toolbelt slack link` (below).

## Linking one message by hand

`toolbelt slack link <message link> --pr <PR link> [--pr ...] [--ask <name>]`
records a Slack message you posted yourself as the post of the PRs (tasks) it is
about, with the same effect as posting from the card: the PR reads posted, its
post age counts from the message, and the person asked is its Assigned Review.
The link is the message's "Copy link"
(`https://<workspace>.slack.com/archives/<channel>/p<digits>`); a link to a reply
in a thread (it ends `?thread_ts=...`) is recorded as a bump.

```
tt slack link https://dbt-labs.slack.com/archives/C0A0TFP0XC6/p1792696217666559 \
  --pr https://github.com/o/r/pull/7 --pr https://github.com/o/r/pull/8 --ask will.james
```

Toolbelt reads nothing from Slack and calls no model: whoever runs the command
(you, or an agent that read the message) names the PRs and who was asked, and
toolbelt validates the links and writes the posts. `--ask` is who was asked: a
`slack.reviewers` name is recorded with their Slack ID, any other name (a one-off
recipient) as given; without it nobody is recorded as asked. The message may be in any channel
or a direct message. The posts go into the stack DB, and a running `tt ui`
notices and updates its cards without a restart. A bump never replies in a channel
other than the configured one, so a message elsewhere only marks the PR as posted.

