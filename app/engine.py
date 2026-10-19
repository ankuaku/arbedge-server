"""
Arbitrage engine: merges feeds from all providers, finds sure-bets across the
enabled bookmakers, tracks how long each arb has existed and pushes per-user
(plan-gated) snapshots to every open WebSocket in realtime.
"""
import asyncio
import json
import time

from fastapi import WebSocket

from .auth import access_for, user_from_token
from .db import q
from .providers.scrapers.matcher import event_key

MAX_SANE_PROFIT = 30.0  # anything above is almost always a data error


def _now():
    return int(time.time() * 1000)


class Engine:
    def __init__(self):
        self.sources: dict[str, list[dict]] = {}
        self.events: dict[str, dict] = {}
        self.arbs: list[dict] = []
        self.first_seen: dict[str, int] = {}
        self.bookmakers: dict[str, dict] = {}
        self.clients: dict[WebSocket, dict] = {}
        self.last_update = 0
        self.stats = {"events": 0, "arbs": 0, "bookmakers": 0, "best": 0, "updated": 0}
        self.reload_bookmakers()

    # ---------- bookmakers ----------
    def reload_bookmakers(self):
        rows = q("SELECT * FROM bookmakers ORDER BY sort, id")
        self.bookmakers = {r["key"]: r for r in rows}
        for ws, c in list(self.clients.items()):
            c["user"] = user_from_token(c["token"])

    def enabled_keys(self) -> list[str]:
        return [k for k, b in self.bookmakers.items() if b["enabled"]]

    def api_book_map(self) -> dict:
        return {b["api_key"]: k for k, b in self.bookmakers.items() if b["api_key"]}

    # ---------- feed ----------
    def make_updater(self, source: str):
        async def _upd(events: list[dict]):
            self.sources[source] = events
            self.recompute()
            await self.broadcast()
        return _upd

    def recompute(self):
        merged: dict[str, dict] = {}
        multi = len(self.sources) > 1
        for evs in self.sources.values():
            for ev in evs:
                key = event_key(ev["sport"], ev["home"], ev["away"], ev["commence"]) if multi else ev["id"]
                if key not in merged:
                    merged[key] = {**ev, "id": ev["id"], "markets": {m["key"]: {**m, "prices": dict(m["prices"])} for m in ev["markets"]}}
                else:
                    tgt = merged[key]["markets"]
                    for m in ev["markets"]:
                        if m["key"] in tgt:
                            tgt[m["key"]]["prices"].update(m["prices"])
                        else:
                            tgt[m["key"]] = {**m, "prices": dict(m["prices"])}
        for ev in merged.values():
            ev["markets"] = list(ev["markets"].values())
        self.events = {ev["id"]: ev for ev in merged.values()}

        enabled = set(self.enabled_keys())
        arbs, seen_now = [], set()
        now = _now()
        for ev in self.events.values():
            for m in ev["markets"]:
                n = len(m["outcomes"])
                best = [None] * n
                for book, prices in m["prices"].items():
                    if book not in enabled or len(prices) != n:
                        continue
                    for i, p in enumerate(prices):
                        if p and (best[i] is None or p > best[i][1]):
                            best[i] = (book, p)
                if any(b is None for b in best):
                    continue
                inv = sum(1 / b[1] for b in best)
                if inv >= 1:
                    continue
                profit = (1 / inv - 1) * 100
                if profit > MAX_SANE_PROFIT:
                    continue
                aid = f"{ev['id']}|{m['key']}|" + "|".join(f"{b[0]}" for b in best)
                seen_now.add(aid)
                fs = self.first_seen.setdefault(aid, now)
                arbs.append({
                    "id": aid, "eventId": ev["id"], "sport": ev["sport"], "league": ev["league"],
                    "home": ev["home"], "away": ev["away"], "commence": ev["commence"], "live": ev["live"],
                    "market": m["key"], "marketName": m["name"], "profit": round(profit, 2),
                    "legs": [{"outcome": m["outcomes"][i], "bookmaker": b[0], "odds": b[1],
                              "stakePct": round((1 / b[1]) / inv * 100, 2)} for i, b in enumerate(best)],
                    "firstSeen": fs, "updated": now,
                })
        for k in list(self.first_seen):
            if k not in seen_now:
                del self.first_seen[k]
        arbs.sort(key=lambda a: -a["profit"])
        self.arbs = arbs
        self.last_update = now
        self.stats = {
            "events": len(self.events), "arbs": len(arbs),
            "bookmakers": len(enabled), "best": arbs[0]["profit"] if arbs else 0, "updated": now,
            "live": sum(1 for a in arbs if a["live"]),
        }

    # ---------- gating ----------
    def view_for(self, user: dict | None) -> list[dict]:
        acc = access_for(user)
        now = _now()
        out = []
        if not acc["active"]:
            for a in self.arbs[:6]:
                out.append(self._lock(a, "subscribe", hide_event=True))
            return out
        for a in self.arbs:
            if a["live"] and not acc["live"]:
                out.append(self._lock(a, "live"))
            elif a["profit"] > acc["maxProfit"]:
                out.append(self._lock(a, "profit"))
            elif acc["delay"] and now - a["firstSeen"] < acc["delay"] * 1000:
                continue
            else:
                out.append(a)
        return out

    @staticmethod
    def _lock(a, reason, hide_event=False):
        return {
            **a, "locked": reason,
            "home": "•••••" if hide_event else a["home"], "away": "•••••" if hide_event else a["away"],
            "id": f"locked:{hash(a['id'])}", "eventId": None,
            "legs": [{"outcome": "•••" if hide_event else l["outcome"], "bookmaker": None, "odds": None, "stakePct": None} for l in a["legs"]],
        }

    # ---------- websockets ----------
    async def connect(self, ws: WebSocket, token: str | None):
        await ws.accept()
        self.clients[ws] = {"token": token, "user": user_from_token(token)}
        await self.send(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.pop(ws, None)

    async def send(self, ws):
        c = self.clients.get(ws)
        if c is None:
            return
        if c["token"] and _now() - c.get("at", 0) > 15000:  # pick up plan changes
            c["user"], c["at"] = user_from_token(c["token"]), _now()
        try:
            await ws.send_text(json.dumps({"type": "arbs", "arbs": self.view_for(c["user"]), "stats": self.stats}))
        except Exception:
            self.disconnect(ws)

    async def broadcast(self):
        if self.clients:
            await asyncio.gather(*(self.send(ws) for ws in list(self.clients)), return_exceptions=True)


engine = Engine()
