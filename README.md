# EuroLeague Fantasy Analyst

A daily agent that thinks like a pro Fantasy manager and a EuroLeague coach.

| File | Role |
|---|---|
| `my_team.json` | **Your squad.** Maintained by the agent: after each deadline it applies the report's Variation A. Tell the agent (or edit this file) only when you did something different. |
| `collect.py` | Evidence layer. Pulls projections, prices and ownership, injuries, news, ABA domestic alerts, game logs and next-round previews from basketballstats.eu into `data/<date>/digest.md`. |
| `PLAYBOOK.md` | The analyst's brain: rules, reasoning checklist, research duties, report format. Edit it to change how the agent thinks. |
| `render.py` | Turns the analyst's `reports/<date>.json` (decisions + reasoning) into the court-style HTML report, filling every number from the data and validating budget/positions/clubs. |
| `reports/<date>.html` | The daily report: 3 squad variations on a court, trades, captain, alerts, locker-room rumours, player verdicts. `index.html` = latest. |

A Claude cloud routine (**EuroLeague Fantasy daily analyst**, 10:00 Budapest every day) clones this repo,
runs the collector, researches domestic games, injuries and rumours on the web, writes and renders
the report, and commits it here. It does not need the laptop or the desktop app.

Run manually: `python collect.py`, write `reports/<date>.json`, then `python render.py <date>`.
