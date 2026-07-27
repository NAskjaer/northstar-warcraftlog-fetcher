# src/uptime_fetcher.py
from __future__ import annotations

from typing import Any, Dict, List

from .api_client import run_wcl_query
from .deaths_fetcher import (
    get_boss_fights_for_report,
    _fetch_player_actors,
    _fetch_player_actors,
)


def _fetch_uptime_table(
    report_code: str,
    start_time: int,
    end_time: int,
    fight_ids: list[int],
    ability_id: int,
) -> list[dict[str, Any]]:
    """
    Call the Warcraft Logs 'table' API for buff/uptime data and return per-target rows.

    NOTE: You need to confirm the exact dataType/arguments in the WCL docs.
    """
    query = """
    query UptimeTable(
      $code: String!,
      $startTime: Float!,
      $endTime: Float!,
      $fightIDs: [Int!]!,
      $abilityID: Float!
    ) {
      reportData {
        report(code: $code) {
          table(
            dataType: Buffs      # <-- or whatever dataType WCL uses for uptime
            startTime: $startTime
            endTime: $endTime
            fightIDs: $fightIDs
            abilityID: $abilityID
            viewBy: Targets      # <- per target
          ) {
            data
          }
        }
      }
    }
    """

    variables = {
        "code": report_code,
        "startTime": int(start_time),
        "endTime": int(end_time),
        "abilityID": ability_id,  # may be None, API is fine with that
    }


    result = run_wcl_query(query, variables)
    # Warcraft Logs GraphQL responses are always wrapped in "data"
    report_data = (
        result.get("data", {})
        .get("reportData", {})
        .get("report", {})
    )

    table = report_data.get("table", {}).get("data", {})

    # For most uptime tables it looks like:
    # {
    #   "entries": [
    #       {
    #           "id": actorId,
    #           "name": "...",
    #           "totalUptime": ...,
    #           "activeTime": ...
    #       },
    #       ...
    #   ]
    # }
    entries = table.get("entries", [])
    return entries



def get_uptime_by_player_for_ability(
    report_code: str,
    boss_id: int,
    ability_id: int,
    difficulty: int | None = 5,
) -> List[Dict[str, Any]]:
    """
    Returns a list of dicts: {"player": str, "avg_uptime": float, "per_fight": {fight_id: float, ...}}
    where uptime values are 0–100 (%).
    """

    fights = get_boss_fights_for_report(
        report_code=report_code,
        boss_id=boss_id,
        difficulty=difficulty,
    )
    if not fights:
        return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["start_time"] for f in fights)
    end_time = max(f["end_time"] for f in fights)

    entries = _fetch_uptime_table(
        report_code=report_code,
        start_time=start_time,
        end_time=end_time,
        fight_ids=fight_ids,
        ability_id=ability_id,
    )


    # Map actor IDs -> names (same helper you already use)
    actors = _fetch_player_actors(report_code)

    rows: list[dict[str, Any]] = []
    for e in entries:
        # You need to match this with whatever WCL actually returns.
        actor_id = e.get("id")
        if actor_id is None:
            continue

        name = actors.get(actor_id, f"ID-{actor_id}")

        # The field that contains uptime will depend on WCL:
        # common is "totalUptime" and "activeTime"; uptime% = totalUptime / activeTime * 100
        total_uptime = e.get("totalUptime", 0)
        active_time = e.get("activeTime", 0)

        if not active_time:
            avg_pct = 0.0
        else:
            avg_pct = 100.0 * float(total_uptime) / float(active_time)

        # If the table has per-fight breakdowns, you can also extract that here
        per_fight = {}
        for f in e.get("subentries", []):
            fid = f.get("fight")
            fuptime = f.get("totalUptime", 0)
            factive = f.get("activeTime", 0) or 0
            if fid is not None and factive:
                per_fight[fid] = 100.0 * fuptime / factive

        rows.append(
            {
                "player": name,
                "avg_uptime": avg_pct,
                "per_fight": per_fight,
            }
        )

    return rows

def get_survivability_by_player(
    report_code: str,
    boss_id: int,
    difficulty: int | None = 5,
    ignore_after_player_deaths: int | None = None,
) -> List[Dict[str, Any]]:
    """
    Compute a survivability percentage per player for *all pulls* of a given boss
    in a single report.

    Definition:
      - For each pull, a player's survivability is the fraction of the pull's
        duration they are alive until their FIRST death in that pull.
      - If they never die in that pull, they count as 100% for that pull.
      - Overall survivability is the time-weighted average across the pulls
        where they appear (sum alive time / sum combat time).

    Returns a list of dicts:

        {
          "player": "Animuzz",
          "alive_ms": 1234567,
          "combat_ms": 1234567,
          "survivability": 100.0,
        }
    """

    # 1) Get all boss fights for this report
    fights = get_boss_fights_for_report(
        report_code=report_code,
        boss_id=boss_id,
        difficulty=difficulty,
    )
    if not fights:
        return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    # 2) Fetch ALL death events for these fights (no wipeCutoff here – we want the real first death)
    death_events = _fetch_death_events(
        report_code=report_code,
        start_time=start_time,
        end_time=end_time,
        fight_ids=fight_ids,
        ignore_after_player_deaths=ignore_after_player_deaths,
    )

    # 3) Map actor IDs → player names (same helper as deaths table)
    actors_map: Dict[int, str] = _fetch_player_actors(report_code)

    # 4) For each (fight, player) remember the FIRST death timestamp
    first_death: Dict[tuple[int, int], float] = {}
    for ev in death_events:
        if ev.get("type") != "death":
            continue

        fight_id = ev.get("fight")
        target_id = ev.get("targetID")
        if fight_id not in fight_ids or target_id is None:
            continue

        ts = float(ev.get("timestamp", 0))
        key = (int(fight_id), int(target_id))
        if key not in first_death or ts < first_death[key]:
            first_death[key] = ts

    # 5) Sum alive/combat time per player across all boss pulls
    alive_ms_by_player: Dict[str, float] = {}
    combat_ms_by_player: Dict[str, float] = {}

    fights_by_id = {f["id"]: f for f in fights}

    for fight_id, fight in fights_by_id.items():
        f_start = float(fight["startTime"])
        f_end = float(fight["endTime"])
        fight_duration = max(0.0, f_end - f_start)

        if fight_duration <= 0:
            continue

        # Only players that ever appear in actors_map are considered
        for actor_id, name in actors_map.items():
            combat_ms_by_player[name] = combat_ms_by_player.get(name, 0.0) + fight_duration

            key = (int(fight_id), int(actor_id))
            death_ts = first_death.get(key)

            if death_ts is None:
                # No death in this pull → alive for the whole fight
                alive_ms = fight_duration
            else:
                alive_ms = max(0.0, min(f_end, float(death_ts)) - f_start)

            alive_ms_by_player[name] = alive_ms_by_player.get(name, 0.0) + alive_ms

    # 6) Build final rows (only players that actually had any combat time)
    rows: List[Dict[str, Any]] = []
    for name, combat_ms in combat_ms_by_player.items():
        if combat_ms <= 0:
            continue

        alive_ms = alive_ms_by_player.get(name, 0.0)
        survivability_pct = 100.0 * alive_ms / combat_ms if combat_ms > 0 else 0.0

        rows.append(
            {
                "player": name,
                "alive_ms": int(alive_ms),
                "combat_ms": int(combat_ms),
                "survivability": survivability_pct,
            }
        )

    return rows

