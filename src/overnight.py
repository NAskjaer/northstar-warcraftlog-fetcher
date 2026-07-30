# src/overnight.py
"""
Overnight (auto-resume) engine for multi-guild ranking runs.

Runs the same aggregation as the Streamlit ranking tab, but designed to survive
the Warcraft Logs hourly *points* budget: before each batch it checks the
current remaining budget and takes as many guilds as that budget actually
affords (not a fixed chunk size) — draining every hourly window down to under
one guild's worth of points before it ever waits, instead of stopping early
with points sitting unused. If even a single guild can't be afforded, it
sleeps straight through to the hourly reset (WCL reports exactly how many
seconds are left via ``points_reset_in``, so this computes the reset's
wall-clock time instead of blind-polling every few minutes) plus a small
safety buffer, then re-checks once and continues — so a large range that
can't fit in one hour just finishes overnight instead of failing. When the
budget is exhausted enough that even the check itself gets a 429, it
estimates the reset from the last successful reading instead of blind-polling
every ``poll_seconds`` — that's only the fallback when there's no prior
reading to estimate from. Either way, only one line prints when a wait starts
and one when it ends, not one per minute of countdown.

State lives in a job directory:
  job.json         - the run parameters (written by the launcher)
  status.json      - live progress/budget/state, polled by the UI
  checkpoint.json  - processed guild ranks + rows per segment (crash-resume)
  STOP             - create this file to ask the run to stop cleanly
  <boss>_<metric>.csv - final output per (boss, metric) segment

This module has no Streamlit dependency so it can run headless from a CLI.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import report_cache
from .api_client import get_rate_limit, is_retryable_error
from .estimate import POINTS_PER_GUILD, ASSUMED_LIMIT_PER_HOUR
from .guild_rankings_store import load_ranking
from .guild_rankings_fetcher import get_encounter_zone_id
from .log_utils import log_line, ts_print
from .multi_guild import aggregate_guilds, class_spec_label, reset_report_caches
from . import boss_config

DIFFICULTY = 5  # Mythic, matching the rest of the app.
_BUDGET_MARGIN = 1.15  # ask for a bit more than the raw estimate before a chunk.

# Sanity upper bound on how many guilds go into one batch, regardless of how
# much budget is available. Chunk size itself is *not* fixed — see
# _next_chunk_size, which fills each batch to whatever the current remaining
# budget actually affords (draining an hour down to <1 guild's worth of
# points before ever waiting, instead of stopping early with points unused).
# This cap just bounds worst-case Stop-responsiveness / checkpoint
# granularity if POINTS_PER_GUILD is ever recalibrated much lower than today.
_MAX_CHUNK = max(1, int(ASSUMED_LIMIT_PER_HOUR / (POINTS_PER_GUILD * _BUDGET_MARGIN)))
_MAX_RETRIES_PER_GUILD = 3  # transient (rate-limit/network) failures before giving up


# --------------------------------------------------------------------------
# Small JSON/state helpers
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_job(path: str | Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_status(job_dir: str | Path) -> Optional[Dict[str, Any]]:
    p = Path(job_dir) / "status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def request_stop(job_dir: str | Path) -> None:
    (Path(job_dir) / "STOP").write_text(_now_iso(), encoding="utf-8")


def _stop_requested(job_dir: Path) -> bool:
    return (job_dir / "STOP").exists()


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
def _metrics_from_job(metric: str) -> List[bool]:
    metric = (metric or "both").lower()
    if metric == "deaths":
        return [True]
    if metric in ("damage", "damage_taken", "damage taken", "hits"):
        return [False]
    return [True, False]  # both


def _safe_name(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in text)
    return "_".join(keep.split()) or "boss"


def _csv_bytes(boss_name: str, ability_label: str, rows: List[Dict[str, Any]],
               metric_is_deaths: bool) -> bytes:
    if metric_is_deaths:
        columns = ["Class", "Player", "Guild", "Total Deaths", "Pulls", "Guild Pulls"]
        table = [
            [class_spec_label(r.get("class_spec")), r["player"], r["guild"],
             r["value"], r["pulls"], r.get("pulls_for_kill", 0)]
            for r in rows
        ]
    else:
        columns = ["Class", "Player", "Guild", "Total Damage", "Hits", "Pulls",
                   "Guild Pulls"]
        table = [
            [class_spec_label(r.get("class_spec")), r["player"], r["guild"],
             r["value"], r.get("hits", 0), r["pulls"], r.get("pulls_for_kill", 0)]
            for r in rows
        ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([boss_name, ability_label])
    w.writerow(columns)
    for row in table:
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


class _Status:
    """Thin wrapper that merges fields into status.json and keeps timestamps."""

    def __init__(self, job_dir: Path, total_units: int, log: Callable[[str], None]):
        self.job_dir = job_dir
        self.log = log
        self.data: Dict[str, Any] = {
            "state": "starting",
            "message": "",
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "progress": {"done": 0, "total": total_units},
            "current": "",
            "budget": None,
            "budget_read_at": None,  # wall-clock time of the last *successful*
                                      # budget reading — lets a later failed
                                      # check still estimate the real reset
                                      # time instead of blind-polling.
            "points_spent_total": 0.0,  # accumulated across hourly resets —
                                         # rateLimitData.pointsSpentThisHour
                                         # resets every hour, so a naive
                                         # last-reading-minus-first-reading
                                         # (the old approach) undercounts to
                                         # ~0 on any run spanning more than
                                         # one reset; this tracks each
                                         # observed delta instead, treating a
                                         # drop in the raw counter as "a
                                         # reset happened" rather than
                                         # "points were un-spent".
            "outputs": [],
            "error": None,
        }
        self.flush()

    def update(self, log: bool = True, **fields: Any) -> None:
        """``log=False`` still updates status.json (so the UI's live caption
        stays current) but skips the terminal/run.log print — for the
        periodic countdown ticks during a wait, where reprinting the same
        message every minute is just noise (see _sleep_with_status)."""
        self.data.update(fields)
        self.data["updated_at"] = _now_iso()
        self.flush()
        if log and "message" in fields and fields["message"]:
            self.log(fields["message"])

    def add_output(self, path: str) -> None:
        outs = self.data.setdefault("outputs", [])
        if path not in outs:
            outs.append(path)
        self.flush()

    def flush(self) -> None:
        try:
            _atomic_write_json(self.job_dir / "status.json", self.data)
        except OSError:
            pass


_RESET_BUFFER_SECONDS = 60  # safety margin past WCL's own reset countdown
_STATUS_TICK_SECONDS = 60   # how often to refresh the on-disk status while sleeping


def _sleep_with_status(
    seconds: int,
    job_dir: Path,
    status: "_Status",
    message_for: Callable[[int], str],
) -> bool:
    """
    Sleep up to ``seconds``, checking for STOP every 5s and refreshing the
    status message (no API calls — just wall-clock math) roughly every
    ``_STATUS_TICK_SECONDS`` so the UI's countdown stays live during a long
    wait. Returns False if a stop was requested mid-sleep.

    The tick updates status.json (for the UI's live "~Nm remaining" caption)
    but doesn't print — the caller already printed one message when the wait
    started; reprinting the same thing every minute for up to an hour is
    exactly the terminal/run.log spam this is meant to avoid.
    """
    slept = 0
    last_tick = 0
    while slept < seconds:
        if _stop_requested(job_dir):
            return False
        step = min(5, seconds - slept)
        time.sleep(step)
        slept += step
        if slept - last_tick >= _STATUS_TICK_SECONDS or slept >= seconds:
            last_tick = slept
            status.update(
                state="waiting", message=message_for(max(0, seconds - slept)),
                log=False,
            )
    return True


def _log_run_summary(outcome: str, status: "_Status") -> None:
    """
    Final wrap-up line for the whole job (every target/metric segment), in
    the same Guild/Result log_line style used per-guild — how many distinct
    WCL reports it pulled data from and roughly what it cost, using the
    rate-limit readings _next_chunk_size already made rather than an extra
    API call just to report this. Points spent is status.data's running
    accumulator (see _Status), not a first-vs-last subtraction — a run
    spanning more than one hourly reset would otherwise undercount toward
    zero, since the raw counter itself resets every hour.
    """
    stats = report_cache.get_stats()
    fields: Dict[str, Any] = {"Reports fetched": stats["reports_fetched"]}
    pts = status.data.get("points_spent_total")
    if pts:
        fields["Points spent"] = f"{round(pts, 2):,}"
    log_line(Result=outcome, **fields)


def _next_chunk_size(
    todo_len: int,
    max_chunk: int,
    poll_seconds: int,
    job_dir: Path,
    status: _Status,
) -> Optional[int]:
    """
    Return how many guilds to process right now, or None if stop was requested.

    Rather than blocking until the budget covers some fixed chunk size, this
    fills the batch to whatever the *currently remaining* budget actually
    affords (capped at ``max_chunk`` and at how many guilds are left) — a
    fresh hour might afford ~19 guilds, a mostly-spent one might afford just
    1. That drains every hourly window down to under one guild's worth of
    points before it ever waits, instead of leaving a fixed chunk's worth of
    headroom unused (e.g. a flat chunk_size=7 stops as soon as remaining
    budget drops below 7 guilds' cost, even with 6 guilds' worth still
    sitting unspent — pure waste, since overnight mode has no reason to
    economize on wall-clock time, only on points across resets).

    Only waits when even a single guild can't be afforded. WCL reports
    exactly how many seconds remain until the hourly window resets
    (``points_reset_in``), so it sleeps straight through to that reset (plus
    a small safety buffer) in one go rather than polling every few minutes.

    When the budget itself is fully exhausted, even the *check* (a 1-point
    query) gets a 429 — so this can't always get a fresh ``points_reset_in``
    to sleep to. Instead it estimates the reset time from the last
    *successful* reading (``status.data["budget"]``/``"budget_read_at"``)
    adjusted for elapsed time, and sleeps to that — falling back to
    ``poll_seconds`` only if there's no prior reading to estimate from (e.g.
    the very first check of the run is already exhausted). Either way, only
    ONE message prints when a wait starts and ONE when it ends; the
    in-between countdown only updates status.json for the UI, not the
    terminal (see _sleep_with_status) — a hard-exhausted budget can mean a
    dozen+ check attempts before the hourly reset, and printing the same
    paragraph every time drowns out everything else in run.log.
    """
    per_guild = POINTS_PER_GUILD * _BUDGET_MARGIN
    was_waiting = False

    while True:
        if _stop_requested(job_dir):
            return None

        try:
            # A single fast attempt with no retry-chatter: this is just
            # "has the budget come back yet", polled repeatedly either way —
            # run_wcl_query's own 3-attempt backoff dance would just add
            # ~7s and 3 more printed lines to every one of those polls.
            rl = get_rate_limit(max_retries=1, quiet=True)
        except Exception as exc:  # includes 429 when fully exhausted
            was_waiting = True
            last_budget = status.data.get("budget")
            last_read_at = status.data.get("budget_read_at")
            retryable = is_retryable_error(str(exc))
            if retryable and last_budget is not None and last_read_at is not None:
                elapsed = time.time() - last_read_at
                wait_seconds = max(
                    60, int(last_budget["points_reset_in"] - elapsed) + _RESET_BUFFER_SECONDS
                )
            else:
                wait_seconds = poll_seconds
            next_attempt = (datetime.now() + timedelta(seconds=wait_seconds)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            if retryable:
                msg = (f"[PAUSED] Warcraft Logs rate limit hit (HTTP 429), trying "
                       f"again at {next_attempt} (~{max(1, wait_seconds // 60)}m).")
            else:
                msg = (f"[PAUSED] Budget check failed ({exc}) — trying again at "
                       f"{next_attempt} (~{max(1, wait_seconds // 60)}m).")
            # Deliberately NOT overwriting status.data["budget"] here — it's
            # the last known-good reading, needed both to estimate the next
            # retry's wait (above) and to keep the points-spent accumulator
            # correct once a check finally succeeds again. The UI showing a
            # slightly stale snapshot while we can't confirm a fresh one is
            # more useful than blanking it to nothing.
            status.update(state="waiting", message=msg)
            if not _sleep_with_status(
                wait_seconds, job_dir, status,
                lambda r: f"[WAITING] Retrying budget check — ~{max(1, r // 60)}m remaining.",
            ):
                return None
            continue

        prev_budget = status.data.get("budget")
        points_spent_total = status.data.get("points_spent_total", 0.0)
        if prev_budget is not None:
            delta = rl["points_spent"] - prev_budget.get("points_spent", 0.0)
            if delta < 0:
                # The raw counter dropped — an hourly reset happened between
                # readings, not "points un-spent". Everything since the
                # reset is new spend.
                delta = rl["points_spent"]
            points_spent_total += delta
        status.update(
            budget_read_at=time.time(),
            points_spent_total=points_spent_total,
            log=False,
        )
        remaining = rl["limit_per_hour"] - rl["points_spent"]
        affordable = int(remaining // per_guild)
        if affordable >= 1:
            n = min(affordable, todo_len, max_chunk)
            if was_waiting:
                status.update(
                    state="running", budget=rl,
                    message=f"[RESUMED] Hourly reset landed — {int(remaining):,} of "
                            f"{rl['limit_per_hour']:,} pts available, taking {n} guild(s).",
                )
            else:
                status.update(state="running", budget=rl)
            return n

        was_waiting = True
        wait_seconds = int(rl["points_reset_in"]) + _RESET_BUFFER_SECONDS
        next_attempt = (datetime.now() + timedelta(seconds=wait_seconds)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        status.update(
            state="waiting",
            budget=rl,
            message=(
                f"[PAUSED] Budget too low for even one more guild "
                f"({int(remaining):,} left, ~{int(per_guild):,} needed each). "
                f"Resuming at {next_attempt} (~{max(1, wait_seconds // 60)}m)."
            ),
        )
        if not _sleep_with_status(
            wait_seconds, job_dir, status,
            lambda r: f"[WAITING] For hourly budget reset — ~{max(1, r // 60)}m remaining.",
        ):
            return None
        # Loop back to verify the reset actually landed before proceeding;
        # if not, this recomputes from the real remaining time.


def run_job(
    job: Dict[str, Any],
    job_dir: str | Path,
    log: Callable[[str], None] = ts_print,
) -> Dict[str, Any]:
    """Execute an overnight job. Returns the final status dict."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    raid_file = job.get("raid_file", "Midnight_season_1.json")
    guild_input_mode = job.get("guild_input_mode", "rank")
    rank_start = int(job["rank_start"])
    rank_end = int(job["rank_end"])
    poll_seconds = int(job.get("poll_seconds", 300))
    max_chunk = int(job.get("chunk_size", _MAX_CHUNK))
    min_attendance = job.get("min_attendance_frac", 0.8)
    ignore_after = job.get("ignore_after_player_deaths")
    stop_at_first_kill = bool(job.get("stop_at_first_kill", False))
    targets = job.get("targets", [])
    metrics = _metrics_from_job(job.get("metric", "both"))

    sd = job.get("start_date", "2026-03-31")
    ed = job.get("end_date")
    start_dt = datetime.fromisoformat(sd).replace(tzinfo=timezone.utc)
    if ed:
        end_dt = datetime.fromisoformat(ed).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)

    if guild_input_mode == "links":
        guilds = job.get("manual_guilds") or []
        guild_scope_desc = f"{len(guilds)} pasted guild(s)"
    else:
        guilds = [g for g in load_ranking(raid_file)
                  if rank_start <= g["rank"] <= rank_end]
        guild_scope_desc = f"rank range {rank_start}-{rank_end}"
    ability_names = boss_config.get_ability_names(raid_file)

    total_units = len(guilds) * max(1, len(targets)) * max(1, len(metrics))
    status = _Status(job_dir, total_units, log)

    if not guilds:
        status.update(state="error",
                      error=f"No guilds found for {guild_scope_desc} in {raid_file}.")
        return status.data
    if not targets:
        status.update(state="error", error="No bosses/abilities in job.")
        return status.data

    # Crash-resume checkpoint: per segment -> processed ranks + accumulated rows.
    cp_path = job_dir / "checkpoint.json"
    checkpoint: Dict[str, Any] = {}
    if cp_path.exists():
        try:
            checkpoint = json.loads(cp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            checkpoint = {}

    done_units = sum(len(seg.get("processed_ranks", []))
                     for seg in checkpoint.values())
    reset_report_caches()

    try:
        for ti, tgt in enumerate(targets):
            boss_id = int(tgt["boss_id"])
            ability_id = tgt.get("ability_id")
            boss_name = tgt.get("boss_name", str(boss_id))
            ability_label = ("All abilities" if ability_id is None
                             else f"{ability_id} ({ability_names.get(ability_id, 'Unknown')})")
            try:
                zone_id = get_encounter_zone_id(boss_id)
            except Exception:
                zone_id = None

            for metric_is_deaths in metrics:
                seg_key = f"{ti}:{'d' if metric_is_deaths else 'g'}"
                seg = checkpoint.setdefault(
                    seg_key, {"processed_ranks": [], "rows": [], "gave_up_ranks": []})
                seg.setdefault("gave_up_ranks", [])
                processed = set(seg["processed_ranks"])
                todo = [g for g in guilds if g["rank"] not in processed]
                metric_label = "deaths" if metric_is_deaths else "hits"
                metric_display = "Deaths" if metric_is_deaths else "Hits"
                retry_counts: Dict[int, int] = {}

                # done_units only advances once a whole chunk is reconciled
                # (a guild might still get requeued after a retryable
                # failure, so it can't count as "done" mid-chunk) — but with
                # chunks now sized up to ~19 guilds, that left the UI's
                # progress bar frozen for however long a full chunk took,
                # even though guilds were visibly finishing one by one. This
                # mutable counter tracks live per-guild completions for
                # *display only*; it's resynced to the authoritative
                # done_units as soon as the chunk actually resolves below.
                live_done = [done_units]

                def _note_guild_progress(_done, _total, guild_label,
                                          _boss_name=boss_name,
                                          _metric_display=metric_display):
                    live_done[0] += 1
                    status.update(
                        current=f"{guild_label} — {_boss_name} — {_metric_display}",
                        progress={"done": min(live_done[0], total_units),
                                  "total": total_units},
                    )

                while todo:
                    status.update(
                        current=f"{boss_name} — {metric_label}",
                        progress={"done": done_units, "total": total_units},
                    )
                    n = _next_chunk_size(
                        len(todo), max_chunk, poll_seconds, job_dir, status)
                    if n is None:
                        status.update(state="stopped",
                                      message="Stop requested — exiting.")
                        _log_run_summary("stopped by user", status)
                        return status.data
                    chunk, todo = todo[:n], todo[n:]

                    rows, skipped, _stats = aggregate_guilds(
                        chunk,
                        boss_id=boss_id,
                        ability_id=ability_id,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        zone_id=zone_id,
                        difficulty=DIFFICULTY,
                        metric_is_deaths=metric_is_deaths,
                        ignore_after_player_deaths=ignore_after,
                        min_attendance_frac=min_attendance,
                        stop_at_first_kill=stop_at_first_kill,
                        progress_callback=_note_guild_progress,
                    )
                    seg["rows"].extend(rows)

                    # A guild whose skip reason looks like a transient failure
                    # (rate limit exhausted mid-chunk despite the budget check,
                    # or a network hiccup) hasn't actually been fully checked —
                    # don't checkpoint it as processed. Requeue it for another
                    # attempt in this same run (the next chunk's budget check
                    # naturally provides backoff) rather than silently losing
                    # it or leaving it for someone to notice and re-run by hand.
                    retryable_ranks = {
                        s["guild"]["rank"] for s in skipped
                        if is_retryable_error(s.get("reason") or "")
                    }

                    requeue = []
                    resolved_this_chunk = 0
                    for g in chunk:
                        if g["rank"] not in retryable_ranks:
                            seg["processed_ranks"].append(g["rank"])
                            resolved_this_chunk += 1
                            continue
                        retry_counts[g["rank"]] = retry_counts.get(g["rank"], 0) + 1
                        if retry_counts[g["rank"]] <= _MAX_RETRIES_PER_GUILD:
                            requeue.append(g)
                        else:
                            # Repeated transient failures — stop retrying so the
                            # run can finish, but flag it instead of pretending
                            # this guild was cleanly checked.
                            seg["processed_ranks"].append(g["rank"])
                            seg["gave_up_ranks"].append(g["rank"])
                            resolved_this_chunk += 1
                    if requeue:
                        todo.extend(requeue)

                    done_units += resolved_this_chunk
                    live_done[0] = done_units
                    _atomic_write_json(cp_path, checkpoint)
                    retry_note = (
                        f" ({len(requeue)} requeued after a transient failure)"
                        if requeue else ""
                    )
                    status.update(
                        state="running",
                        progress={"done": done_units, "total": total_units},
                        message=f"{boss_name} ({metric_label}): "
                                f"{len(seg['processed_ranks'])}/{len(guilds)} guilds."
                                f"{retry_note}",
                    )

                # Finalize this segment: global sort + write CSV.
                seg_rows = sorted(
                    seg["rows"],
                    key=lambda r: (-r["value"], r["player"].lower()),
                )
                fname = f"{_safe_name(boss_name)}_{'deaths' if metric_is_deaths else 'damage'}.csv"
                out_path = job_dir / fname
                out_path.write_bytes(
                    _csv_bytes(boss_name, ability_label, seg_rows, metric_is_deaths))
                status.add_output(str(out_path))

        status.update(state="done", current="",
                      message="All segments complete.",
                      progress={"done": total_units, "total": total_units})
        _log_run_summary("all segments complete", status)
    except Exception as exc:  # noqa: BLE001 - surface any engine failure to the UI
        status.update(state="error", error=str(exc))
        _log_run_summary(f"error — {exc}", status)
    return status.data
