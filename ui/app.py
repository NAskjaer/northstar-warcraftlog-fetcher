from __future__ import annotations
import os
import io
import csv
import sys
import json
import time
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

try:
    # Lets a background thread (a fetch's worker threads, ultimately) push
    # live updates into a placeholder created on the main script thread —
    # without this, anything set from inside a blocking fetch (e.g. a 429
    # retry) is invisible until that whole blocking call returns, since the
    # rate-limit section's own code only runs once per script execution.
    # Defensive import: this is a semi-internal Streamlit API whose module
    # path has moved before across versions — degrade to no live updates
    # rather than crash the app if it's missing.
    from streamlit.runtime.scriptrunner import add_script_run_ctx
except Exception:  # pragma: no cover - depends on installed streamlit version
    add_script_run_ctx = None

from src.calendar_fetcher import reports_up_to_first_kill
from src.deaths_fetcher import get_deaths_by_player_for_ability
from src.damage_taken_fetcher import get_damage_taken_by_player_for_ability
from src.survivability_fetcher import compute_survivability_for_report
from src.api_client import get_rate_limit, is_retryable_error, get_retry_status
from src.guild_rankings_fetcher import get_encounter_zone_id
from src.guild_rankings_store import load_ranking
from src.guild_url import parse_guild_id_from_url
from src.live_runner import LiveMultiGuildRun
from src.log_utils import log_line
from src.multi_guild import class_spec_label
from src.report_cache import (
    get_boss_fights,
    get_guild_reports,
    get_report_actors,
    get_report_class_specs,
    get_stats as get_report_cache_stats,
    reset_report_caches,
)
from src import boss_config
from src import estimate as est
from src import overnight

from sections.env_section import render_env_section
from sections.input_settings import (
    render_input_settings,
    DIFFICULTY,
)
from sections.results_section import render_results, render_class_colored_table


# --------------------------------------------------------------------
# Paths / env bootstrap
# --------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Load .env if present
load_dotenv(ENV_PATH if ENV_PATH.exists() else None)

if "env_validated" not in st.session_state:
    wcl_id = os.getenv("WCL_CLIENT_ID")
    wcl_secret = os.getenv("WCL_CLIENT_SECRET")
    st.session_state["env_validated"] = bool(wcl_id and wcl_secret)

if "analysis_cache" not in st.session_state:
    st.session_state["analysis_cache"] = None

# --------------------------------------------------------------------
# Streamlit page config
# --------------------------------------------------------------------
st.set_page_config(
    page_title="Northstar Warcraft Logs Analyzer",
    layout="centered",
)

st.title("Northstar Warcraft Logs Analyzer")
st.caption("Generate CSV death summaries from Warcraft Logs reports.")


# ====================================================================
# 1. WCL credentials section
# ====================================================================
render_env_section(ENV_PATH)

env_ok = st.session_state.get("env_validated", False)
if not env_ok:
    st.info(
        "Configure and validate your Warcraft Logs credentials above "
        "to unlock the analyzer."
    )
    st.stop()


# ====================================================================
# API rate-limit indicator (same numbers as warcraftlogs.com/profile).
# On-demand: reading it costs 1 point, so only refresh when clicked.
# ====================================================================
_RETRY_WATCHER_STOP_KEY = "_retry_watcher_stop_event"


def _stop_prev_retry_watcher() -> None:
    """Signal any watcher thread from an earlier script run to stop. Its
    writes would be silently dropped anyway once superseded by this new
    run, but there's no reason to leave it polling forever."""
    prev_stop = st.session_state.get(_RETRY_WATCHER_STOP_KEY)
    if prev_stop is not None:
        prev_stop.set()


def _start_retry_watcher(placeholder) -> None:
    """
    Background thread mirroring api_client's live retry/pause status into
    `placeholder`, independent of whether the *main* script thread is stuck
    inside a blocking fetch (single-guild mode runs its whole fetch as one
    blocking call with no reruns in between — without this, a 429 retry
    happening inside it would be invisible until the fetch finishes, since
    the rate-limit section's own code only runs once per script execution).
    """
    if add_script_run_ctx is None:
        return  # installed Streamlit version doesn't expose this API

    stop_event = threading.Event()
    st.session_state[_RETRY_WATCHER_STOP_KEY] = stop_event

    def _watch() -> None:
        last_shown = None
        while not stop_event.is_set():
            status = get_retry_status()
            if status != last_shown:
                try:
                    if status:
                        placeholder.warning(f"⏳ {status['message']}")
                    else:
                        placeholder.empty()
                except Exception:
                    return  # this script run has since ended
                last_shown = status
            stop_event.wait(0.5)

    t = threading.Thread(target=_watch, daemon=True)
    add_script_run_ctx(t)
    t.start()


def _render_rate_limit() -> None:
    _stop_prev_retry_watcher()

    # Kept OUTSIDE the expander (not gated behind it being open) so a retry
    # happening mid-fetch is visible without the user needing to know to
    # click into a collapsed section first.
    retry_placeholder = st.empty()
    _status = get_retry_status()
    if _status:
        retry_placeholder.warning(f"⏳ {_status['message']}")
    _start_retry_watcher(retry_placeholder)

    with st.expander("Warcraft Logs API rate limit", expanded=False):
        if st.button("Check rate limit", key="check_rate_limit"):
            try:
                rl = get_rate_limit()
                st.session_state["rate_limit"] = rl
                st.session_state["rate_limit_fetched_at"] = time.time()
            except Exception as exc:  # the check itself costs a point and can 429 too
                if is_retryable_error(str(exc)):
                    # Estimate a reset time from the last successful check rather
                    # than just dumping the raw 429 — "you're capped" plus a
                    # rough ETA is a lot more useful than a stack-trace-shaped
                    # error message.
                    prev = st.session_state.get("rate_limit")
                    prev_at = st.session_state.get("rate_limit_fetched_at")
                    est_reset_in = None
                    limit_guess = 3600
                    if prev and "error" not in prev:
                        limit_guess = prev.get("limit_per_hour", 3600)
                        if prev_at:
                            elapsed = max(0.0, time.time() - prev_at)
                            est_reset_in = max(
                                0, int(prev.get("points_reset_in", 0) - elapsed)
                            )
                    st.session_state["rate_limit"] = {
                        "error": str(exc),
                        "capped": True,
                        "limit_per_hour": limit_guess,
                        "points_reset_in_estimate": est_reset_in,
                    }
                else:
                    st.session_state["rate_limit"] = {"error": str(exc)}

        rl = st.session_state.get("rate_limit")
        if rl is None:
            st.caption("Click **Check rate limit** to see your current usage.")
            return

        if rl.get("capped"):
            limit = rl.get("limit_per_hour", 3600)
            st.error(f"Capped — {limit:,} of {limit:,} points used this hour (HTTP 429).")
            reset_est = rl.get("points_reset_in_estimate")
            if reset_est is not None:
                mins = max(1, reset_est // 60)
                st.caption(
                    f"Estimated from your last successful check — should reset "
                    f"in ~{mins} min."
                )
            else:
                st.caption(
                    "No earlier check on record to estimate a reset time from — "
                    "try again in a few minutes."
                )
            return

        if "error" in rl:
            st.warning(
                "Couldn't read the rate limit (you may already be throttled): "
                f"{rl['error']}"
            )
            return

        limit = rl["limit_per_hour"]
        spent = rl["points_spent"]
        remaining = max(0.0, limit - spent)
        mins = max(1, rl["points_reset_in"] // 60)
        pct_used = (spent / limit * 100) if limit else 0
        c1, c2, c3 = st.columns(3)
        c1.metric("Points remaining", f"{int(remaining):,}")
        c2.metric("Hourly limit", f"{limit:,}")
        c3.metric("Resets in", f"~{mins} min")
        st.progress(min(1.0, pct_used / 100))
        st.caption(
            f"{int(spent):,} of {limit:,} points used this hour "
            f"({pct_used:.0f}%). Window resets in ~{mins} min."
        )


_render_rate_limit()


# ====================================================================
# 2. Input settings section (delegated to sections/input_settings.py)
# ====================================================================
(
    guild_url,
    start_date,
    end_date,
    targets,
    ignore_after_player_deaths,
    submitted,
    source_settings,
    stop_at_first_kill,
) = render_input_settings()

# Get current raid file for ability name lookups
if "selected_raid_file" not in st.session_state:
    st.session_state["selected_raid_file"] = "Midnight_season_1.json"
current_raid_file = st.session_state["selected_raid_file"]

source_mode = source_settings.get("mode", "single")

# Parse guild id (single-guild mode only; multi-guild mode sources guilds
# from the stored ranking or a pasted list, not a single guild URL).
guild_id: int | None = None
if source_mode == "single":
    guild_id = parse_guild_id_from_url(guild_url)
    if guild_id is None:
        if submitted:
            st.error(
                "Could not parse guild ID from URL. "
                "Expected something like https://www.warcraftlogs.com/guild/id/235490"
            )
        # If URL is bad and we have no previous result, nothing to show
        if not st.session_state["analysis_cache"]:
            st.stop()

# Convert date range to UTC datetimes
start_dt = datetime(
    start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc
)
end_dt = datetime(
    end_date.year,
    end_date.month,
    end_date.day,
    23,
    59,
    59,
    tzinfo=timezone.utc,
)

# 2.5. Metric selection (Deaths vs Hits)
metric_mode = st.radio(
    "What do you want to aggregate?",
    options=["Deaths", "Hits", "Both"],
    index=0,
    horizontal=True,   # 👈 makes the radio buttons line up horizontally
)

show_deaths = metric_mode in ("Deaths", "Both")
show_damage = metric_mode in ("Hits", "Both")

# Initialise per-metric results cache
if "results_cache" not in st.session_state:
    st.session_state["results_cache"] = {"deaths": None, "damage": None}


# ====================================================================
# Top-guilds (world ranking) mode
# ====================================================================
def _ability_label(ability_id, ability_names) -> str:
    if ability_id is None:
        return "All abilities"
    return f"{ability_id} ({ability_names.get(ability_id, 'Unknown')})"


# _class_spec_label for the multi-guild table lives in src/multi_guild.py
# (class_spec_label) so overnight.py's CSV writer can share the exact same
# formatting without importing this Streamlit-heavy module.


def _build_multi_guild_table(rows, metric_is_deaths):
    """Turn merged per-player rows into a display-ready DataFrame, Class first."""
    if metric_is_deaths:
        columns = ["Class", "Player", "Guild", "Total Deaths", "Pulls", "Guild Pulls"]
        data = [
            [
                class_spec_label(r.get("class_spec")),
                r["player"], r["guild"], r["value"], r["pulls"],
                r.get("pulls_for_kill", 0),
            ]
            for r in rows
        ]
    else:
        columns = [
            "Class", "Player", "Guild", "Total Damage", "Hits", "Pulls",
            "Guild Pulls",
        ]
        data = [
            [
                class_spec_label(r.get("class_spec")),
                r["player"],
                r["guild"],
                r["value"],
                r.get("hits", 0),
                r["pulls"],
                r.get("pulls_for_kill", 0),
            ]
            for r in rows
        ]
    return pd.DataFrame(data, columns=columns)


def start_live_multi_guild_run() -> None:
    """
    Validate inputs, do the upfront budget sanity check, then kick off a
    background LiveMultiGuildRun and return immediately — poll_live_multi_
    guild_run() drives it to completion across subsequent reruns so the UI
    stays responsive (progress updates, a working Stop button) instead of
    blocking the script inside one big aggregate_guilds() call per pass.
    """
    if not targets:
        st.error("Please configure at least one boss to analyze.")
        st.session_state["multi_guild_cache"] = None
        return

    guild_input_mode = source_settings.get("guild_input_mode", "rank")
    rank_start = source_settings["rank_start"]
    rank_end = source_settings["rank_end"]

    if guild_input_mode == "links":
        guilds = source_settings.get("manual_guilds") or []
        if not guilds:
            st.error("Paste at least one guild URL above, then try again.")
            st.session_state["multi_guild_cache"] = None
            return
    else:
        # Guilds come from the stored progress ranking (the true world-progress
        # order), which costs ZERO API calls. Slice it to the requested rank range.
        all_ranked = load_ranking(current_raid_file)

        if not all_ranked:
            st.error(
                f"No stored ranking for this raid yet. Add one at "
                f"config/guild_rankings/{current_raid_file}."
            )
            st.session_state["multi_guild_cache"] = None
            return
        guilds = [g for g in all_ranked if rank_start <= g["rank"] <= rank_end]
        if not guilds:
            st.warning(
                f"The stored ranking has {len(all_ranked)} guilds "
                f"(ranks {all_ranked[0]['rank']}–{all_ranked[-1]['rank']}), but none "
                f"in your selected range {rank_start}–{rank_end}. Adjust the range."
            )
            st.session_state["multi_guild_cache"] = None
            return

    metrics: list[bool] = []
    if show_deaths:
        metrics.append(True)
    if show_damage:
        metrics.append(False)

    ability_names = boss_config.get_ability_names(current_raid_file)

    # --- Pre-flight: check the API points budget before a big run ----------
    # Rough per-(guild × metric × boss) cost. Refuses up front if it's clearly
    # not enough; a mid-run shortfall (estimate was too low, or budget spent
    # elsewhere concurrently) is caught by the run itself instead, which
    # stops immediately rather than grinding through doomed guilds.
    num_passes = max(1, len(metrics)) * max(1, len(targets))
    estimated_points = est.estimate_points(len(guilds), num_passes)
    try:
        rl = get_rate_limit()
        st.session_state["rate_limit"] = rl  # keep the indicator fresh
        remaining = rl["limit_per_hour"] - rl["points_spent"]
        if remaining < estimated_points:
            mins = max(1, rl["points_reset_in"] // 60)
            st.error(
                f"Not enough Warcraft Logs API budget for this run. "
                f"Estimated need ≈{estimated_points:,} points, but only "
                f"{int(remaining):,} of {rl['limit_per_hour']:,}/hr remain "
                f"(resets in ~{mins} min). Reduce the rank range or wait, then "
                "try again."
            )
            st.session_state["multi_guild_cache"] = None
            return
        st.caption(
            f"API budget OK — ~{int(remaining):,} points remain; this run "
            f"needs ≈{estimated_points:,}."
        )
    except Exception:
        # Couldn't read the budget (e.g. already throttled). Warn and proceed;
        # the run's own per-chunk budget check will stop it if there's
        # genuinely nothing left.
        st.warning(
            "Couldn't read the API rate-limit budget (you may already be "
            "throttled). Proceeding — the run will stop itself if the budget "
            "turns out to be exhausted."
        )

    passes: list[tuple[int, bool, dict, int | None]] = []
    for target_index, tgt in enumerate(targets):
        # Zone of this boss, so per-guild report scans only pull this raid's
        # reports (skips Mythic+, alt runs, other raids) — a big speedup.
        zone_id = get_encounter_zone_id(tgt["boss_id"])
        for metric_is_deaths in metrics:
            passes.append((target_index, metric_is_deaths, tgt, zone_id))

    run = LiveMultiGuildRun(
        guilds=guilds,
        passes=passes,
        start_dt=start_dt,
        end_dt=end_dt,
        difficulty=DIFFICULTY,
        ignore_after_player_deaths=ignore_after_player_deaths,
        min_attendance_frac=source_settings.get("min_attendance_frac"),
        stop_at_first_kill=stop_at_first_kill,
    )
    run.start()
    st.session_state["mg_live_run"] = run
    st.session_state["mg_live_run_ctx"] = {
        "guilds": guilds,
        "targets": targets,
        "ability_names": ability_names,
        "guild_input_mode": guild_input_mode,
        "rank_start": rank_start,
        "rank_end": rank_end,
        "min_attendance_frac": source_settings.get("min_attendance_frac"),
    }


def poll_live_multi_guild_run() -> bool:
    """
    Poll the active live run, if any, and render its live progress. Returns
    True while a run is still in flight (caller should skip rendering the
    results table this pass — the run just finished writing fresh ones, or
    hasn't started producing any yet). Reruns itself (~2x/sec) via st.rerun()
    while the background thread is alive, so a Stop click gets picked up
    within about one polling interval.
    """
    run = st.session_state.get("mg_live_run")
    if run is None:
        return False

    ctx = st.session_state["mg_live_run_ctx"]
    snap = run.snapshot()

    progress_bar = st.progress(0.0)
    status_area = st.empty()
    budget_area = st.empty()

    num_guilds = max(1, len(ctx["guilds"]))
    num_passes = max(1, snap["num_passes"])
    frac = (snap["pass_index"] + snap["guilds_done_this_pass"] / num_guilds) / num_passes
    progress_bar.progress(min(1.0, max(0.0, frac)))
    status_area.caption(snap["message"])

    if snap["budget"]:
        rl = snap["budget"]
        st.session_state["rate_limit"] = rl
        remaining = rl["limit_per_hour"] - rl["points_spent"]
        budget_area.caption(
            f"💳 {int(rl['points_spent']):,} pts used this hour · "
            f"{int(remaining):,} of {rl['limit_per_hour']:,} remaining"
        )

    if snap["state"] == "running":
        if st.button("Stop", key="mg_live_stop"):
            run.request_stop()
            st.info("Stop requested — finishing the current chunk of guilds, then stopping.")

    if run.is_alive():
        time.sleep(0.5)
        st.rerun()
        return True

    # Finished — either cleanly, stopped, budget-exhausted, or errored.
    # Whatever passes DID complete get shown; nothing is silently discarded.
    entries: list[dict] = []
    for e in snap["entries"]:
        tgt = ctx["targets"][e["target_index"]]
        boss_name = tgt["boss_name"]
        ability_label = _ability_label(tgt["ability_id"], ctx["ability_names"])
        rows = e["rows"]
        df = _build_multi_guild_table(rows, e["metric_is_deaths"])

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow([boss_name, ability_label])
        writer.writerow(df.columns.tolist())
        for r in df.itertuples(index=False, name=None):
            writer.writerow(r)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        entries.append(
            {
                "boss_name": boss_name,
                "ability_label": ability_label,
                "metric_is_deaths": e["metric_is_deaths"],
                "num_guilds": len(ctx["guilds"]),
                "num_players": len(rows),
                "df": df,
                "csv_bytes": csv_bytes,
                "skipped": e["skipped"],
                "stats": e.get("stats", {}),
                "min_attendance_frac": ctx["min_attendance_frac"],
                "guild_input_mode": ctx["guild_input_mode"],
                "rank_start": ctx["rank_start"],
                "rank_end": ctx["rank_end"],
            }
        )

    st.session_state["multi_guild_cache"] = {"entries": entries}
    st.session_state["mg_live_run"] = None
    st.session_state.pop("mg_live_run_ctx", None)

    progress_bar.empty()
    status_area.empty()

    state = snap["state"]
    if state == "done":
        st.success("Run complete.")
    elif state == "stopped":
        st.warning(f"Stopped — {snap['message']}")
    elif state == "budget_exhausted":
        st.warning(snap["message"])
    elif state == "error":
        st.error(snap["message"])

    return False


def render_multi_guild_cache() -> None:
    cache = st.session_state.get("multi_guild_cache")
    if not cache or not cache.get("entries"):
        st.info(
            "Choose a guild list above, pick a boss/ability, "
            "then click **Generate CSV**."
        )
        return

    for i, entry in enumerate(cache["entries"]):
        metric_text = "Deaths" if entry["metric_is_deaths"] else "Hits"
        st.markdown(
            f"#### {entry['boss_name']} — {entry['ability_label']} · {metric_text}"
        )
        if entry.get("guild_input_mode") == "links":
            guild_scope = "from the pasted guild list"
        else:
            guild_scope = f"(ranks {entry['rank_start']}–{entry['rank_end']})"
        st.success(
            f"{entry['num_players']} players across {entry['num_guilds']} "
            f"guilds {guild_scope}."
        )

        stats = entry.get("stats", {})
        frac = entry.get("min_attendance_frac")
        if frac and stats.get("filtered_low_attendance"):
            st.caption(
                f"Filtered out {stats['filtered_low_attendance']} low-attendance "
                f"player(s) below {int(frac * 100)}% of their guild's "
                "pulls-for-kill."
            )

        df = entry["df"]
        search = st.text_input(
            "Search player or guild",
            value="",
            key=f"mg_search_{i}",
            placeholder="Type to filter…",
        )
        if search:
            mask = df["Player"].str.contains(search, case=False, na=False) | df[
                "Guild"
            ].str.contains(search, case=False, na=False)
            df = df[mask]

        render_class_colored_table(df)

        st.download_button(
            "Download CSV",
            data=entry["csv_bytes"],
            file_name="warcraftlogs_top_guilds.csv",
            mime="text/csv",
            key=f"mg_download_{i}",
        )

        skipped = entry.get("skipped", [])
        if skipped:
            with st.expander(
                f"⚠️ {len(skipped)} guild(s) contributed no data "
                "(private logs, boss not pulled, or no matching deaths/hits)"
            ):
                for s in skipped:
                    g = s["guild"]
                    st.markdown(
                        f"- **#{g.get('rank')} {g.get('guild_name')}** "
                        f"({g.get('region')}) — {s['reason']}"
                    )


def render_ranking_estimate(num_guilds: int, num_passes: int) -> None:
    """Show a live, API-free estimate of points + time for the current range."""
    if num_guilds <= 0:
        st.info("No guilds in the selected rank range.")
        return

    rl = st.session_state.get("rate_limit")
    rl = rl if isinstance(rl, dict) and "error" not in rl else None
    plan = est.plan(num_guilds, num_passes, rate_limit=rl)

    c1, c2, c3 = st.columns(3)
    c1.metric("Guilds in range", f"{num_guilds:,}")
    c2.metric("Est. points needed", f"{plan['points']:,}")
    c3.metric("Est. time", est.format_duration(plan["total_seconds"]))

    # "Est. time" above already includes any hourly-window waiting the run
    # would need — even with no live rate-limit reading, plan() assumes the
    # best case (a fresh budget right now) rather than silently reporting
    # pure compute time for a run that obviously can't finish in one window.
    remaining = int(plan["remaining"])
    limit = plan["limit_per_hour"]
    assumed_note = (
        " — assumes a fresh budget right now; click **Check rate limit** "
        "above for a number based on what you've actually got left"
        if plan["budget_is_assumed"]
        else ""
    )

    if plan.get("enough_now"):
        st.success(
            f"Budget OK — ~{remaining:,} of {limit:,}/hr remain; this run needs "
            f"≈{plan['points']:,} points "
            f"(compute {est.format_duration(plan['compute_seconds'])})"
            f"{assumed_note}."
        )
    elif plan.get("exceeds_hourly_budget"):
        st.warning(
            f"This needs ≈{plan['points']:,} points, more than the {limit:,}/hr "
            f"cap — it can't finish in one window. An overnight run would span "
            f"~{plan.get('windows_needed', 0)} hourly reset(s); "
            f"total incl. waiting: {est.format_duration(plan['total_seconds'])}"
            f"{assumed_note}."
        )
    else:
        st.warning(
            f"Not enough budget right now: ~{remaining:,} left, need "
            f"≈{plan['points']:,}. Wait "
            f"{est.format_duration(plan['wait_seconds'])} for reset, reduce the "
            f"range, or run overnight (total incl. waiting: "
            f"{est.format_duration(plan['total_seconds'])})"
            f"{assumed_note}."
        )


def _launch_overnight(job: dict) -> Path:
    """Write the job file and spawn overnight_run.py in its own console."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if job.get("guild_input_mode") == "links":
        suffix = f"links{len(job.get('manual_guilds') or [])}"
    else:
        suffix = f"r{job['rank_start']}-{job['rank_end']}"
    job_dir = PROJECT_ROOT / "output" / "overnight" / f"{stamp}_{suffix}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    creationflags = 0
    if os.name == "nt":
        # A real, visible console window — a background job with no way to
        # tell it's running is exactly what we don't want. Detached from
        # this Streamlit process (CREATE_NEW_PROCESS_GROUP) so it survives
        # closing the browser tab/app; the window itself is now the "is this
        # still running" signal, and closing it is a legitimate (if abrupt —
        # not a clean checkpointed stop) way to kill the run. Output isn't
        # redirected here — overnight_run.py tees its own stdout/stderr to
        # both this console and run.log, so both stay populated regardless
        # of how the script is launched.
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        )
    subprocess.Popen(
        # -u: unbuffered stdout/stderr, so the console (and run.log, via the
        # child's own tee) update live instead of batching in blocks.
        [sys.executable, "-u", str(PROJECT_ROOT / "overnight_run.py"),
         "--job", str(job_dir / "job.json")],
        cwd=str(PROJECT_ROOT),
        creationflags=creationflags,
        close_fds=True,
    )
    return job_dir


def _overnight_state_text(status: dict) -> str:
    """Plain-English state line — 'waiting'/'running' badges alone don't say
    what's actually happening; the detailed message (time remaining, guild
    counts, ...) still shows underneath in the caption."""
    state = status.get("state", "?")
    if state == "starting":
        return "Starting…"
    if state == "waiting":
        return "Waiting for API Limit Reset"
    if state == "running":
        current = status.get("current") or ""
        return f"Currently processing — {current}" if current else "Running…"
    if state == "done":
        return "All Done"
    if state == "stopped":
        return "Stopped"
    if state == "error":
        return "Error"
    return state


def _render_overnight_status(job_dir: str, status: dict) -> None:
    state = status.get("state", "?")
    prog = status.get("progress", {}) or {}
    done, total = prog.get("done", 0), (prog.get("total", 0) or 1)
    badge = {
        "starting": "⏳", "running": "🟢", "waiting": "🟡",
        "done": "✅", "error": "🔴", "stopped": "⏹️",
    }.get(state, "•")

    st.markdown(f"**State:** {badge} {_overnight_state_text(status)}")
    st.progress(min(1.0, done / total))
    st.caption(f"{done}/{total} guild-passes · {status.get('message', '')}")

    b = status.get("budget")
    if isinstance(b, dict):
        rem = int(b["limit_per_hour"] - b["points_spent"])
        st.caption(
            f"Budget: ~{rem:,}/{b['limit_per_hour']:,} pts left · "
            f"resets ~{max(1, b['points_reset_in'] // 60)}m"
        )
    if status.get("error"):
        st.error(status["error"])

    log_path = Path(job_dir) / "run.log"
    if log_path.exists():
        with st.expander("Raw log (run.log)"):
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
            st.code(tail or "(empty so far)", language=None)

    for out in status.get("outputs", []):
        p = Path(out)
        if p.exists():
            st.download_button(
                f"Download {p.name}", data=p.read_bytes(),
                file_name=p.name, mime="text/csv", key=f"ovn_dl_{p.name}",
            )

    if state in ("starting", "running", "waiting"):
        if st.button("Stop run", key="stop_overnight"):
            overnight.request_stop(job_dir)
            st.info("Stop requested — it will exit at the next checkpoint.")


def render_overnight_launcher(job: dict) -> None:
    with st.expander("🌙 Overnight run (auto-resume through rate limits)"):
        st.caption(
            "Opens its own console window with live progress printed to it — "
            "so it's never a silent background process — and keeps going even "
            "if you close this browser tab or the Streamlit app (leave the "
            "machine on). Closing that console window stops the run. If it "
            "runs low on budget mid-range, it sleeps straight through to the "
            "next hourly reset (WCL tells us exactly when that is) rather than "
            "polling every few minutes, then resumes automatically — so a "
            "range too big for one hour just finishes overnight."
        )
        cols = st.columns([1, 1, 2])
        if cols[0].button("Launch overnight run", key="launch_overnight"):
            try:
                job_dir = _launch_overnight(job)
                st.session_state["overnight_job_dir"] = str(job_dir)
                st.success(f"Launched. Job dir: `{job_dir}`")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't launch overnight run: {exc}")
        cols[1].button("Refresh status", key="refresh_overnight")

        job_dir = st.session_state.get("overnight_job_dir")
        if not job_dir:
            return
        status = overnight.read_status(job_dir)
        if status is None:
            # No status.json yet — either it just hasn't started up (normal,
            # takes a couple seconds), or the subprocess died before it got
            # that far (e.g. an import error). run.log has whatever it
            # printed either way — the same output the removed console
            # window would have shown — so surface it directly here instead
            # of leaving this looking permanently stuck.
            log_path = Path(job_dir) / "run.log"
            if log_path.exists():
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                if tail.strip():
                    st.code(tail, language=None)
            st.info("Waiting for the run to write its first status… (auto-refreshing)")
            time.sleep(2)
            st.rerun()
            return
        _render_overnight_status(job_dir, status)
        # Auto-refresh only through the brief startup window — once it's
        # confirmed running, this is meant to be left unattended for hours,
        # so it settles into manual "Refresh status" rather than reloading
        # the whole page indefinitely.
        if status.get("state") == "starting":
            time.sleep(2)
            st.rerun()


if source_mode == "ranking":
    st.markdown("### 3. Results — Multi-guild queue")

    _guild_input_mode = source_settings.get("guild_input_mode", "rank")
    _rank_start = source_settings.get("rank_start", 1)
    _rank_end = source_settings.get("rank_end", 1)
    _manual_guilds = source_settings.get("manual_guilds") or []

    if _guild_input_mode == "links":
        st.caption(
            "Guilds are the ones you pasted above, analyzed together in one queue."
        )
        _num_guilds = len(_manual_guilds)
    else:
        st.caption(
            "Guilds come from a stored world-progress ranking for this raid — "
            "slicing it to a rank range costs no API calls."
        )
        _all_ranked = load_ranking(current_raid_file)
        _num_guilds = sum(
            1 for g in _all_ranked if _rank_start <= g["rank"] <= _rank_end
        )

    # --- Live estimate + overnight launcher (both API-free to build) ----------
    _num_metrics = (1 if show_deaths else 0) + (1 if show_damage else 0)
    _num_passes = max(1, _num_metrics) * max(1, len(targets))
    render_ranking_estimate(_num_guilds, _num_passes)

    _metric_str = (
        "both" if (show_deaths and show_damage)
        else ("deaths" if show_deaths else "damage")
    )
    _overnight_job = {
        "raid_file": current_raid_file,
        "guild_input_mode": _guild_input_mode,
        "rank_start": _rank_start,
        "rank_end": _rank_end,
        "manual_guilds": _manual_guilds,
        "metric": _metric_str,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "min_attendance_frac": source_settings.get("min_attendance_frac"),
        "ignore_after_player_deaths": ignore_after_player_deaths,
        "stop_at_first_kill": stop_at_first_kill,
        "targets": targets,
        "poll_seconds": 300,
    }
    render_overnight_launcher(_overnight_job)

    if submitted:
        start_live_multi_guild_run()
    if not poll_live_multi_guild_run():
        render_multi_guild_cache()
    st.stop()


# ====================================================================
# 3. Results section
# ====================================================================

def compute_and_cache_results(
    *,
    metric_is_deaths: bool,
    cache_key: str,
) -> None:
    """
    Fetch logs + compute aggregation for a metric (deaths or hits)
    and store the result in st.session_state["results_cache"][cache_key].
    """
    if not targets:
        st.error("Please configure at least one boss to analyze.")
        st.session_state["results_cache"][cache_key] = None
        return

    if guild_id is None:
        # Guild URL invalid; don't blow away old results, just show error.
        st.error(
            "Could not parse guild ID from URL. "
            "Expected something like https://www.warcraftlogs.com/guild/id/235490"
        )
        return

    overall_start = time.perf_counter()
    metric_label = "deaths" if metric_is_deaths else "hits"

    # --- Fetch reports for guild/date range -----------------------------
    with st.spinner(f"Fetching reports from Warcraft Logs for {metric_label}…"):
        try:
            reports = get_guild_reports(guild_id, start_dt, end_dt)
        except RuntimeError as exc:
            # Usually the hourly points budget (429). Surface it as a message
            # and keep any previously cached results on screen, rather than
            # crashing the page with a raw traceback.
            st.error(f"Could not fetch reports for {metric_label}: {exc}")
            return

    if not reports:
        st.warning("No reports found in that date range.")
        st.session_state["results_cache"][cache_key] = None
        return

    # --- Build list of jobs (report × target) ---------------------------
    # 1) Group reports by calendar date (based on report start time).
    reports_by_date: dict[str, list[dict]] = {}
    for report in reports:
        start_ms = report["startTime"]
        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        reports_by_date.setdefault(date_str, []).append(report)

    # 2) For each date, choose the report with the longest duration.
    best_reports_per_date: list[tuple[str, dict]] = []
    for date_str, reps in reports_by_date.items():

        def _duration(r: dict) -> int:
            start_ms = r.get("startTime", 0)
            end_ms = r.get("endTime", start_ms)
            return end_ms - start_ms

        best_report = max(reps, key=_duration)
        best_reports_per_date.append((date_str, best_report))

    # 2.5) "End at first kill": per boss, keep only the reports up to and
    # including the one holding its first kill. Each boss gets its own cutoff.
    progress_codes: dict[int, set[str]] = {}
    if stop_at_first_kill:
        reps = [r for _date, r in best_reports_per_date]
        with st.spinner("Locating the first kill of each boss…"):
            for boss_id in {t["boss_id"] for t in targets}:
                progress_codes[boss_id] = {
                    r["code"]
                    for r in reports_up_to_first_kill(
                        reps, boss_id, DIFFICULTY, fights_fn=get_boss_fights
                    )
                }

    # 3) Build jobs only for the chosen report for that date.
    # (date_str, code, target_idx, boss_id, ability_id)
    jobs: list[tuple[str, str, int, int, int | None]] = []
    for date_str, report in best_reports_per_date:
        code = report["code"]
        for target_index, target in enumerate(targets):
            boss_id = target["boss_id"]
            if stop_at_first_kill and code not in progress_codes[boss_id]:
                continue
            jobs.append(
                (
                    date_str,
                    code,
                    target_index,
                    boss_id,
                    target["ability_id"],
                )
            )

    total_jobs = len(jobs)
    if total_jobs == 0:
        st.warning("No report/ability combinations to process.")
        st.session_state["results_cache"][cache_key] = None
        return

    status_area = st.empty()
    progress_bar = st.progress(0.0)

    meta_by_target_date: dict[tuple[int, str], dict] = {}
    all_players: set[str] = set()
    player_class_spec_global: dict[str, tuple[str, str]] = {}

    def process_job(job: tuple[str, str, int, int, int | None]) -> dict:
        """Run the chosen metric fetcher for a single (report, target)."""
        date_str, code, target_index, boss_id, ability_id = job

        # 1) Fetch per-player data for this (report, boss, ability)
        try:
            if metric_is_deaths:
                rows = get_deaths_by_player_for_ability(
                    report_code=code,
                    boss_id=boss_id,
                    ability_id=ability_id,
                    difficulty=DIFFICULTY,
                    wipes_only=True,
                    ignore_after_player_deaths=ignore_after_player_deaths,
                )
            else:
                # Damage-taken mode
                rows = get_damage_taken_by_player_for_ability(
                    report_code=code,
                    boss_id=boss_id,
                    ability_id=ability_id,
                    difficulty=DIFFICULTY,
                    wipes_only=True,
                    ignore_after_player_deaths=ignore_after_player_deaths,
                )
        except Exception as exc:
            return {
                "error": str(exc),
                "target_index": target_index,
                "date_str": date_str,
                "code": code,
            }

        # 2) Build per-player dict
        player_counts = {}
        player_hits = {}
        for r in rows:
            pname = r.get("player", "Unknown")
            if metric_is_deaths:
                player_counts[pname] = int(r.get("total_deaths", 0))
            else:
                player_counts[pname] = int(r.get("total_damage", 0))
                player_hits[pname] = int(r.get("hits", 0))

        # 3) If no hits, skip
        if not player_counts:
            return {
                "target_index": target_index,
                "date_str": date_str,
                "code": code,
                "no_data": True,
            }

        # 4) Fetch fights for that (boss, date) — cached per (report, boss),
        #    shared with the fights fetch inside get_deaths/damage_taken above
        #    instead of re-querying WCL for data we already have.
        try:
            fights = get_boss_fights(
                report_code=code,
                boss_id=boss_id,
                difficulty=DIFFICULTY,
            )
            wipes = [f for f in fights if not f.get("kill", False)]
        except Exception:
            fights = []
            wipes = []

        # 5) Per-player pull (attendance) counts across the wipe fights,
        #    using each fight's friendlyPlayers roster mapped to names.
        player_pulls: dict[str, int] = {}
        if wipes:
            try:
                actors_map = get_report_actors(code)
                for f in wipes:
                    for actor_id in f.get("friendlyPlayers") or []:
                        name = actors_map.get(int(actor_id))
                        if name:
                            player_pulls[name] = player_pulls.get(name, 0) + 1
            except Exception:
                player_pulls = {}

        # 6) Class/spec per player for this report, so results tables can
        #    show "<Class> (<Spec>)" and color rows by class. Use every fight
        #    for this boss (not just wipes) so a clean-kill report still
        #    yields class/spec data. Cached per (report, boss) so multiple
        #    ability targets on the same boss share one playerDetails query.
        player_class_spec: dict[str, tuple[str, str]] = {}
        if fights:
            try:
                fight_ids_for_specs = [f["id"] for f in fights]
                player_class_spec = get_report_class_specs(
                    code, boss_id, fight_ids_for_specs
                )
            except Exception:
                player_class_spec = {}

        return {
            "target_index": target_index,
            "date_str": date_str,
            "code": code,
            "player_counts": player_counts,
            "player_hits": player_hits,
            "num_pulls": len(wipes),
            "player_pulls": player_pulls,
            "player_class_spec": player_class_spec,
        }

    # --- Run jobs in parallel -------------------------------------------
    completed = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_job, j): j for j in jobs}

        for future in as_completed(futures):
            completed += 1
            progress_pct = completed / total_jobs if total_jobs > 0 else 1.0
            progress_bar.progress(progress_pct)
            status_area.caption(
                f"Processed {completed}/{total_jobs} report-ability pairs…"
            )

            result = future.result()

            # Check for error
            if "error" in result:
                continue  # skip this job

            # Check for no-data
            if result.get("no_data"):
                continue

            # Store
            target_index = result["target_index"]
            date_str = result["date_str"]
            code = result["code"]
            player_counts = result["player_counts"]
            player_hits = result["player_hits"]
            num_pulls = result["num_pulls"]
            player_pulls = result.get("player_pulls", {})
            player_class_spec = result.get("player_class_spec", {})

            key = (target_index, date_str)
            meta_by_target_date[key] = {
                "code": code,
                "player_counts": player_counts,
                "player_hits": player_hits,
                "num_pulls": num_pulls,
                "player_pulls": player_pulls,
            }
            all_players.update(player_counts.keys())
            for name, class_spec in player_class_spec.items():
                player_class_spec_global.setdefault(name, class_spec)

    progress_bar.empty()
    status_area.empty()

    # If reports were found but none contained fights for the selected
    # boss(es)/ability, make that explicit instead of silently showing an
    # empty result (which reads like the app failed to generate anything).
    if not meta_by_target_date:
        st.warning(
            f"Fetched {len(best_reports_per_date)} report(s) in this date range, "
            f"but found no **{metric_label}** data for the selected boss(es)/"
            "ability at Mythic difficulty. Likely causes: the boss wasn't pulled "
            "in these logs, the ability ID doesn't match, or the encounter isn't "
            "available on Warcraft Logs yet (e.g. an unreleased raid)."
        )
        st.session_state["results_cache"][cache_key] = None
        return

    # --- Survivability (deaths-only) ------------------------------------------------
    survivability_final: dict[int, dict[str, object]] | None = None
    if metric_is_deaths:
        survivability_final = {}
        surv_keys = {k for k, v in meta_by_target_date.items() if "player_counts" in v}

        for target_index, tgt in enumerate(targets):
            boss_id = tgt["boss_id"]

            boss_keys = {
                k for k in surv_keys if k[0] == target_index
            }

            # Use only first date per target
            if boss_keys:
                first_boss_key = sorted(boss_keys, key=lambda x: x[1])[0]
                date_str = first_boss_key[1]
                code = meta_by_target_date[first_boss_key]["code"]

                try:
                    surv_data = compute_survivability_for_report(
                        report_code=code,
                        boss_id=boss_id,
                        difficulty=DIFFICULTY,
                    )
                    survivability_final[target_index] = {
                        "date_str": date_str,
                        "code": code,
                        "data": surv_data,
                    }
                except Exception:
                    pass

    # --- Build tables ---------------------------------------------------------------
    tables: dict[int, dict[str, object]] = {}

    # Load ability names for current raid
    ability_names = boss_config.get_ability_names(current_raid_file)

    for target_index, _target in enumerate(targets):
        # Collect all entries for this target
        relevant_entries = [
            (k, v)
            for k, v in meta_by_target_date.items()
            if k[0] == target_index and "player_counts" in v
        ]
        if not relevant_entries:
            continue

        # Sort by date
        relevant_entries.sort(key=lambda x: x[0][1])

        entries: list[
            tuple[str, str, dict[str, int], int, dict[str, int], dict[str, int]]
        ] = []
        date_columns: list[str] = []
        friendly_date_labels: list[str] = []

        for (_tgt_idx, date_str), data in relevant_entries:
            code = data["code"]
            player_counts = data["player_counts"]
            pulls = data["num_pulls"]
            player_hits = data["player_hits"]
            player_pulls = data.get("player_pulls", {})

            entries.append(
                (date_str, code, player_counts, pulls, player_hits, player_pulls)
            )
            date_columns.append(date_str)

            # Make a friendlier label
            if pulls:
                friendly_label = f"{date_str} ({pulls}p)"
            else:
                friendly_label = date_str

            friendly_date_labels.append(friendly_label)

        total_pulls = sum(
            pulls for (_date, _code, _counts, pulls, _hits, _ppulls) in entries
        )
        if metric_is_deaths:
            base_total_label = "Total Deaths"
        else:
            base_total_label = "Total Damage"

        # Optionally append total pull count
        if total_pulls <= 0:
            entries = [
                (date_str, code, counts, 0, hits, ppulls)
                for date_str, code, counts, _pulls, hits, ppulls in entries
            ]
        total_col_label = base_total_label
        if total_pulls > 0:
            total_col_label = f"{base_total_label} ({total_pulls} pulls)"

        report_codes = [
            code for (_date, code, _counts, _pulls, _hits, _ppulls) in entries
        ]

        per_report_counts: dict[str, dict[str, int]] = {}
        per_report_hits: dict[str, dict[str, int]] = {}
        per_report_pulls: dict[str, dict[str, int]] = {}
        players_for_target: set[str] = set()

        for date_str, code, player_counts, _pulls, player_hits, player_pulls in entries:
            per_report_counts[code] = player_counts
            per_report_pulls[code] = player_pulls
            if not metric_is_deaths:
                per_report_hits[code] = player_hits
            players_for_target.update(player_counts.keys())

        player_totals: dict[str, int] = {}
        for player in players_for_target:
            total = 0
            for code in report_codes:
                total += per_report_counts.get(code, {}).get(player, 0)
            player_totals[player] = total

        # Per-player pull (attendance) totals across all report-days shown.
        player_pull_totals: dict[str, int] = {}
        for player in players_for_target:
            player_pull_totals[player] = sum(
                per_report_pulls.get(code, {}).get(player, 0)
                for code in report_codes
            )

        sorted_players = sorted(
            players_for_target,
            key=lambda p: (-player_totals[p], p.lower()),
        )

        def _class_spec_label(player: str) -> str:
            cls, spec = player_class_spec_global.get(player, ("", ""))
            if cls and spec:
                return f"{cls} ({spec})"
            if cls:
                return cls
            return "Unknown"

        if metric_is_deaths:
            # Deaths: just one metric column
            df_columns_internal = ["Class", "Player", "Total Deaths"] + date_columns
            rows_for_display: list[list[object]] = []
            for player in sorted_players:
                row = [_class_spec_label(player), player, player_totals[player]]
                for _date, code, _counts, _pulls, _hits, _ppulls in entries:
                    val = per_report_counts.get(code, {}).get(player, 0)
                    row.append(val)
                rows_for_display.append(row)

            df = pd.DataFrame(rows_for_display, columns=df_columns_internal)
            df.reset_index(drop=True, inplace=True)

            df_display = df.copy()
            rename_map = {"Total Deaths": total_col_label}
            rename_map.update(dict(zip(date_columns, friendly_date_labels)))
            df_display = df_display.rename(columns=rename_map)
        else:
            # Damage mode: Damage + Hits
            date_damage_cols: list[str] = []
            for date in date_columns:
                date_damage_cols.append(f"{date}__damage")
                date_damage_cols.append(f"{date}__hits")

            df_columns_internal = (
                ["Class", "Player", "Total Deaths", "Total Hits"] + date_damage_cols
            )

            player_totals_hits: dict[str, int] = {}
            for player in players_for_target:
                total_hits_player = 0
                for code in report_codes:
                    total_hits_player += per_report_hits.get(code, {}).get(
                        player, 0
                    )
                player_totals_hits[player] = total_hits_player

            rows_for_display = []
            for player in sorted_players:
                row = [
                    _class_spec_label(player),
                    player,
                    player_totals[player],
                    player_totals_hits.get(player, 0),
                ]
                for _date, code, _counts, _pulls, _hits, _ppulls in entries:
                    dmg = per_report_counts.get(code, {}).get(player, 0)
                    hits = per_report_hits.get(code, {}).get(player, 0)
                    row.append(dmg)
                    row.append(hits)
                rows_for_display.append(row)

            df = pd.DataFrame(rows_for_display, columns=df_columns_internal)
            df.reset_index(drop=True, inplace=True)

            df_display = df.copy()
            rename_map = {
                "Total Deaths": total_col_label,
                "Total Hits": "Hits",
            }
            for date, friendly in zip(date_columns, friendly_date_labels):
                rename_map[f"{date}__damage"] = f"{friendly} – Damage"
                rename_map[f"{date}__hits"] = f"{friendly} – Hits"

            df_display = df_display.rename(columns=rename_map)

        # Embed each player's pull (attendance) count into the display name,
        # e.g. "Jonach (Pulls: 158)". The raw `df` keeps plain names so the
        # boss-summary view can still group by player.
        df_display["Player"] = df_display["Player"].map(
            lambda name: f"{name} (Pulls: {player_pull_totals.get(name, 0)})"
        )

        # Human labels
        target = targets[target_index]
        boss_label = target["boss_name"]
        ability_id = target["ability_id"]
        if ability_id is not None:
            ability_display = (
                f"{ability_id} ({ability_names.get(ability_id, 'Unknown')})"
            )
        else:
            ability_display = "All abilities"

        # Build CSV for this table
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow([boss_label, ability_display])
        writer.writerow(df_display.columns.tolist())
        for r in df_display.itertuples(index=False, name=None):
            writer.writerow(r)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        # Log links used for this table
        log_links = [
            {"label": label, "report_code": code}
            for label, code in zip(friendly_date_labels, report_codes)
        ]

        # Class/Player/Total(s) are always the leading columns, followed by
        # the per-date/log columns — used to trim to just the summary
        # columns in compact (side-by-side) mode.
        summary_col_count = 3 if metric_is_deaths else 4

        tables[target_index] = {
            "df": df,
            "df_display": df_display,
            "csv_bytes": csv_bytes,
            "log_links": log_links,
            "player_pulls": player_pull_totals,
            "summary_col_count": summary_col_count,
        }

        # Update global list of logs for this run
        global_logs = st.session_state.setdefault("log_links_global", [])
        for entry in log_links:
            if entry not in global_logs:
                global_logs.append(entry)

    num_reports = len({info["code"] for info in meta_by_target_date.values()})
    num_players = len(all_players)

    boss_to_targets: dict[int, list[int]] = {}
    for idx, tgt in enumerate(targets):
        boss_to_targets.setdefault(tgt["boss_id"], []).append(idx)

    elapsed = time.perf_counter() - overall_start
    metric_text = "Deaths" if metric_is_deaths else "Hits"
    st.caption(
        f"{metric_text} aggregation finished in {elapsed:0.1f}s – "
        f"{num_reports} reports, {num_players} players."
    )

    # Store everything needed to re-render without recomputing
    cache_entry = {
        "tables": tables,
        "targets": targets,
        "num_reports": num_reports,
        "num_players": num_players,
        "boss_to_targets": boss_to_targets,
        "raid_file": current_raid_file,  # Store the raid file used
    }
    if metric_is_deaths:
        cache_entry["survivability"] = survivability_final

    if "results_cache" not in st.session_state:
        st.session_state["results_cache"] = {}

    st.session_state["results_cache"][cache_key] = cache_entry

def render_from_cache(
    *,
    metric_is_deaths: bool,
    cache_key: str,
    key_prefix: str,
    section_title: str,
    compact: bool = False,
) -> None:
    """Render results for a metric using cached data (if available)."""
    cache_root = st.session_state.get("results_cache", {})
    cache = cache_root.get(cache_key)
    if not cache:
        st.info("No results for this metric yet. Click **Generate CSV** above.")
        return

    render_results(
        cache["tables"],
        cache["targets"],
        cache["num_reports"],
        cache["num_players"],
        cache["boss_to_targets"],
        metric_is_deaths,
        cache.get("raid_file", "Midnight_season_1.json"),  # Get raid file from cache
        key_prefix=key_prefix,
        section_title=section_title,
        compact=compact,
    )


# --------------------------------------------------------------------
# Trigger computation (on button click) and always render from cache
# --------------------------------------------------------------------
if submitted and guild_id is not None:
    # Fresh fights cache per run, so the deaths and damage passes share the
    # first-kill scan instead of repeating it.
    reset_report_caches()
    try:
        _rl_before = get_rate_limit()
    except Exception:
        _rl_before = None

    if show_deaths:
        compute_and_cache_results(
            metric_is_deaths=True,
            cache_key="deaths",
        )
    if show_damage:
        compute_and_cache_results(
            metric_is_deaths=False,
            cache_key="damage",
        )

    # Run summary — how many distinct reports this touched and roughly what
    # it cost, so that's visible without having to check warcraftlogs.com.
    _stats = get_report_cache_stats()
    _summary_fields = {"Reports fetched": _stats["reports_fetched"]}
    try:
        _rl_after = get_rate_limit()
        if _rl_before is not None:
            _pts = round(_rl_after["points_spent"] - _rl_before["points_spent"], 2)
            _summary_fields["Points spent"] = f"{_pts:,}"
    except Exception:
        pass
    log_line(Result="run complete", **_summary_fields)

# Always render whatever we have cached, based on current metric_mode.
# When both metrics are shown, put them side by side — but only once the
# container is actually wide enough (i.e. Streamlit's "Wide mode" is on).
# st.columns() itself has no such awareness (it always sits side by side,
# just getting cramped in the default centered layout), so instead the two
# columns are given a flex-basis via CSS scoped to this one keyed container:
# under the ~730px-wide centered layout they don't fit side by side and
# wrap onto separate lines; once Wide mode removes that cap, they fit and
# sit side by side.
if show_deaths and show_damage:
    st.markdown(
        """
        <style>
          div.st-key-results_side_by_side [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
          }
          div.st-key-results_side_by_side [data-testid="stColumn"] {
            flex: 1 1 480px;
            min-width: 420px;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key="results_side_by_side"):
        col_deaths, col_damage = st.columns(2)
        with col_deaths:
            render_from_cache(
                metric_is_deaths=True,
                cache_key="deaths",
                key_prefix="deaths_",
                section_title="### 3. Results — Deaths",
                compact=True,
            )
        with col_damage:
            render_from_cache(
                metric_is_deaths=False,
                cache_key="damage",
                key_prefix="damage_",
                section_title="### 4. Results — Hits",
                compact=True,
            )
elif show_deaths:
    render_from_cache(
        metric_is_deaths=True,
        cache_key="deaths",
        key_prefix="deaths_",
        section_title="### 3. Results — Deaths",
    )
elif show_damage:
    render_from_cache(
        metric_is_deaths=False,
        cache_key="damage",
        key_prefix="damage_",
        section_title="### 3. Results — Hits",
    )
