from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests


def _presence(value: str) -> str:
    return "PRESENT" if value else "MISSING"


def _safe_request(
    method: str,
    url: str,
    *,
    timeout_seconds: int = 8,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = requests.request(method=method, url=url, json=json_body, timeout=timeout_seconds)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text[:2000]}
        return {
            "ok": True,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "payload": payload,
        }
    except requests.exceptions.Timeout as exc:
        return {
            "ok": False,
            "status_code": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "payload": None,
            "error": f"timeout: {exc}",
            "timeout": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "payload": None,
            "error": str(exc),
            "timeout": False,
        }


def _flatten_links(payload: dict[str, Any]) -> list[str]:
    links: list[str] = []
    slip_links = payload.get("slip_links")
    if isinstance(slip_links, list):
        for slip in slip_links:
            if not isinstance(slip, list):
                continue
            for item in slip:
                if isinstance(item, str) and item.strip():
                    links.append(item.strip())
    if not links:
        top_links = payload.get("links")
        if isinstance(top_links, list):
            for item in top_links:
                if isinstance(item, str) and item.strip():
                    links.append(item.strip())
    return links


def _unresolved_players(payload: dict[str, Any], players: list[str]) -> list[str]:
    unresolved: list[str] = []
    slip_links = payload.get("slip_links")
    first_slip = slip_links[0] if isinstance(slip_links, list) and slip_links else []
    for idx, player in enumerate(players):
        link = None
        if isinstance(first_slip, list) and idx < len(first_slip):
            link = first_slip[idx]
        if not isinstance(link, str) or not link.strip():
            unresolved.append(player)
    return unresolved


def _analyze_resolve(
    *,
    server_reachable: bool,
    request_result: dict[str, Any],
    players: list[str],
) -> dict[str, Any]:
    payload = request_result.get("payload") if isinstance(request_result.get("payload"), dict) else {}
    response_keys = sorted(payload.keys())
    links = _flatten_links(payload)
    unresolved = _unresolved_players(payload, players)
    return {
        "server_reachable": server_reachable,
        "status_code": request_result.get("status_code"),
        "elapsed_ms": request_result.get("elapsed_ms"),
        "response_keys": response_keys,
        "link_count": len(links),
        "fallback_count": int(payload.get("fallback_links") or 0),
        "unresolved_players": unresolved,
        "error_message": request_result.get("error") or payload.get("detail"),
        "cache_status": {
            "cache_used": payload.get("cache_used"),
            "cache_age_seconds": payload.get("cache_age_seconds"),
        },
        "provider_status": payload.get("provider_status"),
        "timeout_status": payload.get("timeout_status")
        or ("triggered" if request_result.get("timeout") else "not_triggered"),
        "exact_failure_stage": payload.get("exact_failure_stage"),
        "fallback_reason": payload.get("fallback_reason"),
        "raw_payload": payload,
    }


def main() -> int:
    base_url = os.getenv("DINGKING_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    env_info = {
        "THE_ODDS_API_KEY": _presence(os.getenv("THE_ODDS_API_KEY", "")),
        "WW_LINK_AGENT_AUTO_START": os.getenv("WW_LINK_AGENT_AUTO_START", "1") == "1",
    }

    health = _safe_request("GET", f"{base_url}/health", timeout_seconds=6)
    server_reachable = bool(health.get("ok") and int(health.get("status_code") or 0) < 500)

    link_agent_health = _safe_request("GET", f"{base_url}/dashboard/link-agent/health", timeout_seconds=6)
    link_agent_payload = link_agent_health.get("payload") if isinstance(link_agent_health.get("payload"), dict) else {}
    active_mode = str(link_agent_payload.get("mode") or "unknown")
    if not link_agent_health.get("ok") and link_agent_health.get("status_code") == 404:
        active_mode = "unknown_endpoint_missing"

    players = ["Aaron Judge", "Juan Soto"]
    slips = [{"legs": [{"player_name": p} for p in players]}]

    fanduel_result = _safe_request(
        "POST",
        f"{base_url}/dashboard/sportsbook/resolve-links",
        timeout_seconds=10,
        json_body={"book": "fanduel", "slips": slips},
    )
    draftkings_result = _safe_request(
        "POST",
        f"{base_url}/dashboard/sportsbook/resolve-links",
        timeout_seconds=10,
        json_body={"book": "draftkings", "slips": slips},
    )
    actionnetwork_result = _safe_request(
        "POST",
        f"{base_url}/dashboard/sportsbook/resolve-links",
        timeout_seconds=10,
        json_body={"book": "actionnetwork", "slips": slips},
    )

    fanduel_diag = _analyze_resolve(server_reachable=server_reachable, request_result=fanduel_result, players=players)
    draftkings_diag = _analyze_resolve(server_reachable=server_reachable, request_result=draftkings_result, players=players)
    actionnetwork_diag = _analyze_resolve(server_reachable=server_reachable, request_result=actionnetwork_result, players=players)

    report = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url,
        "environment": env_info,
        "server_reachable": server_reachable,
        "link_agent_health_endpoint": {
            "status_code": link_agent_health.get("status_code"),
            "exists": not (link_agent_health.get("status_code") == 404),
            "mode": active_mode,
            "response_keys": sorted(link_agent_payload.keys()) if isinstance(link_agent_payload, dict) else [],
            "error_message": link_agent_health.get("error") or link_agent_payload.get("detail"),
        },
        "tests": {
            "fanduel": fanduel_diag,
            "draftkings": draftkings_diag,
            "actionnetwork": actionnetwork_diag,
        },
    }

    report_path = data_dir / "debug_links_report.json"
    fanduel_path = data_dir / "debug_links_response_fanduel.json"
    draftkings_path = data_dir / "debug_links_response_draftkings.json"
    actionnetwork_path = data_dir / "debug_links_response_actionnetwork.json"

    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    fanduel_path.write_text(json.dumps(fanduel_diag["raw_payload"], indent=2), encoding="utf-8")
    draftkings_path.write_text(json.dumps(draftkings_diag["raw_payload"], indent=2), encoding="utf-8")
    actionnetwork_path.write_text(json.dumps(actionnetwork_diag["raw_payload"], indent=2), encoding="utf-8")

    print("=== DINGKING LINK DIAGNOSTIC REPORT ===")
    print(f"base_url: {base_url}")
    print(f"server_reachable: {server_reachable}")
    print(f"THE_ODDS_API_KEY: {env_info['THE_ODDS_API_KEY']}")
    print(f"link_agent_enabled: {env_info['WW_LINK_AGENT_AUTO_START']}")
    print(f"active_mode: {active_mode}")
    print("--- endpoint: /dashboard/link-agent/health ---")
    print(f"status_code: {link_agent_health.get('status_code')}")
    print(f"response_keys: {report['link_agent_health_endpoint']['response_keys']}")
    if report["link_agent_health_endpoint"]["error_message"]:
        print(f"error_message: {report['link_agent_health_endpoint']['error_message']}")

    for book_name, diag in (("fanduel", fanduel_diag), ("draftkings", draftkings_diag), ("actionnetwork", actionnetwork_diag)):
        print(f"--- endpoint: /dashboard/sportsbook/resolve-links [{book_name}] ---")
        print(f"status_code: {diag['status_code']}")
        print(f"response_keys: {diag['response_keys']}")
        print(f"link_count: {diag['link_count']}")
        print(f"fallback_count: {diag['fallback_count']}")
        print(f"unresolved_players: {diag['unresolved_players']}")
        print(f"cache_status: {diag['cache_status']}")
        print(f"provider_status: {diag['provider_status']}")
        print(f"timeout_status: {diag['timeout_status']}")
        print(f"exact_failure_stage: {diag['exact_failure_stage']}")
        print(f"fallback_reason: {diag['fallback_reason']}")
        if diag["error_message"]:
            print(f"error_message: {diag['error_message']}")

    print("--- files ---")
    print(str(report_path))
    print(str(fanduel_path))
    print(str(draftkings_path))
    print(str(actionnetwork_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
