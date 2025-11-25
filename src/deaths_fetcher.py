# src/deaths_fetcher.py

from __future__ import annotations

from typing import Dict, List, Any, Optional
from collections import defaultdict

from src.api_client import run_wcl_query


def get_boss_fights_for_report(
    report_code: str,
    boss_id: int,
    difficulty: int = 5,
) -> List[Dict[str, Any]]:
    """
    Return all fights for a given boss in a report, filtered by encounterID and difficulty.
    """
    query = """
    query ($code: String!) {
      reportData {
        report(code: $code) {
          fights {
            id
            name
            difficulty
            kill
            startTime
            endTime
            encounterID
          }
        }
      }
    }
    """

    variables = {"code": report_code}
    result = run_wcl_query(query, variables)

    try:
        all_fights = result["data"]["reportData"]["report"]["fights"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected fights response from WCL: {result}") from exc

    fights = [
        f
        for f in all_fights
        if f.get("encounterID") == boss_id and f.get("difficulty") == difficulty
    ]

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{len(fights)} fights for boss {boss_id} at difficulty {difficulty}"
    )

    return fights


def get_actor_name_map(report_code: str) -> Dict[int, str]:
    """
    Fetch masterData.actors for a report and build an ID -> name map.
    This lets us turn targetID into player names.
    """
    query = """
    query ($code: String!) {
      reportData {
        report(code: $code) {
          masterData {
            actors {
              id
              name
              type
            }
          }
        }
      }
    }
    """

    variables = {"code": report_code}
    result = run_wcl_query(query, variables)

    try:
        actors = result["data"]["reportData"]["report"]["masterData"]["actors"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected masterData response from WCL: {result}") from exc

    id_to_name: Dict[int, str] = {}
    for actor in actors:
        actor_id = actor.get("id")
        name = actor.get("name")
        if actor_id is None or not name:
            continue
        # We could filter on actor["type"] == "Player" if needed,
        # but usually only players have deaths to this ability.
        id_to_name[actor_id] = name

    print(
        f"  [deaths_fetcher] Report {report_code}: loaded {len(id_to_name)} actors."
    )

    return id_to_name


def _fetch_death_events(
    report_code: str,
    start_time: int,
    end_time: int,
    fight_ids: List[int],
) -> List[Dict[str, Any]]:
    """
    Fetch all death events for the given fights, handling pagination.
    """
    query = """
    query ($code: String!, $startTime: Float!, $endTime: Float!, $fightIDs: [Int!]) {
      reportData {
        report(code: $code) {
          events(
            startTime: $startTime
            endTime: $endTime
            dataType: Deaths
            fightIDs: $fightIDs
          ) {
            data
            nextPageTimestamp
          }
        }
      }
    }
    """

    all_events: List[Dict[str, Any]] = []
    page_start = start_time

    while True:
        variables = {
            "code": report_code,
            "startTime": float(page_start),
            "endTime": float(end_time),
            "fightIDs": fight_ids,
        }
        result = run_wcl_query(query, variables)

        try:
            events_obj = result["data"]["reportData"]["report"]["events"]
        except KeyError as exc:
            raise RuntimeError(f"Unexpected events response from WCL: {result}") from exc

        data = events_obj.get("data", [])
        all_events.extend(data)

        next_page = events_obj.get("nextPageTimestamp")
        if not next_page:
            break

        page_start = next_page

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{len(all_events)} total death events for fights {fight_ids}"
    )

    if all_events:
        print("    Sample death event:", all_events[0])

    return all_events


def get_deaths_by_player_for_ability(
    report_code: str,
    boss_id: int,
    difficulty: int = 5,
    ability_id: int | None = None,
    wipes_only: bool = True,
    max_deaths_per_pull: int | None = None,  # currently ignored (feature dropped)
) -> List[Dict[str, Any]]:
    """
    For a single report, return a list of players and how many times they
    died to a specific *killing* ability on a given boss.

    Returned format:
        [
          {"player": "Player1", "pulls": 10, "total_deaths": 3},
          ...
        ]

    Notes:
      - `wipes_only=True` => only non-kill pulls are counted.
      - `max_deaths_per_pull` is currently NOT applied. All deaths to the
        chosen ability are counted (this is the stable behaviour we had before).
    """
    # 1) Fights for this boss
    fights = get_boss_fights_for_report(report_code, boss_id, difficulty)
    if not fights:
        print(f"  [deaths_fetcher] Report {report_code}: no fights for this boss.")
        return []

    if wipes_only:
        before = len(fights)
        fights = [f for f in fights if not f.get("kill")]
        print(
            f"  [deaths_fetcher] Report {report_code}: "
            f"{before} fights -> {len(fights)} wipes after filtering."
        )

    if not fights:
        print(f"  [deaths_fetcher] Report {report_code}: no wipes remaining.")
        return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    # 2) Fetch death events
    events = _fetch_death_events(report_code, start_time, end_time, fight_ids)
    if not events:
        print(f"  [deaths_fetcher] Report {report_code}: no death events found.")
        return []

    actor_name_map = get_actor_name_map(report_code)

    counts: Dict[str, int] = {}

    for ev in events:
        # Filter by killing ability if provided
        if ability_id is not None:
            killing_ability = ev.get("killingAbilityGameID")
            # Some logs use abilityGameID, but for the "Total Deaths by Player"
            # per ability, killingAbilityGameID is usually what matters.
            if killing_ability != ability_id:
                continue

        target_id = ev.get("targetID")
        if target_id is None:
            continue

        player_name = actor_name_map.get(target_id)
        if not player_name:
            continue

        counts[player_name] = counts.get(player_name, 0) + 1

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{sum(counts.values())} deaths across {len(counts)} players "
        f"for ability {ability_id}."
    )

    if not counts:
        return []

    pulls = len(fight_ids)

    rows: List[Dict[str, Any]] = [
        {"player": name, "pulls": pulls, "total_deaths": deaths}
        for name, deaths in counts.items()
    ]
    rows.sort(key=lambda r: (-r["total_deaths"], r["player"].lower()))

    return rows
