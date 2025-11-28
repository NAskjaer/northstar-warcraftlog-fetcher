from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Tuple

import streamlit as st


# --- ABILITY NAME LOOKUP ------------------------------------------------------
ABILITY_NAMES: Dict[int, str] = {
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
BOSS_OPTIONS: Dict[str, Dict[str, Any]] = {
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

DIFFICULTY: int = 5  # always Mythic


def _init_boss_blocks() -> None:
    """Initialise the boss_blocks structure in session_state if needed."""
    if "boss_blocks" not in st.session_state:
        boss_names = list(BOSS_OPTIONS.keys())
        default_boss_name = (
            "Nexus-King Salhadaar"
            if "Nexus-King Salhadaar" in BOSS_OPTIONS
            else boss_names[0]
        )
        st.session_state["boss_blocks"] = [
            {
                "id": 0,
                "boss_name": default_boss_name,
                "selected_abilities": [],
            }
        ]
        st.session_state["next_boss_block_id"] = 1


def _build_targets_from_blocks(
    boss_blocks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert boss_blocks into a flat list of targets for analysis.

    Each target is a dict with: boss_name, boss_id, ability_id (or None for all).
    """
    targets: List[Dict[str, Any]] = []
    for block in boss_blocks:
        boss_name = block["boss_name"]
        boss_info = BOSS_OPTIONS[boss_name]
        selected_abilities = block.get("selected_abilities", [])

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

def render_input_settings() -> Tuple[str, datetime.date, datetime.date, List[Dict[str, Any]], int | None, bool]:
    """Render the '2. Input settings' section and return user choices.

    Returns:
        guild_url: The raw guild URL string.
        start_date: Python date object for the start of the range.
        end_date: Python date object for the end of the range.
        targets: List of dicts with boss_name, boss_id, ability_id (or None).
        int: Death cutoff after N player deaths (None = disabled).
        submitted: Whether the user clicked "Generate CSV".
    """
    _init_boss_blocks()

    with st.expander("2. Input settings", expanded=True):
        st.markdown("Configure which logs to analyze and how to group deaths.")

        col_url, col_ignore = st.columns([2, 1])

        with col_url:
            guild_url = st.text_input(
                "Guild URL",
                placeholder="https://www.warcraftlogs.com/guild/id/260153",
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

        today = datetime.now(timezone.utc).date()
        default_start = today - timedelta(days=7)
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

        boss_blocks = st.session_state["boss_blocks"]

        # --- helper used by Remove boss button ------------------------------
        def _remove_boss(block_id: int) -> None:
            st.session_state["boss_blocks"] = [
                b for b in st.session_state["boss_blocks"] if b["id"] != block_id
            ]

        # --- Render each boss block ----------------------------------------
        for block in boss_blocks:
            st.markdown("---")

            # Shared label so the select and button line up nicely
            st.markdown("**Boss**")

            cols = st.columns([3, 1])

            # ---- Boss select ------------------------------------------------
            with cols[0]:
                all_boss_names = list(BOSS_OPTIONS.keys())

                # Bosses already chosen in OTHER blocks
                used_by_others = {
                    b.get("boss_name")
                    for b in boss_blocks
                    if b["id"] != block["id"] and b.get("boss_name") in BOSS_OPTIONS
                }

                # Current boss for this block (keep it selectable even if used)
                current_boss_name = block.get("boss_name")
                if current_boss_name not in all_boss_names:
                    current_boss_name = all_boss_names[0]

                # Available bosses = all minus “used by others”, but always
                # include this block's current boss so the dropdown doesn’t break
                available_boss_names = [
                    name
                    for name in all_boss_names
                    if name not in used_by_others or name == current_boss_name
                ]

                # Safety: if somehow nothing left, fall back to all bosses
                if not available_boss_names:
                    available_boss_names = all_boss_names

                if current_boss_name not in available_boss_names:
                    current_boss_name = available_boss_names[0]

                boss_index = available_boss_names.index(current_boss_name)
                boss_name = st.selectbox(
                    "Boss",
                    available_boss_names,
                    index=boss_index,
                    key=f"boss_{block['id']}",
                    label_visibility="collapsed",
                    help=(
                        "Choose the boss to analyze. "
                        "Only logs containing this boss are processed."
                    ),
                )


            # ---- Remove boss button (aligned with select) ------------------
            with cols[1]:
                if len(st.session_state["boss_blocks"]) > 1:
                    st.button(
                        "Remove boss",
                        key=f"remove_block_{block['id']}",
                        on_click=_remove_boss,
                        args=(block["id"],),
                        use_container_width=True,
                    )

            boss_info = BOSS_OPTIONS[boss_name]
            boss_ability_ids = boss_info["abilities"]

            # Valid previously selected abilities for this boss
            prev_selected_ids = [
                ability_id
                for ability_id in block.get("selected_abilities", [])
                if ability_id in boss_ability_ids
            ]

            # Multiselect options and defaults (labels include ID + name)
            ability_options = [
                f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
                for ability_id in boss_ability_ids
            ]
            default_labels = [
                f"{ability_id} ({ABILITY_NAMES.get(ability_id, 'Unknown')})"
                for ability_id in prev_selected_ids
            ]

            selected_labels = st.multiselect(
                "Abilities to track (leave empty for all abilities)",
                ability_options,
                default=default_labels,
                key=f"abilities_{block['id']}",
                help=(
                    "Choose one or more abilities for this boss. "
                    "If none are selected, all deaths for this boss are counted."
                ),
            )

            # Convert selected labels back to numeric IDs
            selected_ids: list[int] = []
            for label in selected_labels:
                try:
                    ability_id = int(label.split(" ", 1)[0])
                except ValueError:
                    continue
                if ability_id in boss_ability_ids:
                    selected_ids.append(ability_id)

            # Write the latest UI choices back into the block in-place
            block["boss_name"] = boss_name
            block["selected_abilities"] = selected_ids
            
        # ---- Add another boss (single-click, same pattern as before) -------
        def _add_boss() -> None:
            next_id = st.session_state["next_boss_block_id"]
            all_boss_names = list(BOSS_OPTIONS.keys())

            # Bosses already used in existing blocks
            used_bosses = {
                b.get("boss_name")
                for b in st.session_state["boss_blocks"]
                if b.get("boss_name") in BOSS_OPTIONS
            }

            # Pick the first boss that isn't used yet, or fall back to first
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


        st.button("Add another boss", key="add_boss", on_click=_add_boss)

        left, center, right = st.columns([4, 2, 4])
        with center:
            submitted = st.button(
                "Generate CSV",
                key="generate_csv",
                use_container_width=True,
            )

    targets = _build_targets_from_blocks(st.session_state["boss_blocks"])

    # 0 or empty should behave as "null" – i.e. no cutoff.
    if ignore_after_player_deaths_raw and ignore_after_player_deaths_raw > 0:
        ignore_after_player_deaths: int | None = int(ignore_after_player_deaths_raw)
    else:
        ignore_after_player_deaths = None


    return guild_url, start_date, end_date, targets, ignore_after_player_deaths, submitted

