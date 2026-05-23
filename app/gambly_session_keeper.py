from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_BET_BUILDER_URL = (
    "https://gambly.com/bet-builder?type=straight%7Cplayer_prop"
    "&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200"
    "&limit=10&sort_by=popularity"
)


class GamblySessionKeeper:
    def __init__(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.status_path = self.data_dir / "gambly_session_status.json"
        self.user_data_dir = Path(
            os.getenv("GAMBLY_USER_DATA_DIR", str(self.data_dir / "gambly_chromium_profile"))
        )
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self.base_url = os.getenv("GAMBLY_BASE_URL", DEFAULT_BET_BUILDER_URL).strip() or DEFAULT_BET_BUILDER_URL
        self.check_interval_seconds = max(30, int(os.getenv("GAMBLY_KEEPALIVE_SECONDS", "300")))
        self.navigation_timeout_ms = max(10000, int(os.getenv("GAMBLY_NAV_TIMEOUT_MS", "60000")))

        self.email = os.getenv("GAMBLY_EMAIL", "").strip()
        self.password = os.getenv("GAMBLY_PASSWORD", "").strip()
        self.email_selector = os.getenv("GAMBLY_EMAIL_SELECTOR", "input[type='email']")
        self.password_selector = os.getenv("GAMBLY_PASSWORD_SELECTOR", "input[type='password']")
        self.submit_selector = os.getenv("GAMBLY_SUBMIT_SELECTOR", "button[type='submit']")

    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _write_status(self, status: str, message: str) -> None:
        payload = {
            "status": status,
            "message": message,
            "checked_at": self._now(),
            "base_url": self.base_url,
            "interval_seconds": self.check_interval_seconds,
            "user_data_dir": str(self.user_data_dir),
        }
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _needs_login(self, page) -> bool:
        url = page.url.lower()
        if "auth/login" in url or "login" in url and "gambly.com" in url:
            return True

        body = page.content().lower()
        if "sign in" in body and "gambly" in body and "returnto=" in body:
            return True

        return False

    def _try_auto_login(self, page) -> bool:
        if not (self.email and self.password):
            return False

        try:
            page.fill(self.email_selector, self.email, timeout=20000)
            page.fill(self.password_selector, self.password, timeout=20000)
            page.click(self.submit_selector, timeout=20000)
            page.wait_for_load_state("domcontentloaded", timeout=self.navigation_timeout_ms)
            page.wait_for_timeout(2000)
            return not self._needs_login(page)
        except Exception:
            return False

    def run_forever(self, headed: bool = False) -> None:
        self._write_status("starting", "Launching Gambly session keeper")

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=not headed,
            )
            page = context.new_page()

            while True:
                try:
                    page.goto(self.base_url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
                    page.wait_for_timeout(1200)

                    if self._needs_login(page):
                        if self._try_auto_login(page):
                            self._write_status("ok", "Session refreshed via auto-login")
                        else:
                            self._write_status(
                                "login_required",
                                "Gambly login required. Log in once in headed mode or set GAMBLY_EMAIL/GAMBLY_PASSWORD.",
                            )
                    else:
                        self._write_status("ok", "Session is active")
                except PlaywrightTimeoutError:
                    self._write_status("error", "Navigation timeout while checking Gambly session")
                except Exception as exc:  # pylint: disable=broad-except
                    self._write_status("error", f"Unexpected keeper error: {exc}")

                time.sleep(self.check_interval_seconds)


def main() -> None:
    headed = os.getenv("GAMBLY_KEEPER_HEADED", "0").strip().lower() in {"1", "true", "yes", "on"}
    keeper = GamblySessionKeeper()
    keeper.run_forever(headed=headed)


if __name__ == "__main__":
    main()
