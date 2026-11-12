# EuroLeague Fantasy Analyst

A daily agent that thinks like a pro Fantasy manager and a EuroLeague coach.

| File | Role |
|---|---|
| `my_team.json` | **Your squad.** Update it after every trade (players, captain, coach, bank, notes). |
| `collect.py` | Evidence layer. Pulls projections, prices and ownership, injuries, news, ABA domestic alerts, game logs and next-round previews from basketballstats.eu into `data/<date>/digest.md`. |
| `PLAYBOOK.md` | The analyst's brain: rules, reasoning checklist, research duties, report format. Edit it to change how the agent thinks. |
| `reports/<date>.md` | The daily output: trades, lineup, captain, player-by-player verdicts. |

A scheduled task (**EuroLeague Fantasy daily analysis**, 10:00 every day) runs the collector,
researches domestic-league games and news on the web, and writes the report.
It runs while the Claude desktop app is open; if the app was closed, it runs on next launch.

Run the collector manually: `python collect.py`
