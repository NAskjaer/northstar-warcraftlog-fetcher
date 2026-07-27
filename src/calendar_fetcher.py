# src/calendar_fetcher.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .api_client import run_wcl_query


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
