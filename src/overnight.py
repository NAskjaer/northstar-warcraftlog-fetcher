# src/overnight.py
"""
Overnight (auto-resume) engine for multi-guild ranking runs.

Runs the same aggregation as the Streamlit ranking tab, but designed to survive
the Warcraft Logs hourly *points* budget: guilds are processed in small chunks,
and before each chunk it checks the budget. If there isn't enough, it sleeps
straight through to the hourly reset (WCL reports exactly how many seconds are
left via ``points_reset_in``, so this computes the reset's wall-clock time
instead of blind-polling every few minutes) plus a small safety buffer, then
re-checks once and continues — so a large range that can't fit in one hour just
finishes overnight instead of failing. ``poll_seconds`` is only the retry
cadence for the rare case where the budget check itself fails outright (e.g. a
hard 429); it's no longer used while a reset is simply pending.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import report_cache
from .api_client import get_rate_limit, is_retryable_error
from .estimate import POINTS_PER_GUILD
from .guild_rankings_store import load_ranking
from .guild_rankings_fetcher import get_encounter_zone_id
from .log_utils import log_line
from .multi_guild import aggregate_guilds, reset_report_caches
from . import boss_config

DIFFICULTY = 5  # Mythic, matching the rest of the app.
_CHUNK_SIZE = 20
_BUDGET_MARGIN = 1.15  # ask for a bit more than the raw estimate before a chunk.
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
    if metric in ("damage", "damage_taken", "damage taken"):
        return [False]
    return [True, False]  # both


def _safe_name(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in text)
    return "_".join(keep.split()) or "boss"


def _csv_bytes(boss_name: str, ability_label: str, rows: List[Dict[str, Any]],
               metric_is_deaths: bool) -> bytes:
    if metric_is_deaths:
        columns = ["Player", "Guild", "Total Deaths", "Pulls", "Guild Pulls"]
        table = [
            [r["player"], r["guild"], r["value"], r["pulls"],
             r.get("pulls_for_kill", 0)]
            for r in rows
        ]
    else:
        columns = ["Player", "Guild", "Total Damage Taken", "Hits", "Pulls",
                   "Guild Pulls"]
        table = [
            [r["player"], r["guild"], r["value"], r.get("hits", 0), r["pulls"],
             r.get("pulls_for_kill", 0)]
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
            "outputs": [],
            "error": None,
        }
        self.flush()

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["updated_at"] = _now_iso()
        self.flush()
        if "message" in fields and fields["message"]:
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
            status.update(state="waiting", message=message_for(max(0, seconds - slept)))
    return True


def _log_run_summary(
    outcome: str, first_budget: Optional[Dict[str, Any]], status: "_Status"
) -> None:
    """
    Final wrap-up line for the whole job (every target/metric segment), in
    the same Guild/Result log_line style used per-guild — how many distinct
    WCL reports it pulled data from and roughly what it cost, using the
    rate-limit readings _wait_for_budget already made rather than an extra
    API call just to report this.
    """
    stats = report_cache.get_stats()
    fields: Dict[str, Any] = {"Reports fetched": stats["reports_fetched"]}
    last_budget = status.data.get("budget")
    if first_budget is not None and last_budget is not None:
        pts = round(last_budget["points_spent"] - first_budget["points_spent"], 2)
        fields["Points spent"] = f"{pts:,}"
    log_line(Result=outcome, **fields)


def _wait_for_budget(
    chunk_len: int,
    poll_seconds: int,
    job_dir: Path,
    status: _Status,
) -> bool:
    """
    Block until the budget can cover this chunk (or stop is requested).

    Returns True when it's OK to proceed, False if a stop was requested.

    WCL reports exactly how many seconds remain until its hourly points
    window resets (``points_reset_in``), so once the budget is confirmed
    too low, this sleeps straight through to that reset time (plus a small
    safety buffer) in one go, rather than blind-polling every few minutes —
    each poll is itself a point-costing call that, while genuinely
    exhausted, may just 429 anyway. After waking it re-checks once; if the
    budget still isn't enough (clock drift, another session sharing the
    client, or this chunk simply needs more than one fresh window), it
    loops and computes a fresh wait from the up-to-date remaining time
    instead of trusting the first estimate. ``poll_seconds`` is only used
    as the retry cadence when the rate-limit check itself fails outright
    (e.g. a hard 429 on the check itself).
    """
    need = chunk_len * POINTS_PER_GUILD * _BUDGET_MARGIN
    was_waiting = False

    while True:
        if _stop_requested(job_dir):
            return False

        try:
            rl = get_rate_limit()
        except Exception as exc:  # includes 429 when fully exhausted
            was_waiting = True
            status.update(
                state="waiting",
                budget=None,
                message=f"[PAUSED] Budget check failed (likely throttled): {exc}. "
                        f"Retrying in {poll_seconds // 60}m.",
            )
            if not _sleep_with_status(
                poll_seconds, job_dir, status,
                lambda r: f"[WAITING] Budget check failed — retrying in ~{max(1, r // 60)}m.",
            ):
                return False
            continue

        remaining = rl["limit_per_hour"] - rl["points_spent"]
        if remaining >= need:
            if was_waiting:
                status.update(
                    state="running", budget=rl,
                    message=f"[RESUMED] {int(remaining):,} of "
                            f"{rl['limit_per_hour']:,} pts available, continuing.",
                )
            else:
                status.update(state="running", budget=rl)
            return True

        was_waiting = True
        wait_seconds = int(rl["points_reset_in"]) + _RESET_BUFFER_SECONDS
        mins = max(1, wait_seconds // 60)
        status.update(
            state="waiting",
            budget=rl,
            message=(
                f"[PAUSED] Budget low: {int(remaining):,} left, need ~{int(need):,} "
                f"for the next {chunk_len} guilds. Sleeping ~{mins}m until the "
                f"hourly reset, then resuming."
            ),
        )
        if not _sleep_with_status(
            wait_seconds, job_dir, status,
            lambda r: f"[WAITING] For hourly budget reset — ~{max(1, r // 60)}m remaining.",
        ):
            return False
        # Loop back to verify the reset actually landed before proceeding;
        # if not, this recomputes a fresh wait from the real remaining time.


def run_job(
    job: Dict[str, Any],
    job_dir: str | Path,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Execute an overnight job. Returns the final status dict."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    raid_file = job.get("raid_file", "Midnight_season_1.json")
    guild_input_mode = job.get("guild_input_mode", "rank")
    rank_start = int(job["rank_start"])
    rank_end = int(job["rank_end"])
    poll_seconds = int(job.get("poll_seconds", 300))
    chunk_size = int(job.get("chunk_size", _CHUNK_SIZE))
    min_attendance = job.get("min_attendance_frac", 0.8)
    ignore_after = job.get("ignore_after_player_deaths")
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
    first_budget: Optional[Dict[str, Any]] = None

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
                metric_label = "deaths" if metric_is_deaths else "damage taken"
                retry_counts: Dict[int, int] = {}

                while todo:
                    chunk, todo = todo[:chunk_size], todo[chunk_size:]
                    status.update(
                        current=f"{boss_name} — {metric_label}",
                        progress={"done": done_units, "total": total_units},
                    )
                    if not _wait_for_budget(len(chunk), poll_seconds, job_dir, status):
                        status.update(state="stopped",
                                      message="Stop requested — exiting.")
                        _log_run_summary("stopped by user", first_budget, status)
                        return status.data
                    if first_budget is None:
                        first_budget = status.data.get("budget")

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
        _log_run_summary("all segments complete", first_budget, status)
    except Exception as exc:  # noqa: BLE001 - surface any engine failure to the UI
        status.update(state="error", error=str(exc))
        _log_run_summary(f"error — {exc}", first_budget, status)
    return status.data
