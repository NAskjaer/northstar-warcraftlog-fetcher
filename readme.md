Northstar Warcraft Logs Analyzer

A lightweight tool that fetches raid reports from Warcraft Logs and generates a clean CSV summary of player deaths per report, grouped by boss/ability and date.

The tool runs locally on your machine using Streamlit and includes a built-in .env creator for Warcraft Logs API credentials.

Prerequisites

Windows (tested) — should also work on macOS/Linux (Job for Ari)

Python 3.10+

A Warcraft Logs account (free)

You do not need global Python packages. Everything stays inside a .venv.

Installation
1. Clone the repository
git clone https://github.com/NAskjaer/northstar-warcraftlog-fetcher.git
cd northstar-warcraftlog-fetcher

2. Create & activate a virtual environment

Windows (PowerShell):

python -m venv .venv
.\.venv\Scripts\Activate.ps1

macOS / Linux:

python3 -m venv .venv
source .venv/bin/activate

3. Install dependencies
Running the app

python run_app.py

should install the dependencies, but if not:

pip install -r requirements.txt

Your browser will automatically open on:

http://localhost:8501


Stop the app by pressing Ctrl + C in the terminal.

First-time setup: Getting Warcraft Logs API credentials

When you run the app for the first time, you’ll see a section labeled:

“Warcraft Logs API credentials”

Follow the guide shown inside the app:

Go to https://www.warcraftlogs.com/api/clients/

Click + Create Client

Name: anything, e.g. northstar-log-fetcher

Redirect URL: http://localhost

Leave Public Client unchecked

Click Create

Copy your Client ID and Client Secret into the fields in the app

Click Save & Validate

The app will:

Create .env automatically

Validate your credentials

Collapse the credentials panel once verified

You only need to do this once.

Using the analyzer
1. Input settings

Fill out:

Guild URL (full WCL guild link)

https://www.warcraftlogs.com/guild/id/<your guild id>


Start & end dates

Boss

Ability (auto-populated per boss)

Then click:

Generate CSV


The app:

Fetches all reports in the date range

Picks the most relevant report per day (highest deaths for that boss)

Counts deaths per player

Builds the table + downloadable CSV

2. Results & CSV

You will see:

A success message (X reports, Y players)

A search bar (filters displayed players)

A Download CSV button

A scrollable dataframe

CSV includes:

Metadata row (boss + ability)

Player rows

One column per date

Total deaths