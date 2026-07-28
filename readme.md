# Northstar Warcraft Logs Analyzer

A lightweight tool that fetches raid reports from Warcraft Logs and generates clean CSV summaries of player deaths and damage taken, grouped by boss/ability and date — for a single guild, or aggregated across many guilds at once.

The tool runs locally on your machine using Streamlit and includes a built-in .env creator for Warcraft Logs API credentials.

## Prerequisites

- Windows (tested) — should also work on macOS/Linux (Job for Ari)
- Uses Python 3.10+
- A Warcraft Logs account (free) for Client ID and Secret.
- You do not need global Python packages. Everything stays inside a .venv.

## Installation
### 1. Clone the repository

    
    git clone https://github.com/NAskjaer/northstar-warcraftlog-fetcher.git
    cd northstar-warcraftlog-fetcher

### 2. Create & activate a virtual environment (VS Code Method)

*TODO*

### 3. Install required packages

*TODO*

## Running the app

#### Start the application:


    python run_app.py

#### Your browser will automatically open:

    http://localhost:8501

#### Shutting down the app
Stop the app by pressing Ctrl + C in the terminal.

### First-time setup: Getting Warcraft Logs API credentials

1. When you run the app for the first time, you’ll see a section labeled: 
**“Warcraft Logs API credentials”**
2. Follow the guide shown inside the app or go to  https://www.warcraftlogs.com/api/clients/ and do the following

    * Click + **Create Client**
    * **Name:** anything, e.g. northstar-log-fetcher
    * **Redirect URL:** http://localhost
    * Leave **Public Client** unchecked (use private client)
    * Click **Create**
    * Copy your Client ID and Client Secret into the app

3. Click **Save & Validate**

The app will now call warcraftlogs to check if the Secret and Client ID is valid, if so  it will create *.env* automatically and collapse the validation options.

## Using the analyzer
#### Input settings

By default the analyzer targets Midnight Falls with the ability "Heaven's Glaives" pre-selected, "Ignore events after player deaths" set to 3, and "End of range" set to "First kill" (each boss stops at its own first kill instead of a fixed end date) — tuned for the fewest clicks on the current tier. All of these can be changed per run.

Choose **Deaths**, **Damage taken**, or **Both** for what to aggregate.

#### Guild source: single guild vs. multi-guild queue

- **Single guild (URL)** — paste one guild's Warcraft Logs URL and analyze just that guild.
- **Multi-guild queue** — aggregate many guilds at once, sourced either:
  - **By world rank** — a rank range sliced from a stored world-progress ranking for the raid (zero API calls to build the list — only the per-guild log analysis costs points), or
  - **By guild links** — paste specific guild URLs, one per line. Names are resolved for free from the stored ranking when a guild is already in it, falling back to a small live lookup (~1 point each) only for guilds that aren't.

  A live progress bar, a **Stop** button, and a running points/budget counter track the queue while it runs. If the hourly WCL points budget runs out partway through, the run stops itself with a clear message rather than grinding through guilds that are doomed to fail — use the **overnight runner** (an expander in the multi-guild queue view) for ranges too large to finish in one hourly window; it runs as a separate background process that auto-resumes across hourly resets, with its own progress/CSV outputs and a resumable checkpoint if interrupted.

#### API rate limit

The **Warcraft Logs API rate limit** section at the top of the page shows your current hourly points usage on demand (checking it costs 1 point, so it only refreshes on click) and live-updates with a throttle warning if a request hits a 429 and is retrying.

#### Results table

Each results table shows a **Class** column (e.g. "Warrior (Fury)") next to **Player**, colored by the character's WoW class, alongside the metric total and a per-report-day breakdown — **Total Deaths** for deaths tables, or **Hits** for damage-taken tables (the pull count is folded into that column's header; the summary total column shows hit count only, not raw damage — the per-date breakdown and CSV export still include full damage numbers). Click a column header to sort; rows alternate with a faint stripe for readability; neither the header nor the data ever wraps, so columns size to whichever is wider.

If you select both **Deaths** and **Damage taken**, the two tables show side by side once you switch the page to **Wide mode** (Streamlit's settings menu, top right) — in the default (centered) layout, or with only one metric selected, each table shows full per-date detail; side by side, they're trimmed to just Class / Player / Totals to fit the space (the CSV download always includes the full per-date breakdown regardless).
