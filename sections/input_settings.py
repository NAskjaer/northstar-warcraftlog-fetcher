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

def render_input_settings() -> Tuple[str, datetime.date, datetime.date, List[Dict[str, Any]], bool]:
    """Render the '2. Input settings' section and return user choices.

    Returns:
        guild_url: The raw guild URL string.
        start_date: Python date object for the start of the range.
        end_date: Python date object for the end of the range.
        targets: List of targets (boss + ability combos) for analysis.
        submitted: Whether the user clicked "Generate CSV".
    """
    _init_boss_blocks()

    with st.expander("2. Input settings", expanded=True):
        st.markdown("Configure which logs to analyze and how to group deaths.")

        guild_url = st.text_input(
            "Guild URL",
            placeholder="https://www.warcraftlogs.com/guild/id/260153",
            help="Full Warcraft Logs guild URL. The app extracts the guild ID from this.",
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

        # ---- Section separator + centered heading ---------------------------
        # Separator before boss blocks

        boss_blocks = st.session_state["boss_blocks"]

        # --- helper used by Remove boss button ------------------------------
        def _remove_boss(block_id: int) -> None:
            st.session_state["boss_blocks"] = [
                b for b in st.session_state["boss_blocks"] if b["id"] != block_id
            ]

        # --- Render each boss block ----------------------------------------
        for block in boss_blocks:
            st.markdown("---")
            cols = st.columns([3, 1])

            # ---- Boss select ------------------------------------------------
            with cols[0]:
                boss_names = list(BOSS_OPTIONS.keys())
                current_boss_name = block.get("boss_name")
                if current_boss_name not in boss_names:
                    current_boss_name = boss_names[0]

                boss_index = boss_names.index(current_boss_name)
                boss_name = st.selectbox(
                    "Boss",
                    boss_names,
                    index=boss_index,
                    key=f"boss_{block['id']}",
                    help="Choose the boss to analyze. Only logs containing this boss are processed.",
                )

            # ---- Remove boss button (vertically aligned) -------------------
            with cols[1]:
                # Add some vertical space so the button lines up with the middle
                st.write("")
                st.write("")
                if len(st.session_state["boss_blocks"]) > 1:
                    st.button(
                        "Remove boss",
                        key=f"remove_block_{block['id']}",
                        on_click=_remove_boss,
                        args=(block["id"],),
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
            boss_names = list(BOSS_OPTIONS.keys())
            default_boss_name = (
                "Nexus-King Salhadaar"
                if "Nexus-King Salhadaar" in BOSS_OPTIONS
                else boss_names[0]
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

        # ---- Centered Generate CSV button ----------------------------------
        spacer_left, center_col, spacer_right = st.columns([1, 2, 1])
        with center_col:
            submitted = st.button("Generate CSV", key="generate_csv")

    targets = _build_targets_from_blocks(st.session_state["boss_blocks"])

    return guild_url, start_date, end_date, targets, submitted
