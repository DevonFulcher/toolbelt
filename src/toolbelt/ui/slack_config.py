"""``slack`` in toolbelt's config file: where PRs are posted for review.

``$XDG_CONFIG_HOME/toolbelt/config.yaml`` (see ``toolbelt.git.repo_pull.
default_config_path``)::

    slack:
      channel: C0A0TFP0XC6        # Slack channel ID posts go to; omit to turn Slack off
      bump_after_hours: 24        # how long a post waits for a look before a bump

Without ``channel`` toolbelt never reads or posts to Slack and a task's next
action ignores it. A direct message works as the channel (its ID, ``D...``),
which is how to try it out.
"""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

lazy import yaml

from toolbelt.ui.slack_posts import DEFAULT_BUMP_AFTER


@dataclass(frozen=True)
class SlackConfig:
    channel: str
    bump_after: timedelta = DEFAULT_BUMP_AFTER


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
    )
