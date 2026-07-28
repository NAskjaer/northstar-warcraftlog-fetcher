# src/estimate.py
"""
Heuristic cost/time estimates for a multi-guild (ranking-mode) run.

Warcraft Logs meters usage as an hourly *points* budget (not tokens); every
GraphQL call spends points. We can't know the exact cost up front without
querying, so this module gives a cheap, deterministic estimate from the shape
of the run (how many guilds, how many metric/boss passes) and — if given the
current rate-limit snapshot — whether the budget covers it and how long it
will take, including any waiting for the hourly window(s) to reset.

Kept API-free and pure so the UI can recompute it live on every rerun.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

# Rough per-guild point cost of one metric×boss pass: a report-list query plus
# a handful of fights/actors/events queries for the reports that pulled the
# boss. Deliberately generous so we under-promise on budget.
POINTS_PER_GUILD = 30

# Rough wall-clock seconds of network-bound work per guild per pass (sequential
# report queries dominate). Divided across worker threads for total time.
SECONDS_PER_GUILD = 3.0

# aggregate_guilds() default thread pool size.
DEFAULT_WORKERS = 4

SECONDS_PER_HOUR = 3600


def estimate_points(num_guilds: int, num_passes: int) -> int:
    """Estimated points for `num_guilds` over `num_passes` (metrics × bosses)."""
    return max(0, num_guilds) * max(1, num_passes) * POINTS_PER_GUILD


def estimate_compute_seconds(
    num_guilds: int, num_passes: int, workers: int = DEFAULT_WORKERS
) -> float:
    """Estimated pure compute time (excludes any waiting for budget resets)."""
    if num_guilds <= 0:
        return 0.0
    waves = math.ceil(num_guilds / max(1, workers))
    return max(1, num_passes) * waves * SECONDS_PER_GUILD


def plan(
    num_guilds: int,
    num_passes: int,
    rate_limit: Optional[Dict[str, Any]] = None,
    workers: int = DEFAULT_WORKERS,
) -> Dict[str, Any]:
    """
    Build an estimate dict for a run.

    Always includes ``points`` and ``compute_seconds``. If ``rate_limit`` (as
    returned by api_client.get_rate_limit) is supplied, also includes budget
    feasibility and a ``total_seconds`` that accounts for waiting out one or
    more hourly windows when the run exceeds what's currently available.
    """
    points = estimate_points(num_guilds, num_passes)
    compute_s = estimate_compute_seconds(num_guilds, num_passes, workers)
    out: Dict[str, Any] = {
        "num_guilds": num_guilds,
        "num_passes": num_passes,
        "points": points,
        "compute_seconds": compute_s,
        "total_seconds": compute_s,
    }
    if not rate_limit:
        return out

    limit = int(rate_limit.get("limit_per_hour", 0) or 0)
    spent = float(rate_limit.get("points_spent", 0) or 0)
    reset_in = int(rate_limit.get("points_reset_in", 0) or 0)
    remaining = max(0.0, limit - spent)

    out.update(
        {
            "limit_per_hour": limit,
            "remaining": remaining,
            "reset_in_seconds": reset_in,
        }
    )

    if points <= remaining:
        out["enough_now"] = True
        out["windows_needed"] = 0
        out["wait_seconds"] = 0
        out["total_seconds"] = compute_s
        return out

    # Not enough right now. Spend what's left, then wait for full-window refills.
    out["enough_now"] = False
    deficit = points - remaining
    # Each reset refills up to `limit` points. How many resets must we wait for?
    windows_needed = math.ceil(deficit / limit) if limit > 0 else 0
    # First reset is `reset_in` away; each subsequent one is ~1h later.
    wait_seconds = reset_in + max(0, windows_needed - 1) * SECONDS_PER_HOUR
    out["windows_needed"] = windows_needed
    out["wait_seconds"] = wait_seconds
    out["exceeds_hourly_budget"] = points > limit if limit else False
    out["total_seconds"] = compute_s + wait_seconds
    return out


def format_duration(seconds: float) -> str:
    """Human 'about' duration, e.g. '~45s', '~8m', '~2h 15m'."""
    seconds = int(max(0, round(seconds)))
    if seconds < 60:
        return f"~{seconds}s"
    if seconds < SECONDS_PER_HOUR:
        return f"~{seconds // 60}m"
    hours = seconds // SECONDS_PER_HOUR
    minutes = (seconds % SECONDS_PER_HOUR) // 60
    return f"~{hours}h {minutes}m" if minutes else f"~{hours}h"
