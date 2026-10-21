#!/usr/bin/env python3
"""One-time check: how far back do ESPN's NBA box scores match today's format?

For every season from 2001-02 to last season, pulls 3 regular-season games and
checks each against what fetch_stats.py relies on:

  1. The same 14 box score columns, by label (MIN FG 3PT FT OREB DREB REB AST STL BLK TO PF +/- PTS)
  2. Minutes are numbers, shooting is "made-attempted"
  3. The numbers add up: players' points = team score, OREB + DREB = REB, made <= attempted
  4. Both teams have a full set of player rows

Writes data/season_probe.md (readable report) and data/season_probe.json.
Reads nothing from and changes nothing in the stats data. Standard library only.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import ROOT, fetch  # noqa: E402
from fetch_stats import ET, SITE, num  # noqa: E402

EXPECTED = ["MIN", "FG", "3PT", "FT", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PF", "+/-", "PTS"]
FIRST_SEASON = 2001               # 2001-02
SAMPLE_DATES = [(12, 10), (1, 25), (3, 15)]   # early, middle, late season
OUT_MD = ROOT / "data" / "season_probe.md"
OUT_JSON = ROOT / "data" / "season_probe.json"


def get_json(url: str) -> dict:
    time.sleep(0.25)
    return json.loads(fetch(url))


def find_game(day: date) -> str | None:
    """First finished regular-season game on or within 6 days after `day`."""
    for i in range(7):
        d = day + timedelta(days=i)
        board = get_json(f"{SITE}/scoreboard?dates={d:%Y%m%d}&limit=50")
        for ev in board.get("events") or []:
            if (ev.get("season") or {}).get("type") == 2 and ((ev.get("status") or {}).get("type") or {}).get("completed"):
                return str(ev["id"])
    return None


def check_game(doc: dict) -> list[str]:
    """Return a list of problems (empty = identical to today's format)."""
    problems = []
    comp = (doc.get("header") or {}).get("competitions", [{}])[0]
    scores = {(c.get("team") or {}).get("abbreviation"): num(c.get("score")) for c in comp.get("competitors", [])}
    sides = (doc.get("boxscore") or {}).get("players") or []
    if len(sides) != 2:
        return [f"box score has {len(sides)} team sections, expected 2"]
    for side in sides:
        abbr = (side.get("team") or {}).get("abbreviation", "?")
        blocks = side.get("statistics") or []
        if not blocks:
            problems.append(f"{abbr}: no player stats")
            continue
        block = blocks[0]
        labels = block.get("labels") or []
        if labels != EXPECTED:
            missing = [x for x in EXPECTED if x not in labels]
            extra = [x for x in labels if x not in EXPECTED]
            if missing or extra:
                problems.append(f"{abbr}: columns differ (missing {missing or 'none'}, extra {extra or 'none'})")
            else:
                problems.append(f"{abbr}: same columns in a different order")
        col = {lab: i for i, lab in enumerate(labels)}
        played = [a for a in block.get("athletes", []) if a.get("stats") and not a.get("didNotPlay")]
        if len(played) < 7:
            problems.append(f"{abbr}: only {len(played)} players with stats")
        pts_sum = 0.0
        for a in played:
            s = a["stats"]
            name = (a.get("athlete") or {}).get("displayName", "?")
            g = lambda k: s[col[k]] if k in col and col[k] < len(s) else None  # noqa: E731
            m = g("MIN")
            if m is not None and str(m).strip() not in ("", "--") and not str(m).replace(":", "").isdigit():
                problems.append(f"{abbr}: minutes look unusual for {name} ({m!r})")
            for k in ("FG", "3PT", "FT"):
                v = g(k)
                if v is None:
                    continue
                made, sep, att = str(v).partition("-")
                if not sep or not made.isdigit() or not att.isdigit():
                    problems.append(f"{abbr}: {k} not in made-attempted form for {name} ({v!r})")
                elif int(made) > int(att):
                    problems.append(f"{abbr}: {k} made > attempted for {name} ({v})")
            if all(k in col for k in ("OREB", "DREB", "REB")):
                if num(g("OREB")) + num(g("DREB")) != num(g("REB")):
                    problems.append(f"{abbr}: OREB + DREB != REB for {name}")
            pts_sum += num(g("PTS")) if "PTS" in col else 0
        if abbr in scores and "PTS" in col and abs(pts_sum - scores[abbr]) > 0.5:
            problems.append(f"{abbr}: player points add to {int(pts_sum)}, team scored {int(scores[abbr])}")
    return problems


def main() -> int:
    today = datetime.now(ET).date()
    last_start = (today.year if today.month >= 9 else today.year - 1) - 1    # last completed season
    results = []
    for y in range(last_start, FIRST_SEASON - 1, -1):
        label = f"{y}-{str(y + 1)[2:]}"
        games, issues = [], []
        for month, day in SAMPLE_DATES:
            when = date(y if month >= 9 else y + 1, month, day)
            try:
                eid = find_game(when)
                if not eid:
                    issues.append(f"no finished game found near {when}")
                    continue
                probs = check_game(get_json(f"{SITE}/summary?event={eid}"))
                games.append({"event": eid, "near": when.isoformat(), "problems": probs})
                issues += [f"game {eid}: {p}" for p in probs]
            except Exception as e:
                issues.append(f"near {when}: request failed ({type(e).__name__}: {e})")
        ok = not issues and len(games) == len(SAMPLE_DATES)
        results.append({"season": label, "identical": ok, "games_checked": len(games), "issues": issues[:12]})
        print(f"{label}: {'identical' if ok else 'DIFFERENT'}" + ("" if ok else f" ({issues[0]})"))

    # earliest season such that it and every season after it are identical
    cutoff = None
    for r in results:                      # newest first
        if not r["identical"]:
            break
        cutoff = r["season"]

    lines = ["# ESPN box score format check", "",
             f"Run {datetime.now(ET):%Y-%m-%d %H:%M} ET. 3 games per season checked against today's format.", "",
             f"**Earliest season identical to today (with every later season also identical): {cutoff or 'none'}**", "",
             "| Season | Result | Notes |", "|---|---|---|"]
    for r in results:
        note = "; ".join(r["issues"][:3]).replace("|", "/") if r["issues"] else ""
        lines.append(f"| {r['season']} | {'Identical' if r['identical'] else 'Different'} | {note} |")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n")
    OUT_JSON.write_text(json.dumps({"cutoff": cutoff, "seasons": results}, indent=1))
    print(f"\nEarliest consistent season: {cutoff or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
