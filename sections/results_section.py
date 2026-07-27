from __future__ import annotations

from typing import Dict, List, Any

import pandas as pd
import streamlit as st

from src import boss_config


def _get_ability_name(ability_id: int, raid_file: str) -> str:
    """Get ability name from the current raid configuration."""
    ability_names = boss_config.get_ability_names(raid_file)
    return ability_names.get(ability_id, "Unknown")


def _target_label(target: dict, raid_file: str) -> str:
    """Pretty label like 'Nexus-King Salhadaar — 1227472 (Besiege)' or 'All abilities'."""
    boss_name = target.get("boss_name", "Unknown boss")
    ability_id = target.get("ability_id")

    if ability_id is None:
        return f"{boss_name} — All abilities"

    ability_name = _get_ability_name(ability_id, raid_file)
    if ability_name and ability_name != "Unknown":
        return f"{boss_name} — {ability_id} ({ability_name})"
    return f"{boss_name} — {ability_id}"


def render_results(
    tables: Dict[int, Dict[str, Any]],
    targets: List[Dict[str, Any]],
    num_reports: int,
    num_players: int,
    boss_to_targets: Dict[int, List[int]],
    metric_is_deaths: bool,
    raid_file: str,
    *,
    key_prefix: str = "",
    section_title: str | None = None,
) -> None:
    """
    Render the results UI:

      • Single ability view (one table at a time, chosen via dropdown)
      • Boss summary view (sum multiple abilities per boss)
    """
    if not tables:
        st.warning("No data available to display.")
        return

    # Total rows across all tables (for the green banner)
    total_rows = sum(len(t["df"]) for t in tables.values())

    # Section header (in practice the expander already has the title)
    if section_title:
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
        # All target indices that actually have data
        available_indices = sorted(tables.keys())
        if not available_indices:
            st.warning("No tables to show.")
            return

        option_labels = [
            _target_label(targets[idx], raid_file) for idx in available_indices
        ]

        selected_idx_pos = st.selectbox(
            "Boss / ability to inspect",
            options=list(range(len(available_indices))),
            format_func=lambda i: option_labels[i],
            key=f"{key_prefix}single_select",
        )
        target_index = available_indices[selected_idx_pos]
        target = targets[target_index]
        data = tables[target_index]

        st.markdown(f"**Selected:** {_target_label(target, raid_file)}")

        # Player search
        search = st.text_input(
            "Search player",
            value="",
            key=f"{key_prefix}single_search",
            placeholder="Type to filter players…",
        )

        df_display: pd.DataFrame = data["df_display"]
        if search:
            mask = df_display["Player"].str.contains(
                search, case=False, na=False
            )
            df_display = df_display[mask]

        # Show a 1-based row number instead of the default 0-based index.
        df_display = df_display.reset_index(drop=True)
        df_display.index = range(1, len(df_display) + 1)

        st.dataframe(
            df_display,
            use_container_width=True,
        )

        st.download_button(
            "Download CSV",
            data=data["csv_bytes"],
            file_name="warcraftlogs_aggregation.csv",
            mime="text/csv",
            key=f"{key_prefix}single_download",
        )

        # Per-table log links
        log_links = data.get("log_links", [])
        if log_links:
            st.markdown("**Logs used for this table:**")
            for entry in log_links:
                label = entry["label"]
                code = entry["report_code"]
                url = f"https://www.warcraftlogs.com/reports/{code}"
                st.markdown(f"- **{label}** — [Open log]({url})")

        # Done for single ability view
        return

    # ------------------------------------------------------------------ #
    # Boss summary view (sum multiple abilities per boss)
    # ------------------------------------------------------------------ #
    # Build list of bosses that actually have tables
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

    # Collect all underlying internal DataFrames for this boss
    raw_dfs = [tables[i]["df"] for i in target_indices]
    merged = pd.concat(raw_dfs, axis=0, ignore_index=True)

    # Everything except "Player" should be numeric; coerce as needed
    value_cols = [c for c in merged.columns if c != "Player"]
    merged_numeric = merged.copy()
    for c in value_cols:
        merged_numeric[c] = pd.to_numeric(
            merged_numeric[c],
            errors="coerce",
        ).fillna(0)

    # Sum across abilities per player
    grouped = merged_numeric.groupby("Player", as_index=False)[value_cols].sum()

    # Map internal column names -> pretty names using first table as template
    template = tables[target_indices[0]]
    raw_cols = list(template["df"].columns)
    pretty_cols = list(template["df_display"].columns)

    rename_map = {}
    for col in grouped.columns:
        if col in raw_cols:
            idx = raw_cols.index(col)
            rename_map[col] = pretty_cols[idx]
        else:
            # New columns (if any) keep their internal name
            rename_map[col] = col

    df_display = grouped.rename(columns=rename_map)

    # Embed each player's pull (attendance) count into the display name.
    # Pulls are per-boss, so they're identical across this boss's ability
    # tables; merge them (max is a safe combiner) and format the name.
    combined_pulls: Dict[str, int] = {}
    for i in target_indices:
        for name, n in tables[i].get("player_pulls", {}).items():
            combined_pulls[name] = max(combined_pulls.get(name, 0), n)
    df_display["Player"] = df_display["Player"].map(
        lambda name: f"{name} (Pulls: {combined_pulls.get(name, 0)})"
    )

    # Search within boss summary
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

    # Show a 1-based row number instead of the default 0-based index.
    df_display = df_display.reset_index(drop=True)
    df_display.index = range(1, len(df_display) + 1)

    st.dataframe(
        df_display,
        use_container_width=True,
    )

    # ------------------------------------------------------------
    # Hyperlinks under the Boss Summary table
    # ------------------------------------------------------------
    combined_links: List[dict] = []
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

    # Build CSV for boss summary
    csv_buffer: List[str] = []
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
