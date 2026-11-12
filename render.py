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
    return f"""<div class="pl{' sm' if small else ''}">
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


def coach_code_of(name):
    k = norm(name)
    return NAME2CODE.get(k) or next((c for n, c in NAME2CODE.items() if k and (k in n or n in k)), (name or "")[:3].upper())


def mini(name, code, price, kind):
    return f"""<div class="mini {kind}">{jersey(code, cls='mj')}<div><span class="lbl">{'OUT' if kind == 'out' else 'IN'}</span>
<b>{esc(name)}</b>{f' <small>{esc(price)}</small>' if price else ''}</div></div>"""


def split_name(s):
    m = re.match(r"\s*(?:Coach\s+)?(.+?)\s*\(([\d.]+)\)\s*$", s)
    return (m.group(1), m.group(2)) if m else (s.replace("Coach ", ""), "")


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
"""

JS = """
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
document.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected',x===t));
document.querySelectorAll('.var').forEach(v=>v.classList.toggle('on',v.id===t.dataset.v));
try{localStorage.setItem('elf-tab',t.dataset.v)}catch(e){}}));
try{const s=localStorage.getItem('elf-tab');const b=s&&document.querySelector('.tab[data-v="'+s+'"]');if(b)b.click()}catch(e){}
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
<div class="pair">{mini(on, oc, op, 'out')}<span class="arrow">→</span>{mini(inn, ic, ip, 'in')}</div>
<p class="why">{esc(t.get('why', ''))}</p></div>""")
        g_c = cg or {}
        coach_card = f"""<div class="pl sm"><div class="shirt">{jersey(cc, 'HC')}</div>
<div class="tag"><div class="nm"><i>HC</i>{esc(TEAM.get(cc, ('', v['coach']))[1])}</div>
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
· <b>Coach:</b> {esc(TEAM.get(rcc, ('', rv['coach']))[1])}</div></div></li>
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
                f'<span class="team">{esc(r.get("team", ""))}</span></div><p>{esc(r["text"])}{src}</p>'
                f'<div class="imp">{esc(r.get("impact", ""))}</div></div>')
    rums = R.get("rumours", [])
    rum_html = "".join(rum_item(r) for r in rums[:4])
    if len(rums) > 4:
        rum_html += f'<details><summary>{len(rums) - 4} more</summary><div class="rum" style="margin-top:10px">' + \
                    "".join(rum_item(r) for r in rums[4:]) + "</div></details>"
    players = "".join(
        f"<div class='pc'><div class='top'><b>{esc(p['name'])}</b><span class='v {esc(p['verdict'].split('/')[0])}'>{esc(p['verdict'])}</span></div>"
        f"<p>{esc(p['read'])}</p></div>" for p in R.get("players", []))
    watch = "".join(f"<li><b>{esc(w['name'])}</b>: {esc(w['why'])}</li>" for w in R.get("watchlist", []))
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
{f'<span class="chip">{esc(R["window"])}</span>' if R.get('window') else ''}</div></header>
{plan}
<section><h2>Three ways to play it</h2><div class="tabs" role="tablist">{''.join(tabs)}</div>{''.join(panes)}</section>
<section class="two"><div><h2>🚨 Alerts</h2><div class="alerts">{alerts or '<p>None.</p>'}</div></div>
<div><h2>🗞 Locker room &amp; rumours</h2><div class="rum">{rum_html or '<p>Nothing notable.</p>'}</div></div></section>
<section><h2>📋 Your players</h2><div class="players">{players}</div></section>
<section class="two"><div><h2>🔭 Watchlist</h2><ul class="watch">{watch}</ul></div>
<div><h2>📊 Last round</h2><div class="card" style="color:var(--muted)">{esc(R.get('review', ''))}</div></div></section>
<footer>Chance / expected / top-10% come from 6,000 simulated rounds: each player's range from the site's P10–P90 band, players in the same game rise and fall together, win bonus and coach score follow the simulated result.<br>
Data: basketballstats.eu model plus the analyst's own research. Recommendations only: make your changes in the official game.</footer>
</div><script>{JS}</script></body></html>"""
    out = ROOT / "reports" / f"{date}.html"
    out.write_text(page, encoding="utf-8")
    (ROOT / "reports" / "latest.html").write_text(page, encoding="utf-8")
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"Rendered: {out}")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat())
