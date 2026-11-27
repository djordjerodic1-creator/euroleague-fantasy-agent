"""Our own PIR projection: a long-memory player baseline, the opponent's defence, and the player's own record
against that opponent (head-to-head). It replaces the site's projections, which lean heavily on the last few games.

proj = BLEND x (minutes x PIR per minute x opponent defence x head-to-head x home/away) + (1 - BLEND) x site
(minutes: recent-weighted, half-life 8 games; PIR per minute: long memory, half-life 40 games, shrunk to the league)
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
          "MIN_MIN": 3.0, "BLEND": 0.75, "SITE_FLOOR": 0.5,
          # v2 (backtest 28 Nov): minutes x PIR-per-minute helped on both samples and is on. Teammate in/out
          # minute splits, short rest and per-team home edges did not help (or made it worse), so they are off;
          # the splits are still shown on player cards as information.
          "MINRATE": 1, "HL_RATE": 40.0, "RATE_SW": 0.7, "K_RATE": 100.0, "HL_MIN": 8.0, "MIN_SW": 0.3,
          "TEAMMATES": 0, "K_TM": 6.0, "TM_MIN_GAMES": 3, "MAX_MIN": 36.0, "TM_KEY_MIN": 0.0,
          "REST": 0, "TEAM_HOME": 0, "K_HOME": 400.0}
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
        self.games, self.code = {}, {}
        for p in pool:
            code = str(p.get("c") or "").strip()
            self.code[id(p)] = code
            rows = [r for r in self.hist.get(code, [])] + current_rows(p, before_round)
            rows = [r for r in rows if (r[5] or 0) >= self.P["MIN_MIN"]]
            rows.sort(key=lambda r: (-r[0], r[1]))  # oldest season first, then by date
            self.games[id(p)] = rows
        self._defence()
        self._home()
        self._team_games(pool)
        self._league_rate()
        self._rest()

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


    # ---- v2 pieces
    def _team_games(self, pool):
        """(season_rank, club) -> {date: {code: minutes}} for every player who played (this season + last)."""
        tg = defaultdict(lambda: defaultdict(dict))
        for code, rows in self.hist.items():
            for r in rows:
                if r[0] <= 1:
                    tg[(r[0], r[2])][r[1]][code] = r[5]
        for p in pool:
            for r in current_rows(p, self.before):
                tg[(0, r[2])][r[1]][self.code[id(p)]] = r[5]
        self.tg = tg

    def _league_rate(self):
        pir = mins = 0.0
        for rows in self.games.values():
            for r in rows:
                if r[0] <= 1:
                    pir += r[6]
                    mins += r[5]
        self.lrate = pir / mins if mins else 0.45

    def _rest(self):
        """PIR on short rest (<= 2 days since the player's previous game) vs his season average."""
        import datetime as _dt
        num = den = 0.0
        for rows in self.games.values():
            avgs = self.season_avgs(rows)
            prev = None
            for r in rows:
                d = _dt.date.fromisoformat(r[1][:10])
                if prev and prev[0] == r[0] and 0 < (d - prev[1]).days <= 2 and avgs[r[0]] > 3:
                    num += r[6]
                    den += avgs[r[0]]
                prev = (r[0], d)
        self.rest_f = num / den if den else 1.0

    def team_home(self, club, home):
        """Home/away factor for this club's players (this season + last), shrunk toward the league's."""
        if not self.P["TEAM_HOME"]:
            return self.home_f[1 if home else 0]
        if not hasattr(self, "_th"):
            hs, aw = defaultdict(list), defaultdict(list)
            for rows in self.games.values():
                avgs = self.season_avgs(rows)
                for r in rows:
                    if r[0] <= 1 and avgs[r[0]] > 3:
                        (hs if r[4] else aw)[r[2]].append(r[6] / avgs[r[0]])
            self._th = {}
            for c in set(hs) | set(aw):
                if hs[c] and aw[c]:
                    mh, ma = sum(hs[c]) / len(hs[c]), sum(aw[c]) / len(aw[c])
                    n = min(len(hs[c]), len(aw[c]))
                    k = n / (n + self.P["K_HOME"])
                    fh = (1 - k) * self.home_f[1] + k * 2 * mh / (mh + ma)
                    self._th[c] = {1: fh, 0: 2 - fh}
        return self._th.get(club, self.home_f)[1 if home else 0]

    def rate(self, p):
        """PIR per minute: long memory (it barely changes with role), shrunk toward the league rate."""
        rows = self.games.get(id(p)) or []
        n = len(rows)
        num = den = 0.0
        for i, r in enumerate(rows):
            w = 0.5 ** ((n - 1 - i) / self.P["HL_RATE"]) * self.P["RATE_SW"] ** r[0]
            num += w * r[6]
            den += w * r[5]
        k = self.P["K_RATE"]
        return (num + k * self.lrate) / (den + k)

    def minutes(self, p, absent=None):
        """Recent-weighted minutes, adjusted for teammates who are out next game (or back after being out)."""
        rows = self.games.get(id(p)) or []
        if not rows:
            return None, {}
        n = len(rows)
        w = [0.5 ** ((n - 1 - i) / self.P["HL_MIN"]) * self.P["MIN_SW"] ** r[0] for i, r in enumerate(rows)]
        base = sum(wi * r[5] for wi, r in zip(w, rows)) / sum(w)
        adj, notes = 0.0, []
        if self.P["TEAMMATES"]:
            code, club = self.code[id(p)], p.get("t")
            absent = absent or set()
            cur = [(wi, r) for wi, r in zip(w, rows) if r[0] == 0 and r[2] == club]
            cw = sum(wi for wi, _ in cur)
            roster = self.roster(club)
            for t, d, nwo in self.splits(code, club):
                if t not in roster:  # left the club (or never played for it this season): no return to expect
                    continue
                share = (sum(wi for wi, r in cur if t not in self.tg[(0, club)].get(r[1], {})) / cw) if cw else 0.0
                nxt = 1.0 if t in absent else 0.0
                a = (nxt - share) * d
                if abs(a) >= 0.3:
                    adj += a
                    notes.append((t, round(a, 1)))
        m = max(0.0, min(self.P["MAX_MIN"], base + adj))
        return m, {"min_base": round(base, 1), "min_adj": round(adj, 1), "tm": notes}

    def roster(self, club):
        """Players with TM_MIN_GAMES+ appearances for the club this season."""
        if not hasattr(self, "_ros"):
            self._ros = {}
        if club not in self._ros:
            apps = defaultdict(int)
            for pl in (self.tg.get((0, club)) or {}).values():
                for c in pl:
                    apps[c] += 1
            mins = defaultdict(float)
            for pl in (self.tg.get((0, club)) or {}).values():
                for c, m_ in pl.items():
                    mins[c] += m_
            self._ros[club] = {c for c, k in apps.items()
                               if k >= self.P["TM_MIN_GAMES"] and mins[c] / k >= self.P["TM_KEY_MIN"]}
        return self._ros[club]

    def avg_minutes(self, club):
        """Average minutes this season per player of the club (key teammates have 18+)."""
        tot, k = defaultdict(float), defaultdict(int)
        for pl in (self.tg.get((0, club)) or {}).values():
            for c, m_ in pl.items():
                tot[c] += m_
                k[c] += 1
        return {c: tot[c] / k[c] for c in tot}

    def splits(self, code, club):
        """For each teammate: (code, shrunk change in this player's minutes when that teammate doesn't play, games)."""
        if not hasattr(self, "_spl"):
            self._spl = {}
        key = (code, club)
        if key in self._spl:
            return self._spl[key]
        acc = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # with: w*min, w; without: w*min, w
        for rank, sw in ((0, 1.0), (1, 0.5)):
            games = self.tg.get((rank, club)) or {}
            if not any(code in pl for pl in games.values()):
                continue
            apps = defaultdict(int)
            for pl in games.values():
                for c in pl:
                    apps[c] += 1
            mates = [c for c, k in apps.items() if k >= self.P["TM_MIN_GAMES"] and c != code]
            for pl in games.values():
                if code not in pl:
                    continue
                for t in mates:
                    s = acc[t]
                    if t in pl:
                        s[0] += sw * pl[code]
                        s[1] += sw
                    else:
                        s[2] += sw * pl[code]
                        s[3] += sw
        out = []
        for t, (a, b, c, d) in acc.items():
            if b > 0 and d > 0:
                out.append((t, (c / d - a / b) * d / (d + self.P["K_TM"]), round(d, 1)))
        self._spl[key] = out
        return out

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

    def project(self, p, opp, home, site=None, absent=None, short_rest=False):
        """Projected PIR for one game. site: the site's projection for it (prior for thin samples).
        absent: codes of teammates who won't play. short_rest: second game within 2 days."""
        hf = self.team_home(p.get("t"), home)
        prior = None
        if site is not None:  # strip the site's own venue/opponent to get a neutral prior
            prior = site / (self.def_f.get(opp, 1.0) * hf)
        info = {}
        if self.P["MINRATE"] and self.games.get(id(p)):
            mins, info = self.minutes(p, absent)
            rt = self.rate(p)
            base = rt * mins
            if prior is not None:
                sw = sum(self.weights(self.games[id(p)]))
                k = self.P["PRIOR_GAMES"]
                base = (sw * base + k * prior) / (sw + k)
            info.update({"rate": round(rt, 3), "min": round(mins, 1)})
        else:
            base, _ = self.baseline(p, prior)
        if base is None:
            return None, {}
        f_h2h, n, avg = self.h2h(p, opp)
        d = self.def_f.get(opp, 1.0)
        r = self.rest_f if (self.P["REST"] and short_rest) else 1.0
        v = base * d * f_h2h * hf * r
        return v, {**info, "base": round(base, 1), "def": round(d, 3), "h2h": round(f_h2h, 3), "h2h_n": n,
                   "h2h_avg": avg, "home": round(hf, 3), "rest": round(r, 3)}

def apply(pool, log=print):
    """Replace each player's nx/nxe projections (index 3) with ours; keep the site's in nx_site/nxe_site.
    Players the site projects at 0 (injured/out) stay at 0; overrides applied later still win."""
    m = Model(pool)
    # teammates who won't play: injured/out (site status or overrides), or projected at 0 by the site for that round
    out_codes = defaultdict(set)
    for q in pool:
        c = str(q.get("c") or "").strip()
        if c and q.get("status") in ("injured", "out"):
            out_codes[(q.get("t"), None)].add(c)
        for e in (q.get("nx") or []) + (q.get("nxe") or []):
            if c and not e[3]:
                out_codes[(q.get("t"), e[0])].add(c)
    names = {str(q.get("c") or "").strip(): q.get("name") for q in pool}
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
                    absent = out_codes[(p.get("t"), None)] | out_codes[(p.get("t"), e[0])]
                    v, info = m.project(p, e[1], e[2], site, absent=absent, short_rest=bool(e[5]) if len(e) > 5 else False)
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
                            if info.get("tm"):
                                info["tm"] = [[names.get(c, c), a] for c, a in info["tm"]]
                            code = m.code[id(p)]
                            ros = m.roster(p.get("t"))
                            am = m.avg_minutes(p.get("t"))
                            mates = sorted(((t, d, n_) for t, d, n_ in m.splits(code, p.get("t"))
                                            if t in ros and n_ >= 3 and am.get(t, 0) >= 18),
                                           key=lambda x: -abs(x[1]))[:3]
                            info["mates"] = [[names.get(t, t), round(d, 1), n_] for t, d, n_ in mates if abs(d) >= 1.0]
                            p["model"] = {**info, "opp": e[1], "site": site, "ours": round(v, 1)}
                        e[3] = round(v, 1)
                        n += 1
                new.append(e)
            p[key] = new
    log(f"  model: {n} round projections replaced (home edge {m.home_f[1]:.3f}/{m.home_f[0]:.3f})")
    return m
