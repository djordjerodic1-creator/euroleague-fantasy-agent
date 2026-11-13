---
name: team
description: Update the user's EuroLeague Fantasy squad (my_team.json) from a screenshot or a plain-words change list, push it, and offer a fresh report. Use when the user shares their team, bank, trades, lineup or captain.
---

# Update my team

The user gives a screenshot of their Fantasy team, or words like "sold Nunn, bought Carlik Jones,
bank 1.3". Their words and screenshot beat anything assumed in `my_team.json`.

1. `git pull --ff-only` in the repo root.
2. Read `my_team.json`, the latest `reports/*.json` and `data/players_cache.json` (or the latest
   `data/<date>/pool.json`) for exact player spellings and prices.
3. Read the screenshot or text. Pull out whatever it shows: the 10 players, starters / 6th man /
   bench, captain, coach, **bank** (most important), total team value, round and round score.
   Keep every field it doesn't show. Match each name to the pool spelling. If something is
   unreadable or ambiguous, ask once instead of guessing.
4. Write `my_team.json`: set `tracking.source` to "user", `tracking.updated` to today,
   `bank_known: true` when a bank was given, and recompute `total_budget` = squad value at current
   prices + bank. If the update reflects trades for a round whose deadline has passed, set
   `tracking.last_applied_report` to that round's report date so Variation A isn't applied on top.
   Also note the round score in `notes` if given, briefly.
5. Show the user a 3-line diff (players in/out, bank, captain/coach) before committing.
6. Commit only `my_team.json` with "Team update <date> (user)" and push to main. Never commit the
   screenshot: the repo is public.
7. Ask whether to run the analyst now for a fresh report (the `analyst` skill).
