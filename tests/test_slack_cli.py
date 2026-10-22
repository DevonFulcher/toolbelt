"""`toolbelt slack link`: the command that links a Slack message posted by hand
to its PRs. PRs are given with --pr, so Slack is never read; reading the message
is covered in test_slack_link."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from toolbelt.cli import app
from toolbelt.git.stack.store import StackStore, default_db_path

runner = CliRunner()

MESSAGE = "https://dbt-labs.slack.com/archives/C0A0TFP0XC6/p1792696217666559"
PR = "https://github.com/o/r/pull/7"
CONFIG = "slack:\n  channel: CREVIEW\n  reviewers:\n    - {name: will.james, id: UW}\n"


def posts():
    store = StackStore(default_db_path())
    try:
        return store.slack_posts()
    finally:
        store.close()


def configure(config_file: Path) -> None:
    config_file.parent.mkdir(parents=True)
    config_file.write_text(CONFIG)


def test_the_message_is_recorded_as_the_post_of_each_given_pr(
    _isolate_config_dir: Path,
):
    configure(_isolate_config_dir)

    result = runner.invoke(
        app,
        [
            "slack",
            "link",
            MESSAGE,
            "--pr",
            PR,
            "--pr",
            "https://github.com/o/r/pull/8",
            "--ask",
            "will.james",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Linked o/r#7 as a post (asked: will.james)" in result.output
    assert "Linked o/r#8 as a post (asked: will.james)" in result.output
    assert [(p.pr_number, p.channel, p.assignee_name) for p in posts()] == [
        (7, "C0A0TFP0XC6", "will.james"),
        (8, "C0A0TFP0XC6", "will.james"),
    ]


def test_the_prs_must_be_named(_isolate_config_dir: Path):
    configure(_isolate_config_dir)

    result = runner.invoke(app, ["slack", "link", MESSAGE])

    assert result.exit_code == 2 and "--pr" in result.output
    assert posts() == []


def test_it_says_when_slack_is_not_configured():
    result = runner.invoke(app, ["slack", "link", MESSAGE, "--pr", PR])

    assert result.exit_code == 1
    assert "no slack: section" in result.output
    assert posts() == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["https://example.com/x", "--pr", PR], "not a link to a Slack message"),
        (
            [MESSAGE, "--pr", "https://github.com/o/r/issues/7"],
            "not a link to a GitHub PR",
        ),
        ([MESSAGE, "--pr", PR, "--ask", "nobody"], "not one of the slack.reviewers"),
    ],
)
def test_it_refuses_what_it_cannot_understand_and_records_nothing(
    _isolate_config_dir: Path, args, message
):
    configure(_isolate_config_dir)

    result = runner.invoke(app, ["slack", "link", *args])

    assert result.exit_code == 1 and message in result.output
    assert posts() == []
