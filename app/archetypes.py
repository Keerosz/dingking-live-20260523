from __future__ import annotations

import pandas as pd

ARCHETYPES = [
    "Chalk Stability",
    "Controlled Chaos",
    "Wind Warfare",
    "Leverage Bombs",
    "Late Hammer",
    "Bullpen Death",
    "Catcher Leverage",
    "Porch Hunters",
    "Dome Nukes",
    "Ace Killers",
]


def player_archetype_matches(row: pd.Series) -> list[str]:
    tags: list[str] = []

    if row["projected_ownership"] >= 0.21 and row["hr_score"] >= 0.60:
        tags.append("Chalk Stability")

    if row["chaos_score"] >= 0.62 and 0.08 < row["projected_ownership"] < 0.22:
        tags.append("Controlled Chaos")

    if str(row["wind_direction"]).lower() == "out" and row["wind_speed"] >= 12:
        tags.append("Wind Warfare")

    if row["leverage_score"] >= 0.62 and row["projected_ownership"] <= 0.14:
        tags.append("Leverage Bombs")

    if str(row["start_time_bucket"]).lower() == "late":
        tags.append("Late Hammer")

    if row["pitcher_fatigue"] >= 0.30 and row["bullpen_quality"] <= 0.52:
        tags.append("Bullpen Death")

    if str(row.get("position", "")).upper() == "C" and row["projected_ownership"] <= 0.10:
        tags.append("Catcher Leverage")

    if int(row["short_porch"]) == 1:
        tags.append("Porch Hunters")

    if int(row["dome"]) == 1:
        tags.append("Dome Nukes")

    if row["hr_allowed"] <= 16 and row["hard_contact_allowed"] <= 0.32 and row["hr_score"] >= 0.57:
        tags.append("Ace Killers")

    if not tags:
        tags.append("Controlled Chaos")
    return tags


def add_archetype_candidates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["archetype_candidates"] = out.apply(player_archetype_matches, axis=1)
    return out
