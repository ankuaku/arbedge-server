import json

from fastapi import Depends, Header, HTTPException

from .db import ex, now_ms, q1
from .security import decode_token


def user_from_token(token: str | None) -> dict | None:
    uid = decode_token(token)
    if not uid:
        return None
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u or u["status"] == "blocked":
        return None
    return u


def access_for(user: dict | None) -> dict:
    none = {"active": False, "plan": None, "maxProfit": 0, "delay": 0, "live": False, "expiresAt": None}
    if not user:
        return none
    if user["role"] == "admin":
        return {"active": True, "plan": {"name": "Admin", "slug": "admin"}, "maxProfit": 100, "delay": 0, "live": True, "expiresAt": None}
    if not user["plan_id"] or not user["plan_expires_at"] or user["plan_expires_at"] < now_ms():
        return none
    p = q1("SELECT * FROM plans WHERE id=?", (user["plan_id"],))
    if not p:
        return none
    return {
        "active": True,
        "plan": {"id": p["id"], "name": p["name"], "slug": p["slug"]},
        "maxProfit": p["max_profit"],
        "delay": p["delay_seconds"],
        "live": bool(p["live_access"]),
        "expiresAt": user["plan_expires_at"],
    }


def public_user(u: dict) -> dict:
    try:
        settings = json.loads(u.get("settings") or "{}")
    except Exception:
        settings = {}
    return {
        "id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"], "status": u["status"],
        "settings": settings, "trialUsed": bool(u["trial_used"]), "createdAt": u["created_at"],
        "access": access_for(u),
    }


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return None


def current_user(authorization: str | None = Header(None)) -> dict:
    u = user_from_token(_bearer(authorization))
    if not u:
        raise HTTPException(401, "Please log in")
    ex("UPDATE users SET last_seen=? WHERE id=?", (now_ms(), u["id"]))
    return u


def optional_user(authorization: str | None = Header(None)) -> dict | None:
    return user_from_token(_bearer(authorization))


def admin_user(u: dict = Depends(current_user)) -> dict:
    if u["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return u
