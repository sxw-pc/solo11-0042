"""Injectable UTC clock and time helpers used across credrot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, Union


class ClockError(ValueError):
    """Raised when a timestamp cannot be interpreted as an aware UTC value."""


def parse_time(value: Union[str, datetime]) -> datetime:
    """Parse an ISO-8601 timestamp and return an aware UTC datetime."""
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ClockError("timestamp is empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ClockError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    else:
        raise ClockError(f"timestamp must be str or datetime, got {type(value)!r}")

    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    """Normalize a datetime to a second-precision UTC ``Z`` string."""
    normalized = parse_time(value)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current aware UTC datetime."""


class SystemClock:
    """Production clock; the only source of wall-clock time in the app."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class FixedClock:
    """Deterministic clock used by tests and by ``--now`` command options."""

    at: Union[str, datetime]

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", parse_time(self.at))

    def now(self) -> datetime:
        return self.at


def days_until(deadline: datetime, now: datetime) -> int:
    """Whole days until ``deadline``; negative when the deadline has passed."""
    delta = parse_time(deadline) - parse_time(now)
    return int(delta.total_seconds() // 86400)


def is_due(deadline: datetime, now: datetime) -> bool:
    return parse_time(now) >= parse_time(deadline)
