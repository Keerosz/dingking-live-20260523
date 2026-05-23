from __future__ import annotations

import pandas as pd

from .models import PlayerRecord


REQUIRED_COLUMNS = {
    "player_name",
    "team",
    "bats",
    "lineup_slot",
    "recent_hr_form",
    "hard_hit_pct",
    "barrel_pct",
    "pull_pct",
    "fly_ball_pct",
    "iso",
    "hr_pa",
    "recent_hits",
    "recent_hr_streak",
    "pitcher_name",
    "pitcher_hand",
    "hr_allowed",
    "opp_fly_ball_tendency",
    "pitcher_split_weakness",
    "hard_contact_allowed",
    "barrel_rate_allowed",
    "pitcher_fatigue",
    "bullpen_quality",
    "wind_speed",
    "wind_direction",
    "temperature",
    "humidity",
    "air_density",
    "park_name",
    "park_hr_factor",
    "park_lhb_boost",
    "park_rhb_boost",
    "short_porch",
    "dome",
    "implied_total",
    "moneyline",
    "projected_ownership",
    "leverage_score_seed",
    "start_time_bucket",
    "game_id",
}


def load_slate_from_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    return df


def load_slate_from_records(records: list[PlayerRecord]) -> pd.DataFrame:
    if not records:
        raise ValueError("No player records provided")
    return pd.DataFrame([record.model_dump() for record in records])
