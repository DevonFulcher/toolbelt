# Proposals

Agents look through the systems connected to this machine (the MCP servers
installed for Claude Code: issue trackers, source hosting, error monitoring,
chat), propose software work, and you approve or reject each proposal in the
**Proposals** view. An approved proposal is picked up by a worker agent that
implements it on a new branch and stops there, with no PR: the branch then goes
through [validate and personal review](personal-review.md) like any other.

```
sources --(proposer)--> pending --approve--> worker --> local branch --> Validate --> personal review --> PR
                           \--reject (reason, tags)--> kept; the proposer reads these
```

## The cap

At most `proposals.max_pending` (5) proposals wait for you at once, and the
proposer is **not started** while there are that many: the cap is what bounds
what proposing costs. When you decide one, a slot is free and the proposer looks
again after `settle_seconds` (2 minutes: several decisions in a row are answered
by one run that fills every free slot), without waiting for the cadence. A look
that came back with fewer proposals than it was asked for found nothing more
worth proposing, so the next one waits for `cadence_hours` (4). A failed look
also waits for the cadence.

## Never deleted

A proposal is never deleted. Your decision (approve or reject, with an optional
reason and tags) is recorded on its row and decided proposals are only hidden in
the view (**Show decided**). A source item (`source`, `source_ref`) is proposed
once, whatever was decided, so a rejected item does not come back. Each proposal
also keeps the run, model, prompt version and systems read that produced it.
`tests/test_proposals.py` fails if any code deletes from the table.

## The proposer

`autonomous_agents/proposer.py` runs one Claude Agent SDK `query` per look with
the **user's own settings** (`setting_sources=["user"]`: the MCP servers, skills
and plugins installed here, whichever they are). It may not run commands or edit
files; that it only *reads* the systems is enforced by its instructions alone
(`proposals/instructions.py`), and everything it reads is told to be data, not
instructions. It answers with structured output: for each proposal the source,
the problem, why now, verbatim evidence with links, the approach, acceptance
criteria, risks, size, confidence, related work and what a worker will do on
approval. Its prompt lists every proposal already made (with the reasons given
for rejections) and the current guidelines. The server trims the answer to the
free slots, so the cap holds whatever the model returns.

Its runs are `agent_runs` with trigger `propose` and no branch, so they appear
on the Agents view and their spend counts toward the shared limits
(`agents.propose_*` set the per-run limits). It starts only when the agents are
switched on and may start runs, and not while the screen is asleep or locked, like
every agent.

## Learning from rejections

Rejection reasons and tags are the signal. Every proposer run is shown the
recent decisions with their reasons. Once `curate_after_decisions` (20)
proposals have been decided since the guidelines were last written, a curator
run (trigger `curate`, no tools) reads all decisions and writes the
**guidelines**: a short list of what you want proposed and what you do not,
which every later proposer run is given. Each version is kept; you can read and
rewrite them under *What the proposer is told* in the view (a save is a new
version, authored by you).

## The worker

`ui/proposal_worker.py` takes the oldest approved proposal, one at a time:

1. finds the tracked repo named in the proposal;
2. waits while the repo's checkout is not a clean default branch (`toolbelt git
   append` branches from the current branch and commits what is uncommitted);
3. gets a ticket: a Jira proposal's own key, otherwise an agent with the Jira MCP
   server creates one in `proposals.jira_project` (without that setting such a
   proposal is left with a note);
4. makes the stacked branch and worktree with `toolbelt git append`;
5. starts an **implement** run there.

An implement run loads the user's and the project's settings (so your
`CLAUDE.md` files and skills apply as in an interactive session) and appends
only a short block: you are unattended; wherever the guidance says to ask or
stop, use `propose_decisions` (the run ends as *waiting for you* and resumes with
your answer, also when the branch has no PR); stop at a committed branch and do
not open a PR. Limits: `agents.implement_*`.

Anything the worker cannot take up (no matching repo, no ticket possible, branch
creation failed) is shown on the proposal and not retried.

## Configuration

```yaml
proposals:
  cadence_hours: 4
  settle_seconds: 120
  max_pending: 5
  history_shown: 40
  curate_after_decisions: 20
  jira_project: TB      # only for proposals that did not come from Jira
agents:
  propose_model: claude-sonnet-5-5
  propose_run_usd: 1.0
  implement_run_usd: 3.0
```
