"""When the merge queue does not start merges on its own: from 16:00 (15:00 on
Fridays) until 08:00, local time. A queued PR stays queued through it and
merges once the window ends and it is ready; the Merge button is not affected."""

from datetime import datetime, time, timedelta

PAUSE_FROM = time(16, 0)
PAUSE_FROM_ON_FRIDAY = time(15, 0)
RESUME_AT = time(8, 0)

_FRIDAY = 4


def paused_until(local_now: datetime) -> datetime | None:
    """The time the queue resumes, or ``None`` when it is not paused.
    ``local_now`` is the current time in the zone whose clock the hours mean."""
    start = PAUSE_FROM_ON_FRIDAY if local_now.weekday() == _FRIDAY else PAUSE_FROM
    resume_today = datetime.combine(local_now.date(), RESUME_AT, local_now.tzinfo)
    if local_now.time() < RESUME_AT:
        return resume_today
    if local_now.time() >= start:
        return resume_today + timedelta(days=1)
    return None
