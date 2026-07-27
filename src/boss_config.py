from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any

from src.api_client import run_wcl_query

# Always use the config folder next to project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _PROJECT_ROOT / "config" / "bosses"
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Default raid files
_DEFAULT_RAID_FILE = "Manaforge_Omega.json"

# In-memory cache per raid file
_CACHE: Dict[str, Dict[str, Any]] = {}


def _get_raid_json_path(raid_file: str) -> Path:
    """Get the full path for a specific raid JSON file."""
    return _CONFIG_DIR / raid_file


def _ensure_json_exists(raid_file: str) -> None:
    """Create an empty raid JSON file if missing (but never seed defaults)."""
    json_path = _get_raid_json_path(raid_file)
    if not json_path.exists():
        empty = {"bosses": {}, "ability_names": {}}
        json_path.write_text(json.dumps(empty, indent=2), encoding="utf-8")


def _load_raw(raid_file: str = _DEFAULT_RAID_FILE) -> Dict[str, Any]:
    """Load JSON exactly as stored. No default seeding."""
    global _CACHE

    if raid_file in _CACHE:
        return _CACHE[raid_file]

    _ensure_json_exists(raid_file)
    json_path = _get_raid_json_path(raid_file)

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"bosses": {}, "ability_names": {}}

    # Guarantee structure
    data.setdefault("bosses", {})
    data.setdefault("ability_names", {})

    _CACHE[raid_file] = data
    return data


def _save_raw(data: Dict[str, Any], raid_file: str = _DEFAULT_RAID_FILE) -> None:
    """Save raid JSON file and update cache."""
    global _CACHE
    _CACHE[raid_file] = data

    data.setdefault("bosses", {})
    data.setdefault("ability_names", {})

    json_path = _get_raid_json_path(raid_file)
    json_path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def get_boss_options(raid_file: str = _DEFAULT_RAID_FILE) -> Dict[str, Dict[str, Any]]:
    """Return bosses with ids and abilities from the specified raid JSON."""
    raw = _load_raw(raid_file)
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


def get_ability_names(raid_file: str = _DEFAULT_RAID_FILE) -> Dict[int, str]:
    """Return ability names from the specified raid JSON."""
    raw = _load_raw(raid_file)
    out = {}
    for key, val in raw["ability_names"].items():
        try:
            out[int(key)] = str(val)
        except:
            continue
    return out


def add_ability(
    boss_name: str, 
    boss_id: int, 
    ability_id: int, 
    label: str,
    raid_file: str = _DEFAULT_RAID_FILE
) -> None:
    """Add an ability to a boss and persist to the specified raid JSON."""
    data = _load_raw(raid_file)

    boss_entry = data["bosses"].setdefault(
        boss_name,
        {"id": boss_id, "abilities": []},
    )

    boss_entry["id"] = int(boss_id)

    if int(ability_id) not in [int(a) for a in boss_entry["abilities"]]:
        boss_entry["abilities"].append(int(ability_id))

    data["ability_names"][str(int(ability_id))] = label

    _save_raw(data, raid_file)


def lookup_ability_name(ability_id: int) -> str | None:
    """Look up ability name from Warcraft Logs API."""
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


def invalidate_cache(raid_file: str | None = None) -> None:
    """
    Invalidate cache for a specific raid file, or all caches if raid_file is None.
    Useful when switching between raids or after manual JSON edits.
    """
    global _CACHE
    if raid_file is None:
        _CACHE.clear()
    else:
        _CACHE.pop(raid_file, None)
