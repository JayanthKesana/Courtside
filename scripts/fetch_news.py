#!/usr/bin/env python3
"""Collect NBA player news for the Courtside dashboard.

Runs on a schedule in GitHub Actions. Pulls recent player news from a few public
feeds, matches each item to a Sleeper player ID by name, merges it with what's
already in news.json, and rewrites news.json only when the news actually changed.

Standard library only, so the workflow needs no pip installs.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEWS_FILE = ROOT / "news.json"
INDEX_FILE = ROOT / "data" / "players_index.json"

KEEP_DAYS = 21          # drop news older than this
PER_PLAYER = 12         # newest items kept per player
INDEX_TTL = 24 * 3600   # Sleeper asks for the full player list at most once a day

UA = "Mozilla/5.0 (compatible; CourtsideNewsBot/1.0; +https://github.com)"

SOURCES = {
    # name: (url, parser name, display label)
    # ESPN's general NBA news turned out to be previews and features, not player updates,
    # so RotoWire's player-news feed is the only source for now. parse_espn stays available.
    "rotowire": ("https://www.rotowire.com/rss/news.php?sport=NBA", "rss", "RotoWire"),
}
INDEX_VERSION = 2


# ---------------------------------------------------------------- helpers
def fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z\s-]", " ", s).replace("-", " ")
    s = SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def split_analysis(body: str) -> tuple[str, str]:
    m = re.split(r"\n*\s*(?:Analysis|ANALYSIS|Fantasy Impact|Spin)\s*:\s*", body, maxsplit=1)
    return (m[0].strip(), m[1].strip()) if len(m) == 2 else (body.strip(), "")


def to_ms(value) -> int | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return int(value if value > 1e12 else value * 1000)
        if re.match(r"\d{4}-\d{2}-\d{2}", value):
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        return int(email.utils.parsedate_to_datetime(value).timestamp() * 1000)
    except Exception:
        return None


def item_id(src: str, title: str, ts: int | None) -> str:
    day = str((ts or 0) // 864e5)
    return hashlib.sha1(f"{src}|{norm(title)}|{day}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------- player index
def load_index() -> dict:
    cached = json.loads(INDEX_FILE.read_text()) if INDEX_FILE.exists() else None
    if cached and cached.get("v") == INDEX_VERSION and time.time() - cached.get("at", 0) < INDEX_TTL:
        return cached
    try:
        raw = json.loads(fetch("https://api.sleeper.app/v1/players/nba", timeout=60))
    except Exception as e:
        if cached:
            print(f"Sleeper player list unavailable ({e}); using cached index", file=sys.stderr)
            return cached
        raise
    names: dict[str, str] = {}
    rank: dict[str, int] = {}
    for pid, p in raw.items():
        # Sleeper's list also has team entries and long-retired players; skip both
        if not pid.isdigit() or not (p.get("team") or p.get("active")):
            continue
        full = p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
        key = norm(full)
        if not key or " " not in key:
            continue
        score = (2 if p.get("team") else 0) + (1 if p.get("active") else 0)
        if score > rank.get(key, -1):      # on name clashes, prefer the active player on a team
            names[key], rank[key] = pid, score
    idx = {"v": INDEX_VERSION, "at": int(time.time()), "names": names}
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(idx, separators=(",", ":"), sort_keys=True))
    return idx


class Matcher:
    def __init__(self, names: dict[str, str]):
        self.names = names
        # longest names first so "Jaren Jackson" doesn't steal "Jaren Jackson Jr" style matches
        self.ordered = sorted(names, key=len, reverse=True)

    def by_name(self, name: str) -> str | None:
        return self.names.get(norm(name))

    def in_text(self, text: str) -> str | None:
        t = f" {norm(text)} "
        for key in self.ordered:
            if f" {key} " in t:
                return self.names[key]
        return None


# ---------------------------------------------------------------- parsers
def parse_rss(data: bytes, src: str, label: str, match: Matcher) -> list[tuple[str, dict]]:
    out = []
    root = ET.fromstring(data)
    raw_items = list(root.iter("item"))
    print(f"{src}: feed returned {len(raw_items)} items")
    for it in raw_items:
        title = clean(it.findtext("title") or "")
        desc = clean(it.findtext("description") or "")
        link = (it.findtext("link") or "").strip()
        ts = to_ms(it.findtext("pubDate"))
        pid, headline = None, title
        if ":" in title:                       # RotoWire titles look like "Player Name: Headline"
            who, rest = title.split(":", 1)
            pid = match.by_name(who)
            if pid:
                headline = rest.strip()
        pid = pid or match.in_text(title) or match.in_text(desc[:200])
        if not pid or not (headline or desc):
            continue
        body, analysis = split_analysis(desc)
        out.append((pid, {"id": item_id(src, title, ts), "t": headline or title, "b": body, "a": analysis,
                          "src": label, "url": link, "ts": ts or int(time.time() * 1000)}))
    return out


def parse_espn(data: bytes, src: str, label: str, match: Matcher) -> list[tuple[str, dict]]:
    out = []
    doc = json.loads(data)
    feed = doc.get("feed") or doc.get("articles") or doc.get("headlines") or []
    for it in feed:
        title = clean(it.get("headline") or it.get("title") or "")
        body_raw = it.get("story") or it.get("description") or ""
        desc = clean(body_raw)
        ts = to_ms(it.get("published") or it.get("lastModified"))
        link = ""
        links = it.get("links") or {}
        if isinstance(links, dict):
            link = ((links.get("web") or {}).get("href")) or ""
        athletes = [c.get("description") or (c.get("athlete") or {}).get("description") or ""
                    for c in it.get("categories") or [] if c.get("type") == "athlete"]
        pids = [p for p in (match.by_name(a) for a in athletes if a) if p]
        if not pids:
            p = match.in_text(title) or match.in_text(desc[:200])
            pids = [p] if p else []
        # Long features that name many players aren't player updates; skip them.
        if not pids or len(pids) > 2 or not title:
            continue
        body, analysis = split_analysis(desc)
        if len(body) > 900:
            body = body[:880].rsplit(" ", 1)[0] + "…"
        for pid in pids:
            out.append((pid, {"id": item_id(src, title, ts), "t": title, "b": body, "a": analysis,
                              "src": label, "url": link, "ts": ts or int(time.time() * 1000)}))
    return out


PARSERS = {"rss": parse_rss, "espn": parse_espn}


# ---------------------------------------------------------------- main
def main() -> int:
    idx = load_index()
    match = Matcher(idx["names"])
    old = json.loads(NEWS_FILE.read_text()) if NEWS_FILE.exists() else {"players": {}, "sources": {}}

    players: dict[str, list[dict]] = {k: list(v) for k, v in old.get("players", {}).items()}
    status = {}
    for name, (url, kind, label) in SOURCES.items():
        try:
            items = PARSERS[kind](fetch(url), name, label, match)
            status[name] = {"ok": True, "count": len(items)}
            for pid, item in items:
                players.setdefault(pid, []).append(item)
            print(f"{name}: {len(items)} player items")
        except Exception as e:
            status[name] = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"{name}: FAILED {e}", file=sys.stderr)

    cutoff = int(time.time() * 1000) - KEEP_DAYS * 864e5
    labels = {label for _, _, label in SOURCES.values()}
    valid = set(match.names.values())
    merged = {}
    for pid, items in players.items():
        if pid not in valid:          # drops items saved against team entries or retired players
            continue
        items = [it for it in items if it.get("src") in labels]   # drops items from retired sources
        seen, keep = set(), []
        for it in sorted(items, key=lambda x: x.get("ts") or 0, reverse=True):
            # the same story often appears in two feeds; keep the first copy
            sig = norm(it["t"])[:60]
            if it["id"] in seen or sig in seen or (it.get("ts") or 0) < cutoff:
                continue
            seen.update({it["id"], sig})
            keep.append(it)
        if keep:
            merged[pid] = keep[:PER_PLAYER]

    if not any(s["ok"] for s in status.values()):
        print("Every source failed; leaving news.json unchanged.", file=sys.stderr)
        return 1

    ok_changed = {k: v["ok"] for k, v in status.items()} != {k: v.get("ok") for k, v in old.get("sources", {}).items()}
    if merged == old.get("players") and not ok_changed:
        print("No new news; news.json unchanged.")
        return 0

    out = {"updated": int(time.time() * 1000), "sources": status, "players": merged}
    NEWS_FILE.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False, sort_keys=True))
    total = sum(len(v) for v in merged.values())
    print(f"Wrote news.json: {total} items for {len(merged)} players")
    return 0


if __name__ == "__main__":
    sys.exit(main())
