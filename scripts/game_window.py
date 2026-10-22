#!/usr/bin/env python3
"""Are NBA games on, or about to be? Used by the workflows to decide whether to keep looping.

GitHub's schedule is best-effort and often runs "every 5 minutes" jobs hours apart, so the
workflows don't rely on it for timing. Instead each run loops by itself while this says yes,
and the schedule only has to start a run now and then.

Exit code 0  = busy: a game is live, or one tips off within LOOKAHEAD_HOURS
Exit code 1  = quiet: nothing live and nothing starting soon
If ESPN can't be reached it answers busy, so a blip never ends a game-night loop early.

Standard library only.
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
LOOKAHEAD_HOURS = 8      # start looping this long before the first tip-off


def busy() -> bool:
    now = datetime.now(timezone.utc)
    et = now.astimezone(ZoneInfo("America/New_York"))
    for day in (et - timedelta(days=1), et):
        req = urllib.request.Request(ESPN.format(day.strftime("%Y%m%d")),
                                     headers={"User-Agent": "Mozilla/5.0 (Courtside game window)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            events = json.load(r).get("events") or []
        for ev in events:
            state = ((ev.get("status") or {}).get("type") or {}).get("state")
            if state == "in":
                return True
            if state == "pre":
                start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                if start - now <= timedelta(hours=LOOKAHEAD_HOURS):
                    return True
    return False


if __name__ == "__main__":
    try:
        result = busy()
    except Exception as e:
        print(f"Couldn't check the schedule ({e}); assuming games are on.", file=sys.stderr)
        result = True
    print("Games on or starting soon." if result else "No games on or starting soon.")
    sys.exit(0 if result else 1)
