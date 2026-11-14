"""Render a daily report: reports/<date>.json (decisions + reasoning written by the analyst)
-> reports/<date>.html and reports/latest.html (court-style, 3 squad variations).

The analyst only writes names, slots and text. Every number shown next to a player
(opponent, game day/turn, projection, price, form) is filled in here from
data/<date>/pool.json and digest.json, so the report can't drift from the data.

Usage: python render.py [YYYY-MM-DD]
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent

TEAM = {  # code: (short label, full name, primary, secondary)
    "ASV": ("ASV", "ASVEL", "#0f5132", "#ffffff"), "BAR": ("BAR", "Barcelona", "#a50044", "#004d98"),
    "BAS": ("BAS", "Baskonia", "#0b2d6b", "#c8102e"), "BES": ("BJK", "Besiktas", "#151515", "#ffffff"),
    "DUB": ("DUB", "Dubai", "#1c1c1c", "#c9a24a"), "HTA": ("HTA", "Hapoel Tel Aviv", "#d1001f", "#ffffff"),
    "IST": ("EFS", "Anadolu Efes", "#0c2c84", "#ffffff"), "MAD": ("RMB", "Real Madrid", "#f4f4f4", "#4b2a8a"),
    "MIL": ("MIL", "Olimpia Milano", "#f4f4f4", "#e4002b"), "MUN": ("MUN", "Bayern", "#dc052d", "#0066b2"),
    "OLY": ("OLY", "Olympiacos", "#e0001b", "#ffffff"), "PAM": ("VBC", "Valencia", "#f47b20", "#1a1a1a"),
    "PAN": ("PAO", "Panathinaikos", "#007a3d", "#ffffff"), "PAR": ("PAR", "Partizan", "#151515", "#e5e5e5"),
    "PRS": ("PBB", "Paris", "#151515", "#e5004f"), "RED": ("CZV", "Crvena Zvezda", "#c8102e", "#ffffff"),
    "TEL": ("MTA", "Maccabi Tel Aviv", "#ffd200", "#003a8c"), "ULK": ("FBB", "Fenerbahce", "#001a57", "#ffd200"),
    "VIR": ("VIR", "Virtus Bologna", "#151515", "#ffffff"), "ZAL": ("ZAL", "Zalgiris", "#00843d", "#ffffff"),
}
NAME2CODE = {v[1].lower(): k for k, v in TEAM.items()}
NAME2CODE.update({"real madrid": "MAD", "dubai basketball": "DUB", "valencia basket": "PAM", "bayern munich": "MUN",
                  "paris basketball": "PRS", "fenerbahce": "ULK", "zalgiris": "ZAL"})
_CP = ROOT / "data" / "coaches.json"
COACHES = json.loads(_CP.read_text(encoding="utf-8")) if _CP.exists() else {}  # code: head coach name
LABEL2CODE = {v[0]: k for k, v in TEAM.items()}
POSL = {"Guard": "G", "Forward": "F", "Center": "C"}
FORMATIONS = {"2-2-1": (2, 2, 1), "1-2-2": (1, 2, 2), "2-1-2": (2, 1, 2), "1-3-1": (1, 3, 1), "3-1-1": (3, 1, 1)}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", "", s).strip()


def esc(s):
    return html.escape(str(s if s is not None else ""))


# ------------------------------------------------------------------ data
class Data:
    def __init__(self, date):
        d = ROOT / "data" / date
        self.digest = json.loads((d / "digest.json").read_text(encoding="utf-8"))
        self.pool = json.loads((d / "pool.json").read_text(encoding="utf-8"))
        self.target = self.digest["target_round"]
        self.by = {}
        for p in self.pool:
            self.by.setdefault(norm(p["name"]), p)
        # per-team game info for the target round, from previews
        self.games, self.preview_proj = {}, {}
        dates = sorted({v.get("date") for v in self.digest["previews"].values() if v.get("date")},
                       key=lambda x: (dt.datetime.strptime(x, "%d %b").month < 8, dt.datetime.strptime(x, "%d %b")))
        for v in self.digest["previews"].values():
            h, a = v.get("home"), v.get("away")
            if not (h and a):
                continue
            turn = dates.index(v["date"]) + 1 if v.get("date") in dates else None
            sc = v.get("score") or [0, 0]
            for me, opp, home in ((h, a, True), (a, h, False)):
                pw = (v["fav_pct"] if v.get("fav_code") == me else 100 - (v.get("fav_pct") or 50)) / 100
                margin = (sc[0] - sc[1]) * (1 if home else -1)
                self.games[me] = {"opp": opp, "home": home, "turn": turn, "date": v.get("date"),
                                  "time": v.get("time"), "pwin": pw, "margin": margin}
            for n, val in (v.get("player_proj") or {}).items():
                self.preview_proj[norm(n)] = val

    def player(self, name):
        k = norm(name)
        p = self.by.get(k)
        if not p:
            hit = difflib.get_close_matches(k, list(self.by), n=1, cutoff=0.7)
            p = self.by[hit[0]] if hit else None
        if not p:
            last = [q for kk, q in self.by.items() if kk.split()[-1:] == k.split()[-1:]]
            p = last[0] if len(last) == 1 else None
        if not p:
            return {"name": name, "missing": True, "proj": 0, "pos": "?", "t": None}
        out = dict(p)
        rp = next((e for e in (p.get("nx") or []) + (p.get("nxe") or []) if e[0] == self.target), None)
        nxt = next((e for e in (p.get("nx") or []) + (p.get("nxe") or []) if e[0] == self.target + 1), None)
        if rp:
            out["proj"] = rp[3]
        elif norm(p["name"]) in self.preview_proj:
            out["proj"] = self.preview_proj[norm(p["name"])]
        else:
            m = re.search(r"([\d.]+) PIR per game", p.get("season_summary") or "")
            out["proj"] = float(m.group(1)) if m else 0.0
        if p.get("status") in ("injured", "out"):
            out["proj"] = 0.0
        elif p.get("status") == "doubtful":
            out["proj"] = round(out["proj"] * 0.5, 1)
        out["proj_next"] = nxt[3] if nxt else None
        g = self.games.get(p.get("t"), {})
        out["game"] = g
        out["hot"] = (p.get("l3") or 0) >= (p.get("l10") or 0) + 3 and (p.get("l3") or 0) >= 12
        return out


def coach_exp(game):
    """Expected coach points from projected margin (normal, sd 12) with Fantasy brackets."""
    if not game:
        return 0.0
    mu, sd = game.get("margin", 0), 12.0
    cdf = lambda x: 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))
    bands = [(-1e9, -20.5, -20), (-20.5, -10.5, -10), (-10.5, 0, -5), (0, 10.5, 10), (10.5, 20.5, 20), (20.5, 1e9, 25)]
    return round(sum((cdf(b) - cdf(a)) * pts for a, b, pts in bands), 1)


def fantasy(p, mult=1.0):
    pw = (p.get("game") or {}).get("pwin", 0.5)
    return (p.get("proj") or 0) * (1 + 0.1 * pw) * mult




# ------------------------------------------------------------------ html bits
def jersey(code, label=None, cls="jsy"):
    short, _, c1, c2 = TEAM.get(code, ("?", "", "#777777", "#ffffff"))
    r, g, b = (int(c1[i:i + 2], 16) for i in (1, 3, 5))
    txt = "#14152a" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#ffffff"
    return f"""<svg class="{cls}" viewBox="0 0 60 64" aria-hidden="true">
<path d="M14 2h9c1 5 4 8 7 8s6-3 7-8h9l1 13c-4 3-6 7-6 12v35H19V27c0-5-2-9-6-12z" fill="{c1}" stroke="{c2}" stroke-width="2.5"/>
<text x="30" y="40" text-anchor="middle" font-size="11" font-weight="800" fill="{txt}" font-family="Fira Sans,sans-serif">{esc(label or short)}</text></svg>"""


def opp_txt(g, long=False):
    if not g:
        return "–"
    o = TEAM.get(g["opp"], (g["opp"],))[0]
    day = f"T{g.get('turn') or '?'}"
    return f"{day} {'vs' if g.get('home') else '@'} {o}"


def surname(n):
    return n.split(" ", 1)[-1] if " " in n else n


def card(p, captain=False, vice=False, small=False):
    g = p.get("game") or {}
    badge = '<span class="cap">C</span>' if captain else ('<span class="cap vc">V</span>' if vice else "")
    fire = '<span class="fire" title="Hot: last 3 games well above season level">🔥</span>' if p.get("hot") else ""
    warn = ""
    if p.get("status") in ("injured", "out", "doubtful"):
        warn = f'<span class="stat {p["status"]}">{esc(p["status"]).upper()}</span>'
    if p.get("missing"):
        warn = '<span class="stat injured">NOT FOUND</span>'
    dp = f' data-p="{esc(p.get("slug") or p.get("s"))}" role="button" tabindex="0"' if (p.get("slug") or p.get("s")) else ""
    return f"""<div class="pl{' sm' if small else ''}"{dp}>
  <div class="shirt">{jersey(p.get('t'))}{badge}{fire}</div>
  <div class="tag"><div class="nm"><i>{POSL.get(p.get('pos'), '?')}</i>{esc(surname(p['name']))}</div>
  <div class="gm"><span>{esc(opp_txt(g))}</span><b>{p.get('proj', 0):.1f}</b></div></div>{warn}
  <div class="meta">{p.get('price') or '–'} cr · form {p.get('l3') if p.get('l3') is not None else '–'}</div>
</div>"""


def court(starters, formation, captain, vice):
    by = {"Guard": [], "Forward": [], "Center": []}
    for p in starters:
        by.setdefault(p.get("pos"), []).append(p)
    rows = []
    for pos in ("Center", "Forward", "Guard"):
        cells = "".join(card(p, captain=norm(p["name"]) == norm(captain), vice=norm(p["name"]) == norm(vice))
                        for p in by.get(pos, []))
        rows.append(f'<div class="row">{cells}</div>')
    return f"""<div class="court"><svg class="lines" viewBox="0 0 400 300" preserveAspectRatio="none" aria-hidden="true">
<path d="M120 0v95h160V0M150 95a50 50 0 0 0 100 0M40 0v40a160 160 0 0 0 320 0V0" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="2"/>
<circle cx="200" cy="300" r="45" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="2"/></svg>
<div class="frm">{esc(formation)}</div>{''.join(rows)}</div>"""


def squad_points(starters, sixth, bench, captain, coach_game):
    pts = 0.0
    for p in starters:
        pts += fantasy(p, 2.0 if norm(p["name"]) == norm(captain) else 1.0)
    if sixth:
        pts += fantasy(sixth)
    pts += sum(fantasy(p, 0.5) for p in bench)
    cpts = coach_exp(coach_game)
    return round(pts + cpts, 1), cpts


def validate(players, bank, n_trades):
    issues = []
    cnt = {"Guard": 0, "Forward": 0, "Center": 0}
    clubs = {}
    for p in players:
        cnt[p.get("pos")] = cnt.get(p.get("pos"), 0) + 1
        clubs[p.get("t")] = clubs.get(p.get("t"), 0) + 1
        if p.get("missing"):
            issues.append(f"{p['name']}: not found in data")
    if (cnt["Guard"], cnt["Forward"], cnt["Center"]) != (4, 4, 2):
        issues.append(f"positions {cnt['Guard']}G/{cnt['Forward']}F/{cnt['Center']}C (need 4/4/2)")
    over = [k for k, v in clubs.items() if v > 6]
    if over:
        issues.append(f"more than 6 from {over}")
    if bank is not None and bank < -0.05:
        issues.append(f"bank {bank:.1f} < 0")
    if n_trades < 4:
        issues.append(f"uses only {n_trades} of 4 trades")
    return issues


def coach_label(code, fallback=""):
    """'Bartzokas (OLY)': coach surname plus short club label."""
    lab = TEAM.get(code, (code or fallback, fallback))[0]
    nm = COACHES.get(code)
    return f"{nm.split()[-1]} ({lab})" if nm else (TEAM.get(code, ('', fallback))[1] or fallback)


def coach_code_of(name):
    m = re.search(r"\(([A-Z]{3})\)", name or "")
    if m and (m.group(1) in TEAM or m.group(1) in LABEL2CODE):
        return m.group(1) if m.group(1) in TEAM else LABEL2CODE[m.group(1)]
    k = norm(name)
    for code, nm in COACHES.items():
        if k and norm(nm.split()[-1]) in k.split():
            return code
    return NAME2CODE.get(k) or next((c for n, c in NAME2CODE.items() if k and (k in n or n in k)), (name or "")[:3].upper())


def mini(name, code, price, kind, slug=None, team=None):
    attr = (f' data-p="{esc(slug)}" role="button" tabindex="0"' if slug else
            f' data-t="{esc(team)}" role="button" tabindex="0"' if team else "")
    return f"""<div class="mini {kind}"{attr}>{jersey(code, cls='mj')}<div><span class="lbl">{'OUT' if kind == 'out' else 'IN'}</span>
<b>{esc(name)}</b>{f' <small>{esc(price)}</small>' if price else ''}</div></div>"""


def split_name(s):
    m = re.match(r"\s*(?:Coach\s+)?(.+?)\s*\(([\d.]+)\)\s*$", s)
    if s.strip().startswith("Coach"):
        nm, pr = (m.group(1), m.group(2)) if m else (s.replace("Coach ", ""), "")
        return coach_label(coach_code_of(nm), nm), pr
    return (m.group(1), m.group(2)) if m else (s.replace("Coach ", ""), "")


# ------------------------------------------------------------------ popups + schedule tab
def slug_of(p):
    return p.get("slug") or p.get("s")


def slug_or_team(raw, name, code, D):
    if raw.strip().startswith("Coach"):
        return None, code
    p = D.player(name)
    return (None if p.get("missing") else slug_of(p)), None


def team_code(name):
    k = norm(name)
    if not k:
        return None
    hit = NAME2CODE.get(k) or next((c for n, c in NAME2CODE.items() if k in n or n in k), None)
    return hit or (name.upper() if name.upper() in TEAM else LABEL2CODE.get(name.upper()))


def team_attr(name):
    c = team_code(name)
    return f' data-t="{esc(c)}" role="button" tabindex="0"' if c else ""


def pc_attr(name, D):
    p = D.player(name)
    return "" if p.get("missing") else f' data-p="{esc(slug_of(p))}" role="button" tabindex="0"'


MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def day_key(day):
    """'Tue 17 Nov' -> sortable (season half, month, day)."""
    try:
        _, d, m = day.split()[:3]
        mo = MONTHS.get(m[:3], 0)
        return (0 if mo >= 8 else 1, mo, int(d))
    except Exception:
        return (9, 0, 0)


def load_schedule(date):
    f = ROOT / "data" / date / "schedule.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"games": [], "standings": []}


def leg_rounds(D):
    cal = D.digest.get("calendar", {})
    tr = D.target
    leg = [tr] + ([tr + 1] if (cal.get(str(tr)) or {}).get("double_week") and str(tr + 1) in cal else [])
    return leg, [leg[-1] + 1, leg[-1] + 2]


def load_firebase():
    f = ROOT / "firebase-web.json"
    try:
        c = json.loads(f.read_text(encoding="utf-8"))
        return {"config": c.get("config") or {}, "owner": c.get("owner_uid") or ""}
    except Exception:
        return {"config": {}, "owner": ""}


def page_data(D, sched, team, names, rv):
    """JSON blobs the page reads: players shown on the page (full stats), every player (light list for chips and the
    team editor), all teams with their schedule, the report's squad and plan, and the Firebase web config."""
    P = {}
    for n in names:
        p = D.player(n)
        if not p.get("missing") and slug_of(p):
            P[slug_of(p)] = player_blob(D, p)
    PL = {slug_of(p): [p["name"], p.get("t"), p.get("pos"), p.get("price")] for p in D.pool if slug_of(p) and p.get("t")}
    stand = {r[1]: r for r in sched.get("standings", [])}
    T = {}
    for code, (short, full, c1, c2) in TEAM.items():
        gs = []
        for g in sched.get("games", []):
            if code not in (g["h"], g["a"]):
                continue
            home = g["h"] == code
            res = g.get("res")
            gs.append([g["rd"], g["day"], g["time"], g["a"] if home else g["h"], 1 if home else 0,
                       (res if home else res[::-1]) if res else None,
                       (g["pw"] if home else round(1 - g["pw"], 2)) if g.get("pw") is not None else None,
                       (g["proj"] if home else g["proj"][::-1]) if g.get("proj") else None])
        T[code] = {"n": full, "s": short, "c1": c1, "coach": COACHES.get(code), "st": stand.get(code), "g": gs}
    sl = lambda n: slug_of(D.player(n)) if n and not D.player(n).get("missing") else None
    lu = team.get("lineup") or {}
    leg, later = leg_rounds(D)
    trades = rv.get("trades", [])
    M = {"name": "Report team", "players": [s for s in (sl(n) for n in team.get("players", [])) if s],
         "starters": [s for s in (sl(n) for n in lu.get("starters", [])) if s], "sixth": sl(lu.get("sixth")),
         "bench": [s for s in (sl(n) for n in lu.get("bench", [])) if s],
         "cap": sl(team.get("captain")), "coach": coach_code_of(team.get("coach", "")) if team.get("coach") else None,
         "bank": team.get("bank"), "target_pts": team.get("target_points", 200),
         "a_in": [s for s in (sl(split_name(t["in"])[0]) for t in trades if not t["in"].strip().startswith("Coach")) if s],
         "a_out": [s for s in (sl(split_name(t["out"])[0]) for t in trades if not t["out"].strip().startswith("Coach")) if s],
         "a_coach": coach_code_of(rv.get("coach", "")) if rv.get("coach") else None,
         "target": D.target, "leg": leg, "later": later}
    FB = load_firebase()

    def dump(o):
        return json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return "".join(f'<script type="application/json" id="{k}">{dump(v)}</script>'
                   for k, v in (("dP", P), ("dPL", PL), ("dT", T), ("dM", M), ("dFB", FB)))


def player_blob(D, p):
    keys = ("name", "t", "pos", "price", "owned", "status", "l3", "l5", "l10", "m3", "m10", "p10", "p90",
            "since_start", "proj_price_change", "price_d3", "sig", "nx", "nxe", "bio", "pirpm", "start", "proj")
    out = {k: p.get(k) for k in keys if p.get(k) is not None}
    out["gl"] = p.get("gl") or []
    return out


def write_players_file(D):
    """Full stats for every player, loaded by the page only when someone opens a player it doesn't embed."""
    allp = {}
    for p in D.pool:
        if slug_of(p) and p.get("t"):
            allp[slug_of(p)] = player_blob(D, D.player(p["name"]))
    out = ROOT / "site" / "players.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(allp, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def sched_tab(D, sched):
    """Static schedule; the page's script marks the active team's players on it."""
    cal = D.digest.get("calendar", {})
    leg, later = leg_rounds(D)
    games = sorted(sched.get("games", []), key=lambda g: (g["rd"], day_key(g["day"]), g["time"]))
    if not games:
        return "<section><h2>📅 Schedule</h2><p class='sub'>No schedule data for this report.</p></section>"

    def tbtn(code):
        short, full, c1, _ = TEAM.get(code, (code, code, "#777777", "#ffffff"))
        return (f'<button class="tm" data-t="{esc(code)}"><span class="dot" style="background:{c1}"></span>'
                f'<span class="tn">{esc(full)}</span><span class="ts">{esc(short)}</span><b class="cnt" hidden></b></button>')

    def game_row(g):
        pw = g.get("pw")
        if pw is not None:
            proj = " · " + "-".join(map(str, g["proj"])) if g.get("proj") else ""
            mid = (f'<div class="wp"><span style="width:{int(round(pw * 100))}%"></span></div>'
                   f'<small>{int(round(pw * 100))}% – {int(round((1 - pw) * 100))}%{proj}</small>')
        elif g.get("res"):
            mid = f'<b class="fin">{g["res"][0]}-{g["res"][1]}</b>'
        else:
            mid = "<small>–</small>"
        tv = f" · {esc(g['tv'])}" if g.get("tv") else ""
        return (f'<div class="sg" data-h="{esc(g["h"])}" data-a="{esc(g["a"])}"><div class="sgt">{esc(g["time"])}{tv}</div>'
                f'<div class="sgm">{tbtn(g["h"])}<div class="mid">{mid}</div>{tbtn(g["a"])}</div><div class="sgc" hidden></div></div>')

    def round_block(rd, open_=True):
        gs = [g for g in games if g["rd"] == rd]
        if not gs:
            return ""
        c = cal.get(str(rd)) or {}
        tags = ("<span class='pill'>Double week</span>" if c.get("double_week") else "")
        if c.get("unlimited_before"):
            tags += f"<span class='pill hotp'>Unlimited trades before · {esc(c.get('unlimited_window') or '')}</span>"
        days = []
        for day in sorted({g["day"] for g in gs}, key=day_key):
            dg = [g for g in gs if g["day"] == day]
            days.append(f'<div class="sday"><h4>{esc(day)}<small>{len(dg)} games · <span class="dc" data-r="{rd}" '
                        f'data-day="{esc(day)}"></span></small></h4>' + "".join(game_row(g) for g in dg) + "</div>")
        head = f'<span class="rh">Round {rd}</span>{tags}'
        if open_:
            return f'<div class="sround"><h3>{head}</h3>{"".join(days)}</div>'
        return f'<details class="sround"><summary>{head}</summary>{"".join(days)}</details>'

    leg_name = f"Round {leg[0]}" + (f"–{leg[-1]} double week" if len(leg) > 1 else "")
    head = "".join(f"<th>R{r}</th>" for r in leg)
    return f"""<section><h2>📅 Next leg: {esc(leg_name)}</h2>
<div class="showing">Showing <b id="showName">Report team</b> <button class="lnk2" data-edit>Use my team</button></div>
<p class="sub">Marked on every game: <span class="pchip demo"><i>F</i>player</span>
<span class="pchip pcap demo"><i>F</i>captain</span> <span class="pchip out demo planmark"><i>G</i>sold in Variation A</span>
<span class="pchip plan demo planmark"><i>C</i>A+ bought in Variation A</span>. Tap any team or player for details. Times are Athens time (CET+1).</p>
<div class="card"><h4 class="ovh">Squad games</h4><div class="turns" id="turns"></div>
<div class="tblw"><table class="ov"><thead><tr><th>Player</th><th>Team</th>{head}</tr></thead><tbody id="ovb"></tbody></table></div>
<p class="sub" style="margin:8px 0 0">Win chance in green when 65%+, red when 35% or less.</p></div>
{''.join(round_block(r) for r in leg)}
{''.join(round_block(r, open_=False) for r in later)}
</section>"""


# ------------------------------------------------------------------ page
CSS = """
:root{color-scheme:dark;--bg:#0c0d19;--card:#171830;--card2:#1f2042;--ink:#eeedf7;--muted:#a9a7c4;--faint:#77759a;
--line:rgba(255,255,255,.09);--orange:#ff7a1a;--lime:#d4ff3a;--green:#3ddc84;--red:#ff5d6c;--amber:#ffb02e;--blue:#6aa7ff;
--wood1:#b9834c;--wood2:#a87543}
*{box-sizing:border-box}
body{margin:0;font:16px/1.6 'Fira Sans',system-ui,sans-serif;color:var(--ink);background:
radial-gradient(900px 380px at 50% -140px,rgba(255,122,26,.45),transparent 70%),var(--bg);min-height:100vh}
h1,h2,h3,h4,.cond{font-family:'Fira Sans Extra Condensed','Fira Sans',sans-serif;letter-spacing:.2px;line-height:1.15}
a{color:var(--blue)}
.wrap{max-width:1120px;margin:0 auto;padding:20px 16px 40px}
.kick{color:var(--orange);font:700 14px 'Fira Sans Extra Condensed';letter-spacing:2px;text-transform:uppercase}
h1{font-size:46px;margin:2px 0 12px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:14px;color:var(--muted)}
.chip b{color:var(--ink);font-weight:600}.chip.hot{border-color:rgba(255,122,26,.6);color:#ffd2b0}.chip.hot b{color:#fff}
section{margin-top:28px}
h2{font-size:28px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px}
.plan{border-left:4px solid var(--orange);background:linear-gradient(90deg,rgba(255,122,26,.10),transparent 60%),var(--card)}
.plan .lead{font-size:18px;margin:0 0 16px;color:#fff}
.steps{display:grid;gap:12px;counter-reset:s;margin:0;padding:0;list-style:none}
.steps li{display:grid;grid-template-columns:34px 1fr;gap:12px;align-items:start}
.steps li::before{counter-increment:s;content:counter(s);width:34px;height:34px;border-radius:50%;background:var(--orange);color:#140b02;
font:800 18px/34px 'Fira Sans Extra Condensed';text-align:center}
.steps h4{margin:4px 0 6px;font-size:19px}
.tl{display:grid;gap:6px}.tl div{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:15px}
.o{color:var(--red)}.n{color:var(--green)}.cr{margin-left:auto;color:var(--faint);font-size:13px;font-variant-numeric:tabular-nums}
.lineup{font-size:15px;color:var(--muted)}.lineup b{color:var(--ink);font-weight:600}
.delta{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;margin-top:14px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:15px}
.delta strong{font:800 30px 'Fira Sans Extra Condensed';color:var(--lime)}
.fix{margin-top:12px;font-size:15px;color:var(--muted)}.fix b{color:var(--amber)}
.tabs{position:sticky;top:0;z-index:5;display:flex;gap:8px;overflow-x:auto;padding:10px 0;background:linear-gradient(var(--bg) 75%,transparent)}
.tab{flex:0 0 auto;border:1px solid var(--line);cursor:pointer;border-radius:12px;padding:8px 14px;text-align:left;
background:var(--card);color:var(--muted);font:600 17px 'Fira Sans Extra Condensed'}
.tab small{display:block;font:500 12px 'Fira Sans';color:var(--faint)}
.tab[aria-selected=true]{background:var(--card2);color:#fff;border-color:var(--orange);box-shadow:inset 0 -3px 0 var(--orange)}
.var{display:none}.var.on{display:block}
.vhead{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:6px 0 4px}.vhead h3{font-size:30px;margin:0}
.pill{font:700 11px 'Fira Sans';letter-spacing:.6px;text-transform:uppercase;padding:3px 9px;border-radius:999px;background:var(--card2);color:var(--muted)}
.pill.rec{background:var(--lime);color:#14152a}.pill.Low{color:var(--green)}.pill.Medium{color:var(--amber)}.pill.High{color:var(--red)}
.tagline{color:var(--muted);margin:0 0 14px;font-size:16px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.kpi span{display:block;font-size:12px;color:var(--faint);text-transform:uppercase;letter-spacing:.6px}
.kpi b{font:700 24px 'Fira Sans Extra Condensed';color:#fff}.kpi b.l{color:var(--lime)}
@media(max-width:620px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:38px}}
.grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:18px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:minmax(0,1fr)}}
.court{position:relative;border-radius:18px;padding:36px 6px 22px;overflow:hidden;
background:repeating-linear-gradient(90deg,var(--wood1) 0 38px,var(--wood2) 38px 40px,#b07b47 40px 78px,var(--wood2) 78px 80px);
box-shadow:inset 0 0 0 3px rgba(255,255,255,.18),inset 0 0 80px rgba(0,0,0,.35)}
.court .lines{position:absolute;inset:0;width:100%;height:100%}
.frm{position:absolute;top:10px;left:12px;background:rgba(12,13,25,.85);color:#fff;font:700 13px 'Fira Sans Extra Condensed';padding:2px 9px;border-radius:6px}
.row{position:relative;display:flex;justify-content:space-evenly;gap:6px;margin:4px 0 14px}
.pl{width:136px;max-width:31%;text-align:center;position:relative}
.shirt{position:relative;height:62px}.jsy{height:62px;filter:drop-shadow(0 4px 4px rgba(0,0,0,.35))}
.cap{position:absolute;right:12px;top:2px;background:var(--lime);color:#14152a;border-radius:50%;width:24px;height:24px;font:800 13px/24px 'Fira Sans Extra Condensed'}
.cap.vc{background:#fff}
.fire{position:absolute;left:12px;top:0;font-size:15px}
.tag{border-radius:8px;overflow:hidden;box-shadow:0 4px 10px rgba(0,0,0,.35)}
.nm{background:#14152a;color:#fff;font:600 16px 'Fira Sans Extra Condensed';padding:4px 6px 3px;border-bottom:3px solid var(--orange);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nm i{font-style:normal;color:var(--faint);margin-right:5px}
.gm{background:#f4f2ea;color:#14152a;font:600 15px 'Fira Sans Extra Condensed';padding:4px 6px;display:flex;justify-content:space-between;gap:4px;align-items:center}
.gm b{background:#14152a;color:var(--lime);border-radius:5px;padding:0 6px}
.meta{font-size:11.5px;color:rgba(255,255,255,.85);margin-top:4px;text-shadow:0 1px 2px rgba(0,0,0,.5)}
.stat{display:inline-block;margin-top:4px;font:700 11px 'Fira Sans';border-radius:4px;padding:1px 6px;color:#fff;background:var(--red)}
.stat.doubtful{background:var(--amber);color:#14152a}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 6px;margin-top:12px}
.box h4{margin:0 0 8px;text-align:center;color:var(--faint);font:600 13px 'Fira Sans';letter-spacing:1.5px;text-transform:uppercase}
.box .row{margin:0;flex-wrap:wrap;row-gap:12px}.box .pl{width:124px;max-width:23%}.box .meta{color:var(--faint);text-shadow:none}
@media(max-width:520px){.box .pl{max-width:46%}}
.side h4{font-size:20px;margin:18px 0 10px}.side h4:first-child{margin-top:0}
.trade{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:10px}
.trade .pair{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:8px;align-items:center}
.trade .arrow{color:var(--faint);font-size:18px}
.mini{display:flex;gap:8px;align-items:center;min-width:0}.mini b{font-weight:600;font-size:15px}.mini small{color:var(--faint)}
.mini .lbl{display:block;font:700 10px 'Fira Sans';letter-spacing:1px}.mini.out .lbl{color:var(--red)}.mini.in .lbl{color:var(--green)}
.mj{height:34px;flex:0 0 auto}
.trade .why{margin:8px 0 0;font-size:14.5px;color:var(--muted);line-height:1.5}
.trade .num{float:right;font:700 13px 'Fira Sans';color:var(--faint);font-variant-numeric:tabular-nums}
details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin-top:10px}
summary{cursor:pointer;font:600 18px 'Fira Sans Extra Condensed';list-style:none}
summary::-webkit-details-marker{display:none}summary::after{content:'＋';float:right;color:var(--faint)}
details[open] summary::after{content:'－'}
details ul{margin:10px 0 2px;padding-left:20px}details li{margin:6px 0;color:var(--muted)}details li::marker{color:var(--orange)}
.capline{font-size:15px;color:var(--muted)}.capline b{color:var(--lime)}
.issues{background:rgba(255,93,108,.12);border:1px solid rgba(255,93,108,.4);color:#ffc2c8;border-radius:10px;padding:8px 12px;margin-bottom:12px;font-size:14px}
.alerts{display:grid;gap:10px}
.alert{display:grid;grid-template-columns:12px 1fr;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:15.5px}
.alert::before{content:'';width:10px;height:10px;border-radius:50%;margin-top:7px;background:var(--amber)}
.alert.high::before{background:var(--red);box-shadow:0 0 0 4px rgba(255,93,108,.18)}
.rum{display:grid;gap:10px}
.r{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.r .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.r .k{font:700 10.5px 'Fira Sans';letter-spacing:.8px;text-transform:uppercase;border-radius:4px;padding:2px 7px;background:var(--card2);color:var(--muted)}
.r .k.confirmed{color:var(--green)}.r .k.report{color:var(--blue)}.r .k.rumour{color:var(--amber)}
.r .team{font-weight:600}.r p{margin:0;font-size:15px}
.r .imp{margin-top:6px;font-size:14.5px;color:var(--muted)}.r .imp::before{content:'→ ';color:var(--orange)}
.players{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}@media(max-width:760px){.players{grid-template-columns:minmax(0,1fr)}}
.pc{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.pc .top{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:4px}.pc b{font-size:16px}
.pc p{margin:0;font-size:14.5px;color:var(--muted)}
.v{font:700 11px 'Fira Sans';letter-spacing:.6px;padding:3px 8px;border-radius:999px;white-space:nowrap;background:var(--card2)}
.v.KEEP{color:var(--green)}.v.SELL{color:var(--red)}.v.WATCH{color:var(--amber)}.v.BUY{color:var(--blue)}
.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}@media(max-width:860px){.two{grid-template-columns:minmax(0,1fr)}}
.watch{margin:0;padding:0;list-style:none;display:grid;gap:10px}.watch li{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;font-size:15px;color:var(--muted)}
.watch b{color:var(--ink)}
footer{color:var(--faint);font-size:12.5px;text-align:center;margin-top:36px;line-height:1.5}

.ptabs{display:flex;gap:8px;margin-top:16px}
.ptab{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:999px;padding:7px 16px;font:600 17px 'Fira Sans Extra Condensed';cursor:pointer}
.ptab[aria-selected=true]{background:var(--orange);color:#140b02;border-color:var(--orange)}
.pg{display:none}.pg.on{display:block}
[data-p],[data-t]{cursor:pointer}
.pl[data-p]:hover .nm,.pl[data-t]:hover .nm{border-bottom-color:var(--lime)}
.pc[data-p]:hover,.watch li[data-p]:hover{border-color:rgba(255,122,26,.55)}
.mini[data-p]:hover b,.mini[data-t]:hover b,.r .team[data-t]:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--lime);outline-offset:2px}
.sub{color:var(--muted);font-size:14.5px;margin:0 0 12px;line-height:1.9}
.pchip{display:inline-flex;align-items:center;gap:5px;border:1px solid rgba(212,255,58,.45);background:rgba(212,255,58,.10);color:#fff;border-radius:999px;
padding:1px 9px 1px 3px;font:600 14.5px 'Fira Sans Extra Condensed';cursor:pointer;margin:2px 4px 2px 0;white-space:nowrap;vertical-align:middle}
.pchip i{font-style:normal;background:var(--lime);color:#14152a;border-radius:999px;font:800 10.5px/18px 'Fira Sans';min-width:18px;height:18px;text-align:center;padding:0 4px}
.pchip.pcap{border-color:var(--lime);box-shadow:0 0 0 2px rgba(212,255,58,.3)}.pchip.pcap::after{content:'C';font:800 10px/15px 'Fira Sans';background:#fff;color:#14152a;border-radius:50%;width:15px;text-align:center}
.pchip.out{opacity:.55;text-decoration:line-through}
.pchip.plan{border-style:dashed;border-color:var(--blue);background:rgba(106,167,255,.10)}.pchip.plan i{background:var(--blue)}
.pchip.hc{border-color:rgba(255,122,26,.6);background:rgba(255,122,26,.12)}.pchip.hc i{background:var(--orange)}
.pchip.hc.plan{border-color:var(--blue)}.pchip.demo{cursor:default}
.sround{margin-top:22px}.sround h3,.sround summary{font-size:24px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 6px}
details.sround summary{font-size:22px}
.pill.hotp{color:var(--orange)}
.sday h4{font:600 18px 'Fira Sans Extra Condensed';color:#fff;margin:14px 0 8px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
.sday h4 small{font:500 13px 'Fira Sans';color:var(--faint)}
.sg{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 12px;margin-bottom:8px}
details .sg{background:var(--card2)}
.sg.mine{border-color:rgba(212,255,58,.35)}
.sgt{font-size:12px;color:var(--faint);margin-bottom:2px}
.sgm{display:grid;grid-template-columns:minmax(0,1fr) minmax(96px,150px) minmax(0,1fr);gap:10px;align-items:center}
.sgm>.tm:last-child{justify-self:end}
.mid{text-align:center}.mid small{display:block;font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.wp{height:6px;border-radius:3px;background:rgba(255,255,255,.18);overflow:hidden}.wp span{display:block;height:100%;background:var(--orange)}
.fin{font:700 17px 'Fira Sans Extra Condensed'}
.sgc{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:4px}.sgc>div:last-child{text-align:right}
.tm{display:inline-flex;align-items:center;gap:7px;min-width:0;max-width:100%;background:none;border:0;color:var(--muted);font:600 17px 'Fira Sans Extra Condensed';cursor:pointer;padding:2px 0}
.tm .tn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tm .ts{display:none}
.tm.has{color:#fff}.tm:hover .tn,.tm:hover .ts{text-decoration:underline}
.dot{width:12px;height:12px;border-radius:50%;flex:0 0 auto;box-shadow:0 0 0 2px rgba(255,255,255,.25)}
.dot.big{width:18px;height:18px;display:inline-block;margin-right:8px;vertical-align:-1px}
.cnt{background:var(--lime);color:#14152a;border-radius:999px;font:800 11px/18px 'Fira Sans';min-width:18px;padding:0 4px;text-align:center}
@media(max-width:560px){.tm .tn{display:none}.tm .ts{display:inline}.sgm{grid-template-columns:minmax(0,1fr) 112px minmax(0,1fr)}}
.turns{font-size:14.5px;color:var(--muted);margin-bottom:8px}.turns b{color:var(--lime)}
.ovh{margin:0 0 4px;font:600 20px 'Fira Sans Extra Condensed'}
.tblw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th{font:600 11.5px 'Fira Sans';color:var(--faint);text-transform:uppercase;letter-spacing:.5px;text-align:center;padding:6px;border-bottom:1px solid var(--line)}
td{padding:6px;text-align:center;border-bottom:1px solid var(--line);white-space:nowrap}
td.l,th.l,.ov td:nth-child(-n+2),.ov th:nth-child(-n+2){text-align:left}
.fx,.tsm,.lnk{cursor:pointer;border-bottom:1px dotted var(--faint)}.fx.good{color:var(--green)}.fx.bad{color:var(--red)}
dialog{border:1px solid var(--line);border-radius:18px;background:var(--card);color:var(--ink);padding:0;width:min(720px,calc(100vw - 20px));max-height:calc(100vh - 30px)}
dialog::backdrop{background:rgba(5,6,14,.72)}
.x{position:sticky;top:8px;float:right;margin:8px 8px 0 0;z-index:2;width:34px;height:34px;border-radius:50%;border:1px solid var(--line);background:var(--card2);color:#fff;font-size:20px;line-height:1;cursor:pointer}
.dlgin{padding:16px 20px 20px}
.mh h3{font-size:30px;margin:2px 0 6px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.mh .kick{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}.mh .kick .tm{font-size:15px;letter-spacing:.5px;color:var(--orange)}
.mtags{display:flex;gap:6px;flex-wrap:wrap}
.mk{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:14px 0 4px}
.mk>div,.avg>div{background:var(--card2);border-radius:10px;padding:7px 10px}
.mk span,.avg span{display:block;font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.5px}
.mk b{font:700 22px 'Fira Sans Extra Condensed'}.mk small{display:block;font-size:11.5px;color:var(--muted);line-height:1.3}
.avg{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}.avg b{font:700 18px 'Fira Sans Extra Condensed'}
@media(max-width:620px){.mk{grid-template-columns:repeat(2,minmax(0,1fr))}.mh h3{font-size:26px}}
dialog h4{font:600 12.5px 'Fira Sans';letter-spacing:1.2px;text-transform:uppercase;color:var(--faint);margin:16px 0 8px}
dialog details{background:var(--card2)}
.bars{width:100%;height:78px;display:block}.bars rect{fill:#5d5b85}.bars rect.hi{fill:var(--lime)}
.fxl{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.fxl li{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;background:var(--card2);border-radius:10px;padding:6px 10px;font-size:14.5px}
.fxl .pj{margin-left:auto;color:var(--lime);font-weight:600}.fxl .w{color:var(--muted)}
.b2b{font:700 10px 'Fira Sans';background:var(--amber);color:#14152a;border-radius:4px;padding:1px 5px}
.res{display:inline-block;width:18px;border-radius:4px;font:700 11px/18px 'Fira Sans';color:#14152a;text-align:center}.res.w{background:var(--green)}.res.l{background:var(--red)}
.gl td.pir{color:var(--lime);font-weight:700}.gl small,.sch small{color:var(--faint);margin-left:3px}
.sch tr.tg td{background:rgba(255,122,26,.12)}
.wpm{display:inline-block;width:46px;height:6px;border-radius:3px;background:rgba(255,255,255,.18);overflow:hidden;margin-right:6px;vertical-align:middle}
.wpm span{display:block;height:100%;background:var(--orange)}
.chipsrow{display:flex;flex-wrap:wrap;gap:2px}
.ov tr.planrow td{background:rgba(106,167,255,.06)}
.ptab.mybtn{margin-left:auto;color:#fff}
.showing{font-size:15px;color:var(--muted);margin:-6px 0 6px}.showing b{color:var(--lime)}
.lnk2{background:none;border:0;color:var(--blue);font:inherit;cursor:pointer;padding:0 2px;text-decoration:underline}
.btn{background:var(--orange);color:#140b02;border:0;border-radius:999px;padding:9px 18px;font:700 16px 'Fira Sans Extra Condensed';cursor:pointer}
.btn:disabled{opacity:.45;cursor:not-allowed}
.edgrid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:10px;margin-top:10px}.edgrid .wide{grid-column:1/-1}
.edgrid label{font-size:12px;color:var(--faint);text-transform:uppercase;letter-spacing:.5px;display:grid;gap:4px}
input,select{font:15px 'Fira Sans';color:var(--ink);background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:7px 9px;min-width:0}
input[type=radio]{accent-color:var(--lime);width:18px;height:18px}
@media(max-width:560px){.edgrid{grid-template-columns:1fr 1fr}.edgrid label:first-child{grid-column:1/-1}}
.ed td{white-space:normal}.ed td small{color:var(--faint)}.ed .pchip{cursor:default}
.srch{width:100%;margin-top:10px}.eres{display:grid;gap:4px;margin-top:6px}
.eadd{display:flex;align-items:center;gap:8px;text-align:left;background:var(--card2);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:6px 10px;font:15px 'Fira Sans';cursor:pointer}
.eadd i{font-style:normal;background:var(--lime);color:#14152a;border-radius:999px;font:800 10.5px/18px 'Fira Sans';min-width:18px;text-align:center}
.eadd small{color:var(--faint)}
.ederr{margin:10px 0;color:var(--amber);font-size:14px}.edbtns{display:flex;gap:12px;align-items:center}
"""

JS = """
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
document.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected',x===t));
document.querySelectorAll('.var').forEach(v=>v.classList.toggle('on',v.id===t.dataset.v));
try{localStorage.setItem('elf-tab',t.dataset.v)}catch(e){}}));
try{const s=localStorage.getItem('elf-tab');const b=s&&document.querySelector('.tab[data-v="'+s+'"]');if(b)b.click()}catch(e){}

const $=q=>document.querySelector(q);
const P=JSON.parse($('#dP').textContent),T=JSON.parse($('#dT').textContent),M=JSON.parse($('#dM').textContent);
const dlg=$('#dlg'),db=$('#dlgb');
const E=v=>String(v==null?'':v).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const POS={Guard:'G',Forward:'F',Center:'C'};
const f1=x=>x==null?'–':(+x).toFixed(1);
const sur=n=>n.indexOf(' ')>0?n.slice(n.indexOf(' ')+1):n;
const tbtn=c=>T[c]?`<button class="tm" data-t="${c}"><span class="dot" style="background:${T[c].c1}"></span><span class="tn">${E(T[c].n)}</span><span class="ts">${E(T[c].s)}</span></button>`:E(c);
const tl=c=>T[c]?`<span class="lnk" data-t="${c}" role="button" tabindex="0">${E(T[c].s)}</span>`:E(c);
const gameOf=(c,rd)=>T[c]&&T[c].g.find(g=>g[0]===rd);
function show(html){db.innerHTML=html;if(!dlg.open)dlg.showModal();dlg.scrollTop=0}
function showP(s){
  const p=P[s];if(!p)return;const g=p.gl||[],n=g.length,b=p.bio||{};
  const av=i=>n?g.reduce((a,r)=>a+(r[i]||0),0)/n:null;
  const sh=(m,a)=>{const x=g.reduce((t,r)=>t+r[m],0),y=g.reduce((t,r)=>t+r[a],0);return y?Math.round(100*x/y)+'%':'–'};
  const tags=(A.players.includes(s)?`<span class="pill rec">${A.owner&&!A.doc?'In the report team':'Your player'}</span>`:'')+(A.cap===s?'<span class="pill">Captain</span>':'')
    +(A.owner&&M.a_in.includes(s)?'<span class="pill">Variation A buy</span>':'');
  const st=p.status&&p.status!=='playing'?`<span class="stat ${E(p.status)}">${E(p.status).toUpperCase()}</span>`:'';
  const mx=Math.max(10,...g.map(r=>r[15])),bw=100/Math.max(n,1);
  const bars=g.map((r,i)=>{const h=Math.max(.6,Math.max(0,r[15])/mx*36);
    return `<rect x="${(i*bw+bw*.15).toFixed(2)}" y="${(38-h).toFixed(2)}" width="${(bw*.7).toFixed(2)}" height="${h.toFixed(2)}" class="${r[15]>=20?'hi':''}"><title>R${r[0]} ${r[3]?'vs':'@'} ${r[2]}: ${r[15]} PIR, ${r[7]} min</title></rect>`}).join('');
  const fx=[...(p.nx||[]),...(p.nxe||[])].filter(e=>e[0]>=M.target).slice(0,5).map(e=>{const gm=gameOf(p.t,e[0]);
    const w=gm&&gm[6]!=null?`<span class="w">${Math.round(gm[6]*100)}% win</span>`:'';
    return `<li><b>R${e[0]}</b><span>${gm?E(gm[1]):''}</span><span>${e[2]?'vs':'@'} ${tl(e[1])}</span>${w}${e[5]?'<span class="b2b">B2B</span>':''}<span class="pj">proj ${f1(e[3])}</span></li>`}).join('');
  const rows=g.slice().reverse().map(r=>`<tr><td>${r[0]}</td><td class="l">${r[3]?'':'@ '}${tl(r[2])}</td><td><span class="res ${r[4]>r[5]?'w':'l'}">${r[4]>r[5]?'W':'L'}</span> ${r[4]}-${r[5]}</td><td>${f1(r[7])}${r[6]?'':'<small>b</small>'}</td><td>${r[8]}</td><td>${r[9]}</td><td>${r[10]}</td><td>${r[14]>0?'+':''}${r[14]}</td><td class="pir">${r[15]}</td></tr>`).join('');
  const chg=p.proj_price_change!=null?`${p.proj_price_change>0?'+':''}${p.proj_price_change} expected`:'';
  show(`<div class="mh"><div class="kick">${E(p.pos||'')} · ${tbtn(p.t)}${b.no?' · #'+E(b.no):''}${b.age?' · '+E(b.age)+' yrs':''}${b.h?' · '+E(b.h)+' cm':''}</div>
<h3>${E(p.name)} ${st}</h3><div class="mtags">${tags}</div></div>
<div class="mk"><div><span>Price</span><b>${f1(p.price)}</b><small>${chg}</small></div>
<div><span>Owned</span><b>${p.owned!=null?Math.round(p.owned)+'%':'–'}</b><small>${p.since_start!=null?(p.since_start>0?'+':'')+p.since_start+' since start':''}</small></div>
<div><span>Next round</span><b>${f1(p.proj)}</b><small>${p.p10!=null?'range '+f1(p.p10)+'–'+f1(p.p90):''}</small></div>
<div><span>Form (L3)</span><b>${f1(p.l3)}</b><small>L5 ${f1(p.l5)} · L10 ${f1(p.l10)}</small></div>
<div><span>Minutes (L3)</span><b>${f1(p.m3)}</b><small>L10 ${f1(p.m10)}${p.start!=null?' · starts '+Math.round(p.start*100)+'%':''}</small></div></div>
${n?`<h4>Season averages · ${n} games</h4><div class="avg"><div><span>PIR</span><b>${f1(av(15))}</b></div><div><span>Min</span><b>${f1(av(7))}</b></div><div><span>Pts</span><b>${f1(av(8))}</b></div><div><span>Reb</span><b>${f1(av(9))}</b></div><div><span>Ast</span><b>${f1(av(10))}</b></div><div><span>Stl</span><b>${f1(av(11))}</b></div><div><span>TO</span><b>${f1(av(12))}</b></div><div><span>2P%</span><b>${sh(16,17)}</b></div><div><span>3P%</span><b>${sh(18,19)}</b></div><div><span>FT%</span><b>${sh(20,21)}</b></div></div>
<h4>PIR game by game <small style="text-transform:none;letter-spacing:0">(green = 20+)</small></h4><svg class="bars" viewBox="0 0 100 40" preserveAspectRatio="none">${bars}</svg>`:''}
${fx?`<h4>Next games</h4><ul class="fxl">${fx}</ul>`:''}
${n?`<h4>Game log</h4><div class="tblw"><table class="gl"><thead><tr><th>Rd</th><th class="l">Opp</th><th>Result</th><th>Min</th><th>Pts</th><th>Reb</th><th>Ast</th><th>+/−</th><th>PIR</th></tr></thead><tbody>${rows}</tbody></table></div><p class="sub">b = came off the bench.</p>`:'<p class="sub">No game log for this player yet.</p>'}`)}
function showT(c){
  const t=T[c];if(!t)return;const st=t.st;
  const chips=A.players.filter(s=>PL[s]&&PL[s][1]===c).map(s=>chipHTML(s)).join('')
    +(A.owner?M.a_in.filter(s=>PL[s]&&PL[s][1]===c).map(s=>chipHTML(s,'plan')).join(''):'')
    +(A.coach===c?'<span class="pchip hc"><i>HC</i>coach pick</span>':'');
  const row=g=>{const r=g[5];
    const res=r?`<span class="res ${r[0]>r[1]?'w':'l'}">${r[0]>r[1]?'W':'L'}</span> ${r[0]}-${r[1]}`
      :(g[6]!=null?`<span class="wpm"><span style="width:${Math.round(g[6]*100)}%"></span></span>${Math.round(g[6]*100)}%${g[7]?' <small>'+g[7][0]+'-'+g[7][1]+'</small>':''}`:'–');
    return `<tr class="${g[0]===M.target?'tg':''}"><td>R${g[0]}</td><td class="l">${E(g[1])}<small>${E(g[2])}</small></td><td class="l">${g[4]?'vs':'@'} ${tl(g[3])}</td><td class="l">${res}</td></tr>`};
  const up=t.g.filter(g=>!g[5]),done=t.g.filter(g=>g[5]);
  show(`<div class="mh"><div class="kick">${st?'#'+st[0]+' in the standings · '+st[2]+'-'+st[3]+' · form '+E(st[8]):''}</div>
<h3><span class="dot big" style="background:${t.c1}"></span>${E(t.n)}</h3>
<div class="sub" style="margin:0">${t.coach?'Head coach <b>'+E(t.coach)+'</b>':''}${st?' · home '+E(st[6])+' · away '+E(st[7])+' · points '+st[4]+'–'+st[5]:''}</div></div>
${chips?`<h4>${A.owner&&!A.doc?'Report team':'Your'} players here</h4><div class="chipsrow">${chips}</div>`:''}
<h4>Upcoming games</h4><div class="tblw"><table class="sch"><tbody>${up.slice(0,8).map(row).join('')||'<tr><td>None</td></tr>'}</tbody></table></div>
${up.length>8?`<details><summary>Rest of the season (${up.length-8})</summary><div class="tblw"><table class="sch"><tbody>${up.slice(8).map(row).join('')}</tbody></table></div></details>`:''}
${done.length?`<details><summary>Results so far (${done.length})</summary><div class="tblw"><table class="sch"><tbody>${done.slice().reverse().map(row).join('')}</tbody></table></div></details>`:''}`)}
document.addEventListener('click',e=>{
  const el=e.target.closest('[data-p],[data-t]');
  if(el){if(el.dataset.p&&P[el.dataset.p]){e.preventDefault();showP(el.dataset.p)}else if(el.dataset.t&&T[el.dataset.t]){e.preventDefault();showT(el.dataset.t)}return}
  if(e.target===dlg)dlg.close()});
document.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.matches('[role=button]')){e.preventDefault();e.target.click()}});
$('#dlg .x').addEventListener('click',()=>dlg.close());
function pg(id){document.querySelectorAll('.ptab[data-pg]').forEach(b=>b.setAttribute('aria-selected',b.dataset.pg===id));
  document.querySelectorAll('.pg').forEach(x=>x.classList.toggle('on',x.id===id))}
document.querySelectorAll('.ptab[data-pg]').forEach(b=>b.addEventListener('click',()=>{pg(b.dataset.pg);
  try{history.replaceState(null,'',b.dataset.pg==='pg-sched'?'#schedule':location.pathname+location.search)}catch(e){}scrollTo({top:0})}));
if(location.hash==='#schedule')pg('pg-sched');

/* ---------- active team: the report's squad, or the visitor's own saved team ---------- */
const PL=JSON.parse($('#dPL').textContent),FBC=JSON.parse($('#dFB').textContent);
const N2S={};Object.entries(PL).forEach(([s,v])=>{N2S[v[0].toLowerCase()]=s});
const FORMS=[[2,2,1],[1,2,2],[2,1,2],[1,3,1],[3,1,1]];
const ownerTeam=()=>({name:'Report team',players:M.players,starters:M.starters,sixth:M.sixth,bench:M.bench,cap:M.cap,coach:M.coach,owner:true});
let A=ownerTeam();
function fromDoc(d,owner){const s=n=>N2S[String(n||'').toLowerCase()];
  return {name:d.name||'My team',players:(d.players||[]).map(s).filter(Boolean),starters:(d.starters||[]).map(s).filter(Boolean),
    sixth:s(d.sixth),bench:(d.bench||[]).map(s).filter(Boolean),cap:s(d.captain),coach:d.coach,bank:d.bank,target:d.target,owner:!!owner,doc:d}}
function setTeam(t){A=t;renderSched();const b=$('#myBtn');if(b)b.textContent=t.owner&&!t.doc?'👤 My team':'👤 '+t.name}
const CL=c=>T[c]?(T[c].coach?T[c].coach.split(' ').slice(-1)[0]+' ('+T[c].s+')':T[c].n):c;
function chipHTML(s,kind){const p=PL[s];if(!p)return'';let c=kind||'';
  if(!kind){if(A.cap===s)c+=' pcap';if(A.owner&&M.a_out.includes(s))c+=' out'}
  return `<button class="pchip ${c.trim()}" data-p="${s}" title="${E(p[0])}"><i>${POS[p[2]]||'?'}</i>${kind==='plan'?'A+ ':''}${E(sur(p[0]))}</button>`}
const coachChip=(c,k)=>`<span class="pchip hc ${k||''}" data-t="${c}" role="button" tabindex="0"><i>HC</i>${k==='plan'?'A+ ':''}${E(CL(c))}</span>`;
function renderSched(){
  const by={};A.players.forEach(s=>{const t=PL[s]&&PL[s][1];if(t)(by[t]=by[t]||[]).push(s)});
  const plan={};if(A.owner)M.a_in.forEach(s=>{const t=PL[s]&&PL[s][1];if(t)(plan[t]=plan[t]||[]).push(s)});
  const aC=A.owner&&M.a_coach&&M.a_coach!==A.coach?M.a_coach:null;
  const side=c=>(by[c]||[]).map(s=>chipHTML(s)).join('')+(plan[c]||[]).map(s=>chipHTML(s,'plan')).join('')
    +(c===A.coach?coachChip(c,aC?'out':''):'')+(c===aC?coachChip(c,'plan'):'');
  document.querySelectorAll('#pg-sched .sg').forEach(g=>{const hs=side(g.dataset.h),as=side(g.dataset.a),el=g.querySelector('.sgc');
    el.innerHTML=`<div>${hs}</div><div>${as}</div>`;el.hidden=!(hs||as);g.classList.toggle('mine',!!(hs||as))});
  document.querySelectorAll('#pg-sched .tm[data-t]').forEach(b=>{const n=(by[b.dataset.t]||[]).length+(b.dataset.t===A.coach?1:0);
    b.classList.toggle('has',n>0);const c=b.querySelector('.cnt');if(c){c.textContent=n;c.hidden=!n}});
  const turns=[];
  document.querySelectorAll('#pg-sched .dc').forEach(el=>{const r=+el.dataset.r,day=el.dataset.day;let n=0;
    Object.entries(by).forEach(([t,l])=>{const g=T[t]&&T[t].g.find(x=>x[0]===r);if(g&&g[1]===day)n+=l.length});
    el.textContent=n+' of '+(A.owner&&!A.doc?'the':'your')+' players';if(M.leg.includes(r))turns.push(`R${r} ${E(day.split(' ')[0])} <b>${n}</b>`)});
  const tu=$('#turns');if(tu)tu.innerHTML='Players per day: '+(turns.join(' · ')||'–');
  const fx=(t,r)=>{const g=T[t]&&T[t].g.find(x=>x[0]===r);if(!g)return'<td>–</td>';const pw=g[6];
    const cls=pw==null?'':pw>=.65?'good':pw<=.35?'bad':'';
    return `<td><span class="fx ${cls}" data-t="${g[3]}" role="button" tabindex="0">${E(g[1].split(' ')[0])} ${g[4]?'vs':'@'} ${E(T[g[3]]?T[g[3]].s:g[3])}${pw!=null?' · '+Math.round(pw*100)+'%':''}</span></td>`};
  const ord={Guard:0,Forward:1,Center:2};
  const row=(chip,t)=>`<tr${chip.includes('plan')?' class="planrow"':''}><td>${chip}</td><td>${t?`<span class="tsm" data-t="${t}" role="button" tabindex="0">${E(T[t]?T[t].s:t)}</span>`:''}</td>${M.leg.map(r=>fx(t,r)).join('')}</tr>`;
  const sorted=A.players.slice().sort((a,b)=>(ord[PL[a][2]]-ord[PL[b][2]])||PL[a][0].localeCompare(PL[b][0]));
  let rows=sorted.map(s=>row(chipHTML(s),PL[s][1])).join('');
  if(A.owner)rows+=M.a_in.map(s=>row(chipHTML(s,'plan'),PL[s]&&PL[s][1])).join('')+(aC?row(coachChip(aC,'plan'),aC):'');
  if(A.coach)rows+=row(coachChip(A.coach,aC?'out':''),A.coach);
  const ov=$('#ovb');if(ov)ov.innerHTML=rows;
  const sn=$('#showName');if(sn)sn.textContent=A.name;
  document.querySelectorAll('.planmark').forEach(x=>x.hidden=!A.owner);
  document.querySelectorAll('[data-edit]').forEach(x=>x.textContent=A.owner&&!A.doc?'Use my team':'Edit my team');
}
/* ---------- players outside this page: load full stats on demand ---------- */
let ALLP=null;
async function loadAll(){if(ALLP)return ALLP;try{const base=location.pathname.includes('/reports/')?'../':'';
  ALLP=await (await fetch(base+'site/players.json',{cache:'no-cache'})).json()}catch(e){ALLP={}}return ALLP}
document.addEventListener('click',async e=>{const el=e.target.closest('[data-p]');if(!el||P[el.dataset.p])return;
  const all=await loadAll();if(all[el.dataset.p]){P[el.dataset.p]=all[el.dataset.p];showP(el.dataset.p)}});
/* ---------- team editor (Firebase + Google sign-in) ---------- */
const AUTH={user:null,ready:false,enabled:!!(FBC.config&&FBC.config.apiKey)};
let ED=null;
function edFrom(t){const nm=s=>PL[s]?PL[s][0]:null;const role=s=>t.starters.includes(s)?'S':(t.sixth===s?'6':'B');
  return {name:t.name&&t.name!=='Report team'?t.name:((AUTH.user&&AUTH.user.displayName)||'My team').slice(0,40),
    rows:t.players.map(s=>({s,r:role(s)})),cap:t.cap,coach:t.coach||'',bank:t.bank!=null?t.bank:'',target:t.target||M.target_pts||200}}
function edCheck(){const e=[],rows=ED.rows,cnt={Guard:0,Forward:0,Center:0},club={};
  rows.forEach(x=>{const p=PL[x.s];cnt[p[2]]=(cnt[p[2]]||0)+1;club[p[1]]=(club[p[1]]||0)+1});
  if(rows.length!==10)e.push(`Pick 10 players (${rows.length} now).`);
  if(cnt.Guard!==4||cnt.Forward!==4||cnt.Center!==2)e.push(`Need 4 G / 4 F / 2 C (now ${cnt.Guard}/${cnt.Forward}/${cnt.Center}).`);
  Object.entries(club).forEach(([c,n])=>{if(n>6)e.push(`Max 6 from ${T[c]?T[c].n:c}.`)});
  const st=rows.filter(x=>x.r==='S'),six=rows.filter(x=>x.r==='6'),be=rows.filter(x=>x.r==='B');
  const f=[0,1,2].map(i=>st.filter(x=>PL[x.s][2]===['Guard','Forward','Center'][i]).length);
  if(st.length!==5)e.push(`Starting five: ${st.length} picked.`);else if(!FORMS.some(F=>F.join()===f.join()))e.push(`Starters ${f.join('-')} (G-F-C) isn't a valid formation.`);
  if(six.length!==1)e.push('Pick exactly one 6th man.');if(be.length!==4&&rows.length===10)e.push('Bench needs 4 players.');
  if(!st.some(x=>x.s===ED.cap))e.push('Captain must be a starter.');
  if(!ED.coach)e.push('Pick a coach.');
  const b=parseFloat(ED.bank);if(isNaN(b)||b<0||b>60)e.push('Bank must be a number (0–60).');
  return e}
function edRender(){
  if(!AUTH.enabled){show(`<div class="mh"><h3>My team</h3></div><p class="sub">Team saving isn't switched on for this page yet.</p>`);return}
  if(!AUTH.user){show(`<div class="mh"><h3>My team</h3></div><p class="sub">Sign in with Google to save your team. The schedule and player cards then mark <b>your</b> players on any device. Each person can only see and change their own team.</p>
<button class="btn" id="si">Sign in with Google</button><p class="sub" id="aerr"></p>`);
    $('#si').onclick=()=>window.ELF_FB.signIn().catch(err=>{$('#aerr').textContent='Sign-in failed: '+err.message});return}
  const rows=ED.rows.slice().sort((a,b)=>({Guard:0,Forward:1,Center:2}[PL[a.s][2]]-{Guard:0,Forward:1,Center:2}[PL[b.s][2]]));
  const errs=edCheck();
  const coachOpts=Object.keys(T).sort((a,b)=>T[a].n.localeCompare(T[b].n)).map(c=>`<option value="${c}"${c===ED.coach?' selected':''}>${E(CL(c))} · ${E(T[c].n)}</option>`).join('');
  show(`<div class="mh"><div class="kick">Signed in as ${E(AUTH.user.displayName||AUTH.user.email||'')} · <button class="lnk2" id="so">Sign out</button></div><h3>My team</h3></div>
<div class="edgrid"><label>Team name<input id="ename" maxlength="40" value="${E(ED.name)}"></label>
<label>Bank<input id="ebank" inputmode="decimal" value="${E(ED.bank)}" placeholder="e.g. 1.3"></label>
<label>Target<input id="etgt" inputmode="numeric" value="${E(ED.target)}"></label>
<label class="wide">Coach<select id="ecoach"><option value="">Pick a coach…</option>${coachOpts}</select></label></div>
<h4>Players · ${ED.rows.length}/10</h4>
<div class="tblw"><table class="ed"><thead><tr><th class="l">Player</th><th>Role</th><th>Capt.</th><th></th></tr></thead><tbody>${rows.map(x=>{const p=PL[x.s];
  return `<tr><td class="l"><span class="pchip"><i>${POS[p[2]]}</i>${E(p[0])}</span> <small>${E(T[p[1]]?T[p[1]].s:p[1])} · ${f1(p[3])}</small></td>
<td><select data-role="${x.s}"><option value="S"${x.r==='S'?' selected':''}>Starter</option><option value="6"${x.r==='6'?' selected':''}>6th man</option><option value="B"${x.r==='B'?' selected':''}>Bench</option></select></td>
<td><input type="radio" name="ecap" value="${x.s}"${ED.cap===x.s?' checked':''} aria-label="Captain"></td><td><button class="lnk2" data-rm="${x.s}" aria-label="Remove">✕</button></td></tr>`}).join('')}</tbody></table></div>
${ED.rows.length<10?`<input id="esearch" class="srch" placeholder="Add a player: type a name…" autocomplete="off"><div id="eres" class="eres"></div>`:''}
<div class="ederr">${errs.map(x=>'<div>'+E(x)+'</div>').join('')}</div>
<div class="edbtns"><button class="btn" id="esave"${errs.length?' disabled':''}>Save my team</button><span class="sub" id="emsg"></span></div>`);
  $('#so').onclick=()=>window.ELF_FB.signOut();
  $('#ename').oninput=e=>ED.name=e.target.value;$('#ebank').oninput=e=>{ED.bank=e.target.value;edSoft()};$('#etgt').oninput=e=>ED.target=e.target.value;
  $('#ecoach').onchange=e=>{ED.coach=e.target.value;edSoft()};
  document.querySelectorAll('[data-role]').forEach(s=>s.onchange=()=>{ED.rows.find(x=>x.s===s.dataset.role).r=s.value;edRender()});
  document.querySelectorAll('input[name=ecap]').forEach(r=>r.onchange=()=>{ED.cap=r.value;edSoft()});
  document.querySelectorAll('[data-rm]').forEach(b=>b.onclick=()=>{ED.rows=ED.rows.filter(x=>x.s!==b.dataset.rm);if(ED.cap===b.dataset.rm)ED.cap=null;edRender()});
  const si=$('#esearch');if(si){si.oninput=()=>{const q=si.value.trim().toLowerCase();const have=new Set(ED.rows.map(x=>x.s));
    const hits=q.length<2?[]:Object.entries(PL).filter(([s,v])=>!have.has(s)&&v[0].toLowerCase().includes(q)).slice(0,8);
    $('#eres').innerHTML=hits.map(([s,v])=>`<button class="eadd" data-add="${s}"><i>${POS[v[2]]}</i>${E(v[0])} <small>${E(T[v[1]]?T[v[1]].s:v[1])} · ${f1(v[3])}</small></button>`).join('');
    document.querySelectorAll('[data-add]').forEach(b=>b.onclick=()=>{const s=b.dataset.add;const st=ED.rows.filter(x=>x.r==='S').length;
      ED.rows.push({s,r:st<5?'S':(ED.rows.some(x=>x.r==='6')?'B':'6')});edRender();const n=$('#esearch');if(n)n.focus()})};
    if(document.activeElement!==si&&ED.rows.length)si.focus()}
  $('#esave').onclick=async()=>{const m=$('#emsg');m.textContent='Saving…';const nm=s=>PL[s][0];
    const d={name:(ED.name||'My team').slice(0,40),players:ED.rows.map(x=>nm(x.s)),starters:ED.rows.filter(x=>x.r==='S').map(x=>nm(x.s)),
      sixth:nm(ED.rows.find(x=>x.r==='6').s),bench:ED.rows.filter(x=>x.r==='B').map(x=>nm(x.s)),captain:nm(ED.cap),coach:ED.coach,
      bank:Math.round(parseFloat(ED.bank)*10)/10,target:Math.max(50,Math.min(500,parseInt(ED.target)||200))};
    try{await window.ELF_FB.save(d);m.textContent='Saved ✓';try{localStorage.setItem('elf-team',JSON.stringify({uid:AUTH.user.uid,d}))}catch(e){}
      setTeam(fromDoc(d,AUTH.user.uid===FBC.owner))}catch(err){m.textContent='Could not save: '+err.message}}}
function edSoft(){const errs=edCheck();const box=document.querySelector('.ederr');if(box)box.innerHTML=errs.map(x=>'<div>'+E(x)+'</div>').join('');
  const b=$('#esave');if(b)b.disabled=!!errs.length}
function openEditor(){ED=ED||edFrom(A.doc?A:{players:[],starters:[],bench:[],name:''});edRender()}
document.addEventListener('click',e=>{if(e.target.closest('[data-edit],#myBtn')){e.preventDefault();openEditor()}});
window.ELF={onAuth(user,doc){AUTH.user=user;AUTH.ready=true;
    if(user&&doc){setTeam(fromDoc(doc,user.uid===FBC.owner));ED=edFrom(A);try{localStorage.setItem('elf-team',JSON.stringify({uid:user.uid,d:doc}))}catch(e){}}
    else if(!user){try{localStorage.removeItem('elf-team')}catch(e){}ED=null;setTeam(ownerTeam())}
    else ED=null;
    if(dlg.open&&$('#dlgb .edgrid, #si'))openEditor()}};
try{const c=JSON.parse(localStorage.getItem('elf-team')||'null');if(c&&c.d)setTeam(fromDoc(c.d,c.uid===FBC.owner));else renderSched()}catch(e){renderSched()}
"""
FB_JS = r"""
const FBC=JSON.parse(document.getElementById('dFB').textContent);
if(FBC.config&&FBC.config.apiKey){
  const V='10.12.2',B='https://www.gstatic.com/firebasejs/'+V+'/';
  const [{initializeApp},{getAuth,GoogleAuthProvider,signInWithPopup,signInWithRedirect,onAuthStateChanged,signOut},
    {getFirestore,doc,getDoc,setDoc,serverTimestamp}]=await Promise.all([import(B+'firebase-app.js'),import(B+'firebase-auth.js'),import(B+'firebase-firestore.js')]);
  const app=initializeApp(FBC.config),auth=getAuth(app),db=getFirestore(app);
  window.ELF_FB={
    signIn:async()=>{const p=new GoogleAuthProvider();try{await signInWithPopup(auth,p)}catch(e){
      if(e.code==='auth/popup-blocked'||e.code==='auth/operation-not-supported-in-this-environment')await signInWithRedirect(auth,p);else throw e}},
    signOut:()=>signOut(auth),
    save:d=>setDoc(doc(db,'teams',auth.currentUser.uid),{...d,updated:serverTimestamp()})};
  onAuthStateChanged(auth,async u=>{let d=null;
    if(u){try{const s=await getDoc(doc(db,'teams',u.uid));d=s.exists()?s.data():null}catch(e){console.warn('team load failed',e)}}
    window.ELF&&window.ELF.onAuth(u,d)});
}
"""


def current_points(D):
    """Projected points of the squad exactly as it is set now (my_team.json lineup)."""
    try:
        t = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8"))
        lu = t.get("lineup") or {}
        st = [D.player(n) for n in lu.get("starters", [])]
        if len(st) != 5:
            return None
        pts, _ = squad_points(st, D.player(lu["sixth"]) if lu.get("sixth") else None,
                              [D.player(n) for n in lu.get("bench", [])], t.get("captain", ""),
                              D.games.get(coach_code_of(t.get("coach", ""))))
        return pts
    except Exception:
        return None


def sim_of(st, sixth, bench, captain, cc, D, target, n=6000):
    from sim import prep, simulate
    sp = [prep(p) for p in st]
    cap = next((p for p in sp if norm(p["name"]) == norm(captain)), sp[0])
    return simulate(sp, prep(sixth) if sixth else None, [prep(p) for p in bench], cap, cc, D.games, target, n=n)


def current_sim(D, target):
    try:
        t = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8"))
        lu = t.get("lineup") or {}
        st = [D.player(n) for n in lu.get("starters", [])]
        if len(st) != 5:
            return None
        return sim_of(st, D.player(lu["sixth"]) if lu.get("sixth") else None, [D.player(n) for n in lu.get("bench", [])],
                      t.get("captain", ""), coach_code_of(t.get("coach", "")), D, target)
    except Exception:
        return None


def deadline(D):
    t1 = [g for g in D.games.values() if g.get("turn") == 1 and g.get("time")]
    if not t1:
        return None
    g = min(t1, key=lambda g: g["time"])
    hh, mm = map(int, g["time"].split(":"))
    return f"{g['date']} · {hh - 1:02d}:{mm:02d} CET"   # site times are Athens time (CET+1)


def render(date):
    R = json.loads((ROOT / "reports" / f"{date}.json").read_text(encoding="utf-8"))
    D = Data(date)
    try:
        target = float(json.loads((ROOT / "my_team.json").read_text(encoding="utf-8")).get("target_points", 200))
    except Exception:
        target = 200.0
    target = float(R.get("target", target))
    rnd = R.get("round", D.target)
    tabs, panes, built = [], [], []
    for i, v in enumerate(R["variations"]):
        vid = f"v{v['key']}"
        st = [D.player(n) for n in v["starters"]]
        sixth = D.player(v["sixth"]) if v.get("sixth") else None
        bench = [D.player(n) for n in v["bench"]]
        cc = coach_code_of(v["coach"])
        cg = D.games.get(cc)
        total, cpts = squad_points(st, sixth, bench, v["captain"], cg)
        mc = sim_of(st, sixth, bench, v["captain"], cc, D, target)
        trades = v.get("trades", [])
        issues = validate(st + ([sixth] if sixth else []) + bench, v.get("bank_after"), len(trades))
        built.append((v, st, sixth, bench, cc, total, mc))

        tcards = []
        for k, t in enumerate(trades, 1):
            on, op = split_name(t["out"])
            inn, ip = split_name(t["in"])
            oc = D.player(on).get("t") if not t["out"].startswith("Coach") else coach_code_of(on)
            ic = D.player(inn).get("t") if not t["in"].startswith("Coach") else coach_code_of(inn)
            tcards.append(f"""<div class="trade"><span class="num">#{k} · {esc(t.get('credits', ''))}</span>
<div class="pair">{mini(on, oc, op, 'out', *slug_or_team(t["out"], on, oc, D))}<span class="arrow">→</span>{mini(inn, ic, ip, 'in', *slug_or_team(t["in"], inn, ic, D))}</div>
<p class="why">{esc(t.get('why', ''))}</p></div>""")
        g_c = cg or {}
        coach_card = f"""<div class="pl sm" data-t="{esc(cc)}" role="button" tabindex="0"><div class="shirt">{jersey(cc, 'HC')}</div>
<div class="tag"><div class="nm"><i>HC</i>{esc(coach_label(cc, v['coach']))}</div>
<div class="gm"><span>{esc(opp_txt(g_c))}</span><b>{cpts:+.1f}</b></div></div>
<div class="meta">win {int(round((g_c.get('pwin') or 0) * 100))}% · margin {g_c.get('margin', 0):+d}</div></div>"""
        tabs.append(f'<button class="tab" role="tab" data-v="{vid}" aria-selected="{str(i == 0).lower()}">'
                    f'{esc(v["key"])} · {esc(v["name"])}<small>{mc["p_target"]:.0%} chance of {target:.0f}+ · exp {mc["mean"]:.0f}'
                    f'</small></button>')
        panes.append(f"""<div class="var{' on' if i == 0 else ''}" id="{vid}">
<div class="vhead"><h3>{esc(v['name'])}</h3>{'<span class="pill rec">Recommended</span>' if v.get('recommended') else ''}
<span class="pill {esc(v.get('risk', ''))}">{esc(v.get('risk', ''))} risk</span></div>
<p class="tagline">{esc(v.get('tagline', ''))}</p>
{('<div class="issues">⚠ ' + esc('; '.join(issues)) + '</div>') if issues else ''}
<div class="kpis"><div class="kpi"><span>Chance of {target:.0f}+</span><b class="l">{mc['p_target']:.0%}</b></div>
<div class="kpi"><span>Expected</span><b>{mc['mean']:.0f}</b></div>
<div class="kpi"><span>Top-10% night</span><b>{mc['p90']:.0f}+</b></div>
<div class="kpi"><span>Trades · bank</span><b>{len(trades)}/4 · {v.get('bank_after', 0):.1f}</b></div></div>
<div class="grid"><div>
{court(st, v.get('formation', '2-2-1'), v['captain'], v.get('vice', ''))}
<div class="box"><h4>6th man (100%) &amp; coach</h4><div class="row">{card(sixth, small=True) if sixth else ''}{coach_card}</div></div>
<div class="box"><h4>Bench (50%)</h4><div class="row">{''.join(card(p, small=True) for p in bench)}</div></div>
</div><div class="side">
<h4>Trades</h4>{''.join(tcards) or '<p>No trades.</p>'}
<p class="capline">🧢 <b>{esc(surname(v['captain']))}</b> captain{(' · vice ' + esc(surname(v['vice']))) if v.get('vice') else ''}. {esc(v.get('captain_note', ''))}</p>
<details{' open' if v.get('recommended') else ''}><summary>Why this squad</summary><ul>{''.join(f'<li>{esc(x)}</li>' for x in v.get('reasoning', []))}</ul></details>
{('<details><summary>Change the plan if…</summary><ul>' + ''.join(f'<li>{esc(x)}</li>' for x in v.get('cancel_if', [])) + '</ul></details>') if v.get('cancel_if') else ''}
</div></div></div>""")

    # ---- 60-second plan from the recommended variation
    rv, rst, rsix, rbench, rcc, rtotal, rmc = next((b for b in built if b[0].get("recommended")), built[0])
    now = current_sim(D, target)
    tl = "".join(f'<div><span class="o">▼ {esc(split_name(t["out"])[0])}</span> → <span class="n">▲ {esc(split_name(t["in"])[0])}</span>'
                 f'<span class="cr">{esc(t.get("credits", ""))}</span></div>' for t in rv.get("trades", []))
    plan = f"""<section><div class="card plan"><div class="kick">Your plan in 60 seconds · Variation {esc(rv['key'])}</div>
<p class="lead">{esc(R['bottom_line'])}</p>
<ol class="steps">
<li><div><h4>Make {len(rv.get('trades', []))} trades</h4><div class="tl">{tl}</div></div></li>
<li><div><h4>Set the lineup ({esc(rv.get('formation', ''))})</h4><div class="lineup"><b>Start:</b> {esc(', '.join(surname(p['name']) for p in rst))}
· <b>6th:</b> {esc(surname(rsix['name']) if rsix else '–')} · <b>Bench:</b> {esc(', '.join(surname(p['name']) for p in rbench))}
· <b>Coach:</b> {esc(coach_label(rcc, rv['coach']))}</div></div></li>
<li><div><h4>Captain {esc(surname(rv['captain']))}</h4><div class="lineup">Vice: {esc(surname(rv.get('vice', '')) or '–')}</div></div></li>
</ol>
<div class="delta">Chance of {target:.0f}+: {f'now <b style="color:var(--ink)">{now["p_target"]:.1%}</b> →' if now else ''} with this plan <strong>{rmc["p_target"]:.0%}</strong>
<span>· expected {f'{now["mean"]:.0f} → ' if now else ''}{rmc["mean"]:.0f} · top-10% night {rmc["p90"]:.0f}+</span></div>
{f'<div class="fix"><b>No-trade fix:</b> {esc(R["current_fix"])}</div>' if R.get('current_fix') else ''}
</div></section>"""

    alerts = "".join(f'<div class="alert {esc(a.get("level", ""))}"><div>{esc(a["text"])}</div></div>' for a in R.get("alerts", []))

    def rum_item(r):
        src = f' · <a href="{esc(r["source"])}" target="_blank" rel="noopener">source</a>' if r.get("source") else ""
        return (f'<div class="r"><div class="top"><span class="k {esc(r.get("kind", ""))}">{esc(r.get("kind", ""))}</span>'
                f'<span class="team"{team_attr(r.get("team", ""))}>{esc(r.get("team", ""))}</span></div><p>{esc(r["text"])}{src}</p>'
                f'<div class="imp">{esc(r.get("impact", ""))}</div></div>')
    rums = R.get("rumours", [])
    rum_html = "".join(rum_item(r) for r in rums[:4])
    if len(rums) > 4:
        rum_html += f'<details><summary>{len(rums) - 4} more</summary><div class="rum" style="margin-top:10px">' + \
                    "".join(rum_item(r) for r in rums[4:]) + "</div></details>"
    players = "".join(
        f"<div class='pc'{pc_attr(p['name'], D)}><div class='top'><b>{esc(p['name'])}</b><span class='v {esc(p['verdict'].split('/')[0])}'>{esc(p['verdict'])}</span></div>"
        f"<p>{esc(p['read'])}</p></div>" for p in R.get("players", []))
    wname = lambda w: re.sub(r"\s*\(.*$", "", w["name"])
    watch = "".join(f"<li{pc_attr(wname(w), D)}><b>{esc(w['name'])}</b>: {esc(w['why'])}</li>" for w in R.get("watchlist", []))

    # ---- data for the popups + schedule tab
    sched = load_schedule(date)
    try:
        team = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8"))
    except Exception:
        team = {}
    names = set(team.get("players", [])) | {p["name"] for p in R.get("players", [])} | {wname(w) for w in R.get("watchlist", [])}
    for v in R["variations"]:
        names |= set(v["starters"]) | set(v["bench"]) | ({v["sixth"]} if v.get("sixth") else set())
        for t in v.get("trades", []):
            names |= {split_name(x)[0] for x in (t["out"], t["in"]) if not x.strip().startswith("Coach")}
    blobs = page_data(D, sched, team, names, rv)
    schedule_html = sched_tab(D, sched)
    write_players_file(D)
    dl = deadline(D)
    b = R.get("budget", {})

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0c0d19"><title>Fantasy Round {rnd} Plan</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Sans+Extra+Condensed:wght@500;600;700;800&family=Fira+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="wrap">
<header><div class="kick">EuroLeague Fantasy · analyst report · {esc(date)}</div><h1>Round {rnd} plan</h1>
<div class="chips">{f'<span class="chip hot">Deadline <b>{esc(dl)}</b></span>' if dl else ''}
<span class="chip">Budget <b>{esc(b.get('total', '–'))}</b></span><span class="chip">Bank <b>{esc(b.get('bank', '–'))}</b></span>
{f'<span class="chip">{esc(R["window"])}</span>' if R.get('window') else ''}</div>
<nav class="ptabs" role="tablist"><button class="ptab" role="tab" data-pg="pg-report" aria-selected="true">📋 Report</button>
<button class="ptab" role="tab" data-pg="pg-sched" aria-selected="false">📅 Schedule</button>
<button class="ptab mybtn" id="myBtn">👤 My team</button></nav></header>
<div class="pg on" id="pg-report">
{plan}
<section><h2>Three ways to play it</h2><div class="tabs" role="tablist">{''.join(tabs)}</div>{''.join(panes)}</section>
<section class="two"><div><h2>🚨 Alerts</h2><div class="alerts">{alerts or '<p>None.</p>'}</div></div>
<div><h2>🗞 Locker room &amp; rumours</h2><div class="rum">{rum_html or '<p>Nothing notable.</p>'}</div></div></section>
<section><h2>📋 Your players</h2><div class="players">{players}</div></section>
<section class="two"><div><h2>🔭 Watchlist</h2><ul class="watch">{watch}</ul></div>
<div><h2>📊 Last round</h2><div class="card" style="color:var(--muted)">{esc(R.get('review', ''))}</div></div></section>
</div>
<div class="pg" id="pg-sched">{schedule_html}</div>
<footer>Chance / expected / top-10% come from 6,000 simulated rounds: each player's range from the site's P10–P90 band, players in the same game rise and fall together, win bonus and coach score follow the simulated result.<br>
Data: basketballstats.eu model plus the analyst's own research. Recommendations only: make your changes in the official game.</footer>
</div><dialog id="dlg" aria-label="Details"><button class="x" aria-label="Close">×</button><div class="dlgin" id="dlgb"></div></dialog>
{blobs}<script>{JS}</script><script type="module">{FB_JS}</script></body></html>"""
    out = ROOT / "reports" / f"{date}.html"
    out.write_text(page, encoding="utf-8")
    (ROOT / "reports" / "latest.html").write_text(page, encoding="utf-8")
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"Rendered: {out}")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat())
