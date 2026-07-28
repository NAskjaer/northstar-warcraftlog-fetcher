# src/guild_rankings_fetcher.py
"""
Fetch a ranked list of guilds for an encounter via Warcraft Logs' public
`fightRankings` API.

The public v2 API does not expose the zone "progress" guild ranking shown on
the website (and WCL blocks scraping that page). `fightRankings` is the
supported way to get a ranked list of top guilds for a boss: it returns, in
ranking order, the guilds that have a public logged kill, each with the report
code we need to analyse their pulls.

Consequence: guilds that log privately never appear here (they have no public
ranked kill), so an API-sourced list only ever contains public-logging guilds.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .api_client import run_wcl_query

# WCL numeric region id -> serverRegion compact name used by the API.
REGION_ID_TO_SLUG: Dict[int, str] = {
    1: "US",
    2: "EU",
    3: "KR",
    4: "TW",
    5: "CN",
}

_FIGHT_RANKINGS_QUERY = """
query (
  $encounterID: Int!,
  $difficulty: Int!,
  $metric: FightRankingMetricType!,
  $serverRegion: String,
  $page: Int!
) {
  worldData {
    encounter(id: $encounterID) {
      fightRankings(
        difficulty: $difficulty
        metric: $metric
        serverRegion: $serverRegion
        page: $page
      )
    }
  }
}
"""


# NOTE: There is intentionally no API-based progress-ranking builder here.
# `fightRankings` returns each guild's BEST-PARSE kill, whose timestamp is that
# (often later, optimized) kill — not their world-first progression kill — so
# sorting it by time does NOT reproduce WCL's progress order (verified: it
# surfaced unrelated guilds instead of Echo/Method/…). The true progress order
# is only available from the rankings page itself, which the app ingests via
# pasted HTML (see rankings_html_parser + guild_rankings_store).


_ENCOUNTER_ZONE_QUERY = """
query ($id: Int!) {
  worldData { encounter(id: $id) { zone { id } } }
}
"""


def get_encounter_zone_id(encounter_id: int) -> Optional[int]:
    """Return the raid zone id an encounter belongs to (or None)."""
    try:
        result = run_wcl_query(_ENCOUNTER_ZONE_QUERY, {"id": encounter_id})
        return result["data"]["worldData"]["encounter"]["zone"]["id"]
    except Exception:
        return None


_GUILD_INFO_QUERY = """
query ($id: Int!) {
  guildData {
    guild(id: $id) {
      name
      server { name region { compactName } }
    }
  }
}
"""


def get_guild_info(guild_id: int) -> Optional[Dict[str, Any]]:
    """
    Resolve a guild ID to its display name/server/region (~1 point, measured
    live). Used for the "by guild links" input mode, where all the app
    starts with is a numeric ID parsed out of a pasted URL — the stored
    world-progress ranking already carries real names for free, so this
    lookup is only needed for manually-entered guilds. Returns None if the
    guild doesn't exist or the lookup fails.
    """
    try:
        result = run_wcl_query(_GUILD_INFO_QUERY, {"id": guild_id})
        guild = result["data"]["guildData"]["guild"]
        if not guild:
            return None
        server = guild.get("server") or {}
        region = (server.get("region") or {}).get("compactName", "")
        return {
            "guild_name": guild.get("name") or f"Guild {guild_id}",
            "server_name": server.get("name", ""),
            "region": region,
        }
    except Exception:
        return None


def get_ranked_guilds(
    encounter_id: int,
    *,
    difficulty: int = 5,
    region_id: Optional[int] = 2,
    rank_start: int = 1,
    rank_end: int = 50,
    metric: str = "execution",
    max_pages: int = 40,
) -> List[Dict[str, Any]]:
    """
    Return guilds ranked on `encounter_id`, deduped by guild, sliced to the
    inclusive 1-based rank range [rank_start, rank_end].

    Each returned dict:
        {
            "rank": int,            # 1-based position in the ranking
            "guild_id": int,
            "guild_name": str,
            "server_name": str,
            "region": str,
            "report_code": str,     # report containing this guild's ranked kill
            "fight_id": int,
            "start_time": int,      # ms epoch of the ranked fight
        }
    """
    if rank_start < 1:
        rank_start = 1
    if rank_end < rank_start:
        rank_end = rank_start

    server_region = REGION_ID_TO_SLUG.get(region_id) if region_id else None

    guilds: List[Dict[str, Any]] = []
    seen_guild_ids: set[int] = set()
    page = 1

    while len(guilds) < rank_end and page <= max_pages:
        variables = {
            "encounterID": encounter_id,
            "difficulty": difficulty,
            "metric": metric,
            "serverRegion": server_region,
            "page": page,
        }
        result = run_wcl_query(_FIGHT_RANKINGS_QUERY, variables)

        try:
            node = result["data"]["worldData"]["encounter"]["fightRankings"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected fightRankings response from WCL: {result}"
            ) from exc

        rankings = (node or {}).get("rankings", []) if isinstance(node, dict) else []
        if not rankings:
            break

        for entry in rankings:
            guild = entry.get("guild") or {}
            guild_id = guild.get("id")
            if guild_id is None or guild_id in seen_guild_ids:
                continue
            seen_guild_ids.add(guild_id)

            report = entry.get("report") or {}
            server = entry.get("server") or {}
            guilds.append(
                {
                    "rank": len(guilds) + 1,
                    "guild_id": int(guild_id),
                    "guild_name": guild.get("name", f"Guild {guild_id}"),
                    "server_name": server.get("name", ""),
                    "region": server.get("region", server_region or ""),
                    "report_code": report.get("code"),
                    "fight_id": report.get("fightID"),
                    "start_time": report.get("startTime"),
                }
            )

        if not (isinstance(node, dict) and node.get("hasMorePages")):
            break
        page += 1

    # Slice to the requested inclusive 1-based rank range.
    return guilds[rank_start - 1 : rank_end]
