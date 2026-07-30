# src/log_utils.py
"""Shared terminal-output formatting so progress prints read consistently
across the single-guild, multi-guild, and overnight paths."""
from __future__ import annotations

from datetime import datetime
from typing import Any

# kwarg keys must be valid identifiers ("WCL Report" isn't), so call sites
# pass the short form and it's expanded to the full display label here.
_DISPLAY_LABELS = {"Report": "WCL Report"}


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_print(message: str) -> None:
    """Print one line prefixed with the current time — for the ad-hoc
    status/retry prints that don't go through log_line's Key: value format
    (e.g. [PAUSED]/[RESUMED]/[RETRY] messages), so every terminal/run.log
    line can be correlated to when it actually happened."""
    print(f"[{_timestamp()}] {message}")


def log_line(**fields: Any) -> None:
    """
    Print one clean "[timestamp] Key: value | Key: value" line, skipping any
    field whose value is None so callers can pass optional context (e.g. a
    guild name that isn't known in single-guild mode) without special-casing
    it.

    Field order is preserved from call-site kwarg order (Python 3.7+ dicts),
    so callers should pass the most identifying context first, e.g.:
        log_line(Guild=guild_name, Report=report_code, Boss=boss_id, Wipes=n)
        -> "[2026-07-30 14:23:05] Guild: <name> | WCL Report: <code> | Boss: <id> | Wipes: <n>"
    """
    parts = [
        f"{_DISPLAY_LABELS.get(key, key)}: {value}"
        for key, value in fields.items()
        if value is not None
    ]
    if parts:
        print(f"[{_timestamp()}]   " + " | ".join(parts))
