from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agents import RealTimeResearchAgents
from .config import PortfolioConfig
from .db import init_db, save_run
from .service import generate_portfolio_board


def main() -> None:
    parser = argparse.ArgumentParser(description="PropFinder extraction test")
    parser.add_argument("--run-builder", action="store_true", help="Run portfolio generation after extraction")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"

    init_db()
    agent = RealTimeResearchAgents(data_dir=data_dir)
    fetch = agent._fetch_propfinder_tables()
    raw_df = fetch["df"]

    print(f"Extracted raw records: {len(raw_df)}")
    print(f"Source: {fetch.get('source')}")
    print(f"Message: {fetch.get('message')}")

    if raw_df.empty:
        print("No records extracted; skipping builder.")
        return

    from .normalize import normalize_to_weather_warfare

    norm_df = normalize_to_weather_warfare(raw_df)
    print(f"Normalized records: {len(norm_df)}")
    print(f"Unique players: {norm_df['player_name'].nunique()}")

    (data_dir / "debug_raw_players.json").write_text(
        json.dumps(raw_df.to_dict(orient="records"), default=str), encoding="utf-8"
    )
    (data_dir / "debug_normalized_players.json").write_text(
        json.dumps(norm_df.to_dict(orient="records"), default=str), encoding="utf-8"
    )

    if not args.run_builder:
        return

    config = PortfolioConfig(num_slips=14, legs_per_slip=4, min_player_pool=40, max_player_exposure=2)
    board = generate_portfolio_board(norm_df, config)
    save_run(board["run_id"], board, run_label="cli_extract_test")
    print(f"Portfolio run saved: {board['run_id']}")


if __name__ == "__main__":
    main()
