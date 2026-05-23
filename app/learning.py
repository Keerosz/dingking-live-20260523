from __future__ import annotations

import sqlite3
import math
import json
from datetime import datetime, timezone
from typing import Any

from .archetypes import ARCHETYPES
from .db import get_conn


DASHBOARD_CATEGORIES = ("hr", "hits", "tb", "rbi", "hrr", "value", "matchup", "recent")
DASHBOARD_SUBCATEGORIES = ("overall", "value", "safe", "contrarian")


def record_archetype_outcome(run_id: str, archetype: str, win_flag: bool, payout_multiple: float | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO archetype_results (run_id, archetype, win_flag, payout_multiple, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                archetype,
                1 if win_flag else 0,
                payout_multiple,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )


def archetype_performance_snapshot() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                archetype,
                COUNT(*) AS samples,
                AVG(win_flag) AS win_rate,
                AVG(COALESCE(payout_multiple, 0.0)) AS avg_payout_multiple
            FROM archetype_results
            GROUP BY archetype
            ORDER BY win_rate DESC, avg_payout_multiple DESC
            """
        ).fetchall()

    return [
        {
            "archetype": r["archetype"],
            "samples": int(r["samples"]),
            "win_rate": round(float(r["win_rate"]), 4) if r["win_rate"] is not None else 0.0,
            "avg_payout_multiple": round(float(r["avg_payout_multiple"]), 4)
            if r["avg_payout_multiple"] is not None
            else 0.0,
        }
        for r in rows
    ]


def adaptive_archetype_weights(
    lookback_days: int = 45,
    min_samples: int = 8,
    prior_samples: float = 12.0,
    prior_win_rate: float = 0.50,
    recency_half_life_days: float = 21.0,
) -> dict[str, Any]:
    cutoff_iso = datetime.now(tz=timezone.utc)
    cutoff_iso = cutoff_iso.replace(microsecond=0)
    cutoff_ts = cutoff_iso.timestamp() - (lookback_days * 86400)

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT archetype, win_flag, COALESCE(payout_multiple, 0.0) AS payout_multiple, created_at
            FROM archetype_results
            """
        ).fetchall()

    agg: dict[str, dict[str, float]] = {
        a: {"samples_w": 0.0, "wins_w": 0.0, "payout_w": 0.0} for a in ARCHETYPES
    }
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    for row in rows:
        archetype = str(row["archetype"])
        if archetype not in agg:
            continue
        created_at = row["created_at"]
        try:
            created_dt = datetime.fromisoformat(str(created_at))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        created_ts = created_dt.timestamp()
        if created_ts < cutoff_ts:
            continue

        age_days = max(0.0, (now_ts - created_ts) / 86400.0)
        recency_w = math.pow(0.5, age_days / max(recency_half_life_days, 1.0))

        agg[archetype]["samples_w"] += recency_w
        agg[archetype]["wins_w"] += recency_w * float(row["win_flag"])
        agg[archetype]["payout_w"] += recency_w * float(row["payout_multiple"])

    scores: dict[str, float] = {}
    diagnostics: list[dict[str, Any]] = []
    min_samples_f = float(max(1, min_samples))

    for archetype in ARCHETYPES:
        samples_w = agg[archetype]["samples_w"]
        wins_w = agg[archetype]["wins_w"]
        payout_w = agg[archetype]["payout_w"]

        shrunk_win = (wins_w + prior_win_rate * prior_samples) / max(samples_w + prior_samples, 1e-9)
        shrunk_payout = (payout_w + 0.0 * prior_samples) / max(samples_w + prior_samples, 1e-9)
        confidence = min(1.0, samples_w / min_samples_f)

        # Win rate drives most of the weight; payout nudges but does not dominate.
        raw_score = (0.82 * shrunk_win) + (0.18 * max(shrunk_payout, 0.0))
        blended = (confidence * raw_score) + ((1.0 - confidence) * 0.5)
        scores[archetype] = max(0.05, blended)

        diagnostics.append(
            {
                "archetype": archetype,
                "samples_weighted": round(samples_w, 4),
                "confidence": round(confidence, 4),
                "shrunk_win_rate": round(shrunk_win, 4),
                "shrunk_avg_payout": round(shrunk_payout, 4),
                "allocation_score": round(blended, 4),
            }
        )

    total = sum(scores.values())
    if total <= 1e-12:
        equal = 1.0 / len(ARCHETYPES)
        weights = {a: equal for a in ARCHETYPES}
    else:
        weights = {a: (scores[a] / total) for a in ARCHETYPES}

    diagnostics.sort(key=lambda x: x["allocation_score"], reverse=True)
    return {
        "weights": {k: round(float(v), 6) for k, v in weights.items()},
        "diagnostics": diagnostics,
        "meta": {
            "lookback_days": lookback_days,
            "min_samples": min_samples,
            "prior_samples": prior_samples,
            "recency_half_life_days": recency_half_life_days,
        },
    }


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _default_reward(win_flag: bool, payout_multiple: float | None, confidence: float, stake_units: float) -> float:
    payout = float(payout_multiple) if payout_multiple is not None else (1.0 if win_flag else 0.0)
    raw_return = payout - 1.0
    confidence_penalty = max(0.0, _clamp01(confidence) - 0.8) * 0.12
    size_penalty = max(0.0, float(stake_units) - 1.0) * 0.03
    return raw_return - confidence_penalty - size_penalty


def record_decision_outcome(
    playbook_name: str,
    category: str,
    win_flag: bool,
    run_id: str | None = None,
    subcategory: str | None = None,
    market_type: str | None = None,
    book: str | None = None,
    confidence: float = 0.5,
    stake_units: float = 1.0,
    odds_price: float | None = None,
    payout_multiple: float | None = None,
    reward_score: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    playbook = str(playbook_name or "").strip().lower()
    category_key = str(category or "").strip().lower()
    if not playbook:
        raise ValueError("playbook_name is required")
    if not category_key:
        raise ValueError("category is required")

    conf = _clamp01(float(confidence))
    stake = max(0.0, float(stake_units))
    computed_reward = (
        float(reward_score)
        if reward_score is not None
        else _default_reward(bool(win_flag), payout_multiple, conf, stake)
    )
    payload = metadata if isinstance(metadata, dict) else {}

    created_at = datetime.now(tz=timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO decision_outcomes (
                run_id,
                playbook_name,
                category,
                subcategory,
                market_type,
                book,
                confidence,
                stake_units,
                odds_price,
                win_flag,
                payout_multiple,
                reward_score,
                metadata_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                playbook,
                category_key,
                (str(subcategory).strip().lower() if subcategory else None),
                (str(market_type).strip().lower() if market_type else None),
                (str(book).strip().lower() if book else None),
                conf,
                stake,
                float(odds_price) if odds_price is not None else None,
                1 if win_flag else 0,
                float(payout_multiple) if payout_multiple is not None else None,
                computed_reward,
                json.dumps(payload),
                created_at,
            ),
        )

    return {
        "playbook_name": playbook,
        "category": category_key,
        "reward_score": round(computed_reward, 6),
        "recorded_at": created_at,
    }


def category_performance_snapshot(lookback_days: int = 60, min_samples: int = 5) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                category,
                COALESCE(subcategory, 'overall') AS subcategory,
                COALESCE(market_type, 'unknown') AS market_type,
                COUNT(*) AS samples,
                AVG(win_flag) AS win_rate,
                AVG(reward_score) AS avg_reward,
                AVG(confidence) AS avg_confidence,
                AVG(COALESCE(payout_multiple, 0.0)) AS avg_payout_multiple
            FROM decision_outcomes
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY category, COALESCE(subcategory, 'overall'), COALESCE(market_type, 'unknown')
            HAVING COUNT(*) >= ?
            ORDER BY avg_reward DESC, win_rate DESC, samples DESC
            """,
            (f"-{int(max(1, lookback_days))} day", int(max(1, min_samples))),
        ).fetchall()

    return [
        {
            "category": str(r["category"]),
            "subcategory": str(r["subcategory"]),
            "market_type": str(r["market_type"]),
            "samples": int(r["samples"]),
            "win_rate": round(float(r["win_rate"] or 0.0), 4),
            "avg_reward": round(float(r["avg_reward"] or 0.0), 4),
            "avg_confidence": round(float(r["avg_confidence"] or 0.0), 4),
            "avg_payout_multiple": round(float(r["avg_payout_multiple"] or 0.0), 4),
        }
        for r in rows
    ]


def adaptive_category_weights(
    lookback_days: int = 60,
    min_samples: int = 5,
    prior_samples: float = 10.0,
    prior_win_rate: float = 0.50,
    prior_reward: float = 0.0,
    recency_half_life_days: float = 21.0,
) -> dict[str, Any]:
    cutoff_ts = datetime.now(tz=timezone.utc).timestamp() - (max(1, int(lookback_days)) * 86400)
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                category,
                COALESCE(subcategory, 'overall') AS subcategory,
                win_flag,
                COALESCE(reward_score, 0.0) AS reward_score,
                created_at
            FROM decision_outcomes
            """
        ).fetchall()

    category_agg: dict[str, dict[str, float]] = {
        key: {"samples_w": 0.0, "wins_w": 0.0, "reward_w": 0.0} for key in DASHBOARD_CATEGORIES
    }
    subcategory_agg: dict[str, dict[str, float]] = {
        key: {"samples_w": 0.0, "wins_w": 0.0, "reward_w": 0.0} for key in DASHBOARD_SUBCATEGORIES
    }

    for row in rows:
        category = str(row["category"] or "").strip().lower()
        subcategory = str(row["subcategory"] or "overall").strip().lower()
        if category not in category_agg:
            continue
        if subcategory not in subcategory_agg:
            subcategory = "overall"
        try:
            created_dt = datetime.fromisoformat(str(row["created_at"]))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        created_ts = created_dt.timestamp()
        if created_ts < cutoff_ts:
            continue

        age_days = max(0.0, (now_ts - created_ts) / 86400.0)
        recency_w = math.pow(0.5, age_days / max(recency_half_life_days, 1.0))
        win_flag = float(row["win_flag"] or 0.0)
        reward_score = float(row["reward_score"] or 0.0)

        for bucket in (category_agg[category], subcategory_agg[subcategory]):
            bucket["samples_w"] += recency_w
            bucket["wins_w"] += recency_w * win_flag
            bucket["reward_w"] += recency_w * reward_score

    def _build_weights(agg: dict[str, dict[str, float]]) -> tuple[dict[str, float], list[dict[str, Any]]]:
        weights: dict[str, float] = {}
        diagnostics: list[dict[str, Any]] = []
        target_samples = float(max(1, min_samples))

        for key, values in agg.items():
            samples_w = float(values["samples_w"])
            wins_w = float(values["wins_w"])
            reward_w = float(values["reward_w"])
            shrunk_win = (wins_w + (prior_win_rate * prior_samples)) / max(samples_w + prior_samples, 1e-9)
            shrunk_reward = (reward_w + (prior_reward * prior_samples)) / max(samples_w + prior_samples, 1e-9)
            confidence = min(1.0, samples_w / target_samples)
            reward_component = _clamp01(0.5 + max(-0.35, min(0.35, shrunk_reward)))
            blended_score = (shrunk_win * 0.62) + (reward_component * 0.38)
            multiplier = 1.0 + ((blended_score - 0.5) * 0.70 * confidence)
            multiplier = max(0.75, min(1.25, multiplier))
            weights[key] = round(float(multiplier), 6)
            diagnostics.append(
                {
                    "key": key,
                    "samples_weighted": round(samples_w, 4),
                    "confidence": round(confidence, 4),
                    "shrunk_win_rate": round(shrunk_win, 4),
                    "shrunk_reward": round(shrunk_reward, 4),
                    "multiplier": round(multiplier, 4),
                }
            )

        diagnostics.sort(key=lambda item: item["multiplier"], reverse=True)
        return weights, diagnostics

    category_weights, category_diagnostics = _build_weights(category_agg)
    subcategory_weights, subcategory_diagnostics = _build_weights(subcategory_agg)

    return {
        "categories": category_weights,
        "subcategories": subcategory_weights,
        "diagnostics": {
            "categories": category_diagnostics,
            "subcategories": subcategory_diagnostics,
        },
        "meta": {
            "lookback_days": int(max(1, lookback_days)),
            "min_samples": int(max(1, min_samples)),
            "prior_samples": float(prior_samples),
            "recency_half_life_days": float(recency_half_life_days),
        },
    }


def playbook_performance_snapshot(lookback_days: int = 60, min_samples: int = 5) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                playbook_name,
                COUNT(*) AS samples,
                AVG(win_flag) AS win_rate,
                AVG(reward_score) AS avg_reward,
                AVG(confidence) AS avg_confidence,
                AVG(COALESCE(payout_multiple, 0.0)) AS avg_payout_multiple
            FROM decision_outcomes
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY playbook_name
            HAVING COUNT(*) >= ?
            ORDER BY avg_reward DESC, win_rate DESC, samples DESC
            """,
            (f"-{int(max(1, lookback_days))} day", int(max(1, min_samples))),
        ).fetchall()

    return [
        {
            "playbook_name": str(r["playbook_name"]),
            "samples": int(r["samples"]),
            "win_rate": round(float(r["win_rate"] or 0.0), 4),
            "avg_reward": round(float(r["avg_reward"] or 0.0), 4),
            "avg_confidence": round(float(r["avg_confidence"] or 0.0), 4),
            "avg_payout_multiple": round(float(r["avg_payout_multiple"] or 0.0), 4),
        }
        for r in rows
    ]


def recommend_playbook(
    mode: str = "balanced",
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    book: str = "fanduel",
    lookback_days: int = 60,
) -> dict[str, Any]:
    mode_key = str(mode or "balanced").strip().lower()
    profile_key = str(hits_profile or "high-frequency").strip().lower()
    risk_key = str(risk_level or "balanced").strip().lower()
    book_key = str(book or "fanduel").strip().lower()

    with get_conn() as conn:
        profiles = conn.execute(
            """
            SELECT
                playbook_name,
                description,
                mode_filter,
                hits_profile_filter,
                risk_filter,
                book_filter,
                enabled,
                base_weight,
                deep_link_order_json
            FROM playbook_profiles
            WHERE enabled = 1
            """
        ).fetchall()

        perf_rows = conn.execute(
            """
            SELECT
                playbook_name,
                COUNT(*) AS samples,
                AVG(reward_score) AS avg_reward,
                AVG(win_flag) AS win_rate
            FROM decision_outcomes
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY playbook_name
            """,
            (f"-{int(max(1, lookback_days))} day",),
        ).fetchall()

    perf_by_playbook = {
        str(r["playbook_name"]): {
            "samples": int(r["samples"]),
            "avg_reward": float(r["avg_reward"] or 0.0),
            "win_rate": float(r["win_rate"] or 0.0),
        }
        for r in perf_rows
    }

    candidates: list[dict[str, Any]] = []
    for row in profiles:
        p_name = str(row["playbook_name"])
        mode_filter = str(row["mode_filter"] or "any").lower()
        profile_filter = str(row["hits_profile_filter"] or "any").lower()
        risk_filter = str(row["risk_filter"] or "any").lower()
        book_filter = str(row["book_filter"] or "any").lower()

        if mode_filter != "any" and mode_filter != mode_key:
            continue
        if profile_filter != "any" and profile_filter != profile_key:
            continue
        if risk_filter != "any" and risk_filter != risk_key:
            continue
        if book_filter != "any" and book_filter != book_key:
            continue

        perf = perf_by_playbook.get(p_name, {"samples": 0, "avg_reward": 0.0, "win_rate": 0.5})
        samples = int(perf["samples"])
        confidence = min(1.0, samples / 25.0)
        base_weight = float(row["base_weight"] or 1.0)
        perf_score = (0.75 * float(perf["avg_reward"])) + (0.25 * (float(perf["win_rate"]) - 0.5))
        final_score = (base_weight * 0.35) + (confidence * perf_score)

        candidates.append(
            {
                "playbook_name": p_name,
                "description": str(row["description"] or ""),
                "deep_link_order": json.loads(str(row["deep_link_order_json"] or "[]")),
                "samples": samples,
                "avg_reward": round(float(perf["avg_reward"]), 4),
                "win_rate": round(float(perf["win_rate"]), 4),
                "score": round(float(final_score), 6),
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    chosen = candidates[0] if candidates else None
    return {
        "selected": chosen,
        "candidates": candidates,
        "context": {
            "mode": mode_key,
            "hits_profile": profile_key,
            "risk_level": risk_key,
            "book": book_key,
            "lookback_days": int(max(1, lookback_days)),
        },
    }


def record_twitter_signal(
    source_account: str,
    signal_text: str,
    signal_type: str | None = None,
    player_name: str | None = None,
    team: str | None = None,
    confidence: float = 0.5,
    occurred_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = str(source_account or "").strip().lower()
    text = str(signal_text or "").strip()
    if not source:
        raise ValueError("source_account is required")
    if not text:
        raise ValueError("signal_text is required")

    payload = metadata if isinstance(metadata, dict) else {}
    occurred = (occurred_at or datetime.now(tz=timezone.utc)).isoformat()
    created_at = datetime.now(tz=timezone.utc).isoformat()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO twitter_signals (
                source_account,
                signal_text,
                signal_type,
                player_name,
                team,
                confidence,
                occurred_at,
                metadata_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                text,
                (str(signal_type).strip().lower() if signal_type else None),
                (str(player_name).strip() if player_name else None),
                (str(team).strip().upper() if team else None),
                _clamp01(confidence),
                occurred,
                json.dumps(payload),
                created_at,
            ),
        )

    return {
        "source_account": source,
        "signal_type": str(signal_type or "").strip().lower() or None,
        "occurred_at": occurred,
        "recorded_at": created_at,
    }


def twitter_signal_snapshot(hours: int = 24, limit: int = 100) -> dict[str, Any]:
    window_hours = int(max(1, hours))
    max_rows = int(max(1, min(500, limit)))

    with get_conn() as conn:
        recent_rows = conn.execute(
            """
            SELECT
                source_account,
                signal_text,
                COALESCE(signal_type, 'unknown') AS signal_type,
                player_name,
                team,
                confidence,
                occurred_at,
                created_at
            FROM twitter_signals
            WHERE datetime(created_at) >= datetime('now', ?)
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (f"-{window_hours} hour", max_rows),
        ).fetchall()

        feature_rows = conn.execute(
            """
            SELECT
                COALESCE(signal_type, 'unknown') AS signal_type,
                COUNT(*) AS samples,
                AVG(confidence) AS avg_confidence,
                COUNT(DISTINCT source_account) AS unique_sources
            FROM twitter_signals
            WHERE datetime(created_at) >= datetime('now', ?)
            GROUP BY COALESCE(signal_type, 'unknown')
            ORDER BY samples DESC, avg_confidence DESC
            """,
            (f"-{window_hours} hour",),
        ).fetchall()

    recent = [
        {
            "source_account": str(r["source_account"]),
            "signal_text": str(r["signal_text"]),
            "signal_type": str(r["signal_type"]),
            "player_name": str(r["player_name"] or ""),
            "team": str(r["team"] or ""),
            "confidence": round(float(r["confidence"] or 0.0), 4),
            "occurred_at": str(r["occurred_at"]),
            "created_at": str(r["created_at"]),
        }
        for r in recent_rows
    ]
    features = [
        {
            "signal_type": str(r["signal_type"]),
            "samples": int(r["samples"]),
            "avg_confidence": round(float(r["avg_confidence"] or 0.0), 4),
            "unique_sources": int(r["unique_sources"]),
        }
        for r in feature_rows
    ]
    return {
        "window_hours": window_hours,
        "recent": recent,
        "features": features,
        "note": "Twitter signals are stored for context features and confirmation. Core learning should rely on settled outcome data.",
    }
