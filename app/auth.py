from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class PropFinderAuthManager:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def _login_if_needed(self, page) -> None:
        current_url = page.url.lower()
        if "login" not in current_url and "sign in" not in page.content().lower():
            return

        email = os.getenv("PROPFINDER_EMAIL", "").strip()
        password = os.getenv("PROPFINDER_PASSWORD", "").strip()
        if not email or not password:
            raise RuntimeError(
                "Authentication required. Set PROPFINDER_EMAIL and PROPFINDER_PASSWORD or run login bootstrap."
            )

        email_selector = os.getenv("PROPFINDER_EMAIL_SELECTOR", "input[type='email']")
        password_selector = os.getenv("PROPFINDER_PASSWORD_SELECTOR", "input[type='password']")
        submit_selector = os.getenv("PROPFINDER_SUBMIT_SELECTOR", "button[type='submit']")

        try:
            page.fill(email_selector, email, timeout=20000)
            page.fill(password_selector, password, timeout=20000)
            page.click(submit_selector, timeout=20000)
            page.wait_for_load_state("domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Auto re-login timed out while interacting with login form. "
                "Set custom selectors via PROPFINDER_*_SELECTOR env vars if UI changed."
            ) from exc

        if "login" in page.url.lower() or "sign in" in page.content().lower():
            raise RuntimeError("Auto re-login failed. Verify credentials and optional selectors.")

    def fetch_authenticated_payload(self, url: str, headed: bool = False) -> dict[str, Any]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                cmd = [
                    sys.executable,
                    "-m",
                    "app.playwright_fetch",
                    "--url",
                    url,
                    "--state",
                    str(self.state_path),
                    "--output-json",
                    str(tmp_path),
                    "--debug-dir",
                    str(self.state_path.parent),
                ]
                if headed:
                    cmd.append("--headed")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    msg = (result.stderr or result.stdout or "playwright subprocess failed").strip()
                    raise RuntimeError(msg)
                return json.loads(tmp_path.read_text(encoding="utf-8"))
            finally:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

        # On Windows under certain server loop policies, Playwright cannot spawn
        # its browser subprocess unless the Proactor policy is active.
        if os.name == "nt" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context_kwargs = {}
            if self.state_path.exists():
                context_kwargs["storage_state"] = str(self.state_path)
            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
            except PlaywrightTimeoutError as exc:
                browser.close()
                raise RuntimeError(f"Timed out loading PropFinder: {exc}") from exc

            try:
                self._login_if_needed(page)
                context.storage_state(path=str(self.state_path))
                html = page.content()
                browser.close()
                return {
                    "status": "ok",
                    "message": "Fetched page content with Playwright.",
                    "source": "playwright",
                    "records": [],
                    "html": html,
                    "debug_html_path": str(self.state_path.parent / "debug_propfinder.html"),
                    "debug_screenshot_path": str(self.state_path.parent / "debug_propfinder.png"),
                }
            except Exception as exc:
                browser.close()
                msg = str(exc).strip() or repr(exc)
                raise RuntimeError(f"Playwright auth fetch failed: {msg}") from exc

    def bootstrap_interactive_login(self, url: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            print("Complete login in the opened browser window, then press Enter here.")
            input()
            context.storage_state(path=str(self.state_path))
            browser.close()
