import urllib.parse

GAMBLY_BET_BUILDER_BASE = (
    "https://gambly.com/bet-builder?type=straight%7Cplayer_prop"
    "&partials=exclude&alts=exclude&minPrice=-200&maxPrice=200"
    "&limit=10&sort_by=popularity"
)


def build_gambly_link(text: str) -> str:
    encoded = urllib.parse.quote(str(text or "").strip())
    if not encoded:
        return GAMBLY_BET_BUILDER_BASE
    return f"{GAMBLY_BET_BUILDER_BASE}&q={encoded}&text={encoded}"
