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
