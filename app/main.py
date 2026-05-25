from __future__ import annotations

import os
import logging
import secrets
import html
import json
from collections import deque
from pathlib import Path
from threading import Event, Lock, Thread
from datetime import datetime, timezone
import re
import time
from urllib.parse import quote_plus, urlparse, parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
import requests

from .agents import RealTimeResearchAgents
from .archetypes import ARCHETYPES
from .config import PortfolioConfig
from .db import fetch_run, fetch_slip_by_id, init_db, save_run
from .ingest import load_slate_from_csv, load_slate_from_records
from .learning import (
    adaptive_archetype_weights,
    adaptive_category_weights,
    archetype_performance_snapshot,
    category_performance_snapshot,
    playbook_performance_snapshot,
    record_archetype_outcome,
    record_decision_outcome,
    record_twitter_signal,
    recommend_playbook,
    twitter_signal_snapshot,
)
from .models import (
    DecisionOutcomeRequest,
    GeneratePortfolioRequest,
    GeneratePortfolioRequestFile,
    SlipHitEstimateRequest,
    TwitterScreenshotPlaybookRequest,
    TwitterSignalRequest,
)
from .scoring import add_scores
from .service import generate_portfolio_board
from .gambly_links import GAMBLY_BET_BUILDER_BASE, build_gambly_link
from .deeplink_layer import (
    BOOK_LABELS,
    BOOK_ONE_CLICK_CAPABILITY,
    build_route_plan,
    build_standardized_slip,
    filter_books_for_region,
    normalize_book_key,
)

app = FastAPI(title="DINGKING", version="0.1.0")
agents = RealTimeResearchAgents(data_dir=Path(__file__).resolve().parent.parent / "data")
ROOT_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("dingking.links")


class DashboardState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.parlays: list[dict] = []
        self.latest_run_id: str | None = None
        self.generated_at: str | None = None
        self.next_replace_index: int = 0
        self.mode: str = "balanced"
        self.legs_per_slip: int = 4
        self.hits_profile: str = "high-frequency"
        self.risk_level: str = "balanced"
        self.lineup_locked_only: bool = False
        self.allow_live: bool = False
        self.source_csv_mtime: float | None = None

    def snapshot(self) -> dict:
        return {
            "parlays": self.parlays,
            "count": len(self.parlays),
            "latest_run_id": self.latest_run_id,
            "generated_at": self.generated_at,
            "next_replace_index": self.next_replace_index,
            "mode": self.mode,
            "legs_per_slip": self.legs_per_slip,
            "hits_profile": self.hits_profile,
            "risk_level": self.risk_level,
            "lineup_locked_only": self.lineup_locked_only,
            "allow_live": self.allow_live,
            "source_csv_mtime": self.source_csv_mtime,
        }


dashboard_state = DashboardState()

STARTED_GAME_CACHE_TTL_SECONDS = 120
_started_game_cache: dict[int, tuple[float, bool]] = {}
_started_game_cache_lock = Lock()

FINAL_GAME_CACHE_TTL_SECONDS = 120
_final_game_cache: dict[int, tuple[float, bool]] = {}
_final_game_cache_lock = Lock()

_rank_adjustments_lock = Lock()
_rank_adjustments_cache: dict[str, dict[str, float] | dict[str, object]] | None = None
_rank_adjustments_last_source_mtime: float | None = None
_rank_adjustments_last_slate_complete: bool = False
_rank_adjustments_last_reason: str = "never"
_rank_adjustments_last_refreshed_at: str | None = None

GAMBLY_REDIRECT_TTL_SECONDS = 3600
_gambly_redirect_cache: dict[str, tuple[float, str]] = {}
_gambly_redirect_cache_lock = Lock()
_gambly_redirect_target_index: dict[str, tuple[float, str]] = {}
_recent_gambly_links: deque[dict[str, object]] = deque(maxlen=250)


def _record_gambly_link_event(
    *,
    status: str,
    source: str,
    target: str,
    slip_id: str | None = None,
    token: str | None = None,
    reason: str | None = None,
) -> None:
    payload = {
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "status": str(status or "").strip() or "unknown",
        "source": str(source or "").strip() or "unknown",
        "slip_id": str(slip_id or "").strip() or None,
        "token": str(token or "").strip() or None,
        "go_path": f"/dashboard/go/g/{token}" if str(token or "").strip() else None,
        "target": str(target or "").strip() or None,
        "reason": str(reason or "").strip() or None,
    }
    with _gambly_redirect_cache_lock:
        _recent_gambly_links.appendleft(payload)


def _recent_gambly_link_events(limit: int = 50) -> list[dict[str, object]]:
    safe_limit = max(1, min(500, int(limit)))
    with _gambly_redirect_cache_lock:
        return list(_recent_gambly_links)[:safe_limit]


def _cache_gambly_redirect(url: str, *, slip_id: str | None = None, source: str = "unknown") -> str:
    target = str(url or "").strip()
    if not target:
        logger.warning(
            "gambly_link create_failed",
            extra={"reason": "empty_target", "source": source, "slip_id": slip_id},
        )
        _record_gambly_link_event(status="failed", source=source, target=target, slip_id=slip_id, reason="empty_target")
        return ""

    now = time.time()
    expires_at = now + GAMBLY_REDIRECT_TTL_SECONDS
    token = ""
    reused = False
    with _gambly_redirect_cache_lock:
        expired = [k for k, (exp, _) in _gambly_redirect_cache.items() if exp <= now]
        for key in expired:
            _gambly_redirect_cache.pop(key, None)

        stale_targets = [target_key for target_key, (exp, _) in _gambly_redirect_target_index.items() if exp <= now]
        for target_key in stale_targets:
            _gambly_redirect_target_index.pop(target_key, None)

        existing = _gambly_redirect_target_index.get(target)
        if existing:
            existing_exp, existing_token = existing
            cached = _gambly_redirect_cache.get(existing_token)
            if cached and cached[0] > now and cached[1] == target and existing_exp > now:
                token = existing_token
                reused = True

        if not token:
            for _ in range(4):
                candidate = secrets.token_urlsafe(6)
                if candidate not in _gambly_redirect_cache:
                    token = candidate
                    break
            if not token:
                token = secrets.token_urlsafe(10)

        _gambly_redirect_cache[token] = (expires_at, target)
        _gambly_redirect_target_index[target] = (expires_at, token)

    if reused:
        logger.info(
            "gambly_link reused",
            extra={"source": source, "slip_id": slip_id, "token": token},
        )
        _record_gambly_link_event(status="reused", source=source, target=target, slip_id=slip_id, token=token)
    else:
        logger.info(
            "gambly_link created",
            extra={"source": source, "slip_id": slip_id, "token": token},
        )
        _record_gambly_link_event(status="created", source=source, target=target, slip_id=slip_id, token=token)

    return f"/dashboard/go/g/{token}"


def _resolve_cached_gambly_redirect(token: str) -> str | None:
    key = str(token or "").strip()
    if not key:
        return None
    now = time.time()
    with _gambly_redirect_cache_lock:
        entry = _gambly_redirect_cache.get(key)
        if not entry:
            return None
        expires_at, target = entry
        if expires_at <= now:
            _gambly_redirect_cache.pop(key, None)
            return None
        return target


SUPPORTED_SPORTSBOOKS = {
    "gambly",
    "actionnetwork",
    "fanduel",
    "draftkings",
    "fanatics",
    "espn_bet",
    "caesars",
    "betmgm",
}

BOOK_SITE_DOMAINS = {
    "gambly": "gambly.com",
    "fanduel": "sportsbook.fanduel.com",
    "draftkings": "sportsbook.draftkings.com",
    "fanatics": "sportsbook.fanatics.com",
    "espn_bet": "espnbet.com",
    "caesars": "caesars.com",
    "betmgm": "sports.betmgm.com",
    "actionnetwork": "actionnetwork.com",
}

BOOK_HOME_URLS = {
    "gambly": "https://gambly.com/bet-builder?type=straight%7Cplayer_prop&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200&limit=10&sort_by=popularity",
    "fanduel": "https://sportsbook.fanduel.com/",
    "draftkings": "https://sportsbook.draftkings.com/",
    "fanatics": "https://sportsbook.fanatics.com/",
    "espn_bet": "https://espnbet.com/",
    "caesars": "https://www.caesars.com/sportsbook-and-casino",
    "betmgm": "https://sports.betmgm.com/",
    "actionnetwork": "https://www.actionnetwork.com/",
}


def _env_enabled(name: str, default: str = "true") -> bool:
    raw = os.getenv(name, default).strip().lower()
    return raw in {"1", "true", "yes", "on"}


GAMBLY_ENABLED = _env_enabled("GAMBLY_ENABLED", "true")

SUPPORTED_GAMBLY_ROUTE_BOOKS = {
    "gambly",
    "fanduel",
    "draftkings",
    "fanatics",
    "espn_bet",
    "caesars",
    "betmgm",
    "actionnetwork",
}

ODDS_API_TIMEOUT_SECONDS = 5
LINK_CACHE_TTL_SECONDS = 600


class SportsbookLinkAgentState:
    def __init__(self) -> None:
        self.running: bool = False
        self.last_refresh_at: str | None = None
        self.last_status: str = "idle"
        self.last_message: str = ""
        self.total_cached_links: int = 0
        self.cache_ttl_seconds: int = LINK_CACHE_TTL_SECONDS

    def snapshot(self) -> dict[str, object]:
        return {
            "running": self.running,
            "last_refresh_at": self.last_refresh_at,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "total_cached_links": self.total_cached_links,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }


_link_cache_lock = Lock()
_link_cache_by_book: dict[str, dict[str, object]] = {
    "fanduel": {"links": {}, "team_links": {}, "updated_at": 0.0},
    "draftkings": {"links": {}, "team_links": {}, "updated_at": 0.0},
}


def _set_link_cache(book: str, links: dict[str, str], team_links: dict[str, str]) -> None:
    now_ts = time.time()
    with _link_cache_lock:
        _link_cache_by_book[book] = {
            "links": dict(links),
            "team_links": dict(team_links),
            "updated_at": now_ts,
        }


def _get_link_cache(book: str) -> tuple[dict[str, str], dict[str, str], bool, float]:
    with _link_cache_lock:
        payload = dict(_link_cache_by_book.get(book) or {})
    links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
    team_links = payload.get("team_links") if isinstance(payload.get("team_links"), dict) else {}
    updated_at = float(payload.get("updated_at") or 0.0)
    age_seconds = max(0.0, time.time() - updated_at) if updated_at > 0 else 10**9
    fresh = bool(updated_at > 0 and age_seconds <= LINK_CACHE_TTL_SECONDS)
    return dict(links), dict(team_links), fresh, age_seconds


class SportsbookLinkAgent:
    def __init__(self) -> None:
        self.state = SportsbookLinkAgentState()
        self._thread: Thread | None = None
        self._stop_event = Event()

    def run_once(self) -> dict[str, object]:
        api_key = os.getenv("THE_ODDS_API_KEY", "").strip()
        if not api_key:
            self.state.last_refresh_at = datetime.now(tz=timezone.utc).isoformat()
            self.state.last_status = "disabled"
            self.state.last_message = "THE_ODDS_API_KEY is not set. Using fallback search links."
            self.state.total_cached_links = 0
            return {
                "status": "disabled",
                "message": self.state.last_message,
                "total_cached_links": 0,
            }

        cached_total = 0
        refreshed_books: list[str] = []
        for book in ("fanduel", "draftkings"):
            links = _fetch_book_player_links(book=book, api_key=api_key, market_keys=ALL_PLAYER_PROP_MARKET_KEYS)
            team_links = _fetch_book_team_links(book=book, api_key=api_key) if not links else {}
            _set_link_cache(book=book, links=links, team_links=team_links)
            refreshed_books.append(book)
            cached_total += len(links)

        self.state.last_refresh_at = datetime.now(tz=timezone.utc).isoformat()
        self.state.last_status = "ok"
        self.state.last_message = f"Refreshed link cache for {', '.join(refreshed_books)}."
        self.state.total_cached_links = cached_total
        return {
            "status": "ok",
            "books": refreshed_books,
            "total_cached_links": cached_total,
            "cache_ttl_seconds": LINK_CACHE_TTL_SECONDS,
        }

    def _loop(self, every_seconds: int) -> None:
        self.state.running = True
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # pylint: disable=broad-except
                self.state.last_refresh_at = datetime.now(tz=timezone.utc).isoformat()
                self.state.last_status = "error"
                self.state.last_message = str(exc)
            if self._stop_event.wait(timeout=max(60, int(every_seconds))):
                break
        self.state.running = False

    def start(self, every_seconds: int = 300) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._loop, args=(every_seconds,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)


link_agents = SportsbookLinkAgent()

MLB_TEAM_NAME_BY_ABBREV = {
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "CWS": "Chicago White Sox",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals",
    "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "ATH": "Athletics",
    "OAK": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres",
    "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
}


def _norm_player_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


MLB_PLAYER_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
MLB_MARKET_TARGET_POINTS = {
    "batter_home_runs": 0.5,
    "batter_hits": 0.5,
    "batter_rbis": 0.5,
    "batter_total_bases": 1.5,
}

PLAYER_ALIAS_FILE = ROOT_DIR / "data" / "player_aliases.json"
PLAYER_ALIASES_DEFAULT: dict[str, list[str]] = {
    "ronaldacuna": ["ronald acuna jr", "ronald acuna"],
    "fernandotatis": ["fernando tatis jr", "fernando tatis"],
    "vladimirguerrero": ["vladimir guerrero jr", "vladimir guerrero"],
    "lourdesgurriel": ["lourdes gurriel jr", "lourdes gurriel"],
}
_player_alias_lookup_cache: dict[str, list[str]] | None = None
_player_alias_lookup_cache_mtime: float | None = None
_player_alias_lookup_lock = Lock()


def _load_player_alias_lookup() -> dict[str, list[str]]:
    global _player_alias_lookup_cache
    global _player_alias_lookup_cache_mtime

    file_mtime: float | None = None
    try:
        file_mtime = PLAYER_ALIAS_FILE.stat().st_mtime
    except Exception:
        file_mtime = None

    with _player_alias_lookup_lock:
        if _player_alias_lookup_cache is not None and _player_alias_lookup_cache_mtime == file_mtime:
            return dict(_player_alias_lookup_cache)

        lookup: dict[str, list[str]] = {}

        def _store(key: str, value: str) -> None:
            norm_key = _norm_player_name(key)
            norm_value = _norm_player_name(value)
            if not norm_key or not norm_value:
                return
            bucket = lookup.setdefault(norm_key, [])
            if norm_value not in bucket:
                bucket.append(norm_value)

        for canonical, aliases in PLAYER_ALIASES_DEFAULT.items():
            _store(canonical, canonical)
            if isinstance(aliases, list):
                for alias in aliases:
                    _store(canonical, str(alias))
                    _store(alias, canonical)

        if file_mtime is not None:
            try:
                payload = json.loads(PLAYER_ALIAS_FILE.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                for canonical, aliases in payload.items():
                    _store(str(canonical), str(canonical))
                    if isinstance(aliases, list):
                        for alias in aliases:
                            _store(str(canonical), str(alias))
                            _store(str(alias), str(canonical))

        _player_alias_lookup_cache = lookup
        _player_alias_lookup_cache_mtime = file_mtime
        return dict(lookup)


def _player_alias_keys(value: str) -> list[str]:
    key = _norm_player_name(value)
    if not key:
        return []
    lookup = _load_player_alias_lookup()
    aliases = list(lookup.get(key, []))
    if key not in aliases:
        aliases.insert(0, key)
    return aliases


def _player_lookup_keys(value: str) -> list[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return []

    tokens = [token for token in re.findall(r"[a-z0-9]+", raw) if token]
    if not tokens:
        return []

    candidates: list[str] = []

    def _add(candidate: str) -> None:
        key = _norm_player_name(candidate)
        if key and key not in candidates:
            candidates.append(key)

    _add(" ".join(tokens))

    # Drop common suffixes so `Ronald Acuna Jr` can match `Ronald Acuna` listings.
    while len(tokens) > 2 and tokens[-1] in MLB_PLAYER_SUFFIXES:
        tokens = tokens[:-1]
        _add(" ".join(tokens))

    if len(tokens) >= 2:
        _add(f"{tokens[0]} {tokens[-1]}")
        _add(f"{tokens[0][0]} {tokens[-1]}")

    for alias_key in _player_alias_keys(" ".join(tokens)):
        if alias_key not in candidates:
            candidates.append(alias_key)

    return candidates


def _outcome_link_score(*, market_key: str, side: str, point: object) -> float:
    side_key = str(side or "").strip().lower()
    market = str(market_key or "").strip().lower()
    score = 0.0

    if side_key == "over":
        score += 100.0
    elif side_key in {"yes", "to hit"}:
        score += 90.0
    elif side_key == "under":
        score += 10.0

    target = MLB_MARKET_TARGET_POINTS.get(market)
    try:
        numeric_point = float(point)
    except Exception:
        numeric_point = None

    if numeric_point is not None and target is not None:
        score += max(0.0, 40.0 - (abs(numeric_point - target) * 25.0))
    elif target is not None and side_key == "over":
        score += 5.0

    return score


def _prop_label_for_request(mode: str, hits_profile: str) -> str:
    mode_key = str(mode).strip().lower()
    profile = str(hits_profile).strip().lower()
    if mode_key == "hits":
        if profile.startswith("tb-"):
            return "total bases"
        if profile.startswith("rbi-"):
            return "rbis"
        if profile.startswith("hrr-"):
            return "hits runs rbis"
        if profile == "combo":
            return "hits and total bases"
        return "hits"
    if mode_key == "hits-tb-combo":
        return "hits and total bases"
    return "to hit a home run"


def _market_keys_for_request(mode: str, hits_profile: str) -> list[str]:
    mode_key = str(mode).strip().lower()
    profile = str(hits_profile).strip().lower()

    if mode_key == "hits":
        if profile.startswith("tb-"):
            return ["batter_total_bases", "player_total_bases"]
        if profile.startswith("rbi-"):
            return ["batter_rbis", "player_rbis"]
        if profile.startswith("hrr-"):
            return []
        if profile == "combo":
            return []
        return ["batter_hits", "player_hits"]

    if mode_key == "hits-tb-combo":
        return []

    return ["batter_home_runs", "player_home_runs"]


ALL_PLAYER_PROP_MARKET_KEYS = [
    "batter_home_runs",
    "player_home_runs",
    "batter_hits",
    "player_hits",
    "batter_total_bases",
    "player_total_bases",
    "batter_rbis",
    "player_rbis",
]

MLB_MARKET_ALIAS_MAP = {
    "player_home_runs": "batter_home_runs",
    "player_hits": "batter_hits",
    "player_total_bases": "batter_total_bases",
    "player_rbis": "batter_rbis",
}

MLB_VALID_PLAYER_PROP_MARKETS = {
    "batter_home_runs",
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
}


def _normalize_mlb_market_keys(market_keys: list[str]) -> set[str]:
    normalized: set[str] = set()
    for raw_key in market_keys:
        key = str(raw_key).strip().lower()
        if not key:
            continue
        key = MLB_MARKET_ALIAS_MAP.get(key, key)
        if key in MLB_VALID_PLAYER_PROP_MARKETS:
            normalized.add(key)
    return normalized


def _fallback_search_link(book: str, player_name: str, prop_label: str) -> str | None:
    domain = BOOK_SITE_DOMAINS.get(book)
    player = str(player_name).strip()
    prop = str(prop_label).strip()
    if not domain or not player:
        return None

    tokens = [token for token in re.findall(r"[A-Za-z0-9]+", player) if token]
    player_variants: list[str] = []

    def _add_player_variant(value: str) -> None:
        clean = str(value or "").strip()
        if clean and clean not in player_variants:
            player_variants.append(clean)

    _add_player_variant(player)
    if tokens:
        while len(tokens) > 2 and tokens[-1].lower() in MLB_PLAYER_SUFFIXES:
            tokens = tokens[:-1]
            _add_player_variant(" ".join(tokens))

    if len(player_variants) > 1:
        player_clause = "(" + " OR ".join([f'"{name}"' for name in player_variants]) + ")"
    else:
        player_clause = f'"{player_variants[0]}"'

    query = f'site:{domain} {player_clause} "{prop}"'
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _normalize_target_sportsbook(book: str) -> str:
    key = str(book or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fd": "fanduel",
        "dk": "draftkings",
        "espnbet": "espn_bet",
        "espn": "espn_bet",
        "mgm": "betmgm",
        "bet_mgm": "betmgm",
    }
    key = aliases.get(key, key)
    if key not in SUPPORTED_GAMBLY_ROUTE_BOOKS:
        return "gambly"
    return key


def _compose_sportsbook_slip_text(slip: dict, idx: int, prop_label: str) -> str:
    lines: list[str] = [f"#{idx + 1} {str(slip.get('archetype') or '').strip()}".strip()]
    legs = slip.get("legs") if isinstance(slip, dict) else []
    if not isinstance(legs, list):
        return "\n".join([line for line in lines if line])
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        player = str(leg.get("player_name") or "").strip()
        team = str(leg.get("team") or "").strip()
        if not player:
            continue
        if team:
            lines.append(f"{player} ({team}) - {prop_label}")
        else:
            lines.append(f"{player} - {prop_label}")
    return "\n".join([line for line in lines if line])


def _compose_gambly_query_text(slip_text: str, target_sportsbook: str) -> str:
    sportsbook_hint = str(target_sportsbook or "gambly").replace("_", " ").title()
    base = str(slip_text or "").strip()
    if not base:
        return f"Sportsbook: {sportsbook_hint}"
    return f"Sportsbook: {sportsbook_hint}\n{base}"


def _build_gambly_slip_link(slip_text: str, target_sportsbook: str) -> str:
    try:
        return build_gambly_link(_compose_gambly_query_text(slip_text=slip_text, target_sportsbook=target_sportsbook))
    except Exception:
        logger.exception(
            "gambly_link create_failed",
            extra={"reason": "build_exception", "target_sportsbook": target_sportsbook},
        )
        _record_gambly_link_event(
            status="failed",
            source="build_gambly_link",
            target="",
            reason="build_exception",
        )
        return GAMBLY_BET_BUILDER_BASE


def _ensure_dashboard_slip_links(parlays: list[dict], mode: str, hits_profile: str) -> None:
    prop_label = _prop_label_for_request(mode=mode, hits_profile=hits_profile)
    for idx, slip in enumerate(parlays):
        if not isinstance(slip, dict):
            continue

        slip_id = str(slip.get("slip_id") or "").strip()
        if not slip_id:
            slip_id = f"dashboard-S{idx + 1:02d}-{secrets.token_hex(2)}"
            slip["slip_id"] = slip_id

        slip_text = _compose_sportsbook_slip_text(slip=slip, idx=idx, prop_label=prop_label)
        gambly_link = _build_gambly_slip_link(slip_text=slip_text, target_sportsbook="gambly")
        go_path = str(slip.get("gambly_go_path") or slip.get("share_link_path") or "").strip()

        go_token = ""
        if go_path.startswith("/dashboard/go/g/"):
            go_token = go_path.split("/")[-1].strip()
        existing_target = _resolve_cached_gambly_redirect(go_token) if go_token else None
        if (not go_path) or (existing_target != gambly_link):
            go_path = _cache_gambly_redirect(gambly_link, slip_id=slip_id, source="dashboard_slip")

        slip["sportsbook_text"] = slip_text
        slip["gambly_link"] = gambly_link
        slip["gambly_go_path"] = go_path
        slip["share_link_path"] = go_path


def _extract_gambly_text_from_link(link: str) -> str:
    raw = str(link or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        text_values = query.get("text") or query.get("q") or []
        text = str(text_values[0] if text_values else "").strip()
        return text
    except Exception:
        return ""


def _extract_target_sportsbook_from_text(slip_text: str) -> str:
    text = str(slip_text or "").strip()
    if not text:
        return "gambly"
    first_line = text.splitlines()[0].strip()
    if not first_line.lower().startswith("sportsbook:"):
        return "gambly"
    raw_target = first_line.split(":", 1)[1].strip()
    return _normalize_target_sportsbook(raw_target)


def _standardize_region_state(value: str | None) -> str:
    return str(value or "").strip().upper()


def _parse_preferred_books_param(preferred_books: str | None) -> list[str] | None:
    raw = str(preferred_books or "").strip()
    if not raw:
        return None
    parts = [normalize_book_key(item) for item in raw.split(",") if str(item).strip()]
    output: list[str] = []
    for item in parts:
        if item not in output:
            output.append(item)
    return output or None


def _build_slip_routing_payload(
    *,
    slip: dict,
    slip_idx: int,
    mode: str,
    hits_profile: str,
    country: str,
    region_state: str,
    base_origin: str,
    preferred_books: list[str] | None,
) -> dict[str, object]:
    prop_label = _prop_label_for_request(mode=mode, hits_profile=hits_profile)
    slip_text = _compose_sportsbook_slip_text(slip=slip, idx=slip_idx, prop_label=prop_label)
    gambly_link = str(slip.get("gambly_link") or "").strip() or _build_gambly_slip_link(
        slip_text=slip_text,
        target_sportsbook="gambly",
    )

    go_path = str(slip.get("gambly_go_path") or slip.get("share_link_path") or "").strip()
    go_token = go_path.split("/")[-1].strip() if go_path.startswith("/dashboard/go/g/") else ""
    existing_target = _resolve_cached_gambly_redirect(go_token) if go_token else None
    if (not go_path) or (existing_target != gambly_link):
        go_path = _cache_gambly_redirect(gambly_link, slip_id=str(slip.get("slip_id") or "").strip(), source="routing_layer")

    route_plan = build_route_plan(
        country=country,
        region_state=region_state,
        preferred_books=preferred_books,
        gambly_link=gambly_link,
        go_path=go_path,
        share_link_path=go_path,
        base_origin=base_origin,
    )
    return {
        "standard_slip": build_standardized_slip(slip),
        "route_plan": route_plan,
        "links": {
            "gambly_link": gambly_link,
            "gambly_go_path": go_path,
            "share_link_path": go_path,
        },
        "capabilities": {
            "books": {
                book: {
                    "label": BOOK_LABELS.get(book, book.title()),
                    "one_click_capable": bool(BOOK_ONE_CLICK_CAPABILITY.get(book, False)),
                }
                for book in route_plan.get("books", [])
            }
        },
    }


def _is_mobile_user_agent(user_agent: str) -> bool:
    ua = str(user_agent or "").lower()
    if not ua:
        return False
    mobile_tokens = [
        "iphone",
        "ipad",
        "ipod",
        "android",
        "mobile",
        "samsungbrowser",
        "wv",
        "crios",
        "fxios",
    ]
    return any(token in ua for token in mobile_tokens)


def _fetch_book_team_links(book: str, api_key: str, diagnostics: dict[str, object] | None = None) -> dict[str, str]:
    url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
    params = {
        "apiKey": api_key,
        "bookmakers": book,
        "markets": "h2h",
        "regions": "us",
        "oddsFormat": "american",
        "includeLinks": "true",
        "includeSids": "true",
    }
    try:
        response = requests.get(url, params=params, timeout=ODDS_API_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.exception("sportsbook_resolver provider_exception", extra={"book": book, "stage": "team_fallback"})
        if diagnostics is not None:
            diagnostics["error"] = "provider_timeout" if isinstance(exc, requests.exceptions.Timeout) else "provider_unavailable"
            diagnostics["timeout"] = bool(isinstance(exc, requests.exceptions.Timeout))
        return {}

    if not isinstance(payload, list):
        return {}

    links_by_team: dict[str, str] = {}
    for event in payload:
        if not isinstance(event, dict):
            continue
        for bookmaker in event.get("bookmakers", []):
            if not isinstance(bookmaker, dict):
                continue
            if str(bookmaker.get("key", "")).lower() != book:
                continue
            for market in bookmaker.get("markets", []):
                if not isinstance(market, dict):
                    continue
                if str(market.get("key", "")).lower() != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    team_name = str(outcome.get("name", "")).strip()
                    link = str(outcome.get("link", "")).strip()
                    if not team_name or not link:
                        continue
                    links_by_team.setdefault(_norm_player_name(team_name), link)

    return links_by_team


def _team_lookup_candidates(team_value: str) -> list[str]:
    team_raw = str(team_value or "").strip()
    if not team_raw:
        return []
    team_upper = team_raw.upper()
    candidates = [team_raw]
    full = MLB_TEAM_NAME_BY_ABBREV.get(team_upper)
    if full:
        candidates.append(full)
    return [_norm_player_name(value) for value in candidates if _norm_player_name(value)]


def _fetch_book_player_links(
    book: str,
    api_key: str,
    market_keys: list[str],
    diagnostics: dict[str, object] | None = None,
) -> dict[str, str]:
    # MLB player-prop markets are returned on event-level odds endpoints.
    # The league-level /sports/.../odds endpoint rejects these markets with 422.
    return _fetch_book_player_links_from_event_odds(
        book=book,
        api_key=api_key,
        market_keys=market_keys,
        max_events=20,
        diagnostics=diagnostics,
    )


def _fetch_book_player_links_from_event_odds(
    book: str,
    api_key: str,
    market_keys: list[str],
    max_events: int = 20,
    diagnostics: dict[str, object] | None = None,
) -> dict[str, str]:
    wanted_markets = _normalize_mlb_market_keys(market_keys)
    if not wanted_markets:
        return {}

    events_url = "https://api.the-odds-api.com/v4/sports/baseball_mlb/events"
    event_params = {
        "apiKey": api_key,
    }

    try:
        events_response = requests.get(events_url, params=event_params, timeout=ODDS_API_TIMEOUT_SECONDS)
        events_response.raise_for_status()
        events_payload = events_response.json()
    except Exception as exc:
        logger.exception("sportsbook_resolver provider_exception", extra={"book": book, "stage": "provider_event_list"})
        if diagnostics is not None:
            diagnostics["error"] = "provider_timeout" if isinstance(exc, requests.exceptions.Timeout) else "provider_unavailable"
            diagnostics["timeout"] = bool(isinstance(exc, requests.exceptions.Timeout))
        return {}

    if not isinstance(events_payload, list):
        return {}

    event_ids: list[str] = []
    for item in events_payload:
        if not isinstance(item, dict):
            continue
        event_id = str(item.get("id", "")).strip()
        if not event_id:
            continue
        event_ids.append(event_id)
        if len(event_ids) >= max(1, int(max_events)):
            break

    if not event_ids:
        return {}

    links_by_player: dict[str, str] = {}
    best_score_by_player: dict[str, float] = {}
    odds_base = "https://api.the-odds-api.com/v4/sports/baseball_mlb/events"
    odds_params = {
        "apiKey": api_key,
        "bookmakers": book,
        "markets": ",".join(sorted(wanted_markets)),
        "regions": "us",
        "oddsFormat": "american",
        "includeLinks": "true",
        "includeSids": "true",
    }

    for event_id in event_ids:
        try:
            response = requests.get(
                f"{odds_base}/{event_id}/odds",
                params=odds_params,
                timeout=max(3, ODDS_API_TIMEOUT_SECONDS - 1),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.exception("sportsbook_resolver provider_exception", extra={"book": book, "stage": "provider_event_odds"})
            if diagnostics is not None and isinstance(exc, requests.exceptions.Timeout):
                diagnostics["timeout"] = True
                diagnostics["error"] = "provider_timeout"
            continue

        if not isinstance(payload, dict):
            continue

        for bookmaker in payload.get("bookmakers", []):
            if not isinstance(bookmaker, dict):
                continue
            if str(bookmaker.get("key", "")).lower() != book:
                continue
            for market in bookmaker.get("markets", []):
                if not isinstance(market, dict):
                    continue
                if str(market.get("key", "")).lower() not in wanted_markets:
                    continue
                for outcome in market.get("outcomes", []):
                    if not isinstance(outcome, dict):
                        continue
                    link = str(outcome.get("link", "")).strip()
                    if not link:
                        continue
                    player = str(outcome.get("description") or outcome.get("name") or "").strip()
                    if not player:
                        continue
                    key = _norm_player_name(player)
                    if not key:
                        continue
                    market_key = str(market.get("key", "")).strip().lower()
                    point = outcome.get("point")
                    side = str(outcome.get("name", "")).strip().lower()
                    score = _outcome_link_score(market_key=market_key, side=side, point=point)
                    for candidate_key in _player_lookup_keys(player):
                        existing = best_score_by_player.get(candidate_key)
                        if existing is None or score > existing:
                            links_by_player[candidate_key] = link
                            best_score_by_player[candidate_key] = score

    return links_by_player


def _dashboard_source_csv_path() -> Path:
    raw = os.getenv("WW_DASHBOARD_SOURCE_CSV", "data/propfinder_normalized_latest.csv").strip()
    if not raw:
        raw = "data/propfinder_normalized_latest.csv"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _dashboard_source_csv_mtime(path: Path | None = None) -> float | None:
    csv_path = path or _dashboard_source_csv_path()
    try:
        return csv_path.stat().st_mtime
    except Exception:
        return None


def _hermes_adjustment_path() -> Path:
    raw = os.getenv("WW_HERMES_ADJUSTMENTS_PATH", "data/hermes/latest_adjustments.json").strip()
    path = Path(raw or "data/hermes/latest_adjustments.json")
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def _load_hermes_adjustments() -> dict:
    path = _hermes_adjustment_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dashboard_rank_adjustments() -> dict[str, dict[str, float] | dict[str, object]]:
    global _rank_adjustments_cache
    global _rank_adjustments_last_source_mtime
    global _rank_adjustments_last_slate_complete
    global _rank_adjustments_last_reason
    global _rank_adjustments_last_refreshed_at

    category_defaults = {
        "hr": 1.0,
        "hits": 1.0,
        "tb": 1.0,
        "rbi": 1.0,
        "hrr": 1.0,
        "value": 1.0,
        "matchup": 1.0,
        "recent": 1.0,
    }
    subcategory_defaults = {
        "overall": 1.0,
        "value": 1.0,
        "safe": 1.0,
        "contrarian": 1.0,
    }

    source_mtime = _dashboard_source_csv_mtime()
    slate_complete = False
    try:
        csv_path = _dashboard_source_csv_path()
        if csv_path.exists():
            slate_df = load_slate_from_csv(str(csv_path))
            slate_complete = _slate_is_complete(slate_df)
    except Exception:
        slate_complete = False

    reasons: list[str] = []
    with _rank_adjustments_lock:
        if _rank_adjustments_cache is None:
            reasons.append("initial_load")
        if (
            source_mtime is not None
            and _rank_adjustments_last_source_mtime is not None
            and source_mtime > _rank_adjustments_last_source_mtime
        ):
            reasons.append("new_lineups")
        if slate_complete and not _rank_adjustments_last_slate_complete:
            reasons.append("games_complete")

        should_refresh = bool(reasons)
        if not should_refresh and _rank_adjustments_cache is not None:
            frozen = {
                "categories": dict(_rank_adjustments_cache.get("categories", {})),
                "subcategories": dict(_rank_adjustments_cache.get("subcategories", {})),
                "meta": dict(_rank_adjustments_cache.get("meta", {})),
            }
            frozen_meta = dict(frozen.get("meta", {}))
            frozen_meta["learning_update_policy"] = "refresh_on_games_complete_or_new_lineups"
            frozen_meta["weights_refresh_skipped"] = True
            frozen_meta["last_refresh_reason"] = _rank_adjustments_last_reason
            frozen_meta["weights_last_refreshed_at"] = _rank_adjustments_last_refreshed_at
            frozen["meta"] = frozen_meta
            return frozen

    learning_payload: dict[str, object] = {}
    try:
        learning_payload = adaptive_category_weights(lookback_days=60, min_samples=5)
    except Exception:
        learning_payload = {}

    category_weights = dict(category_defaults)
    for key, default in category_defaults.items():
        try:
            category_weights[key] = _clamp_float(
                float((learning_payload.get("categories", {}) or {}).get(key, default)), 0.70, 1.35
            )
        except Exception:
            category_weights[key] = default

    subcategory_weights = dict(subcategory_defaults)
    for key, default in subcategory_defaults.items():
        try:
            subcategory_weights[key] = _clamp_float(
                float((learning_payload.get("subcategories", {}) or {}).get(key, default)), 0.70, 1.35
            )
        except Exception:
            subcategory_weights[key] = default

    hermes = _load_hermes_adjustments()
    adjustments = hermes.get("adjustments", {}) if isinstance(hermes, dict) else {}
    if isinstance(adjustments, dict):
        explicit_category_deltas = adjustments.get("category_weight_deltas", {})
        if isinstance(explicit_category_deltas, dict):
            for key in category_weights:
                raw_delta = explicit_category_deltas.get(key)
                if raw_delta is None:
                    continue
                try:
                    category_weights[key] = _clamp_float(category_weights[key] * (1.0 + float(raw_delta)), 0.65, 1.45)
                except Exception:
                    continue

        explicit_subcategory_deltas = adjustments.get("subcategory_weight_deltas", {})
        if isinstance(explicit_subcategory_deltas, dict):
            for key in subcategory_weights:
                raw_delta = explicit_subcategory_deltas.get(key)
                if raw_delta is None:
                    continue
                try:
                    subcategory_weights[key] = _clamp_float(subcategory_weights[key] * (1.0 + float(raw_delta)), 0.65, 1.45)
                except Exception:
                    continue

        mode_weights = adjustments.get("mode_weights", {})
        if isinstance(mode_weights, dict):
            try:
                category_weights["recent"] = _clamp_float(category_weights["recent"] * float(mode_weights.get("hits", 1.0)), 0.65, 1.45)
                category_weights["hits"] = _clamp_float(category_weights["hits"] * float(mode_weights.get("hits", 1.0)), 0.65, 1.45)
                category_weights["tb"] = _clamp_float(category_weights["tb"] * float(mode_weights.get("hits-tb-combo", 1.0)), 0.65, 1.45)
                category_weights["hrr"] = _clamp_float(category_weights["hrr"] * float(mode_weights.get("hits-tb-combo", 1.0)), 0.65, 1.45)
                category_weights["value"] = _clamp_float(category_weights["value"] * float(mode_weights.get("chalk-city", 1.0)), 0.65, 1.45)
                cream_weight = max(float(mode_weights.get("cream", 1.0)), float(mode_weights.get("extreme-cream", 1.0)))
                category_weights["hr"] = _clamp_float(category_weights["hr"] * cream_weight, 0.65, 1.45)
                category_weights["matchup"] = _clamp_float(category_weights["matchup"] * cream_weight, 0.65, 1.45)
                subcategory_weights["overall"] = _clamp_float(subcategory_weights["overall"] * float(mode_weights.get("balanced", 1.0)), 0.65, 1.45)
            except Exception:
                pass

        risk_weights = adjustments.get("risk_weights", {})
        if isinstance(risk_weights, dict):
            try:
                subcategory_weights["safe"] = _clamp_float(subcategory_weights["safe"] * float(risk_weights.get("safe", 1.0)), 0.65, 1.45)
                subcategory_weights["overall"] = _clamp_float(subcategory_weights["overall"] * float(risk_weights.get("balanced", 1.0)), 0.65, 1.45)
                subcategory_weights["contrarian"] = _clamp_float(subcategory_weights["contrarian"] * float(risk_weights.get("yolo", 1.0)), 0.65, 1.45)
            except Exception:
                pass

    result = {
        "categories": category_weights,
        "subcategories": subcategory_weights,
        "meta": {
            "learning_enabled": bool(learning_payload),
            "hermes_loaded": bool(adjustments),
            "learning_update_policy": "refresh_on_games_complete_or_new_lineups",
            "weights_refresh_skipped": False,
        },
    }

    with _rank_adjustments_lock:
        _rank_adjustments_cache = result
        _rank_adjustments_last_source_mtime = source_mtime
        _rank_adjustments_last_slate_complete = slate_complete
        _rank_adjustments_last_reason = ",".join(reasons) if reasons else "forced"
        _rank_adjustments_last_refreshed_at = datetime.now(tz=timezone.utc).isoformat()

        result_meta = dict(result.get("meta", {}))
        result_meta["last_refresh_reason"] = _rank_adjustments_last_reason
        result_meta["weights_last_refreshed_at"] = _rank_adjustments_last_refreshed_at
        result["meta"] = result_meta

    return result


def _clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _json_float_or_none(value: float | None) -> float | None:
    try:
        numeric = float(value) if value is not None else None
    except Exception:
        return None
    if numeric is None:
        return None
    if numeric != numeric:
        return None
    if numeric == float("inf") or numeric == float("-inf"):
        return None
    return numeric


def _normalize_dashboard_mode(mode: str) -> str:
    mode_key = str(mode).strip().lower()
    if mode_key in {"extreme_cream", "extremecream", "xcream"}:
        mode_key = "extreme-cream"
    if mode_key in {"chalk_city", "chalkcity"}:
        mode_key = "chalk-city"
    if mode_key in {"fun", "random-fun", "random_fun"}:
        mode_key = "random"
    if mode_key in {"combo", "hits_tb_combo", "hitstb", "hits+tb"}:
        mode_key = "hits-tb-combo"
    if mode_key not in {"balanced", "cream", "extreme-cream", "chalk-city", "random", "hits", "hits-tb-combo"}:
        mode_key = "balanced"
    return mode_key


def _normalize_risk_level(risk_level: str) -> str:
    risk_key = str(risk_level or "balanced").strip().lower()
    if risk_key in {"safe", "balanced", "yolo"}:
        return risk_key
    return "balanced"


def _normalize_hits_profile(hits_profile: str) -> str:
    hits_profile_key = str(hits_profile or "high-frequency").strip().lower()
    if hits_profile_key == "tripples":
        hits_profile_key = "triples"
    if hits_profile_key in {"1plus", "1+", "hits1", "oneplus"}:
        hits_profile_key = "one-plus"
    if hits_profile_key in {"2plus", "2+", "hits2", "twoplus"}:
        hits_profile_key = "two-plus"
    if hits_profile_key in {"3plus", "3+", "hits3", "threeplus"}:
        hits_profile_key = "three-plus"
    if hits_profile_key in {"tb1", "tb-1", "1+tb", "one-base"}:
        hits_profile_key = "tb-1"
    if hits_profile_key in {"tb2", "tb-2", "2+tb"}:
        hits_profile_key = "tb-2"
    if hits_profile_key in {"tb3", "tb-3", "3+tb"}:
        hits_profile_key = "tb-3"
    if hits_profile_key in {"tb4", "tb-4", "4+tb"}:
        hits_profile_key = "tb-4"
    if hits_profile_key in {"hrr1", "hrr-1", "1+hrr"}:
        hits_profile_key = "hrr-1"
    if hits_profile_key in {"hrr2", "hrr-2", "2+hrr"}:
        hits_profile_key = "hrr-2"
    if hits_profile_key in {"hrr3", "hrr-3", "3+hrr"}:
        hits_profile_key = "hrr-3"
    if hits_profile_key in {"hrr4", "hrr-4", "4+hrr"}:
        hits_profile_key = "hrr-4"
    if hits_profile_key in {"rbi1", "rbi-1", "1+rbi"}:
        hits_profile_key = "rbi-1"
    if hits_profile_key in {"rbi2", "rbi-2", "2+rbi"}:
        hits_profile_key = "rbi-2"
    if hits_profile_key in {"rbi3", "rbi-3", "3+rbi"}:
        hits_profile_key = "rbi-3"
    if hits_profile_key in {"rbi4", "rbi-4", "4+rbi"}:
        hits_profile_key = "rbi-4"
    if hits_profile_key in {"combo", "hits-tb"}:
        hits_profile_key = "combo"

    if hits_profile_key not in {
        "random",
        "singles",
        "doubles",
        "triples",
        "one-plus",
        "two-plus",
        "three-plus",
        "tb-1",
        "tb-2",
        "tb-3",
        "tb-4",
        "hrr-1",
        "hrr-2",
        "hrr-3",
        "hrr-4",
        "rbi-1",
        "rbi-2",
        "rbi-3",
        "rbi-4",
        "combo",
        "high-frequency",
        "vs-bad-pitchers",
        "streakers",
        "contact-kings",
        "stack-attack",
    }:
        hits_profile_key = "high-frequency"
    return hits_profile_key



def _lineup_locked_filter(df):
    bool_columns = [
        "lineup_confirmed",
        "is_confirmed_starter",
        "confirmed_in_lineup",
        "starting",
        "in_lineup",
    ]
    for col in bool_columns:
        if col in df.columns:
            return df[df[col].astype(str).str.lower().isin({"1", "true", "yes", "y"})].copy()

    if "lineup_slot" in df.columns:
        return df[df["lineup_slot"].between(1, 9)].copy()
    return df.copy()


def _game_has_started(game_id: int) -> bool:
    if game_id <= 0:
        return False

    now_ts = time.time()
    with _started_game_cache_lock:
        cached = _started_game_cache.get(game_id)
        if cached and (now_ts - cached[0]) <= STARTED_GAME_CACHE_TTL_SECONDS:
            return bool(cached[1])

    started = False
    try:
        url = f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("gameData", {}).get("status", {}) if isinstance(payload, dict) else {}
        abstract_code = str(status.get("abstractGameCode", "")).strip().upper()
        abstract_state = str(status.get("abstractGameState", "")).strip().lower()
        detailed_state = str(status.get("detailedState", "")).strip().lower()

        if abstract_code in {"L", "F"}:
            started = True
        elif abstract_state in {"live", "final"}:
            started = True
        elif detailed_state in {"in progress", "final", "game over", "completed early", "suspended"}:
            started = True
        elif "in progress" in detailed_state:
            started = True
    except Exception:
        started = False

    with _started_game_cache_lock:
        _started_game_cache[game_id] = (now_ts, started)

    return started


def _game_is_final(game_id: int) -> bool:
    if game_id <= 0:
        return False

    now_ts = time.time()
    with _final_game_cache_lock:
        cached = _final_game_cache.get(game_id)
        if cached and (now_ts - cached[0]) <= FINAL_GAME_CACHE_TTL_SECONDS:
            return bool(cached[1])

    is_final = False
    try:
        url = f"https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        payload = response.json()
        status = payload.get("gameData", {}).get("status", {}) if isinstance(payload, dict) else {}
        abstract_code = str(status.get("abstractGameCode", "")).strip().upper()
        abstract_state = str(status.get("abstractGameState", "")).strip().lower()
        detailed_state = str(status.get("detailedState", "")).strip().lower()

        if abstract_code == "F":
            is_final = True
        elif abstract_state == "final":
            is_final = True
        elif detailed_state in {"final", "game over", "completed early"}:
            is_final = True
    except Exception:
        is_final = False

    with _final_game_cache_lock:
        _final_game_cache[game_id] = (now_ts, is_final)

    return is_final


def _slate_is_complete(df) -> bool:
    if "game_id" not in df.columns:
        return False

    game_ids: set[int] = set()
    for raw in df["game_id"].dropna().tolist():
        try:
            game_id = int(raw)
        except Exception:
            continue
        if game_id > 0:
            game_ids.add(game_id)

    if not game_ids:
        return False

    return all(_game_is_final(game_id) for game_id in game_ids)


def _exclude_started_games(df):
    if "game_id" not in df.columns:
        return df

    game_ids: set[int] = set()
    for raw in df["game_id"].dropna().tolist():
        try:
            game_id = int(raw)
        except Exception:
            continue
        if game_id > 0:
            game_ids.add(game_id)

    if not game_ids:
        return df

    started_ids = {gid for gid in game_ids if _game_has_started(gid)}
    if not started_ids:
        return df

    return df[~df["game_id"].isin(started_ids)].copy()


def _generate_dashboard_board(
    num_slips: int,
    mode: str,
    legs_per_slip: int,
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    lineup_locked_only: bool = False,
    allow_live: bool = False,
) -> dict:
    csv_path = _dashboard_source_csv_path()
    if not csv_path.exists():
        raise ValueError(
            f"Dashboard source CSV not found: {csv_path}. Run /agents/run-once first or set WW_DASHBOARD_SOURCE_CSV."
        )

    df = load_slate_from_csv(str(csv_path))
    if lineup_locked_only:
        df = _lineup_locked_filter(df)
        if df.empty:
            raise ValueError("No confirmed lineup players found. Turn off Lineup Lock or refresh lineups.")

    if not allow_live:
        df = _exclude_started_games(df)
        if df.empty:
            raise ValueError("All eligible games have already started. Turn on Live Bets or wait for next slate.")

    config = _dashboard_config(
        num_slips=num_slips,
        mode=mode,
        legs_per_slip=legs_per_slip,
        hits_profile=hits_profile,
        risk_level=risk_level,
        lineup_locked_only=lineup_locked_only,
    )

    game_count = 0
    if "game_id" in df.columns:
        try:
            game_count = int(df["game_id"].dropna().nunique())
        except Exception:
            game_count = 0

    # Strategy tuning for compact slates (10 games and below).
    if game_count > 0 and game_count <= 10:
        config.max_same_game_legs = min(config.legs_per_slip, max(config.max_same_game_legs, 3))
        config.max_same_team_legs = min(config.legs_per_slip, max(config.max_same_team_legs, 3))
        config.min_time_buckets_per_slip = 1
        config.max_player_exposure = min(20, max(config.max_player_exposure, 5))
        config.hr_candidate_pool_size = min(300, max(config.hr_candidate_pool_size, 96))
        config.min_hr_score = max(0.0, config.min_hr_score - 0.05)
        config.min_hit_score = max(0.0, config.min_hit_score - 0.04)
        config.min_tb_score = max(0.0, config.min_tb_score - 0.04)
        config.min_rbi_score = max(0.0, config.min_rbi_score - 0.04)
        config.min_hrr_score = max(0.0, config.min_hrr_score - 0.04)

    # Extra relaxation for very small slates (6 games and below).
    if game_count > 0 and game_count <= 6:
        config.max_same_game_legs = min(config.legs_per_slip, max(config.max_same_game_legs, 4))
        config.max_same_team_legs = min(config.legs_per_slip, max(config.max_same_team_legs, 4))
        config.max_player_exposure = min(20, max(config.max_player_exposure, 6))
        config.ownership_penalty_strength = max(0.0, config.ownership_penalty_strength - 0.08)
        config.min_hr_score = max(0.0, config.min_hr_score - 0.04)
        config.min_hit_score = max(0.0, config.min_hit_score - 0.03)

    # Late/live slates can have fewer available players; adapt the floor so
    # dashboard generation still produces slips instead of hard failing at 40.
    available_players = max(0, len(df))
    adaptive_floor = max(12, min(int(config.min_player_pool), available_players))
    config.min_player_pool = adaptive_floor

    result = generate_portfolio_board(df, config=config)
    save_run(result["run_id"], result, run_label=f"dashboard_{mode}_{num_slips}x{legs_per_slip}")
    return result


def _dashboard_config(
    num_slips: int,
    mode: str,
    legs_per_slip: int,
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    lineup_locked_only: bool = False,
) -> PortfolioConfig:
    mode_key = _normalize_dashboard_mode(mode)
    risk_key = _normalize_risk_level(risk_level)
    hits_profile_key = _normalize_hits_profile(hits_profile)

    random_seed = int(datetime.now(tz=timezone.utc).timestamp() * 1000) % 2_147_483_647
    if mode_key == "balanced":
        ownership_penalty_strength = 0.85
        anchor_every_n_slips = 0
        min_hr_score = 0.54
        relax_hr_score_floor = 0.40
        hr_candidate_pool_size = 64
        min_selected_leg_hr_score = 0.0
        strict_hr_leg_floor = False
    elif mode_key == "cream":
        ownership_penalty_strength = 0.55
        anchor_every_n_slips = 4
        min_hr_score = 0.64
        relax_hr_score_floor = 0.48
        hr_candidate_pool_size = 52
        min_selected_leg_hr_score = 0.16
        strict_hr_leg_floor = False
    elif mode_key == "extreme-cream":
        ownership_penalty_strength = 0.35
        anchor_every_n_slips = 4
        min_hr_score = 0.72
        relax_hr_score_floor = 0.58
        hr_candidate_pool_size = 56
        min_selected_leg_hr_score = 0.14
        strict_hr_leg_floor = True
    elif mode_key == "chalk-city":
        ownership_penalty_strength = 0.10
        anchor_every_n_slips = 0
        min_hr_score = 0.46
        relax_hr_score_floor = 0.28
        hr_candidate_pool_size = 140
        min_selected_leg_hr_score = 0.0
        strict_hr_leg_floor = False
    elif mode_key == "random":
        ownership_penalty_strength = 0.06
        anchor_every_n_slips = 0
        min_hr_score = 0.20
        relax_hr_score_floor = 0.12
        hr_candidate_pool_size = 220
        min_selected_leg_hr_score = 0.0
        strict_hr_leg_floor = False
    else:
        ownership_penalty_strength = 0.20
        anchor_every_n_slips = 0
        min_hr_score = 0.42
        relax_hr_score_floor = 0.30
        hr_candidate_pool_size = 120
        min_selected_leg_hr_score = 0.0
        strict_hr_leg_floor = False

    hits_filter_enabled = mode_key in {"hits", "hits-tb-combo"}
    min_hit_score = 0.42
    min_recent_hits = 5
    min_pitcher_vuln_score = 0.40
    min_tb_score = 0.0
    min_rbi_score = 0.0
    min_hrr_score = 0.0
    if mode_key in {"hits", "hits-tb-combo"}:
        if hits_profile_key == "random":
            hits_filter_enabled = False
            min_hit_score = 0.0
            min_recent_hits = 0
            min_pitcher_vuln_score = 0.0
        elif hits_profile_key == "singles":
            min_hit_score = 0.34
            min_recent_hits = 3
            min_pitcher_vuln_score = 0.26
        elif hits_profile_key == "doubles":
            min_hit_score = 0.52
            min_recent_hits = 6
            min_pitcher_vuln_score = 0.34
        elif hits_profile_key == "triples":
            min_hit_score = 0.62
            min_recent_hits = 8
            min_pitcher_vuln_score = 0.38
        elif hits_profile_key == "one-plus":
            min_hit_score = 0.36
            min_recent_hits = 3
            min_pitcher_vuln_score = 0.28
        elif hits_profile_key == "two-plus":
            min_hit_score = 0.50
            min_recent_hits = 6
            min_pitcher_vuln_score = 0.34
        elif hits_profile_key == "three-plus":
            min_hit_score = 0.62
            min_recent_hits = 9
            min_pitcher_vuln_score = 0.38
        elif hits_profile_key == "tb-1":
            min_hit_score = 0.34
            min_recent_hits = 3
            min_pitcher_vuln_score = 0.26
            min_tb_score = 0.28
        elif hits_profile_key == "tb-2":
            min_hit_score = 0.44
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.30
            min_tb_score = 0.44
        elif hits_profile_key == "tb-3":
            min_hit_score = 0.52
            min_recent_hits = 5
            min_pitcher_vuln_score = 0.33
            min_tb_score = 0.58
        elif hits_profile_key == "tb-4":
            min_hit_score = 0.60
            min_recent_hits = 6
            min_pitcher_vuln_score = 0.36
            min_tb_score = 0.72
        elif hits_profile_key == "hrr-1":
            min_hit_score = 0.34
            min_recent_hits = 3
            min_pitcher_vuln_score = 0.26
            min_hrr_score = 0.30
        elif hits_profile_key == "hrr-2":
            min_hit_score = 0.42
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.30
            min_hrr_score = 0.46
        elif hits_profile_key == "hrr-3":
            min_hit_score = 0.50
            min_recent_hits = 5
            min_pitcher_vuln_score = 0.34
            min_hrr_score = 0.60
        elif hits_profile_key == "hrr-4":
            min_hit_score = 0.58
            min_recent_hits = 6
            min_pitcher_vuln_score = 0.38
            min_hrr_score = 0.74
        elif hits_profile_key == "rbi-1":
            min_hit_score = 0.34
            min_recent_hits = 3
            min_pitcher_vuln_score = 0.28
            min_rbi_score = 0.30
        elif hits_profile_key == "rbi-2":
            min_hit_score = 0.42
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.32
            min_rbi_score = 0.46
        elif hits_profile_key == "rbi-3":
            min_hit_score = 0.50
            min_recent_hits = 5
            min_pitcher_vuln_score = 0.36
            min_rbi_score = 0.60
        elif hits_profile_key == "rbi-4":
            min_hit_score = 0.58
            min_recent_hits = 6
            min_pitcher_vuln_score = 0.40
            min_rbi_score = 0.74
        elif hits_profile_key == "combo":
            min_hit_score = 0.44
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.32
            min_tb_score = 0.42
            min_rbi_score = 0.36
            min_hrr_score = 0.36
        elif hits_profile_key == "vs-bad-pitchers":
            min_hit_score = 0.38
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.62
        elif hits_profile_key == "streakers":
            min_hit_score = 0.44
            min_recent_hits = 8
            min_pitcher_vuln_score = 0.34
        elif hits_profile_key == "contact-kings":
            min_hit_score = 0.56
            min_recent_hits = 7
            min_pitcher_vuln_score = 0.30
        elif hits_profile_key == "stack-attack":
            min_hit_score = 0.40
            min_recent_hits = 4
            min_pitcher_vuln_score = 0.36

    if mode_key in {"hits", "hits-tb-combo"}:
        max_same_game_legs = min(max(4, legs_per_slip), legs_per_slip)
        max_same_team_legs = min(max(4, legs_per_slip), legs_per_slip)
        min_time_buckets_per_slip = 1
        max_player_exposure = 12 if legs_per_slip >= 10 else 8
        hr_hitter_filter_enabled = False
    elif mode_key == "chalk-city":
        max_same_game_legs = min(max(4, legs_per_slip), legs_per_slip)
        max_same_team_legs = min(max(4, legs_per_slip), legs_per_slip)
        min_time_buckets_per_slip = 1
        max_player_exposure = 10 if legs_per_slip >= 10 else 7
        hr_hitter_filter_enabled = True
    elif mode_key == "random":
        max_same_game_legs = min(max(5, legs_per_slip), legs_per_slip)
        max_same_team_legs = min(max(5, legs_per_slip), legs_per_slip)
        min_time_buckets_per_slip = 1
        max_player_exposure = 12 if legs_per_slip >= 10 else 9
        hr_hitter_filter_enabled = False
    elif legs_per_slip >= 10:
        max_same_game_legs = min(4, legs_per_slip)
        max_same_team_legs = min(4, legs_per_slip)
        min_time_buckets_per_slip = 1
        max_player_exposure = 5
        hr_hitter_filter_enabled = True
    elif legs_per_slip >= 7:
        max_same_game_legs = min(3, legs_per_slip)
        max_same_team_legs = min(3, legs_per_slip)
        min_time_buckets_per_slip = 1
        max_player_exposure = 5
        hr_hitter_filter_enabled = True
    elif legs_per_slip >= 4:
        max_same_game_legs = min(2, legs_per_slip)
        max_same_team_legs = min(2, legs_per_slip)
        min_time_buckets_per_slip = min(2, legs_per_slip)
        max_player_exposure = 4
        hr_hitter_filter_enabled = True
    else:
        max_same_game_legs = min(2, legs_per_slip)
        max_same_team_legs = min(2, legs_per_slip)
        min_time_buckets_per_slip = min(2, legs_per_slip)
        max_player_exposure = 3
        hr_hitter_filter_enabled = True

    if risk_key == "safe":
        ownership_penalty_strength = min(1.6, ownership_penalty_strength + 0.25)
        max_player_exposure = max(2, max_player_exposure - 2)
        min_hr_score = min(1.0, min_hr_score + 0.08)
        min_hit_score = min(1.0, min_hit_score + 0.06)
        min_tb_score = min(1.0, min_tb_score + 0.05)
        min_rbi_score = min(1.0, min_rbi_score + 0.05)
        min_hrr_score = min(1.0, min_hrr_score + 0.05)
    elif risk_key == "yolo":
        ownership_penalty_strength = max(0.0, ownership_penalty_strength - 0.18)
        max_player_exposure = min(20, max_player_exposure + 2)
        min_hr_score = max(0.0, min_hr_score - 0.10)
        min_hit_score = max(0.0, min_hit_score - 0.08)
        min_tb_score = max(0.0, min_tb_score - 0.07)
        min_rbi_score = max(0.0, min_rbi_score - 0.07)
        min_hrr_score = max(0.0, min_hrr_score - 0.07)

    hermes = _load_hermes_adjustments()
    adjustments = hermes.get("adjustments", {}) if isinstance(hermes, dict) else {}
    if isinstance(adjustments, dict):
        exposure_override = adjustments.get("max_player_exposure")
        if exposure_override is not None:
            try:
                max_player_exposure = max(1, min(20, int(exposure_override)))
            except Exception:
                pass

        ownership_delta = adjustments.get("ownership_penalty_delta")
        if ownership_delta is not None:
            try:
                ownership_penalty_strength = _clamp_float(
                    ownership_penalty_strength + float(ownership_delta), 0.0, 2.0
                )
            except Exception:
                pass

    archetype_weight_multipliers = {
        archetype: 1.0 for archetype in ARCHETYPES
    }
    archetype_deltas = adjustments.get("archetype_weight_deltas", {}) if isinstance(adjustments, dict) else {}
    if isinstance(archetype_deltas, dict):
        for archetype in ARCHETYPES:
            raw_delta = archetype_deltas.get(archetype)
            if raw_delta is None:
                continue
            try:
                archetype_weight_multipliers[archetype] = _clamp_float(1.0 + float(raw_delta), 0.4, 1.6)
            except Exception:
                continue

    return PortfolioConfig(
        num_slips=num_slips,
        legs_per_slip=legs_per_slip,
        min_player_pool=40,
        max_player_exposure=max_player_exposure,
        allow_override_max_exposure=True,
        selection_mode=mode_key,
        ownership_penalty_strength=ownership_penalty_strength,
        anchor_every_n_slips=anchor_every_n_slips,
        max_same_game_legs=max_same_game_legs,
        max_same_team_legs=max_same_team_legs,
        min_time_buckets_per_slip=min_time_buckets_per_slip,
        adaptive_learning_enabled=True,
        learning_lookback_days=45,
        learning_min_samples=8,
        adaptive_archetype_weights=archetype_weight_multipliers,
        hr_hitter_filter_enabled=hr_hitter_filter_enabled,
        min_hr_score=min_hr_score,
        relax_hr_score_floor=relax_hr_score_floor,
        hr_candidate_pool_size=hr_candidate_pool_size,
        min_selected_leg_hr_score=min_selected_leg_hr_score,
        strict_hr_leg_floor=strict_hr_leg_floor,
        hits_profile=hits_profile_key,
        hits_filter_enabled=hits_filter_enabled,
        min_hit_score=min_hit_score,
        min_recent_hits=min_recent_hits,
        min_pitcher_vuln_score=min_pitcher_vuln_score,
        min_tb_score=min_tb_score,
        min_rbi_score=min_rbi_score,
        min_hrr_score=min_hrr_score,
        risk_level=risk_key,
        lineup_locked_only=lineup_locked_only,
        random_seed=random_seed,
    )


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _record_value(record: dict, keys: list[str], default=None):
    for key in keys:
        if key not in record:
            continue
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _build_dashboard_player_pool(lineup_locked_only: bool = False, allow_live: bool = False) -> list[dict]:
    csv_path = _dashboard_source_csv_path()
    if not csv_path.exists():
        raise ValueError(
            f"Dashboard source CSV not found: {csv_path}. Run /agents/run-once first or set WW_DASHBOARD_SOURCE_CSV."
        )

    df = load_slate_from_csv(str(csv_path))
    if lineup_locked_only:
        df = _lineup_locked_filter(df)
    if not allow_live:
        df = _exclude_started_games(df)
        if df.empty:
            return []

    scored_df = df.copy()
    required_score_columns = {
        "hr_score",
        "hit_score",
        "tb_score",
        "rbi_score",
        "hrr_score",
        "portfolio_value_score",
        "pitcher_vuln_score",
        "recent_hits_score",
        "leverage_score",
        "environment_score",
        "correlation_score",
        "chaos_score",
    }
    if not required_score_columns.issubset(set(scored_df.columns)):
        scored_df = add_scores(scored_df)

    rank_adjustments = _dashboard_rank_adjustments()
    category_weights = dict(rank_adjustments.get("categories") or {})
    subcategory_weights = dict(rank_adjustments.get("subcategories") or {})

    records = [row for row in scored_df.to_dict(orient="records") if isinstance(row, dict)]

    by_player: dict[str, dict] = {}
    for row in records:
        if not isinstance(row, dict):
            continue
        player_name = str(
            _record_value(
                row,
                ["player_name", "player", "name", "full_name", "batter_name"],
                default="",
            )
            or ""
        ).strip()
        if not player_name:
            continue
        player_key = _norm_player_name(player_name)
        if not player_key:
            continue

        team = str(_record_value(row, ["team", "team_abbrev", "team_code"], default="") or "").strip().upper()
        park = str(_record_value(row, ["park_name", "park", "venue"], default="") or "").strip()
        bats = str(_record_value(row, ["bats", "bat_hand", "hand"], default="R") or "R").strip().upper()
        pitcher_name = str(_record_value(row, ["pitcher_name", "pitcher"], default="") or "").strip()
        pitcher_hand = str(_record_value(row, ["pitcher_hand", "pitchingType"], default="") or "").strip().upper()
        park_lhb_boost = _to_float(_record_value(row, ["park_lhb_boost"], default=1.0), 1.0)
        park_rhb_boost = _to_float(_record_value(row, ["park_rhb_boost"], default=1.0), 1.0)
        handed_park_boost = park_lhb_boost if bats == "L" else park_rhb_boost
        projected_ownership = _to_float(_record_value(row, ["projected_ownership", "ownership"], default=0.12), 0.12)
        lineup_slot = max(1.0, _to_float(_record_value(row, ["lineup_slot", "batting_order"], default=9.0), 9.0))
        implied_total = _to_float(_record_value(row, ["implied_total"], default=0.0), 0.0)
        hr_allowed = _to_float(_record_value(row, ["hr_allowed"], default=0.0), 0.0)
        pitcher_fatigue = _to_float(_record_value(row, ["pitcher_fatigue"], default=0.0), 0.0)
        bullpen_quality = _to_float(_record_value(row, ["bullpen_quality"], default=0.0), 0.0)
        environment_score = _to_float(_record_value(row, ["environment_score"], default=0.0), 0.0)
        leverage_score = _to_float(_record_value(row, ["leverage_score"], default=0.0), 0.0)
        pitcher_vuln_score = _to_float(_record_value(row, ["pitcher_vuln_score", "matchup_score"], default=0.0), 0.0)
        correlation_score = _to_float(_record_value(row, ["correlation_score"], default=0.0), 0.0)
        chaos_score = _to_float(_record_value(row, ["chaos_score"], default=0.0), 0.0)
        combo_score = _to_float(_record_value(row, ["combo_score"], default=0.0), 0.0)
        hr_score = _to_float(_record_value(row, ["hr_score", "home_run_score"], default=0.0), 0.0)
        hit_score = _to_float(_record_value(row, ["hit_score", "hits_score"], default=0.0), 0.0)
        tb_score = _to_float(_record_value(row, ["tb_score", "total_bases_score"], default=0.0), 0.0)
        rbi_score = _to_float(_record_value(row, ["rbi_score"], default=0.0), 0.0)
        hrr_score = _to_float(_record_value(row, ["hrr_score"], default=0.0), 0.0)
        portfolio_value_score = _to_float(_record_value(row, ["portfolio_value_score", "value_score"], default=0.0), 0.0)
        recent_hits = _to_float(_record_value(row, ["recent_hits", "hits_last_10"], default=0.0), 0.0)
        recent_hits_score = _to_float(_record_value(row, ["recent_hits_score"], default=0.0), 0.0)
        recent_hr_form = _to_float(_record_value(row, ["recent_hr_form"], default=0.0), 0.0)
        recent_hr_streak = _to_float(_record_value(row, ["recent_hr_streak"], default=0.0), 0.0)
        park_hr_factor = _to_float(_record_value(row, ["park_hr_factor"], default=1.0), 1.0)
        short_porch = _to_float(_record_value(row, ["short_porch"], default=0.0), 0.0)
        lineup_slot_bonus = max(0.0, (10.0 - lineup_slot) / 9.0)
        ownership_discount = max(0.0, 0.20 - projected_ownership)
        matchup_context_score = (
            pitcher_vuln_score * 0.55
            + min(1.0, hr_allowed / 24.0) * 0.20
            + pitcher_fatigue * 0.15
            + (1.0 - bullpen_quality) * 0.10
        )
        park_context_score = (
            environment_score * 0.45
            + min(1.0, max(0.0, park_hr_factor - 1.0) * 2.5) * 0.20
            + min(1.0, max(0.0, handed_park_boost - 1.0) * 4.0) * 0.20
            + min(1.0, short_porch) * 0.15
        )

        recent_form_score = (
            recent_hits_score * 0.52
            + recent_hr_form * 0.33
            + min(1.0, recent_hr_streak / 3.0) * 0.15
        ) * float(category_weights.get("recent", 1.0) or 1.0)
        hr_rank_score = hr_score * float(category_weights.get("hr", 1.0) or 1.0)
        hit_rank_score = hit_score * float(category_weights.get("hits", 1.0) or 1.0)
        tb_rank_score = tb_score * float(category_weights.get("tb", 1.0) or 1.0)
        rbi_rank_score = rbi_score * float(category_weights.get("rbi", 1.0) or 1.0)
        hrr_rank_score = hrr_score * float(category_weights.get("hrr", 1.0) or 1.0)
        value_rank_base = portfolio_value_score * float(category_weights.get("value", 1.0) or 1.0)
        matchup_rank_score = matchup_context_score * float(category_weights.get("matchup", 1.0) or 1.0)
        contextual_rank = (
            value_rank_base * 0.24
            + hr_rank_score * 0.16
            + combo_score * 0.12
            + recent_form_score * 0.12
            + matchup_rank_score * 0.10
            + park_context_score * 0.08
            + leverage_score * 0.07
            + correlation_score * 0.05
            + chaos_score * 0.04
            + lineup_slot_bonus * 0.02
            + min(1.0, implied_total / 7.0) * 0.04
            + min(1.0, recent_hits / 8.0) * 0.03
            + ownership_discount * 0.18
        ) * float(subcategory_weights.get("overall", 1.0) or 1.0)
        safe_rank_score = (
            hit_rank_score * 0.24
            + combo_score * 0.20
            + recent_form_score * 0.18
            + tb_rank_score * 0.10
            + rbi_rank_score * 0.08
            + environment_score * 0.08
            + lineup_slot_bonus * 0.07
            + min(1.0, implied_total / 7.0) * 0.05
        ) * float(subcategory_weights.get("safe", 1.0) or 1.0)
        value_rank_score = (
            value_rank_base * 0.44
            + leverage_score * 0.18
            + contextual_rank * 0.14
            + recent_form_score * 0.08
            + matchup_rank_score * 0.06
            + park_context_score * 0.05
            + ownership_discount * 0.25
        ) * float(subcategory_weights.get("value", 1.0) or 1.0)
        contrarian_rank_score = (
            leverage_score * 0.34
            + chaos_score * 0.22
            + hr_rank_score * 0.14
            + recent_form_score * 0.10
            + matchup_rank_score * 0.08
            + park_context_score * 0.05
            + contextual_rank * 0.03
            + ownership_discount * 0.30
        ) * float(subcategory_weights.get("contrarian", 1.0) or 1.0)

        player = {
            "player_name": player_name,
            "team": team,
            "park_name": park,
            "bats": bats,
            "game_id": int(_to_float(_record_value(row, ["game_id", "gameid"], default=0), 0.0)) or None,
            "hr_score": hr_score,
            "hit_score": hit_score,
            "tb_score": tb_score,
            "rbi_score": rbi_score,
            "hrr_score": hrr_score,
            "hr_rank_score": hr_rank_score,
            "hit_rank_score": hit_rank_score,
            "tb_rank_score": tb_rank_score,
            "rbi_rank_score": rbi_rank_score,
            "hrr_rank_score": hrr_rank_score,
            "combo_score": combo_score,
            "portfolio_value_score": portfolio_value_score,
            "pitcher_vuln_score": pitcher_vuln_score,
            "matchup_context_score": matchup_context_score,
            "recent_hits": recent_hits,
            "last5_hits": _json_float_or_none(_record_value(row, ["last5_hits"], default=None)),
            "last5_total_bases": _json_float_or_none(_record_value(row, ["last5_total_bases"], default=None)),
            "last5_home_runs": _json_float_or_none(_record_value(row, ["last5_home_runs"], default=None)),
            "recent_hits_score": recent_hits_score,
            "recent_hr_form": recent_hr_form,
            "recent_hr_streak": recent_hr_streak,
            "recent_form_score": recent_form_score,
            "pitcher_name": pitcher_name,
            "pitcher_hand": pitcher_hand,
            "pitcher_fatigue": pitcher_fatigue,
            "hr_allowed": hr_allowed,
            "bullpen_quality": bullpen_quality,
            "environment_score": environment_score,
            "leverage_score": leverage_score,
            "correlation_score": correlation_score,
            "chaos_score": chaos_score,
            "park_context_score": park_context_score,
            "park_hr_factor": park_hr_factor,
            "park_lhb_boost": park_lhb_boost,
            "park_rhb_boost": park_rhb_boost,
            "handed_park_boost": handed_park_boost,
            "short_porch": short_porch,
            "dome": _to_float(_record_value(row, ["dome"], default=0.0), 0.0),
            "wind_speed": _to_float(_record_value(row, ["wind_speed"], default=0.0), 0.0),
            "wind_direction": str(_record_value(row, ["wind_direction"], default="") or "").strip(),
            "temperature": _to_float(_record_value(row, ["temperature"], default=0.0), 0.0),
            "implied_total": implied_total,
            "lineup_slot": lineup_slot,
            "lineup_slot_bonus": lineup_slot_bonus,
            "projected_ownership": projected_ownership,
            "contextual_rank_score": contextual_rank,
            "overall_rank_score": contextual_rank,
            "safe_rank_score": safe_rank_score,
            "value_rank_score": value_rank_score,
            "contrarian_rank_score": contrarian_rank_score,
        }

        existing = by_player.get(player_key)
        if existing is None:
            by_player[player_key] = player
            continue

        current_value = float(existing.get("contextual_rank_score") or 0.0)
        incoming_value = float(player.get("contextual_rank_score") or 0.0)
        if incoming_value > current_value:
            by_player[player_key] = player

    players = list(by_player.values())
    players.sort(
        key=lambda p: (
            float(p.get("contextual_rank_score") or 0.0),
            float(p.get("portfolio_value_score") or 0.0),
            float(p.get("hr_score") or 0.0),
            float(p.get("hit_score") or 0.0),
        ),
        reverse=True,
    )
    return players


def _extract_players_from_screenshot_text(
    screenshot_text: str,
    player_pool: list[dict],
    max_players: int = 10,
) -> list[dict]:
    text = str(screenshot_text or "").strip()
    if not text:
        return []

    text_norm = _norm_player_name(text)
    text_lower = text.lower()
    picks: list[dict] = []
    seen: set[str] = set()

    for player in player_pool:
        name = str(player.get("player_name") or "").strip()
        if not name:
            continue
        key = _norm_player_name(name)
        if not key or key in seen:
            continue

        full_match = key in text_norm
        last_name = name.split(" ")[-1].strip().lower()
        last_match = bool(last_name) and len(last_name) >= 5 and re.search(rf"\b{re.escape(last_name)}\b", text_lower)
        if not (full_match or last_match):
            continue

        picks.append(player)
        seen.add(key)
        if len(picks) >= max(1, int(max_players)):
            break

    return picks


def _to_playbook_leg(player: dict) -> dict:
    return {
        "player_name": str(player.get("player_name") or "").strip(),
        "team": str(player.get("team") or "").strip(),
        "game_id": player.get("game_id"),
        "start_time_bucket": "",
        "park_name": str(player.get("park_name") or "").strip(),
        "portfolio_value_score": float(player.get("portfolio_value_score") or 0.0),
        "hr_score": float(player.get("hr_score") or 0.0),
        "hit_score": float(player.get("hit_score") or 0.0),
        "tb_score": float(player.get("tb_score") or 0.0),
        "rbi_score": float(player.get("rbi_score") or 0.0),
        "hrr_score": float(player.get("hrr_score") or 0.0),
        "pitcher_vuln_score": float(player.get("pitcher_vuln_score") or 0.0),
        "recent_hits": int(float(player.get("recent_hits") or 0.0)),
        "leverage_score": 0.0,
        "environment_score": 0.0,
        "correlation_score": 0.0,
        "chaos_score": 0.0,
        "projected_ownership": float(player.get("projected_ownership") or 0.12),
        "archetype_candidates": ["Controlled Chaos"],
        "rank_category": "twitter",
    }


def _pin_playbook_players(parlays: list[dict], pinned_players: list[dict]) -> int:
    if not parlays or not pinned_players:
        return 0

    pinned_legs = [_to_playbook_leg(player) for player in pinned_players]
    pinned_count = 0
    for idx, slip in enumerate(parlays):
        if idx >= len(pinned_legs):
            break
        if not isinstance(slip, dict):
            continue
        legs = slip.get("legs")
        if not isinstance(legs, list) or not legs:
            continue

        leg_names = {_norm_player_name(str(leg.get("player_name") or "")) for leg in legs if isinstance(leg, dict)}
        desired = pinned_legs[idx]
        desired_key = _norm_player_name(desired.get("player_name") or "")
        if desired_key and desired_key in leg_names:
            continue

        weakest_index = 0
        weakest_score = float("inf")
        for leg_idx, leg in enumerate(legs):
            if not isinstance(leg, dict):
                continue
            score = float(leg.get("portfolio_value_score") or 0.0)
            if score < weakest_score:
                weakest_score = score
                weakest_index = leg_idx
        legs[weakest_index] = desired
        pinned_count += 1

    return pinned_count


def _normalize_betslip_text(raw_text: str) -> str:
    lines = [
        str(line).strip(" -\t")
        for line in str(raw_text or "").replace("\r\n", "\n").split("\n")
    ]
    filtered = [line for line in lines if line]
    return "\n".join(filtered)


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="theme-color" content="#d2a23d" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <link rel="manifest" href="/dashboard/app-manifest.json?v=gold-k-crown-2" />
    <link rel="icon" href="/dashboard/icon.svg?v=gold-k-crown-2" type="image/svg+xml" />
    <link rel="apple-touch-icon" href="/dashboard/icon.svg?v=gold-k-crown-2" />
    <title>DINGKING Dashboard</title>
    <style>
        :root {
            --bg: #160b00;
            --bg-alt: #3a2100;
            --ink: #fff5d1;
            --ink-soft: #ead18d;
            --accent: #ffcf47;
            --accent-2: #fff0b8;
            --royal: #dca332;
            --ruby: #ff9e4b;
            --panel: rgba(74, 42, 3, 0.88);
            --line: rgba(255, 214, 104, 0.5);
            --line-strong: rgba(255, 238, 172, 0.95);
            --glow: 0 0 0 1px rgba(255, 220, 122, 0.4), 0 28px 78px rgba(0, 0, 0, 0.56);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            font-family: "Trebuchet MS", "Avenir Next", "Segoe UI", sans-serif;
            background:
                radial-gradient(circle at 8% 10%, rgba(255, 227, 130, 0.42) 0%, transparent 44%),
                radial-gradient(circle at 90% 8%, rgba(255, 243, 182, 0.34) 0%, transparent 40%),
                radial-gradient(circle at 75% 92%, rgba(255, 171, 64, 0.3) 0%, transparent 36%),
                linear-gradient(140deg, var(--bg) 0%, var(--bg-alt) 52%, #090400 100%);
            min-height: 100vh;
            overflow-x: hidden;
        }
        body::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            opacity: 0.46;
            background-image:
                radial-gradient(circle, rgba(255, 215, 88, 0.88) 0 2px, transparent 3px),
                radial-gradient(circle, rgba(255, 239, 160, 0.78) 0 1.5px, transparent 2.5px),
                radial-gradient(circle, rgba(255, 168, 70, 0.62) 0 1.5px, transparent 2.5px);
            background-size: 100px 100px, 140px 140px, 180px 180px;
            background-position: 0 0, 34px 56px, 84px 26px;
            animation: festiveDrift 24s linear infinite;
        }
        body::after {
            content: "";
            position: fixed;
            inset: -40% -20%;
            pointer-events: none;
            background:
                linear-gradient(118deg, transparent 34%, rgba(255, 231, 132, 0.2) 50%, transparent 66%),
                linear-gradient(82deg, transparent 43%, rgba(255, 196, 72, 0.14) 50%, transparent 57%);
            animation: royalSheen 8.6s linear infinite;
            z-index: 0;
        }
        .wrap {
            width: min(1320px, 94vw);
            margin: 0 auto;
            padding: 30px 0 42px;
            position: relative;
            z-index: 1;
            overflow: visible;
        }
        .brand {
            margin: 0;
            font-size: clamp(40px, 5vw, 74px);
            letter-spacing: 2.2px;
            text-transform: uppercase;
            color: #fff2cb;
            text-shadow: 0 0 26px rgba(255, 214, 103, 0.62), 0 0 46px rgba(188, 119, 0, 0.34);
            display: inline-flex;
            align-items: baseline;
            gap: 2px;
        }
        .k-crown {
            position: relative;
            display: inline-block;
            color: #ffde70;
            text-shadow: 0 0 18px rgba(255, 216, 100, 0.8), 0 0 34px rgba(185, 116, 0, 0.44);
            padding-top: 0;
            transform: scale(1.34);
            transform-origin: bottom center;
        }
        .k-crown::before {
            content: "";
            position: absolute;
            top: -18px;
            left: 50%;
            width: 50px;
            height: 28px;
            transform: translateX(-50%);
            background: linear-gradient(180deg, #fff2bb 0%, #ffd267 48%, #e39a1b 100%);
            clip-path: polygon(0 100%, 8% 44%, 26% 66%, 40% 18%, 56% 66%, 74% 44%, 86% 72%, 100% 100%);
            filter: drop-shadow(0 4px 5px rgba(0, 0, 0, 0.48));
        }
        .k-crown::after {
            content: "";
            position: absolute;
            top: -3px;
            left: 50%;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            transform: translateX(-50%);
            background: var(--ruby);
            box-shadow: 0 0 18px rgba(255, 163, 74, 0.98);
        }
        .sub {
            color: var(--ink-soft);
            margin: 8px 0 18px;
            font-size: 14px;
        }
        .toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 16px;
            padding: 12px;
            border-radius: 16px;
            border: 1px solid var(--line);
            background:
                linear-gradient(165deg, rgba(88, 51, 7, 0.92), rgba(35, 22, 2, 0.94)),
                linear-gradient(80deg, rgba(255, 223, 129, 0.08), transparent 56%);
            box-shadow: var(--glow);
            backdrop-filter: blur(8px);
            position: relative;
            z-index: 80;
            overflow: visible;
        }
        .toolbar label {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 14px;
            font-weight: 800;
            color: #fff0c2;
        }
        .mode-help {
            flex-basis: 100%;
            margin-top: 2px;
            font-size: 12px;
            color: #f4d68f;
            line-height: 1.35;
        }
        button,
        select {
            border: 1px solid var(--line);
            padding: 12px 14px;
            border-radius: 12px;
            font-weight: 700;
            color: #2a1702 !important;
            background: linear-gradient(180deg, rgba(92, 57, 8, 0.95), rgba(54, 33, 4, 0.95));
        }
        select {
            text-shadow: none;
            -webkit-text-fill-color: #2a1702 !important;
            appearance: none;
            background: linear-gradient(135deg, #fff0b8 0%, #ffd56e 52%, #e8a62d 100%);
            forced-color-adjust: none;
            border-color: rgba(255, 235, 173, 0.92);
        }
        select option {
            color: #1e1101 !important;
            background: #ffe7a5 !important;
            font-weight: 800;
        }
        select optgroup {
            color: #1e1101 !important;
            background: #ffe7a5 !important;
            font-weight: 900;
        }
        select option:checked,
        select option:hover,
        select option:focus {
            color: #1a0d00 !important;
            background: #ffcc58 !important;
        }
        select:focus {
            outline: 2px solid rgba(255, 229, 141, 0.75);
            outline-offset: 1px;
        }
        .native-select-hidden {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            opacity: 0 !important;
            pointer-events: none !important;
            overflow: hidden !important;
        }
        .dk-select {
            position: relative;
            min-width: 240px;
            display: inline-flex;
            flex-direction: column;
        }
        .dk-select-btn {
            border: 1px solid var(--line);
            padding: 13px 15px;
            border-radius: 12px;
            font-weight: 900;
            font-size: 15px;
            line-height: 1.25;
            letter-spacing: 0.2px;
            color: #1c0f01 !important;
            text-shadow: 0 1px 0 rgba(255, 236, 174, 0.65);
            background: linear-gradient(135deg, #ffe7a0 0%, #f4bf49 55%, #d28e19 100%);
            text-align: left;
            min-height: 48px;
        }
        .dk-select-btn::after {
            content: "\25BE";
            float: right;
            color: #5a3406;
            margin-left: 10px;
        }
        .dk-select.open .dk-select-btn {
            border-color: var(--line-strong);
            box-shadow: 0 0 0 2px rgba(255, 229, 141, 0.35);
        }
        .dk-select-list {
            position: fixed;
            z-index: 99999;
            background: linear-gradient(170deg, #56320a, #2f1a02);
            border: 1px solid rgba(255, 232, 162, 0.78);
            border-radius: 12px;
            padding: 6px;
            display: none;
            max-height: 280px;
            overflow-y: auto;
            box-shadow: 0 18px 32px rgba(0, 0, 0, 0.45);
        }
        .dk-select-list.open {
            display: block;
        }
        .dk-select-option {
            width: 100%;
            text-align: left;
            border: 0;
            border-radius: 8px;
            margin: 0;
            padding: 11px 10px;
            font-size: 15px;
            line-height: 1.25;
            font-weight: 900;
            color: #ffedb6;
            text-shadow: 0 0 1px rgba(0, 0, 0, 0.35);
            background: transparent;
            cursor: pointer;
        }
        .dk-select-option.active::before {
            content: "\2713 ";
            color: #5a3508;
            font-weight: 900;
        }
        .dk-select-option:hover,
        .dk-select-option.active {
            color: #1e1001;
            background: linear-gradient(135deg, #ffe8a0, #f2c559);
        }
        button {
            cursor: pointer;
            transition: transform 120ms ease, box-shadow 120ms ease, border-color 120ms ease;
        }
        button:hover {
            transform: translateY(-1px);
            border-color: var(--line-strong);
            box-shadow: 0 8px 22px rgba(0, 0, 0, 0.4);
        }
        .btn-all {
            background: linear-gradient(135deg, #fff8dc 0%, #ffde72 38%, #f2b62e 70%, #cb8310 100%);
            color: #2a1801;
            border-color: rgba(255, 239, 180, 0.95);
            box-shadow: 0 8px 18px rgba(224, 150, 16, 0.34);
        }
        .btn-one {
            background: linear-gradient(135deg, #f7da86 0%, #e6b54a 46%, #b77719 100%);
            color: #2a1702;
            border-color: rgba(255, 229, 150, 0.9);
            box-shadow: 0 6px 14px rgba(177, 109, 11, 0.28);
        }
        .meta {
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 15px;
            padding: 12px;
            margin-bottom: 14px;
            font-size: 13px;
            color: #fff1c8;
            box-shadow: var(--glow);
            backdrop-filter: blur(8px);
        }
        .meta-row {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            margin-top: 6px;
            font-size: 12px;
            color: #f0d990;
        }
        .meta-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 10px;
            border-radius: 999px;
            border: 1px solid rgba(255, 228, 154, 0.35);
            background: rgba(39, 23, 3, 0.45);
            white-space: nowrap;
        }
        .status {
            font-weight: 800;
            margin-bottom: 6px;
            color: #fff5dc;
            letter-spacing: 0.3px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 12px;
        }
        .card {
            background:
                linear-gradient(170deg, rgba(80, 47, 6, 0.96), rgba(34, 21, 2, 0.96)),
                linear-gradient(96deg, rgba(255, 220, 122, 0.08), transparent 56%);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 12px;
            box-shadow: var(--glow);
            animation: pop 220ms ease;
            position: relative;
            overflow: hidden;
        }
        .card::before {
            content: "";
            position: absolute;
            inset: -30% auto auto -30%;
            width: 140px;
            height: 140px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(255, 221, 126, 0.36), transparent 70%);
            pointer-events: none;
        }
        .card::after {
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            background: linear-gradient(125deg, transparent 32%, rgba(255, 232, 139, 0.2) 50%, transparent 68%);
            transform: translateX(-120%);
            animation: cardSheen 4.2s ease-in-out infinite;
        }
        .card h3 {
            margin: 0 0 8px;
            font-size: 14px;
            color: #fff0c6;
            letter-spacing: 0.4px;
        }
        .story {
            font-size: 12px;
            color: #f8da8e;
            margin-bottom: 8px;
            min-height: 34px;
        }
        .leg {
            font-size: 12px;
            padding: 7px;
            border: 1px solid rgba(255, 220, 129, 0.58);
            border-radius: 8px;
            margin-bottom: 6px;
            background: linear-gradient(175deg, rgba(57, 34, 3, 0.95), rgba(37, 22, 2, 0.95));
            line-height: 1.3;
            color: #fff3d3;
        }
        .portal-overlay {
            position: fixed;
            inset: 0;
            background: rgba(4, 8, 18, 0.72);
            display: none;
            align-items: center;
            justify-content: center;
            padding: 20px;
            z-index: 40;
        }
        .portal-overlay.show {
            display: flex;
        }
        .portal-modal {
            width: min(460px, 92vw);
            background: linear-gradient(165deg, rgba(86, 50, 6, 0.96), rgba(36, 22, 3, 0.96));
            border: 1px solid var(--line);
            border-radius: 16px;
            padding: 16px;
            box-shadow: 0 22px 55px rgba(0, 0, 0, 0.55);
        }
        .portal-title {
            margin: 0 0 6px;
            font-size: 18px;
            font-weight: 800;
        }
        .portal-copy {
            margin: 0 0 12px;
            color: #f1da9e;
            font-size: 13px;
            line-height: 1.4;
        }
        .portal-actions {
            display: grid;
            grid-template-columns: 1fr;
            gap: 8px;
        }
        .portal-close {
            margin-top: 10px;
            width: 100%;
            background: linear-gradient(135deg, #d8a646, #9a6716);
            color: #fff1cd;
        }
        .builder {
            margin-top: 14px;
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 12px;
            background: linear-gradient(160deg, rgba(61, 36, 5, 0.9), rgba(26, 16, 3, 0.92));
            box-shadow: 0 16px 35px rgba(0, 0, 0, 0.35);
        }
        .builder h2 {
            margin: 0;
            font-size: 16px;
            color: #ffe9a8;
        }
        .builder-sub {
            margin-top: 4px;
            font-size: 12px;
            color: #e6c874;
        }
        .builder-tools {
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }
        .builder-panels {
            margin-top: 12px;
            display: grid;
            grid-template-columns: 1.4fr 1fr;
            gap: 10px;
        }
        .builder-card {
            border: 1px solid rgba(232, 186, 88, 0.36);
            border-radius: 12px;
            padding: 8px;
            background: rgba(33, 20, 3, 0.6);
            min-height: 190px;
        }
        .builder-card h3 {
            margin: 0 0 8px;
            font-size: 13px;
            color: #ffd670;
            letter-spacing: 0.03em;
        }
        .builder-list {
            display: grid;
            gap: 6px;
            max-height: 300px;
            overflow: auto;
        }
        .builder-row {
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 8px;
            align-items: center;
            border: 1px solid rgba(240, 196, 99, 0.28);
            border-radius: 10px;
            padding: 7px;
            background: rgba(25, 16, 2, 0.7);
        }
        .builder-row strong {
            display: block;
            font-size: 13px;
            color: #fff2cb;
        }
        .builder-row span {
            display: block;
            font-size: 11px;
            color: #d9ba74;
        }
        .btn-mini {
            padding: 6px 10px;
            font-size: 11px;
            border-radius: 9px;
            min-width: 58px;
        }
        .btn-mini[disabled] {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .builder-empty {
            font-size: 12px;
            color: #cfb271;
            padding: 8px;
            border: 1px dashed rgba(230, 186, 96, 0.4);
            border-radius: 10px;
        }
        .analysis {
            margin-top: 14px;
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 12px;
            background: linear-gradient(160deg, rgba(61, 36, 5, 0.9), rgba(26, 16, 3, 0.92));
            box-shadow: 0 16px 35px rgba(0, 0, 0, 0.35);
        }
        .analysis h2 {
            margin: 0;
            font-size: 16px;
            color: #ffe9a8;
        }
        .analysis-sub {
            margin-top: 4px;
            font-size: 12px;
            color: #e6c874;
        }
        .analysis-tools {
            margin-top: 10px;
            display: grid;
            gap: 8px;
        }
        .analysis-tools input,
        .analysis-tools textarea {
            width: 100%;
            border: 1px solid rgba(232, 186, 88, 0.36);
            border-radius: 10px;
            padding: 10px 12px;
            background: rgba(25, 16, 2, 0.7);
            color: #fff3d3;
            font: inherit;
        }
        .analysis-tools textarea {
            min-height: 92px;
            resize: vertical;
        }
        .analysis-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }
        .analysis-result {
            margin-top: 10px;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid rgba(240, 196, 99, 0.28);
            background: rgba(25, 16, 2, 0.7);
            color: #fff3d3;
            font-size: 13px;
            line-height: 1.4;
        }
        @keyframes pop {
            from { transform: translateY(6px) scale(0.99); opacity: 0.45; }
            to { transform: translateY(0) scale(1); opacity: 1; }
        }
        @keyframes festiveDrift {
            from { transform: translateY(0); }
            to { transform: translateY(120px); }
        }
        @keyframes royalSheen {
            from { transform: translateX(-30%); }
            to { transform: translateX(30%); }
        }
        @keyframes cardSheen {
            0%, 55% { transform: translateX(-120%); }
            85%, 100% { transform: translateX(120%); }
        }
        @media (max-width: 760px) {
            .toolbar {
                gap: 8px;
                padding: 10px;
            }
            .builder-panels {
                grid-template-columns: 1fr;
            }
            button,
            select,
            .dk-select {
                width: 100%;
            }
            .toolbar label {
                width: 100%;
                justify-content: space-between;
            }
        }
    </style>
</head>
<body>
    <main class="wrap">
        <h1 class="brand">DING<span class="k-crown">K</span>ING</h1>
        <div class="sub">20 Parlays with 1-20 legs. New ladders: Total Bases, HRR, RBI, plus lineup lock + late swap refresh.</div>
        <div class="toolbar">
            <button class="btn-all" id="btnAll">Generate All 20</button>
            <button class="btn-one" id="btnOne">Generate 1 New</button>
            <button class="btn-one" id="btnLateSwap">Late Swap Refresh</button>
            <button class="btn-one" id="btnCopyAll">One-Click Portal (All)</button>
            <button class="btn-one" id="btnInstall" style="display:none;">Install App</button>
            <label>
                Legs
                <select id="legsSel">
                    <option value="1">1</option>
                    <option value="2">2</option>
                    <option value="3">3</option>
                    <option value="4" selected>4</option>
                    <option value="5">5</option>
                    <option value="6">6</option>
                    <option value="7">7</option>
                    <option value="8">8</option>
                    <option value="9">9</option>
                    <option value="10">10</option>
                    <option value="11">11</option>
                    <option value="12">12</option>
                    <option value="13">13</option>
                    <option value="14">14</option>
                    <option value="15">15</option>
                    <option value="16">16</option>
                    <option value="17">17</option>
                    <option value="18">18</option>
                    <option value="19">19</option>
                    <option value="20">20</option>
                </select>
            </label>
            <select id="modeSel">
                            <option value="balanced">Core Power Mix (HR)</option>
                            <option value="cream">Premium Power Stack (HR)</option>
                            <option value="extreme-cream">Ceiling Bombs Only (HR)</option>
                            <option value="chalk-city">Chalk Power Press (HR)</option>
                            <option value="random">Random (Just For Fun)</option>
                            <option value="hits">Hits (+1 +2 +3)</option>
                            <option value="hits-tb-combo">Hits + Bases Combo</option>
            </select>
            <div class="mode-help">Core Power Mix: balanced power blend. Premium Power Stack: higher-ceiling HR focus. Ceiling Bombs Only: boom-or-bust max upside. Chalk Power Press: safest projected power core. Random: experimental mix. Hits (+1 +2 +3): hit ladder props. Hits + Bases Combo: mixes hit and total-base angles.</div>
            <select id="hitsProfileSel" style="display:none;">
                <option value="random">Random (Hits Fun)</option>
                <option value="singles">Singles</option>
                <option value="doubles">Doubles</option>
                <option value="triples">Triples</option>
                <option value="one-plus">1+ Hits</option>
                <option value="two-plus">2+ Hits</option>
                <option value="three-plus">3+ Hits</option>
                <option value="tb-1">1+ Total Bases</option>
                <option value="tb-2">2+ Total Bases</option>
                <option value="tb-3">3+ Total Bases</option>
                <option value="tb-4">4+ Total Bases</option>
                <option value="hrr-1">1+ HRR</option>
                <option value="hrr-2">2+ HRR</option>
                <option value="hrr-3">3+ HRR</option>
                <option value="hrr-4">4+ HRR</option>
                <option value="rbi-1">1+ RBI</option>
                <option value="rbi-2">2+ RBI</option>
                <option value="rbi-3">3+ RBI</option>
                <option value="rbi-4">4+ RBI</option>
                <option value="combo">Hits + TB Combo</option>
                <option value="high-frequency">High Frequency</option>
                <option value="vs-bad-pitchers">Good Hitters vs Bad Pitchers</option>
                <option value="streakers">Streakers Only</option>
                <option value="contact-kings">Contact Kings</option>
                <option value="stack-attack">Stack Attack</option>
            </select>
            <label>
                Risk
                <select id="riskSel">
                    <option value="safe">Safe-ish</option>
                    <option value="balanced" selected>Balanced</option>
                    <option value="yolo">YOLO</option>
                </select>
            </label>
            <label>
                <input id="lineupLockChk" type="checkbox" />
                Lock Lineups
            </label>
            <label>
                <input id="liveBetsChk" type="checkbox" />
                Include Live Games
            </label>
            <label>
                <input id="strictLinksChk" type="checkbox" checked />
                Exact Player+Prop Links
            </label>
        </div>
        <section class="meta">
            <div class="status" id="statusText">Loading...</div>
            <div id="metaText"></div>
            <div class="meta-row" id="metaRow">
                <span class="meta-pill" id="slateRefreshPill">Slate refresh: waiting</span>
                <span class="meta-pill" id="autoRefreshPill">Auto-refresh: on new slate data</span>
                <span class="meta-pill" id="linkAgentPill">Link agent: checking...</span>
            </div>
            <div style="margin-top:6px;font-size:12px;color:#e6cf92;">Tip: keep Exact Player+Prop Links on to open directly into prop pages (best for phone bet entry).</div>
        </section>
        <section class="builder" id="builderSection">
            <h2>Build Your Own Parlay</h2>
            <div class="builder-sub">Choose ranking category + subcategory, add players to your ticket, then launch One-Click Portal.</div>
            <div class="builder-tools">
                <select id="builderCategorySel">
                    <option value="hr">Home Run Rank</option>
                    <option value="hits">Hits Rank</option>
                    <option value="tb">Total Bases Rank</option>
                    <option value="rbi">RBI Rank</option>
                    <option value="hrr">HRR Rank</option>
                    <option value="value">Best Value</option>
                    <option value="matchup">Pitcher Matchup</option>
                    <option value="recent">Recent Hits</option>
                </select>
                <select id="builderSubcategorySel">
                    <option value="overall">Overall</option>
                    <option value="value">Value</option>
                    <option value="safe">Safe</option>
                    <option value="contrarian">Contrarian</option>
                </select>
                <select id="builderLimitSel">
                    <option value="40">Top 40</option>
                    <option value="80" selected>Top 80</option>
                    <option value="120">Top 120</option>
                    <option value="200">Top 200</option>
                </select>
                <button class="btn-one" id="builderRefreshBtn">Refresh Rankings</button>
                <button class="btn-one" id="builderClearBtn">Clear Ticket</button>
                <button class="btn-all" id="builderPortalBtn">One-Click Portal (Custom)</button>
            </div>
            <div class="builder-panels">
                <article class="builder-card">
                    <h3>Ranked Players</h3>
                    <div class="builder-list" id="builderRankings"></div>
                </article>
                <article class="builder-card">
                    <h3>Your Ticket</h3>
                    <div class="builder-list" id="builderTicket"></div>
                </article>
            </div>
        </section>
        <section class="analysis" id="analysisSection">
            <h2>Could-Have-Hit Checker</h2>
            <div class="analysis-sub">Paste a run ID and the players you want to treat as winners to see how many slips would have hit or came close.</div>
            <div class="analysis-tools">
                <input id="analysisRunId" type="text" placeholder="Run ID" />
                <textarea id="analysisWinners" placeholder="One winning player per line"></textarea>
                <div class="analysis-row">
                    <input id="analysisMinLegs" type="number" min="1" step="1" value="4" style="max-width:120px;" />
                    <button class="btn-all" id="analysisRunBtn">Check Combos</button>
                </div>
            </div>
            <div class="analysis-result" id="analysisResult">Run an analysis to see how many slips would have hit.</div>
        </section>
        <section class="grid" id="cards"></section>
    </main>

    <div class="portal-overlay" id="portalOverlay" role="dialog" aria-modal="true" aria-labelledby="portalTitle">
        <div class="portal-modal">
            <h2 class="portal-title" id="portalTitle">One-Click Sportsbook</h2>
            <p class="portal-copy" id="portalText">Choose where to open this slip.</p>
            <div class="portal-actions">
                <button class="btn-all" id="portalGambly">Open Gambly</button>
                <button class="btn-one" id="portalDraftkings">Open DraftKings</button>
            </div>
            <button class="portal-close" id="portalClose">Cancel</button>
        </div>
    </div>

    <script>
        const statusText = document.getElementById('statusText');
        const metaText = document.getElementById('metaText');
        const cards = document.getElementById('cards');
        const slateRefreshPill = document.getElementById('slateRefreshPill');
        const autoRefreshPill = document.getElementById('autoRefreshPill');
        const linkAgentPill = document.getElementById('linkAgentPill');
        const modeSel = document.getElementById('modeSel');
        const hitsProfileSel = document.getElementById('hitsProfileSel');
        const legsSel = document.getElementById('legsSel');
        const riskSel = document.getElementById('riskSel');
        const lineupLockChk = document.getElementById('lineupLockChk');
        const liveBetsChk = document.getElementById('liveBetsChk');
        const strictLinksChk = document.getElementById('strictLinksChk');
        const installBtn = document.getElementById('btnInstall');
        const lateSwapBtn = document.getElementById('btnLateSwap');
        const copyAllBtn = document.getElementById('btnCopyAll');
        const portalOverlay = document.getElementById('portalOverlay');
        const portalText = document.getElementById('portalText');
        const portalGambly = document.getElementById('portalGambly');
        const portalDraftkings = document.getElementById('portalDraftkings');
        const portalClose = document.getElementById('portalClose');
        const builderCategorySel = document.getElementById('builderCategorySel');
        const builderSubcategorySel = document.getElementById('builderSubcategorySel');
        const builderLimitSel = document.getElementById('builderLimitSel');
        const builderRefreshBtn = document.getElementById('builderRefreshBtn');
        const builderClearBtn = document.getElementById('builderClearBtn');
        const builderPortalBtn = document.getElementById('builderPortalBtn');
        const builderRankings = document.getElementById('builderRankings');
        const builderTicket = document.getElementById('builderTicket');
        const analysisRunId = document.getElementById('analysisRunId');
        const analysisWinners = document.getElementById('analysisWinners');
        const analysisMinLegs = document.getElementById('analysisMinLegs');
        const analysisRunBtn = document.getElementById('analysisRunBtn');
        const analysisResult = document.getElementById('analysisResult');
        let deferredInstallPrompt = null;
        let portalPayload = null;
        let portalGamblyLink = '';
        const STRICT_ONE_CLICK_MODE = true;
        window.__lastParlays = [];
        window.__builderPool = [];
        window.__builderTicket = [];
        const LAST_STATE_KEY = 'dk_last_state_v1';

        function cacheDashboardState(data) {
            try {
                if (!data || data.status !== 'ok') return;
                window.__lastState = data;
                localStorage.setItem(LAST_STATE_KEY, JSON.stringify(data));
            } catch {}
        }

        function formatSlateRefreshText(data) {
            const generatedAt = String(data?.generated_at || '').trim();
            if (!generatedAt) return 'Slate refresh: waiting';
            return `Slate refresh: ${generatedAt}`;
        }

        function syncSlateRefreshPill(data) {
            if (slateRefreshPill) {
                slateRefreshPill.textContent = formatSlateRefreshText(data);
            }
            if (autoRefreshPill) {
                autoRefreshPill.textContent = data?.source_csv_mtime
                    ? `Source updated: ${new Date(Number(data.source_csv_mtime) * 1000).toLocaleString()} (slips stay locked)`
                    : 'Source updated: waiting';
            }
                if (analysisRunId && data?.latest_run_id) {
                    analysisRunId.value = String(data.latest_run_id);
                }
        }

        async function loadLinkAgentStatus() {
            if (!linkAgentPill) return;
            try {
                const res = await fetch('/agents/links/status', { cache: 'no-store' });
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(String(data?.detail || 'status unavailable'));
                }
                const state = String(data?.last_status || 'idle');
                const total = Number(data?.total_cached_links || 0);
                const fdFresh = Boolean(data?.books?.fanduel?.fresh);
                const dkFresh = Boolean(data?.books?.draftkings?.fresh);
                if (state === 'ok') {
                    const freshness = (fdFresh || dkFresh) ? 'warm' : 'cool';
                    linkAgentPill.textContent = `Link agent: ${freshness} (${total} cached)`;
                    return;
                }
                if (state === 'disabled') {
                    linkAgentPill.textContent = 'Link agent: fallback mode (no Odds API key)';
                    return;
                }
                if (state === 'error') {
                    linkAgentPill.textContent = 'Link agent: error, using fallback links';
                    return;
                }
                linkAgentPill.textContent = `Link agent: ${state}`;
            } catch {
                linkAgentPill.textContent = 'Link agent: offline, fallback links active';
            }
        }

            async function runCouldHaveHitAnalysis() {
                const runId = String(analysisRunId?.value || '').trim();
                if (!runId) {
                    analysisResult.textContent = 'Add a run ID first.';
                    return;
                }
                const winners = String(analysisWinners?.value || '')
                    .split(String.fromCharCode(10))
                    .map((line) => line.trim())
                    .filter(Boolean);
                const minLegsHit = Number(analysisMinLegs?.value || '4');
                analysisResult.textContent = 'Checking slips...';
                try {
                    const res = await fetch('/learning/slips/could-have-hit', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            run_id: runId,
                            winning_players: winners,
                            min_legs_hit: Number.isFinite(minLegsHit) ? minLegsHit : 4,
                        }),
                    });
                    const data = await res.json();
                    if (!res.ok) {
                        throw new Error(String(data?.detail || 'Analysis failed.'));
                    }
                    analysisResult.innerHTML = `Would have hit: <strong>${Number(data.would_hit_slips || 0)}</strong> of <strong>${Number(data.total_slips || 0)}</strong> slips. Near misses: <strong>${Number(data.near_miss_slips || 0)}</strong>. Hit rate: <strong>${(Number(data.hit_rate || 0) * 100).toFixed(1)}%</strong>.`;
                } catch (err) {
                    analysisResult.textContent = String(err?.message || 'Analysis failed.');
                }
            }

        function getCachedDashboardState() {
            try {
                const raw = localStorage.getItem(LAST_STATE_KEY);
                if (!raw) return null;
                const parsed = JSON.parse(raw);
                if (!parsed || parsed.status !== 'ok') return null;
                return parsed;
            } catch {
                return null;
            }
        }

        const SPORTSBOOKS = {
            gambly: {
                label: 'Gambly',
                url: 'https://gambly.com/bet-builder?type=straight%7Cplayer_prop&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200&limit=10&sort_by=popularity',
                oneClick: true,
            },
            draftkings: {
                label: 'DraftKings',
                url: 'https://sportsbook.draftkings.com/',
                oneClick: true,
            }
        };

        function setPortalButtonState(btn, enabled, label) {
            if (!btn) return;
            if (label) btn.textContent = label;
            btn.disabled = !enabled;
            btn.style.opacity = enabled ? '1' : '0.55';
            btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
        }

        function oneClickBooks() {
            return Object.values(SPORTSBOOKS)
                .filter((book) => !!book?.oneClick)
                .map((book) => String(book.label || '').trim())
                .filter((x) => x);
        }

        function refreshPortalCapabilities() {
            setPortalButtonState(portalGambly, !!SPORTSBOOKS.gambly?.oneClick, 'Open Gambly');
            setPortalButtonState(portalDraftkings, !!SPORTSBOOKS.draftkings?.oneClick, 'Open DraftKings');

            const hasOneClick = oneClickBooks().length > 0;
            if (strictLinksChk) {
                strictLinksChk.disabled = STRICT_ONE_CLICK_MODE && !hasOneClick;
                if (strictLinksChk.disabled) strictLinksChk.checked = false;
            }
        }

        function sportsbookSlipText(slip, idx) {
            const lines = [];
            lines.push(`#${idx + 1} ${String(slip.archetype || '').trim()}`);
            const mode = String(modeSel?.value || 'balanced').toLowerCase();
            const profile = String(hitsProfileSel?.value || '').toLowerCase();
            const propText = mode === 'hits' || mode === 'hits-tb-combo'
                ? profileLabel(profile)
                : 'To Hit a Home Run';
            (slip.legs || []).forEach((leg) => {
                const rankTag = String(leg.rank_category || '').trim();
                const legPropText = rankTag ? `${propText} [${rankTag.toUpperCase()} Rank]` : propText;
                lines.push(`${String(leg.player_name || '').trim()} (${String(leg.team || '').trim()}) - ${legPropText}`);
            });
            return lines.join('\\n');
        }

        function buildGamblyLinkClient(text) {
            const body = String(text || '').trim();
            const builderBase = 'https://gambly.com/bet-builder?type=straight%7Cplayer_prop&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200&limit=10&sort_by=popularity';
            if (!body) return builderBase;
            const encoded = encodeURIComponent(body);
            return `${builderBase}&q=${encoded}&text=${encoded}`;
        }

        function buildPortalGamblyText(payload) {
            if (payload?.all) {
                const allSlipText = Array.from(cards.querySelectorAll('.card'))
                    .map((_, i) => sportsbookSlipText(window.__lastParlays?.[i] || {}, i))
                    .filter((x) => String(x || '').trim())
                    .join('\\n\\n');
                return `Sportsbook: Gambly\\n${allSlipText}`.trim();
            }
            const idx = Number(payload?.idx || 0);
            const oneSlipText = sportsbookSlipText(payload?.slip || {}, idx);
            return `Sportsbook: Gambly\\n${oneSlipText}`.trim();
        }

        function builderMetricValue(player, categoryKey) {
            const p = player || {};
            if (categoryKey === 'hr') return Number(p.hr_score || 0);
            if (categoryKey === 'hits') return Number(p.hit_score || 0);
            if (categoryKey === 'tb') return Number(p.tb_score || 0);
            if (categoryKey === 'rbi') return Number(p.rbi_score || 0);
            if (categoryKey === 'hrr') return Number(p.hrr_score || 0);
            if (categoryKey === 'value') return Number(p.portfolio_value_score || 0);
            if (categoryKey === 'matchup') return Number(p.pitcher_vuln_score || 0);
            if (categoryKey === 'recent') return Number(p.recent_form_score || p.recent_hits_score || p.recent_hits || 0);
            return Number(p.portfolio_value_score || 0);
        }

        function builderSortScore(player, categoryKey, subKey) {
            const metric = builderMetricValue(player, categoryKey);
            const overall = Number(player?.overall_rank_score || player?.contextual_rank_score || 0);
            const valueRank = Number(player?.value_rank_score || 0);
            const safeRank = Number(player?.safe_rank_score || 0);
            const contrarianRank = Number(player?.contrarian_rank_score || 0);

            if (subKey === 'value') {
                return (valueRank * 0.72) + (metric * 0.28);
            }
            if (subKey === 'safe') {
                return (safeRank * 0.72) + (metric * 0.28);
            }
            if (subKey === 'contrarian') {
                return (contrarianRank * 0.72) + (metric * 0.28);
            }
            return (overall * 0.72) + (metric * 0.28);
        }

        function builderPlayerKey(player) {
            const name = String(player?.player_name || '').toLowerCase();
            return name.replace(/[^a-z0-9]+/g, '');
        }

        function builderStatSummary(player) {
            return `Overall ${Number(player.overall_rank_score || player.contextual_rank_score || 0).toFixed(2)} | Form ${Number(player.recent_form_score || 0).toFixed(2)} | Matchup ${Number(player.matchup_context_score || player.pitcher_vuln_score || 0).toFixed(2)} | Park ${Number(player.park_context_score || player.handed_park_boost || 0).toFixed(2)} | Value ${Number(player.value_rank_score || player.portfolio_value_score || 0).toFixed(2)} | Slot ${Number(player.lineup_slot || 9).toFixed(0)}`;
        }

        function renderBuilderRankings() {
            if (!builderRankings) return;
            const category = String(builderCategorySel?.value || 'hr');
            const sub = String(builderSubcategorySel?.value || 'overall');
            const limit = Number(builderLimitSel?.value || 80);
            const pool = Array.isArray(window.__builderPool) ? window.__builderPool.slice() : [];

            if (!pool.length) {
                builderRankings.innerHTML = '<div class="builder-empty">No ranking data yet. Click Refresh Rankings.</div>';
                return;
            }

            pool.sort((a, b) => builderSortScore(b, category, sub) - builderSortScore(a, category, sub));
            const top = pool.slice(0, Math.max(1, limit));
            const picked = new Set((window.__builderTicket || []).map((p) => builderPlayerKey(p)));

            builderRankings.innerHTML = '';
            top.forEach((player, idx) => {
                const row = document.createElement('div');
                row.className = 'builder-row';
                const score = builderMetricValue(player, category);
                const key = builderPlayerKey(player);

                const info = document.createElement('div');
                info.innerHTML = `<strong>#${idx + 1} ${esc(player.player_name || '')} (${esc(player.team || '')})</strong><span>${esc(builderStatSummary(player))} | Score ${score.toFixed(2)}</span>`;

                const addBtn = document.createElement('button');
                addBtn.className = 'btn-one btn-mini';
                addBtn.textContent = picked.has(key) ? 'Added' : 'Add';
                addBtn.disabled = picked.has(key);
                addBtn.addEventListener('click', () => {
                    const ticket = Array.isArray(window.__builderTicket) ? window.__builderTicket : [];
                    if (ticket.length >= 20) {
                        statusText.textContent = 'Custom ticket max is 20 legs.';
                        return;
                    }
                    if (ticket.some((item) => builderPlayerKey(item) === key)) {
                        return;
                    }
                    window.__builderTicket = [
                        ...ticket,
                        {
                            ...player,
                            rank_category: category,
                            rank_subcategory: sub,
                        }
                    ];
                    renderBuilderTicket();
                    renderBuilderRankings();
                    statusText.textContent = `Added ${String(player.player_name || '')} to custom ticket.`;
                });

                row.appendChild(info);
                row.appendChild(addBtn);
                builderRankings.appendChild(row);
            });
        }

        function renderBuilderTicket() {
            if (!builderTicket) return;
            const ticket = Array.isArray(window.__builderTicket) ? window.__builderTicket : [];
            if (!ticket.length) {
                builderTicket.innerHTML = '<div class="builder-empty">No legs selected yet. Add players from rankings.</div>';
                return;
            }

            builderTicket.innerHTML = '';
            ticket.forEach((player, idx) => {
                const row = document.createElement('div');
                row.className = 'builder-row';

                const info = document.createElement('div');
                info.innerHTML = `<strong>Leg ${idx + 1}: ${esc(player.player_name || '')} (${esc(player.team || '')})</strong><span>${esc(String(player.rank_category || '').toUpperCase())} | ${esc(builderStatSummary(player))}</span>`;

                const removeBtn = document.createElement('button');
                removeBtn.className = 'btn-one btn-mini';
                removeBtn.textContent = 'Remove';
                removeBtn.addEventListener('click', () => {
                    const key = builderPlayerKey(player);
                    window.__builderTicket = ticket.filter((item) => builderPlayerKey(item) !== key);
                    renderBuilderTicket();
                    renderBuilderRankings();
                });

                row.appendChild(info);
                row.appendChild(removeBtn);
                builderTicket.appendChild(row);
            });
        }

        function buildCustomSlip() {
            const ticket = Array.isArray(window.__builderTicket) ? window.__builderTicket : [];
            return {
                archetype: 'Build Your Own Parlay',
                story: `Custom ticket with ${ticket.length} legs.`,
                legs: ticket.map((player) => ({
                    player_name: String(player.player_name || '').trim(),
                    team: String(player.team || '').trim(),
                    park_name: String(player.park_name || '').trim(),
                    game_id: player.game_id ?? null,
                    portfolio_value_score: Number(player.portfolio_value_score || 0),
                    pitcher_vuln_score: Number(player.pitcher_vuln_score || 0),
                    projected_ownership: Number(player.projected_ownership || 0),
                    rank_category: String(player.rank_category || '').trim(),
                })),
            };
        }

        async function loadBuilderPool() {
            if (!builderRankings) return;
            const limit = Number(builderLimitSel?.value || 80);
            const lineupLocked = lineupLockChk?.checked ? 'true' : 'false';
            const allowLive = liveBetsChk?.checked ? 'true' : 'false';
            try {
                const res = await fetch('/dashboard/player-pool?limit=' + encodeURIComponent(String(limit)) + '&lineup_locked_only=' + encodeURIComponent(lineupLocked) + '&allow_live=' + encodeURIComponent(allowLive));
                const data = await res.json();
                if (!res.ok || data.status !== 'ok') {
                    throw new Error(String(data?.detail || 'Failed to load player pool.'));
                }
                window.__builderPool = Array.isArray(data.players) ? data.players : [];
                renderBuilderRankings();
                renderBuilderTicket();
            } catch (err) {
                builderRankings.innerHTML = `<div class="builder-empty">${esc(err?.message || 'Failed to load rankings.')}</div>`;
            }
        }

        function profileLabel(profile) {
            const mapping = {
                'one-plus': '1+ Hits',
                'two-plus': '2+ Hits',
                'three-plus': '3+ Hits',
                'tb-1': '1+ Total Bases',
                'tb-2': '2+ Total Bases',
                'tb-3': '3+ Total Bases',
                'tb-4': '4+ Total Bases',
                'hrr-1': '1+ HRR',
                'hrr-2': '2+ HRR',
                'hrr-3': '3+ HRR',
                'hrr-4': '4+ HRR',
                'rbi-1': '1+ RBI',
                'rbi-2': '2+ RBI',
                'rbi-3': '3+ RBI',
                'rbi-4': '4+ RBI',
                'singles': '1+ Total Bases (Singles)',
                'doubles': '2+ Total Bases (Doubles Lean)',
                'triples': '3+ Total Bases (Triples Lean)',
                'combo': 'Hits + Total Bases Combo',
                'random': 'Random Hits Ladder',
            };
            return mapping[profile] || 'To Record a Hit (1+)';
        }

        function matchupBadge(vuln) {
            const val = Number(vuln || 0);
            if (val >= 0.66) return 'Pitcher Matchup: Soft';
            if (val >= 0.42) return 'Pitcher Matchup: Neutral';
            return 'Pitcher Matchup: Tough';
        }

        function modeLabel(modeValue) {
            const key = String(modeValue || '').toLowerCase();
            const labels = {
                'balanced': 'Core Power Mix (HR)',
                'cream': 'Premium Power Stack (HR)',
                'extreme-cream': 'Ceiling Bombs Only (HR)',
                'chalk-city': 'Chalk Power Press (HR)',
                'random': 'Random (Just For Fun)',
                'hits': 'Hits (+1 +2 +3)',
                'hits-tb-combo': 'Hits + Bases Combo',
            };
            return labels[key] || String(modeValue || '').toUpperCase();
        }

        async function copyText(text, successMsg) {
            try {
                await navigator.clipboard.writeText(text);
                statusText.textContent = successMsg;
            } catch {
                statusText.textContent = 'Copy failed. Your browser blocked clipboard access.';
            }
        }

        function absoluteDashboardUrl(path) {
            const trimmed = String(path || '').trim();
            if (!trimmed) return '';
            try {
                return new URL(trimmed, window.location.origin).toString();
            } catch {
                return trimmed;
            }
        }

        function slipShareUrl(slip) {
            const candidates = [
                slip?.gambly_go_path,
                slip?.share_link_path,
            ];
            for (const candidate of candidates) {
                const trimmed = String(candidate || '').trim();
                if (trimmed && trimmed.includes('/dashboard/go/g/')) {
                    return absoluteDashboardUrl(trimmed);
                }
            }
            return '';
        }

        function appendSlipActionButtons(el, slip, idx) {
            const sportsbookBtn = document.createElement('button');
            sportsbookBtn.className = 'btn-one';
            sportsbookBtn.textContent = 'Sportsbook Portal';
            sportsbookBtn.style.width = '100%';
            sportsbookBtn.style.marginTop = '6px';
            sportsbookBtn.addEventListener('click', () => showPortal({ all: false, slip, idx }));
            el.appendChild(sportsbookBtn);

            const openBtn = document.createElement('button');
            openBtn.className = 'btn-all';
            openBtn.textContent = 'Open in Gambly';
            openBtn.style.width = '100%';
            openBtn.style.marginTop = '6px';
            openBtn.addEventListener('click', () => {
                const shareUrl = slipShareUrl(slip);
                const fallbackLink = String(slip?.gambly_link || '').trim() || buildGamblyLinkClient(sportsbookSlipText(slip || {}, idx));
                const target = shareUrl || fallbackLink;
                if (!target) {
                    statusText.textContent = 'No Gambly link available for this slip.';
                    return;
                }
                window.open(target, '_blank', 'noopener,noreferrer');
                statusText.textContent = `Opened Gambly for slip #${idx + 1}.`;
            });
            el.appendChild(openBtn);

            const copyLinkBtn = document.createElement('button');
            copyLinkBtn.className = 'btn-one';
            copyLinkBtn.textContent = 'Copy link';
            copyLinkBtn.style.width = '100%';
            copyLinkBtn.style.marginTop = '6px';
            copyLinkBtn.addEventListener('click', async () => {
                const shareUrl = slipShareUrl(slip);
                if (!shareUrl) {
                    statusText.textContent = 'No dashboard/go link available for this slip yet.';
                    return;
                }
                await copyText(shareUrl, `Copied slip #${idx + 1} link.`);
            });
            el.appendChild(copyLinkBtn);
        }

        function esc(s) {
            return String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        }

        function openSportsbookTarget(bookKey) {
            const book = SPORTSBOOKS[bookKey];
            if (!book) return;
            window.open(book.url, '_blank', 'noopener,noreferrer');
        }

        function isMobileDevice() {
            const ua = String(navigator.userAgent || '').toLowerCase();
            return /iphone|ipad|ipod|android|mobile/.test(ua) || window.innerWidth <= 760;
        }

        function openLinksReliably(links, fallbackUrl) {
            const safeLinks = Array.isArray(links)
                ? links.filter((x) => typeof x === 'string' && x.trim())
                : [];
            const launcher = window.open('about:blank', '_blank', 'noopener,noreferrer');
            if (launcher) {
                if (safeLinks.length) {
                    launcher.location.href = safeLinks[0];
                } else if (fallbackUrl) {
                    launcher.location.href = fallbackUrl;
                } else {
                    launcher.close();
                }
            }
            return safeLinks.length;
        }

        function renderLinkHub(win, links, bookLabel, fallbackUrl) {
            const safeLinks = Array.isArray(links)
                ? links.filter((x) => typeof x === 'string' && x.trim())
                : [];
            if (!win || win.closed) return false;

            const escaped = (s) => String(s || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
            const listHtml = safeLinks.length
                ? safeLinks.map((link, idx) => `<li style="margin:8px 0;"><a href="${escaped(link)}" target="_self" rel="noopener noreferrer" style="color:#1d4ed8;word-break:break-all;">Open link ${idx + 1}</a><div style="font-size:12px;color:#374151;word-break:break-all;">${escaped(link)}</div></li>`).join('')
                : '<li style="margin:8px 0;color:#6b7280;">No resolved links found.</li>';

            const firstLink = safeLinks[0] || '';
            const safeFallback = String(fallbackUrl || '').trim();

            win.document.open();
            win.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>${escaped(bookLabel)} Link Hub</title></head><body style="font-family:Segoe UI,Arial,sans-serif;padding:16px;background:#f8fafc;color:#111827;"><h2 style="margin:0 0 10px;">${escaped(bookLabel)} Link Hub</h2><p style="margin:0 0 10px;color:#374151;">Click a link below. If direct open fails, use fallback.</p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;"><button id="openFirst" style="padding:8px 10px;border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:8px;cursor:pointer;">Open First Link</button><button id="openFallback" style="padding:8px 10px;border:1px solid #6b7280;background:#fff;color:#111827;border-radius:8px;cursor:pointer;">Open Sportsbook Home</button></div><ol style="padding-left:20px;">${listHtml}</ol><script>const first=${JSON.stringify(firstLink)};const fallback=${JSON.stringify(safeFallback)};document.getElementById('openFirst').onclick=()=>{ if(first){ location.href=first; } };document.getElementById('openFallback').onclick=()=>{ if(fallback){ location.href=fallback; } };<\/script></body></html>`);
            win.document.close();
            return true;
        }

        function navigateWithFallback(targetWindow, url, fallbackUrl) {
            const target = String(url || '').trim() || String(fallbackUrl || '').trim();
            if (!target) return false;
            if (targetWindow && !targetWindow.closed) {
                targetWindow.location.href = target;
                return true;
            }
            // Final guaranteed path if popup is blocked: navigate current tab.
            window.location.href = target;
            return true;
        }

        function syncHitsProfileVisibility() {
            const mode = String(modeSel.value || '').toLowerCase();
            const hitsMode = mode === 'hits' || mode === 'hits-tb-combo';
            hitsProfileSel.style.display = hitsMode ? 'inline-block' : 'none';
            const hitsProfileCustom = customSelectMap.get('hitsProfileSel');
            if (hitsProfileCustom) {
                hitsProfileCustom.style.display = hitsMode ? 'inline-flex' : 'none';
            }
        }

        const customSelectMap = new Map();

        function mountGoldSelect(selectEl) {
            if (!selectEl || customSelectMap.has(selectEl.id)) return;

            const wrap = document.createElement('div');
            wrap.className = 'dk-select';
            wrap.dataset.for = selectEl.id;

            const trigger = document.createElement('button');
            trigger.type = 'button';
            trigger.className = 'dk-select-btn';
            trigger.setAttribute('aria-haspopup', 'listbox');
            trigger.setAttribute('aria-expanded', 'false');

            const list = document.createElement('div');
            list.className = 'dk-select-list';
            list.setAttribute('role', 'listbox');
            document.body.appendChild(list);

            const optionButtons = [];
            Array.from(selectEl.options).forEach((opt) => {
                const item = document.createElement('button');
                item.type = 'button';
                item.className = 'dk-select-option';
                item.dataset.value = String(opt.value);
                item.textContent = String(opt.textContent || opt.value);
                item.setAttribute('role', 'option');
                item.addEventListener('click', () => {
                    selectEl.value = String(opt.value);
                    selectEl.dispatchEvent(new Event('change', { bubbles: true }));
                    syncFromNative();
                    closeList();
                });
                list.appendChild(item);
                optionButtons.push(item);
            });

            function syncFromNative() {
                const selected = selectEl.options[selectEl.selectedIndex] || selectEl.options[0];
                trigger.textContent = selected ? String(selected.textContent || selected.value) : '';
                trigger.title = trigger.textContent;
                optionButtons.forEach((btn) => {
                    btn.classList.toggle('active', btn.dataset.value === String(selectEl.value));
                });
            }

            function positionList() {
                const rect = trigger.getBoundingClientRect();
                list.style.left = `${Math.round(rect.left)}px`;
                list.style.top = `${Math.round(rect.bottom + 6)}px`;
                list.style.width = `${Math.round(Math.max(rect.width, 240))}px`;
            }

            function closeList() {
                wrap.classList.remove('open');
                list.classList.remove('open');
                trigger.setAttribute('aria-expanded', 'false');
            }

            function openList() {
                positionList();
                wrap.classList.add('open');
                list.classList.add('open');
                trigger.setAttribute('aria-expanded', 'true');
                const active = optionButtons.find((btn) => btn.classList.contains('active'));
                if (active) {
                    active.scrollIntoView({ block: 'nearest' });
                }
            }

            trigger.addEventListener('click', () => {
                const open = wrap.classList.contains('open');
                customSelectMap.forEach((node) => {
                    if (typeof node.__close === 'function') node.__close();
                });
                if (!open) {
                    openList();
                } else {
                    closeList();
                }
            });

            trigger.addEventListener('keydown', (event) => {
                const key = String(event.key || '');
                if (key === 'Enter' || key === ' ') {
                    event.preventDefault();
                    trigger.click();
                    return;
                }
                if (key !== 'ArrowDown' && key !== 'ArrowUp') {
                    if (key === 'Escape') closeList();
                    return;
                }
                event.preventDefault();
                const options = Array.from(selectEl.options);
                if (!options.length) return;
                const current = Math.max(0, selectEl.selectedIndex);
                const delta = key === 'ArrowDown' ? 1 : -1;
                const next = (current + delta + options.length) % options.length;
                selectEl.value = String(options[next].value);
                selectEl.dispatchEvent(new Event('change', { bubbles: true }));
                syncFromNative();
            });

            wrap.appendChild(trigger);
            selectEl.classList.add('native-select-hidden');
            selectEl.insertAdjacentElement('afterend', wrap);

            selectEl.addEventListener('change', syncFromNative);
            wrap.__sync = syncFromNative;
            wrap.__close = closeList;
            wrap.__position = positionList;
            syncFromNative();
            customSelectMap.set(selectEl.id, wrap);
        }

        document.addEventListener('click', (event) => {
            if (!(event.target instanceof Element)) return;
            if (!event.target.closest('.dk-select') && !event.target.closest('.dk-select-list')) {
                customSelectMap.forEach((node) => {
                    if (typeof node.__close === 'function') node.__close();
                });
            }
        });

        window.addEventListener('resize', () => {
            customSelectMap.forEach((node) => {
                if (node.classList.contains('open') && typeof node.__position === 'function') {
                    node.__position();
                }
            });
        });
        window.addEventListener('scroll', () => {
            customSelectMap.forEach((node) => {
                if (node.classList.contains('open') && typeof node.__position === 'function') {
                    node.__position();
                }
            });
        }, true);

        function showPortal(payload) {
            portalPayload = payload;
            portalGamblyLink = buildGamblyLinkClient(buildPortalGamblyText(payload));
            const available = oneClickBooks();
            portalText.textContent = available.length
                ? (payload?.all
                    ? `Available one-click books: ${available.join(', ')}. DINGKING will copy all slips, then open that book.`
                    : `Available one-click books: ${available.join(', ')}. DINGKING will copy slip #${(payload?.idx ?? 0) + 1}${payload?.slip?.slip_id ? ` (${payload.slip.slip_id})` : ''}, then open that book.`)
                : 'No one-click sportsbook is enabled right now. Manual-only books are disabled in strict mode.';
            refreshPortalCapabilities();
            portalOverlay.classList.add('show');
        }

        function hidePortal() {
            portalOverlay.classList.remove('show');
            portalPayload = null;
            portalGamblyLink = '';
        }

        async function submitPortal(bookKey) {
            if (!portalPayload) return;
            const book = SPORTSBOOKS[bookKey];
            if (!book) return;
            if (STRICT_ONE_CLICK_MODE && !book.oneClick) {
                statusText.textContent = `${book.label} is manual-only. Strict one-click mode blocked this launch.`;
                hidePortal();
                return;
            }

            // Reserve one user-gesture popup immediately to reduce popup-blocker failures
            // once async resolution completes.
            const preOpenedWindow = window.open('about:blank', '_blank', 'noopener,noreferrer');

            const slips = portalPayload.all
                ? (window.__lastParlays || []).filter((s) => s && Array.isArray(s.legs) && s.legs.length)
                : [portalPayload.slip || {}];

            async function resolveLinks(verifiedOnly) {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 6500);
                const res = await fetch('/dashboard/sportsbook/resolve-links', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    signal: controller.signal,
                    body: JSON.stringify({
                        book: bookKey,
                        slips,
                        mode: String(modeSel?.value || 'balanced'),
                        hits_profile: String(hitsProfileSel?.value || 'one-plus'),
                        verified_only: verifiedOnly,
                        allow_event_fallback: !verifiedOnly,
                    }),
                });
                clearTimeout(timeoutId);
                return res.json();
            }

            let resolved = null;
            try {
                const strictRequested = strictLinksChk?.checked === true;
                resolved = await resolveLinks(strictRequested);

                // If strict matching yields no usable links, auto-retry with non-strict mode
                // so users still get working sportsbook links without extra clicks.
                const strictStatus = String(resolved?.status || '');
                const strictLinks = Array.isArray(resolved?.slip_links?.[0])
                    ? resolved.slip_links[0].filter((x) => typeof x === 'string' && x.trim())
                    : [];
                if (strictRequested && (strictStatus === 'verified_none' || strictLinks.length === 0)) {
                    if (!isMobileDevice()) {
                        const fallbackResolved = await resolveLinks(false);
                        const fallbackLinks = Array.isArray(fallbackResolved?.slip_links?.[0])
                            ? fallbackResolved.slip_links[0].filter((x) => typeof x === 'string' && x.trim())
                            : [];
                        if (fallbackLinks.length) {
                            resolved = fallbackResolved;
                            statusText.textContent = 'No exact player+prop links found. Switched to event links so your opens still work.';
                        }
                    }
                }
            } catch {
                resolved = null;
            }

            const directLinks = Number(resolved?.direct_links || 0);
            const eventLinks = Number(resolved?.event_links || 0);
            const totalLegs = Number(resolved?.total_legs || 0);
            const requestedMarketLinks = Number(resolved?.requested_market_links || 0);
            const backupMarketLinks = Number(resolved?.backup_market_links || 0);
            const eventMarketLinks = Number(resolved?.event_market_links || 0);
            const sourceSuffix = (requestedMarketLinks || backupMarketLinks || eventMarketLinks)
                ? ` | Sources: Primary ${requestedMarketLinks}, Backup ${backupMarketLinks}, Event ${eventMarketLinks}`
                : '';
            let linkKind = 'search links';
            if (String(resolved?.status || '').startsWith('verified_')) {
                linkKind = 'verified player-prop links';
            } else if (directLinks > 0) {
                linkKind = 'deep links';
            } else if (eventLinks > 0) {
                linkKind = 'event links';
            }
            const resolvedGamblyLink = portalPayload.all
                ? String(resolved?.gambly_redirect || resolved?.gambly_link || portalGamblyLink || '').trim()
                : String((Array.isArray(resolved?.gambly_redirects) ? resolved.gambly_redirects[0] : '') || (Array.isArray(resolved?.gambly_links) ? resolved.gambly_links[0] : '') || resolved?.gambly_redirect || resolved?.gambly_link || portalGamblyLink || '').trim();
            if (resolvedGamblyLink) {
                portalGamblyLink = resolvedGamblyLink;
            }

            // On some phone in-app browsers, popup navigation is unreliable.
            // For Gambly, force same-tab navigation for consistent behavior.
            if (bookKey === 'gambly') {
                const fallbackTarget = resolvedGamblyLink || SPORTSBOOKS.gambly.url;
                if (portalPayload.all) {
                    const text = Array.from(cards.querySelectorAll('.card')).map((_, i) => sportsbookSlipText(window.__lastParlays?.[i] || {}, i)).join('\\n\\n');
                    if (!text.trim()) {
                        statusText.textContent = 'Generate slips first.';
                        hidePortal();
                        return;
                    }
                    await copyText(text, 'Copied all slips. Opening Gambly.');
                } else {
                    const text = sportsbookSlipText(portalPayload.slip || {}, portalPayload.idx || 0);
                    await copyText(text, `Copied slip #${(portalPayload.idx || 0) + 1}. Opening Gambly.`);
                }
                window.location.href = fallbackTarget;
                hidePortal();
                return;
            }

            if (portalPayload.all) {
                const text = Array.from(cards.querySelectorAll('.card')).map((_, i) => sportsbookSlipText(window.__lastParlays?.[i] || {}, i)).join('\\n\\n');
                if (!text.trim()) {
                    statusText.textContent = 'Generate slips first.';
                    hidePortal();
                    return;
                }
                await copyText(text, `Copied all slips. Opening ${book.label}.`);
                const links = Array.isArray(resolved?.links) ? resolved.links.filter((x) => typeof x === 'string' && x.trim()) : [];
                const effectiveLinks = bookKey === 'gambly' ? [] : links;
                const fallbackTarget = resolvedGamblyLink || SPORTSBOOKS.gambly.url;
                if (effectiveLinks.length) {
                    if (isMobileDevice()) {
                        navigateWithFallback(preOpenedWindow, effectiveLinks[0], fallbackTarget);
                        statusText.textContent = `Copied all slips. Opened ${book.label} first direct link for mobile bet entry.`;
                    } else {
                        const hubOpened = renderLinkHub(preOpenedWindow, effectiveLinks, book.label, fallbackTarget);
                        if (!hubOpened) {
                            navigateWithFallback(preOpenedWindow, effectiveLinks[0], fallbackTarget);
                        }
                        statusText.textContent = `Copied all slips. Opened ${book.label} link hub with ${effectiveLinks.length} ${linkKind}.${sourceSuffix}`;
                    }
                } else {
                    navigateWithFallback(preOpenedWindow, '', fallbackTarget);
                    const reason = String(resolved?.fallback_reason || 'unavailable');
                    statusText.textContent = `No direct links found for all slips (${reason}). Redirected to Gambly share link.`;
                }
                hidePortal();
                return;
            }

            const text = sportsbookSlipText(portalPayload.slip || {}, portalPayload.idx || 0);
            await copyText(text, `Copied slip #${(portalPayload.idx || 0) + 1}. Opening ${book.label}.`);
            const legLinks = Array.isArray(resolved?.slip_links?.[0])
                ? resolved.slip_links[0].filter((x) => typeof x === 'string' && x.trim())
                : [];
            const effectiveLegLinks = bookKey === 'gambly' ? [] : legLinks;
            const fallbackTarget = resolvedGamblyLink || SPORTSBOOKS.gambly.url;
            if (effectiveLegLinks.length) {
                if (isMobileDevice()) {
                    navigateWithFallback(preOpenedWindow, effectiveLegLinks[0], fallbackTarget);
                    const verifySuffix = strictLinksChk?.checked ? ` (${Math.min(effectiveLegLinks.length, totalLegs)}/${totalLegs || effectiveLegLinks.length} verified)` : '';
                    statusText.textContent = `Copied slip #${(portalPayload.idx || 0) + 1}. Opened ${book.label} direct prop link for mobile${verifySuffix}.`;
                } else {
                    const hubOpened = renderLinkHub(preOpenedWindow, effectiveLegLinks, book.label, fallbackTarget);
                    if (!hubOpened) {
                        navigateWithFallback(preOpenedWindow, effectiveLegLinks[0], fallbackTarget);
                    }
                    const verifySuffix = strictLinksChk?.checked ? ` (${Math.min(effectiveLegLinks.length, totalLegs)}/${totalLegs || effectiveLegLinks.length} verified)` : '';
                    statusText.textContent = `Copied slip #${(portalPayload.idx || 0) + 1}. Opened ${book.label} link hub with ${effectiveLegLinks.length} ${linkKind}${verifySuffix} to build parlay.${sourceSuffix}`;
                }
            } else {
                navigateWithFallback(preOpenedWindow, '', fallbackTarget);
                const reason = String(resolved?.fallback_reason || 'unavailable');
                statusText.textContent = `No direct links found for slip #${(portalPayload.idx || 0) + 1} (${reason}). Redirected to Gambly share link.`;
            }
            hidePortal();
        }

        function renderParlays(parlays) {
            window.__lastParlays = Array.isArray(parlays) ? parlays : [];
            cards.innerHTML = '';
            parlays.forEach((slip, idx) => {
                const legsHtml = (slip.legs || []).map((leg, li) => {
                    const bestValue = Number(leg.portfolio_value_score || 0) >= 0.50 && Number(leg.projected_ownership || 0) <= 0.12;
                    const valueTag = bestValue ? ' | Best Value' : '';
                    const matchup = matchupBadge(leg.pitcher_vuln_score);
                    return `<div class=\"leg\"><strong>Leg ${li + 1}:</strong> ${esc(leg.player_name)} | ${esc(leg.team)} | ${esc(leg.park_name)}<br/><span>${esc(matchup)}${esc(valueTag)}</span></div>`;
                }).join('');
                const el = document.createElement('article');
                el.className = 'card';
                el.dataset.index = String(idx);
                el.innerHTML = `
                    <h3>#${idx + 1} ${esc(slip.archetype || '')}</h3>
                    <div class="story">Slip ID: ${esc(slip.slip_id || '')}</div>
                    <div class=\"story\">${esc(slip.story || '')}</div>
                    ${legsHtml}
                `;
                appendSlipActionButtons(el, slip, idx);
                cards.appendChild(el);
            });
        }

        async function loadState() {
            let data = null;
            try {
                const res = await fetch('/dashboard/state');
                data = await res.json();
            } catch {
                data = getCachedDashboardState();
                if (!data) {
                    statusText.textContent = 'Offline and no cached board yet. Connect once to load your board.';
                    metaText.textContent = 'Offline mode';
                    cards.innerHTML = '';
                    return;
                }
                statusText.textContent = 'Offline mode: showing your last saved board.';
            }

            if (data.status === 'empty') {
                statusText.textContent = 'No active dashboard batch. Click "Generate All 20".';
                metaText.textContent = 'Source: ' + data.source_csv;
                syncSlateRefreshPill(data);
                cards.innerHTML = '';
                return;
            }
            cacheDashboardState(data);
            syncSlateRefreshPill(data);
            if (analysisRunId && !String(analysisRunId.value || '').trim() && data.latest_run_id) {
                analysisRunId.value = String(data.latest_run_id);
            }
            if (data.mode) {
                modeSel.value = data.mode;
            }
            if (data.hits_profile) {
                hitsProfileSel.value = String(data.hits_profile);
            }
            if (data.risk_level) {
                riskSel.value = String(data.risk_level);
            }
            if (typeof data.lineup_locked_only === 'boolean') {
                lineupLockChk.checked = Boolean(data.lineup_locked_only);
            }
            if (typeof data.allow_live === 'boolean') {
                liveBetsChk.checked = Boolean(data.allow_live);
            }
            if (data.legs_per_slip) {
                legsSel.value = String(data.legs_per_slip);
            }
            const modeCustom = customSelectMap.get('modeSel');
            const hitsCustom = customSelectMap.get('hitsProfileSel');
            if (modeCustom && modeCustom.__sync) modeCustom.__sync();
            if (hitsCustom && hitsCustom.__sync) hitsCustom.__sync();
            syncHitsProfileVisibility();
            statusText.textContent = `Ready: ${data.count} parlays (${data.legs_per_slip} legs each)`;
            const modeKey = String(modeSel.value).toLowerCase();
            const hitsMeta = (modeKey === 'hits' || modeKey === 'hits-tb-combo') ? ` | Profile ${String(hitsProfileSel.value || '').toUpperCase()}` : '';
            metaText.textContent = `Mode ${modeLabel(modeSel.value)}${hitsMeta} | Risk ${String(riskSel.value).toUpperCase()} | LineupLock ${lineupLockChk.checked ? 'ON' : 'OFF'} | Live ${liveBetsChk.checked ? 'ON' : 'OFF'} | Run ${data.latest_run_id} | Generated ${data.generated_at}`;
            renderParlays(data.parlays);
        }

        async function generateAll() {
            statusText.textContent = 'Generating all 20...';
            const mode = modeSel.value || 'balanced';
            const legs = Number(legsSel.value || '4');
            const hitsProfile = hitsProfileSel.value || 'high-frequency';
            const riskLevel = riskSel.value || 'balanced';
            const lineupLockedOnly = lineupLockChk.checked ? 'true' : 'false';
            const allowLive = liveBetsChk.checked ? 'true' : 'false';
            const res = await fetch('/dashboard/generate-all?mode=' + encodeURIComponent(mode) + '&legs_per_slip=' + encodeURIComponent(String(legs)) + '&hits_profile=' + encodeURIComponent(hitsProfile) + '&risk_level=' + encodeURIComponent(riskLevel) + '&lineup_locked_only=' + encodeURIComponent(lineupLockedOnly) + '&allow_live=' + encodeURIComponent(allowLive), { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                statusText.textContent = data.detail || 'Failed to generate all 20.';
                return;
            }
            cacheDashboardState(data);
            syncSlateRefreshPill(data);
            statusText.textContent = `Generated ${data.count} parlays (${data.legs_per_slip} legs each).`;
            const modeKey = String(data.mode || mode).toLowerCase();
            const hitsMeta = (modeKey === 'hits' || modeKey === 'hits-tb-combo') ? ` | Profile ${String(data.hits_profile || hitsProfile).toUpperCase()}` : '';
            metaText.textContent = `Mode ${modeLabel(data.mode || mode)}${hitsMeta} | Risk ${String(data.risk_level || riskLevel).toUpperCase()} | LineupLock ${Boolean(data.lineup_locked_only) ? 'ON' : 'OFF'} | Live ${Boolean(data.allow_live) ? 'ON' : 'OFF'} | Run ${data.latest_run_id} | Generated ${data.generated_at}`;
            renderParlays(data.parlays);
        }

        async function generateOne() {
            statusText.textContent = 'Generating one new parlay...';
            const mode = modeSel.value || 'balanced';
            const legs = Number(legsSel.value || '4');
            const hitsProfile = hitsProfileSel.value || 'high-frequency';
            const riskLevel = riskSel.value || 'balanced';
            const lineupLockedOnly = lineupLockChk.checked ? 'true' : 'false';
            const allowLive = liveBetsChk.checked ? 'true' : 'false';
            const res = await fetch('/dashboard/generate-one?mode=' + encodeURIComponent(mode) + '&legs_per_slip=' + encodeURIComponent(String(legs)) + '&hits_profile=' + encodeURIComponent(hitsProfile) + '&risk_level=' + encodeURIComponent(riskLevel) + '&lineup_locked_only=' + encodeURIComponent(lineupLockedOnly) + '&allow_live=' + encodeURIComponent(allowLive), { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                statusText.textContent = data.detail || 'Failed to generate one parlay.';
                return;
            }
            cacheDashboardState({
                status: 'ok',
                count: Number(data.count || 0),
                legs_per_slip: Number(data.legs_per_slip || 0),
                mode: data.mode,
                hits_profile: data.hits_profile,
                risk_level: data.risk_level,
                lineup_locked_only: Boolean(data.lineup_locked_only),
                allow_live: Boolean(data.allow_live),
                latest_run_id: data.latest_run_id,
                generated_at: data.generated_at,
                parlays: Array.isArray(window.__lastParlays) ? window.__lastParlays : [],
            });
            syncSlateRefreshPill({ generated_at: data.generated_at, source_csv_mtime: window.__lastState?.source_csv_mtime });
            statusText.textContent = `Replaced slot #${data.replaced_slot + 1}.`;
            const modeKey = String(data.mode || mode).toLowerCase();
            const hitsMeta = (modeKey === 'hits' || modeKey === 'hits-tb-combo') ? ` | Profile ${String(data.hits_profile || hitsProfile).toUpperCase()}` : '';
            metaText.textContent = `Mode ${modeLabel(data.mode || mode)}${hitsMeta} | Risk ${String(data.risk_level || riskLevel).toUpperCase()} | LineupLock ${Boolean(data.lineup_locked_only) ? 'ON' : 'OFF'} | Live ${Boolean(data.allow_live) ? 'ON' : 'OFF'} | Run ${data.latest_run_id} | Generated ${data.generated_at}`;

            const card = cards.querySelector(`[data-index=\"${data.replaced_slot}\"]`);
            const slip = data.parlay;
            const legsHtml = (slip.legs || []).map((leg, li) => {
                const bestValue = Number(leg.portfolio_value_score || 0) >= 0.50 && Number(leg.projected_ownership || 0) <= 0.12;
                const valueTag = bestValue ? ' | Best Value' : '';
                const matchup = matchupBadge(leg.pitcher_vuln_score);
                return `<div class=\"leg\"><strong>Leg ${li + 1}:</strong> ${esc(leg.player_name)} | ${esc(leg.team)} | ${esc(leg.park_name)}<br/><span>${esc(matchup)}${esc(valueTag)}</span></div>`;
            }).join('');
            const html = `
                <h3>#${data.replaced_slot + 1} ${esc(slip.archetype || '')}</h3>
                <div class="story">Slip ID: ${esc(slip.slip_id || '')}</div>
                <div class=\"story\">${esc(slip.story || '')}</div>
                ${legsHtml}
            `;
            if (card) {
                card.innerHTML = html;
                window.__lastParlays[data.replaced_slot] = slip;
                cacheDashboardState({
                    status: 'ok',
                    count: Number(data.count || window.__lastParlays.length || 0),
                    legs_per_slip: Number(data.legs_per_slip || legs),
                    mode: data.mode || mode,
                    hits_profile: data.hits_profile || hitsProfile,
                    risk_level: data.risk_level || riskLevel,
                    lineup_locked_only: Boolean(data.lineup_locked_only),
                    allow_live: Boolean(data.allow_live),
                    latest_run_id: data.latest_run_id,
                    generated_at: data.generated_at,
                    parlays: window.__lastParlays,
                });
                syncSlateRefreshPill({ generated_at: data.generated_at, source_csv_mtime: window.__lastState?.source_csv_mtime });
                appendSlipActionButtons(card, slip, data.replaced_slot);
            } else {
                loadState();
            }
        }

        async function refreshLateSwap() {
            statusText.textContent = 'Refreshing late-window slips...';
            const res = await fetch('/dashboard/refresh-late-swap', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) {
                statusText.textContent = data.detail || 'Late swap refresh failed.';
                return;
            }
            statusText.textContent = `Late swap refreshed ${Number(data.replaced_count || 0)} slips.`;
            await loadState();
        }

        document.getElementById('btnAll').addEventListener('click', generateAll);
        document.getElementById('btnOne').addEventListener('click', generateOne);
        lateSwapBtn.addEventListener('click', refreshLateSwap);
        modeSel.addEventListener('change', syncHitsProfileVisibility);
        lineupLockChk.addEventListener('change', () => {
            loadBuilderPool();
        });
        liveBetsChk.addEventListener('change', () => {
            loadBuilderPool();
        });
        copyAllBtn.addEventListener('click', () => showPortal({ all: true }));
        portalClose.addEventListener('click', hidePortal);
        portalOverlay.addEventListener('click', (event) => {
            if (event.target === portalOverlay) hidePortal();
        });
        if (portalGambly) {
            portalGambly.addEventListener('click', () => submitPortal('gambly'));
        }
        if (portalDraftkings) {
            portalDraftkings.addEventListener('click', () => submitPortal('draftkings'));
        }
        refreshPortalCapabilities();
        builderCategorySel.addEventListener('change', renderBuilderRankings);
        builderSubcategorySel.addEventListener('change', renderBuilderRankings);
        builderLimitSel.addEventListener('change', loadBuilderPool);
        builderRefreshBtn.addEventListener('click', loadBuilderPool);
        builderClearBtn.addEventListener('click', () => {
            window.__builderTicket = [];
            renderBuilderTicket();
            renderBuilderRankings();
            statusText.textContent = 'Cleared custom ticket.';
        });
        builderPortalBtn.addEventListener('click', () => {
            const ticket = Array.isArray(window.__builderTicket) ? window.__builderTicket : [];
            if (!ticket.length) {
                statusText.textContent = 'Add at least one player to your custom ticket first.';
                return;
            }
            showPortal({ all: false, slip: buildCustomSlip(), idx: 0 });
        });
        analysisRunBtn.addEventListener('click', runCouldHaveHitAnalysis);

                window.addEventListener('beforeinstallprompt', (event) => {
                        event.preventDefault();
                        deferredInstallPrompt = event;
                        installBtn.style.display = 'inline-block';
                });

                installBtn.addEventListener('click', async () => {
                        if (!deferredInstallPrompt) {
                                statusText.textContent = 'Open browser menu and choose Add to Home Screen.';
                                return;
                        }
                        deferredInstallPrompt.prompt();
                        await deferredInstallPrompt.userChoice;
                        deferredInstallPrompt = null;
                        installBtn.style.display = 'none';
                });

                if ('serviceWorker' in navigator) {
                        window.addEventListener('load', () => {
                                navigator.serviceWorker.register('/dashboard/sw.js', { scope: '/dashboard/' }).catch(() => {});
                        });
                }

            mountGoldSelect(modeSel);
            mountGoldSelect(hitsProfileSel);
            mountGoldSelect(builderCategorySel);
            mountGoldSelect(builderSubcategorySel);
            mountGoldSelect(builderLimitSel);
            const modeCustom = customSelectMap.get('modeSel');
            const hitsCustom = customSelectMap.get('hitsProfileSel');
            const builderCategoryCustom = customSelectMap.get('builderCategorySel');
            const builderSubcategoryCustom = customSelectMap.get('builderSubcategorySel');
            const builderLimitCustom = customSelectMap.get('builderLimitSel');
            if (modeCustom && modeCustom.__sync) modeCustom.__sync();
            if (hitsCustom && hitsCustom.__sync) hitsCustom.__sync();
            if (builderCategoryCustom && builderCategoryCustom.__sync) builderCategoryCustom.__sync();
            if (builderSubcategoryCustom && builderSubcategoryCustom.__sync) builderSubcategoryCustom.__sync();
            if (builderLimitCustom && builderLimitCustom.__sync) builderLimitCustom.__sync();

        loadState();
        loadBuilderPool();
        loadLinkAgentStatus();

        // Keep dashboard state fresh so slips appear automatically as slate data updates.
        setInterval(() => {
            loadState();
            loadBuilderPool();
            loadLinkAgentStatus();
        }, 45000);
    </script>
</body>
</html>
"""


PWA_MANIFEST = {
    "name": "DINGKING",
    "short_name": "DINGKING",
        "description": "MLB HR parlay dashboard",
        "start_url": "/dashboard",
        "scope": "/dashboard/",
        "display": "standalone",
        "background_color": "#f7f4ef",
        "theme_color": "#b3472e",
        "icons": [
                {
                        "src": "/dashboard/icon.svg?v=gold-k-crown-2",
                        "sizes": "any",
                        "type": "image/svg+xml",
                        "purpose": "any"
                }
        ]
}


PWA_SW_JS = """
const CACHE_NAME = 'weather-warfare-v2-gold-k-crown';
const APP_SHELL = ['/dashboard', '/dashboard/app-manifest.json?v=gold-k-crown-2', '/dashboard/icon.svg?v=gold-k-crown-2'];

self.addEventListener('install', (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;

    if (url.pathname.startsWith('/dashboard')) {
        event.respondWith(
            fetch(event.request)
                .then((response) => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
                    return response;
                })
                .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/dashboard')))
        );
    }
});
"""


PWA_ICON_SVG = """
<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\" role=\"img\" aria-label=\"Gold crowned K\">
    <defs>
        <linearGradient id=\"bg\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">
            <stop offset=\"0%\" stop-color=\"#0f0b06\"/>
            <stop offset=\"100%\" stop-color=\"#2a1b08\"/>
        </linearGradient>
        <linearGradient id=\"gold\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">
            <stop offset=\"0%\" stop-color=\"#fff4bf\"/>
            <stop offset=\"40%\" stop-color=\"#ffd968\"/>
            <stop offset=\"100%\" stop-color=\"#c7901f\"/>
        </linearGradient>
    </defs>
    <rect width=\"512\" height=\"512\" rx=\"100\" fill=\"url(#bg)\"/>
    <rect x=\"18\" y=\"18\" width=\"476\" height=\"476\" rx=\"84\" fill=\"none\" stroke=\"#a97814\" stroke-width=\"8\"/>
    <g transform=\"translate(256 118)\">
        <path d=\"M-112 64l44-62 44 50 24-40 24 40 44-50 44 62v26h-224z\" fill=\"url(#gold)\" stroke=\"#7a540e\" stroke-width=\"8\" stroke-linejoin=\"round\"/>
        <circle cx=\"-68\" cy=\"52\" r=\"8\" fill=\"#ffef9c\"/>
        <circle cx=\"0\" cy=\"34\" r=\"8\" fill=\"#ffef9c\"/>
        <circle cx=\"68\" cy=\"52\" r=\"8\" fill=\"#ffef9c\"/>
    </g>
    <text x=\"256\" y=\"374\" text-anchor=\"middle\" font-size=\"292\" font-weight=\"900\" font-family=\"Georgia, 'Times New Roman', serif\" fill=\"url(#gold)\" stroke=\"#5f3f08\" stroke-width=\"10\" paint-order=\"stroke\">K</text>
</svg>
"""


@app.on_event("startup")
def _startup() -> None:
    init_db()
    auto_start = os.getenv("WW_AGENT_AUTO_START", "1").strip() == "1"
    if auto_start:
        agents.start(every_seconds=300)
    links_auto_start = os.getenv("WW_LINK_AGENT_AUTO_START", "1").strip() == "1"
    if links_auto_start:
        link_agents.start(every_seconds=300)


@app.on_event("shutdown")
def _shutdown() -> None:
    agents.stop()
    link_agents.stop()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "DINGKING"}


@app.post("/portfolio/generate")
def generate_portfolio(request: GeneratePortfolioRequest):
    try:
        df = load_slate_from_records(request.players)
        result = generate_portfolio_board(df, request.config)
        save_run(result["run_id"], result, request.run_label)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/portfolio/generate-from-csv")
def generate_portfolio_from_csv(request: GeneratePortfolioRequestFile):
    try:
        csv_path = Path(request.csv_path)
        if not csv_path.exists():
            raise ValueError(f"CSV path does not exist: {csv_path}")
        df = load_slate_from_csv(str(csv_path))
        result = generate_portfolio_board(df, request.config)
        save_run(result["run_id"], result, run_label=csv_path.name)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/portfolio/{run_id}")
def get_portfolio(run_id: str):
    payload = fetch_run(run_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Run not found")
    return payload


@app.get("/portfolio/slip/{slip_id}")
def get_portfolio_slip(slip_id: str):
    payload = fetch_slip_by_id(slip_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Slip not found")
    return payload


@app.get("/learning/archetypes")
def get_archetype_performance():
    return {"archetype_performance": archetype_performance_snapshot()}


@app.get("/learning/archetype-weights")
def get_archetype_weights(lookback_days: int = 45, min_samples: int = 8):
    return adaptive_archetype_weights(lookback_days=lookback_days, min_samples=min_samples)


@app.post("/learning/archetypes/{run_id}/{archetype}")
def post_archetype_result(run_id: str, archetype: str, win_flag: bool, payout_multiple: float | None = None):
    record_archetype_outcome(run_id, archetype, win_flag, payout_multiple)
    return {"status": "recorded"}


@app.post("/learning/decision-outcomes")
def post_decision_outcome(payload: DecisionOutcomeRequest):
    try:
        result = record_decision_outcome(
            run_id=payload.run_id,
            playbook_name=payload.playbook_name,
            category=payload.category,
            subcategory=payload.subcategory,
            market_type=payload.market_type,
            book=payload.book,
            confidence=payload.confidence,
            stake_units=payload.stake_units,
            odds_price=payload.odds_price,
            win_flag=payload.win_flag,
            payout_multiple=payload.payout_multiple,
            reward_score=payload.reward_score,
            metadata=payload.metadata,
        )
        return {"status": "recorded", "result": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/learning/categories")
def get_category_performance(lookback_days: int = 60, min_samples: int = 5):
    return {
        "items": category_performance_snapshot(lookback_days=lookback_days, min_samples=min_samples),
        "meta": {"lookback_days": lookback_days, "min_samples": min_samples},
    }


@app.get("/learning/playbooks")
def get_playbook_performance(lookback_days: int = 60, min_samples: int = 5):
    return {
        "items": playbook_performance_snapshot(lookback_days=lookback_days, min_samples=min_samples),
        "meta": {"lookback_days": lookback_days, "min_samples": min_samples},
    }


@app.get("/learning/playbook/recommend")
def get_playbook_recommendation(
    mode: str = "balanced",
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    book: str = "fanduel",
    lookback_days: int = 60,
):
    return recommend_playbook(
        mode=mode,
        hits_profile=hits_profile,
        risk_level=risk_level,
        book=book,
        lookback_days=lookback_days,
    )


@app.post("/signals/twitter")
def post_twitter_signal(payload: TwitterSignalRequest):
    try:
        result = record_twitter_signal(
            source_account=payload.source_account,
            signal_text=payload.signal_text,
            signal_type=payload.signal_type,
            player_name=payload.player_name,
            team=payload.team,
            confidence=payload.confidence,
            occurred_at=payload.occurred_at,
            metadata=payload.metadata,
        )
        return {
            "status": "recorded",
            "result": result,
            "note": "Twitter signals are context-only and should be confirmation features, not direct model labels.",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/signals/twitter")
def get_twitter_signals(hours: int = 24, limit: int = 100):
    return twitter_signal_snapshot(hours=hours, limit=limit)


@app.post("/learning/slips/could-have-hit")
def slips_could_have_hit(payload: SlipHitEstimateRequest):
    run = fetch_run(payload.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    parlays = run.get("parlays", []) if isinstance(run, dict) else []
    if not isinstance(parlays, list):
        parlays = []

    winner_keys = {_norm_player_name(name) for name in payload.winning_players if str(name).strip()}
    winner_keys.discard("")

    evaluated: list[dict] = []
    hit_count = 0
    near_miss_count = 0

    for idx, slip in enumerate(parlays):
        if not isinstance(slip, dict):
            continue
        legs = slip.get("legs", [])
        if not isinstance(legs, list):
            continue

        leg_players: list[str] = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            player_name = str(leg.get("player_name", "")).strip()
            if player_name:
                leg_players.append(player_name)

        leg_keys = [_norm_player_name(name) for name in leg_players if _norm_player_name(name)]
        total_legs = len(leg_keys)
        if total_legs <= 0:
            continue

        matched = [name for name, key in zip(leg_players, leg_keys) if key in winner_keys]
        matched_count = len(matched)
        threshold = payload.min_legs_hit if payload.min_legs_hit is not None else total_legs
        threshold = max(1, min(int(threshold), total_legs))

        would_hit = matched_count >= threshold
        near_miss = (not would_hit) and (matched_count == max(0, threshold - 1))
        if would_hit:
            hit_count += 1
        if near_miss:
            near_miss_count += 1

        evaluated.append(
            {
                "index": idx,
                "archetype": str(slip.get("archetype", "")),
                "legs": total_legs,
                "matched_legs": matched_count,
                "threshold": threshold,
                "would_hit": would_hit,
                "near_miss": near_miss,
                "matched_players": matched,
            }
        )

    evaluated.sort(key=lambda item: (item["would_hit"], item["matched_legs"], -item["legs"]), reverse=True)
    total = len(evaluated)
    return {
        "run_id": payload.run_id,
        "total_slips": total,
        "winning_players_count": len(winner_keys),
        "would_hit_slips": hit_count,
        "near_miss_slips": near_miss_count,
        "hit_rate": round((hit_count / total), 4) if total else 0.0,
        "note": "This is a what-if estimate by matching slip players against supplied winners.",
        "slips": evaluated,
    }


@app.post("/playbook/twitter-screenshot/generate")
def generate_twitter_screenshot_playbook(payload: TwitterScreenshotPlaybookRequest):
    mode_key = _normalize_dashboard_mode(payload.mode)
    risk_key = _normalize_risk_level(payload.risk_level)
    hits_profile_key = _normalize_hits_profile(payload.hits_profile)

    board = _generate_dashboard_board(
        num_slips=int(payload.num_slips),
        mode=mode_key,
        legs_per_slip=int(payload.legs_per_slip),
        hits_profile=hits_profile_key,
        risk_level=risk_key,
        lineup_locked_only=bool(payload.lineup_locked_only),
        allow_live=bool(payload.allow_live),
    )

    player_pool = _build_dashboard_player_pool(
        lineup_locked_only=bool(payload.lineup_locked_only),
        allow_live=bool(payload.allow_live),
    )
    matched_players = _extract_players_from_screenshot_text(
        screenshot_text=payload.screenshot_text,
        player_pool=player_pool,
        max_players=int(payload.max_pinned_players),
    )

    parlays = board.get("parlays", []) if isinstance(board, dict) else []
    pinned_count = _pin_playbook_players(parlays, matched_players)
    save_run(board["run_id"], board, run_label="twitter_screenshot_playbook")

    recommendation = recommend_playbook(
        mode=mode_key,
        hits_profile=hits_profile_key,
        risk_level=risk_key,
        book="fanduel",
        lookback_days=60,
    )

    return {
        "status": "ok",
        "playbook": "twitter_screenshot",
        "run_id": board.get("run_id"),
        "num_slips": int(payload.num_slips),
        "legs_per_slip": int(payload.legs_per_slip),
        "mode": mode_key,
        "hits_profile": hits_profile_key,
        "risk_level": risk_key,
        "matched_players": [
            {
                "player_name": str(p.get("player_name") or ""),
                "team": str(p.get("team") or ""),
                "portfolio_value_score": float(p.get("portfolio_value_score") or 0.0),
            }
            for p in matched_players
        ],
        "matched_count": len(matched_players),
        "pinned_count": pinned_count,
        "recommendation": recommendation,
        "parlays": parlays,
        "note": "Matched names from screenshot text were pinned into generated slips where possible.",
    }


@app.post("/dashboard/betslip/from-text")
def dashboard_betslip_from_text(payload: dict):
    raw_text = _normalize_betslip_text(str(payload.get("text") or ""))
    if not raw_text:
        raise HTTPException(status_code=400, detail="text is required")

    target_sportsbook = _normalize_target_sportsbook(str(payload.get("target_sportsbook") or payload.get("sportsbook") or "gambly"))
    gambly_link = _build_gambly_slip_link(slip_text=raw_text, target_sportsbook=target_sportsbook)
    return {
        "status": "ok",
        "gambly_enabled": GAMBLY_ENABLED,
        "target_sportsbook": target_sportsbook,
        "betslip_text": raw_text,
        "gambly_link": gambly_link,
        "share_targets": ["discord", "x"],
        "note": "Discord/X posting endpoints can be added later; this returns a stable share link now.",
    }


@app.post("/dashboard/betslip/from-screenshot")
def dashboard_betslip_from_screenshot(payload: dict):
    raw_text = _normalize_betslip_text(str(payload.get("screenshot_text") or payload.get("text") or ""))
    if not raw_text:
        raise HTTPException(status_code=400, detail="screenshot_text is required")

    target_sportsbook = _normalize_target_sportsbook(str(payload.get("target_sportsbook") or payload.get("sportsbook") or "gambly"))
    gambly_link = _build_gambly_slip_link(slip_text=raw_text, target_sportsbook=target_sportsbook)
    return {
        "status": "ok",
        "gambly_enabled": GAMBLY_ENABLED,
        "target_sportsbook": target_sportsbook,
        "betslip_text": raw_text,
        "gambly_link": gambly_link,
        "source": "screenshot_text",
        "share_targets": ["discord", "x"],
        "note": "OCR parsing can be layered later; this endpoint expects extracted screenshot text.",
    }


@app.get("/dashboard/go/g/{token}", response_class=HTMLResponse)
def dashboard_go_gambly(token: str, request: Request):
        target = _resolve_cached_gambly_redirect(token)
        if not target:
                return HTMLResponse(
                        content=(
                                "<!doctype html><html><head><meta charset='utf-8'/>"
                                "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
                                "<title>DINGKING Link Expired</title></head>"
                                "<body style='font-family:Segoe UI,Arial,sans-serif;background:#120900;color:#ffecc0;padding:18px;'>"
                                "<h2 style='margin:0 0 10px;'>This Gambly link expired.</h2>"
                                "<p style='margin:0 0 14px;'>Return to DINGKING and generate a fresh sportsbook link.</p>"
                                "<a href='/dashboard' style='display:inline-block;padding:10px 12px;border-radius:10px;background:#f6bf4f;color:#2a1800;text-decoration:none;font-weight:700;'>Back to Dashboard</a>"
                                "</body></html>"
                        ),
                        status_code=410,
                )

        if str(request.query_params.get("direct") or "").strip() == "1":
                return RedirectResponse(url=target, status_code=307)

        slip_text = _extract_gambly_text_from_link(target)
        target_sportsbook = _extract_target_sportsbook_from_text(slip_text)
        target_sportsbook_url = BOOK_HOME_URLS.get(target_sportsbook, BOOK_HOME_URLS["gambly"])
        target_sportsbook_label = target_sportsbook.replace("_", " ").title()
        slip_lines = [line.strip() for line in str(slip_text or "").replace("\r\n", "\n").split("\n") if line.strip()]
        leg_lines: list[str] = []
        for line in slip_lines:
            lower = line.lower()
            if lower.startswith("sportsbook:"):
                continue
            if line.startswith("#"):
                continue
            leg_lines.append(line)
        leg_items_html = "".join(
            [
                (
                    "<li class='leg-item'>"
                    f"<label><input type='checkbox' class='leg-check' data-leg-index='{idx}' data-leg='{html.escape(leg)}'/>"
                    f" <span>{html.escape(leg)}</span></label>"
                    f"<button type='button' class='leg-copy secondary' data-leg='{html.escape(leg)}'>Copy Leg</button>"
                    "</li>"
                )
                for idx, leg in enumerate(leg_lines)
            ]
        )
        slip_display = html.escape(slip_text) if slip_text else "No slip text detected for this link."
        safe_target = html.escape(target)
        safe_target_sportsbook_url = html.escape(target_sportsbook_url)
        safe_target_sportsbook_label = html.escape(target_sportsbook_label)
        html_doc = f"""<!doctype html>
<html lang='en'>
<head>
    <meta charset='utf-8'/>
    <meta name='viewport' content='width=device-width, initial-scale=1'/>
    <title>DINGKING -> Gambly</title>
    <style>
        body {{
            margin: 0;
            font-family: Segoe UI, Arial, sans-serif;
            background: linear-gradient(150deg, #1a0e00, #3a2100);
            color: #ffe8bb;
            min-height: 100vh;
            padding: 14px;
        }}
        .card {{
            border: 1px solid rgba(255, 210, 101, 0.56);
            border-radius: 14px;
            padding: 12px;
            background: rgba(33, 19, 2, 0.9);
            box-shadow: 0 14px 30px rgba(0, 0, 0, 0.35);
        }}
        h1 {{ margin: 0 0 10px; font-size: 20px; }}
        p {{ margin: 0 0 10px; line-height: 1.4; }}
        textarea {{
            width: 100%;
            min-height: 130px;
            resize: vertical;
            margin: 0;
            border-radius: 10px;
            background: rgba(0, 0, 0, 0.35);
            border: 1px solid rgba(255, 210, 101, 0.3);
            padding: 10px;
            font-size: 13px;
            color: #fff3d0;
            line-height: 1.4;
            font-family: Consolas, 'Courier New', monospace;
        }}
        .row {{ display: grid; gap: 8px; margin-top: 12px; }}
        .quick-links {{
            margin-top: 10px;
            display: grid;
            gap: 6px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
        .quick-links a {{
            text-align: center;
            padding: 10px 8px;
            border-radius: 10px;
            border: 1px solid rgba(255, 210, 101, 0.4);
            color: #ffe3a8;
            text-decoration: none;
            font-weight: 700;
            background: rgba(0, 0, 0, 0.18);
        }}
        .leg-checklist {{
            margin-top: 12px;
            border-radius: 10px;
            border: 1px solid rgba(255, 210, 101, 0.3);
            background: rgba(0, 0, 0, 0.25);
            padding: 10px;
        }}
        .leg-checklist h2 {{ margin: 0 0 8px; font-size: 16px; }}
        .leg-checklist .meta {{ font-size: 12px; color: #efcd84; margin-bottom: 8px; }}
        .leg-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }}
        .leg-item {{
            display: grid;
            gap: 8px;
            align-items: center;
            grid-template-columns: 1fr auto;
            border: 1px solid rgba(255, 210, 101, 0.25);
            border-radius: 8px;
            padding: 8px;
            background: rgba(34, 20, 2, 0.6);
        }}
        .leg-item label {{ font-size: 13px; line-height: 1.35; }}
        .leg-actions {{ margin-top: 8px; display: flex; gap: 8px; flex-wrap: wrap; }}
        button, a.btn {{
            display: block;
            width: 100%;
            text-align: center;
            padding: 12px;
            border: 0;
            border-radius: 10px;
            font-weight: 800;
            text-decoration: none;
            cursor: pointer;
        }}
        .primary {{ background: #ffcf58; color: #2a1800; }}
        .secondary {{ background: #2e1a02; color: #ffe3a8; border: 1px solid rgba(255, 210, 101, 0.5); }}
        .hint {{ margin-top: 8px; font-size: 12px; color: #efcd84; }}
    </style>
</head>
<body>
    <div class='card'>
        <h1>DINGKING Slip Ready</h1>
        <p>Your slip text is below. Copy, download, or share it, then open your sportsbook.</p>
        <textarea id='slipText' readonly>{slip_display}</textarea>
        <div class='row'>
            <button id='copyBtn' class='secondary'>Copy Slip Text</button>
            <button id='selectBtn' class='secondary' type='button'>Select Slip Text</button>
            <button id='copyLegsBtn' class='secondary' type='button'>Copy All Legs</button>
            <button id='downloadTxtBtn' class='secondary' type='button'>Download Slip .txt</button>
            <button id='shareBtn' class='secondary' type='button'>Share Slip</button>
            <a id='openTargetBtn' class='btn secondary' href='{safe_target_sportsbook_url}' rel='noopener noreferrer'>Open {safe_target_sportsbook_label}</a>
            <a id='openActionBtn' class='btn secondary' href='https://www.actionnetwork.com/' rel='noopener noreferrer'>Open Action Network</a>
            <a id='openBtn' class='btn primary' href='{safe_target}' rel='noopener noreferrer'>Open Gambly</a>
            <a class='btn secondary' href='/dashboard'>Back to Dashboard</a>
        </div>
        <div class='quick-links'>
            <a href='https://sportsbook.fanduel.com/' rel='noopener noreferrer'>FanDuel Home</a>
            <a href='https://sportsbook.draftkings.com/' rel='noopener noreferrer'>DraftKings Home</a>
            <a href='https://www.betmgm.com/' rel='noopener noreferrer'>BetMGM Home</a>
            <a href='https://www.caesars.com/sportsbook-and-casino' rel='noopener noreferrer'>Caesars Home</a>
        </div>
        <div class='leg-checklist'>
            <h2>Leg Checklist</h2>
            <div class='meta' id='progressText'>0/{len(leg_lines)} added</div>
            <ul class='leg-list'>
                {leg_items_html or "<li class='meta'>No leg lines were detected from this slip.</li>"}
            </ul>
            <div class='leg-actions'>
                <button id='resetChecksBtn' class='secondary' type='button'>Reset Checks</button>
            </div>
        </div>
        <div class='hint'>Best flow: copy first, open your sportsbook, then paste/select legs. If direct book flow fails, open Gambly.</div>
    </div>
    <script>
        const copyBtn = document.getElementById('copyBtn');
        const selectBtn = document.getElementById('selectBtn');
        const copyLegsBtn = document.getElementById('copyLegsBtn');
        const downloadTxtBtn = document.getElementById('downloadTxtBtn');
        const shareBtn = document.getElementById('shareBtn');
        const slipText = document.getElementById('slipText');
        const rawSlipText = {json.dumps(str(slip_text or ''))};
        const progressText = document.getElementById('progressText');
        const legChecks = Array.from(document.querySelectorAll('.leg-check'));
        const legCopyButtons = Array.from(document.querySelectorAll('.leg-copy'));
        const resetChecksBtn = document.getElementById('resetChecksBtn');
        const storageKey = 'dk_legcheck_{token}';

        function fallbackCopyText(text) {{
            const ta = document.createElement('textarea');
            ta.value = text || '';
            ta.setAttribute('readonly', 'readonly');
            ta.style.position = 'fixed';
            ta.style.top = '-9999px';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            ta.setSelectionRange(0, ta.value.length);
            let ok = false;
            try {{
                ok = document.execCommand('copy') === true;
            }} catch {{
                ok = false;
            }}
            document.body.removeChild(ta);
            return ok;
        }}

        async function tryCopyText(text) {{
            const safeText = String(text || '');
            try {{
                if (navigator && navigator.clipboard && navigator.clipboard.writeText) {{
                    await navigator.clipboard.writeText(safeText);
                    return true;
                }}
            }} catch {{}}
            return fallbackCopyText(safeText);
        }}

        async function tryCopy() {{
            const ok = await tryCopyText(rawSlipText || slipText.value || '');
            if (ok) {{
                copyBtn.textContent = 'Copied';
                return;
            }}
            copyBtn.textContent = 'Copy failed - select text below';
            if (slipText) {{
                slipText.focus();
                slipText.select();
            }}
        }}

        function updateProgress() {{
            if (!progressText) return;
            const checked = legChecks.filter((el) => el.checked).length;
            progressText.textContent = `${{checked}}/${{legChecks.length}} added`;
        }}

        function persistChecks() {{
            try {{
                const values = legChecks.map((el) => !!el.checked);
                localStorage.setItem(storageKey, JSON.stringify(values));
            }} catch {{}}
        }}

        function restoreChecks() {{
            try {{
                const raw = localStorage.getItem(storageKey);
                if (!raw) return;
                const values = JSON.parse(raw);
                if (!Array.isArray(values)) return;
                legChecks.forEach((el, idx) => {{
                    el.checked = !!values[idx];
                }});
            }} catch {{}}
        }}

        legChecks.forEach((el) => {{
            el.addEventListener('change', () => {{
                updateProgress();
                persistChecks();
            }});
        }});

        legCopyButtons.forEach((btn) => {{
            btn.addEventListener('click', async () => {{
                const legText = String(btn.getAttribute('data-leg') || '').trim();
                if (!legText) return;
                const ok = await tryCopyText(legText);
                if (ok) {{
                    btn.textContent = 'Copied';
                }} else {{
                    btn.textContent = 'Copy failed';
                }}
            }});
        }});

        if (copyLegsBtn) {{
            copyLegsBtn.addEventListener('click', async () => {{
                const legsText = legChecks
                    .map((el) => String(el.getAttribute('data-leg') || '').trim())
                    .filter((x) => x)
                    .join('\\n');
                if (!legsText) {{
                    copyLegsBtn.textContent = 'No legs found';
                    return;
                }}
                const ok = await tryCopyText(legsText);
                copyLegsBtn.textContent = ok ? 'Legs copied' : 'Copy failed';
            }});
        }}

        if (selectBtn && slipText) {{
            selectBtn.addEventListener('click', () => {{
                slipText.focus();
                slipText.select();
                selectBtn.textContent = 'Selected';
            }});
        }}

        if (downloadTxtBtn) {{
            downloadTxtBtn.addEventListener('click', () => {{
                const content = rawSlipText || slipText.value || '';
                const blob = new Blob([content], {{ type: 'text/plain;charset=utf-8' }});
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'dingking-slip-{token}.txt';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                setTimeout(() => URL.revokeObjectURL(url), 2000);
                downloadTxtBtn.textContent = 'Downloaded';
            }});
        }}

        if (shareBtn) {{
            shareBtn.addEventListener('click', async () => {{
                const content = rawSlipText || slipText.value || '';
                try {{
                    if (navigator && navigator.share) {{
                        await navigator.share({{ title: 'DINGKING Slip', text: content }});
                        shareBtn.textContent = 'Shared';
                        return;
                    }}
                }} catch {{}}
                const ok = await tryCopyText(content);
                shareBtn.textContent = ok ? 'Copied for sharing' : 'Share unavailable';
            }});
        }}

        if (slipText) {{
            slipText.addEventListener('click', () => {{
                slipText.focus();
                slipText.select();
            }});
        }}

        if (resetChecksBtn) {{
            resetChecksBtn.addEventListener('click', () => {{
                legChecks.forEach((el) => {{ el.checked = false; }});
                updateProgress();
                persistChecks();
            }});
        }}

        copyBtn.addEventListener('click', async () => {{
            await tryCopy();
        }});
        restoreChecks();
        updateProgress();
    </script>
</body>
</html>"""
        return HTMLResponse(content=html_doc)


@app.get("/dashboard/debug/links")
def dashboard_debug_links(limit: int = 50):
    safe_limit = max(1, min(500, int(limit)))
    with _gambly_redirect_cache_lock:
        cache_size = len(_gambly_redirect_cache)
        target_index_size = len(_gambly_redirect_target_index)
    return {
        "status": "ok",
        "limit": safe_limit,
        "cache_size": cache_size,
        "target_index_size": target_index_size,
        "ttl_seconds": GAMBLY_REDIRECT_TTL_SECONDS,
        "recent": _recent_gambly_link_events(limit=safe_limit),
    }


@app.get("/agents/status")
def get_agent_status():
    payload = agents.state.snapshot()
    payload["has_storage_state"] = agents.storage_state_path.exists()
    return payload


@app.post("/agents/run-once")
def run_agents_once():
    try:
        return agents.run_once()
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/agents/start")
def start_agents(every_seconds: int = 300):
    _ = every_seconds
    agents.start(every_seconds=300)
    return {"status": "started", "every_seconds": 300}


@app.post("/agents/stop")
def stop_agents():
    agents.stop()
    return {"status": "stopped"}


@app.post("/agents/bootstrap-login")
def bootstrap_login():
    try:
        url = os.getenv("PROPFINDER_CHEATSHEET_URL", "https://propfinder.app/mlb/cheatsheets")
        agents.auth_manager.bootstrap_interactive_login(url=url)
        return {"status": "ok", "storage_state": str(agents.storage_state_path)}
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/agents/links/status")
def get_link_agent_status():
    fanduel_links, fanduel_team_links, fanduel_fresh, fanduel_age = _get_link_cache("fanduel")
    draftkings_links, draftkings_team_links, draftkings_fresh, draftkings_age = _get_link_cache("draftkings")
    return {
        **link_agents.state.snapshot(),
        "books": {
            "fanduel": {
                "cached_links": len(fanduel_links),
                "cached_team_links": len(fanduel_team_links),
                "fresh": fanduel_fresh,
                "age_seconds": round(fanduel_age, 2),
            },
            "draftkings": {
                "cached_links": len(draftkings_links),
                "cached_team_links": len(draftkings_team_links),
                "fresh": draftkings_fresh,
                "age_seconds": round(draftkings_age, 2),
            },
        },
    }


@app.get("/dashboard/link-agent/health")
def dashboard_link_agent_health():
    payload = get_link_agent_status()
    books = payload.get("books") if isinstance(payload, dict) else {}
    fd = books.get("fanduel") if isinstance(books, dict) else {}
    dk = books.get("draftkings") if isinstance(books, dict) else {}
    last_status = str(payload.get("last_status") or "idle")
    if last_status == "error":
        mode = "error"
    elif last_status == "disabled":
        mode = "disabled"
    elif bool(fd.get("fresh")) or bool(dk.get("fresh")):
        mode = "warm_cache"
    else:
        mode = "fallback"
    return {
        "status": "ok",
        "mode": mode,
        "agent": payload,
    }


@app.post("/agents/links/run-once")
def run_link_agents_once():
    try:
        return link_agents.run_once()
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/agents/links/start")
def start_link_agents(every_seconds: int = 300):
    safe_interval = max(60, int(every_seconds))
    link_agents.start(every_seconds=safe_interval)
    return {"status": "started", "every_seconds": safe_interval}


@app.post("/agents/links/stop")
def stop_link_agents():
    link_agents.stop()
    return {"status": "stopped"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/dashboard/app-manifest.json")
def dashboard_manifest() -> JSONResponse:
    return JSONResponse(content=PWA_MANIFEST)


@app.get("/dashboard/sw.js")
def dashboard_sw() -> Response:
    return Response(content=PWA_SW_JS, media_type="application/javascript")


@app.get("/dashboard/icon.svg")
def dashboard_icon() -> Response:
    return Response(content=PWA_ICON_SVG, media_type="image/svg+xml")


@app.get("/dashboard/state")
def dashboard_get_state():
    csv_path = _dashboard_source_csv_path()
    source_mtime = _dashboard_source_csv_mtime(csv_path)

    with dashboard_state._lock:
        if not dashboard_state.parlays and source_mtime is not None:
            try:
                board = _generate_dashboard_board(
                    num_slips=20,
                    mode=dashboard_state.mode,
                    legs_per_slip=dashboard_state.legs_per_slip,
                    hits_profile=dashboard_state.hits_profile,
                    risk_level=dashboard_state.risk_level,
                    lineup_locked_only=dashboard_state.lineup_locked_only,
                    allow_live=dashboard_state.allow_live,
                )
                dashboard_state.parlays = _json_safe(board["parlays"])
                dashboard_state.latest_run_id = board["run_id"]
                dashboard_state.generated_at = datetime.now(tz=timezone.utc).isoformat()
                dashboard_state.next_replace_index = 0
                dashboard_state.source_csv_mtime = source_mtime
            except Exception:
                # Keep returning empty state until the player pool is usable.
                pass

        # Keep already-generated slips stable until user triggers manual regenerate.
        if dashboard_state.parlays and source_mtime is not None and (
            dashboard_state.source_csv_mtime is None or source_mtime > dashboard_state.source_csv_mtime
        ):
            dashboard_state.source_csv_mtime = source_mtime

        _ensure_dashboard_slip_links(
            dashboard_state.parlays,
            mode=dashboard_state.mode,
            hits_profile=dashboard_state.hits_profile,
        )

        snapshot = dashboard_state.snapshot()

    if not snapshot["parlays"]:
        return {
            "status": "empty",
            "source_csv": str(csv_path),
            "source_csv_mtime": source_mtime,
            "count": 0,
            "legs_per_slip": dashboard_state.legs_per_slip,
            "mode": dashboard_state.mode,
            "hits_profile": dashboard_state.hits_profile,
            "risk_level": dashboard_state.risk_level,
            "lineup_locked_only": dashboard_state.lineup_locked_only,
            "allow_live": dashboard_state.allow_live,
            "parlays": [],
        }

    return {
        "status": "ok",
        "source_csv": str(csv_path),
        "source_csv_mtime": source_mtime,
        "count": snapshot["count"],
        "legs_per_slip": snapshot["legs_per_slip"],
        "mode": snapshot["mode"],
        "hits_profile": snapshot["hits_profile"],
        "risk_level": snapshot["risk_level"],
        "lineup_locked_only": snapshot["lineup_locked_only"],
        "allow_live": snapshot["allow_live"],
        "latest_run_id": snapshot["latest_run_id"],
        "generated_at": snapshot["generated_at"],
        "parlays": snapshot["parlays"],
    }


@app.get("/dashboard/routing/books")
def dashboard_routing_books(
    country: str = "US",
    region_state: str = "",
    preferred_books: str | None = None,
):
    state_code = _standardize_region_state(region_state)
    preferred = _parse_preferred_books_param(preferred_books)
    books = filter_books_for_region(country=country, region_state=state_code, preferred_books=preferred)
    return {
        "status": "ok",
        "country": str(country or "US").strip().upper(),
        "region_state": state_code,
        "books": [
            {
                "book": book,
                "label": BOOK_LABELS.get(book, book.title()),
                "one_click_capable": bool(BOOK_ONE_CLICK_CAPABILITY.get(book, False)),
            }
            for book in books
        ],
    }


@app.get("/dashboard/routing/slip/{slip_id}")
def dashboard_routing_for_slip(
    slip_id: str,
    request: Request,
    country: str = "US",
    region_state: str = "",
    preferred_books: str | None = None,
):
    target_slip_id = str(slip_id or "").strip()
    if not target_slip_id:
        raise HTTPException(status_code=400, detail="slip_id is required.")

    with dashboard_state._lock:
        _ensure_dashboard_slip_links(
            dashboard_state.parlays,
            mode=dashboard_state.mode,
            hits_profile=dashboard_state.hits_profile,
        )
        live_idx = -1
        live_slip: dict | None = None
        for idx, slip in enumerate(dashboard_state.parlays):
            if not isinstance(slip, dict):
                continue
            if str(slip.get("slip_id") or "").strip() == target_slip_id:
                live_idx = idx
                live_slip = slip
                break

        if live_slip is not None:
            return {
                "status": "ok",
                "source": "dashboard_state",
                "mode": dashboard_state.mode,
                "hits_profile": dashboard_state.hits_profile,
                "run_id": dashboard_state.latest_run_id,
                **_build_slip_routing_payload(
                    slip=live_slip,
                    slip_idx=live_idx,
                    mode=dashboard_state.mode,
                    hits_profile=dashboard_state.hits_profile,
                    country=country,
                    region_state=_standardize_region_state(region_state),
                    base_origin=str(request.base_url).rstrip("/"),
                    preferred_books=_parse_preferred_books_param(preferred_books),
                ),
            }

    persisted = fetch_slip_by_id(target_slip_id)
    if not persisted:
        raise HTTPException(status_code=404, detail="Slip not found")

    run_payload = persisted.get("run") if isinstance(persisted, dict) else {}
    mode = _normalize_dashboard_mode(str((run_payload or {}).get("mode") or "balanced"))
    hits_profile = _normalize_hits_profile(str((run_payload or {}).get("hits_profile") or "high-frequency"))
    slip_index = int(persisted.get("slip_index") or 0)
    slip = persisted.get("slip") if isinstance(persisted.get("slip"), dict) else {}

    return {
        "status": "ok",
        "source": "portfolio_runs",
        "mode": mode,
        "hits_profile": hits_profile,
        "run_id": str(persisted.get("run_id") or ""),
        **_build_slip_routing_payload(
            slip=slip,
            slip_idx=slip_index,
            mode=mode,
            hits_profile=hits_profile,
            country=country,
            region_state=_standardize_region_state(region_state),
            base_origin=str(request.base_url).rstrip("/"),
            preferred_books=_parse_preferred_books_param(preferred_books),
        ),
    }


@app.post("/dashboard/generate-all")
def dashboard_generate_all(
    mode: str = "balanced",
    legs_per_slip: int = 4,
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    lineup_locked_only: bool = False,
    allow_live: bool = False,
):
    try:
        with dashboard_state._lock:
            if legs_per_slip < 1 or legs_per_slip > 20:
                raise ValueError("legs_per_slip must be between 1 and 20.")
            mode_key = _normalize_dashboard_mode(mode)
            risk_key = _normalize_risk_level(risk_level)
            hits_profile_key = _normalize_hits_profile(hits_profile)

            board = _generate_dashboard_board(
                num_slips=20,
                mode=mode_key,
                legs_per_slip=legs_per_slip,
                hits_profile=hits_profile_key,
                risk_level=risk_key,
                lineup_locked_only=lineup_locked_only,
                allow_live=bool(allow_live),
            )
            dashboard_state.parlays = _json_safe(board["parlays"])
            dashboard_state.latest_run_id = board["run_id"]
            dashboard_state.generated_at = datetime.now(tz=timezone.utc).isoformat()
            dashboard_state.next_replace_index = 0
            dashboard_state.mode = mode_key
            dashboard_state.legs_per_slip = legs_per_slip
            dashboard_state.hits_profile = hits_profile_key
            dashboard_state.risk_level = risk_key
            dashboard_state.lineup_locked_only = bool(lineup_locked_only)
            dashboard_state.allow_live = bool(allow_live)
            dashboard_state.source_csv_mtime = _dashboard_source_csv_mtime()
            _ensure_dashboard_slip_links(
                dashboard_state.parlays,
                mode=dashboard_state.mode,
                hits_profile=dashboard_state.hits_profile,
            )

            return {
                "status": "ok",
                "count": len(dashboard_state.parlays),
                "legs_per_slip": dashboard_state.legs_per_slip,
                "mode": dashboard_state.mode,
                "hits_profile": dashboard_state.hits_profile,
                "risk_level": dashboard_state.risk_level,
                "lineup_locked_only": dashboard_state.lineup_locked_only,
                "allow_live": dashboard_state.allow_live,
                "latest_run_id": dashboard_state.latest_run_id,
                "generated_at": dashboard_state.generated_at,
                "parlays": dashboard_state.parlays,
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/dashboard/generate-one")
def dashboard_generate_one(
    mode: str = "balanced",
    legs_per_slip: int = 4,
    hits_profile: str = "high-frequency",
    risk_level: str = "balanced",
    lineup_locked_only: bool = False,
    allow_live: bool = False,
):
    try:
        with dashboard_state._lock:
            if legs_per_slip < 1 or legs_per_slip > 20:
                raise ValueError("legs_per_slip must be between 1 and 20.")
            mode_key = _normalize_dashboard_mode(mode)
            risk_key = _normalize_risk_level(risk_level)
            hits_profile_key = _normalize_hits_profile(hits_profile)

            dashboard_state.mode = mode_key
            dashboard_state.legs_per_slip = legs_per_slip
            dashboard_state.hits_profile = hits_profile_key
            dashboard_state.risk_level = risk_key
            dashboard_state.lineup_locked_only = bool(lineup_locked_only)
            dashboard_state.allow_live = bool(allow_live)
            dashboard_state.source_csv_mtime = _dashboard_source_csv_mtime()
            if not dashboard_state.parlays:
                board_all = _generate_dashboard_board(
                    num_slips=20,
                    mode=dashboard_state.mode,
                    legs_per_slip=dashboard_state.legs_per_slip,
                    hits_profile=dashboard_state.hits_profile,
                    risk_level=dashboard_state.risk_level,
                    lineup_locked_only=dashboard_state.lineup_locked_only,
                    allow_live=dashboard_state.allow_live,
                )
                dashboard_state.parlays = _json_safe(board_all["parlays"])
                dashboard_state.latest_run_id = board_all["run_id"]
                dashboard_state.generated_at = datetime.now(tz=timezone.utc).isoformat()
                dashboard_state.next_replace_index = 0

            board_one = _generate_dashboard_board(
                num_slips=4,
                mode=dashboard_state.mode,
                legs_per_slip=dashboard_state.legs_per_slip,
                hits_profile=dashboard_state.hits_profile,
                risk_level=dashboard_state.risk_level,
                lineup_locked_only=dashboard_state.lineup_locked_only,
                allow_live=dashboard_state.allow_live,
            )
            if not board_one["parlays"]:
                raise RuntimeError("No parlay generated in one-at-a-time mode.")

            replaced_slot = dashboard_state.next_replace_index % 20
            dashboard_state.parlays[replaced_slot] = _json_safe(board_one["parlays"][0])
            dashboard_state.next_replace_index = (replaced_slot + 1) % 20
            dashboard_state.latest_run_id = board_one["run_id"]
            dashboard_state.generated_at = datetime.now(tz=timezone.utc).isoformat()
            _ensure_dashboard_slip_links(
                dashboard_state.parlays,
                mode=dashboard_state.mode,
                hits_profile=dashboard_state.hits_profile,
            )

            return {
                "status": "ok",
                "count": len(dashboard_state.parlays),
                "legs_per_slip": dashboard_state.legs_per_slip,
                "mode": dashboard_state.mode,
                "hits_profile": dashboard_state.hits_profile,
                "risk_level": dashboard_state.risk_level,
                "lineup_locked_only": dashboard_state.lineup_locked_only,
                "allow_live": dashboard_state.allow_live,
                "latest_run_id": dashboard_state.latest_run_id,
                "generated_at": dashboard_state.generated_at,
                "replaced_slot": replaced_slot,
                "parlay": dashboard_state.parlays[replaced_slot],
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/dashboard/refresh-late-swap")
def dashboard_refresh_late_swap():
    try:
        with dashboard_state._lock:
            if not dashboard_state.parlays:
                raise ValueError("No active board. Generate all parlays first.")

            late_slots: list[int] = []
            for idx, slip in enumerate(dashboard_state.parlays):
                legs = slip.get("legs") if isinstance(slip, dict) else None
                if not isinstance(legs, list):
                    continue
                if any(str(leg.get("start_time_bucket", "")).strip().lower() == "late" for leg in legs if isinstance(leg, dict)):
                    late_slots.append(idx)

            if not late_slots:
                return {
                    "status": "ok",
                    "replaced_slots": [],
                    "replaced_count": 0,
                    "message": "No late-window slips to refresh.",
                    "latest_run_id": dashboard_state.latest_run_id,
                    "generated_at": dashboard_state.generated_at,
                }

            board = _generate_dashboard_board(
                num_slips=max(4, len(late_slots)),
                mode=dashboard_state.mode,
                legs_per_slip=dashboard_state.legs_per_slip,
                hits_profile=dashboard_state.hits_profile,
                risk_level=dashboard_state.risk_level,
                lineup_locked_only=dashboard_state.lineup_locked_only,
                allow_live=dashboard_state.allow_live,
            )

            fresh = _json_safe(board["parlays"])
            for i, slot in enumerate(late_slots):
                dashboard_state.parlays[slot] = fresh[i % len(fresh)]

            dashboard_state.latest_run_id = board["run_id"]
            dashboard_state.generated_at = datetime.now(tz=timezone.utc).isoformat()
            dashboard_state.source_csv_mtime = _dashboard_source_csv_mtime()
            _ensure_dashboard_slip_links(
                dashboard_state.parlays,
                mode=dashboard_state.mode,
                hits_profile=dashboard_state.hits_profile,
            )

            return {
                "status": "ok",
                "replaced_slots": late_slots,
                "replaced_count": len(late_slots),
                "latest_run_id": dashboard_state.latest_run_id,
                "generated_at": dashboard_state.generated_at,
            }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/dashboard/player-pool")
def dashboard_player_pool(limit: int = 120, lineup_locked_only: bool = False, allow_live: bool = False):
    try:
        safe_limit = max(20, min(600, int(limit)))
        players = _build_dashboard_player_pool(lineup_locked_only=bool(lineup_locked_only), allow_live=bool(allow_live))
        rank_adjustments = _dashboard_rank_adjustments()
        return {
            "status": "ok",
            "count": len(players),
            "limit": safe_limit,
            "lineup_locked_only": bool(lineup_locked_only),
            "allow_live": bool(allow_live),
            "rank_adjustments": rank_adjustments,
            "categories": [
                {"key": "hr", "label": "Home Run Rank", "field": "hr_score"},
                {"key": "hits", "label": "Hits Rank", "field": "hit_score"},
                {"key": "tb", "label": "Total Bases Rank", "field": "tb_score"},
                {"key": "rbi", "label": "RBI Rank", "field": "rbi_score"},
                {"key": "hrr", "label": "HRR Rank", "field": "hrr_score"},
                {"key": "value", "label": "Best Value", "field": "portfolio_value_score"},
                {"key": "matchup", "label": "Pitcher Matchup", "field": "pitcher_vuln_score"},
                {"key": "recent", "label": "Recent Form", "field": "recent_form_score"},
            ],
            "subcategories": [
                {"key": "overall", "label": "Overall"},
                {"key": "value", "label": "Value"},
                {"key": "safe", "label": "Safe"},
                {"key": "contrarian", "label": "Contrarian"},
            ],
            "players": players[:safe_limit],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/dashboard/rankings")
def dashboard_rankings(
    category: str = "hr",
    subcategory: str = "overall",
    limit: int = 80,
    lineup_locked_only: bool = False,
    allow_live: bool = False,
):
    try:
        safe_limit = max(20, min(600, int(limit)))
        category_key = _normalize_category_key(category)
        subcategory_key = _normalize_subcategory_key(subcategory)

        players = _build_dashboard_player_pool(lineup_locked_only=bool(lineup_locked_only), allow_live=bool(allow_live))
        rank_adjustments = _dashboard_rank_adjustments()
        categories = rank_adjustments.get("categories", {}) if isinstance(rank_adjustments, dict) else {}
        subcategories = rank_adjustments.get("subcategories", {}) if isinstance(rank_adjustments, dict) else {}

        category_weight = float(categories.get(category_key, 1.0)) if isinstance(categories, dict) else 1.0
        subcategory_weight = float(subcategories.get(subcategory_key, 1.0)) if isinstance(subcategories, dict) else 1.0

        field_by_category = {
            "hr": "hr_score",
            "hits": "hit_score",
            "tb": "tb_score",
            "rbi": "rbi_score",
            "hrr": "hrr_score",
            "value": "portfolio_value_score",
            "matchup": "pitcher_vuln_score",
            "recent": "recent_form_score",
        }
        score_field = field_by_category.get(category_key, "hr_score")

        ranked: list[dict] = []
        for player in players:
            if not isinstance(player, dict):
                continue
            base_score = float(player.get(score_field) or 0.0)
            adjusted_score = round(base_score * category_weight * subcategory_weight, 6)
            row = dict(player)
            row["rank_score"] = adjusted_score
            ranked.append(row)

        ranked.sort(key=lambda item: float(item.get("rank_score") or 0.0), reverse=True)

        return {
            "status": "ok",
            "category": category_key,
            "subcategory": subcategory_key,
            "lineup_locked_only": bool(lineup_locked_only),
            "allow_live": bool(allow_live),
            "count": len(ranked),
            "limit": safe_limit,
            "rank_adjustments": rank_adjustments,
            "players": ranked[:safe_limit],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/dashboard/sportsbook/resolve-links")
def dashboard_resolve_sportsbook_links(payload: dict):
    failure_stage = "request_received"
    requested_book = str(payload.get("book", "")).strip().lower()
    book = requested_book
    if book not in SUPPORTED_SPORTSBOOKS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported book. Use gambly, actionnetwork, fanduel, draftkings, fanatics, espn_bet, caesars, or betmgm.",
        )

    requested_target_sportsbook = str(
        payload.get("target_sportsbook")
        or payload.get("sportsbook")
        or requested_book
        or "gambly"
    ).strip()
    target_sportsbook = _normalize_target_sportsbook(requested_target_sportsbook)
    provider_book = target_sportsbook if target_sportsbook in {"fanduel", "draftkings"} else ""

    slips = payload.get("slips")
    if not isinstance(slips, list) or not slips:
        raise HTTPException(status_code=400, detail="slips must be a non-empty list.")

    player_names: list[str] = []
    for slip in slips:
        if not isinstance(slip, dict):
            continue
        legs = slip.get("legs")
        if not isinstance(legs, list):
            continue
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            player = str(leg.get("player_name", "")).strip()
            if player:
                player_names.append(player)
    logger.info(
        "sportsbook_resolver request_received",
        extra={
            "book": book,
            "slips_count": len(slips),
            "player_names": player_names,
        },
    )

    mode = str(payload.get("mode", "balanced")).strip().lower()
    hits_profile = str(payload.get("hits_profile", "")).strip().lower()
    verified_only = bool(payload.get("verified_only", True))
    allow_event_fallback = bool(payload.get("allow_event_fallback", False))
    prop_label = _prop_label_for_request(mode=mode, hits_profile=hits_profile)
    market_keys = _market_keys_for_request(mode=mode, hits_profile=hits_profile)
    prop_mode = bool(market_keys)

    api_key = os.getenv("THE_ODDS_API_KEY", "").strip()
    using_api = bool(api_key and market_keys and provider_book in {"fanduel", "draftkings"})
    links_by_player: dict[str, str] = {}
    links_by_team: dict[str, str] = {}
    cache_used = False
    cache_age_seconds = None
    provider_diagnostics: dict[str, object] = {"timeout": False, "error": None}
    direct_source_by_player: dict[str, str] = {}
    direct_source_counts = {
        "requested_market_source": 0,
        "backup_market_source": 0,
        "event_market_source": 0,
    }
    if using_api:
        failure_stage = "cache_lookup"
        cached_links, cached_team_links, cache_fresh, cache_age = _get_link_cache(provider_book)
        cache_age_seconds = round(cache_age, 3)
        # Cache is populated from broad market pulls; for prop-accurate resolution,
        # prefer live fetches when a specific prop market is requested.
        if cache_fresh and cached_links and not prop_mode:
            links_by_player = cached_links
            if allow_event_fallback:
                links_by_team = cached_team_links
            cache_used = True
            logger.info(
                "sportsbook_resolver cache_hit",
                extra={"book": provider_book, "cached_links": len(links_by_player), "cache_age_seconds": cache_age_seconds},
            )
            for player_key in links_by_player.keys():
                direct_source_by_player[player_key] = "requested_market_source"
                direct_source_counts["requested_market_source"] += 1
        else:
            logger.info(
                "sportsbook_resolver cache_miss",
                extra={"book": provider_book, "cache_age_seconds": cache_age_seconds},
            )

    if using_api:
        if not links_by_player:
            failure_stage = "provider_primary"
            logger.info(
                "sportsbook_resolver provider_call_started",
                extra={"book": provider_book, "stage": failure_stage, "markets": market_keys},
            )
            primary_links = _fetch_book_player_links(
                book=provider_book,
                api_key=api_key,
                market_keys=market_keys,
                diagnostics=provider_diagnostics,
            )
            logger.info(
                "sportsbook_resolver provider_call_finished",
                extra={"book": provider_book, "stage": failure_stage, "returned_links": len(primary_links)},
            )
            for player_key, link in primary_links.items():
                if player_key not in links_by_player:
                    links_by_player[player_key] = link
                    direct_source_by_player[player_key] = "requested_market_source"
                    direct_source_counts["requested_market_source"] += 1

        # Multi-source fallback within odds data: broaden market pull in case
        # the requested key family is sparse/missing at the book.
            if not links_by_player:
                failure_stage = "provider_event"
                logger.info(
                    "sportsbook_resolver provider_call_started",
                    extra={"book": provider_book, "stage": failure_stage, "markets": ALL_PLAYER_PROP_MARKET_KEYS},
                )
                event_links = _fetch_book_player_links_from_event_odds(
                    book=provider_book,
                    api_key=api_key,
                    market_keys=ALL_PLAYER_PROP_MARKET_KEYS,
                    diagnostics=provider_diagnostics,
                )
                logger.info(
                    "sportsbook_resolver provider_call_finished",
                    extra={"book": provider_book, "stage": failure_stage, "returned_links": len(event_links)},
                )
                for player_key, link in event_links.items():
                    if player_key in links_by_player:
                        continue
                    links_by_player[player_key] = link
                    direct_source_by_player[player_key] = "event_market_source"
                    direct_source_counts["event_market_source"] += 1

        if allow_event_fallback and not links_by_player and not links_by_team:
            failure_stage = "team_fallback"
            links_by_team = _fetch_book_team_links(book=provider_book, api_key=api_key, diagnostics=provider_diagnostics)
            logger.info(
                "sportsbook_resolver team_fallback_finished",
                extra={"book": provider_book, "team_links": len(links_by_team)},
            )
    per_slip_links: list[str | None] = []
    per_slip_leg_links: list[list[str | None]] = []
    per_slip_gambly_links: list[str | None] = []
    per_slip_gambly_redirects: list[str | None] = []
    per_slip_texts: list[str] = []
    # Always allow non-gambly search fallback for unresolved legs so every player
    # still has an actionable open path, even when provider coverage is partial.
    allow_search_fallback = book != "gambly"
    resolved_count = 0
    total_legs = 0
    direct_links_count = 0
    requested_market_links_count = 0
    backup_market_links_count = 0
    event_market_links_count = 0
    event_links_count = 0
    fallback_links_count = 0

    for slip in slips:
        selected_link: str | None = None
        leg_links: list[str | None] = []
        slip_id = str(slip.get("slip_id") or "").strip() if isinstance(slip, dict) else ""
        slip_text = _compose_sportsbook_slip_text(
            slip=slip if isinstance(slip, dict) else {},
            idx=len(per_slip_links),
            prop_label=prop_label,
        )
        if isinstance(slip, dict):
            legs = slip.get("legs")
            if isinstance(legs, list):
                for leg in legs:
                    if not isinstance(leg, dict):
                        leg_links.append(None)
                        continue
                    total_legs += 1
                    player_name = str(leg.get("player_name", "")).strip()
                    player_lookup_keys = _player_lookup_keys(player_name)
                    if not player_lookup_keys:
                        leg_links.append(None)
                        continue
                    link = None
                    source_name = ""
                    for candidate_key in player_lookup_keys:
                        candidate_link = links_by_player.get(candidate_key)
                        if not candidate_link:
                            continue
                        link = candidate_link
                        source_name = direct_source_by_player.get(candidate_key, "")
                        break
                    if link:
                        direct_links_count += 1
                        if source_name == "requested_market_source":
                            requested_market_links_count += 1
                        elif source_name == "backup_market_source":
                            backup_market_links_count += 1
                        elif source_name == "event_market_source":
                            event_market_links_count += 1
                    elif not verified_only and allow_event_fallback:
                        team_candidates = _team_lookup_candidates(str(leg.get("team") or ""))
                        for candidate in team_candidates:
                            event_link = links_by_team.get(candidate)
                            if event_link:
                                link = event_link
                                event_links_count += 1
                                break
                    if not link and allow_search_fallback and book != "gambly":
                        failure_stage = "search_fallback"
                        search_book = provider_book or target_sportsbook or book
                        link = _fallback_search_link(book=search_book, player_name=player_name, prop_label=prop_label)
                        if link:
                            fallback_links_count += 1
                    leg_links.append(link)
                    if link:
                        selected_link = selected_link or link

        if selected_link:
            resolved_count += 1
        per_slip_links.append(selected_link)
        per_slip_leg_links.append(leg_links)
        per_slip_texts.append(slip_text)
        slip_gambly_link = _build_gambly_slip_link(slip_text=slip_text, target_sportsbook=target_sportsbook)
        per_slip_gambly_links.append(slip_gambly_link)
        per_slip_gambly_redirects.append(
            _cache_gambly_redirect(slip_gambly_link, slip_id=slip_id or None, source="resolve_links_slip")
        )

    if verified_only:
        if total_legs > 0 and direct_links_count >= total_legs:
            status = "verified_all"
        elif fallback_links_count > 0:
            status = "fallback_search"
        elif direct_links_count > 0:
            status = "verified_partial"
        else:
            status = "verified_none"
    else:
        if direct_links_count > 0:
            status = "ok"
        elif event_links_count > 0:
            status = "event_only"
        elif using_api:
            status = "fallback_only"
        else:
            status = "fallback_search"

    fallback_reason = None
    provider_status = "not_applicable"
    if provider_book in {"fanduel", "draftkings"}:
        if not api_key:
            provider_status = "disabled"
            fallback_reason = "missing_api_key"
        elif cache_used:
            provider_status = "warm_cache"
        elif bool(provider_diagnostics.get("error")):
            provider_status = "error"
            if bool(provider_diagnostics.get("timeout")):
                fallback_reason = "provider_timeout"
            else:
                fallback_reason = "provider_unavailable"
        else:
            provider_status = "live_provider"

    if fallback_reason is None and fallback_links_count > 0:
        if direct_source_counts["requested_market_source"] == 0 and direct_source_counts["backup_market_source"] == 0 and direct_source_counts["event_market_source"] == 0:
            fallback_reason = "no_market_match"
        elif direct_links_count == 0:
            fallback_reason = "player_match_failed"

    timeout_status = "triggered" if bool(provider_diagnostics.get("timeout")) else "not_triggered"
    logger.info(
        "sportsbook_resolver completed",
        extra={
            "book": book,
            "provider_book": provider_book,
            "slips_count": len(slips),
            "cache_used": cache_used,
            "fallback_reason": fallback_reason,
            "resolved": resolved_count,
            "fallback_links": fallback_links_count,
            "failure_stage": failure_stage,
        },
    )

    bundle_text = "\n\n".join([text for text in per_slip_texts if str(text).strip()])
    gambly_link = _build_gambly_slip_link(slip_text=bundle_text, target_sportsbook=target_sportsbook)
    gambly_redirect = _cache_gambly_redirect(gambly_link, source="resolve_links_bundle")

    return {
        "status": status,
        "gambly_enabled": GAMBLY_ENABLED,
        "requested_book": requested_book,
        "requested_target_sportsbook": requested_target_sportsbook,
        "target_sportsbook": target_sportsbook,
        "book": book,
        "provider_book": provider_book,
        "using_api": using_api,
        "cache_used": cache_used,
        "cache_age_seconds": cache_age_seconds,
        "provider_status": provider_status,
        "timeout_status": timeout_status,
        "fallback_reason": fallback_reason,
        "exact_failure_stage": failure_stage,
        "verified_only": verified_only,
        "direct_source_counts": direct_source_counts,
        "total_legs": total_legs,
        "direct_links": direct_links_count,
        "requested_market_links": requested_market_links_count,
        "backup_market_links": backup_market_links_count,
        "event_market_links": event_market_links_count,
        "event_links": event_links_count,
        "fallback_links": fallback_links_count,
        "links": per_slip_links,
        "slip_links": per_slip_leg_links,
        "sportsbook_deeplinks": per_slip_links,
        "gambly_link": gambly_link,
        "gambly_redirect": gambly_redirect,
        "gambly_links": per_slip_gambly_links,
        "gambly_redirects": per_slip_gambly_redirects,
        "slip_texts": per_slip_texts,
        "resolved": resolved_count,
        "total": len(slips),
    }
