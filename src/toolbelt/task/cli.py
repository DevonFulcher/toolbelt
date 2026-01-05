import asyncio
from pathlib import Path
from typing import assert_never

import typer

from toolbelt.cursor_edit import edit_text_with_cursor
from toolbelt.fs import chdir
from toolbelt.git.exec import run
from toolbelt.git.workflow import git_save
from toolbelt.git.worktrees import append_worktree
from toolbelt.llm import generate_short_title_from_plan
from toolbelt.linear import (
    LinearIssue,
    LinearTeam,
    LinearClient,
)
from toolbelt.logger import logger
from toolbelt.task.textual_picker import (
    confirm_and_edit_title,
    pick_backlog_issue,
    pick_team,
)

task_typer = typer.Typer(help="Task workflow commands")


def _default_new_plan_template() -> str:
    return (
        "# Task plan\n\n"
        "## Goal\n\n"
        "- \n\n"
        "## Scope\n\n"
        "- \n\n"
        "## Implementation plan\n\n"
        "- \n\n"
        "## Risks / Notes\n\n"
        "- \n"
    )


def _worktree_display_name(*, issue: LinearIssue) -> str:
    # The worktree code will normalize path/branch names; we keep this human-readable.
    return f"{issue.identifier} {issue.title}"


def _enhance_plan_with_cursor_agent(*, repo_root: Path, plan_file: Path) -> str:
    prompt_path = Path.home() / ".cursor" / "commands" / "plan.md"
    if not prompt_path.exists():
        logger.error(f"Missing prompt file: {prompt_path}")
        raise typer.Exit(1)

    prompt_text = prompt_path.read_text(encoding="utf-8")
    rel_plan_path = plan_file.relative_to(repo_root)

    full_prompt = (
        f"{prompt_text.rstrip()}\n\n"
        "Improve the plan in the repo file below.\n"
        f"- Plan file: {rel_plan_path}\n\n"
        "Read that file from disk and output the full improved plan only.\n"
    )

    proc = run(
        ["cursor-agent", "-p", full_prompt],
        cwd=repo_root,
        exit_on_error=True,
        capture_output=True,
    )
    enhanced = (proc.stdout or "").strip()
    if not enhanced:
        logger.error("cursor-agent produced no output.")
        raise typer.Exit(1)
    return enhanced


@task_typer.command(name="start")
def start() -> None:
    """
    Start a task from Linear.
    """
    asyncio.run(_start_async())


async def _start_async() -> None:
    async with LinearClient.from_env() as linear:
        issues = await linear.get_backlog_issues_assigned_to_viewer()
        selection = pick_backlog_issue(issues=issues)

        # Selection options:
        # - "issue": start from an existing Linear issue in your backlog
        # - "create_new": draft a plan first, then create a new Linear issue from it
        match selection.kind:
            case "issue":
                assert selection.identifier is not None
                issue = await linear.get_issue_by_identifier(
                    identifier=selection.identifier
                )
                initial_text = issue.description or _default_new_plan_template()
                plan_text = edit_text_with_cursor(initial_text=initial_text)

                started_state_id, _ = await asyncio.gather(
                    linear.get_team_started_state_id(team_id=issue.team_id),
                    linear.update_issue_description(
                        issue_id=issue.id, description=plan_text
                    ),
                )
                if issue.state_type != "started":
                    await linear.move_issue_to_started(
                        issue_id=issue.id,
                        started_state_id=started_state_id,
                    )

                # Keep local state consistent with Linear.
                issue = LinearIssue(
                    id=issue.id,
                    identifier=issue.identifier,
                    title=issue.title,
                    description=plan_text,
                    url=issue.url,
                    team_id=issue.team_id,
                    state_id=started_state_id,
                    state_type="started",
                )
            case "create_new":
                viewer_task = asyncio.create_task(linear.get_viewer())
                teams_task = asyncio.create_task(linear.get_viewer_teams())
                viewer, teams = await asyncio.gather(viewer_task, teams_task)

                if not teams:
                    logger.error("No Linear teams found for your user.")
                    raise typer.Exit(1)
                team: LinearTeam
                if len(teams) == 1:
                    team = teams[0]
                else:
                    team = pick_team(teams=teams)

                plan_text = edit_text_with_cursor(
                    initial_text=_default_new_plan_template()
                )
                title = generate_short_title_from_plan(plan=plan_text)
                title = confirm_and_edit_title(initial_title=title)
                started_state_id = await linear.get_team_started_state_id(
                    team_id=team.id
                )
                issue = await linear.create_issue(
                    team_id=team.id,
                    title=title,
                    description=plan_text,
                    assignee_id=viewer.id,
                    started_state_id=started_state_id,
                )
            case _:
                assert_never(selection.kind)

    worktree_name = _worktree_display_name(issue=issue)
    wt_path = append_worktree(name=worktree_name)

    plans_dir = wt_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / f"{wt_path.name}.md"
    plan_file.write_text(issue.description, encoding="utf-8")

    with chdir(wt_path):
        git_save(
            message="Initial plan",
            no_verify=False,
            no_sync=False,
            amend=False,
            pathspec=[str(plan_file.relative_to(wt_path))],
        )

        enhanced = _enhance_plan_with_cursor_agent(
            repo_root=wt_path, plan_file=plan_file
        )
        plan_file.write_text(enhanced, encoding="utf-8")
        git_save(
            message="Enhanced plan",
            no_verify=False,
            no_sync=False,
            amend=False,
            pathspec=[str(plan_file.relative_to(wt_path))],
        )

    logger.info(f"Worktree created at {wt_path}")
    logger.info(f"Plan saved to {plan_file}")
