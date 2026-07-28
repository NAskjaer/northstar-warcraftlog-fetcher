# src/overnight.py
"""
Overnight (auto-resume) engine for multi-guild ranking runs.

Runs the same aggregation as the Streamlit ranking tab, but designed to survive
the Warcraft Logs hourly *points* budget: guilds are processed in small chunks,
and before each chunk it checks the budget. If there isn't enough, it sleeps and
re-checks every ``poll_seconds`` (pinging only while the run is alive) until the
window resets, then continues — so a large range that can't fit in one hour just
finishes overnight instead of failing.

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

from .api_client import get_rate_limit
from .estimate import POINTS_PER_GUILD
from .guild_rankings_store import load_ranking
from .guild_rankings_fetcher import get_encounter_zone_id
from .multi_guild import aggregate_guilds, reset_report_caches
from . import boss_config

DIFFICULTY = 5  # Mythic, matching the rest of the app.
_CHUNK_SIZE = 20
_BUDGET_MARGIN = 1.15  # ask for a bit more than the raw estimate before a chunk.


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


def _chunks(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


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


def _wait_for_budget(
    chunk_len: int,
    poll_seconds: int,
    job_dir: Path,
    status: _Status,
) -> bool:
    """
    Block until the budget can cover this chunk (or stop is requested).

    Returns True when it's OK to proceed, False if a stop was requested. The
    rate-limit query itself is the 'ping'; when the hourly budget is exhausted
    that query may 429, which we treat as 'still empty' and keep polling.
    """
    need = chunk_len * POINTS_PER_GUILD * _BUDGET_MARGIN
    while True:
        if _stop_requested(job_dir):
            return False
        rl = None
        try:
            rl = get_rate_limit()
        except Exception as exc:  # includes 429 when fully exhausted
            status.update(
                state="waiting",
                budget=None,
                message=f"Budget check failed (likely throttled): {exc}. "
                        f"Retrying in {poll_seconds // 60}m.",
            )
        if rl is not None:
            remaining = rl["limit_per_hour"] - rl["points_spent"]
            if remaining >= need:
                status.update(state="running", budget=rl)
                return True
            mins = max(1, rl["points_reset_in"] // 60)
            status.update(
                state="waiting",
                budget=rl,
                message=(
                    f"Budget low: {int(remaining):,} left, need ~{int(need):,} "
                    f"for the next {chunk_len} guilds. Waiting ~{mins}m for reset; "
                    f"pinging every {poll_seconds // 60}m."
                ),
            )
        # Sleep in short slices so a STOP is picked up promptly.
        slept = 0
        while slept < poll_seconds:
            if _stop_requested(job_dir):
                return False
            step = min(5, poll_seconds - slept)
            time.sleep(step)
            slept += step


def run_job(
    job: Dict[str, Any],
    job_dir: str | Path,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Execute an overnight job. Returns the final status dict."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    raid_file = job.get("raid_file", "Midnight_season_1.json")
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

    guilds = [g for g in load_ranking(raid_file)
              if rank_start <= g["rank"] <= rank_end]
    ability_names = boss_config.get_ability_names(raid_file)

    total_units = len(guilds) * max(1, len(targets)) * max(1, len(metrics))
    status = _Status(job_dir, total_units, log)

    if not guilds:
        status.update(state="error",
                      error=f"No guilds in rank range {rank_start}-{rank_end} "
                            f"for {raid_file}.")
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
                    seg_key, {"processed_ranks": [], "rows": []})
                processed = set(seg["processed_ranks"])
                todo = [g for g in guilds if g["rank"] not in processed]
                metric_label = "deaths" if metric_is_deaths else "damage taken"

                for chunk in _chunks(todo, chunk_size):
                    status.update(
                        current=f"{boss_name} — {metric_label}",
                        progress={"done": done_units, "total": total_units},
                    )
                    if not _wait_for_budget(len(chunk), poll_seconds, job_dir, status):
                        status.update(state="stopped",
                                      message="Stop requested — exiting.")
                        return status.data

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
                    seg["processed_ranks"].extend(g["rank"] for g in chunk)
                    done_units += len(chunk)
                    _atomic_write_json(cp_path, checkpoint)
                    status.update(
                        state="running",
                        progress={"done": done_units, "total": total_units},
                        message=f"{boss_name} ({metric_label}): "
                                f"{len(seg['processed_ranks'])}/{len(guilds)} guilds.",
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
    except Exception as exc:  # noqa: BLE001 - surface any engine failure to the UI
        status.update(state="error", error=str(exc))
    return status.data
