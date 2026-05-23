from __future__ import annotations

import argparse
import json
import os
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def _login_if_needed(page) -> None:
    current_url = page.url.lower()
    if "login" not in current_url and "sign in" not in page.content().lower():
        return

    email = os.getenv("PROPFINDER_EMAIL", "").strip()
    password = os.getenv("PROPFINDER_PASSWORD", "").strip()
    if not email or not password:
        raise RuntimeError(
            "Authentication required. Set PROPFINDER_EMAIL and PROPFINDER_PASSWORD or refresh storage state."
        )

    email_selector = os.getenv("PROPFINDER_EMAIL_SELECTOR", "input[type='email']")
    password_selector = os.getenv("PROPFINDER_PASSWORD_SELECTOR", "input[type='password']")
    submit_selector = os.getenv("PROPFINDER_SUBMIT_SELECTOR", "button[type='submit']")

    page.fill(email_selector, email, timeout=20000)
    page.fill(password_selector, password, timeout=20000)
    page.click(submit_selector, timeout=20000)
    page.wait_for_load_state("domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)

    if "login" in page.url.lower() or "sign in" in page.content().lower():
        raise RuntimeError("Auto re-login failed. Verify credentials and selectors.")


def _parse_tables_from_html(html: str) -> list[dict[str, Any]]:
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return []

    records: list[dict[str, Any]] = []
    for table_idx, df in enumerate(tables):
        for row in df.to_dict(orient="records"):
            row_dict = {str(k): ("" if pd.isna(v) else v) for k, v in row.items()}
            row_dict["_source"] = "html_table"
            row_dict["_table_index"] = table_idx
            records.append(row_dict)
    return records


def _extract_balanced_array(text: str, start_idx: int) -> str | None:
    depth = 0
    in_string = False
    escape = False

    for i in range(start_idx, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]

    return None


def _extract_embedded_records(html: str) -> tuple[list[dict[str, Any]], str | None]:
    keys = ["bvpSlateData", "cheatsheetData", "slateData", "propsData"]
    for key in keys:
        for marker in (f'"{key}":[', f'\\"{key}\\":['):
            idx = html.find(marker)
            if idx < 0:
                continue

            arr_start = html.find("[", idx)
            if arr_start < 0:
                continue

            arr_text = _extract_balanced_array(html, arr_start)
            if not arr_text:
                continue

            parse_candidates = [
                arr_text,
                arr_text.replace('\\"', '"').replace("\\n", ""),
            ]

            for candidate in parse_candidates:
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue

                if isinstance(parsed, list):
                    records: list[dict[str, Any]] = []
                    for row in parsed:
                        if isinstance(row, dict):
                            row_dict = dict(row)
                            row_dict["_source"] = "embedded_json"
                            row_dict["_payload_key"] = key
                            records.append(row_dict)
                    if records:
                        return records, key
    return [], None


def _extract_dom_fallback_records(page) -> list[dict[str, Any]]:
    # Collect visible row/card-like content as a fallback when HTML tables are absent.
    records = page.evaluate(
        """
        () => {
            const selectors = [
                'table tbody tr',
                '[role="row"][data-id]',
                '[data-rowindex]',
                '.player-row',
                '.player-card',
                '.cheatsheet-row',
                '.MuiDataGrid-row'
            ];

            const isVisible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };

            const unique = new Set();
            const out = [];

            for (const selector of selectors) {
                const nodes = Array.from(document.querySelectorAll(selector));
                for (const node of nodes) {
                    if (!isVisible(node)) continue;

                    // Skip high-level containers that include nested row nodes.
                    const nestedRow = node.querySelector('tbody tr, [role="row"][data-id], [data-rowindex], .player-row, .player-card, .cheatsheet-row, .MuiDataGrid-row');
                    if (nestedRow) continue;

                    const cellEls = Array.from(node.querySelectorAll('td, th, [role="cell"], .cell'));
                    const cells = cellEls
                        .map(c => (c.innerText || '').trim())
                        .filter(Boolean);

                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!text) continue;
                    if (text.length > 500) continue;

                    const key = cells.length ? cells.join('|') : text;
                    if (unique.has(key)) continue;
                    unique.add(key);

                    const rec = { _source: 'dom_fallback', raw_text: text };
                    if (cells.length) {
                        for (let i = 0; i < cells.length; i++) {
                            rec[`col_${i + 1}`] = cells[i];
                        }
                    }
                    out.push(rec);
                }
            }
            return out;
        }
        """
    )
    return records if isinstance(records, list) else []


def _first_nonempty(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _dedupe_player_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []

    for row in records:
        safe_row = {str(k): v for k, v in row.items()}
        name = _first_nonempty(safe_row, ["batterName", "player_name", "player", "name", "col_1"])
        team = _first_nonempty(safe_row, ["batterTeamCode", "team", "tm", "col_2"])
        source = str(safe_row.get("_source", "unknown"))

        if name and team:
            key = (name.lower(), team.upper(), source)
        else:
            key = (json.dumps(safe_row, sort_keys=True, default=str), "", source)

        if key in seen:
            continue
        seen.add(key)
        deduped.append(safe_row)

    return deduped


def fetch_records(
    url: str,
    state_path: Path,
    output_json_path: Path,
    debug_dir: Path,
    headed: bool = False,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)

    debug_html_path = debug_dir / "debug_propfinder.html"
    debug_screenshot_path = debug_dir / "debug_propfinder.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context_kwargs = {}
        if state_path.exists():
            context_kwargs["storage_state"] = str(state_path)

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                # Continue even if network never fully idles due to long polling.
                page.wait_for_timeout(1500)

            _login_if_needed(page)

            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1500)

            context.storage_state(path=str(state_path))
            html = page.content()
            debug_html_path.write_text(html, encoding="utf-8")
            page.screenshot(path=str(debug_screenshot_path), full_page=True)

            table_records = _parse_tables_from_html(html)
            if table_records:
                table_records = _dedupe_player_records(table_records)
                payload = {
                    "status": "ok",
                    "message": f"Extracted {len(table_records)} records from HTML tables.",
                    "source": "tables",
                    "records": table_records,
                    "debug_html_path": str(debug_html_path),
                    "debug_screenshot_path": str(debug_screenshot_path),
                }
            else:
                embedded_records, embedded_key = _extract_embedded_records(html)
                if embedded_records:
                    embedded_records = _dedupe_player_records(embedded_records)
                    payload = {
                        "status": "ok",
                        "message": f"No HTML tables found. Extracted {len(embedded_records)} records from embedded payload '{embedded_key}'.",
                        "source": "embedded_json",
                        "records": embedded_records,
                        "debug_html_path": str(debug_html_path),
                        "debug_screenshot_path": str(debug_screenshot_path),
                    }
                else:
                    dom_records = _extract_dom_fallback_records(page)
                    if dom_records:
                        dom_records = _dedupe_player_records(dom_records)
                        payload = {
                            "status": "ok",
                            "message": f"No HTML tables found. Extracted {len(dom_records)} records from DOM fallback.",
                            "source": "dom_fallback",
                            "records": dom_records,
                            "debug_html_path": str(debug_html_path),
                            "debug_screenshot_path": str(debug_screenshot_path),
                        }
                    else:
                        payload = {
                            "status": "empty",
                            "message": "No tables, embedded payload records, or visible DOM rows/cards were found on the page.",
                            "source": "none",
                            "records": [],
                            "debug_html_path": str(debug_html_path),
                            "debug_screenshot_path": str(debug_screenshot_path),
                        }

            output_json_path.write_text(json.dumps(payload), encoding="utf-8")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Playwright timeout: {exc}") from exc
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--debug-dir", required=True)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    fetch_records(
        url=args.url,
        state_path=Path(args.state),
        output_json_path=Path(args.output_json),
        debug_dir=Path(args.debug_dir),
        headed=args.headed,
    )


if __name__ == "__main__":
    main()
