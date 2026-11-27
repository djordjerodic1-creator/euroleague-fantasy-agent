"""Round scorecard: how did the last report's calls actually do?

For every finished round that has saved predictions (data/<date>/predictions.json, written by render.py),
compare predictions with what happened: the real fantasy score of each variation and of your team, the
captain call, and how far off the projections were (by price tier, with the biggest surprises).

  python scorecard.py [YYYY-MM-DD]     (date of today's data folder; run after collect.py)

Writes data/scorecard.json (one entry per round, never recomputed once written).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COACH_BANDS = [(-1e9, -20.5, -20), (-20.5, -10.5, -10), (-10.5, 0, -5), (0, 10.5, 10), (10.5, 20.5, 20), (20.5, 1e9, 25)]


def coach_pts(margin):
    for a, b, p in COACH_BANDS:
        if a <= margin < b:
            return p
    return 0


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def lineup_score(lu, actual, results):
    """Real fantasy points of a lineup: PIR x1.1 on a win, captain x2, bench x0.5, plus the coach."""
    rows, total = [], 0.0

    def pts(name, mult):
        a = actual.get(name)
        if not a:
            rows.append({"name": name, "pir": None, "mult": mult, "pts": 0.0})
            return 0.0
        v = a["pir"] * (1.1 if a["won"] else 1.0) * mult
        rows.append({"name": name, "pir": a["pir"], "mult": mult, "pts": round(v, 1)})
        return v
    for n in lu.get("starters", []):
        total += pts(n, 2.0 if n == lu.get("captain") else 1.0)
    if lu.get("sixth"):
        total += pts(lu["sixth"], 1.0)
    for n in lu.get("bench", []):
        total += pts(n, 0.5)
    c = lu.get("coach_code")
    cp = coach_pts(results[c]) if c in results else 0
    total += cp
    starters = [r for r in rows if r["mult"] >= 1.0][:5]
    best_cap = max((r for r in starters if r["pir"] is not None), key=lambda r: r["pir"], default=None)
    cap_row = next((r for r in rows if r["name"] == lu.get("captain")), None)
    return {"total": round(total, 1), "coach_pts": cp, "players": rows,
            "captain": lu.get("captain"), "captain_pir": cap_row["pir"] if cap_row else None,
            "best_captain": best_cap["name"] if best_cap else None, "best_captain_pir": best_cap["pir"] if best_cap else None}


def score_round(rd, preds, team_snap, pool, sched):
    games = [g for g in sched.get("games", []) if g["rd"] == rd]
    results = {}
    for g in games:
        if g.get("res"):
            m = g["res"][0] - g["res"][1]
            results[g["h"]], results[g["a"]] = m, -m
    actual = {}
    for p in pool:
        for r in p.get("gl") or []:
            if r[0] == rd:
                actual[p["name"]] = {"pir": r[15], "min": r[7], "won": r[4] > r[5], "t": p.get("t")}
    out = {"round": rd, "report_date": preds["date"], "variations": [], "team": None}
    for v in preds.get("variations", []):
        s = lineup_score(v, actual, results)
        s.update({"key": v["key"], "name": v.get("name"), "recommended": v.get("recommended"),
                  "pred_mean": v.get("mean"), "pred_p90": v.get("p90"), "pred_p_target": v.get("p_target")})
        out["variations"].append(s)
    if team_snap:
        out["team"] = lineup_score(team_snap, actual, results)
        out["team"]["source"] = team_snap.get("source")
    # projection accuracy over everyone the report projected who played
    errs, inside, below, above, tiers, misses = [], 0, 0, 0, {"15+": [], "8-15": [], "<8": []}, []
    site_err, min_err, by_pos = [], [], {}
    for name, row in preds.get("proj", {}).items():
        t, proj, p10, p90, price, owned = row[:6]
        site, mexp, pos = (list(row[6:9]) + [None, None, None])[:3]
        a = actual.get(name)
        if not a or proj is None or proj <= 0:
            continue
        e = a["pir"] - proj
        errs.append(e)
        if site is not None and site > 0:
            site_err.append(a["pir"] - site)
        if mexp is not None:
            min_err.append(a["min"] - mexp)
        if pos:
            by_pos.setdefault(pos, []).append(e)
        tiers["15+" if proj >= 15 else ("8-15" if proj >= 8 else "<8")].append(e)
        if p10 is not None and p90 is not None:
            inside += p10 <= a["pir"] <= p90
            below += a["pir"] < p10
            above += a["pir"] > p90
            if a["pir"] > p90 + 4:
                misses.append({"name": name, "t": t, "price": price, "owned": owned, "proj": proj, "pir": a["pir"], "min": a["min"], "kind": "upside"})
            elif a["pir"] < p10 - 4:
                misses.append({"name": name, "t": t, "price": price, "owned": owned, "proj": proj, "pir": a["pir"], "min": a["min"], "kind": "downside"})
    n = len(errs)
    out["projections"] = {
        "n": n, "bias": round(sum(errs) / n, 2) if n else None, "mae": round(sum(abs(x) for x in errs) / n, 2) if n else None,
        "inside_p10_p90": round(inside / n, 3) if n else None, "below_p10": below, "above_p90": above,
        "by_tier": {k: {"n": len(v), "bias": round(sum(v) / len(v), 2) if v else None} for k, v in tiers.items()},
        "site_mae": round(sum(abs(x) for x in site_err) / len(site_err), 2) if site_err else None,
        "site_n": len(site_err),
        "minutes_mae": round(sum(abs(x) for x in min_err) / len(min_err), 1) if min_err else None,
        "minutes_bias": round(sum(min_err) / len(min_err), 1) if min_err else None,
        "by_pos": {k: {"n": len(v), "bias": round(sum(v) / len(v), 2)} for k, v in by_pos.items()},
        "upside_misses": sorted([m for m in misses if m["kind"] == "upside"], key=lambda m: m["proj"] - m["pir"])[:8],
        "downside_misses": sorted([m for m in misses if m["kind"] == "downside"], key=lambda m: m["pir"] - m["proj"])[:5],
    }
    return out


def round_done(rd, sched):
    gs = [g for g in sched.get("games", []) if g["rd"] == rd]
    return bool(gs) and all(g.get("res") for g in gs)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    date = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    from paths import TEAM, data_dir, load_team, scorecard_file
    d = data_dir(date)
    pool, sched = load(d / "pool.json", []), load(d / "schedule.json", {})
    if not pool or not sched:
        print("scorecard: need today's pool.json and schedule.json (run collect.py first)")
        return
    sc_path = scorecard_file()
    sc = load(sc_path, {"rounds": {}})
    preds = sorted((load(f) for f in (ROOT / "data").glob(f"*/{TEAM}/predictions.json" if TEAM else "*/predictions.json")), key=lambda p: p["date"])
    by_round = {}
    for p in preds:
        by_round.setdefault(p["round"], []).append(p)
    days = {}
    for g in sched.get("games", []):
        days.setdefault(g["rd"], []).append(g["day"])
    new = []
    for rd, plist in sorted(by_round.items()):
        if str(rd) in sc["rounds"] or not round_done(rd, sched):
            continue
        last = plist[-1]  # the final report before the round
        # the team actually fielded: snapshot from a report made while the round was being played, else the last one
        during = [p for p in preds if p["round"] == rd + 1 and p.get("team_round") == rd]
        snap = (during[0] if during else last).get("team")
        sc["rounds"][str(rd)] = score_round(rd, last, snap, pool, sched)
        rep_score = (load_team().get("round_scores") or {}).get(str(rd))
        if rep_score is not None and sc["rounds"][str(rd)].get("team"):
            sc["rounds"][str(rd)]["team"]["reported"] = rep_score
        new.append(rd)
    if new:
        sc_path.write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")
    for rd in new:
        r = sc["rounds"][str(rd)]
        pr = r["projections"]
        print(f"R{rd}: " + " · ".join(f"{v['key']} {v['total']} (pred {v['pred_mean']})" for v in r["variations"])
              + (f" · your team {r['team']['total']}" if r.get("team") else "")
              + f" · projections MAE {pr['mae']} bias {pr['bias']:+} · inside P10–P90 {pr['inside_p10_p90']:.0%}")
    if not new:
        print("scorecard: no newly finished round with saved predictions")


if __name__ == "__main__":
    main()
