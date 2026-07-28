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

# Per-guild point cost of one metric×boss pass. Calibrated against a real
# live run (2026-07-28): a single "First kill" pass for one guild with 19
# progression report-days cost 155.79 points. The old placeholder (30) was
# never actually calibrated and underestimated real cost by ~5x. This is
# still a single flat constant standing in for something that really scales
# with report-days-in-range (a guild that takes longer to first-kill, or a
# wider date range, costs more than a guild that clears fast) — rounded up
# a bit past the one measured data point for margin, but still a rough,
# guild-shape-dependent estimate, not a guarantee.
POINTS_PER_GUILD = 160

# Wall-clock seconds of network-bound work per guild per pass. Calibrated
# from the same live run: that one guild's full sequential analysis (its
# reports are processed one at a time within a guild, only guilds themselves
# run concurrently) took 73.63s. Divided across worker threads for total time.
SECONDS_PER_GUILD = 75.0

# aggregate_guilds() default thread pool size.
DEFAULT_WORKERS = 4

SECONDS_PER_HOUR = 3600

# Standard per-client hourly points budget, used as a fallback assumption
# when no live rate-limit reading is available (see plan() below) — it's
# what warcraftlogs.com issues a normal API client, matching what
# get_rate_limit() reports for an unthrottled account.
ASSUMED_LIMIT_PER_HOUR = 3600


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

    Always includes ``points`` and ``compute_seconds``, and ``total_seconds``
    always accounts for waiting out one or more hourly windows when the run
    needs more points than a single window provides — a run needing 16,000
    points obviously can't finish in ~32m of compute time alone even in the
    best case, and the estimate shouldn't quietly omit that wait just
    because nobody's clicked "Check rate limit" yet.

    If ``rate_limit`` (as returned by api_client.get_rate_limit) is supplied,
    the wait is computed from the *actual* remaining budget and reset
    countdown. Without it, this assumes the best case — a completely fresh
    ASSUMED_LIMIT_PER_HOUR-point window starting right now (``budget_is_
    assumed`` is True in the result so callers can flag the estimate as
    provisional) — rather than silently reporting 0 wait.
    """
    points = estimate_points(num_guilds, num_passes)
    compute_s = estimate_compute_seconds(num_guilds, num_passes, workers)
    out: Dict[str, Any] = {
        "num_guilds": num_guilds,
        "num_passes": num_passes,
        "points": points,
        "compute_seconds": compute_s,
        "total_seconds": compute_s,
        "budget_is_assumed": not rate_limit,
    }

    if rate_limit:
        limit = int(rate_limit.get("limit_per_hour", 0) or 0) or ASSUMED_LIMIT_PER_HOUR
        spent = float(rate_limit.get("points_spent", 0) or 0)
        reset_in = int(rate_limit.get("points_reset_in", 0) or 0)
    else:
        limit = ASSUMED_LIMIT_PER_HOUR
        spent = 0.0
        reset_in = SECONDS_PER_HOUR
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
