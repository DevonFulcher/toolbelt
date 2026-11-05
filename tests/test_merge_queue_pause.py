"""The hours the merge queue does not start merges on its own."""

from datetime import UTC, datetime

import pytest

from toolbelt.ui.merge_queue_pause import paused_until


def at(day: int, hour: int, minute: int = 0) -> datetime:
    # November 2026: the 2nd and 9th are Mondays, the 6th a Friday, the 7th a Saturday.
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "resumes"),
    [
        (at(2, 16), at(3, 8)),  # Monday 16:00: paused until Tuesday 08:00
        (at(2, 23, 30), at(3, 8)),
        (at(3, 0), at(3, 8)),  # after midnight: the same morning
        (at(3, 7, 59), at(3, 8)),
        (at(5, 16), at(6, 8)),  # Thursday evening: Friday morning
        (at(6, 15), at(9, 8)),  # Friday 15:00, an hour earlier: until Monday
        (at(6, 20), at(9, 8)),
        (at(7, 1), at(9, 8)),  # Saturday, all day
        (at(7, 12), at(9, 8)),
        (at(8, 9), at(9, 8)),  # Sunday, all day
        (at(8, 23), at(9, 8)),
        (at(9, 7, 59), at(9, 8)),  # Monday morning, before it resumes
    ],
)
def test_it_is_paused_from_the_evening_until_eight(now, resumes):
    assert paused_until(now) == resumes


@pytest.mark.parametrize(
    "now",
    [
        at(2, 8),
        at(2, 12),
        at(2, 15, 59),
        at(6, 8),
        at(6, 14, 59),
        at(9, 8),
        at(9, 15, 59),
    ],
)
def test_it_is_not_paused_in_the_day(now):
    assert paused_until(now) is None
