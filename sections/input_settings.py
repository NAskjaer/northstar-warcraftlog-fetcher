from __future__ import annotations

from datetime import datetime, timedelta, timezone, date as date_cls
from typing import List, Dict, Any, Tuple

import streamlit as st

from src import boss_config

DIFFICULTY: int = 5  # always Mythic

# Available raid files
RAID_FILES = {
    "Omega Manaforge": "Manaforge_Omega.json",
    "Midnight Season 1": "Midnight_season_1.json"
}


def _get_current_raid_file() -> str:
    """Get the currently selected raid file."""
    if "selected_raid_file" not in st.session_state:
        st.session_state["selected_raid_file"] = "Midnight_season_1.json"
    return st.session_state["selected_raid_file"]


def _reload_boss_config():
    """Reload boss configuration from the currently selected raid file."""
    raid_file = _get_current_raid_file()
    return boss_config.get_ability_names(raid_file), boss_config.get_boss_options(raid_file)


def _init_boss_blocks() -> None:
    """Initialise the boss_blocks structure in session_state if needed."""
    if "boss_blocks" not in st.session_state:
        _, boss_options = _reload_boss_config()
        boss_names = list(boss_options.keys())
        if not boss_names:
            st.session_state["boss_blocks"] = []
            st.session_state["next_boss_block_id"] = 0
            return

        # Prefer "Midnight Falls" as the default boss (fewer clicks for the
        # common case); fall back to the first boss for other raids.
        default_boss = "Midnight Falls" if "Midnight Falls" in boss_names else boss_names[0]

        st.session_state["boss_blocks"] = [
            {
                "id": 0,
                "boss_name": default_boss,
                "selected_abilities": [],
            }
        ]
        st.session_state["next_boss_block_id"] = 1


def _build_targets_from_blocks(
    boss_blocks: List[Dict[str, Any]],
    boss_options: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Build the list of (boss, ability) targets from the UI blocks."""
    targets: List[Dict[str, Any]] = []

    for block in boss_blocks:
        boss_name = block.get("boss_name")
        if not boss_name or boss_name not in boss_options:
            continue

        boss_info = boss_options[boss_name]
        selected_abilities = block.get("selected_abilities") or []

        if selected_abilities:
            for ability_id in selected_abilities:
                targets.append(
                    {
                        "boss_name": boss_name,
                        "boss_id": boss_info["id"],
                        "ability_id": ability_id,
                    }
                )
        else:
            # No specific abilities selected: track all deaths for this boss
            targets.append(
                {
                    "boss_name": boss_name,
                    "boss_id": boss_info["id"],
                    "ability_id": None,
                }
            )

    return targets


def render_input_settings() -> Tuple[
    str,
    date_cls,
    date_cls,
    List[Dict[str, Any]],
    int | None,
    bool,
]:
    """Render the '2. Input settings' section and return user choices."""

    with st.expander("2. Input settings", expanded=True):
        st.markdown("Configure which logs to analyze and how to group deaths.")

        # ------------------------------------------------------------------
        # Raid selection dropdown
        # ------------------------------------------------------------------
        st.markdown("**Select Raid**")
        
        # Get current selection display name
        current_file = _get_current_raid_file()
        current_display = next(
            (name for name, file in RAID_FILES.items() if file == current_file),
            list(RAID_FILES.keys())[0]
        )
        
        raid_selection = st.selectbox(
            "Raid",
            list(RAID_FILES.keys()),
            index=list(RAID_FILES.keys()).index(current_display),
            key="raid_selector",
            label_visibility="collapsed",
            help="Select which raid configuration to use"
        )
        
        # Update selected raid file if changed
        new_raid_file = RAID_FILES[raid_selection]
        if new_raid_file != st.session_state.get("selected_raid_file"):
            st.session_state["selected_raid_file"] = new_raid_file
            # Clear boss blocks when switching raids
            st.session_state.pop("boss_blocks", None)
            st.session_state.pop("next_boss_block_id", None)
            st.rerun()
        
        # Reload configuration for current raid
        ability_names, boss_options = _reload_boss_config()
        
        _init_boss_blocks()

        st.markdown("---")

        # ------------------------------------------------------------------
        # Guild URL + ignore-after-deaths
        # ------------------------------------------------------------------
        col_url, col_ignore = st.columns([2, 1])

        with col_url:
            guild_url = st.text_input(
                "Guild URL",
                placeholder="https://www.warcraftlogs.com/guild/id/235490",
                help="Full Warcraft Logs guild URL. The app extracts the guild ID from this.",
            )

        with col_ignore:
            ignore_after_player_deaths_raw = st.number_input(
                "Ignore events after player deaths",
                min_value=0,
                step=1,
                value=0,
                help=(
                    "Matches Warcraft Logs' 'Ignore Events After Player Deaths' option. "
                    "If set to N > 0, events after the Nth player death in a pull "
                    "are ignored. If set to 0, all events are counted."
                ),
                key="ignore_after_player_deaths",
            )

        # ------------------------------------------------------------------
        # Date range
        # ------------------------------------------------------------------
        today = datetime.now(timezone.utc).date()
        # Default to ~6 months back, roughly how long a raid tier lasts.
        month = today.month - 6
        year = today.year
        if month <= 0:
            month += 12
            year -= 1
        try:
            default_start = today.replace(year=year, month=month)
        except ValueError:
            # Day-of-month doesn't exist in the target month (e.g. 31st).
            default_start = today.replace(year=year, month=month, day=1)
        default_end = today

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input(
                "Start date",
                default_start,
                help="Only include logs on or after this date.",
            )
        with col2:
            end_date = st.date_input(
                "End date",
                default_end,
                help="Only include logs on or before this date.",
            )

        # ------------------------------------------------------------------
        # Boss blocks
        # ------------------------------------------------------------------
        boss_blocks = st.session_state.get("boss_blocks", [])

        def _remove_boss(block_id: int) -> None:
            st.session_state["boss_blocks"] = [
                b for b in st.session_state["boss_blocks"] if b["id"] != block_id
            ]

        for block in boss_blocks:
            st.markdown("---")

            st.markdown("**Boss**")
            cols = st.columns([3, 1])

            with cols[0]:
                all_boss_names = list(boss_options.keys())
                if not all_boss_names:
                    st.error(f"No bosses configured in {_get_current_raid_file()}.")
                    continue

                used_by_others = {
                    b.get("boss_name")
                    for b in boss_blocks
                    if b["id"] != block["id"] and b.get("boss_name") in boss_options
                }

                current_boss_name = block.get("boss_name")
                if current_boss_name not in all_boss_names:
                    current_boss_name = all_boss_names[0]

                available_boss_names = [
                    name
                    for name in all_boss_names
                    if name not in used_by_others or name == current_boss_name
                ] or all_boss_names

                if current_boss_name not in available_boss_names:
                    current_boss_name = available_boss_names[0]

                boss_index = available_boss_names.index(current_boss_name)
                boss_name = st.selectbox(
                    "Boss",
                    available_boss_names,
                    index=boss_index,
                    key=f"boss_{block['id']}",
                    label_visibility="collapsed",
                )

            with cols[1]:
                if len(boss_blocks) > 1:
                    st.button(
                        "Remove boss",
                        key=f"remove_block_{block['id']}",
                        on_click=_remove_boss,
                        args=(block["id"],),
                        use_container_width=True,
                    )

            boss_info = boss_options[boss_name]
            boss_ability_ids = boss_info["abilities"]

            # If this boss has zero abilities, show a message and skip multiselect
            if not boss_ability_ids:
                st.info(
                    f"There are currently no abilities configured for **{boss_name}**.\n\n"
                    "You can add them using **Add / edit abilities** below."
                )
                block["boss_name"] = boss_name
                block["selected_abilities"] = []
                continue

            prev_selected_ids = [
                ability_id
                for ability_id in block.get("selected_abilities", [])
                if ability_id in boss_ability_ids
            ]

            ability_options = [
                f"{ability_id} ({ability_names.get(ability_id, 'Unknown')})"
                for ability_id in boss_ability_ids
            ]
            default_labels = [
                f"{ability_id} ({ability_names.get(ability_id, 'Unknown')})"
                for ability_id in prev_selected_ids
            ]

            state_key = f"abilities_{block['id']}"

            # If the widget has stale state that doesn't match any option, drop it.
            if state_key in st.session_state:
                current_val = st.session_state[state_key]
                if isinstance(current_val, (list, tuple)):
                    if not any(v in ability_options for v in current_val):
                        st.session_state.pop(state_key, None)
                else:
                    st.session_state.pop(state_key, None)

            selected_labels = st.multiselect(
                "Abilities to track (leave empty for all abilities)",
                ability_options,
                default=default_labels,
                key=state_key,
                help=(
                    "Choose one or more abilities for this boss. "
                    "If none are selected, all deaths for this boss are counted."
                ),
            )

            # Convert labels back to numeric IDs
            selected_ids: List[int] = []
            for label in selected_labels:
                try:
                    ability_id = int(label.split(" ", 1)[0])
                except ValueError:
                    continue
                if ability_id in boss_ability_ids:
                    selected_ids.append(ability_id)

            block["boss_name"] = boss_name
            block["selected_abilities"] = selected_ids

        # ------------------------------------------------------------------
        # Controls: add boss + toggle add-ability form
        # ------------------------------------------------------------------
        def _add_boss() -> None:
            if "next_boss_block_id" not in st.session_state:
                existing_ids = [
                    b.get("id", 0) for b in st.session_state.get("boss_blocks", [])
                ]
                next_id = max(existing_ids, default=-1) + 1
                st.session_state["next_boss_block_id"] = next_id
            else:
                next_id = st.session_state["next_boss_block_id"]

            all_boss_names = list(boss_options.keys())
            if not all_boss_names:
                return

            used_bosses = {
                b.get("boss_name")
                for b in st.session_state["boss_blocks"]
                if b.get("boss_name") in boss_options
            }

            default_boss_name = next(
                (name for name in all_boss_names if name not in used_bosses),
                all_boss_names[0],
            )

            st.session_state["boss_blocks"].append(
                {
                    "id": next_id,
                    "boss_name": default_boss_name,
                    "selected_abilities": [],
                }
            )
            st.session_state["next_boss_block_id"] = next_id + 1

        controls_left, controls_right = st.columns([1, 1])
        with controls_left:
            st.button(
                "Add another boss",
                key="add_boss",
                on_click=_add_boss,
                use_container_width=True,
            )

        with controls_right:
            if st.button(
                "Add Ability",
                key="toggle_add_ability",
                use_container_width=True,
            ):
                st.session_state["show_add_ability_form"] = not st.session_state.get(
                    "show_add_ability_form", False
                )

        # ------------------------------------------------------------------
        # Inline "Add ability to boss configuration" form
        # ------------------------------------------------------------------
        if st.session_state.get("show_add_ability_form", False):
            st.markdown("---")
            st.markdown("### Add ability to boss configuration")
            st.info(f"Adding to: **{raid_selection}**")

            boss_names = list(boss_options.keys())
            if boss_names:
                selected_boss_for_new = st.selectbox(
                    "Boss",
                    boss_names,
                    key="add_ability_boss",
                )
                boss_info = boss_options[selected_boss_for_new]

                ability_id_str = st.text_input(
                    "Ability ID (numeric, from Warcraft Logs)",
                    key="add_ability_id",
                )

                custom_label = st.text_input(
                    "Label (optional, defaults to WarcraftLogs Ability name if left blank)",
                    key="add_ability_label",
                )

                if st.button("Save ability", key="save_new_ability"):
                    ability_id_str = ability_id_str.strip()
                    if not ability_id_str:
                        st.error("Ability ID is required.")
                    else:
                        try:
                            ability_id = int(ability_id_str)
                        except ValueError:
                            st.error("Ability ID must be a number.")
                        else:
                            # 🔍 Always validate the ID against Warcraft Logs
                            api_name = boss_config.lookup_ability_name(ability_id)
                            if api_name is None:
                                st.error(
                                    f"Ability ID {ability_id} does not exist on Warcraft Logs."
                                )
                            else:
                                # Use custom label if given, otherwise WCL name
                                label = custom_label.strip() or api_name

                                # Persist to the selected raid JSON file
                                raid_file = _get_current_raid_file()
                                boss_config.add_ability(
                                    boss_name=selected_boss_for_new,
                                    boss_id=boss_info["id"],
                                    ability_id=ability_id,
                                    label=label,
                                    raid_file=raid_file,
                                )

                                # Clear any stale ability selections so UI refreshes cleanly
                                for k in list(st.session_state.keys()):
                                    if str(k).startswith("abilities_"):
                                        st.session_state.pop(k)

                                # Hide form and rerun to refresh boss block UI
                                st.session_state["show_add_ability_form"] = False
                                st.success(f"Added ability {ability_id} to {selected_boss_for_new} in {raid_selection}")
                                try:
                                    st.rerun()
                                except AttributeError:
                                    st.experimental_rerun()
            else:
                st.warning(f"No bosses configured in {raid_selection}; cannot add abilities.")

        # ------------------------------------------------------------------
        # Generate button
        # ------------------------------------------------------------------
        left, center, right = st.columns([4, 2, 4])
        with center:
            submitted = st.button(
                "Generate CSV",
                key="generate_csv",
                use_container_width=True,
            )

    targets = _build_targets_from_blocks(st.session_state.get("boss_blocks", []), boss_options)

    # 0 or empty should behave as "null" – i.e. no cutoff.
    if ignore_after_player_deaths_raw and ignore_after_player_deaths_raw > 0:
        ignore_after_player_deaths: int | None = int(ignore_after_player_deaths_raw)
    else:
        ignore_after_player_deaths = None

    return (
        guild_url,
        start_date,
        end_date,
        targets,
        ignore_after_player_deaths,
        submitted,
    )