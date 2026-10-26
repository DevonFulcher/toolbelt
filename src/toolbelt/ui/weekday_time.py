"""Time that counts for a wait: hours that fall on a weekday.

A post that waited all weekend has not waited a day: the people it asks were
not at work. ``weekday_elapsed`` is the time between two instants with every
Saturday and Sunday (in the machine's time zone, as ``tt`` agents' working days
are) taken out."""

from datetime import datetime, timedelta, tzinfo

_DAY = timedelta(days=1)


def weekday_elapsed(
    start: datetime, end: datetime, zone: tzinfo | None = None
) -> timedelta:
    """The time from ``start`` to ``end`` that falls on Monday to Friday in
    ``zone`` (default: this machine's). Zero when ``end`` is not after
    ``start``. Wall-clock time, so a daylight-saving change moves it by an
    hour at most."""
    first = start.astimezone(zone).replace(tzinfo=None)
    last = end.astimezone(zone).replace(tzinfo=None)
    if last <= first:
        return timedelta(0)
    total = timedelta(0)
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < last:
        if day.weekday() < 5:
            total += min(last, day + _DAY) - max(first, day)
        day += _DAY
    return total
