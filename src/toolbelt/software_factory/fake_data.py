"""Hand-written sample data the UI shows until the real pieces (MCP readers,
agent runner, memory store) exist."""

from datetime import datetime, timedelta

from toolbelt.software_factory.model import (
    AgentRun,
    Changeset,
    FileChange,
    HarnessChange,
    McpSource,
    RunEvent,
    WorkItem,
    diff_lines,
)


def _ago(now: datetime, **delta: float) -> str:
    return (now - timedelta(**delta)).isoformat(timespec="seconds")


def _ahead(now: datetime, **delta: float) -> str:
    return (now + timedelta(**delta)).isoformat(timespec="seconds")


def sources(now: datetime) -> list[McpSource]:
    return [
        McpSource(
            "github",
            "GitHub issues & PRs",
            "github-mcp",
            "every 15 min",
            _ago(now, minutes=6),
            _ahead(now, minutes=9),
            42,
            2,
        ),
        McpSource(
            "jira",
            "Jira board",
            "atlassian-mcp",
            "hourly",
            _ago(now, minutes=38),
            _ahead(now, minutes=22),
            17,
            1,
        ),
        McpSource(
            "sentry",
            "Sentry errors",
            "sentry-mcp",
            "every 30 min",
            _ago(now, minutes=21),
            _ahead(now, minutes=9),
            8,
            2,
        ),
        McpSource(
            "slack",
            "Slack #feed-ai-alerts",
            "slack-mcp",
            "every 10 min",
            _ago(now, minutes=3),
            _ahead(now, minutes=7),
            63,
            1,
            healthy=False,
        ),
    ]


def work_items(now: datetime) -> list[WorkItem]:
    return [
        WorkItem(
            "wi-101",
            "Retry Jira fetch on 429 with backoff",
            "TaskFeed gives up on the first rate-limit response, so cards show "
            "stale task titles until the next full refresh.",
            "sentry",
            "TOOLBELT-APP-3F2",
            "https://sentry.example/issues/3F2",
            "Fired 31 times in the last day, all from one call site with no retry.",
            "toolbelt",
            "S",
            _ago(now, minutes=21),
        ),
        WorkItem(
            "wi-102",
            "Show merge-queue position on stacked PR cards",
            "Issue asks for the queue position next to the merge button; the "
            "data is already in the snapshot but never rendered.",
            "github",
            "toolbelt#412",
            "https://github.com/example/toolbelt/issues/412",
            "Three thumbs-up and a comment from the last reviewer asking for it.",
            "toolbelt",
            "M",
            _ago(now, minutes=6),
        ),
        WorkItem(
            "wi-103",
            "Flaky test: test_status_refresher_backoff",
            "Fails roughly 1 in 12 CI runs when the fake clock and the event "
            "loop disagree about which tick comes first.",
            "slack",
            "#feed-ai-alerts 10:42",
            "https://slack.example/archives/p1042",
            "Posted four times this week by the CI bot; each rerun wastes ~6 min.",
            "toolbelt",
            "S",
            _ago(now, minutes=3),
        ),
        WorkItem(
            "wi-104",
            "Estimate: expose champion model in the task dialog",
            "The estimate backtest picks a champion model but the dialog only "
            "shows the number, not which model produced it.",
            "jira",
            "TB-218",
            "https://jira.example/browse/TB-218",
            "Ticket is unassigned, labelled 'good first agent task', 2 weeks old.",
            "toolbelt",
            "M",
            _ago(now, minutes=38),
        ),
        WorkItem(
            "wi-105",
            "Drop unused pandas import from aggregate_feed",
            "Import adds ~180 ms to startup and nothing in the module uses it.",
            "github",
            "toolbelt#409",
            "https://github.com/example/toolbelt/pull/409",
            "Startup-time test failed on the PR that introduced it.",
            "toolbelt",
            "S",
            _ago(now, hours=5),
        ),
        WorkItem(
            "wi-106",
            "Webhook forwarder drops events during reconnect",
            "The forwarder ledger marks an event delivered before the ack, so a "
            "reconnect in between loses it.",
            "sentry",
            "TOOLBELT-APP-3C9",
            "https://sentry.example/issues/3C9",
            "Matches a missing-PR-update report from Thursday.",
            "toolbelt",
            "L",
            _ago(now, hours=7),
            status="implementing",
            run_id="run-1",
        ),
        WorkItem(
            "wi-107",
            "Add --json flag to `tt task list`",
            "Scripts currently parse the table output.",
            "jira",
            "TB-221",
            "https://jira.example/browse/TB-221",
            "Requested in standup notes twice.",
            "toolbelt",
            "S",
            _ago(now, hours=9),
            status="changeset_ready",
            run_id="run-2",
            changeset_id="cs-1",
        ),
        WorkItem(
            "wi-108",
            "Fix typo'd env var name in zsh bootstrap",
            "`TT_UI_PROT` should be `TT_UI_PORT`.",
            "github",
            "toolbelt#401",
            "https://github.com/example/toolbelt/issues/401",
            "One-line fix, reported with a patch.",
            "toolbelt",
            "S",
            _ago(now, days=1),
            status="pr_opened",
            run_id="run-3",
            changeset_id="cs-2",
            pr_url="https://github.com/example/toolbelt/pull/415",
        ),
        WorkItem(
            "wi-109",
            "Rewrite the stack store on Postgres",
            "Large migration suggested by a stale design doc.",
            "jira",
            "TB-150",
            "https://jira.example/browse/TB-150",
            "Doc is eight months old and contradicts the current local-first design.",
            "toolbelt",
            "L",
            _ago(now, days=2),
            status="rejected",
        ),
    ]


def runs(now: datetime) -> list[AgentRun]:
    def event(minutes: float, kind: str, text: str, tokens: int) -> RunEvent:
        return RunEvent(_ago(now, minutes=minutes), kind, text, tokens)

    return [
        AgentRun(
            "run-1",
            "wi-106",
            "claude-opus-5-5",
            _ago(now, minutes=14),
            events=[
                event(
                    14,
                    "read",
                    "Read Sentry issue TOOLBELT-APP-3C9 and 4 linked events",
                    3100,
                ),
                event(13, "skill", "Loaded skill `python`", 900),
                event(12, "tool", "grep -rn 'mark_delivered' src/toolbelt/ui", 400),
                event(
                    10,
                    "message",
                    "The ledger records delivery before the ack arrives; "
                    "moving the write after the ack and adding a replay on reconnect.",
                    2200,
                ),
                event(
                    7, "edit", "Edit src/toolbelt/ui/forwarder_ledger.py (+18 −6)", 3800
                ),
                event(
                    3,
                    "test",
                    "pytest tests/test_forwarder_ledger.py: 1 failed, 11 passed",
                    1200,
                ),
            ],
        ),
        AgentRun(
            "run-2",
            "wi-107",
            "claude-sonnet-5-5",
            _ago(now, hours=2),
            "done",
            [
                event(120, "read", "Read Jira TB-221", 1500),
                event(118, "skill", "Loaded skill `python`, `testing`", 1800),
                event(110, "edit", "Edit src/toolbelt/task/cli.py (+22 −3)", 4200),
                event(104, "edit", "Add tests/test_task_list_json.py (+31)", 2900),
                event(100, "test", "pytest: 14 passed", 900),
            ],
        ),
        AgentRun(
            "run-3",
            "wi-108",
            "claude-haiku-5-5",
            _ago(now, days=1),
            "done",
            [
                event(1440, "read", "Read GitHub issue toolbelt#401", 800),
                event(1438, "edit", "Edit src/toolbelt/zsh.py (+1 −1)", 600),
                event(1436, "test", "pytest: 212 passed", 500),
            ],
        ),
    ]


def changesets() -> list[Changeset]:
    return [
        Changeset(
            "cs-1",
            "wi-107",
            "devon/task-list-json",
            "Adds `--json` to `tt task list`, printing one JSON object per task "
            "with the same fields as the table.",
            [
                FileChange(
                    "src/toolbelt/task/cli.py",
                    22,
                    3,
                    diff_lines(
                        [
                            "@@ -41,9 +41,28 @@ def task_list(",
                            " ) -> None:",
                            '     """List tasks"""',
                            "     tasks = load_tasks()",
                            "-    for task in tasks:",
                            '-        typer.echo(f"{task.key}\\t{task.status}\\t{task.title}")',
                            "+    if as_json:",
                            "+        typer.echo(json.dumps([asdict(task) for task in tasks]))",
                            "+        return",
                            "+    for task in tasks:",
                            '+        typer.echo(f"{task.key}\\t{task.status}\\t{task.title}")',
                        ]
                    ),
                ),
                FileChange(
                    "tests/test_task_list_json.py",
                    31,
                    0,
                    diff_lines(
                        [
                            "@@ -0,0 +1,12 @@",
                            "+def test_task_list_json_has_one_object_per_task(runner, tasks):",
                            '+    result = runner.invoke(app, ["task", "list", "--json"])',
                            '+    assert [row["key"] for row in json.loads(result.output)] == [',
                            "+        task.key for task in tasks",
                            "+    ]",
                        ]
                    ),
                ),
            ],
            "pytest: 14 passed (2 new); ruff and mypy clean",
        ),
        Changeset(
            "cs-2",
            "wi-108",
            "devon/fix-ui-port-typo",
            "Renames the typo'd variable.",
            [
                FileChange(
                    "src/toolbelt/zsh.py",
                    1,
                    1,
                    diff_lines(
                        [
                            "@@ -88,1 +88,1 @@",
                            '-    export TT_UI_PROT="$port"',
                            '+    export TT_UI_PORT="$port"',
                        ]
                    ),
                )
            ],
            "pytest: 212 passed",
            status="pr_opened",
        ),
    ]


def harness_changes(now: datetime) -> list[HarnessChange]:
    return [
        HarnessChange(
            "hc-1",
            "`python` skill: say where lazy imports go",
            ".claude/skills/python/SKILL.md",
            "Two of the last five runs added a top-level pydantic import and "
            "failed the startup-time test, costing a retry each.",
            ["run-2", "run-5"],
            diff_lines(
                [
                    "@@ -12,3 +12,6 @@",
                    " ## Imports",
                    " ",
                    "-Prefer top-level imports.",
                    "+Prefer top-level imports, except heavy libraries (pandas,",
                    "+matplotlib, boto3, anything pulling in pydantic): use",
                    "+`lazy import` at module level so `tt` start-up stays fast.",
                ]
            ),
            _ago(now, hours=3),
        ),
        HarnessChange(
            "hc-2",
            "Hook: run the startup-time test before proposing a changeset",
            ".claude/settings.json",
            "The test is the cheapest check that catches the most common "
            "changeset rejection, but agents only run it when they remember.",
            ["run-3", "run-5", "run-8"],
            diff_lines(
                [
                    "@@ -4,0 +4,8 @@",
                    '+  "hooks": {',
                    '+    "Stop": [{',
                    '+      "command": "uv run pytest tests/test_cli_startup.py -q"',
                    "+    }]",
                    "+  },",
                ]
            ),
            _ago(now, hours=6),
        ),
        HarnessChange(
            "hc-3",
            "Add `triage-sentry` skill",
            ".claude/skills/triage-sentry/SKILL.md",
            "Sentry-sourced items spend their first ~3k tokens rediscovering how "
            "to read an issue's events through the MCP server.",
            ["run-1"],
            diff_lines(
                [
                    "@@ -0,0 +1,5 @@",
                    "+---",
                    "+name: triage-sentry",
                    "+description: Read a Sentry issue and its events through the MCP server",
                    "+---",
                    "+1. Fetch the issue, then its latest event; read the stack frames first.",
                ]
            ),
            _ago(now, days=1),
            status="applied",
        ),
    ]
