"""What has to exist before a worker run can start on an approved proposal: a
clean checkout to branch from, a Jira ticket and a branch with its worktree.

Each step is a plain function the proposal worker (``ui.proposal_worker``)
takes as an argument, so its tests never touch git, Jira or a model.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

lazy from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
lazy from claude_agent_sdk import query as sdk_query

from toolbelt import logged_process
from toolbelt.autonomous_agents.limits import AgentLimits
from toolbelt.autonomous_agents.runner import Query
from toolbelt.proposals.model import Proposal

DEFAULT_BRANCHES = ("main", "master")
_TASK_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_WORKTREE = re.compile(r"Created worktree at (\S.*)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TICKET_SCHEMA = {
    "type": "object",
    "properties": {"task_key": {"type": "string"}},
    "required": ["task_key"],
}


class SetupFailed(Exception):
    """A step could not be done; the message says why, for the proposal's note."""


def is_task_key(text: str) -> bool:
    return bool(_TASK_KEY.match(text))


def branch_name_for(proposal: Proposal) -> str:
    """A bare name for the branch (``toolbelt git append`` adds the prefix)."""
    words = re.sub(r"[^a-z0-9]+", "-", proposal.content.title.lower()).strip("-")
    return words[:40].strip("-") or "proposal"


def agent_link_for(proposal: Proposal) -> str:
    return f"claude://toolbelt/proposals/{proposal.id}"


def brief_of(proposal: Proposal) -> str:
    """The approved proposal as the text a worker is given."""
    c = proposal.content
    evidence = "\n".join(
        f'- "{e.quote}" ({", ".join(x for x in (e.author, e.date, e.url) if x)})'
        for e in c.evidence
    )
    lines = [
        f"Title: {c.title}",
        f"Source: {proposal.source} {proposal.source_ref} {proposal.source_url}",
        f"Ticket: {proposal.task_key or 'none yet'}",
        f"Summary: {c.summary}",
        f"Problem: {c.problem}",
        f"Why now: {c.why_now}",
        f"Evidence:\n{evidence or '- none'}",
        f"Approach: {c.approach}",
        "Done when:\n" + "\n".join(f"- {x}" for x in c.acceptance_criteria),
        "Risks and unknowns:\n" + "\n".join(f"- {x}" for x in c.risks),
        "Related:\n" + "\n".join(f"- {x}" for x in c.related),
    ]
    return "\n".join(lines)


def _git(path: Path, *args: str) -> str:
    done = logged_process.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def checkout_problem(path: Path) -> str | None:
    """Why a worker must not branch from the repo's checkout now (``None``:
    it may). ``toolbelt git append`` forks from the current branch and commits
    any uncommitted work onto it first, so the checkout has to be a clean
    default branch: nobody's work is committed or stacked by accident."""
    try:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if branch not in DEFAULT_BRANCHES:
            return f"the checkout is on {branch}, not {' or '.join(DEFAULT_BRANCHES)}"
        if _git(path, "status", "--porcelain"):
            return "the checkout has uncommitted changes"
    except subprocess.CalledProcessError as error:
        return f"git failed: {error}"
    return None


def append_branch(path: Path, proposal: Proposal, task_key: str) -> tuple[Path, str]:
    """Makes the proposal's stacked branch and worktree with ``toolbelt git
    append``; returns the worktree and the branch name."""
    toolbelt = Path(sys.executable).with_name("toolbelt")
    done = logged_process.run(
        [
            str(toolbelt),
            "git",
            "append",
            branch_name_for(proposal),
            "--task",
            task_key,
            "--agent-link",
            agent_link_for(proposal),
        ],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise SetupFailed(
            f"toolbelt git append failed: {_ANSI.sub('', done.stderr or done.stdout).strip()[-400:]}"
        )
    found = _WORKTREE.search(_ANSI.sub("", done.stdout + done.stderr))
    if found is None:
        raise SetupFailed("toolbelt git append did not say where the worktree is")
    worktree = Path(found.group(1).strip())
    return worktree, _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")


def head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD")


async def create_ticket(
    proposal: Proposal,
    *,
    project: str,
    limits: AgentLimits,
    workdir: Path,
    query: Query | None = None,
) -> str:
    """Has an agent with the user's own Jira MCP server create exactly one
    ticket for the proposal; returns its key."""
    workdir.mkdir(parents=True, exist_ok=True)
    options: Any = ClaudeAgentOptions(
        model=limits.model,
        cwd=str(workdir),
        max_turns=12,
        max_budget_usd=0.25,
        disallowed_tools=["Bash", "Edit", "Write", "NotebookEdit", "Task", "WebFetch"],
        permission_mode="bypassPermissions",
        setting_sources=["user"],
        output_format={"type": "json_schema", "schema": _TICKET_SCHEMA},
    )
    prompt = (
        f"Create exactly one Jira issue in project {project} for the work below, "
        "using the Jira tools you have, then answer with its key. Create "
        "nothing else and change nothing else.\n\n" + brief_of(proposal)
    )
    result: Any = None
    async for message in (query or sdk_query)(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    output = result.structured_output if result is not None else None
    key = str(output.get("task_key", "")).strip() if isinstance(output, dict) else ""
    if not is_task_key(key):
        raise SetupFailed(
            f"the agent did not create a Jira ticket (it answered {key!r})"
        )
    return key
