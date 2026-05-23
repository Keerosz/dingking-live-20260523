from __future__ import annotations

import os
import threading
import json
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .auth import PropFinderAuthManager
from .config import PortfolioConfig
from .db import save_run
from .normalize import normalize_to_weather_warfare
from .service import generate_portfolio_board


class AgentState:
    def __init__(self) -> None:
        self.running = False
        self.last_sync_at: str | None = None
        self.last_status: str = "idle"
        self.last_message: str = ""
        self.last_rows: int = 0
        self.last_run_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_sync_at": self.last_sync_at,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "last_rows": self.last_rows,
            "last_run_id": self.last_run_id,
        }


class RealTimeResearchAgents:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.raw_path = data_dir / "propfinder_raw_latest.csv"
        self.normalized_path = data_dir / "propfinder_normalized_latest.csv"
        self.debug_raw_json_path = data_dir / "debug_raw_players.json"
        self.debug_normalized_json_path = data_dir / "debug_normalized_players.json"
        self.storage_state_path = data_dir / "propfinder_storage_state.json"
        self.auth_manager = PropFinderAuthManager(state_path=self.storage_state_path)
        self.state = AgentState()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def _fetch_with_cookie(self, url: str) -> dict[str, Any]:
        url = os.getenv("PROPFINDER_CHEATSHEET_URL", "https://propfinder.app/mlb/cheatsheets")
        cookie = os.getenv("PROPFINDER_SESSION_COOKIE", "").strip()
        if not cookie:
            raise RuntimeError("PROPFINDER_SESSION_COOKIE is not set.")

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Cookie": cookie,
        }

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        if "login" in response.url.lower() or "sign in" in response.text.lower():
            raise RuntimeError("Cookie auth appears expired or unauthorized.")

        try:
            tables = pd.read_html(StringIO(response.text))
        except ValueError:
            tables = []

        if not tables:
            return {
                "status": "empty",
                "message": "No tables found on PropFinder page using cookie mode.",
                "df": pd.DataFrame(),
                "source": "cookie",
                "debug_html_path": str(self.data_dir / "debug_propfinder.html"),
                "debug_screenshot_path": str(self.data_dir / "debug_propfinder.png"),
            }

        combined = pd.concat(tables, axis=0, ignore_index=True)
        if combined.empty:
            return {
                "status": "empty",
                "message": "Parsed tables were empty in cookie mode.",
                "df": pd.DataFrame(),
                "source": "cookie",
                "debug_html_path": str(self.data_dir / "debug_propfinder.html"),
                "debug_screenshot_path": str(self.data_dir / "debug_propfinder.png"),
            }

        return {
            "status": "ok",
            "message": f"Extracted {len(combined)} rows from cookie-mode tables.",
            "df": combined,
            "source": "cookie",
            "debug_html_path": str(self.data_dir / "debug_propfinder.html"),
            "debug_screenshot_path": str(self.data_dir / "debug_propfinder.png"),
        }

    def _fetch_with_playwright_state(self, url: str) -> dict[str, Any]:
        headed = os.getenv("PROPFINDER_HEADED", "0").strip() == "1"
        payload = self.auth_manager.fetch_authenticated_payload(url=url, headed=headed)
        records = payload.get("records", [])
        message = str(payload.get("message", ""))
        status = str(payload.get("status", "ok")).lower()
        source = str(payload.get("source", "playwright"))

        if not isinstance(records, list):
            records = []

        df = pd.DataFrame(records)
        if status == "empty" or df.empty:
            return {
                "status": "empty",
                "message": message or "No records found via Playwright extraction.",
                "df": pd.DataFrame(),
                "source": source,
                "debug_html_path": payload.get("debug_html_path"),
                "debug_screenshot_path": payload.get("debug_screenshot_path"),
            }

        return {
            "status": "ok",
            "message": message or f"Extracted {len(df)} records via Playwright.",
            "df": df,
            "source": source,
            "debug_html_path": payload.get("debug_html_path"),
            "debug_screenshot_path": payload.get("debug_screenshot_path"),
        }

    def _fetch_propfinder_tables(self) -> dict[str, Any]:
        url = os.getenv("PROPFINDER_CHEATSHEET_URL", "https://propfinder.app/mlb/cheatsheets")
        mode = os.getenv("PROPFINDER_AUTH_MODE", "playwright").strip().lower()

        if mode == "cookie":
            return self._fetch_with_cookie(url)

        try:
            return self._fetch_with_playwright_state(url)
        except Exception as exc:
            # Fallback to cookie mode if available.
            if os.getenv("PROPFINDER_SESSION_COOKIE", "").strip():
                return self._fetch_with_cookie(url)
            raise RuntimeError(f"Playwright auth mode failed: {exc}") from exc

    def run_once(self) -> dict[str, Any]:
        self.state.last_status = "running"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fetch_result = self._fetch_propfinder_tables()
            df_raw = fetch_result["df"]

            if df_raw.empty:
                msg = fetch_result.get("message", "No records found during sync.")
                self.debug_raw_json_path.write_text("[]", encoding="utf-8")
                self.debug_normalized_json_path.write_text("[]", encoding="utf-8")
                self.state.last_sync_at = datetime.now(tz=timezone.utc).isoformat()
                self.state.last_status = "empty"
                self.state.last_message = str(msg)
                self.state.last_rows = 0
                self.state.last_run_id = None
                return {
                    "status": "empty",
                    "message": str(msg),
                    "rows": 0,
                    "source": fetch_result.get("source"),
                    "debug_html_path": fetch_result.get("debug_html_path"),
                    "debug_screenshot_path": fetch_result.get("debug_screenshot_path"),
                    "debug_raw_players_path": str(self.debug_raw_json_path),
                    "debug_normalized_players_path": str(self.debug_normalized_json_path),
                }

            df_raw.to_csv(self.raw_path, index=False)
            raw_records = [
                {str(k): v for k, v in row.items()} for row in df_raw.to_dict(orient="records")
            ]
            self.debug_raw_json_path.write_text(
                json.dumps(raw_records, default=str),
                encoding="utf-8",
            )

            df_norm = normalize_to_weather_warfare(df_raw)
            norm_records = [
                {str(k): v for k, v in row.items()} for row in df_norm.to_dict(orient="records")
            ]
            self.debug_normalized_json_path.write_text(
                json.dumps(norm_records, default=str),
                encoding="utf-8",
            )
            if len(df_norm) < 40:
                msg = f"Normalized player pool too small: {len(df_norm)}. Need at least 40."
                self.state.last_sync_at = datetime.now(tz=timezone.utc).isoformat()
                self.state.last_status = "insufficient_pool"
                self.state.last_message = msg
                self.state.last_rows = len(df_norm)
                self.state.last_run_id = None
                return {
                    "status": "insufficient_pool",
                    "message": msg,
                    "rows": len(df_norm),
                    "source": fetch_result.get("source"),
                    "debug_html_path": fetch_result.get("debug_html_path"),
                    "debug_screenshot_path": fetch_result.get("debug_screenshot_path"),
                    "debug_raw_players_path": str(self.debug_raw_json_path),
                    "debug_normalized_players_path": str(self.debug_normalized_json_path),
                }

            df_norm.to_csv(self.normalized_path, index=False)

            config = PortfolioConfig(
                num_slips=int(os.getenv("WW_NUM_SLIPS", "14")),
                legs_per_slip=int(os.getenv("WW_LEGS_PER_SLIP", "4")),
                min_player_pool=int(os.getenv("WW_MIN_PLAYER_POOL", "40")),
                max_player_exposure=2,
                allow_override_max_exposure=False,
            )

            board = generate_portfolio_board(df_norm, config)
            save_run(board["run_id"], board, run_label="realtime_agent")

            self.state.last_sync_at = datetime.now(tz=timezone.utc).isoformat()
            self.state.last_status = "ok"
            self.state.last_message = "Sync and portfolio generation succeeded."
            self.state.last_rows = len(df_norm)
            self.state.last_run_id = board["run_id"]

            return {
                "status": "ok",
                "rows": len(df_norm),
                "message": fetch_result.get("message", "Sync and portfolio generation succeeded."),
                "source": fetch_result.get("source"),
                "raw_path": str(self.raw_path),
                "normalized_path": str(self.normalized_path),
                "run_id": board["run_id"],
                "debug_html_path": fetch_result.get("debug_html_path"),
                "debug_screenshot_path": fetch_result.get("debug_screenshot_path"),
                "debug_raw_players_path": str(self.debug_raw_json_path),
                "debug_normalized_players_path": str(self.debug_normalized_json_path),
            }
        except Exception as exc:
            self.state.last_sync_at = datetime.now(tz=timezone.utc).isoformat()
            self.state.last_status = "error"
            self.state.last_message = str(exc)
            raise

    def _loop(self, every_seconds: int) -> None:
        self.state.running = True
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # pylint: disable=broad-except
                self.state.last_sync_at = datetime.now(tz=timezone.utc).isoformat()
                self.state.last_status = "error"
                self.state.last_message = str(exc)

            if self._stop_event.wait(timeout=every_seconds):
                break
        self.state.running = False

    def start(self, every_seconds: int) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(every_seconds,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
