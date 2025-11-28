# src/deaths_fetcher.py
from __future__ import annotations

from typing import Any, Dict, List

from .api_client import run_wcl_query


def get_boss_fights_for_report(
    report_code: str,
    boss_id: int,
    difficulty: int | None = 5,
) -> List[Dict[str, Any]]:
    """
    Fetch all fights for a report, then filter by encounterID (boss) and difficulty.

    Returns a list of ReportFight dicts:
    { id, name, encounterID, difficulty, kill, startTime, endTime }.
    """
    # DEBUG: what are we about to query for?
    print(
        f"  [deaths_fetcher] get_boss_fights_for_report("
        f"report={report_code}, boss_id={boss_id}, difficulty={difficulty})"
    )

    query = """
    query ($code: String!) {
      reportData {
        report(code: $code) {
          fights {
            id
            name
            encounterID
            difficulty
            kill
            startTime
            endTime
          }
        }
      }
    }
    """

    variables = {"code": report_code}
    result = run_wcl_query(query, variables)

    try:
        fights = result["data"]["reportData"]["report"]["fights"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected fights response from WCL: {result}") from exc

    # Filter client-side
    boss_fights: List[Dict[str, Any]] = []
    for f in fights:
        if f.get("encounterID") != boss_id:
            continue
        if difficulty is not None and f.get("difficulty") != difficulty:
            continue
        boss_fights.append(f)

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{len(fights)} raw fights, {len(boss_fights)} boss fights "
        f"(boss_id={boss_id}, difficulty={difficulty})"
    )

    return boss_fights

def _fetch_death_events(
    report_code: str,
    start_time: int,
    end_time: int,
    fight_ids: list[int],
    ignore_after_player_deaths: int | None,
) -> List[Dict[str, Any]]:
    """
    Fetch death events for the given fights in [start_time, end_time].

    This ALWAYS:
      - uses the Deaths dataType
      - restricts to the specified fight IDs
      - paginates over nextPageTimestamp
      - optionally applies wipeCutoff when ignore_after_player_deaths > 0
    """
    query = """
    query (
      $code: String!,
      $start: Float!,
      $end: Float!,
      $fightIDs: [Int!],
      $wipeCutoff: Int
    ) {
      reportData {
        report(code: $code) {
          events(
            startTime: $start
            endTime: $end
            dataType: Deaths
            fightIDs: $fightIDs
            wipeCutoff: $wipeCutoff
          ) {
            data
            nextPageTimestamp
          }
        }
      }
    }
    """

    cutoff = (
        int(ignore_after_player_deaths)
        if ignore_after_player_deaths and ignore_after_player_deaths > 0
        else None
    )

    events: list[dict[str, Any]] = []
    next_start = float(start_time)

    while True:
        variables = {
            "code": report_code,
            "start": next_start,
            "end": float(end_time),
            "fightIDs": fight_ids,
            "wipeCutoff": cutoff,
        }

        result = run_wcl_query(query, variables)

        try:
            node = result["data"]["reportData"]["report"]["events"]
        except KeyError as exc:
            raise RuntimeError(
                f"Unexpected events response from WCL: {result}"
            ) from exc

        events.extend(node.get("data", []))

        next_ts = node.get("nextPageTimestamp")
        if not next_ts:
            break

        # Use nextPageTimestamp as the next startTime
        next_start = float(next_ts)

    return events

def _fetch_player_actors(report_code: str) -> Dict[int, str]:
    """
    Fetch player actors (id -> name) for this report.

    Uses masterData.actors(type: "Player").
    """
    query = """
    query ($code: String!) {
      reportData {
        report(code: $code) {
          masterData {
            actors(type: "Player") {
              id
              name
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
        if actor_id is None or name is None:
            continue
        id_to_name[int(actor_id)] = str(name)

    print(f"  [deaths_fetcher] Report {report_code}: loaded {len(id_to_name)} actors.")
    return id_to_name

def get_deaths_by_player_for_ability(
    report_code: str,
    boss_id: int,
    ability_id: int,
    difficulty: int | None = 5,
    ignore_after_player_deaths: int | None = None,
) -> List[Dict[str, Any]]:


    """
    For a single report, return total deaths BY PLAYER for a given boss + ability.

    Output format:
      [
        { "player": "Name", "total_deaths": 7 },
        ...
      ]

    - Filters fights by encounterID and difficulty.
    - Builds a time window covering all those fights.
    - Fetches all death events in that window.
    - Keeps only:
        type == "death"
        fight is one of the boss fights
        abilityGameID == ability_id OR killingAbilityGameID == ability_id
    """
    fights = get_boss_fights_for_report(report_code, boss_id, difficulty)

    if not fights:
        print(
            f"  [deaths_fetcher] Report {report_code}: "
            f"no fights found for boss {boss_id} (difficulty={difficulty})."
        )
        return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    # Fetch death events for these fights, with optional wipeCutoff
    death_events = _fetch_death_events(
        report_code=report_code,
        start_time=start_time,
        end_time=end_time,
        fight_ids=fight_ids,
        ignore_after_player_deaths=ignore_after_player_deaths,
    )

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{len(death_events)} raw death events in time window "
        f"(boss_id={boss_id}, ability_id={ability_id})"
    )
    if death_events:
        print(f"    Sample death event: {death_events[0]}")


    # Filter down to:
    #   - the boss fights
    #   - optional ability_id match (abilityGameID OR killingAbilityGameID)
    filtered: List[Dict[str, Any]] = []
    for ev in death_events:
        if ev.get("type") != "death":
            continue

        fight_id = ev.get("fight")
        if fight_id not in fight_ids:
            continue

        if ability_id is not None:
            ability_match = (
                ev.get("abilityGameID") == ability_id
                or ev.get("killingAbilityGameID") == ability_id
            )
            if not ability_match:
                continue

        filtered.append(ev)

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{len(filtered)} events for ability {ability_id} in boss fights "
        f"(boss_id={boss_id})"
    )

    if not filtered:
        return []

    # Map actor IDs to player names
    actors_map = _fetch_player_actors(report_code)

    # Count deaths per player (targetID)
    deaths_by_player: Dict[str, int] = {}
    for ev in filtered:
        target_id = ev.get("targetID")
        if target_id is None:
            continue
        name = actors_map.get(int(target_id), f"ID-{target_id}")
        deaths_by_player[name] = deaths_by_player.get(name, 0) + 1

    print(
        f"  [deaths_fetcher] Report {report_code}: "
        f"{sum(deaths_by_player.values())} deaths across {len(deaths_by_player)} "
        f"players for ability {ability_id}."
    )

    # Convert to sorted list
    rows: List[Dict[str, Any]] = [
        {"player": name, "total_deaths": count}
        for name, count in deaths_by_player.items()
    ]
    rows.sort(key=lambda r: (-r["total_deaths"], r["player"].lower()))
    return rows
