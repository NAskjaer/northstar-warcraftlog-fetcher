# src/report_cache.py
"""
Per-run cache of report-level Warcraft Logs data, shared by every caller
(single-guild UI flow, multi-guild ranking aggregation, survivability, and
the overnight runner) so the same report is never queried twice for data
that doesn't depend on which ability/metric is being analysed.

Cached at the level each underlying WCL query actually varies by:
  - fights          -- per report_code only (the query has no boss/difficulty
                        filter; it always returns the whole report).
  - actors          -- per report_code.
  - class/specs     -- per (report_code, boss_id) (playerDetails is queried
                        with that boss's fight IDs; identical across every
                        ability target on the same boss).
  - death events     -- per (report_code, boss_id, difficulty, wipe_cutoff).
                        Ability filtering happens client-side after fetch, so
                        every ability target on the same boss shares one fetch.
  - damage events    -- per (report_code, boss_id, difficulty, ability_id).
                        WCL filters damage events server-side by ability, so
                        distinct abilities genuinely need distinct fetches.

Call reset_report_caches() once at the start of each run (single-guild
submit, or a multi-guild aggregation pass) so caches don't leak stale data
across runs sharing the same process.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from .deaths_fetcher import fetch_all_fights, _fetch_player_actors, _fetch_death_events
from .damage_taken_fetcher import _fetch_damage_taken_events
from .player_details_fetcher import get_player_class_specs

_lock = threading.Lock()
_fights_cache: Dict[str, List[Dict[str, Any]]] = {}
_actors_cache: Dict[str, Dict[int, str]] = {}
_class_spec_cache: Dict[Tuple[str, int], Dict[str, Tuple[str, str]]] = {}
_death_events_cache: Dict[tuple, List[Dict[str, Any]]] = {}
_damage_events_cache: Dict[tuple, List[Dict[str, Any]]] = {}


def reset_report_caches() -> None:
    """Clear all per-run report caches (call once before an aggregation run)."""
    with _lock:
        _fights_cache.clear()
        _actors_cache.clear()
        _class_spec_cache.clear()
        _death_events_cache.clear()
        _damage_events_cache.clear()


def get_stats() -> Dict[str, int]:
    """
    Rough usage stats for the run since the last reset_report_caches().
    reports_fetched counts distinct report codes whose fight list was
    fetched — every report this run touched at all goes through
    get_report_fights at least once (cached thereafter), so this is a solid
    proxy for "how many WCL reports did this run actually pull data from".
    """
    with _lock:
        return {"reports_fetched": len(_fights_cache)}


def get_report_fights(report_code: str) -> List[Dict[str, Any]]:
    """All fights in a report (every boss, every difficulty), memoised."""
    with _lock:
        cached = _fights_cache.get(report_code)
    if cached is not None:
        return cached
    fights = fetch_all_fights(report_code)
    with _lock:
        _fights_cache.setdefault(report_code, fights)
        return _fights_cache[report_code]


def get_boss_fights(
    report_code: str, boss_id: int, difficulty: int | None = 5
) -> List[Dict[str, Any]]:
    """Fights for one boss+difficulty, filtered client-side from the cached full list."""
    fights = get_report_fights(report_code)
    return [
        f
        for f in fights
        if f.get("encounterID") == boss_id
        and (difficulty is None or f.get("difficulty") == difficulty)
    ]


def get_report_actors(report_code: str) -> Dict[int, str]:
    """Player actor id -> name for a report, memoised."""
    with _lock:
        cached = _actors_cache.get(report_code)
    if cached is not None:
        return cached
    actors = _fetch_player_actors(report_code)
    with _lock:
        _actors_cache.setdefault(report_code, actors)
        return _actors_cache[report_code]


def get_report_class_specs(
    report_code: str, boss_id: int, fight_ids: List[int]
) -> Dict[str, Tuple[str, str]]:
    """Player class/spec for a report+boss, memoised (shared across abilities)."""
    key = (report_code, boss_id)
    with _lock:
        cached = _class_spec_cache.get(key)
    if cached is not None:
        return cached
    specs = get_player_class_specs(report_code, fight_ids)
    with _lock:
        _class_spec_cache.setdefault(key, specs)
        return _class_spec_cache[key]


def get_death_events(
    report_code: str,
    boss_id: int,
    difficulty: int | None,
    fight_ids: List[int],
    start_time: float,
    end_time: float,
    wipe_cutoff: Optional[int],
    ability_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Raw death events for a boss's fights, memoised per (report, boss, difficulty,
    wipeCutoff, ability_id).

    When ability_id is given it's applied server-side (WCL's abilityID filter),
    which is dramatically cheaper than fetching every death and filtering
    client-side — measured live at ~1 point vs ~22+ points for a mid-size
    report — so distinct abilities are cached (and fetched) separately rather
    than sharing one unfiltered fetch. ability_id=None (an "all abilities"
    target) still fetches and caches the full unfiltered set.
    """
    key = (report_code, boss_id, difficulty, wipe_cutoff, ability_id)
    with _lock:
        cached = _death_events_cache.get(key)
    if cached is not None:
        return cached
    events = _fetch_death_events(
        report_code, start_time, end_time, fight_ids, wipe_cutoff, ability_id
    )
    with _lock:
        _death_events_cache.setdefault(key, events)
        return _death_events_cache[key]


def get_damage_events(
    report_code: str,
    boss_id: int,
    difficulty: int | None,
    fight_ids: List[int],
    start_time: float,
    end_time: float,
    ability_id: Optional[int],
) -> List[Dict[str, Any]]:
    """Raw damage-taken events, memoised per (report, boss, difficulty, ability_id) —
    WCL filters damage events server-side by ability, so distinct abilities need
    distinct fetches, but repeated targets on the same ability share one."""
    key = (report_code, boss_id, difficulty, ability_id)
    with _lock:
        cached = _damage_events_cache.get(key)
    if cached is not None:
        return cached
    events = _fetch_damage_taken_events(report_code, start_time, end_time, fight_ids, ability_id)
    with _lock:
        _damage_events_cache.setdefault(key, events)
        return _damage_events_cache[key]
