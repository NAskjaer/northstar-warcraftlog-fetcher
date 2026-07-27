# src/rankings_html_parser.py
"""
Parse the guild table from a Warcraft Logs *progress rankings* page.

WCL renders that table client-side (the data endpoint refuses scraping and the
v2 API has no zone-progress guild ranking), so the app can't fetch it. Instead
the user pastes the page's rendered HTML and we extract the ranked guilds here.

Each row yields: rank, guild id, guild name, realm, region, progress (e.g. 9/9)
and the kill/progress timestamp.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, List

# One capture per guild row. We split on row markers first, then pull fields.
_ROW_SPLIT = re.compile(r'<tr id="row-\d+"')
_RANK_RE = re.compile(r'class="rank[^"]*"[^>]*>\s*(\d+)')
_GUILD_RE = re.compile(
    r'/guild/id/(\d+)/?"[^>]*class="main-table-guild[^"]*"[^>]*>\s*([^<]+?)\s*</a>'
)
_REALM_RE = re.compile(
    r'main-table-realm"[^>]*>\s*([^<(]+?)\s*\(\s*([^)]+?)\s*\)', re.DOTALL
)
_PROGRESS_RE = re.compile(r'main-table-number primary"[^>]*>\s*(\d+)\s*/\s*(\d+)')
_TS_RE = re.compile(r'<span style="display:none">(\d+)\$')


def parse_progress_rankings_html(html_text: str) -> List[Dict[str, Any]]:
    """
    Extract ranked guilds from pasted rankings-page HTML.

    Returns a list ordered by rank, deduped by guild id (the page often repeats
    its <tbody>), each dict:
        {
            "rank": int,
            "guild_id": int,
            "guild_name": str,
            "server_name": str,
            "region": str,
            "progress": str,       # e.g. "9/9"
            "kill_timestamp": int, # ms epoch, or None
        }
    """
    guilds: List[Dict[str, Any]] = []
    seen: set[int] = set()

    for block in _ROW_SPLIT.split(html_text)[1:]:
        gm = _GUILD_RE.search(block)
        if not gm:
            continue
        guild_id = int(gm.group(1))
        if guild_id in seen:
            continue
        seen.add(guild_id)

        rm = _RANK_RE.search(block)
        realm = _REALM_RE.search(block)
        prog = _PROGRESS_RE.search(block)
        ts = _TS_RE.search(block)

        guilds.append(
            {
                "rank": int(rm.group(1)) if rm else len(guilds) + 1,
                "guild_id": guild_id,
                "guild_name": _html.unescape(gm.group(2)),
                "server_name": _html.unescape(realm.group(1)) if realm else "",
                "region": realm.group(2) if realm else "",
                "progress": f"{prog.group(1)}/{prog.group(2)}" if prog else "",
                "kill_timestamp": int(ts.group(1)) if ts else None,
            }
        )

    guilds.sort(key=lambda g: g["rank"])
    return guilds
