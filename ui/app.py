import os
import io
import csv
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import get_deaths_by_player_for_ability

# --- ABILITY NAME LOOKUP ------------------------------------------------------
ABILITY_NAMES = {
    # Plexus Sentinel
    1219346: "Obliteration Arcanocannon / Tank mechanic",
    1219223: "Atomize / Wall mechanic",

    # Loom'ithar
    1226877: "Primal Spellstorm",
    1226366: "Living Silk",
    1237307: "Lair Weaving",

    # Forgeweaver Araz
    1228168: "Silencing Tempest",
    1237322: "Prime Sequence",

    # The Soul Hunters
    1247495: "Null Explosion",
    1227846: "Soul Hunt / Soaking",

    # Fractillus
    1230163: "Fracture",
    1247424: "Null Consumption",

    # Nexus-King Salhadaar
    1227472: "Besiege",
    1224794: "Conquer",
    1225331: "Galactic Smash",
    1224840: "Behead",
}

# --- BOSS LIST ------------------------------------------------------
BOSS_OPTIONS = {
    "Plexus Sentinel": {
        "id": 3122,
        "abilities": [1219346, 1219223],
    },
    "Loom'ithar": {
        "id": 3123,
        "abilities": [1226877, 1226366, 1237307],
    },
    "Soulbinder Naazindhri": {
        "id": 3129,
        "abilities": [],
    },
    "Forgeweaver Araz": {
        "id": 3132,
        "abilities": [1228168, 1237322],
    },
    "The Soul Hunters": {
        "id": 3133,
        "abilities": [1247495, 1227846],
    },
    "Fractillus": {
        "id": 3135,
        "abilities": [1230163, 1247424],
    },
    "Nexus-King Salhadaar": {
        "id": 3134,
        "abilities": [1227472, 1224794, 1225331, 1224840],
    },
    "Dimensius, the All-Devouring": {
        "id": 3141,
        "abilities": [],
    },
}

# Always Mythic
DIFFICULTY = 5

# --------------------------------------------------------------------
# Streamlit setup
# --------------------------------------------------------------------
load_dotenv()

st.set_page_config(
    page_title="Northstar – Warcraft Logs Deaths Summary",
    layout="centered",
)

if "show_form" not in st.session_state:
    st.session_state["show_form"] = True

st.title("Northstar – Warcraft Logs Deaths Summary")

# Back button when in "results" mode
if not st.session_state["show_form"]:
    if st.button("← Back to inputs"):
        st.session_state["show_form"] = True

# --------------------------------------------------------------------
# INPUT FORM
# --------------------------------------------------------------------
if st.session_state["show_form"]:
    with st.form("input_form"):
        guild_url = st.text_input(
            "Warcraft Logs guild URL",
            placeholder="https://www.warcraftlogs.com/guild/id/260153",
        )

        today = datetime.now(timezone.utc).date()
        default_start = today - timedelta(days=7)
        default_end = today

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start date", default_start)
        with col2:
            end_date = st.date_input("End date", default_end)

        boss_names = list(BOSS_OPTIONS.keys())
        default_boss_index = (
            boss_names.index("Nexus-King Salhadaar")
            if "Nexus-King Salhadaar" in boss_names
            else 0
        )
        boss_name = st.selectbox("Boss", boss_names, index=default_boss_index)
        boss_info = BOSS_OPTIONS[boss_name]

        ability_ids = boss_info["abilities"]
        ability_id = None
        ability_label = "All abilities"

        if ability_ids:
            ability_labels = [
                f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
                for ability_id in ability_ids
            ]
            ability_label = st.selectbox("Ability", ability_labels)
            ability_id_str = ability_label.split(" ", 1)[0]
            try:
                ability_id = int(ability_id_str)
            except ValueError:
                ability_id = None
        else:
            st.info(
                "No specific abilities configured for this boss. "
                "All deaths on this boss will be counted."
            )

        wipes_only = st.checkbox("Wipes only", value=True)

        submitted = st.form_submit_button("Fetch data")

    if not submitted:
        st.stop()

    # ----------------------------------------------------------------
    # Parse guild ID from URL
    # ----------------------------------------------------------------
    try:
        parts = guild_url.strip("/").split("/")
        idx = parts.index("id") + 1
        guild_id = int(parts[idx])
    except Exception:
        st.error(
            "Could not parse guild ID from URL. "
            "Expected something like https://www.warcraftlogs.com/guild/id/260153"
        )
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

    # ----------------------------------------------------------------
    # Fetch reports
    # ----------------------------------------------------------------
    with st.spinner("Fetching reports from Warcraft Logs…"):
        reports = fetch_logs_for_guild(guild_id, start_dt, end_dt)

    if not reports:
        st.warning("No reports found in that date range.")
        st.stop()

    status_area = st.empty()
    progress_bar = st.progress(0.0)

    per_report_counts: dict[str, dict[str, int]] = {}
    all_players: set[str] = set()
    meta_by_date: dict[str, dict[str, object]] = {}

    total_reports = len(reports)

    # ----------------------------------------------------------------
    # Process each report, deduplicate per date
    # ----------------------------------------------------------------
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

        try:
            rows = get_deaths_by_player_for_ability(
                report_code=code,
                boss_id=boss_info["id"],
                ability_id=ability_id,
                difficulty=DIFFICULTY,
                wipes_only=wipes_only,
            )
        except RuntimeError as exc:
            st.warning(f"Skipping report {code} due to API error.")
            continue

        # rows: list[{"player": str, "total_deaths": int, ...}]
        player_counts = {
            row["player"]: row["total_deaths"] for row in rows
        }
        total_deaths = sum(player_counts.values())

        # Rule 1: drop logs with no relevant deaths
        if total_deaths == 0:
            continue

        # Rule 2: if multiple logs with same date, keep the one with most deaths.
        existing = meta_by_date.get(date_str)
        if existing is None or total_deaths > existing["total_deaths"]:
            # If we're replacing an earlier "best" for this date, remove its counts
            if existing is not None:
                old_code = existing["code"]
                per_report_counts.pop(old_code, None)

            meta_by_date[date_str] = {
                "code": code,
                "total_deaths": total_deaths,
            }
            per_report_counts[code] = player_counts

        # track all players across all kept reports
        all_players.update(player_counts.keys())

    status_area.empty()
    progress_bar.empty()

    if not meta_by_date:
        st.warning(
            "No deaths found for the selected boss/ability in this date range."
        )
        st.stop()

    # Build final list of reports: one per date
    report_dates: list[tuple[str, str]] = sorted(
        [(meta["code"], date_str) for date_str, meta in meta_by_date.items()],
        key=lambda x: x[1],
    )

    date_columns = [date for (_code, date) in report_dates]

    # ----------------------------------------------------------------
    # Build matrix (players x dates) + totals
    # ----------------------------------------------------------------
    player_totals: dict[str, int] = {}
    for player in all_players:
        total = 0
        for code, _date in report_dates:
            total += per_report_counts.get(code, {}).get(player, 0)
        player_totals[player] = total

    sorted_players = sorted(
        all_players,
        key=lambda p: (-player_totals[p], p.lower()),
    )

    rows_for_csv: list[list[object]] = []
    for player in sorted_players:
        row = [player]
        total = 0
        for code, _date in report_dates:
            deaths = per_report_counts.get(code, {}).get(player, 0)
            row.append(deaths)
            total += deaths
        row.append(total)
        rows_for_csv.append(row)

    # ----------------------------------------------------------------
    # Build CSV + DataFrame for display
    # ----------------------------------------------------------------
    boss_label = boss_name
    if ability_id is not None:
        ability_display = f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
    else:
        ability_display = "All abilities"

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)

    # First row: boss, ability
    writer.writerow([boss_label, ability_display])
    # Second row: Player + dates + Total
    writer.writerow(["Player"] + date_columns + ["Total Deaths"])
    # Player rows
    for r in rows_for_csv:
        writer.writerow(r)

    csv_bytes = csv_buffer.getvalue().encode("utf-8")

    df_columns = ["Player"] + date_columns + ["Total Deaths"]
    df = pd.DataFrame(rows_for_csv, columns=df_columns)

    # Switch UI into "results" mode
    st.session_state["show_form"] = False

    st.success(
        f"Found {len(report_dates)} reports across {len(all_players)} players."
    )
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="deaths_summary.csv",
        mime="text/csv",
    )
    st.dataframe(df, use_container_width=True)
