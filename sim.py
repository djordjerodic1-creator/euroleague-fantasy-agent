"""Outcome simulator + 4-trade optimiser aimed at a points target (e.g. 200).

Projections are averages; reaching a high target needs upside. This module models each
player's whole range of outcomes (spread taken from the site's P10-P90 band), correlates
players in the same game (a team that blows its opponent out lifts its own players, sinks
the other side and drives the coach score), and estimates P(total >= target).

  python sim.py [YYYY-MM-DD]      -> data/<date>/optimizer.json + printed top plans

Used by render.py (expected / ceiling / chance-of-target for each variation) and by the
analyst as a starting point: the optimiser proposes, the analyst still judges.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORMS = {"2-2-1": (2, 2, 1), "1-2-2": (1, 2, 2), "2-1-2": (2, 1, 2), "1-3-1": (1, 3, 1), "3-1-1": (3, 1, 1)}
POS = ("Guard", "Forward", "Center")
GAME_SD = 12.0


def price_buffer(D):
    """Credits to keep spare while prices are a round old: estimated prices can still be ~0.1 off per player."""
    pr = D.digest.get("prices") or {}
    if not pr.get("stale") or (pr.get("app_prices") or 0) >= 200:  # exact prices from the official game
        return 0.0
    return 0.3 if pr.get("estimated") or pr.get("app_prices") else 0.6      # sd of the final margin around the projected margin
TEAM_LOAD = 0.35    # how much a player's night moves with his team's night
COACH_BANDS = [(-1e9, -20.5, -20), (-20.5, -10.5, -10), (-10.5, 0, -5), (0, 10.5, 10), (10.5, 20.5, 20), (20.5, 1e9, 25)]


def coach_pts(m):
    for a, b, p in COACH_BANDS:
        if a <= m < b:
            return p
    return 0


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def spread(p):
    """Per-player sd of PIR for the target round."""
    m = p.get("proj") or 0.0
    ratio = 0.72
    if p.get("p10") is not None and p.get("p90") is not None and p.get("nx"):
        base = p["nx"][0][3] or 0
        if base > 3:
            ratio = min(1.0, max(0.45, (p["p90"] - p["p10"]) / 2.56 / base))
    return max(3.0, ratio * max(m, 4.0))


def coach_moments(g):
    """Mean and sd of coach points from the projected margin."""
    if not g:
        return 0.0, 0.0
    mu = g.get("margin", 0)
    cdf = lambda x: phi((x - mu) / GAME_SD)
    probs = [(cdf(b) - cdf(a), p) for a, b, p in COACH_BANDS]
    mean = sum(q * p for q, p in probs)
    var = sum(q * (p - mean) ** 2 for q, p in probs)
    return mean, math.sqrt(var)


# ------------------------------------------------------------------ lineup choice (analytic, fast)
def best_lineup(players, coach_g, target):
    """Pick formation, starters, 6th man, bench and captain to maximise P(total >= target),
    using a normal approximation (independent players). Returns dict."""
    cm, cs = coach_moments(coach_g)
    by = {k: sorted([p for p in players if p["pos"] == k], key=lambda p: -p["_m"]) for k in POS}
    best = None
    for f, need in FORMS.items():
        if any(len(by[k]) < n for k, n in zip(POS, need)):
            continue
        st = [p for k, n in zip(POS, need) for p in by[k][:n]]
        rest = sorted([p for p in players if p not in st], key=lambda p: -p["_m"])
        sixth, bench = rest[0], rest[1:]
        base_m = sum(p["_m"] for p in st) + sixth["_m"] + 0.5 * sum(p["_m"] for p in bench) + cm
        base_v = sum(p["_s"] ** 2 for p in st) + sixth["_s"] ** 2 + 0.25 * sum(p["_s"] ** 2 for p in bench) + cs ** 2
        for c in st:  # captain adds one more full copy -> mean +m, variance (2s)^2 - s^2
            m = base_m + c["_m"]
            v = base_v + 3 * c["_s"] ** 2
            prob = 1 - phi((target - m) / math.sqrt(v))
            if not best or prob > best["p"] + 1e-12 or (abs(prob - best["p"]) < 1e-12 and m > best["mean"]):
                best = {"p": prob, "mean": m, "sd": math.sqrt(v), "formation": f, "starters": st,
                        "sixth": sixth, "bench": bench, "captain": c}
    return best


# ------------------------------------------------------------------ nights (turns) and swaps between them
def _turn(p):
    return ((p.get("game") or {}).get("turn")) or 0


def plan_subs(starters, sixth, bench, captain, games):
    """The planned swaps after the first night: each first-night active (not the captain), weakest first, is paired with
    the best later-night bench player who can replace him (valid formation). Swap if the first-night player scores
    under the bench player's expected score. Returns [(out_player, in_player, threshold)]."""
    allp = list(starters) + ([sixth] if sixth else []) + list(bench)
    nights = sorted({_turn(p) for p in allp if _turn(p)})
    if len(nights) < 2:
        return []
    first = nights[0]
    pos = {k: sum(1 for p in starters if p["pos"] == k) for k in POS}
    in_five = {p["name"] for p in starters}
    pool = sorted([b for b in bench if _turn(b) > first], key=lambda b: -expected_pts(b, games))
    acts = [a for a in list(starters) + ([sixth] if sixth else []) if _turn(a) == first and a["name"] != captain["name"]]
    out = []
    for a in sorted(acts, key=lambda a: expected_pts(a, games)):
        for b in pool:
            if a["name"] in in_five:
                c = dict(pos)
                c[a["pos"]] -= 1
                c[b["pos"]] += 1
                if tuple(c[k] for k in POS) not in FORMS.values():
                    continue
                pos = c
            out.append((a, b, expected_pts(b, games)))
            pool.remove(b)
            break
    return out


def _below(mu, m, s):
    """E[(mu - X)^+] for X ~ N(m, s): the expected rescue when X falls below mu."""
    s = max(s, 1.0)
    d = (mu - m) / s
    return s * math.exp(-d * d / 2) / math.sqrt(2 * math.pi) + (mu - m) * phi(d)


def option_gain(starters, sixth, bench, captain, games):
    """Expected extra points from the later-night swaps and the armband move (normal approximation)."""
    g = 0.0
    for a, b, mu in plan_subs(starters, sixth, bench, captain, games):
        g += 0.5 * _below(mu, expected_pts(a, games), a["_s"])  # 0.5*(Y - X) when X < E[Y]
    nights = sorted({_turn(p) for p in starters if _turn(p)})
    if nights and _turn(captain) == nights[0] and len(nights) > 1:
        later = [p for p in starters if _turn(p) > nights[0]]
        if later:
            mu = max(expected_pts(p, games) for p in later)
            g += _below(mu, expected_pts(captain, games), captain["_s"])
    return g


def night_split(starters, sixth, bench, captain=None, games=None):
    """How a lineup splits over the round's nights, and the swaps it allows.

    The official game lets you move a player who has already played out of the starting five / 6th-man slot after his
    night, and bring in a bench player who hasn't played yet. So the useful shape is: first-night actives backed by
    stronger later-night bench players, who come in when a first-night player has a quiet night. Balanced means each
    night has a fair share of the 10 (at least 4 of 10 over two nights), at least one starter on each side (early
    captain, armband switch), and every first-night active player has a later-night bench player who can replace him.
    Returns None for a one-night round."""
    allp = list(starters) + ([sixth] if sixth else []) + list(bench)
    nights = sorted({_turn(p) for p in allp if _turn(p)})
    if len(nights) < 2:
        return None
    first = nights[0]
    cnt = {n: sum(1 for p in allp if _turn(p) == n) for n in nights}
    st_cnt = {n: sum(1 for p in starters if _turn(p) == n) for n in nights}
    st_pos = {k: sum(1 for p in starters if p["pos"] == k) for k in POS}
    late_bench = [b for b in bench if _turn(b) > first]
    swaps = {}
    for a in list(starters) + ([sixth] if sixth else []):
        opts = []
        for b in late_bench:
            if _turn(b) < _turn(a):
                continue
            if a is sixth:
                ok = True
            else:
                c = dict(st_pos)
                c[a["pos"]] -= 1
                c[b["pos"]] = c.get(b["pos"], 0) + 1
                ok = tuple(c[k] for k in POS) in FORMS.values()
            if ok:
                opts.append(b["name"])
        swaps[a["name"]] = opts
    floor = max(1, len(allp) // len(nights) - 1)
    reasons = []
    for n in nights:
        if cnt[n] < floor:
            reasons.append(f"only {cnt[n]} of {len(allp)} play on night {n}")
    if not st_cnt[first]:
        reasons.append("no starter on the first night (no early captain)")
    if not any(st_cnt[n] for n in nights[1:]):
        reasons.append("no starter on a later night (no armband switch)")
    uncovered = [a for a, o in swaps.items() if not o and _turn(next(p for p in allp if p["name"] == a)) == first]
    if uncovered:
        reasons.append("no later-night bench player to replace " + ", ".join(n.split(" ", 1)[-1] for n in uncovered))
    plan = plan_subs(starters, sixth, bench, captain, games) if captain is not None and games is not None else []
    return {"nights": nights, "count": cnt, "starters": st_cnt, "swaps": swaps, "balanced": not reasons,
            "reasons": reasons, "plan": [(a["name"], b["name"], round(mu, 1)) for a, b, mu in plan]}


def balanced_lineup(players, coach_g, target, slack=0.01, games=None):
    """The best lineup among those balanced over the nights, valuing the later-night swap option (option_gain) when
    `games` is given; falls back to best_lineup when nothing balanced is within `slack` of its P(target)."""
    from itertools import combinations
    top = best_lineup(players, coach_g, target)
    if not top or night_split(top["starters"], top["sixth"], top["bench"]) is None:
        return top
    games = games or {}
    cm, cs = coach_moments(coach_g)
    by = {k: [p for p in players if p["pos"] == k] for k in POS}
    best = None
    for f, need in FORMS.items():
        if any(len(by[k]) < n for k, n in zip(POS, need)):
            continue
        for g in combinations(by["Guard"], need[0]):
            for fw in combinations(by["Forward"], need[1]):
                for ce in combinations(by["Center"], need[2]):
                    st = list(g) + list(fw) + list(ce)
                    rest = [p for p in players if p not in st]
                    for sixth in rest:
                        bench = [p for p in rest if p is not sixth]
                        ns = night_split(st, sixth, bench)
                        if not ns["balanced"]:
                            continue
                        base_m = sum(p["_m"] for p in st) + sixth["_m"] + 0.5 * sum(p["_m"] for p in bench) + cm
                        base_v = sum(p["_s"] ** 2 for p in st) + sixth["_s"] ** 2 + 0.25 * sum(p["_s"] ** 2 for p in bench) + cs ** 2
                        for c in st:
                            m = base_m + c["_m"] + (option_gain(st, sixth, bench, c, games) if games else 0.0)
                            v = base_v + 3 * c["_s"] ** 2
                            prob = 1 - phi((target - m) / math.sqrt(v))
                            if not best or prob > best["p"] + 1e-12:
                                best = {"p": prob, "mean": m, "sd": math.sqrt(v), "formation": f, "starters": st,
                                        "sixth": sixth, "bench": bench, "captain": c}
    top_p = top["p"]
    if games:  # compare like with like: the unrestricted lineup gets its swap option too
        m = top["mean"] + option_gain(top["starters"], top["sixth"], top["bench"], top["captain"], games)
        top_p = 1 - phi((target - m) / top["sd"])
    return best if best and best["p"] >= top_p - slack else top


# ------------------------------------------------------------------ Monte Carlo (correlated)
def expected_pts(p, games):
    """Expected fantasy points of one player (projection plus the +10% win bonus at his win probability)."""
    return p["_m"] * (1 + 0.1 * ((games.get(p.get("t")) or {}).get("pwin", 0.5)))


def later_subs(starters, sixth, bench, captain, score, games):
    """The swaps made after the first night, given each first-night player's real score.

    Rules (the official game): between nights a player who has already played can be moved out of the starting five
    or the 6th-man slot, and only a player who hasn't played yet can come in. The armband can move to a starter who
    hasn't played yet. You decide on what you know, so a later-night player's value is his expected score.
    Returns (mult per player name, list of (out, in) swaps, new captain name)."""
    allp = list(starters) + ([sixth] if sixth else []) + list(bench)
    nights = sorted({_turn(p) for p in allp if _turn(p)})
    mult = {p["name"]: 1.0 for p in starters}
    if sixth:
        mult[sixth["name"]] = 1.0
    for p in bench:
        mult[p["name"]] = 0.5
    if len(nights) < 2:
        mult[captain["name"]] = 2.0
        return mult, [], captain["name"]
    first = nights[0]
    exp = {p["name"]: expected_pts(p, games) for p in allp}
    pos = {k: sum(1 for p in starters if p["pos"] == k) for k in POS}
    in_five = {p["name"] for p in starters}
    pool = sorted([b for b in bench if _turn(b) > first], key=lambda b: -exp[b["name"]])
    played = [a for a in list(starters) + ([sixth] if sixth else [])
              if _turn(a) == first and a["name"] != captain["name"]]
    swaps = []
    for a in sorted(played, key=lambda a: score[a["name"]]):
        for b in pool:
            if exp[b["name"]] <= score[a["name"]]:
                break  # pool is sorted: nobody left is worth more than what he already scored
            if a["name"] in in_five:
                c = dict(pos)
                c[a["pos"]] -= 1
                c[b["pos"]] += 1
                if tuple(c[k] for k in POS) not in FORMS.values():
                    continue
                pos = c
                in_five.discard(a["name"])
                in_five.add(b["name"])
            mult[a["name"]], mult[b["name"]] = 0.5, 1.0
            swaps.append((a["name"], b["name"]))
            pool.remove(b)
            break
    cap = captain["name"]
    if _turn(captain) == first:
        later = [p for p in allp if p["name"] in in_five and _turn(p) > first]
        if later:
            l = max(later, key=lambda p: exp[p["name"]])
            if exp[l["name"]] > score[cap]:
                cap = l["name"]
    mult[cap] = mult.get(cap, 1.0) * 2.0
    return mult, swaps, cap


def simulate(starters, sixth, bench, captain, coach_code, games, target, n=4000, seed=7, subs=True):
    """Monte Carlo of the round. With subs (default), after the first night the best later-night bench players replace
    first-night actives who scored below the bench player's expected score, and the armband moves to a later starter
    when the first-night captain scored below that starter's expected score (see later_subs)."""
    rnd = random.Random(seed)
    lineup = list(starters) + ([sixth] if sixth else []) + list(bench)
    gkeys = sorted({tuple(sorted((t, g["opp"]))) for t, g in games.items()})
    totals = []
    for _ in range(n):
        z = {}
        for a, b in gkeys:
            zz = rnd.gauss(0, 1)
            z[a], z[b] = zz, -zz  # same game: one team's good night is the other's bad night
        score = {}
        for p in lineup:
            t = p.get("t")
            g = games.get(t)
            tz = z.get(t, 0.0)
            pir = p["_m"] + p["_s"] * (TEAM_LOAD * tz + math.sqrt(1 - TEAM_LOAD ** 2) * rnd.gauss(0, 1))
            pir = max(pir, -6.0)
            won = g is not None and (g.get("margin", 0) + GAME_SD * tz) > 0
            score[p["name"]] = pir * (1.1 if won else 1.0)
        if subs:
            mult, _, _ = later_subs(starters, sixth, bench, captain, score, games)
        else:
            mult = {p["name"]: 1.0 for p in starters}
            if sixth:
                mult[sixth["name"]] = 1.0
            mult.update({p["name"]: 0.5 for p in bench})
            mult[captain["name"]] = 2.0
        tot = sum(score[p["name"]] * mult[p["name"]] for p in lineup)
        g = games.get(coach_code)
        if g:
            tot += coach_pts(g.get("margin", 0) + GAME_SD * z.get(coach_code, 0.0))
        totals.append(tot)
    totals.sort()
    return {"mean": round(sum(totals) / n, 1), "p90": round(totals[int(0.9 * n)], 1),
            "p10": round(totals[int(0.1 * n)], 1), "p_target": round(sum(t >= target for t in totals) / n, 3)}


# ------------------------------------------------------------------ optimiser
def prep(p):
    p = dict(p)
    p["_m"] = p.get("proj") or 0.0
    p["_s"] = spread(p)
    return p


def optimise(D, team, coach_prices, target, trades=4, beam=80, per_pos=30):
    from render import coach_code_of  # local import to avoid a cycle at module load
    team_now = team
    buf = price_buffer(D)
    rs = team.get("round_start") or {}
    if rs.get("round") == D.target and rs.get("players"):
        # trades already made this round can still be changed: plan all 4 trades from the pre-round squad
        team = {**team, "players": rs["players"], "coach": rs.get("coach", team.get("coach")), "bank": rs.get("bank", team.get("bank"))}
    squad = [prep(D.player(n)) for n in team["players"]]
    coach = coach_code_of(team.get("coach", ""))
    bank = float(team.get("bank") or 0)
    games = D.games

    pool = []
    for q in D.pool:
        p = D.player(q["name"])
        if p.get("missing") or p.get("status") in ("injured", "out") or not p.get("price") or p.get("proj", 0) <= 0:
            continue
        pool.append(prep(p))
    cands = []
    for pos in POS:
        lst = [p for p in pool if p["pos"] == pos]
        cands += sorted(lst, key=lambda p: -(p["_m"] + 1.28 * p["_s"]))[:per_pos]
        cands += sorted([p for p in lst if p["price"] <= 6.5], key=lambda p: -p["_m"] / p["price"])[:8]
        cands += [p for p in lst if p.get("rj")]  # role just grew: projections tend to lag
    seen, uniq = set(), []
    for p in cands:
        if p["name"] not in seen:
            seen.add(p["name"])
            uniq.append(p)
    cands = uniq

    def score(sq, c):
        bl = best_lineup(sq, games.get(c), target)
        return bl["p"] if bl else -1, bl

    def clubs_ok(sq):
        cnt = {}
        for p in sq:
            cnt[p["t"]] = cnt.get(p["t"], 0) + 1
        return max(cnt.values()) <= 6

    start = (tuple(sorted(p["name"] for p in squad)), coach)
    states = {start: (squad, coach, bank, [], set(p["name"] for p in squad))}
    frontier = [start]
    results = []
    for depth in range(trades):
        nxt = {}
        for key in frontier:
            sq, c, b, moves, ever = states[key]
            names = {p["name"] for p in sq}
            for i, out in enumerate(sq):
                for inn in cands:
                    if inn["pos"] != out["pos"] or inn["name"] in names or inn["name"] in ever:
                        continue
                    nb = round(b + out["price"] - inn["price"], 1)
                    if nb < buf - 0.001:
                        continue
                    nsq = sq[:i] + [inn] + sq[i + 1:]
                    if not clubs_ok(nsq):
                        continue
                    k = (tuple(sorted(p["name"] for p in nsq)), c)
                    if k in nxt or k in states:
                        continue
                    nxt[k] = (nsq, c, nb, moves + [(out["name"], out["price"], inn["name"], inn["price"])], ever | {inn["name"]})
            for nc, price in coach_prices.items():
                if nc == c:
                    continue
                nb = round(b + coach_prices.get(c, 0) - price, 1)
                if nb < buf - 0.001:
                    continue
                k = (key[0], nc)
                if k in nxt or k in states:
                    continue
                nxt[k] = (sq, nc, nb, moves + [(f"Coach {c}", coach_prices.get(c, 0), f"Coach {nc}", price)], ever)
        scored = sorted(((score(v[0], v[1])[0], k) for k, v in nxt.items()), reverse=True)[:beam]
        frontier = [k for _, k in scored]
        for _, k in scored:
            states[k] = nxt[k]
        if depth == trades - 1:
            results = [states[k] for k in frontier]
    def plan(sq, c, b, moves, bl):
        mc = simulate(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], c, games, target)
        ns = night_split(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], games)
        return {"trades": [{"out": o, "out_price": op, "in": i, "in_price": ip} for o, op, i, ip in moves],
                "coach": c, "bank_after": b, "formation": bl["formation"],
                "starters": [p["name"] for p in bl["starters"]], "sixth": bl["sixth"]["name"],
                "bench": [p["name"] for p in bl["bench"]], "captain": bl["captain"]["name"], **mc,
                "nights": ({"count": ns["count"], "starters": ns["starters"], "balanced": ns["balanced"],
                            "reasons": ns["reasons"], "plan": ns["plan"]} if ns else None)}

    out = []
    for sq, c, b, moves, _ in results[:20]:
        out.append(plan(sq, c, b, moves, balanced_lineup(sq, games.get(c), target, games=games)))
    out.sort(key=lambda r: (-r["p_target"], -r["mean"]))
    # plans whose squad AND lineup are balanced over the round's nights (so you can sub between them), from the
    # whole final beam: these can sit a little lower on P(target) than the top plans
    bal = []
    for sq, c, b, moves, _ in results:
        bl = balanced_lineup(sq, games.get(c), target, slack=1.0, games=games)
        ns = night_split(bl["starters"], bl["sixth"], bl["bench"])
        if ns and ns["balanced"]:
            bal.append((bl["p"], sq, c, b, moves, bl))
    bal.sort(key=lambda x: -x[0])
    balanced_plans = sorted((plan(sq, c, b, moves, bl) for _, sq, c, b, moves, bl in bal[:15]),
                            key=lambda r: (-r["p_target"], -r["mean"]))[:5]
    base_bl = best_lineup(squad, games.get(coach), target)
    base = simulate(base_bl["starters"], base_bl["sixth"], base_bl["bench"], base_bl["captain"], coach, games, target)
    res = {"target": target, "current_best_lineup": base, "plans": out[:10], "balanced_plans": balanced_plans}
    if rs.get("round") == D.target:
        # the squad with the trades you've made so far, for comparison
        now = [prep(D.player(n)) for n in team_now["players"]]
        cc = coach_code_of(team_now.get("coach", ""))
        bl = best_lineup(now, games.get(cc), target)
        res["pre_round_squad"] = True
        res["with_your_trades"] = {**simulate(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], cc, games, target),
                                   "captain": bl["captain"]["name"]}
        mine = set(team_now["players"])
        for pl in res["plans"]:
            pl["kept_your_trades"] = sum(1 for t in pl["trades"] if t["in"] in mine or t["in"] == f"Coach {cc}")
    return res


# ------------------------------------------------------------------ unlimited-window rebuild planner
from statistics import NormalDist as _ND


def window_round(D):
    """First round within the next 3 whose trades are unlimited beforehand (the rebuild window), else None."""
    cal = D.digest.get("calendar", {})
    for r in range(D.target, D.target + 4):
        c = cal.get(str(r)) or {}
        if c.get("unlimited_before"):
            return r, c.get("unlimited_window")
    return None, None


def round_games(sched, rd):
    """Per-team game info for a future round from the site schedule (win % and projected score)."""
    out = {}
    # rounds the site hasn't priced yet: estimate from season point difference per game + home court
    net = {r[1]: (r[4] - r[5]) / max(1, r[2] + r[3]) for r in sched.get("standings", [])}
    for g in sched.get("games", []):
        if g["rd"] != rd or g.get("res"):
            continue
        if g.get("pw") is None:
            if g["h"] not in net or g["a"] not in net:
                continue
            m = (net[g["h"]] - net[g["a"]]) * 0.6 + 3.0
            pw = min(max(_ND().cdf(m / GAME_SD), 0.01), 0.99)
        else:
            pw = min(max(g["pw"], 0.01), 0.99)
            m = (g["proj"][0] - g["proj"][1]) if g.get("proj") else GAME_SD * _ND().inv_cdf(pw)
        if g.get("mkt"):  # bookmaker spread: 70% market, 30% model
            m = 0.7 * (-g["mkt"]["spread"]) + 0.3 * m
            pw = min(max(_ND().cdf(m / GAME_SD), 0.01), 0.99)
        out[g["h"]] = {"opp": g["a"], "home": True, "pwin": pw, "margin": m}
        out[g["a"]] = {"opp": g["h"], "home": False, "pwin": 1 - pw, "margin": -m}
    return out


def prep_round(p, rd):
    e = next((x for x in (p.get("nx") or []) + (p.get("nxe") or []) if x[0] == rd), None)
    q = dict(p)
    q["proj"] = e[3] if e else 0.0
    q["_m"] = q["proj"]
    q["_s"] = spread(q) if q["proj"] > 0 else 0.5
    return q


def rebuild(D, team, coach_prices, target, sched, rounds, n_plans=3):
    """Best 10 + coach from scratch for an unlimited trade window, judged over several rounds:
    objective = average P(round >= target) across `rounds` (analytic), expected points as tie-break."""
    from render import coach_code_of
    RG = {rd: round_games(sched, rd) for rd in rounds}
    cur = [D.player(n) for n in team["players"]]
    cur_coach = coach_code_of(team.get("coach", ""))
    budget = round(sum(p.get("price") or 0 for p in cur) + coach_prices.get(cur_coach, 0) + float(team.get("bank") or 0), 1)
    if team.get("total_budget") and team.get("bank_known", True):
        budget = float(team["total_budget"])  # the in-app figure the user confirmed beats site prices
    budget = round(budget - price_buffer(D), 1)

    pool = {}
    for q in D.pool:
        p = D.player(q["name"])
        if p.get("missing") or not p.get("price") or p.get("status") in ("injured", "out"):
            continue
        pool[p["name"]] = p
    R = {(n, rd): prep_round(p, rd) for n, p in pool.items() for rd in rounds}
    ceil = {n: sum(R[(n, rd)]["_m"] + 1.28 * R[(n, rd)]["_s"] for rd in rounds) / len(rounds) for n in pool}
    mean_ = {n: sum(R[(n, rd)]["_m"] for rd in rounds) / len(rounds) for n in pool}
    cands = {}
    for pos in POS:
        lst = [n for n in pool if pool[n]["pos"] == pos]
        top = sorted(lst, key=lambda n: -ceil[n])[:26]
        val = sorted([n for n in lst if pool[n]["price"] <= 7.5], key=lambda n: -mean_[n] / pool[n]["price"])[:10]
        rj = [n for n in lst if pool[n].get("rj")][:6]
        cands[pos] = list(dict.fromkeys(top + val + rj))
    price = {n: pool[n]["price"] for n in pool}

    def score(sq, c):
        ps, ms = [], []
        for rd in rounds:
            bl = best_lineup([R[(n, rd)] for n in sq], RG[rd].get(c), target)
            if not bl:
                return -1.0, 0.0
            ps.append(bl["p"])
            ms.append(bl["mean"])
        return sum(ps) / len(ps), sum(ms) / len(ms)

    obj = lambda s: s[0] + 0.0015 * s[1]

    def ok(sq, cap):
        if sum(price[n] for n in sq) > cap + 1e-6:
            return False
        cnt = {}
        for n in sq:
            cnt[pool[n]["t"]] = cnt.get(pool[n]["t"], 0) + 1
        return max(cnt.values()) <= 6

    def climb(sq, c, cap):
        sq = list(sq)
        best = obj(score(sq, c))
        for _ in range(40):
            improved = None
            # 1-swaps
            for i, out in enumerate(sq):
                for inn in cands[pool[out]["pos"]]:
                    if inn in sq:
                        continue
                    nsq = sq[:i] + [inn] + sq[i + 1:]
                    if not ok(nsq, cap):
                        continue
                    v = obj(score(nsq, c))
                    if v > best + 1e-9 and (not improved or v > improved[0]):
                        improved = (v, nsq)
            if not improved:
                # 2-swaps: one upgrade funded by one downgrade
                for i, o1 in enumerate(sq):
                    ups = [n for n in sorted(cands[pool[o1]["pos"]], key=lambda n: -ceil[n])[:12] if n not in sq and price[n] > price[o1]]
                    for a in ups:
                        for j, o2 in enumerate(sq):
                            if j == i:
                                continue
                            downs = [n for n in sorted(cands[pool[o2]["pos"]], key=lambda n: price[n])[:14] if n not in sq and n != a and price[n] < price[o2]]
                            for b in downs:
                                nsq = list(sq)
                                nsq[i], nsq[j] = a, b
                                if not ok(nsq, cap):
                                    continue
                                v = obj(score(nsq, c))
                                if v > best + 1e-9 and (not improved or v > improved[0]):
                                    improved = (v, nsq)
            if not improved:
                break
            best, sq = improved
        return sq, best

    def repair(sq, c, cap):
        sq = list(sq)
        while not ok(sq, cap):
            opts = []
            for i, out in enumerate(sq):
                for inn in cands[pool[out]["pos"]]:
                    if inn in sq or price[inn] >= price[out]:
                        continue
                    nsq = sq[:i] + [inn] + sq[i + 1:]
                    saved = price[out] - price[inn]
                    opts.append(((obj(score(sq, c)) - obj(score(nsq, c))) / saved, nsq))
            if not opts:
                return None
            sq = min(opts, key=lambda x: x[0])[1]
        return sq

    def greedy():
        sq = []
        for pos, k in zip(POS, (4, 4, 2)):
            sq += sorted(cands[pos], key=lambda n: -ceil[n])[:k]
        return sq

    # coaches worth trying: best average coach points over the rounds
    def coach_mean(c):
        return sum(coach_moments(RG[rd].get(c))[0] for rd in rounds) / len(rounds)
    coaches = sorted(coach_prices, key=lambda c: -coach_mean(c))[:4]
    if cur_coach and cur_coach not in coaches:
        coaches.append(cur_coach)

    results = []
    cur_names = [p["name"] for p in cur if not p.get("missing") and p["name"] in pool]
    for c in coaches:
        cap = budget - coach_prices.get(c, 0)
        starts = [greedy()]
        if len(cur_names) == 10:
            starts.append(cur_names)
        for st in starts:
            sq = repair(st, c, cap)
            if not sq:
                continue
            sq, v = climb(sq, c, cap)
            results.append((v, tuple(sorted(sq)), c))
    uniq = {}
    for v, sq, c in sorted(results, reverse=True):
        uniq.setdefault((sq, c), v)
    plans = []
    for (sq, c), v in list(uniq.items())[:n_plans]:
        per = []
        for rd in rounds:
            bl = best_lineup([R[(n, rd)] for n in sq], RG[rd].get(c), target)
            mc = simulate(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], c, RG[rd], target)
            per.append({"rd": rd, "formation": bl["formation"], "starters": [p["name"] for p in bl["starters"]],
                        "sixth": bl["sixth"]["name"], "bench": [p["name"] for p in bl["bench"]],
                        "captain": bl["captain"]["name"], **mc})
        cost = round(sum(price[n] for n in sq) + coach_prices.get(c, 0), 1)
        plans.append({"players": list(sq), "coach": c, "cost": cost, "bank_after": round(budget - cost, 1),
                      "sell": [n for n in cur_names if n not in sq], "buy": [n for n in sq if n not in cur_names],
                      "coach_change": c != cur_coach,
                      "avg_p_target": round(sum(x["p_target"] for x in per) / len(per), 3),
                      "avg_mean": round(sum(x["mean"] for x in per) / len(per), 1), "rounds": per})
    plans.sort(key=lambda p: (-p["avg_p_target"], -p["avg_mean"]))
    # the current squad over the same rounds, for comparison
    base = []
    if len(cur_names) == 10:
        for rd in rounds:
            bl = best_lineup([R[(n, rd)] for n in cur_names], RG[rd].get(cur_coach), target)
            base.append({"rd": rd, **simulate(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], cur_coach, RG[rd], target)})
    return {"budget": budget, "rounds": rounds, "current": base, "plans": plans}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from render import Data
    date = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    D = Data(date)
    from paths import load_team, data_dir
    team = load_team()
    target = float(team.get("target_points", 200))
    proj = json.loads((data_dir(date) / "projections.json").read_text(encoding="utf-8"))
    coach_prices = {c["t"]: c["price"] for c in proj.get("coaches", [])}
    if not team.get("players"):
        # brand-new team: pick the best 10 + coach from scratch for the rounds before the next unlimited window
        from render import load_schedule
        rounds = D.digest.get("horizon") or [D.target]
        res = rebuild(D, team, coach_prices, target, load_schedule(date), rounds, n_plans=5)
        res.update({"draft": True, "target": target})
        (data_dir(date) / "optimizer.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"New team: best squads from scratch for budget {res['budget']}, judged over rounds {rounds}")
        for i, pl in enumerate(res["plans"], 1):
            r0 = pl["rounds"][0]
            print(f" {i}. avg P {pl['avg_p_target']:.1%} mean {pl['avg_mean']} cost {pl['cost']} bank {pl['bank_after']} "
                  f"coach {pl['coach']} cap {r0['captain']}")
            print(f"    {pl['players']}")
    else:
        res = optimise(D, team, coach_prices, target)
        (data_dir(date) / "optimizer.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print_plans(res, target)
    window_plan(D, team, coach_prices, target, date)


def print_plans(res, target):
    b = res["current_best_lineup"]
    label = "Pre-round squad (before this round's trades)" if res.get("pre_round_squad") else "Current squad"
    print(f"Target {target:.0f}. {label}, best lineup: mean {b['mean']}, top-10% {b['p90']}, P(>=target) {b['p_target']:.1%}")
    if res.get("with_your_trades"):
        w = res["with_your_trades"]
        print(f"With the trades you've made: mean {w['mean']}, top-10% {w['p90']}, P(>=target) {w['p_target']:.1%} (captain {w['captain']})")
    def nights(r):
        n = r.get("nights")
        if not n:
            return ""
        split = "/".join(str(v) for v in n["count"].values())
        return f"  nights {split}{'' if n['balanced'] else ' (unbalanced)'}"

    for i, r in enumerate(res["plans"], 1):
        t = "; ".join(f"{x['out']}->{x['in']}" for x in r["trades"])
        print(f"{i:2}. P {r['p_target']:.1%}  mean {r['mean']}  p90 {r['p90']}  bank {r['bank_after']}  cap {r['captain']}{nights(r)}  | {t}")
    if res.get("balanced_plans"):
        print("Balanced over the round's nights (subs possible between them):")
        for i, r in enumerate(res["balanced_plans"], 1):
            t = "; ".join(f"{x['out']}->{x['in']}" for x in r["trades"])
            print(f" b{i}. P {r['p_target']:.1%}  mean {r['mean']}  bank {r['bank_after']}  cap {r['captain']}{nights(r)}  | {t}")
            print(f"     {r['formation']} {', '.join(r['starters'])} | 6th {r['sixth']} | bench {', '.join(r['bench'])}")
            if (r.get("nights") or {}).get("plan"):
                print("     after night 1: " + "; ".join(f"{a} under {mu:.0f} -> {b}" for a, b, mu in r["nights"]["plan"]))


def window_plan(D, team, coach_prices, target, date):
    """Unlimited trade window coming up: plan the full rebuild over the window round and the two after it."""
    from paths import data_dir
    W, win = window_round(D)
    if "--rebuild" in sys.argv:
        W = int(sys.argv[sys.argv.index("--rebuild") + 1])
    if W:
        from render import load_schedule
        rb = rebuild(D, team, coach_prices, target, load_schedule(date), [W, W + 1, W + 2])
        rb.update({"window_before": W, "window": win})
        (data_dir(date) / "rebuild.json").write_text(json.dumps(rb, ensure_ascii=False, indent=1), encoding="utf-8")
        cur = rb["current"]
        print()
        print(f"Unlimited window before R{W} ({win}). Budget {rb['budget']}. Rounds {rb['rounds']}.")
        if cur:
            print("  current squad: " + " · ".join(f"R{x['rd']} P {x['p_target']:.1%} mean {x['mean']}" for x in cur))
        for i, pl in enumerate(rb["plans"], 1):
            print(f" {i}. avg P {pl['avg_p_target']:.1%} mean {pl['avg_mean']} cost {pl['cost']} bank {pl['bank_after']} coach {pl['coach']}")
            print(f"    sell {pl['sell']}")
            print(f"    buy  {pl['buy']}")


if __name__ == "__main__":
    main()
