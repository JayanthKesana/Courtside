#!/usr/bin/env python3
"""Collect NBA box scores for the Courtside dashboard, 2016-17 to today.

Runs in the same GitHub Action as fetch_news.py. Uses ESPN's public scoreboard and
game summary data (unofficial, no API key).

Every game is checked before it's saved. A game is skipped entirely if:
  - either team's player points don't add up to that team's final score
  - any player's offensive + defensive rebounds don't equal total rebounds
  - any player has more shots made than attempted
so every game shown passes the same bar, in every season.

Output (all under stats/):
  index.json             seasons available, games collected and skipped, last update
  totals/<season>.json   season totals per Sleeper player (for the Players list)
  players/<id>.json      every game line for one player, all seasons (for player cards)

Work is incremental: each run fetches only games it hasn't processed yet, newest
season first, until a time budget runs out. Past seasons never need fetching again
once complete. Progress lives in data/stats_state.json.

Standard library only.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import ROOT, fetch, load_index, norm  # noqa: E402

STATS_DIR = ROOT / "stats"
PLAYER_DIR = STATS_DIR / "players"
TOTALS_DIR = STATS_DIR / "totals"
INDEX_FILE = STATS_DIR / "index.json"
STATE_FILE = ROOT / "data" / "stats_state.json"
STATE_VERSION = 4                   # bump to force a full re-collection

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ET = ZoneInfo("America/New_York")

FIRST_SEASON = 2016                 # 2016-17: the oldest season collected
MAX_SECONDS_PER_RUN = 45 * 60       # a full re-collection fits in one run; normal runs take seconds
WORKERS = 4                         # box scores fetched in parallel
PAUSE = 0.2                         # seconds between requests, per worker

LINE = ["min", "pts", "reb", "ast", "stl", "blk", "to", "fgm", "fga", "tpm", "tpa",
        "ftm", "fta", "oreb", "dreb", "pf", "pm"]
LOG_COLS = ["d", "opp", "ha", "res"] + LINE
# Season totals. The bonus counts use Sleeper's scoring-setting names so the page can
# score a whole season exactly with any league's settings.
BONUS = ["dd", "td", "qd", "bonus_pt_40p", "bonus_pt_50p", "bonus_reb_20p", "bonus_ast_15p"]
TOTALS = ["gp"] + LINE[:-1] + BONUS

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
    """Current season first, then past seasons back to FIRST_SEASON, newest first."""
    y = today.year if today.month >= 9 else today.year - 1
    return [{"label": f"{sy}-{str(sy + 1)[2:]}", "start": date(sy, 10, 1),
             # the regular season ends in mid-April; nothing after April counts
             "end": today if sy == y else date(sy + 1, 4, 30), "current": sy == y}
            for sy in range(y, FIRST_SEASON - 1, -1)]


# ---------------------------------------------------------------- parsing + checks
def parse_summary(doc: dict, match_name) -> tuple[list[tuple[str, dict]] | None, str]:
    """Return ([(sleeper_id, line)], "") for a game that passes every check,
    or (None, reason) for a game that should be skipped."""
    comp = (doc.get("header") or {}).get("competitions", [{}])[0]
    teams = {}
    for c in comp.get("competitors", []):
        t = c.get("team") or {}
        teams[t.get("abbreviation")] = {"ha": "H" if c.get("homeAway") == "home" else "A",
                                       "score": num(c.get("score"))}
    sides = (doc.get("boxscore") or {}).get("players") or []
    if len(teams) != 2 or len(sides) != 2:
        return None, "incomplete box score"
    abbrs = list(teams)
    out = []
    for side in sides:
        abbr = (side.get("team") or {}).get("abbreviation")
        me = teams.get(abbr)
        opp_abbr = next((a for a in abbrs if a != abbr), "")
        opp = teams.get(opp_abbr)
        if not me or not opp:
            return None, "team mismatch"
        res = f"{'W' if me['score'] > opp['score'] else 'L'} {int(me['score'])}-{int(opp['score'])}"
        team_pts = 0.0
        for block in side.get("statistics", []):
            labels = block.get("labels") or []
            keys = block.get("keys") or block.get("names") or []
            cols = [LABEL_MAP.get(labels[i] if i < len(labels) else "") or KEY_MAP.get(keys[i] if i < len(keys) else "")
                    for i in range(max(len(labels), len(keys)))]
            for a in block.get("athletes", []):
                stats = a.get("stats") or []
                if a.get("didNotPlay") or not stats:
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
                team_pts += line["pts"]
                if line["oreb"] + line["dreb"] != line["reb"]:
                    return None, "rebounds don't add up"
                if line["fgm"] > line["fga"] or line["tpm"] > line["tpa"] or line["ftm"] > line["fta"]:
                    return None, "made more shots than attempted"
                if line["min"] == 0 and line["pts"] == 0 and line["fga"] == 0 and line["reb"] == 0:
                    continue                       # listed but never got in
                pid = match_name((a.get("athlete") or {}).get("displayName", ""))
                if pid:
                    line.update(opp=opp_abbr, ha=me["ha"], res=res)
                    out.append((pid, line))
        if abs(team_pts - me["score"]) > 0.5:
            return None, "player points don't add up to the team score"
    return out, ""


def season_totals(rows: list[list]) -> list:
    t = {k: 0.0 for k in TOTALS}
    for r in rows:
        g = dict(zip(LINE, r[4:]))
        t["gp"] += 1
        for k in LINE[:-1]:
            t[k] += g[k]
        tens = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if g[k] >= 10)
        t["dd"] += tens >= 2
        t["td"] += tens >= 3
        t["qd"] += tens >= 4
        t["bonus_pt_40p"] += g["pts"] >= 40
        t["bonus_pt_50p"] += g["pts"] >= 50
        t["bonus_reb_20p"] += g["reb"] >= 20
        t["bonus_ast_15p"] += g["ast"] >= 15
    return [round(t[k], 1) for k in TOTALS]


# ---------------------------------------------------------------- crawling
def crawl(st: dict, end: date, deadline: float, on_game) -> None:
    """Walk scoreboard days from st['cursor'] to end. st is updated after each finished
    day, so an error part-way through keeps everything before it."""
    day = date.fromisoformat(st["cursor"])
    done = set(st["done"])
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while day <= end and time.time() < deadline:
            board = get_json(f"{SITE}/scoreboard?dates={day:%Y%m%d}&limit=50")
            all_final, todo = True, []
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
            docs = list(pool.map(lambda e: get_json(f"{SITE}/summary?event={e}"), todo))
            for eid, doc in zip(todo, docs):
                on_game(day, doc)
                done.add(eid)
            st["done"] = sorted(done)
            if not all_final:
                break
            day += timedelta(days=1)
            st["cursor"] = day.isoformat()


# ---------------------------------------------------------------- main
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
        # Fresh start: remove every older output so nothing unchecked survives.
        state = {"v": STATE_VERSION, "seasons": {}}
        if STATS_DIR.exists():
            shutil.rmtree(STATS_DIR)
        for old in (ROOT / "stats.json", ROOT / "logs.json"):
            if old.exists():
                old.unlink()
    PLAYER_DIR.mkdir(parents=True, exist_ok=True)
    TOTALS_DIR.mkdir(parents=True, exist_ok=True)

    # every player's games, all seasons: {pid: {season: [rows]}}
    store: dict[str, dict[str, list]] = {}
    for f in PLAYER_DIR.glob("*.json"):
        store[f.stem] = json.loads(f.read_text()).get("seasons", {})
    dirty: set[str] = set()

    report = []
    for s in seasons:
        label = s["label"]
        st = state["seasons"].setdefault(label, {"cursor": s["start"].isoformat(), "done": [],
                                                 "complete": False, "games": 0, "skipped": {}})
        if st["complete"] and not s["current"]:
            report.append(f"{label}: complete ({st['games']} games)")
            continue
        before = len(st["done"])

        def on_game(day, doc, label=label, st=st):
            rows, why = parse_summary(doc, match_name)
            if rows is None:
                st["skipped"][why] = st["skipped"].get(why, 0) + 1
                return
            st["games"] += 1
            for pid, line in rows:
                games = store.setdefault(pid, {}).setdefault(label, [])
                if not any(r[0] == day.isoformat() for r in games):
                    games.append([day.isoformat(), line["opp"], line["ha"], line["res"]] + [line[k] for k in LINE])
                    games.sort(key=lambda r: r[0])
                    dirty.add(pid)

        try:
            crawl(st, s["end"], deadline, on_game)
        except Exception as e:              # keep everything collected so far; finish next run
            print(f"{label}: stopped early ({type(e).__name__}: {e}); progress saved, continues next run")
        if s["current"]:
            # re-check yesterday every run in case late games finished after midnight
            st["cursor"] = min(date.fromisoformat(st["cursor"]), today - timedelta(days=1)).isoformat()
        elif date.fromisoformat(st["cursor"]) > s["end"]:
            st["complete"] = True
            st["done"] = []                 # not needed once a past season is finished
        skipped = sum(st["skipped"].values())
        report.append(f"{label}: +{len(st['done']) - before if not st['complete'] else 'all'} games this run, "
                      f"{st['games']} saved, {skipped} skipped"
                      + (", complete" if st["complete"] else "" if s["current"] else ", continues next run"))

    # ---- write player files that changed
    for pid in dirty:
        data = {"cols": LOG_COLS, "seasons": {k: store[pid][k] for k in sorted(store[pid], reverse=True)}}
        (PLAYER_DIR / f"{pid}.json").write_text(json.dumps(data, separators=(",", ":")))

    # ---- season totals (only rewritten when they change)
    for s in seasons:
        label = s["label"]
        tot = {pid: season_totals(by[label]) for pid, by in store.items() if by.get(label)}
        path = TOTALS_DIR / f"{label}.json"
        data = {"cols": TOTALS, "players": tot}
        if not path.exists() or json.loads(path.read_text()) != data:
            path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))

    # ---- index
    index = {"cols": TOTALS, "log_cols": LOG_COLS, "source": "ESPN box scores",
             "seasons": [{"label": s["label"], "current": s["current"],
                          "complete": state["seasons"][s["label"]]["complete"],
                          "games": state["seasons"][s["label"]]["games"],
                          "skipped": sum(state["seasons"][s["label"]]["skipped"].values())} for s in seasons]}
    old = json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else {}
    if {k: v for k, v in old.items() if k != "updated"} != index:
        index["updated"] = int(time.time() * 1000)
        INDEX_FILE.write_text(json.dumps(index, separators=(",", ":")))

    state["seasons"] = {s["label"]: state["seasons"][s["label"]] for s in seasons}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))

    for line in report:
        print(line)
    for s in seasons:
        sk = state["seasons"][s["label"]]["skipped"]
        if sk:
            print(f"  {s['label']} skipped games: " + ", ".join(f"{n} ({why})" for why, n in sk.items()))
    if missed:
        print(f"{len(missed)} ESPN names had no Sleeper match (retired, two-way or G League players), "
              f"e.g. {', '.join(sorted(missed)[:8])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:                         # never let stats break the news job
        print(f"Stats collection failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(0)
