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
def jersey(code, label=None):
    short, _, c1, c2 = TEAM.get(code, ("?", "", "#777", "#fff"))
    r, g, b = (int(c1[i:i + 2], 16) for i in (1, 3, 5))
    txt = "#1d1838" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#ffffff"
    return f"""<svg class="jsy" viewBox="0 0 60 64" aria-hidden="true">
<path d="M14 2h9c1 5 4 8 7 8s6-3 7-8h9l1 13c-4 3-6 7-6 12v35H19V27c0-5-2-9-6-12z" fill="{c1}" stroke="{c2}" stroke-width="2.5"/>
<text x="30" y="40" text-anchor="middle" font-size="11" font-weight="800" fill="{txt}">{esc(label or short)}</text></svg>"""


def opp_txt(g):
    if not g:
        return "–"
    o = TEAM.get(g["opp"], (g["opp"],))[0]
    return f"T{g.get('turn') or '?'} {'vs' if g.get('home') else '@'} {o}"


def card(p, slot="", captain=False, vice=False, small=False):
    g = p.get("game") or {}
    badge = '<span class="cap">C</span>' if captain else ('<span class="cap vc">VC</span>' if vice else "")
    fire = '<span class="fire" title="Hot form: last 3 games well above season level">🔥</span>' if p.get("hot") else ""
    warn = ""
    if p.get("status") in ("injured", "out", "doubtful"):
        warn = f'<span class="stat {p["status"]}">{esc(p["status"]).upper()}</span>'
    if p.get("missing"):
        warn = '<span class="stat injured">NOT FOUND</span>'
    return f"""<div class="pl{' sm' if small else ''}">
  <div class="shirt">{jersey(p.get('t'))}{badge}{fire}</div>
  <div class="tag"><div class="nm"><b>{POSL.get(p.get('pos'), '?')}</b> {esc(p['name'].split(' ', 1)[-1] if ' ' in p['name'] else p['name'])}</div>
  <div class="gm">{esc(opp_txt(g))}<span class="pj">{p.get('proj', 0):.1f}</span></div></div>{warn}
  <div class="meta">{p.get('price') or '–'} cr · L3 {p.get('l3') if p.get('l3') is not None else '–'}{(' · ' + slot) if slot else ''}</div>
</div>"""


def court(starters, formation, captain, vice):
    g, f, c = FORMATIONS.get(formation, (2, 2, 1))
    by = {"Guard": [], "Forward": [], "Center": []}
    for p in starters:
        by.setdefault(p.get("pos"), []).append(p)
    rows = []
    for pos in ("Center", "Forward", "Guard"):
        cells = "".join(card(p, captain=norm(p["name"]) == norm(captain), vice=norm(p["name"]) == norm(vice))
                        for p in by.get(pos, []))
        rows.append(f'<div class="row r{pos[0]}">{cells}</div>')
    return f"""<div class="court"><svg class="lines" viewBox="0 0 400 300" preserveAspectRatio="none" aria-hidden="true">
<path d="M120 0v95h160V0M150 95a50 50 0 0 0 100 0M40 0v40a160 160 0 0 0 320 0V0" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="2"/>
<circle cx="200" cy="300" r="45" fill="none" stroke="rgba(255,255,255,.55)" stroke-width="2"/></svg>
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


def validate(players, coach_code, bank, budget_note):
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
    return issues


# ------------------------------------------------------------------ page
CSS = """
:root{--o1:#f6a019;--o2:#ef6a2c;--o3:#e6402f;--ink:#1d1838;--card:#fff;--muted:#6b6880;--panel:rgba(255,255,255,.92);
--line:#2a2350;--good:#12a150;--bad:#d7263d;--wood1:#e9b97f;--wood2:#d99a5b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--card:#24203d;--panel:rgba(28,24,52,.94);--ink:#f3f1ff;--muted:#b7b3cf}}
:root[data-theme=dark]{--card:#24203d;--panel:rgba(28,24,52,.94);--ink:#f3f1ff;--muted:#b7b3cf}
*{box-sizing:border-box}body{margin:0;font-family:'Fira Sans',system-ui,sans-serif;color:var(--ink);
background:linear-gradient(160deg,var(--o1),var(--o2) 45%,var(--o3));background-attachment:fixed;min-height:100vh}
h1,h2,h3,.cond{font-family:'Fira Sans Extra Condensed','Fira Sans',sans-serif;letter-spacing:.3px}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header.top{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between;color:#fff;margin:8px 0 14px}
header.top h1{font-size:40px;margin:0;line-height:1;text-shadow:0 2px 0 rgba(0,0,0,.15)}
header.top .sub{opacity:.95;font-size:15px}
.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{background:var(--line);color:#fff;border-radius:6px;padding:6px 10px;font-weight:600;font-size:14px}
.chip.y{background:#e8ff3a;color:#1d1838}
.panel{background:var(--panel);border-radius:14px;padding:16px 18px;margin:14px 0;box-shadow:0 6px 20px rgba(0,0,0,.12)}
.panel h2{margin:0 0 10px;font-size:26px}.bottom{font-size:17px;line-height:1.5}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.tab{border:0;cursor:pointer;border-radius:10px;padding:10px 14px;font:600 16px 'Fira Sans Extra Condensed',sans-serif;background:rgba(255,255,255,.25);color:#fff}
.tab[aria-selected=true]{background:var(--line);box-shadow:inset 0 -4px 0 #e8ff3a}
.tab small{display:block;font:500 12px 'Fira Sans',sans-serif;opacity:.85}
.var{display:none}.var.on{display:block}
.grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:16px;align-items:start}
@media(max-width:860px){.grid{grid-template-columns:minmax(0,1fr)}}
.court{position:relative;border-radius:18px;padding:34px 8px 26px;overflow:hidden;
background:repeating-linear-gradient(90deg,var(--wood1) 0 38px,var(--wood2) 38px 40px,#e2ab6c 40px 78px,var(--wood2) 78px 80px);
box-shadow:inset 0 0 0 4px rgba(255,255,255,.35),0 10px 24px rgba(0,0,0,.18)}
.court .lines{position:absolute;inset:0;width:100%;height:100%}
.frm{position:absolute;top:8px;left:12px;background:var(--line);color:#fff;font:700 13px 'Fira Sans Extra Condensed';padding:3px 8px;border-radius:5px}
.row{position:relative;display:flex;justify-content:space-evenly;gap:6px;margin:6px 0 14px}
.pl{width:132px;max-width:31%;text-align:center;position:relative}
.shirt{position:relative;height:66px}.jsy{height:66px;filter:drop-shadow(0 3px 3px rgba(0,0,0,.25))}
.cap{position:absolute;right:14px;top:4px;background:#e8ff3a;color:#1d1838;border-radius:50%;width:24px;height:24px;font:800 13px/24px 'Fira Sans Extra Condensed'}
.cap.vc{background:#fff;font-size:10px}
.fire{position:absolute;left:12px;top:2px;font-size:16px;background:#e8ff3a;border-radius:5px;padding:0 2px}
.tag{border-radius:6px;overflow:hidden;box-shadow:0 3px 8px rgba(0,0,0,.2)}
.nm{background:var(--line);color:#fff;font:500 15px 'Fira Sans Extra Condensed';padding:4px 4px 3px;border-bottom:3px solid #19d3a2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nm b{opacity:.7;font-weight:500;margin-right:3px}
.gm{background:#fff;color:#1d1838;font:600 16px 'Fira Sans Extra Condensed';padding:5px 4px;display:flex;justify-content:space-between;gap:4px}
.pj{background:#1d1838;color:#e8ff3a;border-radius:4px;padding:0 5px}
.meta{font-size:11px;color:#3b2a14;margin-top:3px}
.stat{display:inline-block;margin-top:3px;font:700 11px 'Fira Sans';border-radius:4px;padding:1px 5px;color:#fff;background:var(--bad)}
.stat.doubtful{background:#e08a00}
.benchwrap{display:grid;grid-template-columns:1fr;gap:12px;margin-top:12px}
.box{background:rgba(255,255,255,.18);border-radius:14px;padding:10px 6px}
.box h4{margin:0 0 6px;text-align:center;color:#fff;font:600 14px 'Fira Sans Extra Condensed';letter-spacing:1px;opacity:.9}
.box .row{margin:0;flex-wrap:wrap;row-gap:10px}.box .pl{width:124px;max-width:23%}
@media(max-width:520px){.box .pl{max-width:46%}}.box .meta{color:#fff}
.coach .nm{border-bottom-color:#e8ff3a}
.why h3{margin:4px 0 6px;font-size:22px}.why ul{margin:6px 0 10px;padding-left:18px;line-height:1.45}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:8px 0 12px}
.kpi{background:var(--line);color:#fff;border-radius:10px;padding:8px;text-align:center}
.kpi b{display:block;font:700 24px 'Fira Sans Extra Condensed';color:#e8ff3a}.kpi span{font-size:12px;opacity:.85}
.trade{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 0;border-bottom:1px dashed rgba(127,127,127,.35)}
.trade .out{color:var(--bad);font-weight:600}.trade .in{color:var(--good);font-weight:600}.trade .d{margin-left:auto;font-size:13px;color:var(--muted)}
.trade .r{flex-basis:100%;font-size:13px;color:var(--muted)}
.pill{display:inline-block;font:600 12px 'Fira Sans';padding:2px 8px;border-radius:20px;background:#ece9ff;color:#2a2350}
.pill.rec{background:#e8ff3a}.pill.warn{background:#ffe1e1;color:#8a1020}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:7px 6px;border-bottom:1px solid rgba(127,127,127,.25);text-align:left;vertical-align:top}
th{font:600 13px 'Fira Sans Extra Condensed';text-transform:uppercase;color:var(--muted)}
.v{font:700 12px 'Fira Sans';padding:2px 7px;border-radius:4px;color:#fff;white-space:nowrap}
.v.KEEP{background:var(--good)}.v.SELL{background:var(--bad)}.v.WATCH{background:#e08a00}.v.BUY{background:#2a62ff}.v.CAPTAIN{background:#1d1838}
.rumour{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;padding:8px 0;border-bottom:1px solid rgba(127,127,127,.2)}
.rumour .k{font:700 11px 'Fira Sans';text-transform:uppercase;border-radius:4px;padding:2px 6px;height:fit-content;color:#fff;background:#6b6880}
.rumour .k.confirmed{background:var(--good)}.rumour .k.rumour{background:#e08a00}.rumour .k.report{background:#2a62ff}
.rumour .imp{grid-column:2;font-size:13px;color:var(--muted)}.rumour a{color:inherit}
.alert{padding:8px 10px;border-left:4px solid #e08a00;margin:6px 0;background:rgba(224,138,0,.08);border-radius:4px}
.alert.high{border-color:var(--bad);background:rgba(215,38,61,.08)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:860px){.cols{grid-template-columns:1fr}}
footer{color:#fff;opacity:.9;font-size:12px;text-align:center;padding:18px 0 30px}
.issues{color:var(--bad);font-weight:600}
"""

JS = """
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
document.querySelectorAll('.tab').forEach(x=>x.setAttribute('aria-selected',x===t));
document.querySelectorAll('.var').forEach(v=>v.classList.toggle('on',v.id===t.dataset.v));
try{localStorage.setItem('elf-tab',t.dataset.v)}catch(e){}}));
try{const s=localStorage.getItem('elf-tab');const b=s&&document.querySelector('.tab[data-v="'+s+'"]');if(b)b.click()}catch(e){}
"""


def render(date):
    R = json.loads((ROOT / "reports" / f"{date}.json").read_text(encoding="utf-8"))
    D = Data(date)
    rnd = R.get("round", D.target)
    tabs, panes = [], []
    for i, v in enumerate(R["variations"]):
        vid = f"v{v['key']}"
        st = [D.player(n) for n in v["starters"]]
        sixth = D.player(v["sixth"]) if v.get("sixth") else None
        bench = [D.player(n) for n in v["bench"]]
        coach_code = NAME2CODE.get(norm(v["coach"]).replace("  ", " "), v["coach"].upper()[:3])
        cg = D.games.get(coach_code)
        total, cpts = squad_points(st, sixth, bench, v["captain"], cg)
        issues = validate(st + ([sixth] if sixth else []) + bench, coach_code, v.get("bank_after"), None)
        # next-round sum for the same 10 (no lineup weighting) as a double-week signal
        nxt = sum((p.get("proj_next") or 0) for p in st + ([sixth] if sixth else []))
        trades = "".join(
            f"""<div class="trade"><span class="out">▼ {esc(t['out'])}</span>→<span class="in">▲ {esc(t['in'])}</span>
<span class="d">{esc(t.get('credits', ''))}</span><div class="r">{esc(t.get('why', ''))}</div></div>"""
            for t in v.get("trades", [])) or '<p>No trades: lineup changes only.</p>'
        g_c = cg or {}
        coach_card = f"""<div class="pl sm coach"><div class="shirt">{jersey(coach_code, 'HC')}</div>
<div class="tag"><div class="nm"><b>HC</b> {esc(TEAM.get(coach_code, ('', v['coach']))[1])}</div>
<div class="gm">{esc(opp_txt(g_c))}<span class="pj">{cpts:+.1f}</span></div></div>
<div class="meta">win {int(round((g_c.get('pwin') or 0) * 100))}% · margin {g_c.get('margin', 0):+d}</div></div>"""
        rec = '<span class="pill rec">RECOMMENDED</span> ' if v.get("recommended") else ""
        tabs.append(f'<button class="tab" role="tab" data-v="{vid}" aria-selected="{str(i == 0).lower()}">'
                    f'{esc(v["key"])} · {esc(v["name"])}<small>{total:.0f} proj pts · {esc(v.get("risk", ""))} risk</small></button>')
        panes.append(f"""<section class="var{' on' if i == 0 else ''}" id="{vid}">
<div class="grid"><div>
{court(st, v.get('formation', '2-2-1'), v['captain'], v.get('vice', ''))}
<div class="benchwrap"><div class="box"><h4>6TH MAN (100%) &amp; COACH</h4><div class="row">{card(sixth, small=True) if sixth else ''}{coach_card}</div></div>
<div class="box"><h4>BENCH (50%)</h4><div class="row">{''.join(card(p, small=True) for p in bench)}</div></div></div>
</div>
<div class="panel why"><h3>{rec}{esc(v['name'])}</h3><p><i>{esc(v.get('tagline', ''))}</i></p>
<div class="kpis"><div class="kpi"><b>{total:.0f}</b><span>proj. fantasy pts R{rnd}</span></div>
<div class="kpi"><b>{v.get('bank_after', 0):.1f}</b><span>bank after</span></div>
<div class="kpi"><b>{len(v.get('trades', []))}/4</b><span>trades used</span></div></div>
{('<p class="issues">⚠ ' + esc('; '.join(issues)) + '</p>') if issues else ''}
<h4>Trades</h4>{trades}
<h4>Why this squad</h4><ul>{''.join(f'<li>{esc(x)}</li>' for x in v.get('reasoning', []))}</ul>
<h4>Captain</h4><p>{esc(v.get('captain_note', v['captain']))}</p>
{('<h4>Cancel if…</h4><ul>' + ''.join(f'<li>{esc(x)}</li>' for x in v.get('cancel_if', [])) + '</ul>') if v.get('cancel_if') else ''}
<p class="meta" style="color:var(--muted)">Next round (R{rnd + 1}) raw projection of the active six: {nxt:.0f} PIR</p>
</div></div></section>""")

    alerts = "".join(f'<div class="alert {esc(a.get("level", ""))}">{esc(a["text"])}</div>' for a in R.get("alerts", []))
    rum = "".join(
        f"""<div class="rumour"><span class="k {esc(r.get('kind', ''))}">{esc(r.get('kind', ''))}</span>
<div><b>{esc(r.get('team', ''))}</b> {esc(r['text'])} {f'<a href="{esc(r["source"])}" target="_blank" rel="noopener">source</a>' if r.get('source') else ''}</div>
<div class="imp">Fantasy impact: {esc(r.get('impact', ''))}</div></div>""" for r in R.get("rumours", []))
    rows = "".join(
        f"<tr><td><b>{esc(p['name'])}</b></td><td><span class='v {esc(p['verdict'].split('/')[0])}'>{esc(p['verdict'])}</span></td>"
        f"<td>{esc(p['read'])}</td></tr>" for p in R.get("players", []))
    watch = "".join(f"<li><b>{esc(w['name'])}</b>: {esc(w['why'])}</li>" for w in R.get("watchlist", []))
    cur = R.get("current_fix")
    cur_html = f"""<div class="panel"><h2>Your squad right now</h2><p>{esc(cur)}</p></div>""" if cur else ""

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fantasy Round {rnd} Plan</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fira+Sans+Extra+Condensed:wght@500;600;700;800&family=Fira+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="wrap">
<header class="top"><div><div class="sub cond">EUROLEAGUE FANTASY · ANALYST REPORT · {esc(date)}</div>
<h1>Round {rnd} plan</h1></div>
<div class="chips"><span class="chip y">Budget {R.get('budget', {}).get('total', '–')}</span>
<span class="chip">Bank {R.get('budget', {}).get('bank', '–')}</span><span class="chip">{esc(R.get('window', ''))}</span></div></header>
<div class="panel bottom"><h2>Bottom line</h2>{esc(R['bottom_line'])}</div>
{cur_html}
<div class="tabs" role="tablist">{''.join(tabs)}</div>
{''.join(panes)}
<div class="cols"><div class="panel"><h2>🚨 Alerts</h2>{alerts or '<p>None.</p>'}</div>
<div class="panel"><h2>🗞 Locker room &amp; rumours</h2>{rum or '<p>Nothing notable.</p>'}</div></div>
<div class="panel"><h2>📋 Your players</h2><table><thead><tr><th>Player</th><th>Verdict</th><th>Read</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="cols"><div class="panel"><h2>🔭 Watchlist</h2><ul>{watch}</ul></div>
<div class="panel"><h2>📊 Last round review</h2><p>{esc(R.get('review', ''))}</p></div></div>
<footer>Projections: basketballstats.eu model, adjusted by the analyst. Points = PIR × win bonus, captain ×2, bench ×0.5, coach expected value from projected margin.
Recommendations only — make changes in the official game yourself.</footer>
</div><script>{JS}</script></body></html>"""
    out = ROOT / "reports" / f"{date}.html"
    out.write_text(page, encoding="utf-8")
    (ROOT / "reports" / "latest.html").write_text(page, encoding="utf-8")
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(f"Rendered: {out}")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat())
