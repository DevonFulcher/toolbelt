"""How the proposer is paced, with defaults.

Override any of it in ``proposals:`` of ``$XDG_CONFIG_HOME/toolbelt/config.yaml``
(default ``~/.config/toolbelt``)::

    proposals:
      cadence_hours: 6
      max_pending: 3

What a proposer run costs is limited by the same ``agents:`` limits as every
agent (see ``autonomous_agents.limits``).
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ProposalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: A proposer run starts at least this often while there is a free slot.
    cadence_hours: float = Field(default=4.0, gt=0)
    #: After the user decides a proposal the proposer waits this long, so that
    #: several decisions in a row are answered by one run that fills every free
    #: slot.
    settle_seconds: float = Field(default=120.0, ge=0)
    #: At most this many proposals wait for a decision. Nothing is proposed
    #: while there are this many: that bounds what proposing costs.
    max_pending: int = Field(default=5, gt=0)
    #: How many decided proposals (with the reasons given) a proposer run is
    #: shown, newest first.
    history_shown: int = Field(default=40, gt=0)
    #: The curator rewrites the guidelines once this many proposals have been
    #: decided since they were last written.
    curate_after_decisions: int = Field(default=20, gt=0)
    #: The Jira project a worker creates a ticket in for a proposal that did not
    #: come from Jira (``None``: such proposals cannot be worked on).
    jira_project: str | None = None


def load_settings(config_file: Path) -> ProposalSettings:
    """The settings in ``proposals:`` of ``config_file`` over the defaults."""
    if not config_file.exists():
        return ProposalSettings()
    config = yaml.safe_load(config_file.read_text()) or {}
    return ProposalSettings.model_validate(config.get("proposals") or {})
