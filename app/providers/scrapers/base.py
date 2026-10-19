"""
Scraper plug-in contract.

Every bookmaker scraper is one file in this folder with one class that
subclasses BookmakerScraper. The ScraperProvider runs them all concurrently,
matches the same match across bookmakers and feeds the arb engine.

A scraper only has to return a list of ScrapedOdds — one per event+market.
Team names don't need to match other books exactly; the matcher normalises
names and start times (see matcher.py), and you can add aliases there.
"""
from dataclasses import dataclass, field


@dataclass
class ScrapedOdds:
    sport: str                 # "Soccer", "Tennis", "Basketball", ...
    league: str                # "Premier League"
    home: str
    away: str
    commence: int              # start time, unix ms
    market: str                # "h2h" | "totals_2.5" | "btts" | "spreads_-1.5"
    outcomes: list[str]        # in order, e.g. [home, "Draw", away]
    prices: list[float]        # decimal odds, same order as outcomes
    live: bool = False
    extra: dict = field(default_factory=dict)


class BookmakerScraper:
    #: must equal the bookmaker "key" in the admin panel (e.g. "1xbet")
    bookmaker_key: str = ""
    #: seconds between scrapes for this bookmaker
    interval: float = 5.0
    #: set False to keep the file but skip it
    enabled: bool = True

    async def fetch(self, client) -> list[ScrapedOdds]:
        """Return the current odds. `client` is a shared httpx.AsyncClient."""
        raise NotImplementedError
