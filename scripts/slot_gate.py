"""Whether a scheduled backup run of the update workflow should stand down: prints "true" when main's published data is
already newer than the latest scheduled slot in Eastern time (handoffs 466-467). Standard library only."""
import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SLOTS = [(6, 10), (9, 10), (12, 40), (17, 10)]  # the Claude routines' dispatch times, ET (index.html RUNS)
EARLY = timedelta(minutes=10)  # a dispatched run can land a few minutes before its nominal time


def latest_slot(now):
    """The latest slot at or before now, as an aware datetime in ET."""
    et = now.astimezone(ET)
    for h, m in reversed(SLOTS):
        slot = et.replace(hour=h, minute=m, second=0, microsecond=0)
        if slot <= et:
            return slot
    h, m = SLOTS[-1]
    return (et - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)


def should_skip(updated_utc, now):
    """A run starting up to EARLY before a slot works for that slot (handoff 800: GitHub ran the 12:37 PM cron at 12:34
    on Sat Oct 24, it compared against the 9:10 slot and skipped, and the midday data waited until 1:12 PM)."""
    if not updated_utc:
        return False
    published = datetime.fromisoformat(updated_utc.replace("Z", "+00:00"))
    return published >= latest_slot(now + EARLY) - EARLY


if __name__ == "__main__":
    try:
        with open(sys.argv[1]) as f:
            updated = json.load(f).get("updated_utc")
    except (OSError, ValueError, IndexError):
        updated = None
    now = datetime.now(timezone.utc)
    skip = should_skip(updated, now)
    print(f"Published {updated}, latest slot {latest_slot(now).isoformat()}: {'skip' if skip else 'run'}", file=sys.stderr)
    print("true" if skip else "false")
