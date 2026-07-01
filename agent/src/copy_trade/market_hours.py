"""Forex market hours guard.

Forex is open 24 hours Monday–Friday. It closes Friday ~22:00 UTC and
reopens Sunday ~22:00 UTC. Attempting to trade during the weekend causes
connection errors and wasted API calls.
"""

from __future__ import annotations

from datetime import datetime, timezone


def is_market_open(now_utc: datetime | None = None) -> tuple[bool, str]:
    """Return (True, '') if forex is open, or (False, reason) if closed.

    Weekday reference: 0=Monday … 4=Friday, 5=Saturday, 6=Sunday.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    wd = now_utc.weekday()
    h = now_utc.hour

    if wd == 4 and h >= 22:
        return False, "Forex closed — Friday 22:00 UTC close (reopens Sunday 22:00 UTC)"
    if wd == 5:
        return False, "Forex closed — Saturday (reopens Sunday 22:00 UTC)"
    if wd == 6 and h < 22:
        hours_left = 22 - h
        return False, f"Forex closed — Sunday open in ~{hours_left}h (22:00 UTC)"

    return True, ""


def is_sunday_preopen(now_utc: datetime | None = None) -> bool:
    """Return True if it is Sunday before 22:00 UTC (market not yet open)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.weekday() == 6 and now_utc.hour < 22
