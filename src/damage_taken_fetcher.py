from __future__ import annotations

from typing import Any, Dict, List

from .api_client import run_wcl_query
from .deaths_fetcher import get_boss_fights_for_report, _fetch_player_actors


def _fetch_damage_taken_events(
    report_code: str,
    start_time: int,
    end_time: int,
    fight_ids: list[int],
    ability_id: int | None,
) -> List[Dict[str, Any]]:
    """
    Fetch damage taken events for the given fights in [start_time, end_time].

    This uses the DamageTaken dataType and paginates over nextPageTimestamp.
    """
    query = """
    query (
      $code: String!,
      $start: Float!,
      $end: Float!,
      $fightIDs: [Int!],
      $abilityId: Float
    ) {
      reportData {
        report(code: $code) {
          events(
            startTime: $start
            endTime: $end
            dataType: DamageTaken
            fightIDs: $fightIDs
            abilityID: $abilityId
          ) {
            data
            nextPageTimestamp
          }
        }
      }
    }
    """

    events: list[dict[str, Any]] = []
    next_start = float(start_time)

    while True:
        variables = {
            "code": report_code,
            "start": next_start,
            "end": float(end_time),
            "fightIDs": fight_ids,
            # abilityID is Float in the schema
            "abilityId": float(ability_id) if ability_id is not None else None,
        }

        result = run_wcl_query(query, variables)

        try:
            node = result["data"]["reportData"]["report"]["events"]
        except KeyError as exc:
            raise RuntimeError(
                f"Unexpected events response from WCL (damage taken): {result}"
            ) from exc

        events.extend(node.get("data", []))

        next_ts = node.get("nextPageTimestamp")
        if not next_ts:
            break

        # Use nextPageTimestamp as the next startTime
        next_start = float(next_ts)

    return events


def get_damage_taken_by_player_for_ability(
    report_code: str,
    boss_id: int,
    ability_id: int | None,
    difficulty: int | None = 5,
    ignore_after_player_deaths: int | None = None,
) -> List[Dict[str, Any]]:
    """
    For a single report, return total damage TAKEN BY PLAYER for a given boss + ability.

    Output format:
      [
        { "player": "Name", "total_damage": 123456, "hits": 7 },
        ...
      ]

    The ignore_after_player_deaths parameter is accepted for API parity with
    get_deaths_by_player_for_ability, but at the moment it is not applied to
    the damage window (damage is summed over the full boss fights).
    """

    fights = get_boss_fights_for_report(report_code, boss_id, difficulty)

    if not fights:
        print(
            f"  [damage_taken_fetcher] Report {report_code}: "
            f"no fights found for boss {boss_id} (difficulty={difficulty})."
        )
        return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    damage_events = _fetch_damage_taken_events(
        report_code=report_code,
        start_time=start_time,
        end_time=end_time,
        fight_ids=fight_ids,
        ability_id=ability_id,
    )

    print(
        f"  [damage_taken_fetcher] Report {report_code}: "
        f"{len(damage_events)} raw damage events in time window "
        f"(boss_id={boss_id}, ability_id={ability_id})"
    )
    if damage_events:
        print(f"    Sample damage event: {damage_events[0]}")

    # If ability_id is None, we include all damage taken in the boss fights.
    filtered: list[dict[str, Any]] = []
    for ev in damage_events:
        fight_id = ev.get("fight")
        if fight_id not in fight_ids:
            continue

        if ability_id is not None and ev.get("abilityGameID") != ability_id:
            continue

        filtered.append(ev)

    print(
        f"  [damage_taken_fetcher] Report {report_code}: "
        f"{len(filtered)} events for ability {ability_id} in boss fights "
        f"(boss_id={boss_id})"
    )

    if not filtered:
        return []

    # Map actor IDs to player names
    actors_map = _fetch_player_actors(report_code)

    damage_by_player: Dict[str, int] = {}
    hits_by_player: Dict[str, int] = {}

    for ev in filtered:
        target_id = ev.get("targetID")
        if target_id is None:
            continue

        name = actors_map.get(int(target_id), f"ID-{target_id}")

        # 'amount' is the actual damage taken (after mitigation).
        amount = ev.get("amount") or 0
        try:
            amount_int = int(amount)
        except (TypeError, ValueError):
            amount_int = 0

        damage_by_player[name] = damage_by_player.get(name, 0) + amount_int
        hits_by_player[name] = hits_by_player.get(name, 0) + 1

    total_damage = sum(damage_by_player.values())
    total_hits = sum(hits_by_player.values())
    print(
        f"  [damage_taken_fetcher] Report {report_code}: "
        f"{total_damage} total damage and {total_hits} hits across "
        f"{len(damage_by_player)} players for ability {ability_id}."
    )

    rows: List[Dict[str, Any]] = [
        {
            "player": name,
            "total_damage": damage_by_player.get(name, 0),
            "hits": hits_by_player.get(name, 0),
        }
        for name in sorted(damage_by_player.keys())
    ]
    rows.sort(key=lambda r: (-r["total_damage"], r["player"].lower()))
    return rows
