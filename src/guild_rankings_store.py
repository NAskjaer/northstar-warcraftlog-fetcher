# src/guild_rankings_store.py
"""
Persist a raid's guild progress ranking as a static JSON list.

The ranking is read once from the pasted rankings-page HTML and stored here, so
subsequent runs slice a rank range out of it with ZERO Warcraft Logs API calls
(only the per-guild log analysis needs the API). Keyed per raid config file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DIR = _PROJECT_ROOT / "config" / "guild_rankings"
_DIR.mkdir(parents=True, exist_ok=True)


def _path(raid_file: str) -> Path:
    """Static ranking file for a raid config (e.g. Midnight_season_1.json)."""
    return _DIR / (Path(raid_file).stem + ".json")


def load_ranking(raid_file: str) -> List[Dict[str, Any]]:
    """Load the stored ranked guild list for a raid (empty list if none)."""
    path = _path(raid_file)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    guilds = data.get("guilds", []) if isinstance(data, dict) else []
    guilds.sort(key=lambda g: g.get("rank", 1_000_000))
    return guilds


def save_ranking(raid_file: str, guilds: List[Dict[str, Any]]) -> None:
    """Persist a ranked guild list for a raid."""
    payload = {"guilds": guilds}
    _path(raid_file).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
