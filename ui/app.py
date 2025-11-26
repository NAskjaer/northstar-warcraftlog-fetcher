from __future__ import annotations

import os
import io
import csv
from pathlib import Path
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd
from dotenv import load_dotenv

from src.api_client import get_wcl_token
from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import get_deaths_by_player_for_ability


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
def render_env_section() -> None:
    wcl_id = os.getenv("WCL_CLIENT_ID", "")
    wcl_secret = os.getenv("WCL_CLIENT_SECRET", "")

    env_ok = st.session_state.get("env_validated", False)

    with st.expander(
        "1. Warcraft Logs API credentials",
        expanded=not env_ok,
    ):
        if env_ok:
            st.success("Warcraft Logs credentials found and marked as valid.")
        else:
            st.warning(
                "No valid Warcraft Logs API credentials found.\n\n"
                "Before you can query reports, configure your Client ID and Secret."
            )

        st.markdown(
            """
To create a client:

1. Go to **https://www.warcraftlogs.com/api/clients/**  
2. Click **+ Create Client**  
3. **Name:** e.g. `northstar-log-fetcher`  
4. **Redirect URL:** `http://localhost`  
5. Leave **Public Client** unchecked (client should be private)  
6. Click **Create** and copy the **Client ID** and **Client Secret** below.
"""
        )

        client_id = st.text_input(
            "WCL_CLIENT_ID",
            value=wcl_id,
            placeholder="Paste your Warcraft Logs Client ID",
            key="wcl_client_id",
        )

        client_secret = st.text_input(
            "WCL_CLIENT_SECRET",
            value=wcl_secret,
            placeholder="Paste your Warcraft Logs Client Secret",
            type="password",
            key="wcl_client_secret",
        )

        def save_and_validate() -> None:
            cid = client_id.strip()
            csecret = client_secret.strip()

            if not cid or not csecret:
                st.error("Both `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` are required.")
                return

            # Write .env file
            try:
                ENV_PATH.write_text(
                    f"WCL_CLIENT_ID={cid}\nWCL_CLIENT_SECRET={csecret}\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                st.error(f"Could not write `.env` file: {exc}")
                return

            # Update process env and reload dotenv
            os.environ["WCL_CLIENT_ID"] = cid
            os.environ["WCL_CLIENT_SECRET"] = csecret
            load_dotenv(ENV_PATH, override=True)

            st.info("Validating credentials with Warcraft Logs…")

            try:
                get_wcl_token()
            except Exception as exc:  # noqa: BLE001
                st.session_state["env_validated"] = False
                st.error(
                    "❌ Validation failed.\n\n"
                    "Warcraft Logs did not accept your credentials or there was a "
                    "network error."
                )
                st.code(str(exc))
            else:
                st.session_state["env_validated"] = True
                st.success(
                    "✅ Credentials saved and validated successfully! "
                    "You now have a working `.env` file in your project root."
                )
                # Rerun so the rest of the UI sees the updated state/env
                st.rerun()

        if st.button("Save & Validate", key="save_validate_env"):
            save_and_validate()


render_env_section()

env_ok = st.session_state.get("env_validated", False)
if not env_ok:
    st.info(
        "Configure and validate your Warcraft Logs credentials above "
        "to unlock the analyzer."
    )
    st.stop()


# ====================================================================
# 2. Input settings section
# ====================================================================

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
    "Plexus Sentinel": {"id": 3122, "abilities": [1219346, 1219223]},
    "Loom'ithar": {"id": 3123, "abilities": [1226877, 1226366, 1237307]},
    "Soulbinder Naazindhri": {"id": 3129, "abilities": []},
    "Forgeweaver Araz": {"id": 3132, "abilities": [1228168, 1237322]},
    "The Soul Hunters": {"id": 3133, "abilities": [1247495, 1227846]},
    "Fractillus": {"id": 3135, "abilities": [1230163, 1247424]},
    "Nexus-King Salhadaar": {
        "id": 3134,
        "abilities": [1227472, 1224794, 1225331, 1224840],
    },
    "Dimensius, the All-Devouring": {"id": 3141, "abilities": []},
}

DIFFICULTY = 5  # always Mythic

if "analysis_cache" not in st.session_state:
    st.session_state["analysis_cache"] = None

with st.expander("2. Input settings", expanded=True):
    st.markdown("Configure which logs to analyze and how to group deaths.")

    with st.form("input_form"):
        guild_url = st.text_input(
            "Guild URL",
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

        submitted = st.form_submit_button("Generate CSV")

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

if submitted:
    # --- Compute fresh results --------------------------------------
    with st.spinner("Fetching reports from Warcraft Logs…"):
        reports = fetch_logs_for_guild(guild_id, start_dt, end_dt)

    if not reports:
        st.warning("No reports found in that date range.")
        st.session_state["analysis_cache"] = None
        st.stop()

    status_area = st.empty()
    progress_bar = st.progress(0.0)

    per_report_counts: dict[str, dict[str, int]] = {}
    all_players: set[str] = set()
    meta_by_date: dict[str, dict[str, object]] = {}

    total_reports = len(reports)

    # Process each report, deduplicate per date
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
            )
        except RuntimeError:
            st.warning(f"Skipping report {code} due to API error.")
            continue

        player_counts = {row["player"]: row["total_deaths"] for row in rows}
        total_deaths = sum(player_counts.values())

        if total_deaths == 0:
            continue

        existing = meta_by_date.get(date_str)
        if existing is None or total_deaths > existing["total_deaths"]:
            if existing is not None:
                old_code = existing["code"]
                per_report_counts.pop(old_code, None)

            meta_by_date[date_str] = {
                "code": code,
                "total_deaths": total_deaths,
            }
            per_report_counts[code] = player_counts

        all_players.update(player_counts.keys())

    status_area.empty()
    progress_bar.empty()

    if not meta_by_date:
        st.warning(
            "No deaths found for the selected boss/ability in this date range."
        )
        st.session_state["analysis_cache"] = None
        st.stop()

    report_dates: list[tuple[str, str]] = sorted(
        [(meta["code"], date_str) for date_str, meta in meta_by_date.items()],
        key=lambda x: x[1],
    )
    date_columns = [date for (_code, date) in report_dates]

    # Build matrix (players x dates) + totals
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

    df_columns = ["Player", "Total Deaths"] + date_columns

    rows_for_display: list[list[object]] = []
    for player in sorted_players:
        row = [player, player_totals[player]]
        for code, _date in report_dates:
            deaths = per_report_counts.get(code, {}).get(player, 0)
            row.append(deaths)
        rows_for_display.append(row)

    boss_label = boss_name
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

    df = pd.DataFrame(rows_for_display, columns=df_columns)
    df.reset_index(drop=True, inplace=True)

    num_reports = len(report_dates)
    num_players = len(all_players)

    # Cache everything for later interactions (download/search)
    st.session_state["analysis_cache"] = {
        "df": df,
        "csv_bytes": csv_bytes,
        "num_reports": num_reports,
        "num_players": num_players,
    }

else:
    # --- Reuse cached results if available --------------------------
    cache = st.session_state.get("analysis_cache")
    if not cache:
        st.stop()  # nothing to show yet

    df = cache["df"]
    csv_bytes = cache["csv_bytes"]
    num_reports = cache["num_reports"]
    num_players = cache["num_players"]

# --------------------------------------------------------------------
# Display results (always uses cached/current df & csv_bytes)
# --------------------------------------------------------------------
st.markdown("### 3. Results")

st.success(
    f"Found {num_reports} reports across {num_players} players."
)

# Top bar for search (left) + download (right)
top_col_left, top_col_right = st.columns([4, 1])

search_query = top_col_left.text_input(
    label="Search player",
    value="",
    placeholder="Type to filter players…",
)

with top_col_right:
    st.write("")
    st.write("")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="deaths_summary.csv",
        mime="text/csv",
        use_container_width=True,
    )

# Filter df for display (CSV still has all rows)
if search_query:
    df_display = df[df["Player"].str.contains(search_query, case=False, na=False)]
else:
    df_display = df

st.dataframe(df_display, use_container_width=True)
