"""When the merge queue does not start merges on its own: from 16:00 (15:00 on
Fridays) until 08:00 the next morning, and all day on Saturday and Sunday (until
Monday 08:00), local time. A queued PR stays queued through it and merges once
the window ends and it is ready; the Merge button is not affected."""

from datetime import datetime, time, timedelta

PAUSE_FROM = time(16, 0)
PAUSE_FROM_ON_FRIDAY = time(15, 0)
RESUME_AT = time(8, 0)

_MONDAY = 0
_FRIDAY = 4
_SATURDAY = 5


def paused_until(local_now: datetime) -> datetime | None:
    """The time the queue resumes, or ``None`` when it is not paused.
    ``local_now`` is the current time in the zone whose clock the hours mean."""
    weekday = local_now.weekday()
    resume_today = datetime.combine(local_now.date(), RESUME_AT, local_now.tzinfo)
    if weekday >= _SATURDAY:
        return resume_today + timedelta(days=7 - weekday)  # the next Monday
    if local_now.time() < RESUME_AT:
        return resume_today
    start = PAUSE_FROM_ON_FRIDAY if weekday == _FRIDAY else PAUSE_FROM
    if local_now.time() >= start:
        days = 3 if weekday == _FRIDAY else 1
        return resume_today + timedelta(days=days)
    return None
