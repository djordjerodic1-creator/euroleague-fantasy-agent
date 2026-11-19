"""Import the official game's current prices from a browser HAR file into app_prices.json.

How to make the HAR (about a minute, logged in at euroleaguefantasy.euroleaguebasketball.net):
  F12 -> Network tab -> filter "fantaking" -> open the transfer market / player list in the game
  -> right-click any request -> "Save all as HAR with content".
Then:
  python import_prices.py "<path to the .har>" [round]

Only player/coach names, prices and availability are saved. Nothing from your account (and no login token)
leaves the HAR file. Don't commit or share the HAR itself: it contains your session.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# the game's club abbreviations -> ours
CLUB = {"PAO": "PAN", "OLY": "OLY", "RMB": "MAD", "FBT": "ULK", "VBC": "PAM", "BAR": "BAR", "DUB": "DUB", "HTA": "HTA",
        "ZAL": "ZAL", "CZV": "RED", "PAR": "PAR", "EFS": "IST", "BJK": "BES", "PBB": "PRS", "ASV": "ASV", "MTA": "TEL",
        "MIL": "MIL", "KBA": "BAS", "VIR": "VIR", "BAY": "MUN"}


def players_from_har(path):
    har = json.loads(Path(path).read_text(encoding="utf-8"))
    best = None
    for e in har["log"]["entries"]:
        if e["request"]["method"] != "GET" or "/players-lists/" not in e["request"]["url"]:
            continue
        c = e["response"].get("content", {})
        t = c.get("text") or ""
        if c.get("encoding") == "base64":
            t = base64.b64decode(t).decode("utf-8")
        try:
            data = json.loads(t).get("data") or []
        except Exception:
            continue
        if len(data) > len(best or []):
            best = data
    if not best:
        sys.exit("No player list in this HAR. Open the transfer market / player list in the game before saving it.")
    return best


def latest_target_round():
    digs = sorted((ROOT / "data").glob("*/digest.json"))
    return json.loads(digs[-1].read_text(encoding="utf-8")).get("target_round") if digs else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    rows = players_from_har(sys.argv[1])
    rnd = int(sys.argv[2]) if len(sys.argv) > 2 else latest_target_round()
    prices, info, coaches = {}, {}, {}
    for x in rows:
        name = f"{x.get('first_name', '')} {x.get('last_name', '')}".strip()
        q = x.get("quotation")
        if q is None:
            continue
        if (x.get("position") or {}).get("name") == "Head Coach":
            code = CLUB.get((x.get("team") or {}).get("abbreviation"))
            if code:
                coaches[code] = q
            continue
        prices[name] = q
        info[name] = {"t": CLUB.get((x.get("team") or {}).get("abbreviation")), "last": x.get("last_name", ""),
                      "pos": (x.get("position") or {}).get("name"), "injured": bool(x.get("is_injured")), "p_play": x.get("probability_of_playing"),
                      "owned": round(100 * (x.get("popularity") or 0), 1)}
    out = {"round": rnd, "updated": dt.date.today().isoformat(), "source": "official game (browser HAR)",
           "prices": dict(sorted(prices.items())), "coaches": dict(sorted(coaches.items())), "info": info}
    (ROOT / "app_prices.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"app_prices.json: {len(prices)} players and {len(coaches)} coaches for round {rnd}")


if __name__ == "__main__":
    main()
