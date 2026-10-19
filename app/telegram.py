"""
Telegram bot: users link their ArbEdge account, then get arb alerts that match
their own filters (bookmakers, sports, min profit, live / pre-match) and plan.

Runs inside the API process with long polling, so no webhook or extra service
is needed. Set TELEGRAM_BOT_TOKEN to turn it on.

Optional:
  TELEGRAM_CHANNEL_ID   public channel for teaser posts (bot must be admin there)
  TELEGRAM_CHANNEL_EVERY_MIN  minutes between teaser posts (default 30)
  SITE_URL              frontend URL used in buttons
  PUBLIC_API_URL        this API's public URL, used for /go affiliate buttons
"""
import asyncio
import html
import json
import os
import secrets
import time

import httpx

from .auth import access_for
from .db import ex, get_settings, q, q1

API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
MAX_PER_WINDOW = 12          # alerts per user per window
WINDOW_MS = 5 * 60 * 1000
COMMANDS = [
    ("status", "Your plan and alert filters"),
    ("min", "Minimum profit %, e.g. /min 2"),
    ("pause", "Stop alerts for now"),
    ("resume", "Start alerts again"),
    ("live", "In-play arbs on or off, e.g. /live off"),
    ("prematch", "Pre-match arbs on or off"),
    ("stop", "Disconnect this chat from your account"),
    ("help", "How the bot works"),
]


def _now():
    return int(time.time() * 1000)


def _esc(s):
    return html.escape(str(s), quote=False)


def _settings(u):
    try:
        return json.loads(u.get("settings") or "{}")
    except Exception:
        return {}


def _starts(ms):
    d = ms - _now()
    if d <= 0:
        return "🔴 In play"
    m = d // 60000
    if m < 60:
        return f"starts in {m}m"
    h = m // 60
    return f"starts in {h}h {m % 60}m" if h < 24 else time.strftime("%d %b %H:%M UTC", time.gmtime(ms / 1000))


class TelegramBot:
    def __init__(self, engine):
        self.engine = engine
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.enabled = bool(self.token)
        self.username = None
        self.offset = 0
        self.sent: set[tuple[int, str]] = set()
        self.recent: dict[int, list[int]] = {}
        self.users: list[dict] = []
        self.users_at = 0
        self.channel = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
        self.channel_every = max(5, int(os.getenv("TELEGRAM_CHANNEL_EVERY_MIN", "30"))) * 60000
        self.channel_last = 0
        self.site = os.getenv("SITE_URL", "").rstrip("/")
        self.api_public = os.getenv("PUBLIC_API_URL", "").rstrip("/")
        self.client: httpx.AsyncClient | None = None
        self.error = None

    # ---------- telegram api ----------
    async def call(self, method, **params):
        if not self.client:
            return None
        try:
            r = await self.client.post(f"{API_BASE}/bot{self.token}/{method}", json=params, timeout=40)
            data = r.json()
            if not data.get("ok"):
                desc = data.get("description", "")
                if "blocked" in desc or "chat not found" in desc or "deactivated" in desc:
                    chat = params.get("chat_id")
                    if chat:
                        ex("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=?", (str(chat),))
                self.error = desc
                return None
            return data.get("result")
        except Exception as e:
            self.error = str(e)[:200]
            return None

    async def send(self, chat_id, text, buttons=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **params)

    # ---------- lifecycle ----------
    async def run(self):
        if not self.enabled:
            print("[telegram] TELEGRAM_BOT_TOKEN not set, bot disabled")
            return
        async with httpx.AsyncClient() as client:
            self.client = client
            me = await self.call("getMe")
            if not me:
                print("[telegram] could not reach bot:", self.error)
                self.enabled = False
                return
            self.username = me.get("username")
            self.error = None
            await self.call("deleteWebhook", drop_pending_updates=False)
            await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
            print(f"[telegram] bot @{self.username} running")
            while True:
                updates = await self.call("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message"])
                if updates is None:
                    await asyncio.sleep(3)
                    continue
                for upd in updates:
                    self.offset = upd["update_id"] + 1
                    try:
                        await self.handle(upd.get("message") or {})
                    except Exception as e:
                        print("[telegram] handler error", e)

    # ---------- commands ----------
    def user_by_chat(self, chat_id):
        return q1("SELECT * FROM users WHERE tg_chat_id=?", (str(chat_id),))

    def save_setting(self, u, **kv):
        s = _settings(u)
        s.update(kv)
        ex("UPDATE users SET settings=? WHERE id=?", (json.dumps(s), u["id"]))
        self.users_at = 0

    async def handle(self, msg):
        chat = msg.get("chat", {})
        if chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            await self.send(chat_id, "Send /help to see what I can do.")
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        site = get_settings().get("site_name") or "ArbEdge"

        if cmd == "/start":
            if arg:
                u = q1("SELECT * FROM users WHERE tg_link_code=?", (arg,))
                if not u:
                    await self.send(chat_id, "That link has expired. Open <b>Preferences</b> on the website and press <b>Connect Telegram</b> again.")
                    return
                ex("UPDATE users SET tg_chat_id=NULL WHERE tg_chat_id=?", (str(chat_id),))
                ex("UPDATE users SET tg_chat_id=?, tg_username=?, tg_link_code=NULL WHERE id=?",
                   (str(chat_id), msg.get("from", {}).get("username") or "", u["id"]))
                self.users_at = 0
                acc = access_for(u)
                extra = "" if acc["active"] else "\n\nYou don’t have an active plan yet, so alerts will start once you pick one on the website."
                await self.send(chat_id, f"✅ Connected to <b>{_esc(u['email'])}</b>.\n\nYou’ll get a message here for every new arb that matches your filters on {_esc(site)}. Send /status to see them, or /help for commands.{extra}")
                return
            await self.send(chat_id, f"👋 This is the {_esc(site)} alert bot.\n\nTo connect it, log in on the website, open <b>Preferences</b> and press <b>Connect Telegram</b>.",
                            self.site_button())
            return

        u = self.user_by_chat(chat_id)
        if cmd == "/help":
            lines = "\n".join(f"/{c} – {d}" for c, d in COMMANDS)
            await self.send(chat_id, f"I send you arbs the moment they match your filters.\nBookmakers and sports come from your <b>Preferences</b> on the website.\n\n{lines}")
            return
        if not u:
            await self.send(chat_id, "This chat isn’t connected yet. Open <b>Preferences</b> on the website and press <b>Connect Telegram</b>.", self.site_button())
            return
        s = _settings(u)
        if cmd == "/status":
            acc = access_for(u)
            plan = f"{acc['plan']['name']}" + (f", until {time.strftime('%d %b %Y', time.gmtime(acc['expiresAt'] / 1000))}" if acc.get("expiresAt") else "") if acc["active"] else "none (alerts are off until you pick a plan)"
            books = s.get("bookmakers") or []
            sports = s.get("sports") or []
            await self.send(chat_id, (
                f"<b>Account</b> {_esc(u['email'])}\n<b>Plan</b> {_esc(plan)}\n\n"
                f"<b>Alerts</b> {'⏸ paused' if s.get('tgPaused') else '▶️ on'}\n"
                f"<b>Minimum profit</b> {float(s.get('tgMinProfit', s.get('minProfit', 0)) or 0):.1f}%\n"
                f"<b>In play</b> {'on' if s.get('tgLive', True) else 'off'}   <b>Pre-match</b> {'on' if s.get('tgPrematch', True) else 'off'}\n"
                f"<b>Bookmakers</b> {len(books) if books else 'all'}\n<b>Sports</b> {', '.join(sports) if sports else 'all'}\n"
                f"<b>Stake used in messages</b> {s.get('currency', '$')}{s.get('stake', 100)}"))
            return
        if cmd == "/min":
            try:
                v = max(0.0, min(30.0, float(arg.replace("%", ""))))
            except ValueError:
                await self.send(chat_id, "Send a number, for example <code>/min 1.5</code>")
                return
            self.save_setting(u, tgMinProfit=v)
            await self.send(chat_id, f"Done. You’ll only get arbs of <b>{v:.1f}%</b> or more.")
            return
        if cmd in ("/pause", "/resume"):
            self.save_setting(u, tgPaused=cmd == "/pause")
            await self.send(chat_id, "⏸ Alerts paused. Send /resume to start again." if cmd == "/pause" else "▶️ Alerts are on again.")
            return
        if cmd in ("/live", "/prematch"):
            on = arg.lower() not in ("off", "0", "no")
            key = "tgLive" if cmd == "/live" else "tgPrematch"
            self.save_setting(u, **{key: on})
            await self.send(chat_id, f"{'In-play' if cmd == '/live' else 'Pre-match'} arbs turned <b>{'on' if on else 'off'}</b>.")
            return
        if cmd == "/stop":
            ex("UPDATE users SET tg_chat_id=NULL WHERE id=?", (u["id"],))
            self.users_at = 0
            await self.send(chat_id, "Disconnected. You won’t get any more alerts here. Connect again from Preferences any time.")
            return
        await self.send(chat_id, "I don’t know that one. Send /help.")

    def site_button(self):
        return [[{"text": "Open the website", "url": self.site}]] if self.site.startswith("https://") else None

    # ---------- alerts ----------
    def linked_users(self):
        if _now() - self.users_at > 10000:
            self.users = q("SELECT * FROM users WHERE tg_chat_id IS NOT NULL AND status='active'")
            self.users_at = _now()
        return self.users

    def matches(self, u, s, acc, a):
        if a.get("locked"):
            return False
        if a["live"] and (not acc["live"] or not s.get("tgLive", True)):
            return False
        if not a["live"] and not s.get("tgPrematch", True):
            return False
        if a["profit"] > acc["maxProfit"]:
            return False
        if a["profit"] < float(s.get("tgMinProfit", s.get("minProfit", 0)) or 0):
            return False
        if acc["delay"] and _now() - a["firstSeen"] < acc["delay"] * 1000:
            return False
        books = s.get("bookmakers") or []
        if books and not all(l["bookmaker"] in books for l in a["legs"]):
            return False
        sports = s.get("sports") or []
        if sports and a["sport"] not in sports:
            return False
        return True

    def format_arb(self, u, s, a):
        bm = self.engine.bookmakers
        stake = float(s.get("stake") or 100)
        cur = s.get("currency") or "$"
        inv = sum(1 / l["odds"] for l in a["legs"])
        lines = []
        for i, l in enumerate(a["legs"]):
            st = stake * (1 / l["odds"]) / inv
            name = bm.get(l["bookmaker"], {}).get("name", l["bookmaker"])
            lines.append(f"{i + 1}. {_esc(l['outcome'])} @ <b>{l['odds']:.2f}</b>  {_esc(name)}  stake {cur}{st:,.2f}")
        profit = stake / inv - stake
        text = (f"🟢 <b>{a['profit']:.2f}% arb</b>  {_esc(a['sport'])}\n"
                f"<b>{_esc(a['home'])} vs {_esc(a['away'])}</b>\n"
                f"{_esc(a['league'])}, {_esc(a['marketName'])}, {_starts(a['commence'])}\n\n"
                + "\n".join(lines) +
                f"\n\nStake {cur}{stake:,.0f} in total, profit {cur}{profit:,.2f} whoever wins.\n<i>Check every price on the bookmaker before you place it.</i>")
        buttons = []
        if self.api_public.startswith("https://"):
            row = []
            for l in a["legs"]:
                b = bm.get(l["bookmaker"], {})
                if b.get("affiliate_url") or b.get("site_url"):
                    row.append({"text": f"Open {b.get('name', l['bookmaker'])}", "url": f"{self.api_public}/go/{l['bookmaker']}?u={u['id']}"})
            if row:
                buttons.append(row)
        if self.site.startswith("https://"):
            buttons.append([{"text": "Open in scanner", "url": f"{self.site}/app"}])
        return text, buttons or None

    def allow(self, uid):
        now = _now()
        r = [t for t in self.recent.get(uid, []) if now - t < WINDOW_MS]
        if len(r) >= MAX_PER_WINDOW:
            self.recent[uid] = r
            return False
        r.append(now)
        self.recent[uid] = r
        return True

    async def on_arbs(self, arbs):
        if not self.enabled or not self.client:
            return
        live_ids = {a["id"] for a in arbs}
        self.sent = {k for k in self.sent if k[1] in live_ids}
        queue = []
        for u in self.linked_users():
            s = _settings(u)
            if s.get("tgPaused"):
                continue
            acc = access_for(u)
            if not acc["active"]:
                continue
            for a in arbs:
                key = (u["id"], a["id"])
                if key in self.sent or not self.matches(u, s, acc, a):
                    continue
                self.sent.add(key)
                if self.allow(u["id"]):
                    queue.append((u, s, a))
        for u, s, a in queue:
            text, buttons = self.format_arb(u, s, a)
            await self.send(u["tg_chat_id"], text, buttons)
            await asyncio.sleep(0.04)  # stay well under Telegram's 30 msg/s
        await self.maybe_channel(arbs)

    async def maybe_channel(self, arbs):
        if not self.channel or _now() - self.channel_last < self.channel_every:
            return
        pool = [a for a in arbs if 1 <= a["profit"] <= 15]
        if not pool:
            return
        a = max(pool, key=lambda x: x["profit"])
        self.channel_last = _now()
        site = get_settings().get("site_name") or "ArbEdge"
        text = (f"🔥 <b>{a['profit']:.2f}% arb found</b>\n{_esc(a['sport'])}, {_esc(a['league'])}, {_esc(a['marketName'])}\n"
                f"{_starts(a['commence'])}\n\nBack every outcome, profit {a['profit']:.2f}% whoever wins. "
                f"The bookmakers, odds and stakes are on {_esc(site)}.")
        btn = [[{"text": "Start the $10 trial", "url": f"{self.site}/register"}]] if self.site.startswith("https://") else None
        await self.send(self.channel, text, btn)

    # ---------- used by API ----------
    def link_url(self, uid):
        code = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]
        ex("UPDATE users SET tg_link_code=? WHERE id=?", (code, uid))
        return f"https://t.me/{self.username}?start={code}" if self.username else None

    async def test(self, u):
        s = _settings(u)
        a = self.engine.arbs[0] if self.engine.arbs else None
        if not a:
            return await self.send(u["tg_chat_id"], "✅ Test message. Alerts are working; there are no open arbs this second.")
        text, buttons = self.format_arb(u, s, a)
        return await self.send(u["tg_chat_id"], "🧪 <b>Test alert</b>\n\n" + text, buttons)

    async def broadcast(self, text, audience="all"):
        users = q("SELECT * FROM users WHERE tg_chat_id IS NOT NULL AND status='active'")
        n = 0
        for u in users:
            if audience == "active" and not access_for(u)["active"]:
                continue
            if await self.send(u["tg_chat_id"], _esc(text)):
                n += 1
            await asyncio.sleep(0.05)
        return n

    def status(self):
        linked = q1("SELECT COUNT(*) c FROM users WHERE tg_chat_id IS NOT NULL")["c"]
        return {"enabled": self.enabled, "bot": self.username, "linked": linked, "channel": self.channel or None,
                "error": self.error if not self.username else None}


from .engine import engine  # noqa: E402

bot = TelegramBot(engine)
engine.notifier = bot
