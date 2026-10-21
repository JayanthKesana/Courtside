#!/usr/bin/env python3
"""Collect NBA box scores for the Courtside dashboard.

Runs in the same GitHub Action as fetch_news.py. Uses ESPN's public scoreboard and
game summary data (unofficial, no API key) to build, for this season and the two
before it:

  stats/<season>.json  - every game line per Sleeper player for that season
  stats.json           - season totals per player, per season (small, for lists)

Work is incremental: each run fetches only games it hasn't processed, newest season
first, and keeps going until a time budget runs out. Past seasons finish in a run
or two and then never need fetching again. Progress lives in data/stats_state.json.

Standard library only.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import ROOT, fetch, load_index, norm  # noqa: E402

STATS_FILE = ROOT / "stats.json"
SEASON_DIR = ROOT / "stats"
STATE_FILE = ROOT / "data" / "stats_state.json"
STATE_VERSION = 3                   # bump to force a full re-collection

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ET = ZoneInfo("America/New_York")

PAST_SEASONS = 2                    # seasons kept besides the current one
MAX_SECONDS_PER_RUN = 9 * 60        # keep working until this much time has passed
PAUSE = 0.2                         # seconds between ESPN requests, per worker, to be polite
WORKERS = 4                         # box scores fetched in parallel

# One game line, in this order, everywhere
LINE = ["min", "pts", "reb", "ast", "stl", "blk", "to", "fgm", "fga", "tpm", "tpa",
        "ftm", "fta", "oreb", "dreb", "pf", "pm"]
LOG_COLS = ["d", "opp", "ha", "res"] + LINE
TOTALS = ["gp"] + LINE[:-1] + ["dd", "td"]

LABEL_MAP = {"MIN": "min", "PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "BLK": "blk",
             "TO": "to", "OREB": "oreb", "DREB": "dreb", "PF": "pf", "+/-": "pm",
             "FG": ("fgm", "fga"), "3PT": ("tpm", "tpa"), "FT": ("ftm", "fta")}
KEY_MAP = {"minutes": "min", "points": "pts", "rebounds": "reb", "assists": "ast", "steals": "stl",
           "blocks": "blk", "turnovers": "to", "offensiveRebounds": "oreb", "defensiveRebounds": "dreb",
           "fouls": "pf", "plusMinus": "pm",
           "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
           "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("tpm", "tpa"),
           "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta")}


def get_json(url: str) -> dict:
    time.sleep(PAUSE)
    return json.loads(fetch(url))


def num(v) -> float:
    s = str(v).strip()
    if not s or s in ("--", "-"):
        return 0.0
    if ":" in s:
        m, sec = s.split(":", 1)
        return round(int(m) + int(sec) / 60, 1)
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return 0.0


def seasons_for(today: date) -> list[dict]:
    """Current season first, then past seasons, newest first."""
    y = today.year if today.month >= 9 else today.year - 1
    out = []
    for k in range(PAST_SEASONS + 1):
        sy = y - k
        out.append({"label": f"{sy}-{str(sy + 1)[2:]}", "start": date(sy, 10, 1),
                    # the regular season ends in mid-April; nothing after April counts
                    "end": today if k == 0 else date(sy + 1, 4, 30), "current": k == 0})
    return out


# ---------------------------------------------------------------- parsing
def parse_summary(doc: dict, match_name) -> list[tuple[str, dict]]:
    comp = (doc.get("header") or {}).get("competitions", [{}])[0]
    teams = {}
    for c in comp.get("competitors", []):
        t = c.get("team") or {}
        teams[t.get("abbreviation")] = {"ha": "H" if c.get("homeAway") == "home" else "A",
                                       "score": num(c.get("score"))}
    abbrs = list(teams)
    out = []
    for side in (doc.get("boxscore") or {}).get("players", []):
        abbr = (side.get("team") or {}).get("abbreviation")
        me = teams.get(abbr, {})
        opp_abbr = next((a for a in abbrs if a != abbr), "")
        opp = teams.get(opp_abbr, {})
        res = ""
        if me and opp:
            res = f"{'W' if me['score'] > opp['score'] else 'L'} {int(me['score'])}-{int(opp['score'])}"
        for block in side.get("statistics", []):
            labels = block.get("labels") or []
            keys = block.get("keys") or block.get("names") or []
            cols = [LABEL_MAP.get(labels[i] if i < len(labels) else "") or KEY_MAP.get(keys[i] if i < len(keys) else "")
                    for i in range(max(len(labels), len(keys)))]
            for a in block.get("athletes", []):
                stats = a.get("stats") or []
                if a.get("didNotPlay") or not stats:
                    continue
                pid = match_name((a.get("athlete") or {}).get("displayName", ""))
                if not pid:
                    continue
                line = {k: 0.0 for k in LINE}
                for col, raw in zip(cols, stats):
                    if not col:
                        continue
                    if isinstance(col, tuple):
                        made, _, att = str(raw).partition("-")
                        line[col[0]], line[col[1]] = num(made), num(att)
                    else:
                        line[col] = num(raw)
                if line["min"] == 0 and line["pts"] == 0 and line["fga"] == 0 and line["reb"] == 0:
                    continue
                line.update(opp=opp_abbr, ha=me.get("ha", ""), res=res)
                out.append((pid, line))
    return out


def season_totals(rows: list[list]) -> list:
    """Totals for one player's season from their game rows (each row in LOG_COLS order)."""
    t = {k: 0.0 for k in TOTALS}
    for r in rows:
        g = dict(zip(LINE, r[4:]))
        t["gp"] += 1
        for k in LINE[:-1]:
            t[k] += g[k]
        tens = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if g[k] >= 10)
        t["dd"] += tens >= 2
        t["td"] += tens >= 3
    return [round(t[k], 1) for k in TOTALS]


# ---------------------------------------------------------------- crawling
def crawl(st: dict, end: date, deadline: float, on_game) -> None:
    """Walk scoreboard days from st['cursor'] to end, fetching unprocessed finished games."""
    day = date.fromisoformat(st["cursor"])
    done = set(st["done"])
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        _crawl_days(st, day, done, end, deadline, on_game, pool)
    finally:
        pool.shutdown()


def _crawl_days(st, day, done, end, deadline, on_game, pool) -> None:
    # st["cursor"]/st["done"] are updated as each day finishes, so an error keeps all earlier days
    while day <= end and time.time() < deadline:
        board = get_json(f"{SITE}/scoreboard?dates={day:%Y%m%d}&limit=50")
        all_final = True
        todo = []
        for ev in board.get("events") or []:
            if (ev.get("season") or {}).get("type") != 2:      # regular season only
                continue
            status = (ev.get("status") or {}).get("type") or {}
            if not status.get("completed"):
                if status.get("state") == "post":               # postponed or canceled
                    continue
                all_final = False
                continue
            eid = str(ev.get("id"))
            if eid not in done:
                todo.append(eid)
        # fetch the day's box scores in parallel, then record them in order
        docs = list(pool.map(lambda e: get_json(f"{SITE}/summary?event={e}"), todo))
        for eid, doc in zip(todo, docs):
            on_game(day, doc)
            done.add(eid)
        st["done"] = sorted(done)
        if not all_final:
            break
        day += timedelta(days=1)
        st["cursor"] = day.isoformat()


def main() -> int:
    deadline = time.time() + MAX_SECONDS_PER_RUN
    names = load_index()["names"]
    missed: set[str] = set()

    def match_name(n: str):
        pid = names.get(norm(n))
        if not pid:
            missed.add(n)
        return pid

    today = datetime.now(ET).date()
    seasons = seasons_for(today)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if state.get("v") != STATE_VERSION:
        state = {"v": STATE_VERSION, "seasons": {}}
    SEASON_DIR.mkdir(exist_ok=True)

    summary = []
    for s in seasons:
        label = s["label"]
        fresh = label not in state["seasons"]          # new season, or a forced re-collection
        st = state["seasons"].setdefault(label, {"cursor": s["start"].isoformat(), "done": [], "complete": False})
        path = SEASON_DIR / f"{label}.json"
        logs = json.loads(path.read_text()).get("players", {}) if path.exists() and not fresh else {}
        if st["complete"] and not s["current"]:
            summary.append(f"{label}: complete")
            s["logs"] = logs
            continue
        before = len(st["done"])

        def on_game(day, doc, logs=logs):
            for pid, line in parse_summary(doc, match_name):
                rows = logs.setdefault(pid, [])
                if not any(r[0] == day.isoformat() for r in rows):
                    rows.append([day.isoformat(), line["opp"], line["ha"], line["res"]] + [line[k] for k in LINE])

        try:
            crawl(st, s["end"], deadline, on_game)
        except Exception as e:              # keep everything collected so far; finish next run
            print(f"{label}: stopped early ({type(e).__name__}: {e}); progress saved, continues next run")
        if s["current"]:
            # re-check yesterday every run in case late games finished after midnight
            st["cursor"] = min(date.fromisoformat(st["cursor"]), today - timedelta(days=1)).isoformat()
        elif date.fromisoformat(st["cursor"]) > s["end"]:
            st["complete"] = True
        for rows in logs.values():
            rows.sort(key=lambda r: r[0])
        s["logs"] = logs
        new = len(st["done"]) - before
        summary.append(f"{label}: +{new} games, {len(st['done'])} total"
                       + (", complete" if st["complete"] else "" if s["current"] else ", continues next run"))
        data = {"cols": LOG_COLS, "label": label, "players": logs}
        old = json.loads(path.read_text()) if path.exists() else None
        if old != data:
            path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))

    # complete past seasons don't need their game-id lists any more
    for s in seasons:
        st = state["seasons"][s["label"]]
        if st["complete"] and not s["current"]:
            st["done"] = []
    state["seasons"] = {s["label"]: state["seasons"][s["label"]] for s in seasons}

    out = {
        "cols": TOTALS,
        "source": "ESPN box scores",
        "seasons": [{"label": s["label"], "current": s["current"],
                     "complete": state["seasons"][s["label"]]["complete"],
                     "players": {pid: season_totals(rows) for pid, rows in s["logs"].items() if rows}}
                    for s in seasons],
    }
    old = json.loads(STATS_FILE.read_text()) if STATS_FILE.exists() else {}
    if {k: v for k, v in old.items() if k != "updated"} != out:
        out["updated"] = int(time.time() * 1000)
        STATS_FILE.write_text(json.dumps(out, separators=(",", ":"), sort_keys=True))
        print("stats.json updated")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    old_logs = ROOT / "logs.json"                  # replaced by stats/<season>.json
    if old_logs.exists():
        old_logs.unlink()

    for line in summary:
        print(line)
    if missed:
        print(f"{len(missed)} ESPN names had no Sleeper match (usually two-way or G League players), "
              f"e.g. {', '.join(sorted(missed)[:8])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                         # never let stats break the news job
        print(f"Stats collection failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
