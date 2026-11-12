"""EuroLeague Fantasy evidence collector.

Pulls raw data from basketballstats.eu and writes a compact, analyst-ready digest
(data/<date>/digest.md + digest.json). It does NOT make the decisions: the daily
agent reads the digest together with PLAYBOOK.md and does the actual thinking.

Usage:  python collect.py            (uses my_team.json next to this file)
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://basketballstats.eu"
UA = "Mozilla/5.0 (fantasy-research-script)"
TODAY = dt.date.today()
OUT = ROOT / "data" / TODAY.isoformat()
CACHE = ROOT / "data" / "players_cache.json"

TEAMS = {
    "ASV": ("ASVEL", "LNB Élite (France)"), "BAR": ("Barcelona", "Liga ACB (Spain)"),
    "BAS": ("Baskonia", "Liga ACB (Spain)"), "BES": ("Besiktas", "BSL (Turkey)"),
    "DUB": ("Dubai Basketball", "ABA League"), "HTA": ("Hapoel Tel Aviv", "Winner League (Israel)"),
    "IST": ("Anadolu Efes", "BSL (Turkey)"), "MAD": ("Real Madrid", "Liga ACB (Spain)"),
    "MIL": ("Olimpia Milano", "LBA (Italy)"), "MUN": ("Bayern Munich", "BBL (Germany)"),
    "OLY": ("Olympiacos", "GBL (Greece)"), "PAM": ("Valencia Basket", "Liga ACB (Spain)"),
    "PAN": ("Panathinaikos", "GBL (Greece)"), "PAR": ("Partizan", "ABA League"),
    "PRS": ("Paris Basketball", "LNB Élite (France)"), "RED": ("Crvena Zvezda", "ABA League"),
    "TEL": ("Maccabi Tel Aviv", "Winner League (Israel)"), "ULK": ("Fenerbahce", "BSL (Turkey)"),
    "VIR": ("Virtus Bologna", "LBA (Italy)"), "ZAL": ("Zalgiris", "LKL (Lithuania)"),
}
NAME2CODE = {v[0].lower(): k for k, v in TEAMS.items()}
ROSTER = {"Guard": 4, "Forward": 4, "Center": 2}
MAX_PER_TEAM, CAP = 6, 100.0
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"], 1)}


# ---------------------------------------------------------------- fetching
def get(path: str, delay: float = 0.4) -> str:
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as e:  # keep going; a missing page should not kill the run
        print(f"  ! {url}: {e}", file=sys.stderr)
        body = ""
    time.sleep(delay)
    return body


def text_of(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def num(s):
    try:
        return float(str(s).replace("+", "").replace("%", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- parsers
def parse_calendar(txt: str):
    rounds = {}
    for m in re.finditer(r"Round (\d+)(?: · next)? (\d+) (\w+) – (\d+) (\w+) (\d) ((?:Double week )?)"
                         r"(♾ Unlimited trades \(([^)]*)\)|4 trades)", txt):
        r = int(m.group(1))
        def d(day, mon):
            mo = MONTHS.get(mon[:4] if mon.startswith("Sept") else mon[:3], 0)
            return dt.date(2026 if mo >= 8 else 2027, mo, int(day)) if mo else None
        rounds[r] = {"start": d(m.group(2), m.group(3)), "end": d(m.group(4), m.group(5)),
                     "double_week": bool(m.group(7)), "unlimited_before": bool(m.group(9)),
                     "unlimited_window": m.group(9)}
    return rounds


def parse_prices(page: str):
    """'All players' table: every player incl. teams already done this round."""
    i = page.find("<h2>All players")
    out = {}
    if i < 0:
        return out
    seg = page[i:page.find("</table>", i)]
    for row in re.findall(r"<tr>(.*?)</tr>", seg, flags=re.S):
        m = re.search(r'href="/en/player/([^/]+)/">([^<]+)</a>\s*<small>([^<]+)</small>', row)
        if not m:
            continue
        cells = [text_of(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
        team, pos = [x.strip() for x in m.group(3).split("·")]
        out[m.group(1)] = {
            "slug": m.group(1), "name": html.unescape(m.group(2)), "team_name": team,
            "t": NAME2CODE.get(team.lower()), "pos": {"G": "Guard", "F": "Forward", "C": "Center"}.get(pos, pos),
            "price": num(cells[1]), "since_start": num(cells[2]), "owned": num(cells[3]),
            "pir_ge_price": cells[4], "proj_price_change": num(cells[5]),
        }
    return out


def parse_round_log(page: str):
    txt = text_of(page)
    return [(int(r), int(p), float(m)) for r, p, m in re.findall(r"Round (\d+): (-?\d+) PIR, ([\d.]+)'", txt)]


def parse_preview(page: str):
    txt = text_of(page)
    a = txt.find("Preview ")
    b = txt.find("Scenario simulator")
    core = txt[a:b if b > a else a + 1500]
    km = txt.find("Key matchups")
    extra = txt[km:km + 900] if km > 0 else ""
    fav = re.search(r"Favourite: (.+?), with a (\d+)% chance", txt)
    teams = re.search(r"Preview (.+?) vs (.+?): ", txt)
    when = re.search(r"tips off \w+ (\d+) (\w+) at ([\d:]+)", txt)
    score = re.search(r"projects (\d+)-(\d+)", txt)
    w = txt.find("Projected PIR", txt.find("Players to watch", txt.find("Coaches:")))
    proj = {n.strip(): float(v) for n, v in
            re.findall(r"([A-Z][\w'.\- ]+?) (-?\d+\.\d) PIR", txt[w + 14:w + 900])} if w > 0 else {}
    return {"summary": core[:1400], "key_matchups": extra,
            "favourite": fav.group(1) if fav else None, "fav_pct": int(fav.group(2)) if fav else None,
            "home": NAME2CODE.get(teams.group(1).lower()) if teams else None,
            "away": NAME2CODE.get(teams.group(2).lower()) if teams else None,
            "fav_code": NAME2CODE.get(fav.group(1).lower()) if fav else None,
            "date": f"{when.group(1)} {when.group(2)[:3]}" if when else None, "time": when.group(3) if when else None,
            "score": [int(score.group(1)), int(score.group(2))] if score else None, "player_proj": proj}


# ---------------------------------------------------------------- helpers
def match_player(name, pool):
    key = norm(name)
    by = {}
    for p in pool.values():
        by.setdefault(norm(p["name"]), p)
        if p.get("n"):  # "VEZENKOV, SASHA"
            last, _, first = p["n"].partition(",")
            by.setdefault(norm(f"{first} {last}"), p)
    if key in by:
        return by[key]
    hit = difflib.get_close_matches(key, list(by), n=1, cutoff=0.72)
    if hit:
        return by[hit[0]]
    # last-name only
    cands = [p for k, p in by.items() if k.split()[-1:] == key.split()[-1:]]
    return cands[0] if len(cands) == 1 else None


def round_proj(p, r):
    for e in (p.get("nx") or []) + (p.get("nxe") or []):
        if e[0] == r:
            return {"round": r, "opp": e[1], "home": bool(e[2]), "proj": e[3], "difficulty": e[4], "b2b": bool(e[5])}
    return None


def status_factor(s):
    return {"injured": 0.0, "out": 0.0, "rest": 0.0, "doubtful": 0.5}.get(s, 1.0)


def window_score(p, rounds):
    s, w = 0.0, 1.0
    for r in rounds:
        rp = round_proj(p, r)
        if rp:
            s += w * rp["proj"]
        w *= 0.7
    return round(s * status_factor(p.get("status")) if rounds and rounds[0] else s, 2)


# ---------------------------------------------------------------- main
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    team_cfg = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8"))

    print("Fetching projections, news, calendar, injuries, prices…")
    proj = json.loads(get("/api/fantasy/projections/latest.json") or "{}")
    if not proj.get("players"):
        sys.exit("FATAL: basketballstats.eu unreachable or returned no projections "
                 "(in a cloud run: allow basketballstats.eu in the environment's network settings). "
                 "Nothing written.")
    news = json.loads(get("/api/fantasy/news.json") or "{}")
    cal_txt = text_of(get("/en/fantasy/calendar/"))
    inj_txt = text_of(get("/en/injury-report/"))
    hub = get("/en/fantasy/")
    pr_link = re.search(r'href="(/en/fantasy/round-\d+/player-prices/)"', hub)
    prices = parse_prices(get(pr_link.group(1))) if pr_link else {}
    tm = {m.group(1): m.groupdict() for m in re.finditer(
        r'data-c="(?P<c>\d+)"[^>]*?data-d3="(?P<d3>[^"]*)" data-d5="(?P<d5>[^"]*)" data-d10="(?P<d10>[^"]*)"',
        get("/en/fantasy/transfer-market/"))}
    (OUT / "projections.json").write_text(json.dumps(proj, ensure_ascii=False), encoding="utf-8")
    (OUT / "news.json").write_text(json.dumps(news, ensure_ascii=False), encoding="utf-8")

    rounds = parse_calendar(cal_txt)
    upcoming = [r for r, v in sorted(rounds.items()) if v["start"] and v["start"] > TODAY]
    target = upcoming[0] if upcoming else proj.get("round", 0) + 1
    in_progress = [r for r, v in rounds.items() if v["start"] and v["start"] <= TODAY <= v["end"]]
    # horizon: target round up to the round right before the next unlimited-trade window
    horizon = [target]
    for r in range(target + 1, target + 4):
        if r not in rounds or rounds[r]["unlimited_before"]:
            break
        horizon.append(r)

    # merge projection players into a persistent cache (teams that already played drop out of latest.json)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    for p in proj.get("players", []):
        p["_seen"] = TODAY.isoformat()
        if p["c"] in tm:
            p["price_d3"], p["price_d5"], p["price_d10"] = (num(tm[p["c"]][k]) for k in ("d3", "d5", "d10"))
        cache[p["s"]] = p
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    pool = {}
    for slug, pr in prices.items():
        pool[slug] = {**pr, **cache.get(slug, {}), "name": pr["name"], "owned": pr["owned"],
                      "since_start": pr["since_start"], "proj_price_change": pr["proj_price_change"],
                      "price_official": pr["price"], "pir_ge_price": pr["pir_ge_price"]}
        if pr.get("t") and not pool[slug].get("t"):
            pool[slug]["t"] = pr["t"]
    for slug, p in cache.items():
        pool.setdefault(slug, {**p, "name": p.get("dn", slug)})
    for p in pool.values():
        p["price"] = p.get("price_official") or p.get("price")
        p["score"] = window_score(p, horizon) if p.get("nx") else None
        p["fresh"] = p.get("_seen") == TODAY.isoformat()

    # news indexed by player code
    news_by = {}
    for it in news.get("items", []):
        news_by.setdefault(it.get("c"), []).append(
            {k: it.get(k) for k in ("ts", "kind", "tag", "title", "before", "after", "source", "detail", "link")})

    code2name = {p.get("c"): p["name"] for p in pool.values() if p.get("c")}
    for p in pool.values():
        if p.get("vx") and p["vx"].get("a"):
            p["vx"]["absent_names"] = [code2name.get(c, c) for c in p["vx"]["a"]]

    # ----- my team
    mine, missing = [], []
    for nm in team_cfg.get("players", []):
        p = match_player(nm, pool)
        (mine.append(p) if p else missing.append(nm))
    coach_code = NAME2CODE.get(str(team_cfg.get("coach", "")).lower(), team_cfg.get("coach"))
    coaches = {c["t"]: c for c in proj.get("coaches", [])}
    coach_price = (coaches.get(coach_code) or {}).get("price", 0) or 0
    cost = sum(p.get("price") or 0 for p in mine) + coach_price
    bank = team_cfg.get("bank")
    bank = round(CAP - cost, 1) if bank in (None, "") else float(bank)
    club_count = {}
    for p in mine:
        club_count[p.get("t")] = club_count.get(p.get("t"), 0) + 1

    print(f"Fetching game logs for {len(mine)} roster players…")
    for p in mine:
        slug = p.get("slug") or p.get("s")
        p["round_log"] = parse_round_log(get(f"/en/fantasy/player/{slug}/"))
        if not p["round_log"]:  # team already played this round -> no fantasy page; use season summary
            t = text_of(get(f"/en/player/{slug}/"))
            m = re.search(r"in the EuroLeague \d{4}-\d{2}: (.+?PIR per game)", t)
            p["season_summary"] = m.group(1) if m else "no data"

    # ----- next-round game previews (matchup / coach view)
    print(f"Fetching Round {target} game previews…")
    links = set(re.findall(rf'href="(/en/game/[a-z0-9-]+-{target}/)"', get("/en/schedule/") + hub + get("/en/")))
    previews = {}
    for ln in sorted(links):
        pv = parse_preview(get(ln))
        pv["url"] = BASE + ln
        previews[ln.split("/")[3]] = pv

    # ----- mechanical baseline: candidate replacements (the analyst decides)
    def feasible(cand, out_p):
        if cand.get("pos") != out_p.get("pos") or cand in mine:
            return False
        if (cand.get("price") or 99) > bank + (out_p.get("price") or 0):
            return False
        cc = club_count.get(cand.get("t"), 0) - (1 if out_p.get("t") == cand.get("t") else 0)
        return cc < MAX_PER_TEAM
    avail = [p for p in pool.values() if p.get("score") is not None and p.get("status") not in ("injured", "out")]
    swaps = []
    for o in mine:
        best = sorted((c for c in avail if feasible(c, o)), key=lambda c: -(c["score"] or 0))[:5]
        swaps.append({"out": o["name"], "out_score": o.get("score"), "options": [
            {"name": c["name"], "team": c.get("t"), "price": c.get("price"), "score": c["score"],
             "gain": round((c["score"] or 0) - (o.get("score") or 0), 1), "owned": c.get("owned")} for c in best]})

    shortlists = {}
    for pos in ROSTER:
        lst = sorted((p for p in avail if p.get("pos") == pos), key=lambda p: -(p["score"] or 0))
        shortlists[pos] = {"top": lst[:12],
                           "value": sorted((p for p in lst if (p.get("price") or 0) <= 9 and (p["score"] or 0) > 0),
                                           key=lambda p: -(p["score"] / max(p["price"], 1)))[:8],
                           "differentials": [p for p in lst if (p.get("owned") or 100) < 5][:6]}

    digest = {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "today": TODAY.isoformat(), "source_round": proj.get("round"), "rounds_in_progress": in_progress,
        "target_round": target, "horizon": horizon,
        "calendar": {r: {k: (str(v) if isinstance(v, dt.date) else v) for k, v in rounds[r].items()}
                     for r in range(target - 1, target + 4) if r in rounds},
        "team": {"players": [p["name"] for p in mine], "unmatched": missing, "coach": coach_code,
                 "captain": team_cfg.get("captain"), "cost": round(cost, 1), "bank": bank, "club_count": club_count},
        "swaps": swaps, "previews": previews,
    }
    keep = ("name", "slug", "s", "c", "t", "pos", "price", "status", "owned", "l3", "l5", "l10", "m3", "m10",
            "nx", "nxe", "sig", "p10", "p90", "pwin", "proj_price_change", "price_d3", "season_summary")
    slim = [{k: p.get(k) for k in keep if p.get(k) is not None} for p in pool.values()]
    (OUT / "pool.json").write_text(json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    (OUT / "digest.json").write_text(json.dumps({**digest, "roster": mine}, ensure_ascii=False, default=str, indent=1),
                                     encoding="utf-8")
    write_markdown(digest, mine, shortlists, coaches, news_by, inj_txt, pool, news)
    print(f"Done → {OUT / 'digest.md'}")


def fmt_p(p, rounds):
    rp = [round_proj(p, r) for r in rounds]
    sched = " | ".join(f"R{x['round']} {'vs' if x['home'] else '@'}{x['opp']} {x['proj']} (diff {x['difficulty']}/5{', B2B' if x['b2b'] else ''})"
                       for x in rp if x) or "no projection"
    return sched


def write_markdown(dg, mine, shortlists, coaches, news_by, inj_txt, pool, news):
    H = dg["horizon"]
    L = [f"# Evidence digest — {dg['today']}",
         f"Target round: **R{dg['target_round']}** · decision horizon: {', '.join('R'+str(r) for r in H)} "
         f"· rounds in progress today: {dg['rounds_in_progress'] or 'none'} · projections file is for R{dg['source_round']}",
         "", "## Calendar"]
    for r, v in dg["calendar"].items():
        L.append(f"- R{r}: {v['start']} → {v['end']}{' · DOUBLE WEEK' if v['double_week'] else ''}"
                 f"{' · UNLIMITED trades before it (' + v['unlimited_window'] + ')' if v['unlimited_before'] else ' · 4 trades'}")
    t = dg["team"]
    L += ["", "## My team", f"Cost {t['cost']} · bank {t['bank']} · coach {t['coach']} · captain {t['captain']} · clubs {t['club_count']}"]
    if t["unmatched"]:
        L.append(f"**Could not match:** {t['unmatched']} — check spelling in my_team.json")
    if t["bank"] < 0:
        L.append(f"**WARNING: squad costs {t['cost']} > 100 — prices in my_team.json or roster are wrong; set 'bank' explicitly.**")
    for p in mine:
        L += ["", f"### {p['name']} — {TEAMS.get(p.get('t'), (p.get('t'),))[0]} · {p.get('pos')} · {p.get('price')} cr · owned {p.get('owned')}%",
              f"- Status: **{p.get('status', '?')}** · site signal {p.get('sig')} ({', '.join(p.get('why') or [])}) · grade {p.get('grade')} · data fresh today: {p.get('fresh')}",
              f"- Form PIR L3/L5/L10: {p.get('l3')}/{p.get('l5')}/{p.get('l10')} · minutes L3 {p.get('m3')} vs L10 {p.get('m10')} · PIR/min {p.get('pirpm')} · starts {int((p.get('start') or 0)*100)}%",
              f"- Round log (round, PIR, min): {p.get('round_log') or p.get('season_summary')}",
              f"- Price Δ last 3/5/10 rounds: {p.get('price_d3')}/{p.get('price_d5')}/{p.get('price_d10')} · since start {p.get('since_start')} · projected change after round {p.get('proj_price_change')} · PIR≥price {p.get('pir_ge_price')}",
              f"- Schedule: {fmt_p(p, H)}",
              f"- Window score (site proj, weighted): {p.get('score')} · P10–P90 this round {p.get('p10')}–{p.get('p90')} · win prob {p.get('pwin')}"]
        if p.get("vx"):
            v = p["vx"]
            L.append(f"- When {', '.join(v.get('absent_names', []))} out ({v.get('n')} games): minutes {v.get('dm'):+}, PIR {v.get('dp'):+} (avg min then {v.get('m')})")
        L.append(f"- Domestic league: {TEAMS.get(p.get('t'), ('', '?'))[1]}")
        for n in news_by.get(p.get("c"), [])[:5]:
            L.append(f"  - news [{n['kind']}] {n['ts'][:10]}: {n['title'] or ''} {json.dumps(n['detail'], ensure_ascii=False) if n.get('detail') else ''}")

    L += ["", "## Mechanical swap baseline (same position, budget & 6-per-club feasible — NOT advice)"]
    for s in dg["swaps"]:
        opts = "; ".join(f"{o['name']} ({o['team']}, {o['price']}cr, score {o['score']}, gain {o['gain']:+}, own {o['owned']}%)" for o in s["options"][:4])
        L.append(f"- OUT {s['out']} (score {s['out_score']}): {opts}")

    for pos, sl in shortlists.items():
        L += ["", f"## {pos}s — market"]
        for label, lst in (("Top window score", sl["top"]), ("Cheap value (≤9 cr)", sl["value"]), ("Differentials (<5% owned)", sl["differentials"])):
            L.append(f"**{label}:**")
            for p in lst:
                L.append(f"- {p['name']} ({p.get('t')}) {p.get('price')}cr own {p.get('owned')}% · {p.get('status')} · Δ3 {p.get('price_d3')} · L3/L10 {p.get('l3')}/{p.get('l10')} · min {p.get('m3')}/{p.get('m10')} · {fmt_p(p, H)} · Δprice {p.get('proj_price_change')} · sig {p.get('sig')}")

    L += ["", f"## Round {dg['target_round']} game previews (site model)"]
    for k, v in dg["previews"].items():
        L += [f"### {k}", v["summary"], v["key_matchups"], v["url"], ""]

    L += ["", "## Coaches (current-round projections)"]
    for c in sorted(coaches.values(), key=lambda c: -c.get("exp", 0)):
        L.append(f"- {c['t']} {c['price']}cr exp {c['exp']} · win {c.get('pwin')} · vs {c.get('opp')} {'home' if c.get('home') else 'away'}")

    L += ["", "## Domestic-league alerts (ABA only from site; other leagues → research)"]
    for it in news.get("items", []):
        if str(it.get("kind", "")).startswith("dom_"):
            L.append(f"- {it['n']} ({it['t']}) {it['kind']} {json.dumps(it.get('detail'), ensure_ascii=False)}")
    L += ["", "## Minutes / role changes flagged by site model"]
    for it in news.get("items", []):
        if it.get("kind") in ("minutes_up", "minutes_drop", "return", "transfer", "projection_change"):
            L.append(f"- {it['n']} ({it['t']}) {it['kind']}: {it.get('before')} → {it.get('after')}")
    L += ["", "## Press items"]
    for it in news.get("items", []):
        if it.get("kind") == "press":
            L.append(f"- {it['ts'][:10]} {it['n']} [{it.get('tag')}] {it['title']} — {it.get('link')}")
    i = inj_txt.find("Latest updates")
    L += ["", "## Injury report (raw)", inj_txt[i:i + 6000] if i > 0 else inj_txt[:6000]]
    (OUT / "digest.md").write_text("\n".join(str(x) for x in L), encoding="utf-8")


if __name__ == "__main__":
    main()
