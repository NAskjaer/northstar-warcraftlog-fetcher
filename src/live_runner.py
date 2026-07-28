# src/live_runner.py
"""
Background worker for the live (interactive) multi-guild ranking tab.

overnight.py's run_job is built to run unattended for hours — when the
budget runs low it sleeps straight through to the hourly reset. That's
wrong for a Streamlit tab sitting open in someone's browser: sleeping the
whole run for up to an hour would just hang the page. This runner instead
processes guilds in small chunks and, the moment the budget can't cover the
next chunk, stops immediately with a clear message pointing at the
overnight runner for the rest — it never blind-waits.

It also fixes the "no way to stop a live run" gap: the previous
aggregate_guilds() call for the ranking tab submitted every guild to a
thread pool up front with no way to interrupt it short of closing the tab
(and by the time the budget was long gone, it just kept trying every
remaining guild — each attempt still spending a little, all doomed). This
runs on a background thread with a stop_event checked between chunks, so a
Stop button in the UI actually does something within about one chunk's
worth of work.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import report_cache
from .api_client import get_rate_limit
from .estimate import POINTS_PER_GUILD
from .log_utils import log_line
from .multi_guild import aggregate_guilds, reset_report_caches

CHUNK_SIZE = 10
_BUDGET_MARGIN = 1.15


class LiveMultiGuildRun:
    """
    Started once via .start(), then polled: read `.snapshot()` on every
    Streamlit rerun to render progress, and call `.request_stop()` from a
    button's on_click. `.snapshot()` returns a shallow copy so a read from
    the polling thread is never torn by a concurrent write from the worker
    thread (each field is replaced wholesale, never mutated in place).
    """

    def __init__(
        self,
        *,
        guilds: List[Dict[str, Any]],
        passes: List[Tuple[int, bool, Dict[str, Any], Optional[int]]],
        # each pass: (target_index, metric_is_deaths, target_dict, zone_id)
        start_dt,
        end_dt,
        difficulty: int,
        ignore_after_player_deaths: Optional[int],
        min_attendance_frac: Optional[float],
        stop_at_first_kill: bool,
    ) -> None:
        self._guilds = guilds
        self._passes = passes
        self._start_dt = start_dt
        self._end_dt = end_dt
        self._difficulty = difficulty
        self._ignore_after = ignore_after_player_deaths
        self._min_attendance_frac = min_attendance_frac
        self._stop_at_first_kill = stop_at_first_kill

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status: Dict[str, Any] = {
            "state": "running",  # running | stopped | budget_exhausted | done | error
            "message": "Starting…",
            "pass_index": 0,
            "num_passes": len(passes),
            "guilds_done_this_pass": 0,
            "num_guilds": len(guilds),
            "budget": None,
            "entries": [],  # completed passes: {target_index, metric_is_deaths, rows, skipped}
            "error": None,
        }
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._first_budget: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stop_event.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._status = {**self._status, **fields}

    def _note_budget(self, rl: Dict[str, Any]) -> None:
        if self._first_budget is None:
            self._first_budget = rl

    def _log_run_summary(self, outcome: str) -> None:
        """
        Final wrap-up line for the whole run (all passes), in the same
        Guild/Result log_line style used per-guild — how many distinct WCL
        reports this run pulled data from and roughly what it cost, using
        the rate-limit readings the per-chunk budget check already made
        (no extra API calls just to report this).
        """
        stats = report_cache.get_stats()
        fields: Dict[str, Any] = {"Reports fetched": stats["reports_fetched"]}
        with self._lock:
            last_budget = self._status.get("budget")
        if self._first_budget is not None and last_budget is not None:
            pts = round(
                last_budget["points_spent"] - self._first_budget["points_spent"], 2
            )
            fields["Points spent"] = f"{pts:,}"
        log_line(Result=outcome, **fields)

    def _run(self) -> None:
        reset_report_caches()

        for pass_index, (target_index, metric_is_deaths, target, zone_id) in enumerate(self._passes):
            if self._stop_event.is_set():
                self._update(state="stopped", message="Stopped by user.")
                self._log_run_summary("stopped by user")
                return

            boss_name = target["boss_name"]
            metric_label = "deaths" if metric_is_deaths else "damage taken"
            todo = list(self._guilds)
            merged: List[Dict[str, Any]] = []
            skipped: List[Dict[str, Any]] = []
            filtered_low_attendance = 0

            self._update(
                pass_index=pass_index, guilds_done_this_pass=0,
                message=f"{boss_name} — {metric_label}: starting…",
            )

            while todo:
                if self._stop_event.is_set():
                    self._update(state="stopped", message="Stopped by user.")
                    self._log_run_summary("stopped by user")
                    return

                chunk, todo = todo[:CHUNK_SIZE], todo[CHUNK_SIZE:]

                try:
                    rl = get_rate_limit()
                except Exception as exc:
                    msg = f"Couldn't read the WCL rate limit: {exc}"
                    self._update(state="error", error=str(exc), message=msg)
                    self._log_run_summary(f"error — {msg}")
                    return
                self._note_budget(rl)

                need = len(chunk) * POINTS_PER_GUILD * _BUDGET_MARGIN
                remaining = rl["limit_per_hour"] - rl["points_spent"]
                if remaining < need:
                    guilds_left = len(todo) + len(chunk)
                    msg = (
                        f"Stopped — only {int(remaining):,} of "
                        f"{rl['limit_per_hour']:,} pts left, need ~{int(need):,} "
                        f"for the next {len(chunk)} guilds. {guilds_left} guild(s) "
                        f"left unprocessed for {boss_name} ({metric_label}) — use "
                        f"the overnight runner to finish those, or wait for the "
                        f"hourly reset and re-run."
                    )
                    self._update(state="budget_exhausted", budget=rl, message=msg)
                    self._log_run_summary(f"budget exhausted — {msg}")
                    return

                rows, chunk_skipped, chunk_stats = aggregate_guilds(
                    chunk,
                    boss_id=target["boss_id"],
                    ability_id=target["ability_id"],
                    start_dt=self._start_dt,
                    end_dt=self._end_dt,
                    zone_id=zone_id,
                    difficulty=self._difficulty,
                    metric_is_deaths=metric_is_deaths,
                    ignore_after_player_deaths=self._ignore_after,
                    min_attendance_frac=self._min_attendance_frac,
                    stop_at_first_kill=self._stop_at_first_kill,
                )
                merged.extend(rows)
                skipped.extend(chunk_skipped)
                filtered_low_attendance += chunk_stats.get("filtered_low_attendance", 0)

                done_this_pass = len(self._guilds) - len(todo)
                self._update(
                    state="running",
                    budget=rl,
                    guilds_done_this_pass=done_this_pass,
                    message=f"{boss_name} — {metric_label}: analyzed "
                            f"{done_this_pass}/{len(self._guilds)} guilds…",
                )

            # This pass finished cleanly — record it and move to the next.
            with self._lock:
                entries = list(self._status["entries"])
            entries.append({
                "target_index": target_index,
                "metric_is_deaths": metric_is_deaths,
                "rows": merged,
                "skipped": skipped,
                "stats": {"filtered_low_attendance": filtered_low_attendance},
            })
            self._update(entries=entries)

        self._update(state="done", message="All passes complete.")
        self._log_run_summary("all passes complete")
