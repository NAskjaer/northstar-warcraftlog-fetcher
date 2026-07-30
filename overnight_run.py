#!/usr/bin/env python
"""
Headless overnight runner for multi-guild (ranking-mode) CSVs.

Runs the same aggregation as the Streamlit "Top guilds" tab, but survives the
Warcraft Logs hourly points budget: when it runs low it sleeps straight through
to the next hourly reset (computed from WCL's own countdown) and resumes, so a
big rank range finishes overnight instead of failing. Progress is checkpointed,
so a crash/reboot continues where it left off if you re-run the same job
directory.

Usage (job file, as written by the app's "Launch overnight run" button):
    python overnight_run.py --job output/overnight/<id>/job.json

Usage (ad-hoc flags):
    python overnight_run.py --ranks 1-300 --metric both
    python overnight_run.py --ranks 1-100 --metric deaths --boss "Midnight Falls"

By default every boss in the raid config is analysed for "All abilities". Use
--boss (repeatable) to limit which bosses, and their configured ability IDs are
used when present (otherwise All abilities).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import boss_config
from src.log_utils import ts_print
from src.overnight import run_job

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RAID = "Midnight_season_1.json"


class _Tee:
    """Duplicate writes to several streams (best-effort — a write that fails
    on one stream, e.g. a closed console, shouldn't take the others down)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def _tee_output_to_log(job_dir: Path) -> None:
    """
    Mirror everything this process prints to both its own stdout/stderr (a
    visible console window when launched that way — see ui/app.py's
    _launch_overnight, which now allocates one instead of hiding it) and
    job_dir/run.log (so the UI's "Raw log" viewer and post-hoc debugging
    still work regardless of whether anyone was watching the window live).
    Opened here (not by the launcher) so this also works correctly when the
    script is run directly from an existing terminal.
    """
    log_f = open(job_dir / "run.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, log_f)
    sys.stderr = _Tee(sys.stderr, log_f)


def _set_console_title(job: dict, job_dir: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        if job.get("guild_input_mode") == "links":
            scope = f"{len(job.get('manual_guilds') or [])} guilds"
        else:
            scope = f"ranks {job.get('rank_start')}-{job.get('rank_end')}"
        ctypes.windll.kernel32.SetConsoleTitleW(  # type: ignore[attr-defined]
            f"Overnight run — {scope} — {job_dir.name}"
        )
    except Exception:
        pass


def _parse_ranks(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition("-")
    if not hi:
        lo = hi = text
    a, b = int(lo), int(hi)
    return (a, b) if a <= b else (b, a)


def _build_targets(raid_file: str, only_bosses: list[str]) -> list[dict]:
    """One target per boss × configured ability (or All abilities if none)."""
    options = boss_config.get_boss_options(raid_file)
    wanted = set(only_bosses) if only_bosses else None
    targets: list[dict] = []
    for boss_name, info in options.items():
        if wanted is not None and boss_name not in wanted:
            continue
        abilities = info.get("abilities") or [None]
        for ability_id in abilities:
            targets.append({
                "boss_name": boss_name,
                "boss_id": info["id"],
                "ability_id": ability_id,
            })
    return targets


def _job_from_args(args: argparse.Namespace) -> tuple[dict, Path]:
    rank_start, rank_end = _parse_ranks(args.ranks)
    targets = _build_targets(args.raid, args.boss or [])
    job = {
        "raid_file": args.raid,
        "rank_start": rank_start,
        "rank_end": rank_end,
        "metric": args.metric,
        "start_date": args.start,
        "end_date": args.end,
        "min_attendance_frac": args.min_attendance,
        "ignore_after_player_deaths": None,
        "targets": targets,
        "poll_seconds": args.poll,
    }
    if args.chunk_size is not None:
        job["chunk_size"] = args.chunk_size
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    job_dir = PROJECT_ROOT / "output" / "overnight" / f"{stamp}_r{rank_start}-{rank_end}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return job, job_dir


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", help="Path to a job.json (overrides all other flags).")
    p.add_argument("--ranks", default="1-100", help="Rank range, e.g. 1-300.")
    p.add_argument("--metric", default="both",
                   choices=["deaths", "hits", "damage", "both"])
    p.add_argument("--raid", default=DEFAULT_RAID, help="Raid config filename.")
    p.add_argument("--boss", action="append",
                   help="Limit to this boss (repeatable). Default: all bosses.")
    p.add_argument("--start", default="2026-03-31", help="Start date YYYY-MM-DD.")
    p.add_argument("--end", default=None, help="End date YYYY-MM-DD (default today).")
    p.add_argument("--min-attendance", dest="min_attendance", type=float,
                   default=0.8, help="Drop players below this fraction of pulls.")
    p.add_argument("--poll", type=int, default=300,
                   help="Seconds between retries if a budget check itself fails "
                        "outright (e.g. a hard 429). Once budget is confirmed "
                        "low, the run instead sleeps straight through to WCL's "
                        "own hourly reset countdown, so this rarely matters.")
    p.add_argument("--chunk-size", dest="chunk_size", type=int, default=None,
                   help="Cap on guilds analysed per budget-check batch "
                        "(each batch already takes as many guilds as the "
                        "current budget affords, up to this cap). Default: "
                        "derived from estimate.POINTS_PER_GUILD so a batch "
                        "can never need more than one hourly window.")
    args = p.parse_args()

    if args.job:
        job_path = Path(args.job)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        job_dir = job_path.parent
    else:
        job, job_dir = _job_from_args(args)

    _tee_output_to_log(job_dir)
    _set_console_title(job, job_dir)

    ts_print(f"Overnight run starting. Job dir: {job_dir}")
    ts_print(f"  ranks {job['rank_start']}-{job['rank_end']}, metric={job['metric']}, "
             f"{len(job['targets'])} boss/ability target(s)")
    ts_print("  Watch progress in status.json; create a STOP file in the job dir to stop.")

    final = run_job(job, job_dir)
    state = final.get("state")
    if state == "done":
        ts_print(f"Done. CSVs written to: {job_dir}")
        for o in final.get("outputs", []):
            ts_print(f"  - {o}")
    elif state == "stopped":
        ts_print("Stopped on request. Re-run the same job dir to resume.")
    else:
        ts_print(f"Finished with state={state}. error={final.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
