
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth import access_for, optional_user, public_user
from ..db import ex, get_settings, now_ms, plan_row, q, q1
from ..engine import engine
from ..security import hash_password, sign_token, verify_password

router = APIRouter()


class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=6, max_length=128)
    name: str = Field(default="", max_length=80)


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/api/auth/register")
def register(body: RegisterIn):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email")
    if q1("SELECT id FROM users WHERE email=?", (email,)):
        raise HTTPException(400, "This email is already registered")
    uid = ex("INSERT INTO users (email,name,password_hash,created_at) VALUES (?,?,?,?)",
             (email, body.name.strip() or email.split("@")[0], hash_password(body.password), now_ms()))
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    return {"token": sign_token(uid), "user": public_user(u)}


@router.post("/api/auth/login")
def login(body: LoginIn):
    u = q1("SELECT * FROM users WHERE email=?", (body.email.strip().lower(),))
    if not u or not verify_password(body.password, u["password_hash"]):
        raise HTTPException(400, "Wrong email or password")
    if u["status"] == "blocked":
        raise HTTPException(403, "Account blocked — contact support")
    return {"token": sign_token(u["id"]), "user": public_user(u)}


@router.get("/api/public/config")
def config():
    s = get_settings()
    return {
        "siteName": s.get("site_name"), "support": s.get("support_telegram"), "announcement": s.get("announcement"),
        "wallets": {"TRC20": s.get("wallet_trc20"), "BEP20": s.get("wallet_bep20")},
        "plans": [plan_row(p) for p in q("SELECT * FROM plans WHERE active=1 ORDER BY sort, price")],
        "bookmakers": [{"key": b["key"], "name": b["name"], "color": b["color"], "region": b["region"],
                        "hasLink": bool(b["affiliate_url"] or b["site_url"])}
                       for b in q("SELECT * FROM bookmakers WHERE enabled=1 ORDER BY sort, id")],
        "stats": engine.stats,
    }


@router.get("/api/public/stats")
def stats():
    return engine.stats


@router.get("/go/{key}")
def go(key: str, u: str | None = None):
    """Affiliate redirect + click tracking. Every bookmaker button routes here."""
    b = q1("SELECT * FROM bookmakers WHERE key=?", (key,))
    if not b:
        raise HTTPException(404, "Unknown bookmaker")
    url = b["affiliate_url"] or b["site_url"]
    if not url:
        raise HTTPException(404, "No link set for this bookmaker yet")
    ex("UPDATE bookmakers SET clicks=clicks+1 WHERE id=?", (b["id"],))
    ex("INSERT INTO clicks (bookmaker_id,user_id,created_at) VALUES (?,?,?)", (b["id"], int(u) if (u or "").isdigit() else None, now_ms()))
    return RedirectResponse(url, status_code=302)


@router.get("/api/odds/events")
def odds_events(sport: str | None = None, search: str | None = None, user=Depends(optional_user)):
    """Odds comparison feed. Without an active plan only a 5-event preview is returned."""
    acc = access_for(user)
    evs = sorted(engine.events.values(), key=lambda e: (not e["live"], e["commence"]))
    if sport:
        evs = [e for e in evs if e["sport"] == sport]
    if search:
        s = search.lower()
        evs = [e for e in evs if s in e["home"].lower() or s in e["away"].lower() or s in e["league"].lower()]
    enabled = set(engine.enabled_keys())
    def clean(e):
        return {**e, "markets": [{**m, "prices": {b: p for b, p in m["prices"].items() if b in enabled}} for m in e["markets"]]}
    total = len(evs)
    if not acc["active"]:
        evs = evs[:5]
    return {"events": [clean(e) for e in evs], "total": total, "limited": not acc["active"],
            "sports": sorted({e["sport"] for e in engine.events.values()})}
