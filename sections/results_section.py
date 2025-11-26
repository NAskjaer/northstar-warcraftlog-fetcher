from __future__ import annotations

import re
import streamlit as st
import pandas as pd

from sections.input_settings import ABILITY_NAMES


def render_results(
    tables: dict[int, dict[str, object]],
    targets: list[dict],
    num_reports: int,
    num_players: int,
) -> None:
    """Render the results section: summary, view mode, table output."""

    st.markdown("### 3. Results")

    if not tables:
        st.info("No results to display yet.")
        return

    st.success(
        f"Found {num_reports} report-day entries across {num_players} players."
    )

    # View mode selector
    VIEW_OPTIONS = {
        "single": "Single ability view",
        "boss_summary": "Boss summary (sum multiple abilities)",
    }

    view_mode = st.radio(
        "View mode",
        options=list(VIEW_OPTIONS.keys()),
        format_func=lambda k: VIEW_OPTIONS[k],
        horizontal=True,
        key="results_view_mode",
    )

    # Pull mapping boss_id -> target indices from cache
    cache = st.session_state.get("analysis_cache", {})
    boss_to_targets = cache.get("boss_to_targets", {})

    # ---------------------------------------------------------
    # MODE 1: SINGLE ABILITY VIEW (original functionality)
    # ---------------------------------------------------------
    if view_mode == "single":

        target_indices = sorted(tables.keys())

        def format_target(idx: int) -> str:
            tgt = targets[idx]
            boss = tgt["boss_name"]
            ability = tgt["ability_id"]
            if ability is None:
                return f"{boss} – All abilities"
            return f"{boss} – {ability} ({ABILITY_NAMES.get(ability, 'Unknown')})"

        selected_index = st.selectbox(
            "Boss / ability to inspect",
            options=target_indices,
            format_func=format_target,
            key="results_select_single",
        )

        table = tables[selected_index]
        df: pd.DataFrame = table["df"]        # type: ignore
        csv_bytes: bytes = table["csv_bytes"] # type: ignore

        tgt = targets[selected_index]
        boss_name = tgt["boss_name"]
        ability_id = tgt["ability_id"]

        # Label building
        if ability_id is None:
            ability_label = "All abilities"
            ability_slug = "all"
        else:
            ability_label = f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
            ability_slug = str(ability_id)

        boss_slug = re.sub(r"[^a-z0-9]+", "-", boss_name.lower()).strip("-")
        file_name = f"deaths_{boss_slug}_{ability_slug}.csv"

        st.markdown(f"**Selected:** {boss_name} — {ability_label}")

        # Search + Download columns
        col_search, col_dl = st.columns([4, 1])

        search_query = col_search.text_input(
            "Search player",
            value="",
            placeholder="Type to filter players…",
            key=f"search_{selected_index}",
        )

        # Download button
        with col_dl:
            st.write("")  # align vertically
            st.write("")
            st.download_button(
                "Download CSV",
                data=csv_bytes,
                file_name=file_name,
                mime="text/csv",
                use_container_width=True,
            )

        # Filter
        if search_query:
            df_display = df[df["Player"].str.contains(search_query, case=False, na=False)]
        else:
            df_display = df

        # Widen table
        st.dataframe(df_display, use_container_width=True, height=600)
        return

    # ---------------------------------------------------------
    # MODE 2: BOSS SUMMARY (combine all abilities)
    # ---------------------------------------------------------
    elif view_mode == "boss_summary":

        # Build list of bosses the user selected
        boss_options = {}
        for tgt in targets:
            boss_options[tgt["boss_id"]] = tgt["boss_name"]

        boss_ids = sorted(boss_options.keys(), key=lambda bid: boss_options[bid])

        selected_boss_id = st.selectbox(
            "Select boss",
            options=boss_ids,
            format_func=lambda bid: boss_options[bid],
            key="summary_boss_select",
        )

        st.markdown(f"**Boss summary:** {boss_options[selected_boss_id]}")

        # All target indices belonging to this boss
        target_indices = boss_to_targets.get(selected_boss_id, [])

        if not target_indices:
            st.warning("This boss has no tracked abilities in the current selection.")
            return

        ability_cols = []
        dfs = []

        # Combine totals from each ability
        for idx in target_indices:
            tgt = targets[idx]

            ability_id = tgt["ability_id"]
            if ability_id is None:
                label = "All abilities"
            else:
                label = f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"

            df = tables[idx]["df"]  # contains Total Deaths column

            # Extract only "Player" + "Total Deaths", renaming column to ability name
            sub = df[["Player", "Total Deaths"]].rename(columns={"Total Deaths": label})
            dfs.append(sub)
            ability_cols.append(label)

        # Merge all ability totals on Player
        from functools import reduce
        summary_df = reduce(
            lambda left, right: pd.merge(left, right, on="Player", how="outer"),
            dfs,
        ).fillna(0)

        # Add overall total column
        summary_df["Total (all abilities)"] = summary_df[ability_cols].sum(axis=1)

        # Sort by total descending
        summary_df = summary_df.sort_values("Total (all abilities)", ascending=False)

        # Widen display
        st.dataframe(summary_df, use_container_width=True, height=600)

        # Download CSV
        boss_slug = re.sub(r"[^a-z0-9]+", "-", boss_options[selected_boss_id].lower())
        csv = summary_df.to_csv(index=False).encode("utf-8")

        st.download_button(
            "Download Boss Summary CSV",
            data=csv,
            file_name=f"summary_{boss_slug}.csv",
            mime="text/csv",
            use_container_width=True,
        )
