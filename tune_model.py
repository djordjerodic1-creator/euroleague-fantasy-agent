"""Re-check the projection model's settings against real results (run monthly, or when the scorecard drifts).

Rolling backtest: for each round, project every player who then played 10+ minutes using only games played before
that round, and compare with what he scored. Two samples:
  - last season (data/history, the newest file), rounds 15 to the end of the regular season;
  - this season so far (the latest data/<date>/pool.json game logs), round 8 on.
It prints the average miss (MAE), bias and correlation for the current PARAMS and for small changes to each one, so
the analyst can update model.PARAMS when a change helps on BOTH samples. Nothing is written automatically.

    python tune_model.py            (uses the newest data/<date>/pool.json)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import model
from model import Model

ROOT = Path(__file__).resolve().parent


def stats(pairs):
    n = len(pairs)
    if not n:
        return {"n": 0}
    mae = sum(abs(a - b) for a, b in pairs) / n
    bias = sum(a - b for a, b in pairs) / n
    ma = sum(a for a, _ in pairs) / n
    mb = sum(b for _, b in pairs) / n
    cov = sum((a - ma) * (b - mb) for a, b in pairs)
    va = sum((a - ma) ** 2 for a, _ in pairs)
    vb = sum((b - mb) ** 2 for _, b in pairs)
    return {"n": n, "mae": round(mae, 3), "bias": round(bias, 2), "corr": round(cov / math.sqrt(va * vb), 3) if va and vb else 0}


def season_cases(byp, hist, rounds):
    """byp: code -> [(rnd, date, club, opp, home, min, pir)] for the test season."""
    cases = []
    for R in rounds:
        pool, tests = [], []
        roster, played = defaultdict(lambda: defaultdict(int)), defaultdict(set)
        for code, gs in byp.items():
            before = sorted([g for g in gs if g[0] < R], key=lambda g: g[1])
            now = [g for g in gs if g[0] == R]
            if now:
                played[now[0][2]].add(code)
            if not before:
                continue
            club = now[0][2] if now else before[-1][2]
            for g in before:
                if g[2] == club:
                    roster[club][code] += 1
            gl = [[g[0], g[1], g[3], g[4], 0, 0, 0, g[5]] + [0] * 7 + [g[6]] for g in before]
            p = {"c": code, "t": club, "gl": gl}
            pool.append(p)
            if now and len(before) >= 3 and now[0][5] >= 10:
                d1 = dt.date.fromisoformat(now[0][1][:10])
                d0 = dt.date.fromisoformat(before[-1][1][:10])
                tests.append((p, now[0], 0 < (d1 - d0).days <= 2))
        absent = {c: {k for k, n in r.items() if n >= 3} - played[c] for c, r in roster.items()}
        if tests:
            cases.append((pool, tests, absent))
    return cases


def run(cases, hist, params):
    pairs = []
    for pool, tests, absent in cases:
        m = Model(pool, params, hist=hist)
        for p, nw, rest in tests:
            v, _ = m.project(p, nw[3], nw[4], None, absent=absent.get(p["t"], set()), short_rest=rest)
            if v is not None:
                pairs.append((v, nw[6]))
    return stats(pairs)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    files = sorted((ROOT / "data" / "history").glob("E*.json"))
    if not files:
        sys.exit("no data/history files (run fetch_history.py)")
    # last season = newest history file; its own history = the older files
    last = json.loads(files[-1].read_text(encoding="utf-8"))["rows"]
    hist_old = defaultdict(list)
    for rank, f in enumerate(reversed(files[:-1]), 1):
        for code, club, opp, home, date, rnd, mins, pir in json.loads(f.read_text(encoding="utf-8"))["rows"]:
            hist_old[code.strip()].append((rank, date, club, opp, home, mins, pir))
    byp = defaultdict(list)
    for code, club, opp, home, date, rnd, mins, pir in last:
        if rnd <= 34:
            byp[code.strip()].append((rnd, date, club, opp, home, mins, pir))
    A = season_cases(byp, hist_old, range(15, 35))
    # this season from the newest pool's game logs
    pools = sorted(ROOT.glob("data/20*/pool.json"))
    B, hist_all = [], model.load_history()
    if pools:
        cur = json.loads(pools[-1].read_text(encoding="utf-8"))
        byc = defaultdict(list)
        for p in cur:
            for g in p.get("gl") or []:
                if g[7] >= model.PARAMS["MIN_MIN"]:
                    byc[str(p.get("c") or "").strip()].append((g[0], g[1], p.get("t"), g[2], g[3], g[7], g[15]))
        last_rd = max((g[0] for v in byc.values() for g in v), default=0)
        B = season_cases(byc, hist_all, range(8, last_rd + 1))
        print(f"this season: {pools[-1].parent.name} pool, rounds 8-{last_rd}")
    grid = {"HL_MIN": (4, 8, 15), "HL_RATE": (20, 40, 80), "K_RATE": (30, 100, 300), "TEAMMATES": (0, 1), "REST": (0, 1), "TEAM_HOME": (0, 1),
            "DEF_K": (50, 150, 400), "H2H_K": (12, 30, 1e9), "PRIOR_GAMES": (2, 4, 8)}

    def line(label, prm):
        a = run(A, hist_old, prm)
        b = run(B, hist_all, prm) if B else {"n": 0}
        print(f"{label:24} last season {a.get('mae')} (corr {a.get('corr')}, n {a['n']}) | this season "
              f"{b.get('mae')} (corr {b.get('corr')}, n {b['n']})")
        return a, b

    print("current PARAMS:")
    base = line("  as set", {})
    for k, vals in grid.items():
        for v in vals:
            if v != model.PARAMS.get(k):
                line(f"  {k}={v:g}", {k: v})
    print("Update model.PARAMS only for a change that lowers the miss on both samples.")
    return base


if __name__ == "__main__":
    main()
