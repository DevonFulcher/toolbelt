"""Time that falls on a weekday: how long a Slack post has waited."""

from datetime import UTC, datetime, timedelta

from toolbelt.ui.weekday_time import weekday_elapsed

# 2026-10-23 is a Friday.
FRIDAY = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, 0, tzinfo=UTC)


def elapsed(start: datetime, end: datetime) -> timedelta:
    return weekday_elapsed(start, end, UTC)


def test_within_a_weekday_it_is_the_plain_difference():
    assert elapsed(at(21, 9), at(21, 15)) == timedelta(hours=6)  # Wednesday


def test_across_weekdays_it_is_the_plain_difference():
    assert elapsed(at(21), at(23)) == timedelta(hours=48)  # Wed noon to Fri noon


def test_a_weekend_is_taken_out():
    # Friday 5pm to Monday 5pm: 7h of Friday, nothing on the weekend, 17h of Monday.
    assert elapsed(FRIDAY, at(26, 17)) == timedelta(hours=24)


def test_only_the_weekend_counts_for_nothing():
    assert elapsed(at(24, 9), at(25, 20)) == timedelta(0)  # Saturday to Sunday


def test_starting_or_ending_on_a_weekend():
    assert elapsed(at(24), at(26, 6)) == timedelta(
        hours=6
    )  # Saturday noon to Monday 6am
    assert elapsed(at(23, 20), at(25)) == timedelta(
        hours=4
    )  # Friday 8pm to Sunday noon


def test_several_weekends():
    # Friday 24th to Friday 6th: ten weekdays.
    assert elapsed(
        datetime(2026, 10, 23, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 0, 0, tzinfo=UTC),
    ) == timedelta(days=10)


def test_an_end_before_the_start_is_nothing():
    assert elapsed(at(22), at(21)) == timedelta(0)
    assert elapsed(at(22), at(22)) == timedelta(0)


def test_the_weekend_is_the_one_of_the_zone_asked_for():
    # Friday 11pm UTC is already Saturday in Tokyo (UTC+9): no weekday time.
    from datetime import timezone

    tokyo = timezone(timedelta(hours=9))
    assert weekday_elapsed(at(23, 23), at(24, 3), tokyo) == timedelta(0)
    assert weekday_elapsed(at(23, 23), at(24, 3), UTC) == timedelta(hours=1)
