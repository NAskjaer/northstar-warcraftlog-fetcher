# main.py
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any

from dotenv import load_dotenv

from src.calendar_fetcher import fetch_logs_for_guild
from src.deaths_fetcher import get_deaths_by_player_for_ability


def main() -> None:
    load_dotenv()

    print("Starting Warcraft logs processing...")

    # === HARD-CODED CONFIG (adjust as you like) ===
    guild_id = 260153  # example: https://www.warcraftlogs.com/guild/id/260153

    # Date range (UTC)
    start = datetime(2025, 8, 28, tzinfo=timezone.utc)
    end = datetime(2025, 9, 8, tzinfo=timezone.utc)

    # Boss / ability
    boss_id = 3134
    boss_name = "Nexus-King Salhadaar"

    ability_id = 1227472  # Besiege
    ability_name = "Besiege"

    difficulty = 5  # Mythic
    wipes_only = True

    # === FETCH REPORTS ===
    reports = fetch_logs_for_guild(guild_id, start, end)
    print(f"Found {len(reports)} reports in range.\n")

    if not reports:
        print("No reports found for the given range.")
        return

    # For the matrix:
    #   report_dates: [(report_code, "YYYY-MM-DD")]
    #   per_report_counts: report_code -> { player_name -> deaths }
    #   all_players: set of all player names seen
    report_dates: List[Tuple[str, str]] = []
    per_report_counts: Dict[str, Dict[str, int]] = {}
    all_players: set[str] = set()

    for report in reports:
        code = report["code"]
        start_ms = report["startTime"]
        title = report.get("title", "")

        date_str = datetime.fromtimestamp(
            start_ms / 1000, tz=timezone.utc
        ).date().isoformat()

        report_dates.append((code, date_str))

        print(f"Processing report {code} - {title} ({date_str})")

        rows = get_deaths_by_player_for_ability(
            report_code=code,
            boss_id=boss_id,
            ability_id=ability_id,
            difficulty=difficulty,
            wipes_only=wipes_only,
        )

        # rows: [{ "player": str, "total_deaths": int }, ...]
        player_counts: Dict[str, int] = {
            r["player"]: int(r["total_deaths"]) for r in rows
        }

        per_report_counts[code] = player_counts
        all_players.update(player_counts.keys())

    # === BUILD CSV MATRIX ===

    # First header row: boss & ability
    header_row_1 = [boss_name, ability_name]

    # Second header row: empty, then one column per report (date+code), plus total
    date_columns: List[str] = []
    for code, date in report_dates:
        date_columns.append(f"{date} ({code})")

    header_row_2 = ["Player"] + date_columns + ["Total Deaths"]

    # 1) total deaths across all reports per player
    player_totals: Dict[str, int] = {}
    for player in all_players:
        total = 0
        for code, _date in report_dates:
            total += per_report_counts.get(code, {}).get(player, 0)
        player_totals[player] = total

    # 2) sort players by total desc, then name
    sorted_players = sorted(
        all_players,
        key=lambda p: (-player_totals[p], p.lower()),
    )

    # 3) build rows
    player_rows: List[List[Any]] = []
    for player in sorted_players:
        row: List[Any] = [player]
        total_for_player = 0

        for code, _date in report_dates:
            deaths = per_report_counts.get(code, {}).get(player, 0)
            row.append(deaths)
            total_for_player += deaths

        row.append(total_for_player)
        player_rows.append(row)

    # === DEBUG (optional – you can nuke these once you're happy) ===
    print("\n=== DEBUG: REPORT DATES ===")
    for code, date in report_dates:
        print(f"{code} -> {date}")

    print("\n=== DEBUG: PER REPORT COUNTS ===")
    for code, counts in per_report_counts.items():
        print(f"{code}: {counts}")

    print("\n=== DEBUG: PLAYER TOTALS ===")
    for p, total in player_totals.items():
        print(f"{p}: {total}")

    # === WRITE CSV ===
    os.makedirs("output", exist_ok=True)
    csv_path = os.path.join("output", "deaths_summary.csv")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # First row: boss + ability
        writer.writerow(header_row_1)
        # Second row: Player + per-report columns + total
        writer.writerow(header_row_2)
        # Player data
        for row in player_rows:
            writer.writerow(row)

    print(f"\nCSV written to: {csv_path}")


if __name__ == "__main__":
    main()
