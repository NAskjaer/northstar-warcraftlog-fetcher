# src/log_utils.py
"""Shared terminal-output formatting so progress prints read consistently
across the single-guild, multi-guild, and overnight paths."""
from __future__ import annotations

from typing import Any

# kwarg keys must be valid identifiers ("WCL Report" isn't), so call sites
# pass the short form and it's expanded to the full display label here.
_DISPLAY_LABELS = {"Report": "WCL Report"}


def log_line(**fields: Any) -> None:
    """
    Print one clean "Key: value | Key: value" line, skipping any field whose
    value is None so callers can pass optional context (e.g. a guild name
    that isn't known in single-guild mode) without special-casing it.

    Field order is preserved from call-site kwarg order (Python 3.7+ dicts),
    so callers should pass the most identifying context first, e.g.:
        log_line(Guild=guild_name, Report=report_code, Boss=boss_id, Wipes=n)
        -> "Guild: <name> | WCL Report: <code> | Boss: <id> | Wipes: <n>"
    """
    parts = [
        f"{_DISPLAY_LABELS.get(key, key)}: {value}"
        for key, value in fields.items()
        if value is not None
    ]
    if parts:
        print("  " + " | ".join(parts))
