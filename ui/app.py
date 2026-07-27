from __future__ import annotations
import os
import io
import csv
import time
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import (
    get_deaths_by_player_for_ability,
    get_boss_fights_for_report,
    _fetch_player_actors,
)
from src.damage_taken_fetcher import get_damage_taken_by_player_for_ability
from src.survivability_fetcher import compute_survivability_for_report
from src.api_client import get_rate_limit
from src.guild_rankings_fetcher import get_encounter_zone_id
from src.guild_rankings_store import load_ranking
from src.multi_guild import aggregate_guilds, reset_report_caches
from src import boss_config

from sections.env_section import render_env_section
from sections.input_settings import (
    render_input_settings,
    DIFFICULTY,
)
from sections.results_section import render_results


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
def _render_rate_limit() -> None:
    with st.expander("Warcraft Logs API rate limit", expanded=False):
        if st.button("Check rate limit", key="check_rate_limit"):
            try:
                st.session_state["rate_limit"] = get_rate_limit()
            except Exception as exc:  # already throttled / network error
                st.session_state["rate_limit"] = {"error": str(exc)}

        rl = st.session_state.get("rate_limit")
        if rl is None:
            st.caption("Click **Check rate limit** to see your current usage.")
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
) = render_input_settings()

# Get current raid file for ability name lookups
if "selected_raid_file" not in st.session_state:
    st.session_state["selected_raid_file"] = "Midnight_season_1.json"
current_raid_file = st.session_state["selected_raid_file"]

source_mode = source_settings.get("mode", "single")

# Parse guild id (single-guild mode only; ranking mode sources guilds from
# the rankings API and does not use a guild URL).
guild_id: int | None = None
if source_mode == "single":
    try:
        parts = guild_url.strip("/").split("/")
        idx = parts.index("id") + 1
        guild_id = int(parts[idx])
    except Exception:
        if submitted:
            st.error(
                "Could not parse guild ID from URL. "
                "Expected something like https://www.warcraftlogs.com/guild/id/235490"
            )
        # If URL is bad and we have no previous result, nothing to show
        if not st.session_state["analysis_cache"]:
            st.stop()
        guild_id = None  # will not be used when we load from cache

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

# 2.5. Metric selection (Deaths vs Damage Taken)
metric_mode = st.radio(
    "What do you want to aggregate?",
    options=["Deaths", "Damage taken", "Both"],
    index=0,
    horizontal=True,   # 👈 makes the radio buttons line up horizontally
)

show_deaths = metric_mode in ("Deaths", "Both")
show_damage = metric_mode in ("Damage taken", "Both")

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


def _build_multi_guild_table(rows, metric_is_deaths):
    """Turn merged per-player rows into (df_display, csv-ready df)."""
    if metric_is_deaths:
        columns = ["Player", "Guild", "Total Deaths", "Pulls", "Guild Pulls"]
        data = [
            [r["player"], r["guild"], r["value"], r["pulls"], r.get("pulls_for_kill", 0)]
            for r in rows
        ]
    else:
        columns = [
            "Player", "Guild", "Total Damage Taken", "Hits", "Pulls", "Guild Pulls"
        ]
        data = [
            [
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


def compute_multi_guild_cache() -> None:
    """Fetch top guilds and aggregate players for each target + metric."""
    if not targets:
        st.error("Please configure at least one boss to analyze.")
        st.session_state["multi_guild_cache"] = None
        return

    # Fresh caches per run so fights/actors/death-events fetched for one
    # metric or ability are reused by the others instead of re-queried.
    reset_report_caches()

    rank_start = source_settings["rank_start"]
    rank_end = source_settings["rank_end"]

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
    entries: list[dict] = []

    # --- Pre-flight: check the API points budget before a big run ----------
    # Rough per-(guild × metric × boss) cost: reports + a few reports' worth of
    # fights/actors/events queries. Refuse up front if the budget can't cover it.
    POINTS_PER_GUILD = 30
    estimated_points = (
        len(guilds) * max(1, len(metrics)) * max(1, len(targets)) * POINTS_PER_GUILD
    )
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
        # per-call backoff + the skipped-guilds list handle throttling.
        st.warning(
            "Couldn't read the API rate-limit budget (you may already be "
            "throttled). Proceeding — any guilds that fail will be listed below."
        )

    for target_index, tgt in enumerate(targets):
        boss_id = tgt["boss_id"]
        ability_id = tgt["ability_id"]
        boss_name = tgt["boss_name"]

        # Zone of this boss, so per-guild report scans only pull this raid's
        # reports (skips Mythic+, alt runs, other raids) — a big speedup.
        zone_id = get_encounter_zone_id(boss_id)

        for metric_is_deaths in metrics:
            progress_bar = st.progress(0.0)
            status_area = st.empty()
            metric_label = "deaths" if metric_is_deaths else "damage taken"

            def _cb(done, total, _label=metric_label, _boss=boss_name):
                progress_bar.progress(done / total if total else 1.0)
                status_area.caption(
                    f"{_boss} — {_label}: analyzed {done}/{total} guilds…"
                )

            rows, skipped, stats = aggregate_guilds(
                guilds,
                boss_id=boss_id,
                ability_id=ability_id,
                start_dt=start_dt,
                end_dt=end_dt,
                zone_id=zone_id,
                difficulty=DIFFICULTY,
                metric_is_deaths=metric_is_deaths,
                ignore_after_player_deaths=ignore_after_player_deaths,
                min_attendance_frac=source_settings.get("min_attendance_frac"),
                progress_callback=_cb,
            )
            progress_bar.empty()
            status_area.empty()

            df = _build_multi_guild_table(rows, metric_is_deaths)

            csv_buffer = io.StringIO()
            writer = csv.writer(csv_buffer)
            writer.writerow(
                [boss_name, _ability_label(ability_id, ability_names)]
            )
            writer.writerow(df.columns.tolist())
            for r in df.itertuples(index=False, name=None):
                writer.writerow(r)
            csv_bytes = csv_buffer.getvalue().encode("utf-8")

            entries.append(
                {
                    "boss_name": boss_name,
                    "ability_label": _ability_label(ability_id, ability_names),
                    "metric_is_deaths": metric_is_deaths,
                    "num_guilds": len(guilds),
                    "num_players": len(rows),
                    "df": df,
                    "csv_bytes": csv_bytes,
                    "skipped": skipped,
                    "stats": stats,
                    "min_attendance_frac": source_settings.get(
                        "min_attendance_frac"
                    ),
                    "rank_start": rank_start,
                    "rank_end": rank_end,
                }
            )

    st.session_state["multi_guild_cache"] = {"entries": entries}


def render_multi_guild_cache() -> None:
    cache = st.session_state.get("multi_guild_cache")
    if not cache or not cache.get("entries"):
        st.info(
            "Pick a rank range above, choose a boss/ability, "
            "then click **Generate CSV**."
        )
        return

    for i, entry in enumerate(cache["entries"]):
        metric_text = "Deaths" if entry["metric_is_deaths"] else "Damage taken"
        st.markdown(
            f"#### {entry['boss_name']} — {entry['ability_label']} · {metric_text}"
        )
        st.success(
            f"{entry['num_players']} players across {entry['num_guilds']} "
            f"guilds (ranks {entry['rank_start']}–{entry['rank_end']})."
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

        df = df.reset_index(drop=True)
        df.index = range(1, len(df) + 1)
        st.dataframe(df, use_container_width=True)

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


if source_mode == "ranking":
    st.markdown("### 3. Results — Top guilds (world ranking)")
    st.caption(
        "Guilds are ranked via Warcraft Logs' public fight rankings for the "
        "selected boss. Privately-logged guilds have no public ranking and are "
        "not included."
    )
    if submitted:
        compute_multi_guild_cache()
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
    Fetch logs + compute aggregation for a metric (deaths or damage taken)
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
    metric_label = "deaths" if metric_is_deaths else "damage taken"

    # --- Fetch reports for guild/date range -----------------------------
    with st.spinner(f"Fetching reports from Warcraft Logs for {metric_label}…"):
        reports = fetch_logs_for_guild(guild_id, start_dt, end_dt)

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

    # 3) Build jobs only for the chosen report for that date.
    # (date_str, code, target_idx, boss_id, ability_id)
    jobs: list[tuple[str, str, int, int, int | None]] = []
    for date_str, report in best_reports_per_date:
        code = report["code"]
        for target_index, target in enumerate(targets):
            jobs.append(
                (
                    date_str,
                    code,
                    target_index,
                    target["boss_id"],
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

        # 4) Fetch fights for that (boss, date)
        # ...
        try:
            fights = get_boss_fights_for_report(
                report_code=code,
                boss_id=boss_id,
                difficulty=DIFFICULTY,
            )
            wipes = [f for f in fights if not f.get("kill", False)]
        except Exception:
            wipes = []

        # 5) Per-player pull (attendance) counts across the wipe fights,
        #    using each fight's friendlyPlayers roster mapped to names.
        player_pulls: dict[str, int] = {}
        if wipes:
            try:
                actors_map = _fetch_player_actors(code)
                for f in wipes:
                    for actor_id in f.get("friendlyPlayers") or []:
                        name = actors_map.get(int(actor_id))
                        if name:
                            player_pulls[name] = player_pulls.get(name, 0) + 1
            except Exception:
                player_pulls = {}

        return {
            "target_index": target_index,
            "date_str": date_str,
            "code": code,
            "player_counts": player_counts,
            "player_hits": player_hits,
            "num_pulls": len(wipes),
            "player_pulls": player_pulls,
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

            key = (target_index, date_str)
            meta_by_target_date[key] = {
                "code": code,
                "player_counts": player_counts,
                "player_hits": player_hits,
                "num_pulls": num_pulls,
                "player_pulls": player_pulls,
            }
            all_players.update(player_counts.keys())

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
            base_total_label = "Total Damage Taken"

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

        if metric_is_deaths:
            # Deaths: just one metric column
            df_columns_internal = ["Player", "Total Deaths"] + date_columns
            rows_for_display: list[list[object]] = []
            for player in sorted_players:
                row = [player, player_totals[player]]
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
                ["Player", "Total Deaths", "Total Hits"] + date_damage_cols
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
                rename_map[f"{date}__damage"] = f"{friendly} – Damage Taken"
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

        tables[target_index] = {
            "df": df,
            "df_display": df_display,
            "csv_bytes": csv_bytes,
            "log_links": log_links,
            "player_pulls": player_pull_totals,
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
    metric_text = "Deaths" if metric_is_deaths else "Damage taken"
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
    )


# --------------------------------------------------------------------
# Trigger computation (on button click) and always render from cache
# --------------------------------------------------------------------
if submitted and guild_id is not None:
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

# Always render whatever we have cached, based on current metric_mode
if show_deaths:
    render_from_cache(
        metric_is_deaths=True,
        cache_key="deaths",
        key_prefix="deaths_",
        section_title="### 3. Results — Deaths",
    )

if show_damage:
    sec_number = "4" if show_deaths else "3"
    render_from_cache(
        metric_is_deaths=False,
        cache_key="damage",
        key_prefix="damage_",
        section_title=f"### {sec_number}. Results — Damage taken",
    )
