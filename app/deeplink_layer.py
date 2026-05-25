from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Output-layer only module: this file must never influence strategy selection.
# Strategy engine decides WHAT to bet. Deeplink layer decides WHERE/HOW to open.

BOOK_HOME_URLS: dict[str, str] = {
    "gambly": "https://gambly.com/bet-builder?type=straight%7Cplayer_prop&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200&limit=10&sort_by=popularity",
    "fanduel": "https://sportsbook.fanduel.com/",
    "draftkings": "https://sportsbook.draftkings.com/",
    "fanatics": "https://sportsbook.fanatics.com/",
    "espn_bet": "https://espnbet.com/",
    "caesars": "https://www.caesars.com/sportsbook-and-casino",
    "betmgm": "https://sports.betmgm.com/",
    "actionnetwork": "https://www.actionnetwork.com/",
}

BOOK_LABELS: dict[str, str] = {
    "gambly": "Gambly",
    "fanduel": "FanDuel",
    "draftkings": "DraftKings",
    "fanatics": "Fanatics",
    "espn_bet": "ESPN BET",
    "caesars": "Caesars",
    "betmgm": "BetMGM",
    "actionnetwork": "Action Network",
}

# Set to True only for books where we can reliably prebuild a full betslip.
BOOK_ONE_CLICK_CAPABILITY: dict[str, bool] = {
    "gambly": False,
    "fanduel": False,
    "draftkings": False,
    "fanatics": False,
    "espn_bet": False,
    "caesars": False,
    "betmgm": False,
    "actionnetwork": False,
}


def normalize_book_key(value: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "fd": "fanduel",
        "dk": "draftkings",
        "espn": "espn_bet",
        "espnbet": "espn_bet",
        "mgm": "betmgm",
        "bet_mgm": "betmgm",
    }
    key = aliases.get(key, key)
    if key in BOOK_HOME_URLS:
        return key
    return "gambly"


def _book_available_in_region(book_key: str, country: str, region_state: str) -> bool:
    # Region model is intentionally conservative for now: US books are shown in US,
    # and we can layer per-state licensing rules later without changing strategy code.
    c = str(country or "US").strip().upper()
    _ = str(region_state or "").strip().upper()
    if c != "US":
        return book_key in {"gambly", "actionnetwork"}
    return True


def filter_books_for_region(
    *,
    country: str = "US",
    region_state: str = "",
    preferred_books: list[str] | None = None,
) -> list[str]:
    if preferred_books:
        normalized = [normalize_book_key(item) for item in preferred_books]
        ordered = [item for item in normalized if item in BOOK_HOME_URLS]
    else:
        ordered = list(BOOK_HOME_URLS.keys())

    output: list[str] = []
    for book in ordered:
        if _book_available_in_region(book, country=country, region_state=region_state):
            output.append(book)
    return output


@dataclass
class StandardizedSlip:
    slip_id: str
    archetype: str
    story: str
    legs: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": "1.0",
            "slip_id": self.slip_id,
            "archetype": self.archetype,
            "story": self.story,
            "legs": self.legs,
        }


def build_standardized_slip(slip: dict[str, Any]) -> dict[str, Any]:
    legs_raw = slip.get("legs") if isinstance(slip, dict) else []
    legs: list[dict[str, Any]] = []
    if isinstance(legs_raw, list):
        for idx, leg in enumerate(legs_raw):
            if not isinstance(leg, dict):
                continue
            legs.append(
                {
                    "index": idx,
                    "player_name": str(leg.get("player_name") or "").strip(),
                    "team": str(leg.get("team") or "").strip(),
                    "market": str(leg.get("market") or leg.get("rank_category") or "").strip(),
                    "game_id": leg.get("game_id"),
                    "park_name": str(leg.get("park_name") or "").strip(),
                }
            )

    payload = StandardizedSlip(
        slip_id=str((slip or {}).get("slip_id") or "").strip(),
        archetype=str((slip or {}).get("archetype") or "").strip(),
        story=str((slip or {}).get("story") or "").strip(),
        legs=legs,
    )
    return payload.as_dict()


def build_route_plan(
    *,
    country: str,
    region_state: str,
    preferred_books: list[str] | None,
    gambly_link: str,
    go_path: str,
    share_link_path: str,
    base_origin: str,
) -> dict[str, Any]:
    books = filter_books_for_region(
        country=country,
        region_state=region_state,
        preferred_books=preferred_books,
    )

    absolute_share = ""
    if share_link_path:
        absolute_share = f"{str(base_origin).rstrip('/')}{share_link_path}"

    routes: list[dict[str, Any]] = []
    for book in books:
        routes.append(
            {
                "book": book,
                "label": BOOK_LABELS.get(book, book.title()),
                "one_click_capable": bool(BOOK_ONE_CLICK_CAPABILITY.get(book, False)),
                "open_url": BOOK_HOME_URLS.get(book, ""),
                "fallback_url": str(gambly_link or "").strip() or BOOK_HOME_URLS["gambly"],
                "share_url": absolute_share,
            }
        )

    return {
        "country": str(country or "US").upper(),
        "region_state": str(region_state or "").upper(),
        "books": books,
        "routes": routes,
    }
