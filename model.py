"""Our own PIR projection: a long-memory player baseline, the opponent's defence, and the player's own record
against that opponent (head-to-head). It replaces the site's projections, which lean heavily on the last few games.

proj = BLEND x (baseline x opponent defence x head-to-head x home/away) + (1 - BLEND) x site projection
(the site's share keeps its news on minutes and roles; the backtest liked 0.75 of ours). When the site projects
less than SITE_FLOOR x our number it has news we don't (injury, lost role), so the site's number is used as is.

- baseline: weighted PIR average over this season and the last three EuroLeague seasons. Weights halve every
  HALF_LIFE games and each older season counts SEASON_W times the one after it, so a hot or cold week moves the
  number a little, not a lot. Players with few EuroLeague games lean on the site's projection (PRIOR_GAMES games' worth).
- opponent defence: PIR the opponent's opponents score compared with their own averages (this season + last,
  shrunk toward 1 by sample size).
- head-to-head: the player's own PIR against this opponent, all seasons, compared with what his average and that
  defence would predict; shrunk by H2H_K games and capped at H2H_CAP. The backtest found head-to-head is mostly
  noise (weighting it strongly made predictions worse), so it is a gentle nudge: 10 games against a team moves
  the number by a quarter of the gap, 2 games by about 6%.
- home/away: the league-wide home edge from the data.

Past seasons come from data/history/*.json (fetch_history.py); this season from the site's game logs (`gl`).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"

# Chosen by backtest (data/history, Nov 2026): last season R15-34 (3,596 player-games, pure model) and this
# season R12+R14 (428 player-games, against the site's own projections). See PLAYBOOK.md, "Projection model".
PARAMS = {"HALF_LIFE": 20.0, "SEASON_W": 0.6, "PRIOR_GAMES": 4.0, "DEF_K": 150.0, "H2H_K": 30.0, "H2H_CAP": 0.15,
          "MIN_MIN": 3.0, "BLEND": 0.75, "SITE_FLOOR": 0.5}
CUR = "CUR"  # season key for this season's games (from the site's logs)


def load_history():
    """rows per person code: (season_rank, date, club, opp, home, minutes, pir), season_rank 1 = last season."""
    seasons = sorted(HIST.glob("E*.json"), reverse=True)
    by = defaultdict(list)
    for rank, f in enumerate(seasons, 1):
        for code, club, opp, home, date, rnd, mins, pir in json.loads(f.read_text(encoding="utf-8"))["rows"]:
            by[code.strip()].append((rank, date, club, opp, home, mins, pir))
    return by


def current_rows(p, before_round=None):
    """This season's games from the site's log (gl): rd, date, opp, home, ..., min (7), ..., PIR (15)."""
    out = []
    for g in p.get("gl") or []:
        if before_round is not None and g[0] >= before_round:
            continue
        out.append((0, g[1], p.get("t"), g[2], g[3], g[7], g[15]))
    return out


class Model:
    def __init__(self, pool, params=None, before_round=None, hist=None):
        """pool: iterable of player dicts with c (person code), t, gl. before_round: use only this season's games
        played before that round (for backtests)."""
        self.P = {**PARAMS, **(params or {})}
        self.hist = hist if hist is not None else load_history()
        self.before = before_round
        self.games = {}
        for p in pool:
            code = str(p.get("c") or "").strip()
            rows = [r for r in self.hist.get(code, [])] + current_rows(p, before_round)
            rows = [r for r in rows if (r[5] or 0) >= self.P["MIN_MIN"]]
            rows.sort(key=lambda r: (-r[0], r[1]))  # oldest season first, then by date
            self.games[id(p)] = rows
        self._defence()
        self._home()

    # ---- player baseline
    def weights(self, rows):
        n = len(rows)
        return [0.5 ** ((n - 1 - i) / self.P["HALF_LIFE"]) * self.P["SEASON_W"] ** r[0] for i, r in enumerate(rows)]

    def baseline(self, p, prior=None):
        rows = self.games.get(id(p)) or []
        w = self.weights(rows)
        sw = sum(w)
        if not rows:
            return prior, 0.0
        avg = sum(wi * r[6] for wi, r in zip(w, rows)) / sw
        if prior is not None:  # few games: lean on the site's number (as a neutral-venue baseline)
            k = self.P["PRIOR_GAMES"]
            avg = (sw * avg + k * prior) / (sw + k)
        return avg, sw

    def season_avgs(self, rows):
        s = defaultdict(list)
        for r in rows:
            s[r[0]].append(r[6])
        return {k: sum(v) / len(v) for k, v in s.items()}

    # ---- opponent defence: PIR allowed vs the scorers' own season averages (this season + last)
    def _defence(self):
        num, den, n = defaultdict(float), defaultdict(float), defaultdict(int)
        for rows in self.games.values():
            avgs = self.season_avgs(rows)
            for r in rows:
                if r[0] > 1:
                    continue
                w = 1.0 if r[0] == 0 else 0.5
                num[r[3]] += w * r[6]
                den[r[3]] += w * avgs[r[0]]
                n[r[3]] += w
        k = self.P["DEF_K"]
        self.def_f = {}
        for t in num:
            raw = num[t] / den[t] if den[t] > 0 else 1.0
            self.def_f[t] = 1 + (n[t] / (n[t] + k)) * (raw - 1)

    def _home(self):
        h, a = [], []
        for rows in self.games.values():
            for r in rows:
                (h if r[4] else a).append(r[6])
        mh, ma = (sum(h) / len(h), sum(a) / len(a)) if h and a else (1, 1)
        self.home_f = {1: 2 * mh / (mh + ma), 0: 2 * ma / (mh + ma)}

    # ---- head-to-head
    def h2h(self, p, opp):
        rows = self.games.get(id(p)) or []
        avgs = self.season_avgs(rows)
        vs = [r for r in rows if r[3] == opp]
        if not vs:
            return 1.0, 0, None
        actual = sum(r[6] for r in vs)
        expect = sum(avgs[r[0]] * self.def_f.get(opp, 1.0) * self.home_f[r[4]] for r in vs)
        if expect <= 1:
            return 1.0, len(vs), round(actual / len(vs), 1)
        raw = actual / expect
        k, cap = self.P["H2H_K"], self.P["H2H_CAP"]
        f = 1 + (len(vs) / (len(vs) + k)) * (raw - 1)
        return max(1 - cap, min(1 + cap, f)), len(vs), round(actual / len(vs), 1)

    def project(self, p, opp, home, site=None):
        """Projected PIR for one game. site: the site's projection for it (prior for thin samples)."""
        prior = None
        if site is not None:  # strip the site's own venue/opponent to get a neutral prior
            prior = site / (self.def_f.get(opp, 1.0) * self.home_f[1 if home else 0])
        base, _ = self.baseline(p, prior)
        if base is None:
            return None, {}
        f_h2h, n, avg = self.h2h(p, opp)
        d = self.def_f.get(opp, 1.0)
        v = base * d * f_h2h * self.home_f[1 if home else 0]
        return v, {"base": round(base, 1), "def": round(d, 3), "h2h": round(f_h2h, 3), "h2h_n": n, "h2h_avg": avg}


def apply(pool, log=print):
    """Replace each player's nx/nxe projections (index 3) with ours; keep the site's in nx_site/nxe_site.
    Players the site projects at 0 (injured/out) stay at 0; overrides applied later still win."""
    m = Model(pool)
    n = 0
    for p in pool:
        for key in ("nx", "nxe"):
            if not p.get(key):
                continue
            p[key + "_site"] = [list(e) for e in p[key]]
            new = []
            for e in p[key]:
                e = list(e)
                site = e[3]
                if site and site > 0:
                    v, info = m.project(p, e[1], e[2], site)
                    if v is not None:
                        info["pure"] = round(v, 1)
                        if site < m.P["SITE_FLOOR"] * v:  # the site knows of an injury / lost role: trust it
                            info["site_wins"] = True
                            v = site
                        else:
                            v = m.P["BLEND"] * v + (1 - m.P["BLEND"]) * site
                        if key == "nx" and not new:  # first round: keep the spread's shape
                            k = v / site
                            for f in ("p10", "p90"):
                                if p.get(f) is not None:
                                    p[f] = round(p[f] * k, 1)
                            p["model"] = {**info, "opp": e[1], "site": site, "ours": round(v, 1)}
                        e[3] = round(v, 1)
                        n += 1
                new.append(e)
            p[key] = new
    log(f"  model: {n} round projections replaced (home edge {m.home_f[1]:.3f}/{m.home_f[0]:.3f})")
    return m
