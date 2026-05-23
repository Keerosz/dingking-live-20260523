from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Any


def build_exposure_report(parlays: list[dict[str, Any]], max_player_exposure: int) -> dict[str, Any]:
    player_counter: Counter[str] = Counter()
    game_counter: Counter[str] = Counter()
    team_counter: Counter[str] = Counter()
    time_counter: Counter[str] = Counter()
    archetype_counter: Counter[str] = Counter()
    pair_counter: Counter[tuple[str, str]] = Counter()

    for slip in parlays:
        archetype_counter[slip["archetype"]] += 1
        for leg in slip["legs"]:
            player_counter[leg["player_name"]] += 1
            game_counter[leg["game_id"]] += 1
            team_counter[leg["team"]] += 1
            time_counter[leg["start_time_bucket"]] += 1

        names = sorted([leg["player_name"] for leg in slip["legs"]])
        for pair in combinations(names, 2):
            pair_counter[pair] += 1

    violations = [
        {"player_name": p, "count": c}
        for p, c in player_counter.items()
        if c > max_player_exposure
    ]

    repeated_pairings = [
        {"pair": list(pair), "count": count}
        for pair, count in pair_counter.items()
        if count > 1
    ]
    repeated_pairings.sort(key=lambda x: x["count"], reverse=True)

    return {
        "player_exposure": {str(k): int(v) for k, v in player_counter.items()},
        "game_exposure": {str(k): int(v) for k, v in game_counter.items()},
        "team_exposure": {str(k): int(v) for k, v in team_counter.items()},
        "time_slot_exposure": {str(k): int(v) for k, v in time_counter.items()},
        "archetype_exposure": {str(k): int(v) for k, v in archetype_counter.items()},
        "violations": violations,
        "repeated_pairings": repeated_pairings,
    }
