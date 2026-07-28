from __future__ import annotations

import html as _html
from typing import Dict, List, Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src import boss_config
from src.class_colors import class_color

# Outline effect on class-colored text. Stacking -webkit-text-stroke *and*
# a 4-direction text-shadow (the previous approach) double-outlines every
# glyph edge and reads as blurry, especially at small font sizes. A single
# stroke, painted behind the fill (paint-order) so it doesn't eat into the
# glyph shapes, stays crisp — -webkit-text-stroke has solid support across
# Chrome/Edge/Safari/Firefox now, so no text-shadow fallback is needed.
_TEXT_OUTLINE_CSS = (
    "font-weight: 700;"
    "-webkit-font-smoothing: antialiased;"
    "text-rendering: optimizeLegibility;"
    "paint-order: stroke fill;"
    "-webkit-text-stroke: 0.5px rgba(0,0,0,0.85);"
)


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


def _render_class_colored_table(df_display: pd.DataFrame, *, max_height: int = 480) -> None:
    """
    Render df_display as a sortable HTML table.

    st.dataframe's Styler support can only do background-color/color (no
    text-stroke outline, no nth-child zebra striping), so this renders a
    real <table> instead — via st.components.v1.html (not st.markdown,
    since st.markdown never executes <script> tags) so clicking a column
    header can sort the rows, similar to st.dataframe's built-in sort:

      - "Class" and "Player" cell text colored by class, with a dark outline
        so light class colors (Priest white, Rogue yellow, ...) stay legible.
      - Odd rows get a faint grey tint; even rows keep the normal background.
      - Click a header to sort by that column; click again to reverse.
    """
    df_display = df_display.reset_index(drop=True)
    columns = list(df_display.columns)
    numeric_cols = {c for c in columns if c not in ("Class", "Player")}

    header_html = "".join(
        f'<th data-type="{"num" if c in numeric_cols else "str"}">'
        f"{_html.escape(str(c))}</th>"
        for c in columns
    )

    row_html_parts: List[str] = []
    for _, row in df_display.iterrows():
        class_name = str(row.get("Class", "")).split(" (")[0]
        color = class_color(class_name)
        cell_parts = []
        for col in columns:
            text = _html.escape(str(row[col]))
            if col in ("Class", "Player") and color:
                style = f"color:{color};{_TEXT_OUTLINE_CSS}"
                cell_parts.append(f'<td style="{style}">{text}</td>')
            else:
                cell_parts.append(f"<td>{text}</td>")
        row_html_parts.append(f"<tr>{''.join(cell_parts)}</tr>")

    row_h = 28
    header_h = round(row_h * 1.5)  # header is at most 1.5x a data row's height

    num_rows = len(df_display)
    component_height = min(max_height, header_h + row_h * max(num_rows, 1) + 4)

    doc_html = f"""
    <!doctype html>
    <html>
    <head>
    <style>
      :root {{ color-scheme: light dark; }}
      body {{
        margin: 0;
        background: transparent;
        color: #31333F;
        font-family: "Source Sans Pro", -apple-system, BlinkMacSystemFont,
          "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      }}
      @media (prefers-color-scheme: dark) {{
        body {{ color: #fafafa; }}
      }}
      .wcl-table-wrap {{
        max-height: {max_height}px;
        overflow: auto;
        border: 1px solid rgba(128,128,128,0.35);
        border-radius: 6px;
      }}
      .wcl-table {{
        border-collapse: collapse;
        width: 100%;
        font-size: 0.85rem;
      }}
      .wcl-table th, .wcl-table td {{
        box-sizing: border-box;
        vertical-align: middle;
      }}
      .wcl-table th {{
        position: sticky;
        top: 0;
        height: {header_h}px;
        text-align: left;
        padding: 0 12px;
        background-color: #e6e6e6;
        border-bottom: 1px solid rgba(128,128,128,0.4);
        z-index: 1;
        cursor: pointer;
        user-select: none;
        white-space: nowrap;
      }}
      .wcl-table th:hover {{
        background-color: #d5d5d5;
      }}
      @media (prefers-color-scheme: dark) {{
        .wcl-table th {{ background-color: #3a3a3a; }}
        .wcl-table th:hover {{ background-color: #484848; }}
      }}
      .wcl-table th[data-sort="asc"]::after {{ content: " \\25B2"; }}
      .wcl-table th[data-sort="desc"]::after {{ content: " \\25BC"; }}
      .wcl-table td {{
        height: {row_h}px;
        padding: 0 12px;
        white-space: normal;
        overflow-wrap: anywhere;
      }}
      .wcl-table tbody tr:nth-child(odd) td {{
        background-color: rgba(128,128,128,0.12);
      }}
    </style>
    </head>
    <body>
      <div class="wcl-table-wrap">
        <table class="wcl-table" id="wcl-table">
          <thead><tr>{header_html}</tr></thead>
          <tbody>{''.join(row_html_parts)}</tbody>
        </table>
      </div>
      <script>
        (function () {{
          var table = document.getElementById('wcl-table');
          var tbody = table.tBodies[0];
          var headers = table.tHead.rows[0].cells;
          var sortState = {{ idx: -1, dir: 1 }};

          function cellValue(row, idx, type) {{
            var text = row.cells[idx].innerText.trim();
            if (type === 'num') {{
              var n = parseFloat(text.replace(/,/g, ''));
              return isNaN(n) ? -Infinity : n;
            }}
            return text.toLowerCase();
          }}

          Array.prototype.forEach.call(headers, function (th, idx) {{
            th.addEventListener('click', function () {{
              var dir = (sortState.idx === idx) ? -sortState.dir : 1;
              sortState = {{ idx: idx, dir: dir }};

              var type = th.getAttribute('data-type');
              var rows = Array.prototype.slice.call(tbody.rows);
              rows.sort(function (a, b) {{
                var va = cellValue(a, idx, type);
                var vb = cellValue(b, idx, type);
                if (va < vb) return -1 * dir;
                if (va > vb) return 1 * dir;
                return 0;
              }});
              rows.forEach(function (r) {{ tbody.appendChild(r); }});

              Array.prototype.forEach.call(headers, function (h) {{
                h.removeAttribute('data-sort');
              }});
              th.setAttribute('data-sort', dir === 1 ? 'asc' : 'desc');
            }});
          }});
        }})();
      </script>
    </body>
    </html>
    """
    components.html(doc_html, height=component_height, scrolling=False)


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
    compact: bool = False,
) -> None:
    """
    Render the results UI:

      • Single ability view (one table at a time, chosen via dropdown)
      • Boss summary view (sum multiple abilities per boss)

    compact=True (used for the side-by-side wide-mode layout) trims each
    table down to just Class / Player / Total column(s), dropping the
    per-date/log breakdown columns and the "logs used" link list — there
    isn't room for that detail when both metrics are shown side by side.
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

        if compact:
            summary_col_count = data.get("summary_col_count", len(df_display.columns))
            df_display = df_display.iloc[:, :summary_col_count]

        _render_class_colored_table(df_display)

        st.download_button(
            "Download CSV",
            data=data["csv_bytes"],
            file_name="warcraftlogs_aggregation.csv",
            mime="text/csv",
            key=f"{key_prefix}single_download",
        )

        # Per-table log links — skipped in compact mode, no room for the
        # per-log breakdown when both metrics are shown side by side.
        log_links = data.get("log_links", [])
        if log_links and not compact:
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

    # Everything except "Player" and "Class" should be numeric; coerce as needed
    value_cols = [c for c in merged.columns if c not in ("Player", "Class")]
    merged_numeric = merged.copy()
    for c in value_cols:
        merged_numeric[c] = pd.to_numeric(
            merged_numeric[c],
            errors="coerce",
        ).fillna(0)

    # Sum across abilities per player
    grouped = merged_numeric.groupby("Player", as_index=False)[value_cols].sum()

    # Class doesn't vary by ability, so reattach it (rather than summing it),
    # and move it to the front to match the per-ability table layout.
    if "Class" in merged.columns:
        class_by_player = (
            merged.dropna(subset=["Class"])
            .drop_duplicates(subset=["Player"])
            .set_index("Player")["Class"]
            .to_dict()
        )
        grouped["Class"] = grouped["Player"].map(
            lambda p: class_by_player.get(p, "Unknown")
        )
        grouped = grouped[["Class"] + [c for c in grouped.columns if c != "Class"]]

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

    # Trim to a display-only copy in compact mode; the CSV export (built
    # below from df_display) keeps the full per-date breakdown regardless.
    render_df = df_display
    if compact:
        summary_col_count = template.get("summary_col_count", len(df_display.columns))
        render_df = df_display.iloc[:, :summary_col_count]

    _render_class_colored_table(render_df)

    # ------------------------------------------------------------
    # Hyperlinks under the Boss Summary table — skipped in compact mode.
    # ------------------------------------------------------------
    combined_links: List[dict] = []
    if not compact:
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
