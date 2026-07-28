# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local Streamlit app that pulls raid reports from the Warcraft Logs v2 GraphQL API and produces CSV summaries of per-player **deaths** and **damage taken** (Mythic difficulty, wipes only), grouped by boss/ability and date. Two analysis sources: a single guild (by URL) or the top-N world-progress guilds for a boss (from a stored ranking).

## Commands

```bash
# Run the app (installs missing deps from requirements.txt, then launches Streamlit on :8501)
python run_app.py

# Run Streamlit directly (skips the dependency auto-install)
python -m streamlit run ui/app.py

# Install deps manually
pip install -r requirements.txt

# Diagnostic "test" (a standalone script, NOT pytest — verifies boss_config JSON loads)
python test_boss_config.py

# Overnight multi-guild run: pauses/resumes around the hourly points budget
python overnight_run.py --ranks 1-300 --metric both
python overnight_run.py --job output/overnight/<id>/job.json   # resume/relaunch a job
python overnight_run.py --help

# Legacy CLI path: hard-coded single-guild death CSV to output/deaths_summary.csv
python main.py
```

There is no pytest/lint/build tooling. `test_boss_config.py` is the only test and is a print-based diagnostic.

## Credentials

Every API call needs `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` in a `.env` at the project root (gitignored). The app creates this for you via the in-UI "Warcraft Logs API credentials" section (`sections/env_section.py`) — get a private client at https://www.warcraftlogs.com/api/clients/ (Redirect URL `http://localhost`, Public Client unchecked). Without a valid `.env` the UI hard-stops before showing the analyzer.

## Architecture

Layered, with a hard split between the API/data layer (`src/`) and the Streamlit UI (`ui/`, `sections/`):

- **`src/api_client.py`** — the only module that talks to Warcraft Logs. `run_wcl_query()` is the single GraphQL entry point: OAuth token caching, 429 backoff (short, capped waits — the hourly *points* budget can't be waited out in one run), retry-with-backoff on read timeouts/connection errors too (warcraftlogs.com occasionally stalls on a single request without actually being rate-limited), and `truststore.inject_into_ssl()` so corporate/AV TLS interception (private root CAs) doesn't break HTTPS. `get_rate_limit()` exposes the same points budget shown at warcraftlogs.com/profile. **All new API access should go through `run_wcl_query`.**

- **`src/*_fetcher.py`** — thin query wrappers over `run_wcl_query`: `calendar_fetcher` (guild report list), `deaths_fetcher` / `damage_taken_fetcher` (per-player event aggregation for a report + boss + ability), `survivability_fetcher`, `guild_rankings_fetcher` (`get_encounter_zone_id`, live ranking fetch), and `player_details_fetcher` (`get_player_class_specs` — per-report class/spec via WCL's `playerDetails` field, with an `icon`-parsing fallback if `type`/`specs` come back empty).

- **`src/class_colors.py`** — the standard WoW class → hex color map (`class_color()`), used to color the "Class" column text in results tables.

- **`ui/app.py`** — the whole page and all orchestration. Flow: env gate → rate-limit widget → `render_input_settings()` → metric radio (Deaths / Damage taken / Both) → branch on `source_settings["mode"]`:
  - `"single"`: parse guild ID from URL, fan out report×boss jobs across a `ThreadPoolExecutor`, build per-date/per-player tables (each job also fetches per-player class/spec via `get_player_class_specs`, merged into a global `player_class_spec_global` map so a "Class" column can be shown), render via `sections/results_section.py`.
  - `"ranking"`: render a live points/time estimate for the chosen rank range (from `src/estimate.py`, using the *last* cached rate-limit snapshot so dragging the range costs nothing), offer the overnight launcher, then on submit slice the stored ranking and call `src/multi_guild.aggregate_guilds()`.
  - Results are computed on submit and stored in `st.session_state` caches (`results_cache`, `multi_guild_cache`); rendering always reads from cache so reruns don't re-hit the API.
  - When both Deaths and Damage taken are shown, their two `render_from_cache()` calls run inside a keyed `st.container(key="results_side_by_side")` + `st.columns(2)`, with CSS (`flex-wrap` + per-column `min-width`) scoped to that container's `st-key-results_side_by_side` class. Streamlit has no API to read the client-side "Wide mode" toggle, so this is a pure-CSS proxy: in the ~730px default centered layout the two 420px-min columns don't fit and wrap onto separate lines; once Wide mode removes that cap, they sit side by side. That paired rendering also passes `compact=True` into `render_results`, trimming each table to `Class | Player | Total …` (see `summary_col_count` below).

- **`sections/results_section.py`** — renders the per-boss/ability results tables. `st.dataframe`'s Styler support only understands `background-color`/`color` (no text-stroke outline, no `nth-child` zebra striping, and `st.markdown` never executes `<script>` so no click-to-sort either) — so tables are instead built as a real `<table>` and rendered via `st.components.v1.html` (`_render_class_colored_table`), which runs in an iframe and does execute the inline sort script. Each table: colors "Class"/"Player" text by class (outline via `paint-order: stroke fill` + `-webkit-text-stroke`, not a stacked stroke+shadow — that doubles every glyph edge and reads as blurry), zebra-stripes odd rows with a translucent grey overlay (theme-safe in both light/dark), caps header height at 1.5× a data row's height, and sizes columns to the (nowrap) header text width rather than to cell content. Click a header to sort. `compact=True` (the side-by-side case above) trims the displayed table to its leading `summary_col_count` columns (3 for deaths: Class/Player/Total Deaths; 4 for damage: + Total Hits) and hides the "logs used" link list — CSV export is unaffected and always contains the full per-date breakdown.

- **`src/multi_guild.py`** — the top-guilds engine. Per run it resets thread-safe caches (`reset_report_caches()`) so fights/actors/death-events fetched for one metric or ability are reused by the others. Drops players below `min_attendance_frac` of their guild's total wipe pulls (so people who sat out don't look "clean").

- **`src/estimate.py`** — pure, API-free heuristics for "how many points / how long will this range take": `estimate_points`, `estimate_compute_seconds`, `plan()` (adds budget feasibility + wait time for hourly resets), `format_duration`. **Single source of truth** for both the live estimate in the ranking tab and the pre-flight in `compute_multi_guild_cache`. The `POINTS_PER_GUILD = 30` / `SECONDS_PER_GUILD = 3.0` constants are deliberately rough — calibrate them against a real run.

- **`src/overnight.py` + `overnight_run.py`** — the auto-resume runner for ranges too big for one hourly window. Processes guilds in chunks; before each chunk it checks the points budget and, if short, sleeps and re-pings every `poll_seconds` (default 300) **only while the run is alive**, then resumes. Writes a job directory under `output/overnight/<stamp>_r<a>-<b>/` containing `job.json`, live `status.json` (polled by the UI), `checkpoint.json` (processed ranks + rows, so a crash/reboot resumes), `run.log`, per-(boss, metric) CSVs, and an optional `STOP` sentinel for a clean early exit. No Streamlit import, so it runs headless. The ranking tab's 🌙 expander launches it as a **detached** subprocess (survives closing the browser tab) and renders its status file.

### Two config data stores (both under `config/`, both keyed by *raid file*)

- **`config/bosses/<raid>.json`** (`src/boss_config.py`) — boss IDs, their tracked ability IDs, and ability-name labels. Read/written through `boss_config` with a per-file in-memory cache; the UI can add abilities and it persists here. Note the default constant is `Manaforge_Omega.json` but the app's active raid defaults to `Midnight_season_1.json` (`st.session_state["selected_raid_file"]`).

- **`config/guild_rankings/<raid>.json`** (`src/guild_rankings_store.py`) — a static, committed list of world-progress guilds (`{rank, guild_id, guild_name, server_name, region}`). This is the source of truth for ranking mode and is **sliced with zero API calls** — only the per-guild log analysis costs points. It is shared via git so all users analyze the same guild set. The file is generated offline from pasted WCL progress-rankings HTML via `src/rankings_html_parser.py` (progress ranks are append-only by kill date, so existing guilds keep their earlier rank when extending the list).

### Conventions worth knowing

- **Mythic + wipes-only everywhere.** `DIFFICULTY = 5` (in `sections/input_settings.py`), and kill fights are filtered out — analysis is about progression pulls.
- **Ranking mode does a points pre-flight** (`compute_multi_guild_cache` in `ui/app.py`): it estimates cost and refuses up front if the hourly budget can't cover the run, rather than failing mid-way. For runs that exceed the budget, use the overnight runner instead of raising the retry waits — `run_wcl_query`'s 429 backoff is intentionally short/capped (≤10s) because the *hourly points budget* can't be waited out inside one interactive request.
- **Reading the rate limit costs 1 point**, so the UI only refreshes it on an explicit button click (and reuses the cached snapshot elsewhere) rather than polling it.
- **"Best report per day"**: when a guild has multiple reports on one date, only the longest-duration one is analyzed.
- Adding a new tracked raid means creating both a `config/bosses/<raid>.json` and (for ranking mode) a `config/guild_rankings/<raid>.json`.
- **Default input settings favor "fewest clicks" for the current tier**: default boss is "Midnight Falls" with ability 1254076 (Heaven's Glaives) pre-selected, "Ignore events after player deaths" defaults to 3, and "End of range" defaults to "First kill" (each boss stops at its own first kill instead of a fixed end date). All set in `sections/input_settings.py`; update them as the current progression target changes.
