# src/multi_guild.py
"""
Aggregate per-player deaths / damage-taken across many guilds at once.

Given a ranked list of guilds (from guild_rankings_fetcher), analyse each
guild's logs over a date range exactly like the single-guild flow does:
group reports by day, take the longest report per day, and sum per-player
deaths/hits AND per-player pulls (wipe attendance) across them.

That gives, per guild:
  * guild_total_pulls  -- total wipe pulls across the range (their progression)
  * per-player pulls    -- how many of those pulls each player attended

which lets us drop players who attended far fewer pulls than their guild
(and would therefore look "clean" simply for sitting out).
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .calendar_fetcher import fetch_logs_for_guild
from .deaths_fetcher import (
    get_boss_fights_for_report,
    _fetch_death_events,
    _fetch_player_actors,
)
from .damage_taken_fetcher import _fetch_damage_taken_events


# --------------------------------------------------------------------------
# Per-run caches to avoid re-fetching the same data across metric/ability
# passes. Small, reusable data only: boss fights, actor maps, and the full
# death-event set per report (deaths are cheap and filtered by ability
# locally). Damage events are NOT cached — they're filtered server-side by
# abilityID, so per-ability fetches are already the cheap path.
#
# Call reset_report_caches() at the start of each aggregation run.
# --------------------------------------------------------------------------
_cache_lock = threading.Lock()
_fights_cache: Dict[tuple, List[Dict[str, Any]]] = {}
_actors_cache: Dict[str, Dict[int, str]] = {}
_deaths_cache: Dict[tuple, List[Dict[str, Any]]] = {}


def reset_report_caches() -> None:
    """Clear the per-run report caches (call once before an aggregation run)."""
    with _cache_lock:
        _fights_cache.clear()
        _actors_cache.clear()
        _deaths_cache.clear()


def _cached_fights(code: str, boss_id: int, difficulty: int) -> List[Dict[str, Any]]:
    key = (code, boss_id, difficulty)
    with _cache_lock:
        if key in _fights_cache:
            return _fights_cache[key]
    val = get_boss_fights_for_report(code, boss_id, difficulty)
    with _cache_lock:
        _fights_cache[key] = val
    return val


def _cached_actors(code: str) -> Dict[int, str]:
    with _cache_lock:
        if code in _actors_cache:
            return _actors_cache[code]
    val = _fetch_player_actors(code)
    with _cache_lock:
        _actors_cache[code] = val
    return val


def _cached_death_events(
    code: str,
    start_time: int,
    end_time: int,
    fight_ids: List[int],
    boss_id: int,
    difficulty: int,
    ignore_after_player_deaths: Optional[int],
) -> List[Dict[str, Any]]:
    # Keyed by what determines the fetch (window derives from boss+diff fights).
    key = (code, boss_id, difficulty, ignore_after_player_deaths)
    with _cache_lock:
        if key in _deaths_cache:
            return _deaths_cache[key]
    val = _fetch_death_events(
        code, start_time, end_time, fight_ids, ignore_after_player_deaths
    )
    with _cache_lock:
        _deaths_cache[key] = val
    return val


def _best_reports_in_range(
    guild_id: int, start_dt, end_dt, zone_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Reports for the guild in range, keeping the longest report per day."""
    reports = fetch_logs_for_guild(guild_id, start_dt, end_dt, zone_id)
    if not reports:
        return []
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for rep in reports:
        start_ms = rep.get("startTime", 0)
        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(rep)

    def _duration(r: Dict[str, Any]) -> int:
        s = r.get("startTime", 0)
        return r.get("endTime", s) - s

    return [max(reps, key=_duration) for reps in by_date.values()]


def _analyse_report(
    report_code: str,
    *,
    boss_id: int,
    ability_id: Optional[int],
    difficulty: int,
    metric_is_deaths: bool,
    ignore_after_player_deaths: Optional[int],
) -> Optional[Dict[str, Any]]:
    """
    Analyse one report's WIPE pulls for a boss + ability.

    Fetches fights, actors and events once each (no redundant calls) and returns
    per-player values, per-player pulls, and the wipe-pull count, or None if the
    boss isn't present in this report.
    """
    fights = _cached_fights(report_code, boss_id, difficulty)
    if not fights:
        return None
    # Wipes only, to match the rest of the app.
    fights = [f for f in fights if not f.get("kill", False)]
    if not fights:
        return None

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    actors = _cached_actors(report_code)

    # Per-player pulls (attendance) + total wipe pulls in this report.
    pulls: Dict[str, int] = {}
    for f in fights:
        for actor_id in f.get("friendlyPlayers") or []:
            name = actors.get(int(actor_id))
            if name:
                pulls[name] = pulls.get(name, 0) + 1
    num_wipes = len(fights)

    values: Dict[str, int] = {}
    hits: Dict[str, int] = {}

    if metric_is_deaths:
        events = _cached_death_events(
            report_code,
            start_time,
            end_time,
            fight_ids,
            boss_id,
            difficulty,
            ignore_after_player_deaths,
        )
        for ev in events:
            if ev.get("type") != "death":
                continue
            if ev.get("fight") not in fight_ids:
                continue
            if ability_id is not None and not (
                ev.get("abilityGameID") == ability_id
                or ev.get("killingAbilityGameID") == ability_id
            ):
                continue
            target_id = ev.get("targetID")
            if target_id is None:
                continue
            name = actors.get(int(target_id), f"ID-{target_id}")
            values[name] = values.get(name, 0) + 1
    else:
        events = _fetch_damage_taken_events(
            report_code, start_time, end_time, fight_ids, ability_id
        )
        for ev in events:
            if ev.get("fight") not in fight_ids:
                continue
            if ability_id is not None and ev.get("abilityGameID") != ability_id:
                continue
            target_id = ev.get("targetID")
            if target_id is None:
                continue
            name = actors.get(int(target_id), f"ID-{target_id}")
            values[name] = values.get(name, 0) + int(ev.get("amount") or 0)
            hits[name] = hits.get(name, 0) + 1

    return {
        "values": values,
        "hits": hits,
        "pulls": pulls,
        "num_wipes": num_wipes,
    }


def _analyse_one_guild(
    guild: Dict[str, Any],
    *,
    boss_id: int,
    ability_id: Optional[int],
    difficulty: int,
    metric_is_deaths: bool,
    ignore_after_player_deaths: Optional[int],
    start_dt,
    end_dt,
    zone_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Aggregate a single guild's logs over the date range for one boss+ability."""
    guild_id = guild.get("guild_id")
    result: Dict[str, Any] = {"guild": guild, "rows": [], "error": None}

    try:
        reports = _best_reports_in_range(guild_id, start_dt, end_dt, zone_id)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if not reports:
        result["error"] = "no public logs in range"
        return result

    per_value: Dict[str, int] = {}
    per_hits: Dict[str, int] = {}
    per_pulls: Dict[str, int] = {}
    guild_total_pulls = 0

    for rep in reports:
        code = rep.get("code")
        if not code:
            continue
        try:
            rep_data = _analyse_report(
                code,
                boss_id=boss_id,
                ability_id=ability_id,
                difficulty=difficulty,
                metric_is_deaths=metric_is_deaths,
                ignore_after_player_deaths=ignore_after_player_deaths,
            )
        except Exception:
            continue
        if rep_data is None:
            continue  # boss not pulled in this report

        guild_total_pulls += rep_data["num_wipes"]
        for name, n in rep_data["pulls"].items():
            per_pulls[name] = per_pulls.get(name, 0) + n
        for name, v in rep_data["values"].items():
            per_value[name] = per_value.get(name, 0) + v
        for name, h in rep_data["hits"].items():
            per_hits[name] = per_hits.get(name, 0) + h

    if guild_total_pulls == 0:
        result["error"] = "boss not pulled in range (or farm-only)"
        return result

    out_rows: List[Dict[str, Any]] = []
    for player, value in per_value.items():
        row = {
            "player": player,
            "guild": guild.get("guild_name", ""),
            "guild_rank": guild.get("rank"),
            "pulls": per_pulls.get(player, 0),
            "pulls_for_kill": guild_total_pulls,
            "value": int(value),
        }
        if not metric_is_deaths:
            row["hits"] = int(per_hits.get(player, 0))
        out_rows.append(row)

    result["rows"] = out_rows
    result["guild_total_pulls"] = guild_total_pulls
    return result


def aggregate_guilds(
    guilds: List[Dict[str, Any]],
    *,
    boss_id: int,
    ability_id: Optional[int],
    start_dt,
    end_dt,
    zone_id: Optional[int] = None,
    difficulty: int = 5,
    metric_is_deaths: bool = True,
    ignore_after_player_deaths: Optional[int] = None,
    min_attendance_frac: Optional[float] = 0.8,
    max_workers: int = 4,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """
    Analyse every guild in parallel (over the date range) and merge all players.

    If ``min_attendance_frac`` is set (e.g. 0.8), players who attended fewer
    than that fraction of their guild's total pulls in the range are dropped.

    Returns (rows, skipped, stats) where:
      rows    = merged per-player dicts across all guilds, sorted by value desc
      skipped = guilds that produced no usable data, each as {guild, reason}
      stats   = {"total_players", "kept", "filtered_low_attendance"}.
    """
    merged: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total = len(guilds)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _analyse_one_guild,
                g,
                boss_id=boss_id,
                ability_id=ability_id,
                difficulty=difficulty,
                metric_is_deaths=metric_is_deaths,
                ignore_after_player_deaths=ignore_after_player_deaths,
                start_dt=start_dt,
                end_dt=end_dt,
                zone_id=zone_id,
            ): g
            for g in guilds
        }
        for future in as_completed(futures):
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

            res = future.result()
            guild = res["guild"]
            if res["rows"]:
                merged.extend(res["rows"])
            else:
                skipped.append(
                    {
                        "guild": guild,
                        "reason": res["error"] or "no public data for this boss",
                    }
                )

    total_players = len(merged)
    filtered_low = 0
    if min_attendance_frac is not None:
        kept_rows: List[Dict[str, Any]] = []
        for r in merged:
            pfk = r.get("pulls_for_kill", 0)
            if pfk > 0 and r.get("pulls", 0) < min_attendance_frac * pfk:
                filtered_low += 1
                continue
            kept_rows.append(r)
        merged = kept_rows

    merged.sort(key=lambda r: (-r["value"], r["player"].lower()))
    stats = {
        "total_players": total_players,
        "kept": len(merged),
        "filtered_low_attendance": filtered_low,
    }
    return merged, skipped, stats
