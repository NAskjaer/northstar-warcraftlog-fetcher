# src/multi_guild.py
"""
Aggregate per-player deaths / damage-taken across many guilds at once.

Given a ranked list of guilds (from guild_rankings_fetcher), analyse each
guild's logs over a date range exactly like the single-guild flow does:
group reports by day, take the longest report per day, and sum per-player
deaths/hits AND per-player pulls (wipe attendance) across them.

That gives, per guild:
  * guild_total_pulls  -- total wipe pulls across the range (their progression)
  * per-player pulls    -- how many of those pulls each player attended

which lets us drop players who attended far fewer pulls than their guild
(and would therefore look "clean" simply for sitting out).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .api_client import is_retryable_error
from .calendar_fetcher import reports_up_to_first_kill
from .log_utils import log_line
from . import report_cache
from .report_cache import reset_report_caches

# Re-exported for existing importers (ui/app.py, overnight.py) — the actual
# per-run cache now lives in report_cache so it's shared with the
# single-guild flow instead of duplicating fetches across the two paths.
cached_boss_fights = report_cache.get_boss_fights

# Per-guild report concurrency once the report list is fixed (after any
# first-kill scan, which stays sequential — see _analyse_one_guild). Guilds
# themselves also run concurrently via aggregate_guilds' own pool, so total
# in-flight threads can be guild-workers × this; kept modest for that reason.
_REPORT_WORKERS = 6


def _best_reports_in_range(
    guild_id: int, start_dt, end_dt, zone_id: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Reports for the guild in range, keeping the longest report per day."""
    reports = report_cache.get_guild_reports(guild_id, start_dt, end_dt, zone_id)
    if not reports:
        return []
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for rep in reports:
        start_ms = rep.get("startTime", 0)
        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(rep)

    def _duration(r: Dict[str, Any]) -> int:
        s = r.get("startTime", 0)
        return r.get("endTime", s) - s

    return [max(reps, key=_duration) for reps in by_date.values()]


def _guild_label(guild: Dict[str, Any]) -> str:
    """'Northstar (235490)' for log lines — name alone is ambiguous/blank
    for "by guild links" entries before their name lookup resolves."""
    name = guild.get("guild_name") or f"Guild {guild.get('guild_id')}"
    return f"{name} ({guild.get('guild_id')})"


def class_spec_label(class_spec: Optional[Tuple[str, str]]) -> str:
    """'Warrior (Fury)' from a (class, spec) pair — shared by every results
    table (single-guild, live multi-guild, overnight CSVs) so "Class" reads
    the same everywhere."""
    cls, spec = class_spec or ("", "")
    if cls and spec:
        return f"{cls} ({spec})"
    if cls:
        return cls
    return "Unknown"


def _analyse_report(
    report_code: str,
    *,
    boss_id: int,
    ability_id: Optional[int],
    difficulty: int,
    metric_is_deaths: bool,
    ignore_after_player_deaths: Optional[int],
) -> Optional[Dict[str, Any]]:
    """
    Analyse one report's WIPE pulls for a boss + ability.

    Fetches fights, actors and events once each (no redundant calls) and returns
    per-player values, per-player pulls, and the wipe-pull count, or None if the
    boss isn't present in this report.
    """
    fights = report_cache.get_boss_fights(report_code, boss_id, difficulty)
    if not fights:
        return None
    # Wipes only, to match the rest of the app.
    fights = [f for f in fights if not f.get("kill", False)]
    if not fights:
        return None

    fight_ids = [f["id"] for f in fights]
    start_time = min(f["startTime"] for f in fights)
    end_time = max(f["endTime"] for f in fights)

    actors = report_cache.get_report_actors(report_code)
    class_specs = report_cache.get_report_class_specs(report_code, boss_id, fight_ids)

    # Per-player pulls (attendance) + total wipe pulls in this report.
    pulls: Dict[str, int] = {}
    for f in fights:
        for actor_id in f.get("friendlyPlayers") or []:
            name = actors.get(int(actor_id))
            if name:
                pulls[name] = pulls.get(name, 0) + 1
    num_wipes = len(fights)

    values: Dict[str, int] = {}
    hits: Dict[str, int] = {}

    if metric_is_deaths:
        events = report_cache.get_death_events(
            report_code=report_code,
            boss_id=boss_id,
            difficulty=difficulty,
            fight_ids=fight_ids,
            start_time=start_time,
            end_time=end_time,
            wipe_cutoff=ignore_after_player_deaths,
            ability_id=ability_id,
        )
        for ev in events:
            if ev.get("type") != "death":
                continue
            if ev.get("fight") not in fight_ids:
                continue
            if ability_id is not None and not (
                ev.get("abilityGameID") == ability_id
                or ev.get("killingAbilityGameID") == ability_id
            ):
                continue
            target_id = ev.get("targetID")
            if target_id is None:
                continue
            name = actors.get(int(target_id), f"ID-{target_id}")
            values[name] = values.get(name, 0) + 1
    else:
        events = report_cache.get_damage_events(
            report_code=report_code,
            boss_id=boss_id,
            difficulty=difficulty,
            fight_ids=fight_ids,
            start_time=start_time,
            end_time=end_time,
            ability_id=ability_id,
        )
        for ev in events:
            if ev.get("fight") not in fight_ids:
                continue
            if ability_id is not None and ev.get("abilityGameID") != ability_id:
                continue
            target_id = ev.get("targetID")
            if target_id is None:
                continue
            name = actors.get(int(target_id), f"ID-{target_id}")
            values[name] = values.get(name, 0) + int(ev.get("amount") or 0)
            hits[name] = hits.get(name, 0) + 1

    return {
        "values": values,
        "hits": hits,
        "pulls": pulls,
        "num_wipes": num_wipes,
        "class_specs": class_specs,
    }


def _analyse_one_guild(
    guild: Dict[str, Any],
    *,
    boss_id: int,
    ability_id: Optional[int],
    difficulty: int,
    metric_is_deaths: bool,
    ignore_after_player_deaths: Optional[int],
    start_dt,
    end_dt,
    zone_id: Optional[int] = None,
    stop_at_first_kill: bool = False,
) -> Dict[str, Any]:
    """Aggregate a single guild's logs over the date range for one boss+ability."""
    guild_id = guild.get("guild_id")
    result: Dict[str, Any] = {"guild": guild, "rows": [], "error": None}

    try:
        reports = _best_reports_in_range(guild_id, start_dt, end_dt, zone_id)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if not reports:
        result["error"] = "no public logs in range"
        return result

    if stop_at_first_kill:
        # Uses the per-run fights cache, so this costs nothing the analysis
        # below wouldn't have paid anyway.
        try:
            reports = reports_up_to_first_kill(
                reports,
                boss_id,
                difficulty,
                fights_fn=cached_boss_fights,
            )
        except Exception as exc:
            result["error"] = f"could not locate first kill: {exc}"
            return result

    per_value: Dict[str, int] = {}
    per_hits: Dict[str, int] = {}
    per_pulls: Dict[str, int] = {}
    per_class_spec: Dict[str, Tuple[str, str]] = {}
    guild_total_pulls = 0

    codes = [rep.get("code") for rep in reports if rep.get("code")]

    # The first-kill SCAN above has to be sequential (you can't know where
    # to stop without checking oldest-to-newest), but once the report list
    # is fixed, analysing them has no ordering dependency — values/pulls
    # just get summed. That made the sequential version the actual
    # wall-clock bottleneck (~1 network round-trip per report, one at a
    # time — not extra API calls, every report here was getting fetched
    # regardless of order), so these run concurrently like guilds already do.
    with ThreadPoolExecutor(max_workers=min(_REPORT_WORKERS, max(1, len(codes)))) as ex:
        futures = {
            ex.submit(
                _analyse_report,
                code,
                boss_id=boss_id,
                ability_id=ability_id,
                difficulty=difficulty,
                metric_is_deaths=metric_is_deaths,
                ignore_after_player_deaths=ignore_after_player_deaths,
            ): code
            for code in codes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                rep_data = future.result()
            except Exception as exc:
                if is_retryable_error(str(exc)):
                    # A transient failure (rate limit / network) somewhere in
                    # this guild's reports means we don't actually know its
                    # full totals — discard whatever's accumulated so far and
                    # bail out with a retryable error rather than returning
                    # partial/undercounted data that looks complete to the
                    # caller. (Other in-flight reports finish before this
                    # function returns — the `with` block waits for them —
                    # but their results are simply discarded.)
                    result["error"] = f"transient API failure on report {code}: {exc}"
                    return result
                continue
            if rep_data is None:
                continue  # boss not pulled in this report

            guild_total_pulls += rep_data["num_wipes"]
            for name, n in rep_data["pulls"].items():
                per_pulls[name] = per_pulls.get(name, 0) + n
            for name, v in rep_data["values"].items():
                per_value[name] = per_value.get(name, 0) + v
            for name, h in rep_data["hits"].items():
                per_hits[name] = per_hits.get(name, 0) + h
            for name, cs in rep_data["class_specs"].items():
                per_class_spec.setdefault(name, cs)

            if metric_is_deaths:
                metric_summary = (
                    f"{sum(rep_data['values'].values())} across "
                    f"{len(rep_data['values'])} players"
                )
            else:
                metric_summary = (
                    f"{sum(rep_data['hits'].values())} across "
                    f"{len(rep_data['values'])} players "
                    f"({sum(rep_data['values'].values()):,} damage)"
                )
            log_line(
                Guild=_guild_label(guild), Report=code, Boss=boss_id,
                Wipes=rep_data["num_wipes"],
                **{"Deaths" if metric_is_deaths else "Hits": metric_summary},
            )

    if guild_total_pulls == 0:
        result["error"] = "boss not pulled in range (or farm-only)"
        return result

    out_rows: List[Dict[str, Any]] = []
    for player, value in per_value.items():
        row = {
            "player": player,
            "guild": guild.get("guild_name", ""),
            "guild_rank": guild.get("rank"),
            "pulls": per_pulls.get(player, 0),
            "pulls_for_kill": guild_total_pulls,
            "value": int(value),
            "class_spec": per_class_spec.get(player, ("", "")),
        }
        if not metric_is_deaths:
            row["hits"] = int(per_hits.get(player, 0))
        out_rows.append(row)

    result["rows"] = out_rows
    result["guild_total_pulls"] = guild_total_pulls
    return result


def aggregate_guilds(
    guilds: List[Dict[str, Any]],
    *,
    boss_id: int,
    ability_id: Optional[int],
    start_dt,
    end_dt,
    zone_id: Optional[int] = None,
    difficulty: int = 5,
    metric_is_deaths: bool = True,
    ignore_after_player_deaths: Optional[int] = None,
    min_attendance_frac: Optional[float] = 0.8,
    stop_at_first_kill: bool = False,
    max_workers: int = 4,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """
    Analyse every guild in parallel (over the date range) and merge all players.

    If ``min_attendance_frac`` is set (e.g. 0.8), players who attended fewer
    than that fraction of their guild's total pulls in the range are dropped.

    With ``stop_at_first_kill``, each guild's logs are cut off at its own first
    kill of the boss, so only that guild's progression pulls are counted.

    Returns (rows, skipped, stats) where:
      rows    = merged per-player dicts across all guilds, sorted by value desc
      skipped = guilds that produced no usable data, each as {guild, reason}
      stats   = {"total_players", "kept", "filtered_low_attendance"}.
    """
    merged: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total = len(guilds)
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _analyse_one_guild,
                g,
                boss_id=boss_id,
                ability_id=ability_id,
                difficulty=difficulty,
                metric_is_deaths=metric_is_deaths,
                ignore_after_player_deaths=ignore_after_player_deaths,
                start_dt=start_dt,
                end_dt=end_dt,
                zone_id=zone_id,
                stop_at_first_kill=stop_at_first_kill,
            ): g
            for g in guilds
        }
        for future in as_completed(futures):
            completed += 1
            res = future.result()
            guild = res["guild"]
            if progress_callback:
                progress_callback(completed, total, _guild_label(guild))

            if res["rows"]:
                merged.extend(res["rows"])
                log_line(
                    Guild=_guild_label(guild),
                    Result=f"done — {len(res['rows'])} players, "
                           f"{res.get('guild_total_pulls', 0)} pulls",
                )
            else:
                reason = res["error"] or "no public data for this boss"
                skipped.append({"guild": guild, "reason": reason})
                log_line(Guild=_guild_label(guild), Result=f"skipped — {reason}")

    total_players = len(merged)
    filtered_low = 0
    if min_attendance_frac is not None:
        kept_rows: List[Dict[str, Any]] = []
        for r in merged:
            pfk = r.get("pulls_for_kill", 0)
            if pfk > 0 and r.get("pulls", 0) < min_attendance_frac * pfk:
                filtered_low += 1
                continue
            kept_rows.append(r)
        merged = kept_rows

    merged.sort(key=lambda r: (-r["value"], r["player"].lower()))
    stats = {
        "total_players": total_players,
        "kept": len(merged),
        "filtered_low_attendance": filtered_low,
    }
    return merged, skipped, stats
