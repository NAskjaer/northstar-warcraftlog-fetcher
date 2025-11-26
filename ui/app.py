from __future__ import annotations

import os
import io
import csv 
from pathlib import Path
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import get_deaths_by_player_for_ability

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
guild_url, start_date, end_date, targets, submitted = render_input_settings()

# Parse guild id (only once; error out if bad)
try:
    parts = guild_url.strip("/").split("/")
    idx = parts.index("id") + 1
    guild_id = int(parts[idx])
except Exception:
    if submitted:
        st.error(
            "Could not parse guild ID from URL. "
            "Expected something like https://www.warcraftlogs.com/guild/id/260153"
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
        st.stop()

    # --- Compute fresh results --------------------------------------
    with st.spinner("Fetching reports from Warcraft Logs…"):
        reports = fetch_logs_for_guild(guild_id, start_dt, end_dt)

    if not reports:
        st.warning("No reports found in that date range.")
        st.session_state["analysis_cache"] = None
        st.stop()

    status_area = st.empty()
    progress_bar = st.progress(0.0)

    all_players: set[str] = set()
    # key = (target_index, date_str) -> {code, total_deaths, player_counts}
    meta_by_target_date: dict[tuple[int, str], dict] = {}

    total_reports = len(reports)

    for idx, report in enumerate(reports, start=1):
        code = report["code"]
        start_ms = report["startTime"]
        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).date().isoformat()

        status_area.write(
            f"Processing report {idx}/{total_reports}: "
            f"{report.get('title', '')} ({code}, {date_str})"
        )
        progress_bar.progress(idx / total_reports)

        for target_index, target in enumerate(targets):
            try:
                rows = get_deaths_by_player_for_ability(
                    report_code=code,
                    boss_id=target["boss_id"],
                    ability_id=target["ability_id"],
                    difficulty=DIFFICULTY,
                )
            except RuntimeError:
                # Skip this target for this report on API error
                continue

            player_counts = {row["player"]: row["total_deaths"] for row in rows}
            total_deaths = sum(player_counts.values())

            if total_deaths == 0:
                continue

            key = (target_index, date_str)
            existing = meta_by_target_date.get(key)
            if existing is None or total_deaths > existing["total_deaths"]:
                meta_by_target_date[key] = {
                    "code": code,
                    "total_deaths": total_deaths,
                    "player_counts": player_counts,
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
    per_target_entries: dict[int, list[tuple[str, str, dict[str, int]]]] = {}
    for (target_index, date_str), info in meta_by_target_date.items():
        per_target_entries.setdefault(target_index, []).append(
            (date_str, info["code"], info["player_counts"])
        )

    tables: dict[int, dict[str, object]] = {}

    for target_index, entries in per_target_entries.items():
        # Sort days for this target
        entries.sort(key=lambda tup: tup[0])  # sort by date_str
        date_columns = [date for (date, _code, _counts) in entries]
        report_codes = [code for (_date, code, _counts) in entries]

        # Build per-report player counts and total player set
        per_report_counts: dict[str, dict[str, int]] = {}
        players_for_target: set[str] = set()

        for date_str, code, player_counts in entries:
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

        df_columns = ["Player", "Total Deaths"] + date_columns

        rows_for_display: list[list[object]] = []
        for player in sorted_players:
            row = [player, player_totals[player]]
            for _date, code, _counts in entries:
                deaths = per_report_counts.get(code, {}).get(player, 0)
                row.append(deaths)
            rows_for_display.append(row)

        df = pd.DataFrame(rows_for_display, columns=df_columns)
        df.reset_index(drop=True, inplace=True)

        # CSV header with boss / ability label, like the original script
        target = targets[target_index]
        boss_label = target["boss_name"]
        ability_id = target["ability_id"]
        if ability_id is not None:
            ability_display = f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
        else:
            ability_display = "All abilities"

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow([boss_label, ability_display])
        writer.writerow(df_columns)
        for r in rows_for_display:
            writer.writerow(r)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        tables[target_index] = {
            "df": df,
            "csv_bytes": csv_bytes,
        }

    num_reports = len({info["code"] for info in meta_by_target_date.values()})
    num_players = len(all_players)

    # Build mapping: boss_id -> list of target_indices (abilities)
    boss_to_targets = {}
    for idx, tgt in enumerate(targets):
        boss_to_targets.setdefault(tgt["boss_id"], []).append(idx)

    st.session_state["analysis_cache"] = {
        "tables": tables,
        "targets": targets,
        "num_reports": num_reports,
        "num_players": num_players,
        "boss_to_targets": boss_to_targets,
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
render_results(tables, targets, num_reports, num_players)
