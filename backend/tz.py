# backend/tz.py
"""Project-wide time standard: Asia/Tashkent.

All DB timestamps (online status, block/mute expiry, chat/game history) use
naive wall-clock time in Asia/Tashkent (UTC+5) so that every displayed time
matches Tashkent local time regardless of server timezone.
"""
from datetime import datetime, timedelta, timezone

TASHKENT = timezone(timedelta(hours=5), name="Asia/Tashkent")


def now_local() -> datetime:
    """Naive wall-clock datetime in Asia/Tashkent."""
    return datetime.now(TASHKENT).replace(tzinfo=None)
