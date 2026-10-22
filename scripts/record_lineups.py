#!/usr/bin/env python3
"""Record which players were in each Sleeper starting lineup while their NBA game was live.

Sleeper's public API only shows the lineup as it is right now, so it can't tell whether a
player was starting when he actually played. Sleeper's app can, and in lock-in leagues it
won't count a game a player played from the bench. This script keeps that history so
Courtside can show scores the same way.

Runs every few minutes from .github/workflows/lineups.yml. On each run:
  1. Read today's and yesterday's NBA games from ESPN's scoreboard.
  2. If a game is live, note for every rostered player in it whether he is in his team's
     starting lineup. Sleeper locks a player's slot while his game is on, so one look
     during the game is enough. The first look is kept.
  3. When a game we watched goes final, save every player's Sleeper points from it.

Output: lineups/<league_id>-w<week>.json
  games:       {espn_game_id: {start, teams}}
  started:     {roster_id: {player_id: {espn_game_id: true | false}}}
  score_after: {espn_game_id: {player_id: sleeper points when the game ended}}
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LEAGUE = os.environ["LEAGUE_ID"]
OUT = "lineups"
SLEEPER = "https://api.sleeper.app/v1"
ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"

# ESPN and Sleeper spell a few team codes differently; map both to one spelling.
ALIAS = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS",
         "PHO": "PHX", "BRK": "BKN", "CHO": "CHA", "NJ": "BKN"}


def team(code):
    code = (code or "").upper()
    return ALIAS.get(code, code)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Courtside lineup recorder)"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")


def todays_games():
    """NBA games for yesterday and today (US Eastern), so late games past midnight are included."""
    now = datetime.now(ZoneInfo("America/New_York"))
    games = {}
    for day in (now - timedelta(days=1), now):
        for ev in get(ESPN.format(day.strftime("%Y%m%d"))).get("events", []):
            comp = ev["competitions"][0]
            games[ev["id"]] = {
                "start": ev["date"],
                "state": ev["status"]["type"]["state"],          # pre, in or post
                "teams": sorted(team(c["team"]["abbreviation"]) for c in comp["competitors"]),
            }
    return games


def player_teams():
    """Sleeper player id -> NBA team. Sleeper asks that the full player list be pulled at most
    about once a day, so it's cached in lineups/_teams.json for 12 hours."""
    path = f"{OUT}/_teams.json"
    cached = load(path)
    if cached and time.time() - cached.get("at", 0) < 12 * 3600:
        return cached["teams"]
    players = get(f"{SLEEPER}/players/nba")
    teams = {pid: team(p.get("team")) for pid, p in players.items() if p.get("team")}
    save(path, {"at": int(time.time()), "teams": teams})
    return teams


def main():
    os.makedirs(OUT, exist_ok=True)
    games = todays_games()
    state = get(f"{SLEEPER}/state/nba")
    week = state.get("display_week") or state.get("week")   # same week the page shows
    path = f"{OUT}/{LEAGUE}-w{week}.json"
    data = load(path) or {"league_id": LEAGUE, "week": week, "games": {}, "started": {}, "score_after": {}}

    live = {gid: g for gid, g in games.items() if g["state"] == "in"}
    finished = [gid for gid, g in games.items()
                if g["state"] == "post" and gid in data["games"] and gid not in data["score_after"]]
    if not live and not finished:
        print(f"Week {week}: no live games and nothing to finish.")
        return

    teams = player_teams()
    matchups = get(f"{SLEEPER}/league/{LEAGUE}/matchups/{week}") or []
    changed = False

    for gid, g in live.items():
        if gid not in data["games"]:
            data["games"][gid] = {"start": g["start"], "teams": g["teams"]}
            changed = True
        for m in matchups:
            starters = set(m.get("starters") or [])
            roster = data["started"].setdefault(str(m["roster_id"]), {})
            for pid in m.get("players") or []:
                if teams.get(pid) in g["teams"]:
                    seen = roster.setdefault(pid, {})
                    if gid not in seen:
                        seen[gid] = pid in starters
                        changed = True

    for gid in finished:
        playing = data["games"][gid]["teams"]
        scores = {}
        for m in matchups:
            pts = m.get("players_points") or {}
            for pid in m.get("players") or []:
                if teams.get(pid) in playing and pid in pts:
                    scores[pid] = pts[pid]
        data["score_after"][gid] = scores
        changed = True

    if changed:
        data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save(path, data)
    print(f"Week {week}: {len(live)} live, {len(finished)} finished, {'saved' if changed else 'no change'}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                      # a blip at ESPN or Sleeper: try again next run
        print(f"Skipped this run: {e}", file=sys.stderr)
