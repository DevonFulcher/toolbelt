"""``estimate`` in toolbelt's config file::

estimate:
  working_hours:
    days: [mon, tue, wed, thu, fri]    # the default
    start: "09:00"                     # quote times: YAML reads 09:00 as a number
    end: "17:00"
    timezone: America/Chicago          # default: this machine's zone
"""

from pathlib import Path

lazy import yaml

from toolbelt.finish_estimate.model import Prior
from toolbelt.finish_estimate.working_hours import (
    WorkingHours,
    working_hours_from_config,
)


def configured_working_hours(config_file: Path) -> WorkingHours:
    """``estimate.working_hours``, with Monday to Friday 9-17 local time for
    whatever is not set."""
    if not config_file.exists():
        return working_hours_from_config(None)
    estimate = (yaml.safe_load(config_file.read_text()) or {}).get("estimate") or {}
    return working_hours_from_config(estimate.get("working_hours"))


def configured_priors(config_file: Path) -> dict[str, Prior]:
    """``estimate.priors``: what to answer for a stage with no history of its
    own (working hours at p50 and p80), by stage kind. None by default."""
    if not config_file.exists():
        return {}
    estimate = (yaml.safe_load(config_file.read_text()) or {}).get("estimate") or {}
    return {
        stage: Prior(float(p["p50_hours"]) * 3600, float(p["p80_hours"]) * 3600)
        for stage, p in (estimate.get("priors") or {}).items()
    }
