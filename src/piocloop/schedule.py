"""Weekly job-admission windows using a supported subset of OSM opening_hours.

The established parser supplies rule/overnight semantics. We materialize its
weekly wall-clock membership once, then evaluate actual instants in the chosen
zone. In particular, we do not use the parser's next_change(): its handling of
ambiguous local times can skip the first occurrence during a DST rollback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opening_hours import OpeningHours

UTC = timezone.utc
_MINUTES_PER_WEEK = 7 * 24 * 60
_REFERENCE_MONDAY = datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC"))
_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_DAYS = rf"{_DAY}(?:\s*-\s*{_DAY})?(?:\s*,\s*{_DAY}(?:\s*-\s*{_DAY})?)*"
_TIME = r"\d{2}:\d{2}"
_RANGE = rf"{_TIME}\s*-\s*{_TIME}"
_RULE = rf"(?:{_DAYS}\s+)?{_RANGE}(?:\s*,\s*{_RANGE})*"
_WEEKLY = re.compile(rf"{_RULE}(?:\s*;\s*{_RULE})*")
_TIME_RANGES = re.compile(rf"({_TIME})\s*-\s*({_TIME})")


@dataclass(frozen=True)
class ScheduleDecision:
    allowed: bool
    reasons: tuple[str, ...]
    next_change: datetime | None
    # Next combined permission, not merely the end of one pause window.
    next_allowed: datetime | None


def _window(expression: str) -> bytes:
    expression = expression.strip()
    if len(expression) > 4096:
        raise ValueError("Window expressions must be at most 4096 characters")
    if expression != "24/7":
        if not _WEEKLY.fullmatch(expression):
            raise ValueError(
                f"Unsupported window {expression!r}: use weekly OSM clock ranges "
                "such as 'Mo-Fr 09:00-17:00', '22:00-06:00', or '24/7'. "
                "Holidays, solar times, modifiers and open-ended ranges are not supported."
            )
        for start, end in _TIME_RANGES.findall(expression):
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError(
                    f"Invalid start time {start!r}; use 00:00 through 23:59"
                )
            if not (0 <= eh <= 24 and 0 <= em < 60 and (eh < 24 or em == 0)):
                raise ValueError(
                    f"Invalid end time {end!r}; only 24:00 is valid after 23:59"
                )
            if start == end:
                raise ValueError(
                    "Equal start/end times are ambiguous; use '00:00-24:00' for a full day"
                )
    try:
        parsed = OpeningHours(expression, timezone=ZoneInfo("UTC"))
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"Invalid OSM window {expression!r}: {exc}") from exc
    # Evaluate the second week so overnight spillover from the prior Sunday
    # is included even for implementations with a finite interval origin.
    monday = _REFERENCE_MONDAY + timedelta(days=7)
    return bytes(
        parsed.is_open(monday + timedelta(minutes=minute))
        for minute in range(_MINUTES_PER_WEEK)
    )


def _union(expressions: Sequence[str], *, default: bool) -> bytes:
    if not expressions:
        return bytes([default]) * _MINUTES_PER_WEEK
    result = bytearray(_MINUTES_PER_WEEK)
    for expression in expressions:
        for minute, matches in enumerate(_window(expression)):
            result[minute] |= matches
    return bytes(result)


class Schedule:
    """Parsed, bounded weekly schedule; evaluate() always takes an aware instant.

    Repeated expressions are independent union members. Semicolon rules inside
    an expression retain the OSM parser's override semantics.
    """

    def __init__(
        self,
        run_windows: Sequence[str] = (),
        pause_windows: Sequence[str] = (),
        timezone_name: str = "UTC",
    ) -> None:
        try:
            self.timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Unknown schedule timezone {timezone_name!r}; use an IANA name such as 'Europe/Paris'"
            ) from exc
        self.run_windows = tuple(run_windows)
        self.pause_windows = tuple(pause_windows)
        self._run = _union(self.run_windows, default=True)
        self._pause = _union(self.pause_windows, default=False)
        self._allowed = bytes(r and not p for r, p in zip(self._run, self._pause))
        self._constant = len(set(self._allowed)) == 1
        self._cached_minute: datetime | None = None
        self._cached_change: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.run_windows or self.pause_windows)

    def _index(self, instant: datetime) -> int:
        local = instant.astimezone(self.timezone)
        return (local.weekday() * 24 + local.hour) * 60 + local.minute

    def evaluate(self, now: datetime) -> ScheduleDecision:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Schedule evaluation requires a timezone-aware timestamp")
        # UTC conversion is essential: arithmetic/comparison of datetimes with
        # the same ZoneInfo ignores fold and can misorder repeated wall times.
        utc_now = now.astimezone(UTC)
        index = self._index(utc_now)
        allowed = bool(self._allowed[index])
        reasons = []
        if not self._run[index]:
            reasons.append("outside run windows")
        if self._pause[index]:
            reasons.append("scheduled pause")
        change = self._next_change(utc_now, allowed)
        return ScheduleDecision(
            allowed, tuple(reasons), change, change if not allowed else None
        )

    def _next_change(self, now: datetime, allowed: bool) -> datetime | None:
        if self._constant:
            return None
        minute = now.replace(second=0, microsecond=0)
        if minute != self._cached_minute:
            self._cached_minute = minute
            self._cached_change = None
            candidate = minute
            # Only explicit minute-resolution weekly rules are accepted. Walk
            # real UTC minutes (including both folds and excluding imaginary
            # times). Sixteen days cover two weeks plus offset shifts: a
            # weekly opening may be entirely skipped during spring-forward.
            for _ in range(16 * 24 * 60):
                candidate += timedelta(minutes=1)
                if bool(self._allowed[self._index(candidate)]) != allowed:
                    self._cached_change = candidate.astimezone(self.timezone)
                    break
        return self._cached_change
