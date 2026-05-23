from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "weather_warfare.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                run_label TEXT,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archetype_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                archetype TEXT NOT NULL,
                win_flag INTEGER NOT NULL,
                payout_multiple REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playbook_profiles (
                playbook_name TEXT PRIMARY KEY,
                description TEXT,
                mode_filter TEXT NOT NULL DEFAULT 'any',
                hits_profile_filter TEXT NOT NULL DEFAULT 'any',
                risk_filter TEXT NOT NULL DEFAULT 'any',
                book_filter TEXT NOT NULL DEFAULT 'any',
                enabled INTEGER NOT NULL DEFAULT 1,
                base_weight REAL NOT NULL DEFAULT 1.0,
                deep_link_order_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                playbook_name TEXT NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT,
                market_type TEXT,
                book TEXT,
                confidence REAL,
                stake_units REAL,
                odds_price REAL,
                win_flag INTEGER NOT NULL,
                payout_multiple REAL,
                reward_score REAL NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS twitter_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_account TEXT NOT NULL,
                signal_text TEXT NOT NULL,
                signal_type TEXT,
                player_name TEXT,
                team TEXT,
                confidence REAL NOT NULL DEFAULT 0.5,
                occurred_at TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        created_at = datetime.now(tz=timezone.utc).isoformat()
        default_profiles = [
            (
                "pregame_value",
                "Pre-game value edges with conservative sizing.",
                "any",
                "any",
                "safe",
                "any",
                1,
                1.05,
                json.dumps(["exact_prop", "secondary_provider", "search_fallback"]),
                created_at,
                created_at,
            ),
            (
                "news_shock",
                "Lineup/injury driven dislocations with confirmation gates.",
                "any",
                "any",
                "balanced",
                "any",
                1,
                1.00,
                json.dumps(["exact_prop", "secondary_provider", "search_fallback"]),
                created_at,
                created_at,
            ),
            (
                "live_momentum",
                "Live-only momentum entries under strict limits.",
                "any",
                "any",
                "yolo",
                "any",
                1,
                0.90,
                json.dumps(["exact_prop", "secondary_provider", "search_fallback"]),
                created_at,
                created_at,
            ),
            (
                "twitter_screenshot",
                "Extracted screenshot angles mapped to current slate and validated through sportsbook links.",
                "any",
                "any",
                "balanced",
                "any",
                1,
                1.00,
                json.dumps(["exact_prop", "event_link", "search_fallback"]),
                created_at,
                created_at,
            ),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO playbook_profiles (
                playbook_name,
                description,
                mode_filter,
                hits_profile_filter,
                risk_filter,
                book_filter,
                enabled,
                base_weight,
                deep_link_order_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            default_profiles,
        )


def save_run(run_id: str, payload: dict[str, Any], run_label: str | None = None) -> None:
    def _json_default(obj):
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:
                pass
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    created_at = datetime.now(tz=timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_runs (run_id, created_at, run_label, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, created_at, run_label, json.dumps(payload, default=_json_default)),
        )


def fetch_run(run_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM portfolio_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["payload_json"])


def fetch_runs_between(
    start_iso: str | None = None,
    end_iso: str | None = None,
    run_label_prefix: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if start_iso:
        clauses.append("created_at >= ?")
        params.append(str(start_iso))
    if end_iso:
        clauses.append("created_at < ?")
        params.append(str(end_iso))
    if run_label_prefix:
        clauses.append("run_label LIKE ?")
        params.append(f"{run_label_prefix}%")

    query = "SELECT run_id, created_at, run_label, payload_json FROM portfolio_runs"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC"

    with get_conn() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()

    runs: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        runs.append(
            {
                "run_id": str(row["run_id"]),
                "created_at": str(row["created_at"]),
                "run_label": str(row["run_label"] or ""),
                "payload": payload,
            }
        )
    return runs


def fetch_slip_by_id(slip_id: str) -> dict[str, Any] | None:
    target = str(slip_id or "").strip()
    if not target:
        return None

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT run_id, created_at, payload_json FROM portfolio_runs ORDER BY created_at DESC",
        ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        parlays = payload.get("parlays", []) if isinstance(payload, dict) else []
        if not isinstance(parlays, list):
            continue
        for idx, parlay in enumerate(parlays):
            if not isinstance(parlay, dict):
                continue
            if str(parlay.get("slip_id", "")).strip() != target:
                continue
            return {
                "slip_id": target,
                "run_id": str(row["run_id"]),
                "created_at": str(row["created_at"]),
                "slip_index": idx,
                "slip": parlay,
                "run": payload,
            }

    return None
