from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


COLUMN_ALIASES: dict[str, list[str]] = {
    "player_name": ["player", "name", "batter", "hitter", "player_name", "batterName"],
    "team": ["team", "tm", "batterTeamCode"],
    "bats": ["bats", "hand", "bat_hand", "batting_hand"],
    "lineup_slot": ["lineup_slot", "lineup", "order", "batting_order"],
    "recent_hr_form": ["recent_hr_form", "hr_form", "form", "woba", "ops"],
    "hard_hit_pct": ["hard_hit_pct", "hard_hit%", "hard_hit"],
    "barrel_pct": ["barrel_pct", "barrel%", "barrel"],
    "pull_pct": ["pull_pct", "pull%", "pull"],
    "fly_ball_pct": ["fly_ball_pct", "fb%", "fly_ball", "flyball"],
    "iso": ["iso", "isolated_power"],
    "hr_pa": ["hr_pa", "hr_per_pa", "atBatsPerHomeRun"],
    "recent_hits": ["recent_hits", "hits_last7", "hits"],
    "last5_hits": ["last5_hits", "last_5_hits", "hits_last5", "hits_last_5"],
    "last5_total_bases": ["last5_total_bases", "last_5_total_bases", "tb_last5", "total_bases_last5"],
    "last5_home_runs": ["last5_home_runs", "last_5_home_runs", "hr_last5", "home_runs_last5"],
    "recent_hr_streak": ["recent_hr_streak", "hr_streak", "streak", "homeRuns"],
    "pitcher_name": ["pitcher_name", "opp_pitcher", "pitcher", "pitcherName"],
    "pitcher_hand": ["pitcher_hand", "pitcher_handedness", "pitchingType"],
    "hr_allowed": ["hr_allowed", "pitcher_hr_allowed"],
    "opp_fly_ball_tendency": ["opp_fly_ball_tendency", "pitcher_fb_tendency"],
    "pitcher_split_weakness": ["pitcher_split_weakness", "split_weakness"],
    "hard_contact_allowed": ["hard_contact_allowed", "pitcher_hard_contact_allowed"],
    "barrel_rate_allowed": ["barrel_rate_allowed", "pitcher_barrel_rate_allowed"],
    "pitcher_fatigue": ["pitcher_fatigue", "fatigue"],
    "bullpen_quality": ["bullpen_quality", "bullpen"],
    "wind_speed": ["wind_speed", "wind_mph"],
    "wind_direction": ["wind_direction", "wind_dir"],
    "temperature": ["temperature", "temp"],
    "humidity": ["humidity"],
    "air_density": ["air_density"],
    "park_name": ["park_name", "ballpark", "park"],
    "park_hr_factor": ["park_hr_factor", "hr_factor"],
    "park_lhb_boost": ["park_lhb_boost", "lhb_boost"],
    "park_rhb_boost": ["park_rhb_boost", "rhb_boost"],
    "short_porch": ["short_porch", "porch", "short_field"],
    "dome": ["dome", "is_dome"],
    "implied_total": ["implied_total", "team_total", "ops", "slg"],
    "moneyline": ["moneyline", "ml"],
    "projected_ownership": ["projected_ownership", "ownership", "own%"],
    "leverage_score_seed": ["leverage_score_seed", "leverage", "lev"],
    "start_time_bucket": ["start_time_bucket", "time_bucket", "window", "gameDate"],
    "game_id": ["game_id", "game", "matchup", "gameId"],
    "position": ["position", "pos"],
}


DEFAULTS: dict[str, object] = {
    "bats": "R",
    "lineup_slot": 5,
    "recent_hr_form": 0.50,
    "hard_hit_pct": 42.0,
    "barrel_pct": 10.0,
    "pull_pct": 40.0,
    "fly_ball_pct": 38.0,
    "iso": 0.20,
    "hr_pa": 0.045,
    "recent_hits": 7,
    "last5_hits": float("nan"),
    "last5_total_bases": float("nan"),
    "last5_home_runs": float("nan"),
    "recent_hr_streak": 0,
    "pitcher_name": "Unknown",
    "pitcher_hand": "R",
    "hr_allowed": 18.0,
    "opp_fly_ball_tendency": 0.35,
    "pitcher_split_weakness": "RHB",
    "hard_contact_allowed": 0.34,
    "barrel_rate_allowed": 0.07,
    "pitcher_fatigue": 0.20,
    "bullpen_quality": 0.55,
    "wind_speed": 9.0,
    "wind_direction": "out",
    "temperature": 75.0,
    "humidity": 50.0,
    "air_density": 0.97,
    "park_name": "Unknown Park",
    "park_hr_factor": 1.00,
    "park_lhb_boost": 1.00,
    "park_rhb_boost": 1.00,
    "short_porch": 0,
    "dome": 0,
    "implied_total": 4.5,
    "moneyline": -105.0,
    "projected_ownership": 0.12,
    "leverage_score_seed": 0.50,
    "start_time_bucket": "mid",
    "game_id": "UNKNOWN@UNKNOWN",
    "position": "",
}


NUMERIC_COLUMNS = {
    "lineup_slot",
    "recent_hr_form",
    "hard_hit_pct",
    "barrel_pct",
    "pull_pct",
    "fly_ball_pct",
    "iso",
    "hr_pa",
    "recent_hits",
    "last5_hits",
    "last5_total_bases",
    "last5_home_runs",
    "recent_hr_streak",
    "hr_allowed",
    "opp_fly_ball_tendency",
    "hard_contact_allowed",
    "barrel_rate_allowed",
    "pitcher_fatigue",
    "bullpen_quality",
    "wind_speed",
    "temperature",
    "humidity",
    "air_density",
    "park_hr_factor",
    "park_lhb_boost",
    "park_rhb_boost",
    "short_porch",
    "dome",
    "implied_total",
    "moneyline",
    "projected_ownership",
    "leverage_score_seed",
}


def _find_first(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    lower_map = {c.strip().lower(): c for c in df.columns}
    for name in candidates:
        key = name.strip().lower()
        if key in lower_map:
            return lower_map[key]
    return None


def normalize_to_weather_warfare(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()

    for target, aliases in COLUMN_ALIASES.items():
        source = _find_first(df, aliases)
        if source is not None:
            out[target] = df[source]
        else:
            out[target] = DEFAULTS.get(target, "")

    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
        out[col] = out[col].fillna(DEFAULTS.get(col, 0.0))

    # Embedded payload may provide AB/HR; convert to HR/PA style signal.
    if "atBatsPerHomeRun" in df.columns:
        ab_per_hr = pd.to_numeric(df["atBatsPerHomeRun"], errors="coerce")
        converted = 1.0 / ab_per_hr.replace(0, pd.NA)
        converted = converted.fillna(out["hr_pa"])
        out["hr_pa"] = converted

    out["player_name"] = out["player_name"].astype(str).str.strip()
    out["team"] = out["team"].astype(str).str.strip().str.upper()
    out = out[out["player_name"].str.len() > 0]
    out = out[out["team"].str.len() > 0]

    out["lineup_slot"] = out["lineup_slot"].clip(lower=1, upper=9).astype(int)
    out["short_porch"] = out["short_porch"].round().clip(lower=0, upper=1).astype(int)
    out["dome"] = out["dome"].round().clip(lower=0, upper=1).astype(int)

    for pct_col in ["projected_ownership", "opp_fly_ball_tendency", "hard_contact_allowed", "barrel_rate_allowed", "pitcher_fatigue", "bullpen_quality", "recent_hr_form", "leverage_score_seed"]:
        out[pct_col] = out[pct_col].clip(lower=0.0, upper=1.0)

    # If timestamps are present, bucket into early/mid/late.
    if "gameDate" in df.columns:
        parsed_time = pd.to_datetime(df["gameDate"], errors="coerce", utc=True)

        def _bucket(ts):
            if pd.isna(ts):
                return None
            hour = ts.hour
            if hour < 20:
                return "early"
            if hour < 23:
                return "mid"
            return "late"

        derived_bucket = parsed_time.apply(_bucket)
        out["start_time_bucket"] = out["start_time_bucket"].astype(str)
        out.loc[derived_bucket.notna(), "start_time_bucket"] = derived_bucket[derived_bucket.notna()]

    out = out.drop_duplicates(subset=["player_name", "game_id"], keep="first").reset_index(drop=True)
    return out
