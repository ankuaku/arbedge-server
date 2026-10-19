import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .security import hash_password

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "arbedge.db"))
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA foreign_keys=ON")


def now_ms() -> int:
    return int(time.time() * 1000)


def q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def q1(sql: str, params=()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  status TEXT NOT NULL DEFAULT 'active',
  plan_id INTEGER,
  plan_expires_at INTEGER,
  settings TEXT NOT NULL DEFAULT '{}',
  notes TEXT DEFAULT '',
  trial_used INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_seen INTEGER
);
CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  price REAL NOT NULL,
  duration_days INTEGER NOT NULL,
  max_profit REAL NOT NULL DEFAULT 100,
  delay_seconds INTEGER NOT NULL DEFAULT 0,
  live_access INTEGER NOT NULL DEFAULT 1,
  is_trial INTEGER NOT NULL DEFAULT 0,
  features TEXT NOT NULL DEFAULT '[]',
  highlight INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bookmakers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#334155',
  region TEXT DEFAULT 'global',
  affiliate_url TEXT DEFAULT '',
  site_url TEXT DEFAULT '',
  api_key TEXT DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  clicks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  plan_id INTEGER NOT NULL REFERENCES plans(id),
  amount REAL NOT NULL,
  network TEXT NOT NULL,
  tx_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  admin_note TEXT DEFAULT '',
  created_at INTEGER NOT NULL,
  reviewed_at INTEGER
);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event TEXT NOT NULL,
  market TEXT,
  legs TEXT NOT NULL DEFAULT '[]',
  stake REAL NOT NULL DEFAULT 0,
  expected_profit REAL NOT NULL DEFAULT 0,
  result_profit REAL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clicks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bookmaker_id INTEGER NOT NULL,
  user_id INTEGER,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

# key, name, brand colour, region, The-Odds-API key ('' = needs scraper / other feed)
BOOKMAKERS = [
    ("pinnacle", "Pinnacle", "#0f3b6f", "global", "pinnacle"),
    ("bet365", "Bet365", "#126e51", "global", ""),
    ("1xbet", "1xBet", "#1a5aa6", "global", "onexbet"),
    ("betway", "Betway", "#1b1b1b", "global", "betway"),
    ("williamhill", "William Hill", "#00143c", "uk", "williamhill"),
    ("unibet", "Unibet", "#147b45", "eu", "unibet_eu"),
    ("betfair", "Betfair Exchange", "#d99a00", "uk", "betfair_ex_eu"),
    ("bwin", "Bwin", "#2e2e2e", "eu", ""),
    ("888sport", "888sport", "#f26522", "uk", "sport888"),
    ("paddypower", "Paddy Power", "#004833", "uk", "paddypower"),
    ("betfred", "Betfred", "#003a8c", "uk", "betfred_uk"),
    ("ladbrokes", "Ladbrokes", "#e4002b", "uk", "ladbrokes_uk"),
    ("coral", "Coral", "#0b2a6f", "uk", "coral"),
    ("skybet", "Sky Bet", "#00205b", "uk", "skybet"),
    ("marathonbet", "Marathonbet", "#0a3d7a", "global", "marathonbet"),
    ("stake", "Stake", "#1a2c38", "crypto", ""),
    ("bcgame", "BC.Game", "#2a2d33", "crypto", ""),
    ("1win", "1win", "#1c3fa8", "global", ""),
    ("parimatch", "Parimatch", "#b8bd00", "global", ""),
    ("melbet", "Melbet", "#3a3a3a", "global", ""),
    ("22bet", "22Bet", "#0f4c3a", "global", ""),
    ("betsson", "Betsson", "#ff6600", "eu", "betsson"),
    ("betvictor", "BetVictor", "#0a2d4d", "uk", "betvictor"),
    ("draftkings", "DraftKings", "#3f9f2a", "us", "draftkings"),
    ("fanduel", "FanDuel", "#1493ff", "us", "fanduel"),
    ("betmgm", "BetMGM", "#a88b4f", "us", "betmgm"),
    ("caesars", "Caesars", "#0b3d2e", "us", "williamhill_us"),
    ("betonline", "BetOnline", "#c8102e", "us", "betonlineag"),
    ("bovada", "Bovada", "#cc0000", "us", "bovada"),
    ("dafabet", "Dafabet", "#c41e3a", "asia", ""),
    ("mostbet", "Mostbet", "#0050b3", "global", ""),
    ("matchbook", "Matchbook", "#ec1c24", "uk", "matchbook"),
]

PLANS = [
    dict(slug="trial", name="Trial", price=10, duration_days=3, max_profit=3, delay_seconds=30, live_access=0, is_trial=1, highlight=0, sort=1,
         features=["3 days scanner access", "Pre-match arbs up to 3% profit", "30s delayed feed", "Odds comparison — all bookmakers", "Stake calculator & bet tracker"]),
    dict(slug="pro", name="Pro", price=100, duration_days=30, max_profit=100, delay_seconds=0, live_access=1, is_trial=0, highlight=1, sort=2,
         features=["Realtime arbs — zero delay", "Pre-match + live arbs", "Unlimited profit %", "30+ bookmakers", "Sound alerts", "Bet tracker with P/L"]),
    dict(slug="elite", name="Elite", price=250, duration_days=90, max_profit=100, delay_seconds=0, live_access=1, is_trial=0, highlight=0, sort=3,
         features=["Everything in Pro", "90 days access (save $50)", "Priority feed", "Custom bookmaker set", "Priority support"]),
    dict(slug="desk", name="Personal Desk", price=1000, duration_days=180, max_profit=100, delay_seconds=0, live_access=1, is_trial=0, highlight=0, sort=4,
         features=["Everything in Elite, for 6 months", "1-on-1 onboarding call to set you up", "Your own account manager on Telegram",
                   "Help choosing and funding the right bookmakers", "Bankroll and stake plan built around your budget",
                   "Weekly check-in on your results", "Guidance on keeping bookmaker accounts healthy", "Replies within 1 hour, 7 days a week"]),
]

DEFAULT_SETTINGS = {
    "site_name": "ArbEdge",
    "support_telegram": "@arbedge_support",
    "wallet_trc20": "",
    "wallet_bep20": "",
    "announcement": "",
}


def init_db():
    with _lock:
        _conn.executescript(SCHEMA)
        _conn.commit()
    if not q1("SELECT id FROM bookmakers LIMIT 1"):
        for i, b in enumerate(BOOKMAKERS):
            ex("INSERT INTO bookmakers (key,name,color,region,api_key,sort) VALUES (?,?,?,?,?,?)", (*b, i))
    existing = {r["slug"] for r in q("SELECT slug FROM plans")}
    for p in PLANS:
        if p["slug"] not in existing:
            ex("""INSERT INTO plans (slug,name,price,duration_days,max_profit,delay_seconds,live_access,is_trial,features,highlight,sort)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (p["slug"], p["name"], p["price"], p["duration_days"], p["max_profit"], p["delay_seconds"],
                p["live_access"], p["is_trial"], json.dumps(p["features"]), p["highlight"], p["sort"]))
    admin_email = os.getenv("ADMIN_EMAIL", "admin@arbedge.io").lower()
    if not q1("SELECT id FROM users WHERE email=?", (admin_email,)):
        ex("INSERT INTO users (email,name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
           (admin_email, "Admin", hash_password(os.getenv("ADMIN_PASSWORD", "admin123")), "admin", now_ms()))
    for k, v in DEFAULT_SETTINGS.items():
        ex("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))


def get_settings() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key,value FROM settings")}


def plan_row(p: dict) -> dict:
    p = dict(p)
    p["features"] = json.loads(p.get("features") or "[]")
    for k in ("live_access", "is_trial", "highlight", "active"):
        p[k] = bool(p[k])
    return p
