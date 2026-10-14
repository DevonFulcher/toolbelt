"""The limits autonomous agents run under, with conservative defaults.

Override any of them in ``agents:`` of ``$XDG_CONFIG_HOME/toolbelt/config.yaml``
(default ``~/.config/toolbelt``)::

    agents:
      run_usd: 0.50
      global_usd_per_day: 5

Whether agents run at all, and whether they start runs or only decide, are not
configuration: they are switches in the UI, off until turned on (see
``AGENTS_ENABLED_SETTING`` and ``AGENTS_LAUNCH_SETTING``).
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

AGENTS_ENABLED_SETTING = "agents_enabled"
#: Whether decided tasks actually get a run. Off, agents are in a dry run:
#: tasks are decided and nothing is started.
AGENTS_LAUNCH_SETTING = "agents_launch"

#: The next actions an agent is started for: what a run is for.
TRIGGERS = ("resolve_conflicts", "resolve_comments", "fix_ci")


class AgentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = "claude-haiku-5-5"
    #: One run: dollars (the SDK stops it), turns, and wall-clock seconds.
    run_usd: float = Field(default=0.25, gt=0)
    run_turns: int = Field(default=40, gt=0)
    run_timeout_seconds: int = Field(default=15 * 60, gt=0)
    #: One task: runs on the same head commit, runs in 24 hours, and dollars
    #: in 24 hours.
    task_runs_per_head: int = Field(default=2, gt=0)
    task_runs_per_day: int = Field(default=3, gt=0)
    task_usd_per_day: float = Field(default=0.50, gt=0)
    #: All agents: dollars in the last 24 hours and in the calendar month
    #: (UTC), separate from the account's own limit.
    global_usd_per_day: float = Field(default=3.0, gt=0)
    global_usd_per_month: float = Field(default=30.0, gt=0)
    #: Wait this long after a task's run ends before starting another.
    backoff_seconds: int = Field(default=10 * 60, ge=0)
    max_concurrent_runs: int = Field(default=1, gt=0)
    #: A task is picked up only if its branch was committed to within this
    #: many working days (weekends do not count).
    active_working_days: int = Field(default=3, gt=0)
    #: A PR that keeps getting re-blocked is left to you: this many reviews
    #: by bots, or this many commits whose CI failed.
    thrash_bot_reviews: int = Field(default=3, gt=0)
    thrash_failed_commits: int = Field(default=3, gt=0)


def load_limits(config_file: Path) -> AgentLimits:
    """The limits in ``agents:`` of ``config_file`` over the defaults."""
    if not config_file.exists():
        return AgentLimits()
    config = yaml.safe_load(config_file.read_text()) or {}
    return AgentLimits.model_validate(config.get("agents") or {})
