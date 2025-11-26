# Northstar Warcraft Logs Analyzer

A lightweight tool that fetches raid reports from Warcraft Logs and generates a clean CSV summary of player deaths per report, grouped by boss/ability and date.

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

TODO
