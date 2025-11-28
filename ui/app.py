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
from src.deaths_fetcher import (get_deaths_by_player_for_ability, get_boss_fights_for_report)


from sections.env_section import render_env_section
from sections.input_settings import (
    render_input_settings,
    ABILITY_NAMES,
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
# 2. Input settings section (delegated to sections/input_settings.py)
# ====================================================================
guild_url, start_date, end_date, targets, ignore_after_player_deaths, submitted = (
    render_input_settings()
)

# Parse guild id (only once; error out if bad)
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

# ====================================================================
# 3. Results section (compute OR reuse cached)
# ====================================================================
if submitted and guild_id is not None:
    if not targets:
        st.error("Please configure at least one boss to analyze.")
        st.session_state["analysis_cache"] = None
        st.stop()

    overall_start = time.perf_counter()

    # --- Fetch reports for guild/date range -----------------------------
    with st.spinner("Fetching reports from Warcraft Logs…"):
        reports = fetch_logs_for_guild(guild_id, start_dt, end_dt)

    if not reports:
        st.warning("No reports found in that date range.")
        st.session_state["analysis_cache"] = None
        st.stop()

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
    jobs: list[tuple[str, str, int, int, int | None]] = []  # (date_str, code, target_idx, boss_id, ability_id)

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
        st.session_state["analysis_cache"] = None
        st.stop()

    status_area = st.empty()
    progress_bar = st.progress(0.0)

    meta_by_target_date: dict[tuple[int, str], dict] = {}
    all_players: set[str] = set()

    def process_job(job: tuple[str, str, int, int, int | None]) -> dict:
        """Run get_deaths_by_player_for_ability for a single (report, target)."""
        date_str, code, target_index, boss_id, ability_id = job
        try:
            rows = get_deaths_by_player_for_ability(
                report_code=code,
                boss_id=boss_id,
                ability_id=ability_id,
                difficulty=DIFFICULTY,
                ignore_after_player_deaths=ignore_after_player_deaths,
            )
        except RuntimeError:
            # API error – treat as no data for this job
            rows = []

        # Count pulls for this boss in this report
        try:
            fights = get_boss_fights_for_report(
                report_code=code,
                boss_id=boss_id,
                difficulty=DIFFICULTY,
            )
            num_pulls = len(fights)
        except RuntimeError:
            num_pulls = 0

        player_counts = {row["player"]: row["total_deaths"] for row in rows}
        total_deaths = sum(player_counts.values())

        return {
            "date_str": date_str,
            "code": code,
            "target_index": target_index,
            "total_deaths": total_deaths,
            "player_counts": player_counts,
            "boss_id": boss_id,
            "ability_id": ability_id,
            "num_pulls": num_pulls,
        }


    # --- Run jobs in parallel -------------------------------------------
    max_workers = 8  # tweak if needed
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_job, job) for job in jobs]

        for i, future in enumerate(as_completed(futures), start=1):
            result = future.result()

            date_str = result["date_str"]
            code = result["code"]
            target_index = result["target_index"]
            total_deaths = result["total_deaths"]
            player_counts = result["player_counts"]
            boss_id = result["boss_id"]
            ability_id = result["ability_id"]
            num_pulls = result["num_pulls"]

            # Update status/progress in the main thread
            ability_label = "All abilities" if ability_id is None else str(ability_id)
            status_area.write(
                f"Processed {i}/{total_jobs} jobs "
                f"(report={code}, boss_id={boss_id}, ability={ability_label})..."
            )
            progress_bar.progress(i / total_jobs)

            if num_pulls == 0:
                continue

            key = (target_index, date_str)
            existing = meta_by_target_date.get(key)
            if existing is None or total_deaths > existing["total_deaths"]:
                meta_by_target_date[key] = {
                    "code": code,
                    "total_deaths": total_deaths,
                    "player_counts": player_counts,
                    "num_pulls": num_pulls,
                }
                all_players.update(player_counts.keys())

    status_area.empty()
    progress_bar.empty()

    if not meta_by_target_date:
        st.warning(
            "No deaths found for the selected bosses/abilities in this date range."
        )
        st.session_state["analysis_cache"] = None
        st.stop()

    # ----------------------------------------------------------------
    # Build one matrix-style table per target (boss + ability)
    # ----------------------------------------------------------------
    # Group meta rows per target_index
    per_target_entries: dict[int, list[tuple[str, str, dict[str, int], int]]] = {}
    for (target_index, date_str), info in meta_by_target_date.items():
        per_target_entries.setdefault(target_index, []).append(
            (
                date_str,
                info["code"],
                info["player_counts"],
                info.get("num_pulls", 0),
            )
        )

    tables: dict[int, dict[str, object]] = {}

    for target_index, entries in per_target_entries.items():
        # Sort days for this target
        entries.sort(key=lambda tup: tup[0])  # sort by date_str

        # Raw ISO date strings and pulls per date
        date_columns = [date for (date, _code, _counts, _pulls) in entries]
        pulls_per_date = {date: pulls for (date, _code, _counts, pulls) in entries}
        total_pulls = sum(pulls_per_date.values())

        # Build nice DD/MM labels with pulls
        friendly_date_labels: list[str] = []
        for date in date_columns:
            dt = datetime.strptime(date, "%Y-%m-%d").date()
            base = dt.strftime("%d/%m")
            pulls = pulls_per_date.get(date, 0)
            if pulls > 0:
                label = f"{base} ({pulls} pulls)"
            else:
                label = base
            friendly_date_labels.append(label)

        # Label for the total column
        total_col_label = "Total Deaths"
        if total_pulls > 0:
            total_col_label = f"{total_col_label} ({total_pulls} pulls)"

        report_codes = [code for (_date, code, _counts, _pulls) in entries]

        # Build per-report player counts and total player set
        per_report_counts: dict[str, dict[str, int]] = {}
        players_for_target: set[str] = set()

        for date_str, code, player_counts, _pulls in entries:
            per_report_counts[code] = player_counts
            players_for_target.update(player_counts.keys())


        # Totals per player across all days for this target
        player_totals: dict[str, int] = {}
        for player in players_for_target:
            total = 0
            for code in report_codes:
                total += per_report_counts.get(code, {}).get(player, 0)
            player_totals[player] = total

        sorted_players = sorted(
            players_for_target,
            key=lambda p: (-player_totals[p], p.lower()),
        )

        # Internal column names (raw, used for boss summary)
        df_columns_internal = ["Player", "Total Deaths"] + date_columns

        rows_for_display: list[list[object]] = []
        for player in sorted_players:
            row = [player, player_totals[player]]
            for _date, code, _counts, _pulls in entries:
                deaths = per_report_counts.get(code, {}).get(player, 0)
                row.append(deaths)
            rows_for_display.append(row)

        # DataFrame with raw column names
        df = pd.DataFrame(rows_for_display, columns=df_columns_internal)
        df.reset_index(drop=True, inplace=True)

        # DataFrame for display / CSV with pretty labels
        df_display = df.copy()
        rename_map = {"Total Deaths": total_col_label}
        rename_map.update(dict(zip(date_columns, friendly_date_labels)))
        df_display = df_display.rename(columns=rename_map)


        # CSV header with boss / ability label
        target = targets[target_index]
        boss_label = target["boss_name"]
        ability_id = target["ability_id"]
        if ability_id is not None:
            ability_display = (
                f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
            )
        else:
            ability_display = "All abilities"

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow([boss_label, ability_display])
        writer.writerow(df_display.columns.tolist())
        for r in df_display.itertuples(index=False, name=None):
            writer.writerow(r)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        tables[target_index] = {
            "df": df,               # raw (used for boss summary)
            "df_display": df_display,  # pretty (for single-ability view)
            "csv_bytes": csv_bytes,
        }


    num_reports = len({info["code"] for info in meta_by_target_date.values()})
    num_players = len(all_players)

    # Build mapping boss_id -> list of target indices (for boss summary view)
    boss_to_targets: dict[int, list[int]] = {}
    for idx, tgt in enumerate(targets):
        boss_to_targets.setdefault(tgt["boss_id"], []).append(idx)

    elapsed = time.perf_counter() - overall_start

    # Cache everything for later interactions (download/search/summary)
    st.session_state["analysis_cache"] = {
        "tables": tables,
        "targets": targets,
        "num_reports": num_reports,
        "num_players": num_players,
        "boss_to_targets": boss_to_targets,
        "elapsed_seconds": elapsed,
    }

else:
    # --- Reuse cached results if available --------------------------
    cache = st.session_state.get("analysis_cache")
    if not cache:
        st.stop()  # nothing to show yet

    tables = cache["tables"]
    targets = cache["targets"]
    num_reports = cache["num_reports"]
    num_players = cache["num_players"]

# --------------------------------------------------------------------
# Display results (delegated to sections/results_section.py)
# --------------------------------------------------------------------
elapsed = None
cache = st.session_state.get("analysis_cache")
if cache and "elapsed_seconds" in cache:
    elapsed = cache["elapsed_seconds"]

if elapsed is not None:
    st.caption(f"Analysis time (fetch + tables): {elapsed:.1f} seconds.")

render_results(tables, targets, num_reports, num_players)
