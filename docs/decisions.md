# Decisions

An agent that needs the user to make a choice asks through toolbelt instead of
in prose, and the question is kept until it is answered, however long that takes
and whether or not the session that asked is still open.

## What a decision is

A decision is a set of *points* about one task. Each point has a context, any
number of options (each with a summary, and a context, benefits, costs and
risks shown under "More context") and optionally a recommendation. The user
answers every point with one of its options, their own words, or both. The
record is in the stack database (`decisions`), so every toolbelt process shares
it. Deleting one deletes the row.

## Where it shows

A task with an unanswered question reads **Deciding** (it needs you), ahead of
whoever holds the task; the action it replaced is in the tooltip. The button
opens the questions: choose or write an answer for each point, open "More
context" on an option, or **Delete** a question (after asking: deleting is for
good). Answers can be given after the asking session has closed.

## Interactive sessions

`toolbelt decisions mcp` is an MCP server (stdio). Register it once:

```sh
claude mcp add --scope user toolbelt-decisions -- toolbelt decisions mcp
```

* `propose_decisions` records the question, shows it as an MCP App in Claude
  (answer there) and waits for the answer, which is its result. The question is
  also in `tt ui`, so it can be answered there instead.
* `read_decisions` returns the questions and answers of a task (the one checked
  out in the session's worktree, or named by branch, ticket or task id), so a
  later session finds an answer given after the first closed. Reading an
  answer marks it delivered.
* `pending_decision` and `answer_decision` are for the App only.

A call that waits is not limited by toolbelt. Claude Code applies its own tool
limits (`MCP_TOOL_TIMEOUT`, a per-server `timeout`, and an idle limit that
progress notifications reset: the server sends one every 20 seconds).

## Autonomous agents

A run has the same two tools, built in. `propose_decisions` records the question
and tells the agent to stop; the run ends as `deciding` ("waiting for you") and
holds nothing: its claim is released and no limit is running. No new run starts
on the task meanwhile. When the question is answered the dispatcher **resumes
the run's session** with the answer as the next prompt, as a new run record that
points back at the earlier one (`resumed_from`). A resumed run is not another
try at the job: it does not count against the per-commit or per-day run limits,
its cost does count, and it may be resumed in a worktree its earlier run left
dirty. It is resumed only when that is safe (the PR is open, nobody else holds
the task, the worktree exists and is not mid-merge or mid-rebase, and the global
spend limits, sign-in and the screen allow it). A deleted question settles its
run as stopped.
