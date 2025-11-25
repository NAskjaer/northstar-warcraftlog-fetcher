import csv
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import get_deaths_by_player_for_ability


def main() -> None:
    load_dotenv()

    print("Starting Warcraft logs processing...")

    # === CONFIG ===
    guild_id = 260153  # from https://www.warcraftlogs.com/guild/id/260153

    # Date range for reports (UTC)
    start = datetime(2025, 8, 28, tzinfo=timezone.utc)
    end = datetime(2025, 9, 8, tzinfo=timezone.utc)

    # Boss / ability you care about
    boss_id = 3134
    boss_name = "Nexus-King Salhadaar"

    ability_id = 1227472  # Besiege ID
    ability_name = "Besiege"

    difficulty = 5  # Mythic
    wipes_only = False

    max_deaths_per_pull = 3  # Limit deaths counted per pull

    # === FETCH REPORTS ===
    reports = fetch_logs_for_guild(guild_id, start, end)
    print(f"Found {len(reports)} reports in range.\n")

    if not reports:
        print("No reports found for the given range.")
        return

    # We’ll keep:
    #  - report_dates: list of (report_code, date_string)
    #  - per_report_counts: report_code -> { player_name -> deaths }
    #  - all_players: set of all players seen in any report
    report_dates: list[tuple[str, str]] = []
    per_report_counts: dict[str, dict[str, int]] = {}
    all_players: set[str] = set()

    for report in reports:
        code = report["code"]
        start_ms = report["startTime"]

        # Convert start timestamp (ms) to a date string YYYY-MM-DD
        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).date().isoformat()

        report_dates.append((code, date_str))

        print(f"Processing report {code} - {report.get('title', '')} ({date_str})")

        deaths_rows = get_deaths_by_player_for_ability(
            report_code=code,
            boss_id=boss_id,
            ability_id=ability_id,
            difficulty=difficulty,
            wipes_only=True,
            max_deaths_per_pull=max_deaths_per_pull,  # or None
        )

        # deaths_rows is a list of { 'player': str, 'total_deaths': int }
        player_counts: dict[str, int] = {
            row["player"]: row["total_deaths"] for row in deaths_rows
        }

        per_report_counts[code] = player_counts
        all_players.update(player_counts.keys())

    # === BUILD MATRIX FOR CSV ===

    # First header row: boss name, ability name
    header_row_1 = [boss_name, ability_name]

    # Second header row: empty cell, then one column per date, plus total
    date_columns = [date for (_code, date) in report_dates]
    header_row_2 = [""] + date_columns + ["Total Deaths"]

    # 1) Compute total deaths across all reports for each player
    player_totals: dict[str, int] = {}
    for player in all_players:
        total = 0
        for code, _date in report_dates:
            total += per_report_counts.get(code, {}).get(player, 0)
        player_totals[player] = total

    # 2) Sort players by total deaths (desc), then by name (asc)
    sorted_players = sorted(
        all_players,
        key=lambda p: (-player_totals[p], p.lower()),
    )

    # 3) Build player rows in that sorted order (for debug print)
    player_rows: list[list[object]] = []

    for player in sorted_players:
        row = [player]
        total_for_player = 0
        for code, _date in report_dates:
            deaths = per_report_counts.get(code, {}).get(player, 0)
            row.append(deaths)
            total_for_player += deaths
        row.append(total_for_player)
        player_rows.append(row)

    # === DEBUG OUTPUT ===
    print("\n=== DEBUG: REPORT DATES ===")
    for code, date in report_dates:
        print(f"{code} -> {date}")

    print("\n=== DEBUG: PER REPORT COUNTS ===")
    for code, counts in per_report_counts.items():
        print(f"{code}: {counts}")

    print("\n=== DEBUG: ALL PLAYERS ===")
    print(all_players)

    print("\n=== DEBUG: PLAYER TOTALS ===")
    for p, total in player_totals.items():
        print(f"{p}: {total}")

    print("\n=== DEBUG: SORTED PLAYERS ===")
    print(sorted_players)

    print("\n=== DEBUG: PLAYER ROWS ===")
    for row in player_rows:
        print(row)

    # === WRITE CSV ===
    csv_path = os.path.join("output", "deaths_summary.csv")
    os.makedirs("output", exist_ok=True)

    with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)

        # Header rows
        writer.writerow(header_row_1)
        writer.writerow(header_row_2)

        # Player rows
        for row in player_rows:
            writer.writerow(row)

    print(f"\nCSV written to: {csv_path}")


if __name__ == "__main__":
    main()
