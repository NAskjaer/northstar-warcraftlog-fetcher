# src/calendar_fetcher.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .api_client import run_wcl_query
from .deaths_fetcher import get_boss_fights_for_report


def _fetch_reports_for_guild_raw(
    guild_id: int,
    start_ms: int,
    end_ms: int,
    zone_id: int | None = None,
) -> List[Dict[str, Any]]:
    """
    Call WCL's GraphQL API to fetch reports for a guild in the given time window.

    If ``zone_id`` is given, only reports for that raid zone are returned, which
    dramatically cuts the number of reports (skipping Mythic+, alt runs and
    other raids) when scanning many guilds.

    Returns the raw list of report objects: { code, title, startTime, endTime }.
    """
    query = """
    query ($guildID: Int!, $start: Float!, $end: Float!, $zoneID: Int) {
      reportData {
        reports(
          guildID: $guildID
          startTime: $start
          endTime: $end
          zoneID: $zoneID
          limit: 100
        ) {
          data {
            code
            title
            startTime
            endTime
          }
        }
      }
    }
    """

    variables = {
        "guildID": guild_id,
        "start": float(start_ms),
        "end": float(end_ms),
        "zoneID": zone_id,
    }

    result = run_wcl_query(query, variables)

    try:
        reports = result["data"]["reportData"]["reports"]["data"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected reports response from WCL: {result}") from exc

    return reports


def fetch_logs_for_guild(
    guild_id: int,
    start: datetime,
    end: datetime,
    zone_id: int | None = None,
) -> List[Dict[str, Any]]:
    """
    Public helper: fetch reports for a guild between two datetimes (UTC).

    Pass ``zone_id`` to restrict to a single raid zone (much faster when
    scanning many guilds).
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    return _fetch_reports_for_guild_raw(guild_id, start_ms, end_ms, zone_id)


def reports_up_to_first_kill(
    reports: List[Dict[str, Any]],
    boss_id: int,
    difficulty: int | None = 5,
    fights_fn=get_boss_fights_for_report,
) -> List[Dict[str, Any]]:
    """
    Trim a report list to the progression logs: everything up to and including
    the report holding the guild's first kill of ``boss_id``.

    Reports are scanned oldest first and scanning stops at that kill, so farm
    logs after it are never fetched. If the boss was never killed, every report
    is progression and all of them are returned.

    ``fights_fn`` is injectable so callers with a per-run fights cache (see
    multi_guild) pay nothing extra, and so tests can run offline.
    """
    # ponytail: keeps the whole kill report, including any wipes logged after
    # the kill. Filter per-fight if a guild ever re-pulls a boss post-kill in
    # the same log.
    ordered = sorted(reports, key=lambda r: r.get("startTime", 0))
    kept: List[Dict[str, Any]] = []
    for report in ordered:
        kept.append(report)
        if any(f.get("kill") for f in fights_fn(report["code"], boss_id, difficulty)):
            return kept
    return ordered
