"""Download past EuroLeague seasons' box scores (official public API) into compact files for the projection model.

Writes data/history/<season>.json: {"season": "E2024", "rows": [[person_code, club, opp, home, date, round, min, pir], ...]}
Past seasons never change, so this runs once per season (the files are committed). The current season comes from
basketballstats.eu's game logs in collect.py, so the daily run never needs this API.

Usage:  python fetch_history.py E2023 E2024 E2025
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

API = "https://api-live.euroleague.net"
OUT = Path(__file__).resolve().parent / "data" / "history"
UA = "Mozilla/5.0 (fantasy-research-script)"


def get(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # transient network errors: back off and retry
            if i == tries - 1:
                print(f"  ! {url}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))


def season(code: str):
    games = (get(f"{API}/v2/competitions/E/seasons/{code}/games") or {}).get("data") or []
    games = [g for g in games if g.get("played")]
    print(f"{code}: {len(games)} played games")
    rows = []
    for i, g in enumerate(sorted(games, key=lambda g: g["gameCode"])):
        st = get(f"{API}/v3/competitions/E/seasons/{code}/games/{g['gameCode']}/stats")
        if not st:
            continue
        clubs = {"local": g["local"]["club"]["code"], "road": g["road"]["club"]["code"]}
        for side, other in (("local", "road"), ("road", "local")):
            for p in (st.get(side) or {}).get("players") or []:
                s = p.get("stats") or {}
                sec = s.get("timePlayed") or 0
                if sec <= 0:
                    continue  # DNP: not a performance (availability is handled by status, not by the average)
                rows.append([p["player"]["person"]["code"], clubs[side], clubs[other], 1 if side == "local" else 0,
                             g["date"][:10], g["round"], round(sec / 60, 1), s.get("valuation", 0)])
        if i % 50 == 0:
            print(f"  {i}/{len(games)}")
        time.sleep(0.15)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{code}.json").write_text(json.dumps({"season": code, "rows": rows}, separators=(",", ":")), encoding="utf-8")
    print(f"  wrote {len(rows)} player-games")


if __name__ == "__main__":
    for c in sys.argv[1:] or ["E2023", "E2024", "E2025"]:
        season(c)
