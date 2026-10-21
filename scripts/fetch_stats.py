#!/usr/bin/env python3
"""Collect NBA box scores for the Courtside dashboard.

Runs in the same GitHub Action as fetch_news.py. Uses ESPN's public scoreboard and
game summary data (unofficial, no API key) to build:

  stats.json  - season totals per Sleeper player, this season and last season
  logs.json   - this season's game-by-game lines per Sleeper player

Work is incremental: each run only fetches games it hasn't processed yet, and
last season is backfilled a chunk at a time so no single run takes long.
Progress lives in data/stats_state.json.

Standard library only.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import ROOT, fetch, load_index, norm  # noqa: E402

STATS_FILE = ROOT / "stats.json"
LOGS_FILE = ROOT / "logs.json"
STATE_FILE = ROOT / "data" / "stats_state.json"

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ET = ZoneInfo("America/New_York")

MAX_SECONDS_PER_RUN = 9 * 60   # work until this much time has passed; anything left continues next run
MAX_GAMES_PER_RUN = 2000        # effectively "a whole season", the time limit is what stops a run
MAX_DAYS_PER_RUN = 400
PAUSE = 0.25                # seconds between ESPN requests, to be polite

# One game line, in this order, everywhere
LINE = ["min", "pts", "reb", "ast", "stl", "blk", "to", "fgm", "fga", "tpm", "tpa",
        "ftm", "fta", "oreb", "dreb", "pf", "pm"]
TOTALS = ["gp"] + LINE[:-1] + ["dd", "td"]      # season totals (no +/-), plus double/triple doubles
LOG_COLS = ["d", "opp", "ha", "res"] + LINE

# ESPN box score columns, by label and by key
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
    if ":" in s:                                   # "34:12" minutes
        m, sec = s.split(":", 1)
        return round(int(m) + int(sec) / 60, 1)
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return 0.0


def season_bounds(today: date) -> tuple[tuple[date, date, str], tuple[date, date, str]]:
    """Current and previous NBA seasons as (start scan date, end scan date, label)."""
    y = today.year if today.month >= 9 else today.year - 1     # season that started in fall of y
    cur = (date(y, 10, 1), today, f"{y}-{str(y + 1)[2:]}")
    prev = (date(y - 1, 10, 1), date(y, 6, 30), f"{y - 1}-{str(y)[2:]}")
    return cur, prev


# ---------------------------------------------------------------- parsing
def parse_summary(doc: dict, match_name) -> list[tuple[str, dict]]:
    """Return (sleeper_id, line) for every player who played in this game."""
    comp = (doc.get("header") or {}).get("competitions", [{}])[0]
    teams = {}
    for c in comp.get("competitors", []):
        t = c.get("team") or {}
        teams[t.get("abbreviation")] = {"ha": "H" if c.get("homeAway") == "home" else "A",
                                       "score": num(c.get("score")), "win": bool(c.get("winner"))}
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
            cols = []
            for i in range(max(len(labels), len(keys))):
                lab = labels[i] if i < len(labels) else ""
                key = keys[i] if i < len(keys) else ""
                cols.append(LABEL_MAP.get(lab) or KEY_MAP.get(key))
            for a in block.get("athletes", []):
                stats = a.get("stats") or []
                if a.get("didNotPlay") or not stats:
                    continue
                name = (a.get("athlete") or {}).get("displayName", "")
                pid = match_name(name)
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
                    continue                       # listed but never got in
                line.update(opp=opp_abbr, ha=me.get("ha", ""), res=res)
                out.append((pid, line))
    return out


def add_to_totals(tot: dict, line: dict) -> None:
    row = tot or {k: 0 for k in TOTALS}
    row["gp"] += 1
    for k in LINE[:-1]:
        row[k] = round(row[k] + line[k], 1)
    tens = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if line[k] >= 10)
    row["dd"] += 1 if tens >= 2 else 0
    row["td"] += 1 if tens >= 3 else 0
    tot.clear()
    tot.update(row)


# ---------------------------------------------------------------- crawling
def crawl(cursor: date, end: date, done: set[str], budget: dict, on_game) -> date:
    """Walk scoreboard days from cursor to end. Returns the new cursor (first day not fully done)."""
    day, days = cursor, 0
    while day <= end and days < MAX_DAYS_PER_RUN and budget["games"] > 0 and time.time() < budget["deadline"]:
        board = get_json(f"{SITE}/scoreboard?dates={day:%Y%m%d}&limit=50")
        days += 1
        events = board.get("events") or []
        all_final = True
        for ev in events:
            season = ev.get("season") or {}
            if season.get("type") != 2:            # regular season only
                continue
            st = (ev.get("status") or {}).get("type") or {}
            if not st.get("completed"):
                if st.get("state") == "post":      # postponed or canceled: nothing to collect
                    continue
                all_final = False
                continue
            eid = str(ev.get("id"))
            if eid in done:
                continue
            if budget["games"] <= 0 or time.time() >= budget["deadline"]:
                all_final = False
                break
            summary = get_json(f"{SITE}/summary?event={eid}")
            on_game(day, summary)
            done.add(eid)
            budget["games"] -= 1
        if not all_final:
            break                                  # come back to this day next run
        day += timedelta(days=1)
    return day


def main() -> int:
    idx = load_index()
    names = idx["names"]
    missed: set[str] = set()

    def match_name(n: str):
        pid = names.get(norm(n))
        if not pid:
            missed.add(n)
        return pid

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    today = datetime.now(ET).date()
    (cs, ce, clabel), (ps, pe, plabel) = season_bounds(today)

    # A new season resets the current-season log and starts last season's backfill over.
    if state.get("cur_label") != clabel:
        state = {"cur_label": clabel, "cur_cursor": cs.isoformat(), "cur_done": [],
                 "prev_label": plabel, "prev_cursor": ps.isoformat(), "prev_done": [], "prev_tot": {},
                 "prev_complete": False}
        logs = {}
    else:
        logs = json.loads(LOGS_FILE.read_text()).get("players", {}) if LOGS_FILE.exists() else {}

    budget = {"games": MAX_GAMES_PER_RUN, "deadline": time.time() + MAX_SECONDS_PER_RUN}
    stats = {"cur": 0, "prev": 0}

    # ---- this season: game logs
    def on_cur(day, summary):
        rows = parse_summary(summary, match_name)
        for pid, line in rows:
            row = [day.isoformat(), line["opp"], line["ha"], line["res"]] + [line[k] for k in LINE]
            logs.setdefault(pid, [])
            if not any(r[0] == row[0] for r in logs[pid]):
                logs[pid].append(row)
        stats["cur"] += 1

    done_cur = set(state["cur_done"])
    cur_cursor = crawl(date.fromisoformat(state["cur_cursor"]), ce, done_cur, budget, on_cur)
    # yesterday and today are re-checked every run in case late games finished
    state["cur_cursor"] = min(cur_cursor, today - timedelta(days=1)).isoformat()
    state["cur_done"] = sorted(done_cur)

    # ---- last season: totals only, backfilled in chunks
    if not state.get("prev_complete") and budget["games"] > 0 and time.time() < budget["deadline"]:
        def on_prev(day, summary):
            for pid, line in parse_summary(summary, match_name):
                add_to_totals(state["prev_tot"].setdefault(pid, {}), line)
            stats["prev"] += 1

        done_prev = set(state["prev_done"])
        prev_cursor = crawl(date.fromisoformat(state["prev_cursor"]), pe, done_prev, budget, on_prev)
        state["prev_cursor"] = prev_cursor.isoformat()
        state["prev_done"] = sorted(done_prev)
        if prev_cursor > pe:
            state["prev_complete"] = True

    # ---- write outputs
    cur_tot: dict[str, dict] = {}
    for pid, rows in logs.items():
        rows.sort(key=lambda r: r[0])
        t: dict = {}
        for r in rows:
            add_to_totals(t, dict(zip(LINE, r[4:])))
        cur_tot[pid] = t

    pack = lambda tots: {pid: [t[k] for k in TOTALS] for pid, t in tots.items() if t}  # noqa: E731
    out_stats = {
        "updated": int(time.time() * 1000),
        "cols": TOTALS,
        "cur": {"label": clabel, "players": pack(cur_tot)},
        "last": {"label": plabel, "complete": state.get("prev_complete", False),
                 "games": len(state["prev_done"]), "players": pack(state["prev_tot"])},
        "source": "ESPN box scores",
    }
    out_logs = {"cols": LOG_COLS, "label": clabel, "players": logs}

    def write_if_changed(path: Path, data: dict, ignore=("updated",)) -> bool:
        old = json.loads(path.read_text()) if path.exists() else None
        if old is not None and {k: v for k, v in old.items() if k not in ignore} == \
                {k: v for k, v in data.items() if k not in ignore}:
            return False
        path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
        return True

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    a = write_if_changed(STATS_FILE, out_stats)
    b = write_if_changed(LOGS_FILE, out_logs)
    print(f"This season: {stats['cur']} new games, {len(logs)} players with logs.")
    print(f"Last season: {stats['prev']} new games this run, {len(state['prev_done'])} total, "
          f"{'complete' if state.get('prev_complete') else 'backfilling (continues next run)'}.")
    print("stats.json", "updated" if a else "unchanged", "| logs.json", "updated" if b else "unchanged")
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
