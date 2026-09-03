"""O(1) working-day arithmetic over the Indian calendar.

Built once as a prefix-sum index plus its inverse (the ordered list of working
days), so both `working_days_between` and `add_working_days` are array lookups
rather than day-by-day loops. Settlement lag is computed on every candidate
pair in the matcher, so this is on the hot path.
"""
from datetime import date, timedelta
from functools import lru_cache

import holidays as pyholidays

from src.config import load

__all__ = [
    "WorkingCalendar", "get_calendar", "working_days_between",
    "add_working_days", "is_working_day",
]


class WorkingCalendar:
    def __init__(self, start: date, end: date, extra_holidays=()):
        if start > end:
            raise ValueError("start after end")
        self.start, self.end = start, end
        # National Indian holidays. Bank/state closures differ; config's
        # extra_holidays is the knob for a specific merchant's bank calendar.
        years = range(start.year, end.year + 1)
        holidays = set(pyholidays.India(years=list(years)).keys())
        holidays.update(date.fromisoformat(str(h)) for h in extra_holidays)

        n = (end - start).days + 1
        self._working_dates: list[date] = []
        # _before[i] = number of working days strictly before start+i days
        self._before: list[int] = [0] * (n + 1)
        for i in range(n):
            d = start + timedelta(days=i)
            self._before[i] = len(self._working_dates)
            if d.weekday() < 5 and d not in holidays:
                self._working_dates.append(d)
        self._before[n] = len(self._working_dates)
        self._holidays = holidays

    # -- internals -------------------------------------------------------
    def _offset(self, d: date) -> int:
        if not (self.start <= d <= self.end):
            raise ValueError(f"{d} outside calendar range {self.start}..{self.end}")
        return (d - self.start).days

    def _ceil_index(self, d: date) -> int:
        """Position in _working_dates of the first working day >= d."""
        return self._before[self._offset(d)]

    # -- public API ------------------------------------------------------
    def is_working_day(self, d: date) -> bool:
        self._offset(d)  # range check
        return d.weekday() < 5 and d not in self._holidays

    def working_days_between(self, d1: date, d2: date) -> int:
        """Working days in the half-open interval [d1, d2). Negative if d2 < d1."""
        return self._before[self._offset(d2)] - self._before[self._offset(d1)]

    def add_working_days(self, d: date, n: int) -> date:
        """d plus n working days.

        A non-working d rolls forward to the next working day first, and the
        roll itself does not consume one of the n days -- so a Saturday order
        at T+1 settles Tuesday, not Monday. This keeps the observed lag
        uniform per instrument, which is what makes it learnable at all.
        """
        if n < 0:
            raise ValueError("n must be >= 0; use working_days_between to go back")
        idx = self._ceil_index(d) + n
        if idx >= len(self._working_dates):
            raise ValueError(f"{d} + {n} working days runs past {self.end}")
        return self._working_dates[idx]


@lru_cache(maxsize=1)
def get_calendar() -> WorkingCalendar:
    cfg = load()["calendar"]
    return WorkingCalendar(
        date.fromisoformat(cfg["start_date"]),
        date.fromisoformat(cfg["end_date"]),
        tuple(cfg.get("extra_holidays") or ()),
    )


def working_days_between(d1: date, d2: date) -> int:
    return get_calendar().working_days_between(d1, d2)


def add_working_days(d: date, n: int) -> date:
    return get_calendar().add_working_days(d, n)


def is_working_day(d: date) -> bool:
    return get_calendar().is_working_day(d)
