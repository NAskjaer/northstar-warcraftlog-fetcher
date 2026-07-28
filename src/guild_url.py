# src/guild_url.py
"""Shared parsing for Warcraft Logs guild URLs (id-based only, e.g.
https://www.warcraftlogs.com/guild/id/235490)."""
from __future__ import annotations


def parse_guild_id_from_url(url: str) -> int | None:
    """Extract the numeric guild ID from a WCL guild URL, or None if it can't be parsed."""
    try:
        parts = url.strip().strip("/").split("/")
        idx = parts.index("id") + 1
        return int(parts[idx])
    except (ValueError, IndexError):
        return None
