# src/survivability_fetcher.py

from __future__ import annotations
from typing import Dict

from .deaths_fetcher import (
    get_boss_fights_for_report,
    _fetch_death_events,
    _fetch_player_actors,
)


def compute_survivability_for_report(
    report_code: str,
    boss_id: int,
    difficulty: int,
) -> Dict[str, Dict[str, float]]:
    """
    Compute Warcraft Logs–style survivability for a single report and boss.

    We define survivability as:
        sum_over_all_pulls(time_alive_in_pull_ms) / sum_over_all_pulls(pull_duration_ms)

    This uses:
      - boss fight windows from get_boss_fights_for_report
      - ALL death events for that boss via _fetch_death_events
      - player actor list via _fetch_player_actors

    Returns mapping:
        {
            "PlayerName": {"alive": total_alive_ms, "total": total_fight_ms},
            ...
        }
    """

    fights = get_boss_fights_for_report(report_code, boss_id, difficulty)
    if not fights:
        return {}

    # Full actor list for this report: {actor_id: name}
    actors_id_to_name = _fetch_player_actors(report_code)
    if not actors_id_to_name:
        return {}

    # Time window and fight IDs covering all boss pulls
    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    # Fetch ALL death events in this window for these fights.
    # We pass ignore_after_player_deaths=None so that survivability is
    # ALWAYS based on the true first death timestamp, just like WCL.
    death_events = _fetch_death_events(
        report_code=report_code,
        start_time=start_time,
        end_time=end_time,
        fight_ids=fight_ids,
        ignore_after_player_deaths=None,
    )

    # Build fight_id -> {player_name -> first_death_timestamp}
    deaths_by_fight: Dict[int, Dict[str, int]] = {}

    for ev in death_events:
        if ev.get("type") != "death":
            continue

        fight_id = ev.get("fight")
        target_id = ev.get("targetID")
        ts = ev.get("timestamp")

        if fight_id is None or target_id is None or ts is None:
            continue

        name = actors_id_to_name.get(int(target_id))
        if not name:
            continue

        per_fight = deaths_by_fight.setdefault(int(fight_id), {})
        existing = per_fight.get(name)
        if existing is None or ts < existing:
            per_fight[name] = ts

    # Aggregate alive / total time per player across all pulls
    result: Dict[str, Dict[str, float]] = {}

    # Names of all players that ever appear as actors in this report.
    # This slightly over-approximates presence (we do not have a per-fight
    # roster), but matches the common case where the same raid plays all pulls.
    all_players = list(actors_id_to_name.values())

    for fight in fights:
        fight_id = int(fight["id"])
        start = fight["startTime"]
        end = fight["endTime"]
        fight_total = max(0, end - start)
        if fight_total <= 0:
            continue

        fight_deaths = deaths_by_fight.get(fight_id, {})

        for player in all_players:
            alive = float(fight_total)

            death_ts = fight_deaths.get(player)
            if death_ts is not None:
                dt = death_ts - start
                if 0 <= dt <= fight_total:
                    alive = float(dt)

            rec = result.setdefault(player, {"alive": 0.0, "total": 0.0})
            rec["alive"] += alive
            rec["total"] += float(fight_total)

    return result
