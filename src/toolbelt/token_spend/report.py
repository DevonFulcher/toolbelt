"""``tt spend``: the spend summary as text."""

import typer

from toolbelt.git.stack.store import StackStore, default_db_path
from toolbelt.token_spend.ingest import ingest_transcripts
from toolbelt.token_spend.summary import NamedTaskUsage, SpendSummary, summarize
from toolbelt.token_spend.transcripts import projects_dir

_TOP_TASKS = 15


def _tokens(count: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if count >= size:
            return f"{count / size:.1f}{unit}"
    return f"{count:.0f}"


def _task_label(task: NamedTaskUsage) -> str:
    if task.task_id is None:
        return "(no claim covered it)"
    if task.branch is None:
        return f"(task {task.task_id[:8]}, no longer tracked)"
    return f"{task.repo}:{task.branch}"


def format_summary(summary: SpendSummary) -> str:
    lines = [
        (
            "Spend in Claude Code on this machine, at list prices "
            f"(as of {summary.as_of:%Y-%m-%d %H:%M} UTC)"
        ),
        "",
        f"{'':20s}{'calls':>8s}{'tokens':>10s}{'cost':>12s}",
    ]
    for window in summary.windows:
        lines.append(
            f"{window.label:20s}{window.calls:>8d}{_tokens(window.tokens.total):>10s}"
            f"{'$' + format(window.cost_usd, ',.2f'):>12s}"
        )
    lines += ["", f"By model, last {summary.breakdown_days} days"]
    for usage in summary.by_model:
        lines.append(
            f"  {usage.model:28s}{usage.calls:>8d}{_tokens(usage.tokens.total):>10s}"
            f"{'$' + format(usage.cost_usd, ',.2f'):>12s}"
        )
    lines += [
        "",
        f"By task (through claims), last {summary.breakdown_days} days",
    ]
    for task in summary.by_task[:_TOP_TASKS]:
        lines.append(
            f"  {_task_label(task):44.44s}{task.calls:>8.0f}"
            f"{_tokens(task.tokens):>10s}{'$' + format(task.cost_usd, ',.2f'):>12s}"
        )
    if len(summary.by_task) > _TOP_TASKS:
        lines.append(f"  ... and {len(summary.by_task) - _TOP_TASKS} more")
    if summary.unpriced_models:
        listed = ", ".join(
            f"{model} ({calls} calls)"
            for model, calls in summary.unpriced_models.items()
        )
        lines += [
            "",
            (
                f"No price for: {listed}. Their tokens are counted but not "
                "their cost, so the figures above are low. Upgrade the price "
                "table: `uv lock --upgrade-package genai-prices`, then reinstall."
            ),
        ]
    return "\n".join(lines)


def run_spend_report(*, days: int) -> None:
    store = StackStore(default_db_path())
    try:
        result = ingest_transcripts(store, projects_dir())
        if result.new_calls:
            typer.echo(
                f"Read {result.new_calls} new model calls from "
                f"{result.files_read} transcripts.\n",
                err=True,
            )
        typer.echo(format_summary(summarize(store, breakdown_days=days)))
    finally:
        store.close()
