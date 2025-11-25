# src/calendar_fetcher.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .api_client import run_wcl_query


def _fetch_reports_for_guild_raw(
    guild_id: int,
    start_ms: int,
    end_ms: int,
) -> List[Dict[str, Any]]:
    """
    Call WCL's GraphQL API to fetch reports for a guild in the given time window.

    Returns the raw list of report objects: { code, title, startTime, endTime }.
    """
    query = """
    query ($guildID: Int!, $start: Float!, $end: Float!) {
      reportData {
        reports(
          guildID: $guildID
          startTime: $start
          endTime: $end
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
) -> List[Dict[str, Any]]:
    """
    Public helper: fetch reports for a guild between two datetimes (UTC).
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    return _fetch_reports_for_guild_raw(guild_id, start_ms, end_ms)
