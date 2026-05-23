from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .archetypes import ARCHETYPES
from .db import fetch_runs_between, init_db


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _coerce_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(str(raw))
    return datetime.now(tz=timezone.utc).date()


def _load_outcomes(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    winners: set[str] = set()

    if isinstance(payload, dict):
        if isinstance(payload.get("winning_players"), list):
            for item in payload["winning_players"]:
                key = _norm_name(str(item))
                if key:
                    winners.add(key)

        player_results = payload.get("player_results")
        if isinstance(player_results, dict):
            for player_name, result in player_results.items():
                won = False
                if isinstance(result, dict):
                    won = bool(result.get("won") or result.get("hit") or result.get("cash"))
                else:
                    won = bool(result)
                if won:
                    key = _norm_name(str(player_name))
                    if key:
                        winners.add(key)

        settled = payload.get("settled_players")
        if isinstance(settled, list):
            for item in settled:
                if not isinstance(item, dict):
                    continue
                if not bool(item.get("won") or item.get("hit") or item.get("cash")):
                    continue
                key = _norm_name(str(item.get("player_name") or item.get("name") or ""))
                if key:
                    winners.add(key)

    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                key = _norm_name(item)
                if key:
                    winners.add(key)
            elif isinstance(item, dict) and bool(item.get("won") or item.get("hit") or item.get("cash")):
                key = _norm_name(str(item.get("player_name") or item.get("name") or ""))
                if key:
                    winners.add(key)

    return winners


def _run_window(target_date: date) -> tuple[str, str]:
    start_dt = datetime.combine(target_date, time.min, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat()


def _payload_config(payload: dict[str, Any], run_label: str) -> dict[str, Any]:
    config = payload.get("config") if isinstance(payload, dict) else None
    config_dict = config if isinstance(config, dict) else {}
    selection_mode = str(config_dict.get("selection_mode") or payload.get("mode") or "balanced").strip().lower()
    risk_level = str(config_dict.get("risk_level") or payload.get("risk_level") or "balanced").strip().lower()
    hits_profile = str(config_dict.get("hits_profile") or payload.get("hits_profile") or "high-frequency").strip().lower()
    if selection_mode == "balanced" and run_label.startswith("dashboard_"):
        parts = run_label.split("_")
        if len(parts) >= 2:
            selection_mode = str(parts[1]).strip().lower() or selection_mode
    return {
        "selection_mode": selection_mode,
        "risk_level": risk_level,
        "hits_profile": hits_profile,
        "legs_per_slip": int(config_dict.get("legs_per_slip") or payload.get("summary", {}).get("legs_per_slip") or 0),
    }


def _evaluate_run(run: dict[str, Any], winning_players: set[str]) -> dict[str, Any]:
    payload = run.get("payload", {}) if isinstance(run, dict) else {}
    parlays = payload.get("parlays", []) if isinstance(payload, dict) else []
    config = _payload_config(payload, str(run.get("run_label") or ""))

    slip_results: list[dict[str, Any]] = []
    archetype_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"appearances": 0, "full_hits": 0, "near_misses": 0})
    player_hits: Counter[str] = Counter()
    player_dead: Counter[str] = Counter()
    pair_dead: Counter[tuple[str, str]] = Counter()

    full_hits = 0
    near_miss_1 = 0
    near_miss_2 = 0
    matched_total = 0

    for slip in parlays if isinstance(parlays, list) else []:
        if not isinstance(slip, dict):
            continue
        legs = slip.get("legs", [])
        if not isinstance(legs, list) or not legs:
            continue

        leg_names: list[str] = []
        matched = 0
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            player_name = str(leg.get("player_name") or "").strip()
            if not player_name:
                continue
            leg_names.append(player_name)
            if _norm_name(player_name) in winning_players:
                matched += 1
                player_hits[player_name] += 1
            else:
                player_dead[player_name] += 1

        total_legs = len(leg_names)
        if total_legs <= 0:
            continue

        matched_total += matched
        full_hit = matched >= total_legs
        miss_distance = total_legs - matched
        if full_hit:
            full_hits += 1
        elif miss_distance == 1:
            near_miss_1 += 1
        elif miss_distance == 2:
            near_miss_2 += 1

        archetype = str(slip.get("archetype") or "Controlled Chaos")
        archetype_stats[archetype]["appearances"] += 1
        if full_hit:
            archetype_stats[archetype]["full_hits"] += 1
        elif miss_distance <= 2:
            archetype_stats[archetype]["near_misses"] += 1

        if not full_hit:
            ordered_names = sorted(set(leg_names))
            for idx, left in enumerate(ordered_names):
                for right in ordered_names[idx + 1 :]:
                    pair_dead[(left, right)] += 1

        slip_results.append(
            {
                "slip_id": str(slip.get("slip_id") or ""),
                "archetype": archetype,
                "legs": total_legs,
                "matched_legs": matched,
                "full_hit": full_hit,
                "miss_distance": miss_distance,
            }
        )

    total_slips = len(slip_results)
    dead_slips = max(total_slips - full_hits - near_miss_1 - near_miss_2, 0)
    return {
        "run_id": str(run.get("run_id") or ""),
        "created_at": str(run.get("created_at") or ""),
        "run_label": str(run.get("run_label") or ""),
        "mode": config["selection_mode"],
        "risk_level": config["risk_level"],
        "hits_profile": config["hits_profile"],
        "legs_per_slip": config["legs_per_slip"],
        "total_slips": total_slips,
        "full_hits": full_hits,
        "near_miss_1": near_miss_1,
        "near_miss_2": near_miss_2,
        "dead_slips": dead_slips,
        "avg_matched_legs": round((matched_total / total_slips), 4) if total_slips else 0.0,
        "archetype_stats": dict(archetype_stats),
        "player_hits": dict(player_hits),
        "player_dead": dict(player_dead),
        "pair_dead": {f"{left} + {right}": count for (left, right), count in pair_dead.items()},
    }


def _bucket_metrics(run_results: list[dict[str, Any]], field: str, values: list[str]) -> dict[str, dict[str, Any]]:
    metrics = {
        value: {
            "slips": 0,
            "full_hits": 0,
            "near_misses": 0,
            "avg_matched_legs": 0.0,
        }
        for value in values
    }
    matched_sums: Counter[str] = Counter()

    for result in run_results:
        key = str(result.get(field) or "").strip().lower()
        if key not in metrics:
            metrics[key] = {"slips": 0, "full_hits": 0, "near_misses": 0, "avg_matched_legs": 0.0}
        metrics[key]["slips"] += int(result.get("total_slips") or 0)
        metrics[key]["full_hits"] += int(result.get("full_hits") or 0)
        metrics[key]["near_misses"] += int(result.get("near_miss_1") or 0) + int(result.get("near_miss_2") or 0)
        matched_sums[key] += float(result.get("avg_matched_legs") or 0.0) * int(result.get("total_slips") or 0)

    for key, item in metrics.items():
        slips = int(item["slips"])
        item["hit_rate"] = round(item["full_hits"] / slips, 4) if slips else 0.0
        item["near_miss_rate"] = round(item["near_misses"] / slips, 4) if slips else 0.0
        item["avg_matched_legs"] = round(matched_sums[key] / slips, 4) if slips else 0.0
    return metrics


def _aggregate_archetypes(run_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats = {
        archetype: {"appearances": 0, "full_hits": 0, "near_misses": 0, "win_signal": 0.0}
        for archetype in ARCHETYPES
    }
    for result in run_results:
        archetype_stats = result.get("archetype_stats", {})
        if not isinstance(archetype_stats, dict):
            continue
        for archetype, values in archetype_stats.items():
            if archetype not in stats:
                stats[archetype] = {"appearances": 0, "full_hits": 0, "near_misses": 0, "win_signal": 0.0}
            if not isinstance(values, dict):
                continue
            stats[archetype]["appearances"] += int(values.get("appearances") or 0)
            stats[archetype]["full_hits"] += int(values.get("full_hits") or 0)
            stats[archetype]["near_misses"] += int(values.get("near_misses") or 0)

    for archetype, values in stats.items():
        appearances = int(values["appearances"])
        if appearances:
            values["win_signal"] = round(
                ((values["full_hits"] * 1.0) + (values["near_misses"] * 0.35)) / appearances,
                4,
            )
    return stats


def _build_adjustments(summary: dict[str, Any], mode_metrics: dict[str, dict[str, Any]], risk_metrics: dict[str, dict[str, Any]], archetype_metrics: dict[str, dict[str, Any]], run_results: list[dict[str, Any]]) -> dict[str, Any]:
    mode_weights = {
        "balanced": 1.0,
        "cream": 1.0,
        "extreme-cream": 1.0,
        "chalk-city": 1.0,
        "hits": 1.0,
        "hits-tb-combo": 1.0,
        "random": 1.0,
    }
    risk_weights = {"safe": 1.0, "balanced": 1.0, "yolo": 1.0}
    max_player_exposure = 4
    ownership_penalty_delta = 0.0
    preferred_leg_counts = [2, 3, 4]
    archetype_weight_deltas = {archetype: 0.0 for archetype in ARCHETYPES}
    notes: list[str] = []

    full_hit_rate = float(summary.get("full_hit_rate") or 0.0)
    near_miss_rate = float(summary.get("near_miss_rate") or 0.0)

    if near_miss_rate >= 0.30 and full_hit_rate <= 0.08:
        risk_weights["safe"] += 0.10
        risk_weights["yolo"] -= 0.10
        mode_weights["hits"] += 0.10
        mode_weights["hits-tb-combo"] += 0.10
        mode_weights["extreme-cream"] -= 0.10
        notes.append("Near misses were high; shorten slip shape and bias toward mixed hit/TB paths.")

    if full_hit_rate <= 0.05 and near_miss_rate <= 0.12:
        mode_weights["random"] -= 0.20
        mode_weights["balanced"] += 0.10
        notes.append("Read quality was weak; reduce experimental volume and lean on core mixes.")

    if mode_metrics.get("chalk-city", {}).get("hit_rate", 0.0) > full_hit_rate + 0.05:
        mode_weights["chalk-city"] += 0.10
        ownership_penalty_delta -= 0.08
        notes.append("Chalk-heavy slips outperformed; soften ownership penalty slightly.")

    if mode_metrics.get("hits", {}).get("hit_rate", 0.0) > mode_metrics.get("balanced", {}).get("hit_rate", 0.0):
        mode_weights["hits"] += 0.10
        mode_weights["balanced"] -= 0.05

    if risk_metrics.get("safe", {}).get("hit_rate", 0.0) > risk_metrics.get("yolo", {}).get("hit_rate", 0.0):
        risk_weights["safe"] += 0.05
        risk_weights["yolo"] -= 0.05

    dead_counter: Counter[str] = Counter()
    for result in run_results:
        dead_counter.update(result.get("player_dead", {}))
    if dead_counter:
        top_dead_player, top_dead_count = dead_counter.most_common(1)[0]
        total_slips = max(int(summary.get("total_slips") or 0), 1)
        if top_dead_count / total_slips >= 0.30:
            max_player_exposure = 3
            notes.append(f"{top_dead_player} repeated too often in dead slips; tighten max exposure.")

    ranked_archetypes = sorted(
        archetype_metrics.items(),
        key=lambda item: float(item[1].get("win_signal") or 0.0),
        reverse=True,
    )
    for archetype, _values in ranked_archetypes[:2]:
        archetype_weight_deltas[archetype] = min(archetype_weight_deltas.get(archetype, 0.0) + 0.10, 0.30)
    for archetype, _values in ranked_archetypes[-2:]:
        archetype_weight_deltas[archetype] = max(archetype_weight_deltas.get(archetype, 0.0) - 0.10, -0.30)

    mode_weights = {key: round(max(0.40, min(1.40, value)), 4) for key, value in mode_weights.items()}
    risk_weights = {key: round(max(0.40, min(1.40, value)), 4) for key, value in risk_weights.items()}

    return {
        "mode_weights": mode_weights,
        "risk_weights": risk_weights,
        "max_player_exposure": max_player_exposure,
        "preferred_leg_counts": preferred_leg_counts,
        "ownership_penalty_delta": round(ownership_penalty_delta, 4),
        "archetype_weight_deltas": archetype_weight_deltas,
        "notes": notes,
    }


def _build_report(summary: dict[str, Any], mode_metrics: dict[str, dict[str, Any]], risk_metrics: dict[str, dict[str, Any]], archetype_metrics: dict[str, dict[str, Any]], adjustments: dict[str, Any]) -> str:
    lines = [
        "# Hermes Nightly Report",
        f"Date: {summary['date']}",
        f"Runs analyzed: {summary['runs_analyzed']}",
        f"Total slips: {summary['total_slips']}",
        "",
        "## Topline",
        f"- Full hits: {summary['full_hits']}",
        f"- Near misses by 1 leg: {summary['near_miss_1']}",
        f"- Near misses by 2 legs: {summary['near_miss_2']}",
        f"- Dead slips: {summary['dead_slips']}",
        f"- Full-hit rate: {summary['full_hit_rate']:.2%}",
        f"- Near-miss rate: {summary['near_miss_rate']:.2%}",
        f"- Avg matched legs per slip: {summary['avg_matched_legs']:.2f}",
        "",
        "## By Mode",
        "| Mode | Slips | Full Hits | Near Misses | Hit Rate | Near-Miss Rate | Avg Matched Legs |",
        "|------|------:|----------:|------------:|---------:|---------------:|-----------------:|",
    ]
    for mode, values in mode_metrics.items():
        lines.append(
            f"| {mode} | {values['slips']} | {values['full_hits']} | {values['near_misses']} | {values['hit_rate']:.2%} | {values['near_miss_rate']:.2%} | {values['avg_matched_legs']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## By Risk",
            "| Risk | Slips | Full Hits | Near Misses | Hit Rate | Near-Miss Rate |",
            "|------|------:|----------:|------------:|---------:|---------------:|",
        ]
    )
    for risk, values in risk_metrics.items():
        lines.append(
            f"| {risk} | {values['slips']} | {values['full_hits']} | {values['near_misses']} | {values['hit_rate']:.2%} | {values['near_miss_rate']:.2%} |"
        )

    lines.extend(
        [
            "",
            "## By Archetype",
            "| Archetype | Appearances | Full Hits | Near Misses | Win Signal |",
            "|-----------|------------:|----------:|------------:|-----------:|",
        ]
    )
    for archetype, values in archetype_metrics.items():
        lines.append(
            f"| {archetype} | {values['appearances']} | {values['full_hits']} | {values['near_misses']} | {values['win_signal']:.4f} |"
        )

    lines.extend(["", "## Recommended Next-Day Changes"])
    lines.append(f"- Max player exposure: {adjustments['max_player_exposure']}")
    lines.append(f"- Ownership penalty delta: {adjustments['ownership_penalty_delta']:+.2f}")
    lines.append(f"- Preferred leg counts: {', '.join(str(x) for x in adjustments['preferred_leg_counts'])}")
    if adjustments.get("notes"):
        for note in adjustments["notes"]:
            lines.append(f"- {note}")

    return "\n".join(lines) + "\n"


def run_nightly(target_date: date, outcomes_path: Path, output_dir: Path | None = None) -> dict[str, Any]:
    init_db()
    winners = _load_outcomes(outcomes_path)
    start_iso, end_iso = _run_window(target_date)
    runs = fetch_runs_between(start_iso=start_iso, end_iso=end_iso)
    run_results = [_evaluate_run(run, winners) for run in runs]

    summary = {
        "date": target_date.isoformat(),
        "runs_analyzed": len(run_results),
        "total_slips": sum(int(item.get("total_slips") or 0) for item in run_results),
        "full_hits": sum(int(item.get("full_hits") or 0) for item in run_results),
        "near_miss_1": sum(int(item.get("near_miss_1") or 0) for item in run_results),
        "near_miss_2": sum(int(item.get("near_miss_2") or 0) for item in run_results),
        "dead_slips": sum(int(item.get("dead_slips") or 0) for item in run_results),
    }
    total_slips = max(summary["total_slips"], 1)
    summary["full_hit_rate"] = round(summary["full_hits"] / total_slips, 4)
    summary["near_miss_rate"] = round((summary["near_miss_1"] + summary["near_miss_2"]) / total_slips, 4)
    summary["avg_matched_legs"] = round(
        sum(float(item.get("avg_matched_legs") or 0.0) * int(item.get("total_slips") or 0) for item in run_results) / total_slips,
        4,
    )

    mode_metrics = _bucket_metrics(
        run_results,
        "mode",
        ["balanced", "cream", "extreme-cream", "chalk-city", "hits", "hits-tb-combo", "random"],
    )
    risk_metrics = _bucket_metrics(run_results, "risk_level", ["safe", "balanced", "yolo"])
    archetype_metrics = _aggregate_archetypes(run_results)
    adjustments = _build_adjustments(summary, mode_metrics, risk_metrics, archetype_metrics, run_results)

    payload = {
        "date": target_date.isoformat(),
        "summary": summary,
        "mode_metrics": mode_metrics,
        "risk_metrics": risk_metrics,
        "archetype_metrics": archetype_metrics,
        "adjustments": adjustments,
    }

    if output_dir is None:
        output_dir = Path(__file__).resolve().parent.parent / "data" / "hermes"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"nightly_{target_date.isoformat()}.md"
    json_path = output_dir / f"nightly_{target_date.isoformat()}.json"
    latest_path = output_dir / "latest_adjustments.json"

    report_path.write_text(_build_report(summary, mode_metrics, risk_metrics, archetype_metrics, adjustments), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "json_path": str(json_path),
        "latest_path": str(latest_path),
        "runs_analyzed": summary["runs_analyzed"],
        "total_slips": summary["total_slips"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Hermes nightly evaluation job.")
    parser.add_argument("--date", dest="run_date", help="UTC date to analyze in YYYY-MM-DD format.")
    parser.add_argument("--outcomes", required=True, help="Path to outcomes JSON containing winning players.")
    parser.add_argument("--output-dir", help="Optional output directory for Hermes reports.")
    args = parser.parse_args()

    target_date = _coerce_date(args.run_date)
    outcomes_path = Path(args.outcomes)
    output_dir = Path(args.output_dir) if args.output_dir else None
    result = run_nightly(target_date=target_date, outcomes_path=outcomes_path, output_dir=output_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()