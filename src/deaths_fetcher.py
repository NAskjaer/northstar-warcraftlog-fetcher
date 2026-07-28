# src/deaths_fetcher.py
from __future__ import annotations

from typing import Any, Dict, List

from .api_client import run_wcl_query
from .log_utils import log_line


def fetch_all_fights(report_code: str) -> List[Dict[str, Any]]:
    """
    Fetch every fight in a report (all bosses, all difficulties).

    The underlying WCL query has no boss/difficulty filter — it always
    returns the whole report — so this is safe to cache per report_code
    alone (see report_cache.get_report_fights) and reuse across every
    boss/ability target that touches this report.

    Returns a list of ReportFight dicts:
    { id, name, encounterID, difficulty, kill, startTime, endTime, friendlyPlayers }.
    """
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
            friendlyPlayers
          }
        }
      }
    }
    """

    variables = {"code": report_code}
    result = run_wcl_query(query, variables)

    try:
        return result["data"]["reportData"]["report"]["fights"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected fights response from WCL: {result}") from exc


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
    fights = fetch_all_fights(report_code)

    # Filter client-side
    boss_fights: List[Dict[str, Any]] = []
    for f in fights:
        if f.get("encounterID") != boss_id:
            continue
        if difficulty is not None and f.get("difficulty") != difficulty:
            continue
        boss_fights.append(f)

    return boss_fights


def _fetch_death_events(
    report_code: str,
    start_time: int,
    end_time: int,
    fight_ids: list[int],
    ignore_after_player_deaths: int | None,
    ability_id: int | None = None,
) -> List[Dict[str, Any]]:
    """
    Fetch death events for the given fights in [start_time, end_time].

    This ALWAYS:
      - uses the Deaths dataType
      - restricts to the specified fight IDs
      - paginates over nextPageTimestamp
      - optionally applies wipeCutoff when ignore_after_player_deaths > 0

    If ``ability_id`` is given, it's passed server-side as WCL's abilityID
    filter. Measured live against the API: for a report with 148 raw death
    events, the unfiltered fetch cost 22.6 points while the same fetch with
    abilityID cost 1.0 point and returned byte-identical results to the
    client-side match this module used to do alone (abilityGameID == id OR
    killingAbilityGameID == id) — verified across several abilities. The
    caller still re-applies that OR match locally as a cheap no-op safety net.
    """
    query = """
    query (
      $code: String!,
      $start: Float!,
      $end: Float!,
      $fightIDs: [Int!],
      $wipeCutoff: Int,
      $abilityId: Float
    ) {
      reportData {
        report(code: $code) {
          events(
            startTime: $start
            endTime: $end
            dataType: Deaths
            fightIDs: $fightIDs
            wipeCutoff: $wipeCutoff
            abilityID: $abilityId
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
            "abilityId": float(ability_id) if ability_id is not None else None,
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

    return id_to_name


def get_deaths_by_player_for_ability(
    report_code: str,
    boss_id: int,
    ability_id: int | None,
    difficulty: int | None = 5,
    wipes_only: bool = True,
    ignore_after_player_deaths: int | None = None,
    guild_name: str | None = None,
) -> List[Dict[str, Any]]:
    """
    For a single report, return total deaths BY PLAYER for a given boss + ability.

    Output format:
      [
        { "player": "Name", "total_deaths": 7 },
        ...
      ]

    - Filters fights by encounterID and difficulty.
    - If wipes_only is True, only non-kill pulls (kill == False) are counted.
    - Builds a time window covering all those fights.
    - Fetches all death events in that window.
    - Keeps only:
        type == "death"
        fight is one of the boss fights
        (if ability_id is not None)
            abilityGameID == ability_id OR killingAbilityGameID == ability_id

    ``guild_name`` is purely cosmetic — it's only used to prefix the summary
    log line, and is omitted there when the caller doesn't know it (e.g. the
    single-guild UI flow, which never fetches a guild's display name).
    """
    # Deferred import: report_cache imports the raw fetchers from this module,
    # so importing it at module scope here would be circular.
    from . import report_cache

    fights = report_cache.get_boss_fights(report_code, boss_id, difficulty)

    if not fights:
        log_line(Guild=guild_name, Report=report_code, Boss=boss_id,
                  Result="no fights for this boss")
        return []

    # Optionally keep only wipes (non-kill pulls)
    if wipes_only:
        fights = [f for f in fights if not f.get("kill")]
        if not fights:
            log_line(Guild=guild_name, Report=report_code, Boss=boss_id,
                      Result="no wipe fights (kill-only report)")
            return []

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    # Fetch death events for these fights, with optional wipeCutoff. When a
    # specific ability is targeted, it's applied server-side too — far
    # cheaper than fetching every death in the window (see report_cache).
    death_events = report_cache.get_death_events(
        report_code=report_code,
        boss_id=boss_id,
        difficulty=difficulty,
        fight_ids=fight_ids,
        start_time=start_time,
        end_time=end_time,
        wipe_cutoff=ignore_after_player_deaths,
        ability_id=ability_id,
    )

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

    if not filtered:
        log_line(Guild=guild_name, Report=report_code, Boss=boss_id,
                  Wipes=len(fights), Ability=ability_id, Deaths=0)
        return []

    # Map actor IDs to player names
    actors_map = report_cache.get_report_actors(report_code)

    # Count deaths per player (targetID)
    deaths_by_player: Dict[str, int] = {}
    for ev in filtered:
        target_id = ev.get("targetID")
        if target_id is None:
            continue
        name = actors_map.get(int(target_id), f"ID-{target_id}")
        deaths_by_player[name] = deaths_by_player.get(name, 0) + 1

    log_line(
        Guild=guild_name, Report=report_code, Boss=boss_id,
        Wipes=len(fights), Ability=ability_id,
        Deaths=f"{sum(deaths_by_player.values())} across {len(deaths_by_player)} players",
    )

    # Convert to sorted list
    rows: List[Dict[str, Any]] = [
        {"player": name, "total_deaths": count}
        for name, count in deaths_by_player.items()
    ]
    rows.sort(key=lambda r: (-r["total_deaths"], r["player"].lower()))
    return rows
