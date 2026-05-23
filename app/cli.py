from __future__ import annotations

import json
from pathlib import Path

from .config import PortfolioConfig
from .db import init_db, save_run
from .ingest import load_slate_from_csv
from .service import generate_portfolio_board


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sample_csv = root / "data" / "sample_slate.csv"

    init_db()

    df = load_slate_from_csv(str(sample_csv))
    config = PortfolioConfig(
        num_slips=14,
        legs_per_slip=4,
        min_player_pool=40,
        max_player_exposure=2,
    )

    board = generate_portfolio_board(df, config)
    save_run(board["run_id"], board, run_label="sample_cli_run")

    print("=== WEATHER WARFARE PORTFOLIO BOARD ===")
    print(json.dumps(board["summary"], indent=2))
    print("\nTop repeated pairings:")
    for row in board["pairing_frequency"][:10]:
        print(f"- {row['pair'][0]} + {row['pair'][1]}: {row['count']}x")
    print("\nArchetype mix:")
    for archetype, count in board["archetype_tags"].items():
        print(f"- {archetype}: {count}")


if __name__ == "__main__":
    main()
