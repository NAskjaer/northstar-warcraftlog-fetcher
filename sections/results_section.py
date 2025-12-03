from __future__ import annotations

from typing import Dict, List, Any

import pandas as pd
import streamlit as st

from sections.input_settings import ABILITY_NAMES


def _target_label(target: dict) -> str:
    boss_name = target.get("boss_name", "Unknown boss")
    ability_id = target.get("ability_id")

    if ability_id is None:
        return f"{boss_name} — All abilities"

    ability_name = ABILITY_NAMES.get(ability_id)
    if ability_name:
        return f"{boss_name} — {ability_id} ({ability_name})"
    return f"{boss_name} — {ability_id}"


def render_results(
    tables: Dict[int, Dict[str, Any]],
    targets: List[Dict[str, Any]],
    num_reports: int,
    num_players: int,
    boss_to_targets: Dict[int, List[int]],
    metric_is_deaths: bool,
    *,
    key_prefix: str = "",
    section_title: str | None = None,
) -> None:
    """
    Render the results UI (single ability + boss summary).

    tables[target_index] = {
        "df":        DataFrame with internal columns
        "df_display": DataFrame with pretty column names
        "csv_bytes": CSV bytes for download
    }
    """
    if not tables:
        st.warning("No data available to display.")
        return

    total_rows = sum(len(t["df"]) for t in tables.values())

    if section_title is None:
        metric_label = "Deaths" if metric_is_deaths else "Damage taken"
        section_title = f"### 3. Results — {metric_label}"

    st.markdown(section_title)

    st.success(
        f"Found {total_rows} report-day entries across {num_players} players "
        f"from {num_reports} reports."
    )

    # ------------------------------------------------------------------ #
    # View mode selector
    # ------------------------------------------------------------------ #
    view_mode = st.radio(
        "View mode",
        options=["Single ability view", "Boss summary (sum multiple abilities)"],
        key=f"{key_prefix}view_mode",
    )

    # ------------------------------------------------------------------ #
    # Single ability view
    # ------------------------------------------------------------------ #
    if view_mode == "Single ability view":
        available_indices = sorted(tables.keys())

        option_labels = [
            _target_label(targets[idx]) for idx in available_indices
        ]

        selected_idx = st.selectbox(
            "Boss / ability to inspect",
            options=list(range(len(available_indices))),
            format_func=lambda i: option_labels[i],
            key=f"{key_prefix}single_select",
        )
        target_index = available_indices[selected_idx]
        target = targets[target_index]
        data = tables[target_index]

        st.markdown(
            f"**Selected:** {_target_label(target)}"
        )

        search = st.text_input(
            "Search player",
            value="",
            key=f"{key_prefix}single_search",
            placeholder="Type to filter players…",
        )

        df_display: pd.DataFrame = data["df_display"]
        if search:
            mask = df_display["Player"].str.contains(search, case=False, na=False)
            df_display = df_display[mask]

        st.dataframe(
            df_display,
            width="stretch",
        )

        # ------------------------------------------------------------
        # Hyperlinks under the table (single ability)
        # ------------------------------------------------------------
        log_links = tables[target_index].get("log_links", [])
        if log_links:
            st.markdown("**Logs used for this table:**")
            for entry in log_links:
                label = entry["label"]
                code = entry["report_code"]
                url = f"https://www.warcraftlogs.com/reports/{code}"
                st.markdown(f"- **{label}** — [Open log]({url})")



        st.download_button(
            "Download CSV",
            data=data["csv_bytes"],
            file_name="warcraftlogs_aggregation.csv",
            mime="text/csv",
            key=f"{key_prefix}single_download",
        )
        return

    # ------------------------------------------------------------------ #
    # Boss summary view (sum multiple abilities per boss)
    # ------------------------------------------------------------------ #
    # Build boss options that actually have tables
    boss_options: List[int] = []
    boss_labels: List[str] = []
    for boss_id, idxs in boss_to_targets.items():
        idxs_with_data = [i for i in idxs if i in tables]
        if not idxs_with_data:
            continue
        boss_name = targets[idxs_with_data[0]]["boss_name"]
        boss_options.append(boss_id)
        boss_labels.append(boss_name)

    if not boss_options:
        st.warning("No bosses with data to summarise.")
        return

    boss_pos = st.selectbox(
        "Boss to summarise",
        options=list(range(len(boss_options))),
        format_func=lambda i: boss_labels[i],
        key=f"{key_prefix}boss_select",
    )
    boss_id = boss_options[boss_pos]
    target_indices = [i for i in boss_to_targets[boss_id] if i in tables]

    # Collect raw DataFrames for this boss and concatenate
    print("\n========== DEBUG: BOSS SUMMARY ==========")
    print("Target indices:", target_indices)

    raw_dfs = []
    for i in target_indices:
        print(f"\n--- RAW DF for table {i} ---")
        print(tables[i]["df"])
        raw_dfs.append(tables[i]["df"])

    merged = pd.concat(raw_dfs, axis=0, ignore_index=True)
    print("\n--- MERGED DF ---")
    print(merged)
    print("Merged columns:", merged.columns.tolist())

    # Identify which columns are numeric OR should be treated numeric
    value_cols = [c for c in merged.columns if c != "Player"]
    print("Value columns selected for sum:", value_cols)

    # Convert everything except Player to numeric (coerce errors → NaN → 0)
    merged_numeric = merged.copy()
    for c in value_cols:
        merged_numeric[c] = pd.to_numeric(merged_numeric[c], errors="coerce").fillna(0).astype(int)

    print("\n--- MERGED (FORCED NUMERIC) ---")
    print(merged_numeric)

    # Now aggregate
    group = merged_numeric.groupby("Player", as_index=False)[value_cols].sum()
    print("\n--- GROUPED DF ---")
    print(group)

    # Build pretty rename map
    template = tables[target_indices[0]]
    template_display = template["df_display"]

    raw_cols = list(template["df"].columns)
    pretty_cols = list(template_display.columns)

    print("\nRaw template columns:", raw_cols)
    print("Pretty template columns:", pretty_cols)

    rename_map = {}
    for col in group.columns:
        if col in raw_cols:
            raw_index = raw_cols.index(col)
            rename_map[col] = pretty_cols[raw_index]
        else:
            rename_map[col] = col

    print("Rename map:", rename_map)

    df_display = group.rename(columns=rename_map)
    print("\n--- FINAL DF_DISPLAY (what Streamlit shows) ---")
    print(df_display)
    print("===============================================\n")

    # ----------------------------------------------------
    # Add hyperlinks to date columns in the column headers
    # ----------------------------------------------------

    search_summary = st.text_input(
        "Search player",
        value="",
        key=f"{key_prefix}boss_search",
        placeholder="Type to filter players…",
    )
    if search_summary:
        mask = df_display["Player"].str.contains(
            search_summary, case=False, na=False
        )
        df_display = df_display[mask]

    st.dataframe(
        df_display,
        width="stretch",
    )

    # ------------------------------------------------------------
    # Hyperlinks under the Boss Summary table
    # ------------------------------------------------------------
    # Build a combined list of logs actually used in the summary
    combined_links = []

    for idx in target_indices:
        for entry in tables[idx].get("log_links", []):
            if entry not in combined_links:
                combined_links.append(entry)

    if combined_links:
        st.markdown("**Logs used for this summary:**")
        for entry in combined_links:
            label = entry["label"]
            code = entry["report_code"]
            url = f"https://www.warcraftlogs.com/reports/{code}"
            st.markdown(f"- **{label}** — [Open log]({url})")

    # Build a CSV just like in single mode
    csv_buffer = []
    csv_header = [boss_labels[boss_pos], "Boss summary (all abilities)"]
    csv_buffer.append(",".join(csv_header))
    csv_buffer.append(",".join(str(c) for c in df_display.columns))
    for row in df_display.itertuples(index=False, name=None):
        csv_buffer.append(",".join(str(v) for v in row))
    csv_bytes = ("\n".join(csv_buffer)).encode("utf-8")

    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="warcraftlogs_aggregation_boss_summary.csv",
        mime="text/csv",
        key=f"{key_prefix}boss_download",
    )
