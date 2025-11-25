# src/calendar_fetcher.py

from datetime import datetime
from typing import List, Dict, Any

from .api_client import run_wcl_query


def _fetch_reports_for_guild_raw(
    guild_id: int,
    start_ms: int,
    end_ms: int,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Low-level helper that actually calls the WCL GraphQL API.

    start_ms / end_ms are UNIX timestamps in milliseconds (ints),
    which are JSON-serializable and what WCL expects.
    """
    query = """
    query ($guildID: Int!, $startTime: Float!, $endTime: Float!, $limit: Int!) {
      reportData {
        reports(
          guildID: $guildID,
          startTime: $startTime,
          endTime: $endTime,
          limit: $limit
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
        "startTime": float(start_ms),
        "endTime": float(end_ms),
        "limit": limit,
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
    Public helper: takes Python datetime objects and converts them to
    milliseconds before querying WCL.

    This is what main.py should call.
    """
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    return _fetch_reports_for_guild_raw(guild_id, start_ms, end_ms)
