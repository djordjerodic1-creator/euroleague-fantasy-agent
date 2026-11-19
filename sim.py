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


def night_split(starters, sixth, bench):
    """How a lineup splits over the round's nights, and which swaps stay possible between them.

    Between nights you can only swap players who haven't played yet, so a later-night starter or 6th man can only
    be replaced by a later-night bench player (and the starting five must stay a valid formation). Balanced means:
    each night has a fair share of the 10 (at least 4 of 10 over two nights), at least one starter plays on the
    first night (early captain) and one on a later night (switch target), and every later-night active player has
    a later-night bench player who can replace him. Returns None for a one-night round."""
    allp = list(starters) + ([sixth] if sixth else []) + list(bench)
    nights = sorted({_turn(p) for p in allp if _turn(p)})
    if len(nights) < 2:
        return None
    n2 = nights[1]
    cnt = {n: sum(1 for p in allp if _turn(p) == n) for n in nights}
    st_cnt = {n: sum(1 for p in starters if _turn(p) == n) for n in nights}
    st_pos = {k: sum(1 for p in starters if p["pos"] == k) for k in POS}
    late_bench = [b for b in bench if _turn(b) >= n2]
    late_active = [a for a in list(starters) + ([sixth] if sixth else []) if _turn(a) >= n2]
    swaps = {}
    for a in late_active:
        opts = []
        for b in late_bench:
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
    if not st_cnt[nights[0]]:
        reasons.append("no starter on the first night (no early captain)")
    if not any(st_cnt[n] for n in nights[1:]):
        reasons.append("no starter on a later night (no armband switch)")
    uncovered = [a for a, o in swaps.items() if not o]
    if uncovered:
        reasons.append("no later-night bench cover for " + ", ".join(n.split(" ", 1)[-1] for n in uncovered))
    return {"nights": nights, "count": cnt, "starters": st_cnt, "swaps": swaps, "balanced": not reasons,
            "reasons": reasons}


def balanced_lineup(players, coach_g, target, slack=0.01):
    """The best lineup (by the same normal approximation as best_lineup) among those that are balanced over the
    nights; falls back to best_lineup when nothing balanced is within `slack` of its P(target)."""
    from itertools import combinations
    top = best_lineup(players, coach_g, target)
    if not top or night_split(top["starters"], top["sixth"], top["bench"]) is None:
        return top
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
                            m, v = base_m + c["_m"], base_v + 3 * c["_s"] ** 2
                            prob = 1 - phi((target - m) / math.sqrt(v))
                            if not best or prob > best["p"] + 1e-12:
                                best = {"p": prob, "mean": m, "sd": math.sqrt(v), "formation": f, "starters": st,
                                        "sixth": sixth, "bench": bench, "captain": c}
    return best if best and best["p"] >= top["p"] - slack else top


# ------------------------------------------------------------------ Monte Carlo (correlated)
def simulate(starters, sixth, bench, captain, coach_code, games, target, n=4000, seed=7):
    rnd = random.Random(seed)
    slots = [(p, 2.0 if p is captain or p["name"] == captain["name"] else 1.0) for p in starters]
    if sixth:
        slots.append((sixth, 1.0))
    slots += [(p, 0.5) for p in bench]
    gkeys = sorted({tuple(sorted((t, g["opp"]))) for t, g in games.items()})
    totals = []
    for _ in range(n):
        z = {}
        for a, b in gkeys:
            zz = rnd.gauss(0, 1)
            z[a], z[b] = zz, -zz  # same game: one team's good night is the other's bad night
        tot = 0.0
        for p, mult in slots:
            t = p.get("t")
            g = games.get(t)
            tz = z.get(t, 0.0)
            pir = p["_m"] + p["_s"] * (TEAM_LOAD * tz + math.sqrt(1 - TEAM_LOAD ** 2) * rnd.gauss(0, 1))
            pir = max(pir, -6.0)
            won = g is not None and (g.get("margin", 0) + GAME_SD * tz) > 0
            tot += pir * (1.1 if won else 1.0) * mult
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
        ns = night_split(bl["starters"], bl["sixth"], bl["bench"])
        return {"trades": [{"out": o, "out_price": op, "in": i, "in_price": ip} for o, op, i, ip in moves],
                "coach": c, "bank_after": b, "formation": bl["formation"],
                "starters": [p["name"] for p in bl["starters"]], "sixth": bl["sixth"]["name"],
                "bench": [p["name"] for p in bl["bench"]], "captain": bl["captain"]["name"], **mc,
                "nights": ({"count": ns["count"], "starters": ns["starters"], "balanced": ns["balanced"],
                            "reasons": ns["reasons"]} if ns else None)}

    out = []
    for sq, c, b, moves, _ in results[:20]:
        out.append(plan(sq, c, b, moves, balanced_lineup(sq, games.get(c), target)))
    out.sort(key=lambda r: (-r["p_target"], -r["mean"]))
    # plans whose squad AND lineup are balanced over the round's nights (so you can sub between them), from the
    # whole final beam: these can sit a little lower on P(target) than the top plans
    bal = []
    for sq, c, b, moves, _ in results:
        bl = balanced_lineup(sq, games.get(c), target, slack=1.0)
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
