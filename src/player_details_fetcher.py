# src/player_details_fetcher.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .api_client import run_wcl_query

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _split_camel(name: str) -> str:
    """'DemonHunter' -> 'Demon Hunter', 'BeastMastery' -> 'Beast Mastery'."""
    if not name:
        return ""
    return _CAMEL_RE.sub(" ", name)


def get_player_class_specs(
    report_code: str,
    fight_ids: List[int] | None = None,
) -> Dict[str, Tuple[str, str]]:
    """
    Return {player_name: (class_display, spec_display)} for a report.

    Uses WCL's playerDetails field (grouped into tanks/healers/dps), which
    reports each player's class ("type") and spec ("specs[0].spec").
    """
    query = """
    query ($code: String!, $fightIDs: [Int]) {
      reportData {
        report(code: $code) {
          playerDetails(fightIDs: $fightIDs)
        }
      }
    }
    """
    variables: Dict[str, Any] = {"code": report_code, "fightIDs": fight_ids}
    result = run_wcl_query(query, variables)

    try:
        raw = result["data"]["reportData"]["report"]["playerDetails"]
    except KeyError as exc:
        raise RuntimeError(
            f"Unexpected playerDetails response from WCL: {result}"
        ) from exc

    # WCL wraps this JSON scalar as {"data": {"playerDetails": {...}}}; be
    # defensive since the exact nesting isn't guaranteed.
    details = raw
    if isinstance(details, dict) and "data" in details:
        details = details["data"]
    if isinstance(details, dict) and "playerDetails" in details:
        details = details["playerDetails"]

    out: Dict[str, Tuple[str, str]] = {}
    if not isinstance(details, dict):
        return out

    for role_key in ("tanks", "healers", "dps"):
        for entry in details.get(role_key) or []:
            name = entry.get("name")
            if not name:
                continue
            class_raw = entry.get("type") or ""
            specs = entry.get("specs") or []
            spec_raw = ""
            if specs and isinstance(specs[0], dict):
                spec_raw = specs[0].get("spec") or ""

            # Fall back to parsing the icon ("Warrior-Fury") if type/specs
            # weren't populated for this entry.
            if (not class_raw or not spec_raw) and entry.get("icon"):
                icon_parts = str(entry["icon"]).split("-", 1)
                if not class_raw and len(icon_parts) >= 1:
                    class_raw = icon_parts[0]
                if not spec_raw and len(icon_parts) == 2:
                    spec_raw = icon_parts[1]

            out[name] = (_split_camel(class_raw), _split_camel(spec_raw))

    return out
