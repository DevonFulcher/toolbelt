from pathlib import Path

import typer

from toolbelt.cursor.edit import edit_text
from toolbelt.fs import chdir
from toolbelt.git.exec import run
from toolbelt.git.workflow import git_save
from toolbelt.git.worktrees import append_worktree
from toolbelt.llm import generate_short_title_from_plan
from toolbelt.logger import logger

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
    Start a task by drafting a plan and creating a worktree.
    """
    plan_text = edit_text(initial_text=_default_new_plan_template())
    title = generate_short_title_from_plan(plan=plan_text)

    wt_path = append_worktree(name=title)

    plans_dir = wt_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plans_dir / f"{wt_path.name}.md"
    plan_file.write_text(plan_text, encoding="utf-8")

    with chdir(wt_path):
        git_save(
            message="Initial plan",
            no_verify=False,
            no_sync=False,
            amend=False,
            pathspec=[str(plan_file.relative_to(wt_path))],
            yes=True,
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
            yes=True,
        )

    logger.info(f"Worktree created at {wt_path}")
    logger.info(f"Plan saved to {plan_file}")
