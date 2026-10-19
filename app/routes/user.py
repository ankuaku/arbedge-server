import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user, public_user
from ..db import ex, now_ms, q, q1
from ..security import hash_password, verify_password

router = APIRouter(prefix="/api")

ALLOWED_SETTINGS = {"bookmakers", "sports", "minProfit", "maxProfit", "stake", "rounding", "currency",
                    "sound", "showLive", "showPrematch", "markets", "hideStartedMinutes",
                    "tgMinProfit", "tgLive", "tgPrematch", "tgPaused"}


@router.get("/me")
def me(u=Depends(current_user)):
    return public_user(u)


@router.put("/me/settings")
def save_settings(body: dict, u=Depends(current_user)):
    cur = json.loads(u["settings"] or "{}")
    for k, v in body.items():
        if k in ALLOWED_SETTINGS:
            cur[k] = v
    ex("UPDATE users SET settings=? WHERE id=?", (json.dumps(cur), u["id"]))
    return public_user(q1("SELECT * FROM users WHERE id=?", (u["id"],)))


class ProfileIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    current_password: str | None = None
    new_password: str | None = Field(default=None, min_length=6, max_length=128)


@router.put("/me/profile")
def save_profile(body: ProfileIn, u=Depends(current_user)):
    if body.name is not None:
        ex("UPDATE users SET name=? WHERE id=?", (body.name.strip(), u["id"]))
    if body.new_password:
        if not body.current_password or not verify_password(body.current_password, u["password_hash"]):
            raise HTTPException(400, "Current password is wrong")
        ex("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new_password), u["id"]))
    return public_user(q1("SELECT * FROM users WHERE id=?", (u["id"],)))


# ---------- payments (USDT) ----------
class PaymentIn(BaseModel):
    plan_id: int
    network: str
    tx_hash: str = Field(min_length=10, max_length=120)


TX_RE = {"TRC20": re.compile(r"^[0-9a-fA-F]{64}$"), "BEP20": re.compile(r"^0x[0-9a-fA-F]{64}$")}


@router.post("/payments")
def create_payment(body: PaymentIn, u=Depends(current_user)):
    p = q1("SELECT * FROM plans WHERE id=? AND active=1", (body.plan_id,))
    if not p:
        raise HTTPException(400, "Plan not found")
    net = body.network.upper()
    if net not in TX_RE:
        raise HTTPException(400, "Network must be TRC20 or BEP20")
    tx = body.tx_hash.strip()
    if not TX_RE[net].match(tx):
        raise HTTPException(400, f"That doesn't look like a {net} transaction hash")
    if p["is_trial"] and u["trial_used"]:
        raise HTTPException(400, "Trial can be bought only once — choose Pro or Elite")
    if q1("SELECT id FROM payments WHERE tx_hash=?", (tx,)):
        raise HTTPException(400, "This transaction was already submitted")
    pid = ex("INSERT INTO payments (user_id,plan_id,amount,network,tx_hash,created_at) VALUES (?,?,?,?,?,?)",
             (u["id"], p["id"], p["price"], net, tx, now_ms()))
    return q1("SELECT * FROM payments WHERE id=?", (pid,))


@router.get("/payments")
def my_payments(u=Depends(current_user)):
    return q("""SELECT pay.*, pl.name plan_name FROM payments pay JOIN plans pl ON pl.id=pay.plan_id
                WHERE user_id=? ORDER BY pay.id DESC""", (u["id"],))


# ---------- bet tracker ----------
class BetIn(BaseModel):
    event: str = Field(max_length=200)
    market: str | None = Field(default="", max_length=80)
    legs: list[dict] = []
    stake: float = Field(ge=0)
    expected_profit: float = 0


class BetUpdate(BaseModel):
    status: str | None = None
    result_profit: float | None = None


@router.get("/bets")
def list_bets(u=Depends(current_user)):
    rows = q("SELECT * FROM bets WHERE user_id=? ORDER BY id DESC LIMIT 500", (u["id"],))
    for r in rows:
        r["legs"] = json.loads(r["legs"] or "[]")
    settled = [r for r in rows if r["status"] == "settled"]
    return {
        "bets": rows,
        "summary": {
            "count": len(rows), "open": sum(1 for r in rows if r["status"] == "open"),
            "turnover": round(sum(r["stake"] for r in rows), 2),
            "profit": round(sum((r["result_profit"] if r["result_profit"] is not None else r["expected_profit"]) for r in settled), 2),
            "expected": round(sum(r["expected_profit"] for r in rows if r["status"] == "open"), 2),
        },
    }


@router.post("/bets")
def add_bet(body: BetIn, u=Depends(current_user)):
    bid = ex("INSERT INTO bets (user_id,event,market,legs,stake,expected_profit,created_at) VALUES (?,?,?,?,?,?,?)",
             (u["id"], body.event, body.market, json.dumps(body.legs[:6]), body.stake, body.expected_profit, now_ms()))
    return {"id": bid}


@router.put("/bets/{bid}")
def update_bet(bid: int, body: BetUpdate, u=Depends(current_user)):
    b = q1("SELECT * FROM bets WHERE id=? AND user_id=?", (bid, u["id"]))
    if not b:
        raise HTTPException(404, "Bet not found")
    if body.status in ("open", "settled", "void"):
        ex("UPDATE bets SET status=? WHERE id=?", (body.status, bid))
    if body.result_profit is not None:
        ex("UPDATE bets SET result_profit=? WHERE id=?", (body.result_profit, bid))
    return {"ok": True}


@router.delete("/bets/{bid}")
def delete_bet(bid: int, u=Depends(current_user)):
    ex("DELETE FROM bets WHERE id=? AND user_id=?", (bid, u["id"]))
    return {"ok": True}


# ---------- telegram ----------
@router.post("/me/telegram/link")
def telegram_link(u=Depends(current_user)):
    from ..telegram import bot
    if not bot.enabled or not bot.username:
        raise HTTPException(503, "Telegram alerts aren’t switched on yet. Please contact support.")
    return {"url": bot.link_url(u["id"]), "bot": bot.username}


@router.delete("/me/telegram")
def telegram_unlink(u=Depends(current_user)):
    ex("UPDATE users SET tg_chat_id=NULL, tg_username=NULL WHERE id=?", (u["id"],))
    from ..telegram import bot
    bot.users_at = 0
    return public_user(q1("SELECT * FROM users WHERE id=?", (u["id"],)))


@router.post("/me/telegram/test")
async def telegram_test(u=Depends(current_user)):
    from ..telegram import bot
    if not u.get("tg_chat_id"):
        raise HTTPException(400, "Connect Telegram first")
    if not await bot.test(u):
        raise HTTPException(502, "Telegram didn’t accept the message. Make sure you haven’t blocked the bot.")
    return {"ok": True}
