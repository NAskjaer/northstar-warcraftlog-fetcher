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
)
from src.damage_taken_fetcher import get_damage_taken_by_player_for_ability



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
                    ignore_after_player_deaths=ignore_after_player_deaths,
                )
                value_key = "total_deaths"
                player_hits: dict[str, int] = {}
                total_hits = 0
            else:
                rows = get_damage_taken_by_player_for_ability(
                    report_code=code,
                    boss_id=boss_id,
                    ability_id=ability_id,
                    difficulty=DIFFICULTY,
                    ignore_after_player_deaths=ignore_after_player_deaths,
                )
                value_key = "total_damage"
                # damage fetcher already returns hits per row
                player_hits = {
                    row["player"]: row.get("hits", 0) for row in rows
                }
                total_hits = sum(player_hits.values())
        except RuntimeError:
            rows = []
            value_key = "total_deaths" if metric_is_deaths else "total_damage"
            player_hits = {}
            total_hits = 0

        # 2) Build per-player values for this report
        player_counts = {
            row["player"]: row.get(value_key, 0) for row in rows
        }
        total_deaths = sum(player_counts.values())

        # 3) Count pulls for this boss in this report
        try:
            fights = get_boss_fights_for_report(
                report_code=code,
                boss_id=boss_id,
                difficulty=DIFFICULTY,
            )
            num_pulls = len(fights)
        except RuntimeError:
            num_pulls = 0

        return {
            "date_str": date_str,
            "code": code,
            "target_index": target_index,
            "total_deaths": total_deaths,
            "player_counts": player_counts,
            "boss_id": boss_id,
            "ability_id": ability_id,
            "num_pulls": num_pulls,
            "player_hits": player_hits,
            "total_hits": total_hits,
        }

    # --- Run jobs in parallel -------------------------------------------
    max_workers = 8
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_job, job) for job in jobs]

        for i, future in enumerate(as_completed(futures), start=1):
            result = future.result()

            code = result["code"]
            date_str = result["date_str"]
            target_index = result["target_index"]
            total_deaths = result["total_deaths"]
            player_counts = result["player_counts"]
            boss_id = result["boss_id"]
            ability_id = result["ability_id"]
            num_pulls = result["num_pulls"]
            player_hits = result.get("player_hits") or {}
            total_hits = result.get("total_hits", 0)

            ability_label = "All abilities" if ability_id is None else str(ability_id)
            status_area.write(
                f"Processed {i}/{total_jobs} jobs "
                f"(report={code}, boss_id={boss_id}, ability={ability_label})."
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
                    "player_hits": player_hits,
                    "total_hits": total_hits,
                }
                all_players.update(player_counts.keys())

    status_area.empty()
    progress_bar.empty()

    if not meta_by_target_date:
        msg = (
            "No deaths found for the selected bosses/abilities in this date range."
            if metric_is_deaths
            else "No damage taken found for the selected bosses/abilities "
                 "in this date range."
        )
        st.warning(msg)
        st.session_state["results_cache"][cache_key] = None
        return

    # ----------------------------------------------------------------
    # Build one matrix-style table per target (boss + ability)
    # ----------------------------------------------------------------
    per_target_entries: dict[
        int, list[tuple[str, str, dict[str, int], int, dict[str, int]]]
    ] = {}
    for (target_index, date_str), info in meta_by_target_date.items():
        per_target_entries.setdefault(target_index, []).append(
            (
                date_str,
                info["code"],
                info["player_counts"],
                info.get("num_pulls", 0),
                info.get("player_hits", {}),
            )
        )

    tables: dict[int, dict[str, object]] = {}

    for target_index, entries in per_target_entries.items():
        # Sort days for this target
        entries.sort(key=lambda tup: tup[0])

        # Raw ISO date strings and pulls per date
        date_columns = [date for (date, _code, _counts, _pulls, _hits) in entries]
        pulls_per_date = {
            date: pulls for (date, _code, _counts, pulls, _hits) in entries
        }
        total_pulls = sum(pulls_per_date.values())

        # Build nice DD/MM labels with pulls
        from datetime import datetime as _dt

        friendly_date_labels: list[str] = []
        for date in date_columns:
            dt = _dt.strptime(date, "%Y-%m-%d").date()
            base = dt.strftime("%d/%m")
            pulls = pulls_per_date.get(date, 0)
            if pulls > 0:
                label = f"{base} ({pulls} pulls)"
            else:
                label = base
            friendly_date_labels.append(label)

        base_total_label = (
            "Total Deaths" if metric_is_deaths else "Total Damage Taken"
        )
        total_col_label = base_total_label
        if total_pulls > 0:
            total_col_label = f"{base_total_label} ({total_pulls} pulls)"

        report_codes = [code for (_date, code, _counts, _pulls, _hits) in entries]

        # Build per-report player counts and totals
        per_report_counts: dict[str, dict[str, int]] = {}
        per_report_hits: dict[str, dict[str, int]] = {}
        players_for_target: set[str] = set()

        for date_str, code, player_counts, _pulls, player_hits in entries:
            per_report_counts[code] = player_counts
            if not metric_is_deaths:
                per_report_hits[code] = player_hits
            players_for_target.update(player_counts.keys())

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

        if metric_is_deaths:
            # Deaths: just one metric column
            df_columns_internal = ["Player", "Total Deaths"] + date_columns
            rows_for_display: list[list[object]] = []
            for player in sorted_players:
                row = [player, player_totals[player]]
                for _date, code, _counts, _pulls, _hits in entries:
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
                for _date, code, _counts, _pulls, _hits in entries:
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

        # Build per-column log metadata (for links in the UI)
        # friendly_date_labels and report_codes are in the same order
        log_links = [
            {"label": label, "report_code": code}
            for label, code in zip(friendly_date_labels, report_codes)
        ]

        tables[target_index] = {
            "df": df,                    # raw (used for boss summary)
            "df_display": df_display,    # pretty (for single-ability view)
            "csv_bytes": csv_bytes,
            "log_links": log_links,      # <--- NEW: log metadata
        }


    num_reports = len({info["code"] for info in meta_by_target_date.values()})
    num_players = len(all_players)

    # Build mapping boss_id -> list of target indices (for boss summary view)
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
    st.session_state["results_cache"][cache_key] = {
        "tables": tables,
        "targets": targets,
        "num_reports": num_reports,
        "num_players": num_players,
        "boss_to_targets": boss_to_targets,
    }


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

    from sections.results_section import render_results

    render_results(
        cache["tables"],
        cache["targets"],
        cache["num_reports"],
        cache["num_players"],
        cache["boss_to_targets"],
        metric_is_deaths,
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
