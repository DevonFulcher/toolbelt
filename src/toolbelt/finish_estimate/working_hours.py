"""Working time: the hours of the viewer's calendar that count when a stage is
measured (nights and weekends do not), so a review requested on Friday at 6pm
effectively starts on Monday."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

#: ``datetime.weekday()`` numbers.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: How far ``advance`` looks for working time before giving up (an empty
#: calendar would never end).
_MAX_DAYS = 3660


@dataclass(frozen=True)
class WorkingHours:
    """Which days and hours count. ``tz`` is the zone the hours are in; ``None``
    is the machine's local zone."""

    days: tuple[int, ...] = (0, 1, 2, 3, 4)
    start: time = time(9, 0)
    end: time = time(17, 0)
    tz: tzinfo | None = None

    def __post_init__(self) -> None:
        if not self.days or self.end <= self.start:
            raise ValueError("working hours need a day and an end after the start")

    @property
    def day_seconds(self) -> float:
        """The length of one working day."""
        return (
            datetime.combine(date.min, self.end)
            - datetime.combine(date.min, self.start)
        ).total_seconds()

    def _window(self, day: date) -> tuple[datetime, datetime]:
        def at(clock: time) -> datetime:
            naive = datetime.combine(day, clock)
            return (
                naive.replace(tzinfo=self.tz) if self.tz else naive.astimezone()
            ).astimezone(UTC)

        return at(self.start), at(self.end)

    def _local_day(self, when: datetime) -> date:
        return (when.astimezone(self.tz) if self.tz else when.astimezone()).date()

    def hour_of_week(self, when: datetime) -> int:
        """Hours since Monday 00:00 in the calendar's zone (0-167)."""
        local = when.astimezone(self.tz) if self.tz else when.astimezone()
        return local.weekday() * 24 + local.hour

    def seconds(self, begin: datetime, end: datetime) -> float:
        """Working seconds between two instants (0 when ``end`` is not after
        ``begin``)."""
        if end <= begin:
            return 0.0
        total = 0.0
        day = self._local_day(begin)
        last = self._local_day(end)
        while day <= last:
            if day.weekday() in self.days:
                lo, hi = self._window(day)
                total += max(0.0, (min(hi, end) - max(lo, begin)).total_seconds())
            day += timedelta(days=1)
        return total

    def advance(self, begin: datetime, seconds: float) -> datetime:
        """The instant ``seconds`` of working time after ``begin``. Zero
        seconds is ``begin`` itself; otherwise the result is inside working
        hours."""
        if seconds <= 0:
            return begin
        left = seconds
        day = self._local_day(begin)
        for _ in range(_MAX_DAYS):
            if day.weekday() in self.days:
                lo, hi = self._window(day)
                lo = max(lo, begin)
                if hi > lo:
                    room = (hi - lo).total_seconds()
                    if left <= room:
                        return lo + timedelta(seconds=left)
                    left -= room
            day += timedelta(days=1)
        raise ValueError("no working time within ten years")


def working_hours_from_config(section: dict | None) -> WorkingHours:
    """``estimate.working_hours`` of the config file::

    estimate:
      working_hours:
        days: [mon, tue, wed, thu, fri]    # the default
        start: "09:00"
        end: "17:00"
        timezone: America/Chicago          # default: this machine's zone
    """
    section = section or {}
    names = [str(day).lower()[:3] for day in section.get("days", WEEKDAYS[:5])]
    unknown = [name for name in names if name not in WEEKDAYS]
    if unknown:
        raise ValueError(f"estimate.working_hours.days: unknown day {unknown[0]!r}")
    return WorkingHours(
        days=tuple(sorted({WEEKDAYS.index(name) for name in names})),
        start=time.fromisoformat(str(section.get("start", "09:00"))),
        end=time.fromisoformat(str(section.get("end", "17:00"))),
        tz=ZoneInfo(section["timezone"]) if section.get("timezone") else None,
    )
