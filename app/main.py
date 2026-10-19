import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from .db import init_db  # noqa: E402

init_db()

from .engine import engine  # noqa: E402
from .routes import admin, public, user  # noqa: E402


def start_providers():
    mode = os.getenv("ODDS_PROVIDER", "demo").lower()
    tasks = []
    if mode in ("theoddsapi", "hybrid"):
        from .providers.theoddsapi import TheOddsApiProvider
        key = os.getenv("ODDS_API_KEY", "")
        if not key:
            print("[odds] ODDS_API_KEY missing — falling back to demo feed")
            mode = "demo"
        else:
            p = TheOddsApiProvider(
                api_key=key,
                sports=[s.strip() for s in os.getenv("ODDS_SPORTS", "soccer_epl,basketball_nba,tennis_atp_paris_masters").split(",") if s.strip()],
                regions=os.getenv("ODDS_REGIONS", "uk,eu,us,au"),
                markets=os.getenv("ODDS_MARKETS", "h2h,totals"),
                poll_seconds=int(os.getenv("ODDS_POLL_SECONDS", "60")),
                get_book_map=engine.api_book_map,
                on_update=engine.make_updater("theoddsapi"),
            )
            tasks.append(p.run())
    if mode in ("scrapers", "hybrid"):
        from .providers.scraper_provider import ScraperProvider
        sp = ScraperProvider(on_update=engine.make_updater("scrapers"))
        engine.scraper_status = sp.status
        tasks.append(sp.run())
    if mode == "demo":
        from .providers.demo import DemoProvider
        d = DemoProvider(get_bookmakers=engine.enabled_keys, on_update=engine.make_updater("demo"),
                         tick_seconds=float(os.getenv("DEMO_TICK_SECONDS", "2.5")))
        tasks.append(d.run())
    print(f"[odds] provider mode: {mode}")
    return [asyncio.create_task(t) for t in tasks]


@asynccontextmanager
async def lifespan(_app):
    tasks = start_providers()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ArbEdge API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(public.router)
app.include_router(user.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"ok": True, **engine.stats}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = None):
    await engine.connect(ws, token)
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from client
    except WebSocketDisconnect:
        pass
    finally:
        engine.disconnect(ws)


# ---------- serve built frontend (single deploy) ----------
DIST = Path(os.getenv("FRONTEND_DIST", Path(__file__).resolve().parent.parent.parent / "web" / "dist"))
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
