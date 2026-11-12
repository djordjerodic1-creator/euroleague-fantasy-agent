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
GAME_SD = 12.0      # sd of the final margin around the projected margin
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
                    if nb < -0.001:
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
                if nb < -0.001:
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
    out = []
    for sq, c, b, moves, _ in results[:20]:
        bl = best_lineup(sq, games.get(c), target)
        mc = simulate(bl["starters"], bl["sixth"], bl["bench"], bl["captain"], c, games, target)
        out.append({"trades": [{"out": o, "out_price": op, "in": i, "in_price": ip} for o, op, i, ip in moves],
                    "coach": c, "bank_after": b, "formation": bl["formation"],
                    "starters": [p["name"] for p in bl["starters"]], "sixth": bl["sixth"]["name"],
                    "bench": [p["name"] for p in bl["bench"]], "captain": bl["captain"]["name"], **mc})
    out.sort(key=lambda r: (-r["p_target"], -r["mean"]))
    base_bl = best_lineup(squad, games.get(coach), target)
    base = simulate(base_bl["starters"], base_bl["sixth"], base_bl["bench"], base_bl["captain"], coach, games, target)
    return {"target": target, "current_best_lineup": base, "plans": out[:10]}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    from render import Data
    date = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    D = Data(date)
    team = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8"))
    target = float(team.get("target_points", 200))
    proj = json.loads((ROOT / "data" / date / "projections.json").read_text(encoding="utf-8"))
    coach_prices = {c["t"]: c["price"] for c in proj.get("coaches", [])}
    res = optimise(D, team, coach_prices, target)
    (ROOT / "data" / date / "optimizer.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    b = res["current_best_lineup"]
    print(f"Target {target:.0f}. Current squad, best lineup: mean {b['mean']}, top-10% {b['p90']}, P(>=target) {b['p_target']:.1%}")
    for i, r in enumerate(res["plans"], 1):
        t = "; ".join(f"{x['out']}->{x['in']}" for x in r["trades"])
        print(f"{i:2}. P {r['p_target']:.1%}  mean {r['mean']}  p90 {r['p90']}  bank {r['bank_after']}  cap {r['captain']}  | {t}")


if __name__ == "__main__":
    main()
