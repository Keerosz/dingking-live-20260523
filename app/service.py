from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .config import PortfolioConfig
from .exposure import build_exposure_report
from .learning import adaptive_archetype_weights
from .portfolio import build_portfolio
from .reporting import (
    build_chalk_vs_leverage,
    build_pairing_frequency,
    build_portfolio_summary,
    build_rr_survivability_metrics,
    build_time_slot_distribution,
)
from .scoring import add_scores


def generate_portfolio_board(df, config: PortfolioConfig) -> dict[str, Any]:
    scored_df = add_scores(df)
    learning_payload: dict[str, Any] | None = None
    manual_weights = getattr(config, "adaptive_archetype_weights", None)

    if getattr(config, "adaptive_learning_enabled", True):
        learning_payload = adaptive_archetype_weights(
            lookback_days=int(getattr(config, "learning_lookback_days", 45)),
            min_samples=int(getattr(config, "learning_min_samples", 8)),
        )
        learned_weights = dict(learning_payload["weights"])
        if isinstance(manual_weights, dict) and manual_weights:
            merged_weights: dict[str, float] = {}
            for archetype, weight in learned_weights.items():
                manual_multiplier = float(manual_weights.get(archetype, 1.0))
                merged_weights[archetype] = max(float(weight) * manual_multiplier, 1e-6)
            total = sum(merged_weights.values())
            if total > 0:
                merged_weights = {
                    archetype: value / total for archetype, value in merged_weights.items()
                }
            learning_payload["weights"] = merged_weights
            learning_payload.setdefault("meta", {})["manual_adjustment_applied"] = True
            setattr(config, "adaptive_archetype_weights", merged_weights)
        else:
            setattr(config, "adaptive_archetype_weights", learned_weights)
    elif isinstance(manual_weights, dict) and manual_weights:
        setattr(config, "adaptive_archetype_weights", manual_weights)

    run_id = str(uuid4())
    parlays = build_portfolio(scored_df, config)
    for idx, parlay in enumerate(parlays):
        if isinstance(parlay, dict):
            parlay["slip_id"] = f"{run_id}-S{idx + 1:02d}"

    max_exposure = config.effective_max_exposure()
    exposure_report = build_exposure_report(parlays, max_exposure)
    summary = build_portfolio_summary(parlays, len(scored_df), max_exposure, exposure_report)

    if exposure_report["violations"]:
        raise RuntimeError(
            "Hard exposure rule violated. Generated portfolio includes players above max exposure."
        )

    archetype_counter = Counter([p["archetype"] for p in parlays])

    output = {
        "run_id": run_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "config": config.model_dump(),
        "summary": summary,
        "exposure_report": exposure_report,
        "parlays": parlays,
        "archetype_tags": dict(archetype_counter),
        "pairing_frequency": build_pairing_frequency(parlays),
        "time_slot_distribution": build_time_slot_distribution(parlays),
        "rr_survivability_metrics": build_rr_survivability_metrics(parlays),
        "chalk_vs_leverage_balance": build_chalk_vs_leverage(parlays),
        "learning": learning_payload
        if learning_payload is not None
        else {
            "weights": {a: round(1.0 / max(len(archetype_counter), 1), 6) for a in archetype_counter},
            "diagnostics": [],
            "meta": {"adaptive_learning_enabled": False},
        },
    }
    return output
