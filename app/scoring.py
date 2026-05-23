from __future__ import annotations

import numpy as np
import pandas as pd


def _minmax(series: pd.Series) -> pd.Series:
    lo = float(series.min())
    hi = float(series.max())
    if hi - lo <= 1e-9:
        return pd.Series(np.full(len(series), 0.5), index=series.index)
    return (series - lo) / (hi - lo)


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    optional_defaults = {
        "last5_hits": np.nan,
        "last5_total_bases": np.nan,
        "last5_home_runs": np.nan,
    }
    for col, default in optional_defaults.items():
        if col not in out.columns:
            out[col] = default

    last5_hits_signal = out["last5_hits"].fillna(out["recent_hits"] * (5.0 / 7.0))
    last5_tb_signal = out["last5_total_bases"].fillna(last5_hits_signal * 1.6)
    last5_hr_signal = out["last5_home_runs"].fillna(out["recent_hr_form"] * 1.2)

    weather_boost = (
        out["wind_speed"] * np.where(out["wind_direction"].str.lower().eq("out"), 1.0, -0.2)
        + (out["temperature"] - 65.0) * 0.35
        + (55.0 - out["humidity"]) * 0.05
        + (1.0 - out["air_density"]) * 70.0
    )

    handed_park_boost = np.where(
        out["bats"].str.upper().eq("L"), out["park_lhb_boost"], out["park_rhb_boost"]
    )

    raw_hr = (
        out["recent_hr_form"] * 0.16
        + last5_hr_signal * 0.10
        + out["hard_hit_pct"] * 0.008
        + out["barrel_pct"] * 0.02
        + out["pull_pct"] * 0.004
        + out["fly_ball_pct"] * 0.006
        + out["iso"] * 1.2
        + out["hr_pa"] * 3.0
        + out["recent_hr_streak"] * 0.05
        + out["hr_allowed"] * 0.015
        + out["hard_contact_allowed"] * 0.5
        + out["barrel_rate_allowed"] * 1.8
        + out["pitcher_fatigue"] * 0.45
        + out["park_hr_factor"] * 0.8
        + handed_park_boost * 0.35
        + weather_boost * 0.02
    )

    raw_leverage = (1.0 - out["projected_ownership"]) * 0.65 + out["leverage_score_seed"] * 0.35

    raw_environment = (
        out["implied_total"] * 0.5
        + out["park_hr_factor"] * 1.3
        + handed_park_boost * 0.75
        + weather_boost * 0.03
        + (1.0 - out["bullpen_quality"]) * 0.9
    )

    raw_hit = (
        out["recent_hits"] * 0.24
        + last5_hits_signal * 0.16
        + out["hard_hit_pct"] * 0.010
        + out["barrel_pct"] * 0.010
        + out["pull_pct"] * 0.002
        + out["fly_ball_pct"] * 0.002
        + out["implied_total"] * 0.95
        + (10 - out["lineup_slot"]).clip(lower=1) * 0.15
        + (1.0 - out["bullpen_quality"]) * 0.95
        + weather_boost * 0.012
        + handed_park_boost * 0.22
    )

    raw_pitcher_vuln = (
        out["hr_allowed"] * 0.030
        + out["hard_contact_allowed"] * 1.25
        + out["barrel_rate_allowed"] * 3.5
        + out["pitcher_fatigue"] * 1.1
        + (1.0 - out["bullpen_quality"]) * 1.2
        + out["opp_fly_ball_tendency"] * 0.8
    )

    raw_recent_hits = (
        out["recent_hits"] * 0.6
        + last5_hits_signal * 0.28
        + out["lineup_slot"].rsub(10).clip(lower=1) * 0.08
        + out["implied_total"] * 0.2
    )

    raw_tb = (
        out["recent_hits"] * 0.30
        + last5_tb_signal * 0.14
        + out["hard_hit_pct"] * 0.010
        + out["barrel_pct"] * 0.016
        + out["iso"] * 1.8
        + out["hr_pa"] * 2.1
        + out["implied_total"] * 0.75
        + out["park_hr_factor"] * 0.55
        + weather_boost * 0.012
    )

    raw_rbi = (
        out["implied_total"] * 1.15
        + out["lineup_slot"].rsub(10).clip(lower=1) * 0.18
        + out["hard_hit_pct"] * 0.007
        + out["barrel_pct"] * 0.014
        + out["pitcher_fatigue"] * 0.80
        + (1.0 - out["bullpen_quality"]) * 1.05
        + out["hr_allowed"] * 0.020
    )

    raw_hrr = (
        out["recent_hits"] * 0.42
        + last5_hits_signal * 0.18
        + out["implied_total"] * 0.90
        + out["lineup_slot"].rsub(10).clip(lower=1) * 0.16
        + out["hard_hit_pct"] * 0.008
        + out["barrel_pct"] * 0.010
        + weather_boost * 0.010
    )

    raw_correlation = (
        out["implied_total"] * 0.4
        + (9 - out["lineup_slot"]).clip(lower=0) * 0.08
        + out["opp_fly_ball_tendency"] * 0.8
    )

    raw_chaos = (
        out["barrel_pct"] * 0.011
        + out["pull_pct"] * 0.005
        + out["fly_ball_pct"] * 0.005
        + out["wind_speed"] * 0.04
        + (1.0 - out["projected_ownership"]) * 0.9
        + (1.0 - out["bullpen_quality"]) * 0.5
    )

    out["hr_score"] = _minmax(raw_hr)
    out["leverage_score"] = _minmax(raw_leverage)
    out["environment_score"] = _minmax(raw_environment)
    out["hit_score"] = _minmax(raw_hit)
    out["pitcher_vuln_score"] = _minmax(raw_pitcher_vuln)
    out["recent_hits_score"] = _minmax(raw_recent_hits)
    out["tb_score"] = _minmax(raw_tb)
    out["rbi_score"] = _minmax(raw_rbi)
    out["hrr_score"] = _minmax(raw_hrr)
    out["correlation_score"] = _minmax(raw_correlation)
    out["chaos_score"] = _minmax(raw_chaos)
    out["combo_score"] = (
        out["hit_score"] * 0.35
        + out["tb_score"] * 0.35
        + out["rbi_score"] * 0.15
        + out["hrr_score"] * 0.15
    )

    out["portfolio_value_score"] = (
        out["hr_score"] * 0.24
        + out["hit_score"] * 0.10
        + out["leverage_score"] * 0.22
        + out["environment_score"] * 0.20
        + out["correlation_score"] * 0.14
        + out["chaos_score"] * 0.12
    )

    out["chalk_flag"] = out["projected_ownership"] >= 0.20
    out["leverage_flag"] = out["projected_ownership"] <= 0.12
    return out.sort_values("portfolio_value_score", ascending=False).reset_index(drop=True)
