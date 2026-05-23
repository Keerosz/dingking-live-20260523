from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any


def build_portfolio_summary(
    parlays: list[dict[str, Any]],
    player_pool_size: int,
    max_exposure: int,
    exposure_report: dict[str, Any],
) -> dict[str, Any]:
    player_counts = exposure_report["player_exposure"]
    max_actual = max(player_counts.values()) if player_counts else 0

    return {
        "total_slips": len(parlays),
        "legs_per_slip": len(parlays[0]["legs"]) if parlays else 0,
        "unique_players_used": len(player_counts),
        "player_pool_size": player_pool_size,
        "hard_rule_max_exposure": max_exposure,
        "max_actual_exposure": max_actual,
        "hard_2x_respected": max_actual <= 2,
    }


def build_pairing_frequency(parlays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str]] = Counter()
    for slip in parlays:
        names = sorted([leg["player_name"] for leg in slip["legs"]])
        for pair in combinations(names, 2):
            counter[pair] += 1

    rows = [{"pair": list(p), "count": c} for p, c in counter.items()]
    rows.sort(key=lambda x: x["count"], reverse=True)
    return rows


def build_time_slot_distribution(parlays: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for slip in parlays:
        for leg in slip["legs"]:
            counter[str(leg["start_time_bucket"]).lower()] += 1
    return {str(k): int(v) for k, v in counter.items()}


def build_rr_survivability_metrics(parlays: list[dict[str, Any]]) -> dict[str, Any]:
    if not parlays:
        return {
            "avg_leg_portfolio_value": 0.0,
            "avg_slip_portfolio_value": 0.0,
            "median_slip_portfolio_value": 0.0,
            "diversity_index": 0.0,
        }

    slip_values = [p["avg_portfolio_value"] for p in parlays]
    all_leg_values = [l["portfolio_value_score"] for p in parlays for l in p["legs"]]
    all_names = [l["player_name"] for p in parlays for l in p["legs"]]

    counts = Counter(all_names)
    total = sum(counts.values())
    # Herfindahl complement for concentration, closer to 1 means more diversified.
    concentration = sum((c / total) ** 2 for c in counts.values())
    diversity_index = 1.0 - concentration

    sorted_slips = sorted(slip_values)
    median_val = sorted_slips[len(sorted_slips) // 2]

    return {
        "avg_leg_portfolio_value": round(sum(all_leg_values) / max(len(all_leg_values), 1), 4),
        "avg_slip_portfolio_value": round(sum(slip_values) / max(len(slip_values), 1), 4),
        "median_slip_portfolio_value": round(median_val, 4),
        "diversity_index": round(diversity_index, 4),
    }


def build_chalk_vs_leverage(parlays: list[dict[str, Any]]) -> dict[str, Any]:
    chalk = 0
    leverage = 0
    neutral = 0

    for slip in parlays:
        for leg in slip["legs"]:
            own = leg["projected_ownership"]
            if own >= 0.20:
                chalk += 1
            elif own <= 0.12:
                leverage += 1
            else:
                neutral += 1

    total = chalk + leverage + neutral
    if total == 0:
        return {
            "chalk_count": 0,
            "leverage_count": 0,
            "neutral_count": 0,
            "chalk_pct": 0.0,
            "leverage_pct": 0.0,
        }

    return {
        "chalk_count": chalk,
        "leverage_count": leverage,
        "neutral_count": neutral,
        "chalk_pct": round(chalk / total, 4),
        "leverage_pct": round(leverage / total, 4),
    }
