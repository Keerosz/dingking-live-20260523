from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from .archetypes import ARCHETYPES, add_archetype_candidates
from .config import PortfolioConfig


def _weighted_pick(rng: np.random.Generator, candidates: pd.DataFrame, weight_col: str) -> pd.Series:
    weights = candidates[weight_col].to_numpy(dtype=float)
    weights = np.clip(weights, 1e-6, None)
    probs = weights / weights.sum()
    idx = rng.choice(candidates.index.to_numpy(), p=probs)
    return candidates.loc[idx]


def _boost_for_archetype(candidates: pd.DataFrame, archetype: str) -> pd.Series:
    return candidates["archetype_candidates"].apply(
        lambda tags: 1.35 if archetype in tags else 0.85
    )


def _story_text(archetype: str) -> str:
    mapping = {
        "Chalk Stability": "Core chalk power stabilizes in favorable run environments.",
        "Controlled Chaos": "Balanced volatility with ceiling bats from mixed ownership tiers.",
        "Wind Warfare": "Wind-aided carry creates elevated multi-HR probability paths.",
        "Leverage Bombs": "Low-owned power cluster provides differentiation and upside.",
        "Late Hammer": "Late-window bombs create comeback equity and swap leverage.",
        "Bullpen Death": "Starter fatigue and weak bullpen depth open late-inning HR windows.",
        "Catcher Leverage": "Under-owned catcher pop anchors contrarian HR construction.",
        "Porch Hunters": "Short-field dimensions increase pull-side HR conversion.",
        "Dome Nukes": "Stable indoor hitting environment supports clean HR contact quality.",
        "Ace Killers": "Elite pitchers are challenged by concentrated upper-tail slugging outcomes.",
    }
    return mapping.get(archetype, "Distinct slate path with diversified HR exposure.")


def build_portfolio(scored_df: pd.DataFrame, config: PortfolioConfig) -> list[dict[str, Any]]:
    if len(scored_df) < config.min_player_pool:
        raise ValueError(
            f"Player pool too small ({len(scored_df)}). Minimum required: {config.min_player_pool}."
        )

    rng = np.random.default_rng(config.random_seed)
    base_df = add_archetype_candidates(scored_df)
    mode_key = str(getattr(config, "selection_mode", "balanced")).strip().lower()
    is_hits_mode = mode_key in {"hits", "hits-tb-combo"}
    is_hits_tb_combo_mode = mode_key == "hits-tb-combo"
    hits_profile_key = str(getattr(config, "hits_profile", "")).strip().lower()
    is_hits_random_mode = is_hits_mode and hits_profile_key == "random"
    is_chalk_city_mode = mode_key == "chalk-city"
    is_random_mode = mode_key == "random"
    focus_col = "combo_score" if is_hits_tb_combo_mode else ("hit_score" if is_hits_mode else "hr_score")

    if is_hits_mode and bool(getattr(config, "hits_filter_enabled", False)):
        min_hit_score = float(getattr(config, "min_hit_score", 0.0))
        min_recent_hits = int(getattr(config, "min_recent_hits", 0))
        min_pitcher_vuln_score = float(getattr(config, "min_pitcher_vuln_score", 0.0))
        min_tb_score = float(getattr(config, "min_tb_score", 0.0))
        min_rbi_score = float(getattr(config, "min_rbi_score", 0.0))
        min_hrr_score = float(getattr(config, "min_hrr_score", 0.0))

        hit_filtered = base_df[
            (base_df["hit_score"] >= min_hit_score)
            & (base_df["recent_hits"] >= min_recent_hits)
            & (base_df["pitcher_vuln_score"] >= min_pitcher_vuln_score)
            & (base_df["tb_score"] >= min_tb_score)
            & (base_df["rbi_score"] >= min_rbi_score)
            & (base_df["hrr_score"] >= min_hrr_score)
        ].copy()

        minimum_needed = max(config.min_player_pool, config.legs_per_slip * 4)
        if len(hit_filtered) >= minimum_needed:
            base_df = hit_filtered
        elif len(hit_filtered) >= max(config.min_player_pool, config.legs_per_slip * 2):
            base_df = hit_filtered

    if getattr(config, "hr_hitter_filter_enabled", True):
        target_floor = float(getattr(config, "min_hr_score", 0.5))
        relax_floor = float(getattr(config, "relax_hr_score_floor", 0.35))
        relax_floor = min(relax_floor, target_floor)
        thresholds = [target_floor]
        if target_floor > relax_floor:
            thresholds.append((target_floor + relax_floor) / 2.0)
            thresholds.append(relax_floor)

        minimum_needed = max(config.min_player_pool, config.legs_per_slip * 4)
        chosen_pool = base_df
        for threshold in thresholds:
            filtered = base_df[base_df[focus_col] >= threshold].copy()
            if len(filtered) >= minimum_needed:
                chosen_pool = filtered
                break
            if len(filtered) > len(chosen_pool):
                chosen_pool = filtered

        if not chosen_pool.empty:
            base_df = chosen_pool

        top_pool_n = int(getattr(config, "hr_candidate_pool_size", 56))
        top_pool_n = max(config.min_player_pool, top_pool_n)
        if len(base_df) > top_pool_n:
            base_df = base_df.sort_values(focus_col, ascending=False).head(top_pool_n).copy()

    max_exposure = config.effective_max_exposure()
    exposure_counts: Counter[str] = Counter()
    game_exposure_counts: Counter[str] = Counter()
    team_exposure_counts: Counter[str] = Counter()
    pairing_counts: Counter[tuple[str, str]] = Counter()

    slips: list[dict[str, Any]] = []

    adaptive_weights = getattr(config, "adaptive_archetype_weights", None)
    if isinstance(adaptive_weights, dict) and adaptive_weights:
        archetype_names = np.array(ARCHETYPES, dtype=object)
        weights = np.array([max(float(adaptive_weights.get(a, 0.0)), 1e-6) for a in ARCHETYPES], dtype=float)
        probs = weights / weights.sum()
        picks = rng.choice(archetype_names, size=config.num_slips, replace=True, p=probs)
        archetype_cycle = [str(p) for p in picks]
    else:
        archetype_cycle = [ARCHETYPES[i % len(ARCHETYPES)] for i in range(config.num_slips)]

    for i in range(config.num_slips):
        target_archetype = archetype_cycle[i]
        selected: list[pd.Series] = []
        force_anchor_slip = config.anchor_every_n_slips > 0 and ((i + 1) % config.anchor_every_n_slips == 0)
        anchor_player_name: str | None = None
        per_leg_hr_floor = float(getattr(config, "min_selected_leg_hr_score", 0.0))
        strict_hr_leg_floor = bool(getattr(config, "strict_hr_leg_floor", False))

        for _ in range(config.legs_per_slip):
            remaining = base_df[
                (~base_df["player_name"].isin([s["player_name"] for s in selected]))
                & (base_df["player_name"].map(exposure_counts).fillna(0) < max_exposure)
            ].copy()

            if remaining.empty:
                break

            if selected:
                selected_games = Counter([row["game_id"] for row in selected])
                selected_teams = Counter([row["team"] for row in selected])

                remaining = remaining[
                    remaining.apply(
                        lambda row: selected_games[row["game_id"]] < config.max_same_game_legs
                        and selected_teams[row["team"]] < config.max_same_team_legs,
                        axis=1,
                    )
                ]

            if remaining.empty:
                break

            if per_leg_hr_floor > 0.0:
                hr_floor_remaining = remaining[remaining[focus_col] >= per_leg_hr_floor].copy()
                if not hr_floor_remaining.empty:
                    remaining = hr_floor_remaining
                elif strict_hr_leg_floor:
                    # In strict mode, relax gradually to preserve board completeness.
                    relaxed = None
                    for mult in (0.9, 0.8, 0.7, 0.6):
                        threshold = per_leg_hr_floor * mult
                        candidate = remaining[remaining[focus_col] >= threshold].copy()
                        if not candidate.empty:
                            relaxed = candidate
                            break
                    if relaxed is not None:
                        remaining = relaxed
                    else:
                        break

            if force_anchor_slip and not selected:
                anchor_candidates = remaining.sort_values(focus_col, ascending=False).head(12).copy()
                anchor_candidates["selection_weight"] = anchor_candidates[focus_col].clip(lower=1e-6)
                pick = _weighted_pick(rng, anchor_candidates, "selection_weight")
                selected.append(pick)
                anchor_player_name = str(pick["player_name"])
                continue

            game_penalty = remaining["game_id"].map(game_exposure_counts).fillna(0).astype(float)
            team_penalty = remaining["team"].map(team_exposure_counts).fillna(0).astype(float)

            pair_penalty_vals = []
            selected_names = sorted([s["player_name"] for s in selected])
            for _, row in remaining.iterrows():
                name = row["player_name"]
                penalty = 0.0
                for existing in selected_names:
                    pair = tuple(sorted((name, existing)))
                    penalty += pairing_counts[pair]
                pair_penalty_vals.append(penalty)

            pair_penalty = pd.Series(pair_penalty_vals, index=remaining.index, dtype=float)
            archetype_boost = _boost_for_archetype(remaining, target_archetype)
            ownership_multiplier = 1.25 - (
                remaining["projected_ownership"].clip(upper=0.95) * config.ownership_penalty_strength
            )

            if is_hits_mode:
                if is_hits_random_mode:
                    random_boost = pd.Series(rng.random(len(remaining)), index=remaining.index, dtype=float)
                    remaining["selection_weight"] = (
                        (0.50 + random_boost * 1.40)
                        * (0.45 + remaining["hit_score"] * 0.90)
                        * (0.40 + remaining["portfolio_value_score"] * 0.65)
                        * (0.80 + archetype_boost * 0.20)
                        * ownership_multiplier
                        / (1.0 + game_penalty * 0.015 + team_penalty * 0.015 + pair_penalty * 0.06)
                    )
                elif is_hits_tb_combo_mode:
                    remaining["selection_weight"] = (
                        remaining["portfolio_value_score"]
                        * (0.28 + remaining["hit_score"] * 1.10)
                        * (0.32 + remaining["tb_score"] * 1.45)
                        * (0.24 + remaining["rbi_score"] * 1.00)
                        * (0.24 + remaining["hrr_score"] * 1.00)
                        * (0.25 + remaining["pitcher_vuln_score"] * 1.05)
                        * archetype_boost
                        * ownership_multiplier
                        / (1.0 + game_penalty * 0.02 + team_penalty * 0.02 + pair_penalty * 0.09)
                    )
                else:
                    remaining["selection_weight"] = (
                        remaining["portfolio_value_score"]
                        * (0.34 + remaining["hit_score"] * 1.90)
                        * (0.30 + remaining["pitcher_vuln_score"] * 1.30)
                        * (0.45 + remaining["recent_hits_score"] * 1.10)
                        * archetype_boost
                        * ownership_multiplier
                        / (1.0 + game_penalty * 0.02 + team_penalty * 0.02 + pair_penalty * 0.10)
                    )
            elif is_random_mode:
                random_boost = pd.Series(rng.random(len(remaining)), index=remaining.index, dtype=float)
                remaining["selection_weight"] = (
                    (0.45 + random_boost * 1.55)
                    * (0.35 + remaining[focus_col] * 0.75)
                    * (0.55 + remaining["portfolio_value_score"] * 0.60)
                    * (0.80 + archetype_boost * 0.20)
                    * ownership_multiplier
                    / (1.0 + game_penalty * 0.02 + team_penalty * 0.02 + pair_penalty * 0.06)
                )
            else:
                game_penalty_mult = 0.03 if is_chalk_city_mode else 0.08
                team_penalty_mult = 0.025 if is_chalk_city_mode else 0.06
                pair_penalty_mult = 0.10 if is_chalk_city_mode else 0.25
                remaining["selection_weight"] = (
                    remaining["portfolio_value_score"]
                    * (0.30 + remaining[focus_col] * 1.80)
                    * archetype_boost
                    * ownership_multiplier
                    / (
                        1.0
                        + game_penalty * game_penalty_mult
                        + team_penalty * team_penalty_mult
                        + pair_penalty * pair_penalty_mult
                    )
                )

            pick = _weighted_pick(rng, remaining, "selection_weight")
            selected.append(pick)

        if len(selected) < config.legs_per_slip:
            # Fallback completion path: keep HR bias but relax strict gating so boards can complete.
            while len(selected) < config.legs_per_slip:
                repair_pool = base_df[
                    (~base_df["player_name"].isin([s["player_name"] for s in selected]))
                    & (base_df["player_name"].map(exposure_counts).fillna(0) < max_exposure)
                ].copy()

                if repair_pool.empty:
                    break

                if selected:
                    selected_games = Counter([row["game_id"] for row in selected])
                    selected_teams = Counter([row["team"] for row in selected])
                    constrained_pool = repair_pool[
                        repair_pool.apply(
                            lambda row: selected_games[row["game_id"]] < config.max_same_game_legs
                            and selected_teams[row["team"]] < config.max_same_team_legs,
                            axis=1,
                        )
                    ]
                    if not constrained_pool.empty:
                        repair_pool = constrained_pool

                if is_hits_mode:
                    if is_hits_random_mode:
                        random_boost = pd.Series(rng.random(len(repair_pool)), index=repair_pool.index, dtype=float)
                        repair_pool["selection_weight"] = (
                            (0.52 + random_boost * 1.35)
                            * (0.45 + repair_pool["hit_score"] * 0.80)
                            * (0.45 + repair_pool["portfolio_value_score"] * 0.60)
                        )
                    elif is_hits_tb_combo_mode:
                        repair_pool["selection_weight"] = (
                            repair_pool["portfolio_value_score"]
                            * (0.35 + repair_pool["tb_score"] * 1.10)
                            * (0.30 + repair_pool["hit_score"] * 0.95)
                            * (0.25 + repair_pool["rbi_score"] * 0.85)
                        )
                    else:
                        repair_pool["selection_weight"] = (
                            repair_pool["portfolio_value_score"]
                            * (0.32 + repair_pool["hit_score"] * 1.60)
                            * (0.35 + repair_pool["pitcher_vuln_score"] * 1.05)
                        )
                elif is_random_mode:
                    random_boost = pd.Series(rng.random(len(repair_pool)), index=repair_pool.index, dtype=float)
                    repair_pool["selection_weight"] = (
                        (0.50 + random_boost * 1.40)
                        * (0.60 + repair_pool["portfolio_value_score"] * 0.55)
                        * (0.45 + repair_pool[focus_col] * 0.50)
                    )
                else:
                    repair_pool["selection_weight"] = (
                        repair_pool["portfolio_value_score"]
                        * (0.30 + repair_pool[focus_col] * 1.50)
                    )
                pick = _weighted_pick(rng, repair_pool, "selection_weight")
                selected.append(pick)

            if len(selected) < config.legs_per_slip:
                continue

        time_buckets = {str(s["start_time_bucket"]).lower() for s in selected}
        if len(time_buckets) < config.min_time_buckets_per_slip:
            fix_candidates = base_df[
                (base_df["player_name"].map(exposure_counts).fillna(0) < max_exposure)
                & (~base_df["player_name"].isin([s["player_name"] for s in selected]))
                & (~base_df["start_time_bucket"].str.lower().isin(time_buckets))
            ].copy()
            if not fix_candidates.empty:
                fix_candidates["selection_weight"] = fix_candidates["portfolio_value_score"]
                replacement = _weighted_pick(rng, fix_candidates, "selection_weight")
                selected[-1] = replacement

        selected_names = [s["player_name"] for s in selected]
        for name in selected_names:
            exposure_counts[name] += 1

        for row in selected:
            game_exposure_counts[row["game_id"]] += 1
            team_exposure_counts[row["team"]] += 1

        sorted_names = sorted(selected_names)
        for a_i in range(len(sorted_names)):
            for b_i in range(a_i + 1, len(sorted_names)):
                pairing_counts[(sorted_names[a_i], sorted_names[b_i])] += 1

        legs = [
            {
                "player_name": s["player_name"],
                "team": s["team"],
                "game_id": s["game_id"],
                "start_time_bucket": s["start_time_bucket"],
                "portfolio_value_score": float(round(s["portfolio_value_score"], 4)),
                "hr_score": float(round(s["hr_score"], 4)),
                "hit_score": float(round(s.get("hit_score", 0.0), 4)),
                "tb_score": float(round(s.get("tb_score", 0.0), 4)),
                "rbi_score": float(round(s.get("rbi_score", 0.0), 4)),
                "hrr_score": float(round(s.get("hrr_score", 0.0), 4)),
                "pitcher_vuln_score": float(round(s.get("pitcher_vuln_score", 0.0), 4)),
                "recent_hits": int(s.get("recent_hits", 0)),
                "leverage_score": float(round(s["leverage_score"], 4)),
                "environment_score": float(round(s["environment_score"], 4)),
                "correlation_score": float(round(s["correlation_score"], 4)),
                "chaos_score": float(round(s["chaos_score"], 4)),
                "projected_ownership": float(round(s["projected_ownership"], 4)),
                "archetype_candidates": s["archetype_candidates"],
            }
            for s in selected
        ]

        slips.append(
            {
                "slip_id": f"WW-{i + 1:03d}",
                "archetype": target_archetype,
                "story": _story_text(target_archetype),
                "avg_portfolio_value": float(round(np.mean([l["portfolio_value_score"] for l in legs]), 4)),
                "anchor_forced": force_anchor_slip,
                "anchor_player_name": anchor_player_name,
                "legs": legs,
            }
        )

    return slips
