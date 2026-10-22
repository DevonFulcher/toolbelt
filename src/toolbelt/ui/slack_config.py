"""``slack`` in toolbelt's config file: where PRs are posted for review.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (see ``toolbelt.git.repo_pull.
default_config_path``)::

    slack:
      channel: C0A0TFP0XC6        # Slack channel ID posts go to; omit to turn Slack off
      bump_after_hours: 24        # how long a post waits for a look before a bump
      reviewers:                  # who may be asked: Slack display name and user ID
        - {name: will.james, id: U08129C20GZ}
        - {name: patrick.yost, id: U06GXCV7REV}
      assign:                     # who to ask for a PR: the first rule that matches
        - {repo: dbt-labs/ai-workflows, to: patrick.yost}
        - {min_lines: 500, to: will.james}            # +added and -deleted lines
        - {max_lines: 150, rotate: [patrick.yost, will.james]}   # asked longest ago
      default: will.james         # when no rule matches
      user_id: U04460UT8GK        # whose posts an import reads (your Slack user ID)

Without ``channel`` toolbelt never reads or posts to Slack and a task's next
action ignores it. A direct message works as the channel (its ID, ``D...``),
which is how to try it out. A bump asks someone other than who was asked
before, from ``reviewers``.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

lazy import yaml

from toolbelt.ui.slack_compose import Person
from toolbelt.ui.slack_posts import DEFAULT_BUMP_AFTER


@dataclass(frozen=True)
class AssignRule:
    """Who to ask for a PR that matches: ``to`` a person, or ``rotate`` among
    several (whoever was asked longest ago). ``repo`` is ``owner/name`` or just
    the name; ``min_lines`` / ``max_lines`` bound the PR's added plus deleted
    lines (``max_lines`` is exclusive)."""

    to: str | None = None
    rotate: tuple[str, ...] = ()
    repo: str | None = None
    min_lines: int | None = None
    max_lines: int | None = None


@dataclass(frozen=True)
class SlackConfig:
    channel: str
    bump_after: timedelta = DEFAULT_BUMP_AFTER
    reviewers: tuple[Person, ...] = ()
    rules: tuple[AssignRule, ...] = field(default_factory=tuple)
    default: str | None = None
    user_id: str | None = None


def slack_config(config_file: Path) -> SlackConfig | None:
    """The ``slack`` section, or ``None`` when Slack is not configured."""
    if not config_file.exists():
        return None
    section = (yaml.safe_load(config_file.read_text()) or {}).get("slack") or {}
    channel = section.get("channel")
    if not channel:
        return None
    hours = section.get("bump_after_hours")
    return SlackConfig(
        channel=str(channel),
        bump_after=timedelta(hours=float(hours)) if hours else DEFAULT_BUMP_AFTER,
        reviewers=tuple(
            Person(name=str(r["name"]), id=str(r["id"]))
            for r in section.get("reviewers") or []
        ),
        rules=tuple(
            AssignRule(
                to=rule.get("to"),
                rotate=tuple(rule.get("rotate") or ()),
                repo=rule.get("repo"),
                min_lines=rule.get("min_lines"),
                max_lines=rule.get("max_lines"),
            )
            for rule in section.get("assign") or []
        ),
        default=section.get("default"),
        user_id=section.get("user_id"),
    )
