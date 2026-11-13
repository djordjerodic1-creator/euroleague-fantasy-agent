---
name: analyst
description: Run the EuroLeague Fantasy analyst on demand and show the fresh report. Use when the user asks to run the agent, refresh the report, or re-check after news or a team update.
---

# Run the analyst now

Default: fire the cloud routine, so the run is identical to the daily one.

1. Make sure any local team change is pushed (`git status`; if `my_team.json` changed, use the
   `team` skill first).
2. `RemoteTrigger` `list`, find the routine named **EuroLeague Fantasy daily analyst**, then
   `run` it. Tell the user it started and link the run (`list_runs`).
3. You'll be told nothing automatically, so tell the user it takes about 5–15 minutes and that
   the report appears at the GitHub Pages link in the README when done. If they wait here,
   check `list_runs` once when they ask, then `git pull` and show `reports/<today>.html`.
4. If the run failed, read `get_run_log` and explain the cause in one line.

Fallback, run locally (when the cloud is down or the user asks): follow the routine's steps
yourself in this repo: read PLAYBOOK.md, apply `inbox/team.md` and team tracking,
`python collect.py`, `python sim.py <date>`, research, write `reports/<date>.json`,
`python render.py <date>`, fix any "⚠" lines, commit and push.

A same-day re-run overwrites today's report. Say in `review` what changed since the morning run.
