"""Domestic-league box scores straight from league sites (no web search needed).

Covered: Liga ACB (Spain: BAR, BAS, MAD, PAM) and Greek Basket League (OLY, PAN).
The ABA League (PAR, RED, DUB) is already covered by basketballstats.eu's own alerts.
Other leagues stay with the analyst's web research (see PLAYBOOK.md).

Each player record: league, date, opponent text, minutes, points, rebounds, assists, league PIR, url.
Never raises: a league that fails just returns nothing and is listed in `errors`.
"""
from __future__ import annotations

import html
import re
import unicodedata
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept-Language": "en,es;q=0.8,el;q=0.6"}
ACB_TEAMS = {"barca": "BAR", "kosner-baskonia": "BAS", "baskonia": "BAS", "real-madrid": "MAD", "valencia-basket": "PAM"}
GBL_TEAMS = {"ΟΛΥΜΠΙΑΚΟΣ": "OLY", "ΠΑΝΑΘΗΝΑΪΚΟΣ": "PAN"}
ES_MONTHS = {m: i for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                                         "septiembre", "octubre", "noviembre", "diciembre"], 1)}
GR_MONTHS = {"Ιαν": 1, "Φεβ": 2, "Μαρ": 3, "Απρ": 4, "Μαΐ": 5, "Μαι": 5, "Ιουν": 6, "Ιουλ": 7, "Αυγ": 8, "Σεπ": 9,
             "Οκτ": 10, "Νοε": 11, "Δεκ": 12}


def _get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _text(page):
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s).split()


def _match(raw_name, team, pool_by_team):
    """Box-score name ('A. Feliz', 'Luwawu-Cabarrot', 'BARTLEY FRANK') -> pool player of that team."""
    toks = [t for t in _norm(raw_name) if len(t) > 1]
    best = None
    for p in pool_by_team.get(team, []):
        pn = _norm(p["name"])
        hits = sum(1 for t in toks if t in pn)
        if hits and (not best or hits > best[0]):
            best = (hits, p)
    return best[1]["name"] if best else None


def _mmss(s):
    parts = [int(x) for x in s.split(":")]
    return round(parts[-2] + parts[-1] / 60 + (parts[0] * 60 if len(parts) == 3 else 0), 1)


# ---------------------------------------------------------------- Liga ACB
ACB_JSON = re.compile(
    r'"firstName":"([^"]*)","lastName":"([^"]*)"[^{}]*\},"onCourt":(?:true|false),"playTime":"(\d+:\d+)","isStarted":(true|false),'
    r'"points":(-?\d+),[^{}]*?"assists":(\d+),[^{}]*?"totalRebounds":(\d+),[^{}]*?"plusMinus":(-?\d+),"rating":(-?\d+)')
ACB_ROW = re.compile(r"(\d{1,2}) ([^\d:]{2,40}?) (\d{2}:\d{2}) (\d+) (\d+) / (\d+) \d+% (\d+) / (\d+) \d+% (\d+) / (\d+) \d+% "
                     r"(\d+) (\d+) (\d+) (\d+)((?: -?\d+){7}) (-?\d+) (-?\d+)(?= \d{1,2} [^\d]| Equipo)")


def acb(pool_by_team, year):
    out = {}
    page = _get("https://www.acb.com/resultados-clasificacion/ver")
    pat = r'(https://live\.acb\.com/partidos/([a-z0-9-]+)-vs-([a-z0-9-]+)-(\d+))/resumen'
    links = set(re.findall(pat, page))
    # each EuroLeague club's own page: its latest finished game (higher id = newer)
    for slug, n in set(re.findall(r'href="/es/liga/equipos/([a-z0-9-]+)-(\d+)"', page)):
        if slug not in ACB_TEAMS:
            continue
        try:
            tl = [x for x in set(re.findall(pat, _get(f"https://www.acb.com/es/liga/equipos/{slug}-{n}"))) if slug in (x[1], x[2])]
        except Exception:
            continue
        if tl:
            links.add(max(tl, key=lambda x: int(x[3])))
    latest = {}
    for x in links:  # keep only each club's newest finished game
        for side in (x[1], x[2]):
            c = ACB_TEAMS.get(side)
            if c and (c not in latest or int(x[3]) > int(latest[c][3])):
                latest[c] = x
    for base, home, away, _ in sorted(set(latest.values())):
        teams = [ACB_TEAMS.get(home), ACB_TEAMS.get(away)]
        if not any(teams):
            continue
        try:
            st = _get(base + "/estadisticas")
        except Exception:
            continue
        t = _text(st)
        d = re.search(r"(20\d\d-\d\d-\d\d)T\d\d:\d\d", st)
        date = d.group(1) if d else None
        # every player's full-game line sits in the page's embedded data (first occurrence = whole game)
        data = st.replace('\\"', '"')
        seen = set()
        for m in ACB_JSON.finditer(data):
            raw = f"{m.group(1)} {m.group(2)}"
            if raw in seen:
                continue
            seen.add(raw)
            for side, code in enumerate(teams):
                if not code:
                    continue
                name = _match(raw, code, pool_by_team)
                if not name:
                    continue
                opp = (away if side == 0 else home).replace("-", " ").title()
                out[name] = {"league": "ACB", "date": date, "opp": ("vs " if side == 0 else "@ ") + opp,
                             "min": _mmss(m.group(3)), "start": m.group(4) == "true", "pts": int(m.group(5)),
                             "reb": int(m.group(7)), "ast": int(m.group(6)), "pir": int(m.group(9)),
                             "url": base + "/estadisticas"}
                break
    return out


# ---------------------------------------------------------------- Greek Basket League (ESAKE)
GBL_ROW = re.compile(r"#\d+ ([A-Z’'\-\. ]{3,40}?) (\d+) (\d+) - (\d+) (\d+) - (\d+) (\d+) - (\d+) (\d+) (\d+) (\d+) (\d+) "
                     r"(\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d{2}:\d{2}:\d{2}) (-?\d+)")


def gbl(pool_by_team, year):
    out = {}
    page = _get("https://www.esake.gr/el/action/EsakeResults")
    cur = re.search(r"(\d{2})η Αγωνιστική", _text(page))
    if cur and int(cur.group(1)) > 1:  # also the previous round: clubs often play their league game later
        page += _get(f"https://www.esake.gr/el/action/EsakeResults?series={int(cur.group(1)) - 1:02d}")
    for gid in dict.fromkeys(re.findall(r"EsakegameView\?idgame=([0-9A-F]+)&mode=", page)):
        url = f"https://www.esake.gr/el/action/EsakegameView?idgame={gid}&mode=3"
        try:
            t = _text(_get(url))
        except Exception:
            continue
        sc = re.search(r"(\S+(?: \S+){0,4}) (\d+) vs (\d+) (\S+(?: \S+){0,4}?) [A-Z]", t)
        d = re.search(r"(\d{1,2}) (" + "|".join(GR_MONTHS) + r")\b", t)
        date = f"{year if GR_MONTHS[d.group(2)] >= 8 else year + 1}-{GR_MONTHS[d.group(2)]:02d}-{int(d.group(1)):02d}" if d else None
        sections = re.split(r"ΑΝΑΛΥΤΙΚΑ ΣΤΑΤΙΣΤΙΚΑ| (?=\S+(?: \S+){0,3} SHOTS REBOUNDS ΠΑΙΚΤΗΣ)", t)[1:]
        for sec in sections:
            code = next((c for k, c in GBL_TEAMS.items() if k in sec[:60]), None)
            if not code:
                continue
            for m in GBL_ROW.finditer(sec):
                name = _match(m.group(1), code, pool_by_team)
                if not name:
                    continue
                out[name] = {"league": "GBL", "date": date, "opp": "", "min": _mmss(m.group(19)), "pts": int(m.group(2)),
                             "reb": int(m.group(9)), "ast": int(m.group(12)), "pir": int(m.group(20)), "url": url}
    return out


def collect(pool, year):
    """pool: iterable of player dicts with name/t. Returns ({player name: last domestic game}, errors)."""
    by_team = {}
    for p in pool:
        by_team.setdefault(p.get("t"), []).append(p)
    res, errors = {}, []
    for name, fn in (("ACB", acb), ("GBL", gbl)):
        try:
            got = fn(by_team, year)
            for k, v in got.items():
                if k not in res or (v.get("date") or "") > (res[k].get("date") or ""):
                    res[k] = v
        except Exception as e:  # a broken league site must never stop the collector
            errors.append(f"{name}: {e}")
    return res, errors


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    pool = json.loads(open(sys.argv[1], encoding="utf-8").read())
    r, e = collect(pool, 2026)
    for k, v in sorted(r.items(), key=lambda x: x[1]["league"]):
        print(v["league"], v["date"], k, v["opp"], f"{v['min']}' {v['pts']}p {v['reb']}r {v['ast']}a PIR {v['pir']}")
    print(len(r), "players", e)
