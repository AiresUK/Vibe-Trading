"""News event filter — pauses trading around high-impact economic releases.

Fetches this week's calendar from ForexFactory (no API key required).
Falls back to hardcoded NFP check if the network call fails.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

_FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_cache: dict[str, Any] = {"ts": None, "events": []}


def _fetch_calendar() -> list[dict]:
    """Return this week's high-impact events, cached for 1 hour."""
    now = datetime.now(timezone.utc)
    if _cache["ts"] and (now - _cache["ts"]).total_seconds() < 3600:
        return _cache["events"]

    try:
        req = urllib.request.Request(
            _FF_CALENDAR_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; vibe-trading-bot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            events: list[dict] = json.loads(resp.read().decode())
        high = [e for e in events if str(e.get("impact", "")).lower() == "high"]
        _cache["events"] = high
        _cache["ts"] = now
        logger.debug("[news] Fetched %d high-impact events this week", len(high))
        return high
    except Exception as exc:
        logger.debug("[news] Calendar fetch failed: %s", exc)
        return list(_cache.get("events") or [])


def _parse_event_utc(event: dict) -> datetime | None:
    """Convert a ForexFactory event dict to a UTC datetime, or None if unparseable."""
    date_str = str(event.get("date") or "")
    time_str = str(event.get("time") or "").strip().lower()

    if not date_str or time_str in ("", "all day", "tentative"):
        return None

    try:
        event_date = datetime.fromisoformat(date_str).date()
    except ValueError:
        return None

    match = re.match(r"(\d{1,2}):(\d{2})(am|pm)", time_str)
    if not match:
        return None

    hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    # ForexFactory times are US Eastern: UTC-4 (summer) / UTC-5 (winter)
    utc_offset = 4 if event_date.month in range(4, 11) else 5
    naive = datetime(event_date.year, event_date.month, event_date.day, hour, minute)
    return naive.replace(tzinfo=timezone.utc) + timedelta(hours=utc_offset)


def _is_nfp_window(now_utc: datetime, blackout_minutes: int) -> bool:
    """Hardcoded fallback: NFP = first Friday of month at 13:30 UTC."""
    if now_utc.weekday() != 4 or now_utc.day > 7:
        return False
    nfp = now_utc.replace(hour=13, minute=30, second=0, microsecond=0)
    return abs((now_utc - nfp).total_seconds()) / 60 <= blackout_minutes


def is_news_blackout(now_utc: datetime, blackout_minutes: int = 30) -> tuple[bool, str]:
    """Return (True, reason) if now is within *blackout_minutes* of a high-impact event.

    Checks the live ForexFactory calendar first; falls back to hardcoded NFP.
    Returns (False, "") when it is safe to trade.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    try:
        for event in _fetch_calendar():
            event_utc = _parse_event_utc(event)
            if event_utc is None:
                continue
            diff_mins = abs((now_utc - event_utc).total_seconds()) / 60
            if diff_mins <= blackout_minutes:
                title = event.get("title", "High-impact event")
                country = event.get("country", "")
                label = f"{country} {title}".strip()
                logger.info(
                    "[news] BLACKOUT — %s at %s UTC (%.0f min away)",
                    label, event_utc.strftime("%H:%M"), diff_mins,
                )
                return True, f"{label} at {event_utc.strftime('%H:%M')} UTC"
    except Exception as exc:
        logger.debug("[news] Calendar check error: %s", exc)

    if _is_nfp_window(now_utc, blackout_minutes):
        return True, "Non-Farm Payrolls (NFP) — first Friday 13:30 UTC"

    return False, ""
