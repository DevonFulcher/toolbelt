"""Who is asked to review: the config's rules, rotation and bumps."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolbelt.git.stack.store import SlackPostRow
from toolbelt.ui.slack_assignee import choose_assignee
from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_config import AssignRule, SlackConfig, slack_config

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
PEOPLE = tuple(Person(name, f"U-{name}") for name in ("will", "patrick", "alan", "dan"))
CONFIG = SlackConfig(
    channel="C1",
    reviewers=PEOPLE,
    rules=(
        AssignRule(to="patrick", repo="dbt-labs/ai-workflows"),
        AssignRule(to="will", min_lines=500),
        AssignRule(rotate=("patrick", "alan"), max_lines=150),
    ),
    default="dan",
)


def asked(name: str, hours_ago: float) -> SlackPostRow:
    return SlackPostRow(
        channel="C1",
        ts=str(hours_ago),
        thread_ts="1",
        repo="o/r",
        pr_number=1,
        kind="post",
        posted_at=NOW - timedelta(hours=hours_ago),
        assignee_name=name,
    )


def choose(repo="dbt-labs/ai-codegen-api", lines=300, history=(), **kwargs):
    person = choose_assignee(
        CONFIG, repo=repo, lines=lines, history=list(history), **kwargs
    )
    return person.name if person else None


def test_the_first_matching_rule_wins():
    assert (
        choose(repo="dbt-labs/ai-workflows", lines=900) == "patrick"
    )  # repo before size
    assert choose(lines=900) == "will"


def test_a_repo_rule_may_name_just_the_repo():
    config = SlackConfig(
        "C1", reviewers=PEOPLE, rules=(AssignRule(to="alan", repo="ai-workflows"),)
    )

    assert (
        choose_assignee(config, repo="dbt-labs/ai-workflows", lines=1, history=[]).name
        == "alan"
    )  # type: ignore[union-attr]


def test_a_rotation_picks_who_was_asked_longest_ago():
    assert choose(lines=100, history=[asked("patrick", 5), asked("alan", 50)]) == "alan"
    assert (
        choose(lines=100, history=[asked("patrick", 50), asked("alan", 5)]) == "patrick"
    )


def test_someone_never_asked_comes_first_in_a_rotation():
    assert choose(lines=100, history=[asked("patrick", 5)]) == "alan"
    assert choose(lines=100) == "patrick"  # nobody asked yet: the first named


def test_with_no_matching_rule_the_default_is_asked():
    assert choose(lines=300) == "dan"


def test_with_no_default_either_whoever_was_asked_longest_ago_is_asked():
    config = SlackConfig("C1", reviewers=PEOPLE)

    got = choose_assignee(
        config, repo="o/r", lines=1, history=[asked("will", 1), asked("dan", 2)]
    )

    assert got is not None and got.name in ("patrick", "alan")


def test_a_bump_asks_a_different_reviewer_whatever_the_rules_say():
    got = choose(
        lines=900, bump=True, exclude=frozenset({"will"}), history=[asked("will", 30)]
    )

    assert got is not None and got != "will"


def test_a_bump_picks_the_reviewer_asked_longest_ago_other_than_the_excluded():
    history = [
        asked("patrick", 40),
        asked("alan", 10),
        asked("dan", 20),
        asked("will", 30),
    ]

    assert choose(bump=True, exclude=frozenset({"patrick"}), history=history) == "will"


def test_nobody_is_chosen_when_there_is_nobody_to_choose():
    assert choose_assignee(SlackConfig("C1"), repo="o/r", lines=1, history=[]) is None
    one = SlackConfig("C1", reviewers=(Person("will", "U1"),))
    assert (
        choose_assignee(
            one, repo="o/r", lines=1, history=[], bump=True, exclude=frozenset({"will"})
        )
        is None
    )


def test_a_name_in_a_rule_that_is_not_a_reviewer_is_never_chosen():
    config = SlackConfig("C1", reviewers=PEOPLE, rules=(AssignRule(to="ghost"),))

    assert choose_assignee(config, repo="o/r", lines=1, history=[]) is None


def test_the_config_file_is_read(tmp_path: Path):
    file = tmp_path / "config.yaml"
    file.write_text(
        """
slack:
  channel: D1
  bump_after_hours: 12
  reviewers:
    - {name: will, id: U1}
  assign:
    - {repo: o/r, to: will}
    - {max_lines: 150, rotate: [will]}
  default: will
"""
    )

    config = slack_config(file)

    assert config is not None
    assert (config.channel, config.bump_after) == ("D1", timedelta(hours=12))
    assert config.reviewers == (Person("will", "U1"),)
    assert config.rules == (
        AssignRule(to="will", repo="o/r"),
        AssignRule(rotate=("will",), max_lines=150),
    )
    assert config.default == "will"


def test_no_channel_means_slack_is_off(tmp_path: Path):
    file = tmp_path / "config.yaml"
    file.write_text("slack:\n  bump_after_hours: 12\n")

    assert slack_config(file) is None
    assert slack_config(tmp_path / "missing.yaml") is None
