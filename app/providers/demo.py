"""
Demo odds provider — simulates a realistic multi-bookmaker feed so the whole
platform runs end-to-end without a paid data key or scrapers. Each bookmaker
prices off a shared "true" probability with its own margin + noise, and now
and then one book lags or boosts a line — exactly what creates real arbs.
"""
import asyncio
import random
import time

SPORTS = [
    dict(key="soccer_epl", sport="Soccer", league="Premier League", three=True, total=2.5,
         teams=["Arsenal", "Chelsea", "Liverpool", "Man City", "Man United", "Tottenham", "Newcastle", "Aston Villa", "Brighton", "West Ham", "Brentford", "Fulham", "Everton", "Wolves"]),
    dict(key="soccer_spain_la_liga", sport="Soccer", league="La Liga", three=True, total=2.5,
         teams=["Real Madrid", "Barcelona", "Atletico Madrid", "Sevilla", "Real Sociedad", "Villarreal", "Real Betis", "Valencia", "Athletic Club", "Girona"]),
    dict(key="soccer_italy_serie_a", sport="Soccer", league="Serie A", three=True, total=2.5,
         teams=["Inter", "AC Milan", "Juventus", "Napoli", "Roma", "Lazio", "Atalanta", "Fiorentina", "Bologna", "Torino"]),
    dict(key="soccer_uefa_champs_league", sport="Soccer", league="Champions League", three=True, total=2.5,
         teams=["Bayern Munich", "PSG", "Benfica", "Porto", "Ajax", "Dortmund", "Celtic", "Leipzig", "Feyenoord", "Club Brugge"]),
    dict(key="basketball_nba", sport="Basketball", league="NBA", three=False, total=224.5,
         teams=["Lakers", "Celtics", "Warriors", "Bucks", "Nuggets", "Suns", "Heat", "76ers", "Knicks", "Mavericks", "Thunder", "Cavaliers"]),
    dict(key="tennis_atp", sport="Tennis", league="ATP Tour", three=False, total=22.5,
         teams=["Sinner", "Alcaraz", "Djokovic", "Zverev", "Medvedev", "Rune", "Fritz", "Ruud", "De Minaur", "Tsitsipas", "Rublev", "Draper"]),
    dict(key="cricket_t20", sport="Cricket", league="T20 League", three=False, total=330.5,
         teams=["Mumbai", "Chennai", "Kolkata", "Bangalore", "Delhi", "Rajasthan", "Punjab", "Hyderabad", "Lucknow", "Gujarat"]),
    dict(key="icehockey_nhl", sport="Ice Hockey", league="NHL", three=False, total=6.5,
         teams=["Rangers", "Bruins", "Oilers", "Panthers", "Avalanche", "Maple Leafs", "Stars", "Golden Knights", "Canucks", "Jets"]),
    dict(key="mma_ufc", sport="MMA", league="UFC", three=False, total=None,
         teams=["Makhachev", "Pereira", "Topuria", "Du Plessis", "Aspinall", "Edwards", "Oliveira", "Holloway", "Adesanya", "Strickland"]),
    dict(key="esports_cs2", sport="Esports", league="CS2 Major", three=False, total=None,
         teams=["NAVI", "FaZe", "Vitality", "G2", "Spirit", "MOUZ", "Liquid", "Heroic", "Astralis", "Falcons"]),
]

SHARP = {"pinnacle", "betfair", "matchbook"}


def _norm(ps):
    s = sum(ps)
    return [p / s for p in ps]


def _r2(x):
    return max(1.01, round(x, 2))


def _now():
    return int(time.time() * 1000)


class DemoProvider:
    name = "demo"

    def __init__(self, get_bookmakers, on_update, tick_seconds=2.5, size=90):
        self.get_bookmakers = get_bookmakers
        self.on_update = on_update
        self.tick = tick_seconds
        self.size = size
        self.events = {}
        self.margins = {}
        self.anomalies = []
        self.counter = 0

    def margin(self, b):
        if b not in self.margins:
            self.margins[b] = random.uniform(0.015, 0.025) if b in SHARP else random.uniform(0.035, 0.075)
        return self.margins[b]

    def make_event(self):
        i = self.counter
        self.counter += 1
        s = SPORTS[i % len(SPORTS)]
        home, away = random.sample(s["teams"], 2)
        live = random.random() < 0.22
        commence = _now() - int(random.uniform(5, 80) * 60000) if live else _now() + int(random.uniform(0.3, 72) * 3600000)
        st = random.uniform(-1, 1)
        if s["three"]:
            ph, pd = 0.45 + st * 0.3, random.uniform(0.22, 0.3)
            h2h = _norm([max(0.08, ph), pd, max(0.08, 1 - ph - pd)])
            markets = [dict(key="h2h", name="1X2", outcomes=[home, "Draw", away], probs=h2h)]
        else:
            ph = 0.5 + st * 0.35
            markets = [dict(key="h2h", name="Moneyline", outcomes=[home, away], probs=[ph, 1 - ph])]
        if s["total"]:
            po = random.uniform(0.4, 0.6)
            t = s["total"]
            markets.append(dict(key=f"totals_{t}", name=f"Total {t}", outcomes=[f"Over {t}", f"Under {t}"], probs=[po, 1 - po]))
        if s["three"]:
            pb = random.uniform(0.42, 0.62)
            markets.append(dict(key="btts", name="Both Teams To Score", outcomes=["Yes", "No"], probs=[pb, 1 - pb]))
        cov = random.uniform(0.55, 0.9)
        books = [b for b in self.get_bookmakers() if random.random() < cov]
        noise = {b: [[random.uniform(-0.012, 0.012) for _ in m["outcomes"]] for m in markets] for b in books}
        eid = f"demo_{s['key']}_{i}"
        return dict(id=eid, sportKey=s["key"], sport=s["sport"], league=s["league"], home=home, away=away,
                    commence=commence, live=live, markets=markets, books=books, noise=noise)

    def price(self, ev):
        out = []
        an_map = {(a["e"], a["m"], a["b"], a["i"]): a["boost"] for a in self.anomalies}
        for mi, m in enumerate(ev["markets"]):
            prices = {}
            for b in ev["books"]:
                mg = self.margin(b)
                row = []
                for oi, p in enumerate(m["probs"]):
                    pp = p * (1 + mg) * (1 + ev["noise"][b][mi][oi])
                    boost = an_map.get((ev["id"], m["key"], b, oi))
                    if boost:
                        pp /= boost
                    row.append(_r2(1 / pp))
                prices[b] = row
            out.append(dict(key=m["key"], name=m["name"], outcomes=m["outcomes"], prices=prices))
        return dict(id=ev["id"], sportKey=ev["sportKey"], sport=ev["sport"], league=ev["league"], home=ev["home"],
                    away=ev["away"], commence=ev["commence"], live=ev["live"], markets=out, updated=_now())

    def step(self):
        for ev in self.events.values():
            vol = 0.02 if ev["live"] else 0.004
            for m in ev["markets"]:
                m["probs"] = _norm([max(0.03, p * (1 + random.uniform(-vol, vol))) for p in m["probs"]])
            for b in ev["books"]:
                if random.random() < (0.35 if ev["live"] else 0.1):
                    ev["noise"][b] = [[max(-0.015, min(0.015, n + random.uniform(-0.004, 0.004))) for n in row] for row in ev["noise"][b]]
        # stale / boosted lines
        for a in self.anomalies:
            a["ttl"] -= 1
        self.anomalies = [a for a in self.anomalies if a["ttl"] > 0 and a["e"] in self.events]
        evs = [e for e in self.events.values() if e["books"]]
        while len(self.anomalies) < 14 and evs:
            ev = random.choice(evs)
            m = random.choice(ev["markets"])
            r = random.random()
            boost = random.uniform(1.06, 1.09) if r < 0.55 else random.uniform(1.09, 1.13) if r < 0.88 else random.uniform(1.13, 1.2)
            self.anomalies.append(dict(e=ev["id"], m=m["key"], b=random.choice(ev["books"]),
                                       i=random.randrange(len(m["outcomes"])), boost=boost, ttl=random.randint(6, 40)))
        # lifecycle
        for eid, ev in list(self.events.items()):
            if ev["live"] and _now() - ev["commence"] > 110 * 60000:
                del self.events[eid]
            elif not ev["live"] and ev["commence"] < _now():
                ev["live"] = True
        if random.random() < 0.05 and self.events:
            del self.events[next(iter(self.events))]
        while len(self.events) < self.size:
            e = self.make_event()
            self.events[e["id"]] = e

    async def run(self):
        while True:
            try:
                self.step()
                await self.on_update([self.price(e) for e in self.events.values()])
            except Exception as e:  # never kill the feed
                print("[demo] error", e)
            await asyncio.sleep(self.tick)
