"""Where each team's files live. The main team uses the original paths; extra teams (ELF_TEAM=<id>) live in
teams/<id>/ (team.json, reports/, scorecard.json, index.html) and data/<date>/<id>/.

  ELF_TEAM=team2 python collect.py && ELF_TEAM=team2 python sim.py <date> && ELF_TEAM=team2 python render.py <date>
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEAM = os.environ.get("ELF_TEAM", "").strip()
TEAM_DIR = ROOT / "teams" / TEAM if TEAM else ROOT


def team_file() -> Path:
    return TEAM_DIR / "team.json" if TEAM else ROOT / "my_team.json"


def load_team() -> dict:
    return json.loads(team_file().read_text(encoding="utf-8"))


def data_dir(date: str) -> Path:
    return ROOT / "data" / date / TEAM if TEAM else ROOT / "data" / date


def reports_dir() -> Path:
    return TEAM_DIR / "reports"


def index_file() -> Path:
    return TEAM_DIR / "index.html"


def scorecard_file() -> Path:
    return TEAM_DIR / "scorecard.json" if TEAM else ROOT / "data" / "scorecard.json"


def all_teams() -> list[dict]:
    """[{id, name, href (from the site root)}] for the page's team switcher."""
    out = [{"id": "", "name": "My team", "href": ""}]
    try:
        out[0]["name"] = json.loads((ROOT / "my_team.json").read_text(encoding="utf-8")).get("name") or "My team"
    except Exception:
        pass
    for f in sorted((ROOT / "teams").glob("*/team.json")):
        try:
            name = json.loads(f.read_text(encoding="utf-8")).get("name") or f.parent.name
        except Exception:
            name = f.parent.name
        out.append({"id": f.parent.name, "name": name, "href": f"teams/{f.parent.name}/"})
    return out
