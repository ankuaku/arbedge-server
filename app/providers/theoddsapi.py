"""
The Odds API provider (https://the-odds-api.com) — real bookmaker odds, legal
and stable. Set ODDS_PROVIDER=theoddsapi + ODDS_API_KEY in .env.
Each request costs credits (markets x regions), so tune ODDS_SPORTS and
ODDS_POLL_SECONDS to your plan.

Our bookmakers are matched through the "API key" field in the admin panel
(e.g. our "1xbet" <-> api "onexbet").
"""
import asyncio
import time

import httpx

BASE = "https://api.the-odds-api.com/v4"
LABELS = {"soccer": "Soccer", "basketball": "Basketball", "tennis": "Tennis", "icehockey": "Ice Hockey",
          "americanfootball": "American Football", "baseball": "Baseball", "cricket": "Cricket", "mma": "MMA",
          "rugbyleague": "Rugby League", "rugbyunion": "Rugby Union", "boxing": "Boxing"}


def _fmt(pt):
    return f"+{pt}" if pt > 0 else f"{pt}"


class TheOddsApiProvider:
    name = "theoddsapi"

    def __init__(self, api_key, sports, regions, markets, poll_seconds, get_book_map, on_update):
        self.api_key = api_key
        self.sports = sports
        self.regions = regions
        self.markets = markets
        self.poll = poll_seconds
        self.get_book_map = get_book_map  # () -> {api_key: our_key}
        self.on_update = on_update
        self.cache = {}
        self.remaining = None

    def normalize(self, raw, sport_key):
        bmap = self.get_book_map()
        out = []
        for ev in raw:
            home, away = ev["home_team"], ev["away_team"]
            markets = {}
            for bm in ev.get("bookmakers", []):
                ours = bmap.get(bm["key"])
                if not ours:
                    continue
                for mk in bm.get("markets", []):
                    oc = mk["outcomes"]
                    if mk["key"] == "h2h":
                        names = [o["name"] for o in oc]
                        order = [home, "Draw", away] if "Draw" in names else [home, away]
                        m = markets.setdefault("h2h", dict(key="h2h", name="1X2" if len(order) == 3 else "Moneyline", outcomes=order, prices={}))
                        pr = [next((o["price"] for o in oc if o["name"] == n), None) for n in order]
                        if all(pr):
                            m["prices"][ours] = pr
                    elif mk["key"] == "totals":
                        by = {}
                        for o in oc:
                            by.setdefault(o.get("point"), {})[o["name"]] = o["price"]
                        for pt, d in by.items():
                            if "Over" in d and "Under" in d:
                                k = f"totals_{pt}"
                                m = markets.setdefault(k, dict(key=k, name=f"Total {pt}", outcomes=[f"Over {pt}", f"Under {pt}"], prices={}))
                                m["prices"][ours] = [d["Over"], d["Under"]]
                    elif mk["key"] == "spreads":
                        h = next((o for o in oc if o["name"] == home), None)
                        a = next((o for o in oc if o["name"] == away), None)
                        if not h or not a or h.get("point") != -a.get("point", 0):
                            continue
                        k = f"spreads_{h['point']}"
                        m = markets.setdefault(k, dict(key=k, name=f"Handicap {_fmt(h['point'])}",
                                                       outcomes=[f"{home} {_fmt(h['point'])}", f"{away} {_fmt(a['point'])}"], prices={}))
                        m["prices"][ours] = [h["price"], a["price"]]
            commence = int(ev["commence_time"]) * 1000
            out.append(dict(id=ev["id"], sportKey=sport_key, sport=LABELS.get(sport_key.split("_")[0], ev.get("sport_title")),
                            league=ev.get("sport_title", sport_key), home=home, away=away, commence=commence,
                            live=commence < time.time() * 1000, markets=list(markets.values()), updated=int(time.time() * 1000)))
        return out

    async def fetch_sport(self, client, sport):
        r = await client.get(f"{BASE}/sports/{sport}/odds", params=dict(
            apiKey=self.api_key, regions=self.regions, markets=self.markets, oddsFormat="decimal", dateFormat="unix"))
        self.remaining = r.headers.get("x-requests-remaining")
        r.raise_for_status()
        return self.normalize(r.json(), sport)

    async def run(self):
        async with httpx.AsyncClient(timeout=20) as client:
            while True:
                results = await asyncio.gather(*(self.fetch_sport(client, s) for s in self.sports), return_exceptions=True)
                for s, res in zip(self.sports, results):
                    if isinstance(res, Exception):
                        print(f"[odds-api] {s}: {res}")
                    else:
                        self.cache[s] = res
                await self.on_update([e for evs in self.cache.values() for e in evs])
                print(f"[odds-api] polled {len(self.sports)} sports, credits left: {self.remaining}")
                await asyncio.sleep(self.poll)
