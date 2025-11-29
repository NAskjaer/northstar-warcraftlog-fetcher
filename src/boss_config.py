from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any

from src.api_client import run_wcl_query

# Always use the config folder next to project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _PROJECT_ROOT / "config"
_CONFIG_DIR.mkdir(exist_ok=True)

_BOSSES_JSON_PATH = _CONFIG_DIR / "bosses.json"


# In-memory cache
_CACHE: Dict[str, Any] | None = None


def _ensure_json_exists() -> None:
    """Create an empty bosses.json if missing (but never seed defaults)."""
    if not _BOSSES_JSON_PATH.exists():
        empty = {"bosses": {}, "ability_names": {}}
        _BOSSES_JSON_PATH.write_text(json.dumps(empty, indent=2), encoding="utf-8")


def _load_raw() -> Dict[str, Any]:
    """Load JSON exactly as stored. No default seeding."""
    global _CACHE

    if _CACHE is not None:
        return _CACHE

    _ensure_json_exists()

    try:
        data = json.loads(_BOSSES_JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"bosses": {}, "ability_names": {}}

    # Guarantee structure
    data.setdefault("bosses", {})
    data.setdefault("ability_names", {})

    _CACHE = data
    return data


def _save_raw(data: Dict[str, Any]) -> None:
    """Save bosses.json and update cache."""
    global _CACHE
    _CACHE = data

    data.setdefault("bosses", {})
    data.setdefault("ability_names", {})

    _BOSSES_JSON_PATH.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def get_boss_options() -> Dict[str, Dict[str, Any]]:
    """Return bosses with ids and abilities from JSON."""
    raw = _load_raw()
    bosses = {}

    for boss_name, info in raw["bosses"].items():
        try:
            boss_id = int(info["id"])
        except Exception:
            continue

        abilities = []
        for a in info.get("abilities", []):
            try:
                abilities.append(int(a))
            except:
                continue

        bosses[boss_name] = {"id": boss_id, "abilities": abilities}

    return bosses


def get_ability_names() -> Dict[int, str]:
    raw = _load_raw()
    out = {}
    for key, val in raw["ability_names"].items():
        try:
            out[int(key)] = str(val)
        except:
            continue
    return out


def add_ability(boss_name: str, boss_id: int, ability_id: int, label: str) -> None:
    """Add an ability to a boss and persist to JSON."""
    data = _load_raw()

    boss_entry = data["bosses"].setdefault(
        boss_name,
        {"id": boss_id, "abilities": []},
    )

    boss_entry["id"] = int(boss_id)

    if int(ability_id) not in [int(a) for a in boss_entry["abilities"]]:
        boss_entry["abilities"].append(int(ability_id))

    data["ability_names"][str(int(ability_id))] = label

    _save_raw(data)


def lookup_ability_name(ability_id: int) -> str | None:
    query = """
    query ($id: Int!) {
      gameData {
        ability(id: $id) {
          id
          name
        }
      }
    }
    """
    result = run_wcl_query(query, {"id": ability_id})
    ability = (
        result.get("data", {})
        .get("gameData", {})
        .get("ability")
    )
    if not ability:
        return None
    return ability.get("name") or None
