import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import access_for, admin_user
from ..db import DEFAULT_SETTINGS, ex, get_settings, now_ms, plan_row, q, q1
from ..engine import engine
from ..security import hash_password

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_user)])
DAY = 86400000


def activate_plan(user_id: int, plan_id: int, days: int | None = None):
    u = q1("SELECT * FROM users WHERE id=?", (user_id,))
    p = q1("SELECT * FROM plans WHERE id=?", (plan_id,))
    if not u or not p:
        raise HTTPException(404, "User or plan not found")
    days = days if days is not None else p["duration_days"]
    base = u["plan_expires_at"] if (u["plan_id"] == plan_id and (u["plan_expires_at"] or 0) > now_ms()) else now_ms()
    ex("UPDATE users SET plan_id=?, plan_expires_at=?, trial_used=CASE WHEN ? THEN 1 ELSE trial_used END WHERE id=?",
       (plan_id, base + days * DAY, p["is_trial"], user_id))


@router.get("/stats")
def stats():
    now = now_ms()
    month = now - 30 * DAY
    return {
        "users": q1("SELECT COUNT(*) c FROM users WHERE role='user'")["c"],
        "activeSubs": q1("SELECT COUNT(*) c FROM users WHERE role='user' AND plan_expires_at>?", (now,))["c"],
        "newUsers7d": q1("SELECT COUNT(*) c FROM users WHERE created_at>?", (now - 7 * DAY,))["c"],
        "pendingPayments": q1("SELECT COUNT(*) c FROM payments WHERE status='pending'")["c"],
        "revenue30d": q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved' AND reviewed_at>?", (month,))["s"],
        "revenueAll": q1("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE status='approved'")["s"],
        "clicks30d": q1("SELECT COUNT(*) c FROM clicks WHERE created_at>?", (month,))["c"],
        "online": len(engine.clients),
        "feed": engine.stats,
        "topBookmakers": q("""SELECT b.name, COUNT(c.id) clicks FROM clicks c JOIN bookmakers b ON b.id=c.bookmaker_id
                             WHERE c.created_at>? GROUP BY b.id ORDER BY clicks DESC LIMIT 8""", (month,)),
        "revenueDaily": q("""SELECT strftime('%Y-%m-%d', reviewed_at/1000, 'unixepoch') d, SUM(amount) s FROM payments
                            WHERE status='approved' AND reviewed_at>? GROUP BY d ORDER BY d""", (month,)),
        "providers": list(engine.sources.keys()),
    }


# ---------- users / clients ----------
@router.get("/users")
def users(search: str = "", status: str = ""):
    sql = """SELECT u.id,u.email,u.name,u.role,u.status,u.plan_id,u.plan_expires_at,u.created_at,u.last_seen,u.notes,u.trial_used,
                    p.name plan_name,
                    (SELECT COALESCE(SUM(amount),0) FROM payments WHERE user_id=u.id AND status='approved') paid
             FROM users u LEFT JOIN plans p ON p.id=u.plan_id WHERE 1=1"""
    params = []
    if search:
        sql += " AND (u.email LIKE ? OR u.name LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if status == "active":
        sql += " AND u.plan_expires_at > ?"
        params.append(now_ms())
    elif status == "expired":
        sql += " AND (u.plan_expires_at IS NULL OR u.plan_expires_at <= ?)"
        params.append(now_ms())
    elif status == "blocked":
        sql += " AND u.status='blocked'"
    sql += " ORDER BY u.id DESC LIMIT 500"
    return q(sql, params)


class UserUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    role: str | None = None
    notes: str | None = None
    password: str | None = None
    plan_id: int | None = None
    days: int | None = None
    remove_plan: bool = False


@router.put("/users/{uid}")
def update_user(uid: int, body: UserUpdate):
    if not q1("SELECT id FROM users WHERE id=?", (uid,)):
        raise HTTPException(404, "User not found")
    if body.name is not None:
        ex("UPDATE users SET name=? WHERE id=?", (body.name, uid))
    if body.status in ("active", "blocked"):
        ex("UPDATE users SET status=? WHERE id=?", (body.status, uid))
    if body.role in ("user", "admin"):
        ex("UPDATE users SET role=? WHERE id=?", (body.role, uid))
    if body.notes is not None:
        ex("UPDATE users SET notes=? WHERE id=?", (body.notes, uid))
    if body.password:
        ex("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), uid))
    if body.remove_plan:
        ex("UPDATE users SET plan_id=NULL, plan_expires_at=NULL WHERE id=?", (uid,))
    elif body.plan_id:
        activate_plan(uid, body.plan_id, body.days)
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    return {**{k: u[k] for k in ("id", "email", "name", "status", "role", "plan_id", "plan_expires_at", "notes")}, "access": access_for(u)}


@router.delete("/users/{uid}")
def delete_user(uid: int, me=Depends(admin_user)):
    if uid == me["id"]:
        raise HTTPException(400, "You can't delete yourself")
    ex("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


# ---------- plans ----------
class PlanIn(BaseModel):
    slug: str
    name: str
    price: float
    duration_days: int
    max_profit: float = 100
    delay_seconds: int = 0
    live_access: bool = True
    is_trial: bool = False
    features: list[str] = []
    highlight: bool = False
    active: bool = True
    sort: int = 0


@router.get("/plans")
def plans():
    return [plan_row(p) for p in q("SELECT * FROM plans ORDER BY sort, price")]


def _plan_params(b: PlanIn):
    return (b.slug.strip().lower(), b.name, b.price, b.duration_days, b.max_profit, b.delay_seconds, int(b.live_access),
            int(b.is_trial), json.dumps(b.features), int(b.highlight), int(b.active), b.sort)


@router.post("/plans")
def create_plan(b: PlanIn):
    if q1("SELECT id FROM plans WHERE slug=?", (b.slug.lower(),)):
        raise HTTPException(400, "Slug already used")
    pid = ex("""INSERT INTO plans (slug,name,price,duration_days,max_profit,delay_seconds,live_access,is_trial,features,highlight,active,sort)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", _plan_params(b))
    return plan_row(q1("SELECT * FROM plans WHERE id=?", (pid,)))


@router.put("/plans/{pid}")
def update_plan(pid: int, b: PlanIn):
    ex("""UPDATE plans SET slug=?,name=?,price=?,duration_days=?,max_profit=?,delay_seconds=?,live_access=?,is_trial=?,
          features=?,highlight=?,active=?,sort=? WHERE id=?""", (*_plan_params(b), pid))
    return plan_row(q1("SELECT * FROM plans WHERE id=?", (pid,)))


@router.delete("/plans/{pid}")
def delete_plan(pid: int):
    if q1("SELECT id FROM payments WHERE plan_id=? LIMIT 1", (pid,)):
        ex("UPDATE plans SET active=0 WHERE id=?", (pid,))
        return {"ok": True, "deactivated": True}
    ex("DELETE FROM plans WHERE id=?", (pid,))
    return {"ok": True}


# ---------- bookmakers + affiliate links ----------
class BookmakerIn(BaseModel):
    key: str
    name: str
    color: str = "#334155"
    region: str = "global"
    affiliate_url: str = ""
    site_url: str = ""
    api_key: str = ""
    enabled: bool = True
    sort: int = 0


@router.get("/bookmakers")
def bookmakers():
    rows = q("SELECT * FROM bookmakers ORDER BY sort, id")
    covered = {}
    for ev in engine.events.values():
        for m in ev["markets"]:
            for b in m["prices"]:
                covered[b] = covered.get(b, 0) + 1
    month = now_ms() - 30 * DAY
    clicks = {r["bookmaker_id"]: r["c"] for r in q("SELECT bookmaker_id, COUNT(*) c FROM clicks WHERE created_at>? GROUP BY bookmaker_id", (month,))}
    for r in rows:
        r["enabled"] = bool(r["enabled"])
        r["lines"] = covered.get(r["key"], 0)
        r["clicks30d"] = clicks.get(r["id"], 0)
    return rows


def _bm_params(b: BookmakerIn):
    return (b.key.strip().lower(), b.name, b.color, b.region, b.affiliate_url.strip(), b.site_url.strip(), b.api_key.strip(), int(b.enabled), b.sort)


@router.post("/bookmakers")
def create_bookmaker(b: BookmakerIn):
    if q1("SELECT id FROM bookmakers WHERE key=?", (b.key.lower(),)):
        raise HTTPException(400, "Key already used")
    ex("INSERT INTO bookmakers (key,name,color,region,affiliate_url,site_url,api_key,enabled,sort) VALUES (?,?,?,?,?,?,?,?,?)", _bm_params(b))
    engine.reload_bookmakers()
    return {"ok": True}


@router.put("/bookmakers/{bid}")
def update_bookmaker(bid: int, b: BookmakerIn):
    ex("UPDATE bookmakers SET key=?,name=?,color=?,region=?,affiliate_url=?,site_url=?,api_key=?,enabled=?,sort=? WHERE id=?", (*_bm_params(b), bid))
    engine.reload_bookmakers()
    engine.recompute()
    return {"ok": True}


@router.delete("/bookmakers/{bid}")
def delete_bookmaker(bid: int):
    ex("DELETE FROM bookmakers WHERE id=?", (bid,))
    engine.reload_bookmakers()
    engine.recompute()
    return {"ok": True}


# ---------- payments ----------
@router.get("/payments")
def payments(status: str = ""):
    sql = """SELECT pay.*, u.email, u.name user_name, pl.name plan_name FROM payments pay
             JOIN users u ON u.id=pay.user_id JOIN plans pl ON pl.id=pay.plan_id"""
    params = []
    if status:
        sql += " WHERE pay.status=?"
        params.append(status)
    return q(sql + " ORDER BY pay.id DESC LIMIT 500", params)


class ReviewIn(BaseModel):
    action: str  # approve | reject
    note: str = ""


@router.post("/payments/{pid}")
def review_payment(pid: int, body: ReviewIn):
    p = q1("SELECT * FROM payments WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Payment not found")
    if p["status"] != "pending":
        raise HTTPException(400, f"Already {p['status']}")
    if body.action == "approve":
        activate_plan(p["user_id"], p["plan_id"])
        ex("UPDATE payments SET status='approved', admin_note=?, reviewed_at=? WHERE id=?", (body.note, now_ms(), pid))
    elif body.action == "reject":
        ex("UPDATE payments SET status='rejected', admin_note=?, reviewed_at=? WHERE id=?", (body.note, now_ms(), pid))
    else:
        raise HTTPException(400, "action must be approve or reject")
    return {"ok": True}


# ---------- site settings ----------
@router.get("/settings")
def settings():
    return get_settings()


@router.put("/settings")
def save_settings(body: dict):
    if "theme" in body and body["theme"] not in ("bookie", "royal", "emerald", "crimson", "midnight", "paper"):
        raise HTTPException(400, "Unknown theme")
    for k, v in body.items():
        if k in DEFAULT_SETTINGS:
            ex("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return get_settings()
