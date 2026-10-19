"""
Runs every enabled scraper in app/providers/scrapers/ concurrently (each on its
own interval), merges the same event across bookmakers and pushes the merged
feed to the arb engine. Set ODDS_PROVIDER=scrapers (or "hybrid" to combine
with The Odds API).
"""
import asyncio
import importlib
import inspect
import pkgutil
import time

import httpx

from . import scrapers as scrapers_pkg
from .scrapers.base import BookmakerScraper
from .scrapers.matcher import event_key, norm_name

STALE_MS = 90_000  # drop a bookmaker's line if not refreshed for 90s


def load_scrapers() -> list[BookmakerScraper]:
    found = []
    for mod in pkgutil.iter_modules(scrapers_pkg.__path__):
        if mod.name in ("base", "matcher"):
            continue
        m = importlib.import_module(f"{scrapers_pkg.__name__}.{mod.name}")
        for _, cls in inspect.getmembers(m, inspect.isclass):
            if issubclass(cls, BookmakerScraper) and cls is not BookmakerScraper and cls.enabled and cls.bookmaker_key:
                found.append(cls())
    return found


class ScraperProvider:
    name = "scrapers"

    def __init__(self, on_update, push_every=2.0):
        self.on_update = on_update
        self.push_every = push_every
        self.scrapers = load_scrapers()
        self.lines = {}  # (bookmaker, event_key, market) -> (ScrapedOdds, ts)
        self.status = {s.bookmaker_key: {"ok": None, "count": 0, "error": None, "at": None} for s in self.scrapers}

    async def _loop(self, s: BookmakerScraper, client):
        while True:
            try:
                odds = await s.fetch(client)
                ts = int(time.time() * 1000)
                for o in odds:
                    self.lines[(s.bookmaker_key, event_key(o.sport, o.home, o.away, o.commence), o.market)] = (o, ts)
                self.status[s.bookmaker_key] = {"ok": True, "count": len(odds), "error": None, "at": ts}
            except Exception as e:
                self.status[s.bookmaker_key] = {"ok": False, "count": 0, "error": str(e)[:200], "at": int(time.time() * 1000)}
            await asyncio.sleep(s.interval)

    def merged(self) -> list[dict]:
        now = int(time.time() * 1000)
        events = {}
        for (book, ekey, market), (o, ts) in list(self.lines.items()):
            if now - ts > STALE_MS:
                self.lines.pop((book, ekey, market), None)
                continue
            ev = events.setdefault(ekey, dict(id=ekey, sportKey=o.sport.lower(), sport=o.sport, league=o.league,
                                              home=o.home, away=o.away, commence=o.commence, live=o.live,
                                              markets={}, updated=ts))
            prices = list(o.prices)
            # align orientation if this book lists home/away the other way round
            if market == "h2h" or market.startswith("spreads"):
                if norm_name(o.home) != norm_name(ev["home"]):
                    prices.reverse()
            m = ev["markets"].setdefault(market, dict(key=market, name=market.replace("_", " ").title() if market != "h2h" else ("1X2" if len(prices) == 3 else "Moneyline"),
                                                      outcomes=o.outcomes if norm_name(o.home) == norm_name(ev["home"]) else list(reversed(o.outcomes)),
                                                      prices={}))
            if len(prices) == len(m["outcomes"]):
                m["prices"][book] = prices
            ev["live"] = ev["live"] or o.live
            ev["updated"] = max(ev["updated"], ts)
        for ev in events.values():
            ev["markets"] = list(ev["markets"].values())
        return list(events.values())

    async def run(self):
        if not self.scrapers:
            print("[scrapers] no enabled scrapers found in app/providers/scrapers/")
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as client:
            for s in self.scrapers:
                asyncio.create_task(self._loop(s, client))
            while True:
                await self.on_update(self.merged())
                await asyncio.sleep(self.push_every)
