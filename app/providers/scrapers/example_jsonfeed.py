"""
TEMPLATE — copy this file to e.g. `onexbet.py`, set bookmaker_key and write
fetch() against that bookmaker's odds source (official API, partner feed,
or a JSON endpoint you are allowed to use). Disabled by default.
"""
from .base import BookmakerScraper, ScrapedOdds


class ExampleJsonFeed(BookmakerScraper):
    bookmaker_key = "example"
    interval = 5.0
    enabled = False

    FEED_URL = "https://example.com/odds.json"

    async def fetch(self, client) -> list[ScrapedOdds]:
        r = await client.get(self.FEED_URL)
        r.raise_for_status()
        out = []
        for g in r.json().get("games", []):
            out.append(ScrapedOdds(
                sport=g["sport"], league=g["league"], home=g["home"], away=g["away"],
                commence=int(g["start"]) * 1000, market="h2h",
                outcomes=[g["home"], "Draw", g["away"]],
                prices=[g["odds"]["1"], g["odds"]["X"], g["odds"]["2"]],
                live=g.get("live", False),
            ))
        return out
