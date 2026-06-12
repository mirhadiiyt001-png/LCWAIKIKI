"""
LC Waikiki RU registration bot — Telegram control panel.

Replaces the original CLI entry point. An operator drives the browser
automation through Telegram instead of editing number.txt / proxy.txt and
running the script by hand.

Two extras inspired by the 0x_HAWK_TG reference bot:
  * A premium-emoji styled control panel — inline buttons with custom-emoji
    icons and colour styles, rendered through the raw Telegram API with a
    graceful fallback (see core/premium.py).
  * Strict admin-only access — only the Telegram user IDs in ADMIN_IDS can
    use the bot; everyone else is rejected.

Commands:
  /start       Show the control panel (admins only).
  /addnumbers  Add phone numbers (inline text or attach/reply a .txt file).
  /addproxy    Add proxies (inline text, or attach a file with caption "proxy").
  /run         Start processing the loaded numbers.
               `/run skip` resumes, skipping numbers that already succeeded.
  /stop        Request a graceful stop.
  /status      Show live run statistics.
  /history     Show per-number outcomes recorded so far (alias /results).
  /clear       Clear loaded numbers, proxies and result history.

Config via environment variables:
  BOT_TOKEN            Telegram bot token (required).
  ADMIN_IDS            Comma-separated Telegram user IDs that own the bot (required).
  EMOJI_PACKS          Comma-separated sticker-pack names for premium emoji (optional).
  NUMBERS_PER_SESSION    Numbers processed per browser session (default 10).
  CONCURRENT_SESSIONS    Parallel browser sessions per chunk (default 2).
  AUTO_RUN_INTERVAL_MIN  Minutes between auto-run ticks when auto is ON (default 30).
  LOOP_FOREVER           "true" to keep re-running rounds until /stop (default false).
  HEADLESS               "false" to run with a visible browser (default true).
  BROWSER_CHANNEL        Optional Playwright channel, e.g. "chrome".
  FIXED_PASSWORD         Password used on every registration (default Hadii@2024).
"""

import asyncio
import html
import json
import logging
import os
import tempfile
import time

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core import automation, premium
from core.premium import ce, with_icon
from core.runner import RegistrationRunner

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx/telegram request logs include the full API URL, which embeds the bot
# token. Silence them so the token never lands in logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("lcw-bot")

SEP = "━━━━━━━━━━━━━━━"


# ─────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()


def _parse_admin_ids():
    raw = os.environ.get("ADMIN_IDS", "")
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_admin_ids()
NUMBERS_PER_SESSION = int(os.environ.get("NUMBERS_PER_SESSION", "10") or "10")
CONCURRENT_SESSIONS = int(os.environ.get("CONCURRENT_SESSIONS", "2") or "2")
AUTO_RUN_INTERVAL_MIN = int(os.environ.get("AUTO_RUN_INTERVAL_MIN", "30") or "30")
LOOP_FOREVER = os.environ.get("LOOP_FOREVER", "false").lower() == "true"

# Where loaded numbers/proxies are persisted so they survive a restart.
STATE_FILE = os.environ.get(
    "STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json"),
)


def _dedup_preserve(seq):
    """Return seq with duplicates removed, preserving first-seen order."""
    seen = set()
    out = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────
#  Shared state
# ─────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.numbers = []
        self.proxies = []
        # Per-number outcome history, keyed by phone number:
        #   {"status": "success"|"failed"|"error_stop",
        #    "detail": str, "last_attempt": epoch_float, "attempts": int}
        self.results = {}
        self.runner = None
        self.run_task = None
        self.auto_run_enabled: bool = False
        self.auto_chat_id: int | None = None

    def reset_collection(self):
        self.numbers = []
        self.proxies = []
        self.results = {}
        self.save()

    def clear_numbers(self):
        self.numbers = []
        self.save()

    def clear_proxies(self):
        self.proxies = []
        self.save()

    def clear_results(self):
        self.results = {}
        self.save()

    def succeeded_numbers(self):
        """Set of phone numbers that have a recorded successful attempt."""
        return {n for n, r in self.results.items() if r.get("status") == "success"}

    def record_result(self, phone, status, detail=""):
        """Persist one per-number outcome with a timestamp and attempt count."""
        phone = str(phone)
        prev = self.results.get(phone, {})
        self.results[phone] = {
            "status": status,
            "detail": detail or "",
            "last_attempt": time.time(),
            "attempts": int(prev.get("attempts", 0)) + 1,
        }
        self.save()

    def load(self):
        """Reload persisted numbers/proxies/results from disk on startup."""
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read state file %s: %s", STATE_FILE, e)
            return
        if isinstance(data, dict):
            nums = data.get("numbers")
            proxies = data.get("proxies")
            results = data.get("results")
            changed = False
            if isinstance(nums, list):
                self.numbers = _dedup_preserve([str(n) for n in nums])
                changed = changed or len(self.numbers) != len(nums)
            if isinstance(proxies, list):
                self.proxies = _dedup_preserve([str(p) for p in proxies])
                changed = changed or len(self.proxies) != len(proxies)
            if isinstance(results, dict):
                self.results = {str(k): v for k, v in results.items() if isinstance(v, dict)}
            logger.info(
                "Restored %d numbers, %d proxies and %d results from %s",
                len(self.numbers), len(self.proxies), len(self.results), STATE_FILE,
            )
            if changed:
                logger.info("Removed duplicate numbers/proxies from persisted state")
                self.save()

    def save(self):
        """Persist current numbers/proxies/results to disk atomically."""
        data = {
            "numbers": self.numbers,
            "proxies": self.proxies,
            "results": self.results,
        }
        try:
            directory = os.path.dirname(STATE_FILE) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp_path, STATE_FILE)
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        except OSError as e:
            logger.warning("Could not save state file %s: %s", STATE_FILE, e)


state = BotState()

# Access control. Admins (ADMIN_IDS) are pre-approved owners. Other users
# may request access; an admin approves or declines each request.
approved_users: set[int] = set()
pending_users: dict[int, dict] = {}  # user_id -> {"name", "username"}


# ─────────────────────────────────────────────────────────────────
#  Auth helpers
# ─────────────────────────────────────────────────────────────────
def _uid(update: Update):
    return update.effective_user.id if update.effective_user else None


def is_admin_id(uid) -> bool:
    return bool(uid) and uid in ADMIN_IDS


def has_access(uid) -> bool:
    if not ADMIN_IDS:
        return False
    return bool(uid) and (uid in ADMIN_IDS or uid in approved_users)


async def deny(update: Update):
    await update.message.reply_text(
        "🔒 This bot is private. Send /start to request access."
    )


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


# ─────────────────────────────────────────────────────────────────
#  Styled message + keyboard builders
# ─────────────────────────────────────────────────────────────────
def locked_welcome_text() -> str:
    return (
        f'{ce("🛍️")} <b>LC WAIKIKI · RU</b> {ce("🛍️")}\n'
        f"{SEP}\n\n"
        f'{ce("🔒")} <i>This bot is private.</i>\n\n'
        f"Send a request below and an admin will review it."
    )


def request_access_rows() -> list[list[dict]]:
    return [[with_icon({"text": "Request Access", "callback_data": "req:access"}, "🔓")]]


def panel_text() -> str:
    running = bool(state.runner and state.runner.is_running)
    dot = ce("🟢") if running else ce("🟡")
    auto_dot = ce("🟢") if state.auto_run_enabled else ce("🔴")
    auto_label = f"ON · every {AUTO_RUN_INTERVAL_MIN} min" if state.auto_run_enabled else "OFF"
    return (
        f'{ce("🛍️")} <b>LC WAIKIKI · CONTROL PANEL</b>\n'
        f"{SEP}\n\n"
        f'╭─ {ce("📦")} <b>LOADED</b>\n'
        f'├ {ce("📱")} Numbers  →  <b>{len(state.numbers)}</b>\n'
        f'╰ {ce("🌐")} Proxies  →  <b>{len(state.proxies)}</b>\n\n'
        f'╭─ {ce("⚙️")} <b>STATE</b>\n'
        f'├ {dot} <b>{"RUNNING" if running else "IDLE"}</b>\n'
        f'╰ {auto_dot} <b>AUTO {auto_label}</b>\n\n'
        f'{ce("👇")} <i>Tap a button or use the commands.</i>'
    )


def control_rows(uid) -> list[list[dict]]:
    auto_lbl = "Auto: ON ✅" if state.auto_run_enabled else "Auto: OFF"
    rows = [
        [
            with_icon({"text": "Run", "callback_data": "panel:run"}, "▶️"),
            with_icon({"text": "Resume", "callback_data": "panel:resume"}, "➡️"),
            with_icon({"text": "Stop", "callback_data": "panel:stop"}, "⛔️"),
        ],
        [
            with_icon({"text": "Status", "callback_data": "panel:status"}, "📊"),
            with_icon({"text": "Results", "callback_data": "panel:history"}, "🗂"),
            with_icon({"text": "Clear", "callback_data": "panel:clear"}, "🗑️"),
        ],
        [
            with_icon({"text": "Add Numbers", "callback_data": "panel:addnum"}, "📱"),
            with_icon({"text": "Add Proxy", "callback_data": "panel:addproxy"}, "🌐"),
        ],
        [with_icon({"text": auto_lbl, "callback_data": "panel:auto"}, "🤖")],
    ]
    if is_admin_id(uid):
        rows.append([with_icon({"text": "Members", "callback_data": "panel:users"}, "👥")])
    rows.append([with_icon({"text": "Refresh", "callback_data": "panel:refresh"}, "🔄")])
    return rows


def status_text_styled() -> str:
    if not state.runner:
        return (
            f'{ce("📊")} <b>STATUS</b>\n{SEP}\n\n'
            f'╭─ {ce("🟡")} <b>IDLE</b>\n'
            f'├ {ce("📱")} Numbers  →  <b>{len(state.numbers)}</b>\n'
            f'╰ {ce("🌐")} Proxies  →  <b>{len(state.proxies)}</b>'
        )
    r = state.runner
    s = r.stats
    out = (
        f'{ce("📊")} <b>LIVE STATUS</b>\n{SEP}\n\n'
        f'╭─ {ce("⚡️")} <b>RUN</b>\n'
        f'├ {ce("🔄")} Round    →  <b>{s["round"]}</b>\n'
        f'├ {ce("🧩")} Session  →  <b>{s["current_session"]}/{s["total_sessions"]}</b>\n'
        f'╰ {ce("📍")} {esc(s["status"])}\n\n'
        f'╭─ {ce("📈")} <b>TOTALS</b>\n'
        f'├ {ce("✅")} Success    →  <b>{s["successful"]}</b>\n'
        f'├ {ce("❌")} Failed     →  <b>{s["failed"]}</b>\n'
        f'├ {ce("📨")} Submitted  →  <b>{s["total_submissions"]}</b>\n'
        f'╰ {ce("⏳")} Remaining  →  <b>{r.remaining()}</b>\n\n'
        f'╭─ {ce("⏱️")} <b>UPTIME</b>\n'
        f"╰ {r.runtime_str()}"
    )
    if s["last_error"]:
        out += f'\n\n{ce("⚠️")} <i>{esc(s["last_error"])}</i>'
    return out


def _fmt_age(ts) -> str:
    """Human-friendly relative time for an epoch timestamp."""
    try:
        secs = int(time.time() - float(ts))
    except (TypeError, ValueError):
        return "—"
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def history_text_styled() -> str:
    results = state.results
    if not results:
        return (
            f'{ce("🗂")} <b>RESULTS</b>\n{SEP}\n\n'
            f'{ce("📭")} <i>No attempts recorded yet.</i>\n'
            f"Run the bot first — outcomes are tracked here."
        )

    succeeded = [n for n, r in results.items() if r.get("status") == "success"]
    failed = [n for n, r in results.items() if r.get("status") != "success"]

    # Most-recently attempted numbers first.
    recent = sorted(
        results.items(),
        key=lambda kv: kv[1].get("last_attempt", 0),
        reverse=True,
    )[:12]

    lines = []
    for phone, r in recent:
        icon = ce("✅") if r.get("status") == "success" else ce("❌")
        attempts = r.get("attempts", 1)
        att = f" ×{attempts}" if attempts and attempts > 1 else ""
        lines.append(
            f'╰ {icon} <code>+7{esc(phone)}</code> — {_fmt_age(r.get("last_attempt"))}{att}'
        )
    recent_str = "\n".join(lines)

    extra = len(results) - len(recent)
    more = f"\n<i>… and {extra} more</i>" if extra > 0 else ""

    return (
        f'{ce("🗂")} <b>RESULTS</b>\n{SEP}\n\n'
        f'╭─ {ce("📊")} <b>TOTALS</b>\n'
        f'├ {ce("✅")} Succeeded  →  <b>{len(succeeded)}</b>\n'
        f'├ {ce("❌")} Failed     →  <b>{len(failed)}</b>\n'
        f'╰ {ce("🔢")} Tracked    →  <b>{len(results)}</b>\n\n'
        f'╭─ {ce("🕒")} <b>RECENT</b>\n'
        f"{recent_str}"
        f"{more}"
    )


# ─────────────────────────────────────────────────────────────────
#  Members view builder
# ─────────────────────────────────────────────────────────────────
def members_text() -> str:
    lines = [f'{ce("👥")} <b>MEMBERS</b>\n{SEP}\n']
    if not pending_users:
        lines.append(f'{ce("⏳")} No pending requests.')
    else:
        lines.append(f'{ce("⏳")} <b>PENDING ({len(pending_users)})</b>')
        for u_id, info in pending_users.items():
            name = esc(info.get("name", "Unknown"))
            uname_part = f' · @{esc(info.get("username",""))}' if info.get("username") else ""
            lines.append(f'  {ce("👤")} <b>{name}</b>{uname_part}  <code>{u_id}</code>')
    non_admin_approved = [u for u in approved_users if u not in ADMIN_IDS]
    if non_admin_approved:
        lines.append(f'\n{ce("✅")} <b>APPROVED ({len(non_admin_approved)})</b>')
        for u_id in non_admin_approved:
            lines.append(f'  <code>{u_id}</code>')
    return "\n".join(lines)


def members_rows() -> list[list[dict]]:
    rows = []
    for u_id, info in pending_users.items():
        name = (info.get("name") or str(u_id))[:20]
        rows.append([
            with_icon({"text": f"✅ {name}", "callback_data": f"approve:{u_id}"}, "✅"),
            with_icon({"text": "❌ Decline", "callback_data": f"decline:{u_id}"}, "❌"),
        ])
    rows.append([with_icon({"text": "↩ Back", "callback_data": "panel:refresh"}, "🔙")])
    return rows


# ─────────────────────────────────────────────────────────────────
#  Throttled event streaming
# ─────────────────────────────────────────────────────────────────
class EventStreamer:
    """Coalesces a burst of progress lines into periodic Telegram messages."""

    def __init__(self, bot, chat_id, interval=1.5, max_chars=3500):
        self.bot = bot
        self.chat_id = chat_id
        self.interval = interval
        self.max_chars = max_chars
        self.queue = asyncio.Queue()
        self._task = None
        self._stopped = False

    def start(self):
        self._task = asyncio.create_task(self._drain())

    def emit(self, text):
        try:
            self.queue.put_nowait(text)
        except Exception:
            pass

    async def _send(self, text):
        if not text:
            return
        try:
            # Premium emoji + HTML styling. The runner emits HTML-safe cards
            # (dynamic values escaped at the source), and raw_send degrades
            # gracefully if the API rejects the richer payload.
            await premium.raw_send(BOT_TOKEN, self.chat_id, text)
        except Exception as e:
            logger.warning("stream send failed: %s", e)

    async def _drain(self):
        buf = []
        while not self._stopped:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=self.interval)
                buf.append(item)
                # Greedily pull anything else already queued.
                while not self.queue.empty():
                    buf.append(self.queue.get_nowait())
            except asyncio.TimeoutError:
                pass

            if buf:
                chunk = ""
                for line in buf:
                    if len(chunk) + len(line) + 1 > self.max_chars:
                        await self._send(chunk)
                        chunk = ""
                    chunk += line + "\n"
                if chunk:
                    await self._send(chunk)
                buf = []

    async def stop(self):
        self._stopped = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval * 2)
            except Exception:
                self._task.cancel()
        # Flush anything remaining.
        rest = []
        while not self.queue.empty():
            rest.append(self.queue.get_nowait())
        if rest:
            await self._send("\n".join(rest))


# ─────────────────────────────────────────────────────────────────
#  Live animated "processing" card
# ─────────────────────────────────────────────────────────────────
class LiveCard:
    """A single message, edited in place, that animates the current number."""

    STEP_ORDER = ["fill", "phone", "consent", "submit", "waiting"]
    STEP_LABELS = {
        "fill": "Filling details",
        "phone": "Phone number",
        "consent": "Accepting terms",
        "submit": "Clicking register",
        "waiting": "Waiting for OTP",
    }
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _PULSE = ["🟢", "🔵", "🟣"]

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.message_id = None
        self.session = 0
        self.total_sessions = 0     # total sessions per round; shown as N/total
        self.proxy = ""             # current session proxy; shown masked on the card
        self.round = 0              # current round; shown in the header, edited in place
        self.phase = "start"        # start|launch|open|cookies|number|result
        self.phone = ""
        self.index = 0
        self.total = 0
        self.cur = -1               # active step index for the current number
        self.result = None          # success|failed|error_stop
        self.detail = ""
        self.frame = 0
        self.done = False
        self.final_text = ""

    # Called synchronously from the runner's event-loop thread.
    def on_event(self, ev):
        kind = ev.get("kind")
        if ev.get("session"):
            self.session = ev["session"]
        if kind == "round":
            # Just bump the round counter; the new round's first session/number
            # events will refresh the rest of the card. The header re-renders in
            # place so Round 1 -> Round 2 -> ... never spawns a new message.
            self.round = ev.get("round", 0) or 0
            return
        if kind == "session":
            # Session indicator + proxy update in place on the same card, so a new
            # session never spawns a fresh message (Session 1/45 -> 2/45 -> ...).
            self.total_sessions = ev.get("total_sessions", 0) or 0
            self.proxy = ev.get("proxy", "") or ""
            return
        if kind == "phase":
            self.phase = ev["phase"]
        elif kind == "number":
            self.phase = "number"
            self.phone = ev.get("phone", "") or ""
            self.index = ev.get("index", 0) or 0
            self.total = ev.get("total", 0) or 0
            self.cur = -1
            self.result = None
            self.detail = ""
        elif kind == "step":
            self.phase = "number"
            self.result = None
            try:
                self.cur = max(self.cur, self.STEP_ORDER.index(ev["step"]))
            except (ValueError, KeyError):
                pass
        elif kind == "result":
            self.phase = "result"
            self.result = ev.get("status")
            self.detail = ev.get("detail", "") or ""

    # ── rendering ──
    @staticmethod
    def _bar(done, total, width=10, frame=0, active=False):
        if total <= 0:
            total = 1
        filled = max(0, min(width, int(round(done / total * width))))
        cells = ["▰"] * filled + ["▱"] * (width - filled)
        # While work is in progress, sweep a bright marker across the
        # remaining cells so the bar visibly animates even when the
        # percentage is momentarily static (e.g. waiting for OTP).
        if active and filled < width:
            cells[filled + (frame % (width - filled))] = "▸"
        return "".join(cells)

    def _tally(self):
        r = state.runner
        if not r:
            return ""
        s = r.stats
        return (
            f'{ce("✅")} <b>{s["successful"]}</b>   '
            f'{ce("❌")} <b>{s["failed"]}</b>   '
            f'{ce("⏳")} <b>{r.remaining()}</b> left'
        )

    def _number_block(self, spin):
        steps = self.STEP_ORDER
        n = len(steps)
        cur = max(self.cur, 0)
        success = self.result == "success"
        failed = self.result in ("failed", "error_stop")

        phone_disp = f"+7{self.phone}" if self.phone else "—"
        lines = [f'{ce("📱")} <b>{phone_disp}</b>']
        if self.total:
            sess_lbl = f'{self.session}/{self.total_sessions}' if self.total_sessions else f'{self.session}'
            lines.append(f'{ce("🧩")} Session {sess_lbl} · {self.index}/{self.total}')

        done = n if success else cur
        active = not success and not failed
        bar = self._bar(done, n, frame=self.frame, active=active)
        lines.append("")
        lines.append(f'{bar}  {int(done / n * 100)}%')
        lines.append("")

        dots = "." * (1 + self.frame % 3)
        for i, key in enumerate(steps):
            label = self.STEP_LABELS[key]
            if success or i < cur:
                lines.append(f'{ce("✅")} {label}')
            elif i == cur and failed:
                lines.append(f'{ce("❌")} <b>{label}</b>')
            elif i == cur:
                lines.append(f'<b>{spin} {label}{dots}</b>')
            else:
                lines.append(f'{ce("🔹")} <i>{label}</i>')

        if success:
            lines.append("")
            lines.append(f'{ce("✅")} <b>OTP SENT</b>')
        elif failed:
            lines.append("")
            lines.append(f'{ce("⚠️")} <i>{esc(self.detail[:90]) or "failed"}</i>')
        return "\n".join(lines)

    def render(self):
        spin = self.SPINNER[self.frame % len(self.SPINNER)]
        pulse = self._PULSE[self.frame % len(self._PULSE)]
        rnd = f'  {ce("🔄")} <b>R{self.round}</b>' if self.round else ""
        head = f'{ce("🛍️")} <b>LC WAIKIKI · LIVE</b>{rnd}  {ce(pulse)}\n{SEP}\n\n'
        tally = self._tally()
        prep = {
            "start": ("🚀", "Starting engine"),
            "launch": ("🚀", "Launching browser"),
            "open": ("🌐", "Opening lcwaikiki.ru"),
            "cookies": ("🍪", "Accepting cookies"),
        }
        if self.phase in prep:
            ico, label = prep[self.phase]
            if self.session:
                sess = f" · S{self.session}/{self.total_sessions}" if self.total_sessions else f" · S{self.session}"
            else:
                sess = ""
            dots = "." * (1 + self.frame % 3)
            body = f'<b>{ce(ico)} {label}{sess}{dots}</b>  {spin}'
        elif self.phase in ("number", "result"):
            body = self._number_block(spin)
        else:
            body = ""
        meta = ""
        if self.proxy:
            meta = f'{ce("🌐")} <tg-spoiler><i>{esc(self.proxy)}</i></tg-spoiler>\n\n'
        out = head + meta + body
        if tally:
            out += f"\n\n{SEP}\n{tally}"
        return out


ANIM_INTERVAL = 1.5


async def _animate_card(card: "LiveCard"):
    """Edit the card on a fixed cadence so the spinner/steps appear to animate."""
    last = None
    fails = 0
    while not card.done:
        await asyncio.sleep(ANIM_INTERVAL)
        card.frame += 1
        if card.message_id is None:
            continue
        text = card.render()
        if text == last:
            continue
        last = text
        try:
            resp = await premium.raw_edit(BOT_TOKEN, card.chat_id, card.message_id, text)
        except Exception:
            resp = None
        # Stop wasting edits if the card message is gone (e.g. deleted by the
        # user) — a string of failures means there is nothing to update.
        if resp and resp.get("ok"):
            fails = 0
        else:
            fails += 1
            if fails >= 5:
                card.message_id = None
    if card.message_id is not None:
        final = card.final_text or card.render()
        try:
            await premium.raw_edit(BOT_TOKEN, card.chat_id, card.message_id, final)
        except Exception:
            pass


def _final_card_text(r) -> str:
    s = r.stats
    return (
        f'{ce("🏁")} <b>RUN FINISHED</b>\n{SEP}\n\n'
        f'╭─ {ce("📈")} <b>RESULTS</b>\n'
        f'├ {ce("✅")} Success  →  <b>{s["successful"]}</b>\n'
        f'├ {ce("❌")} Failed   →  <b>{s["failed"]}</b>\n'
        f'╰ {ce("📨")} Total    →  <b>{s["total_submissions"]}</b>\n\n'
        f'{ce("⏱️")} Runtime  →  <b>{r.runtime_str()}</b>'
    )


# ─────────────────────────────────────────────────────────────────
#  Operational helpers (shared by commands and panel buttons)
# ─────────────────────────────────────────────────────────────────
async def start_run(context, chat_id, skip_succeeded=False, on_complete=None) -> str:
    # Atomic guard: a started run leaves a live run_task. There is no await
    # between this check and assigning state.run_task below, so two starts
    # cannot both pass.
    if state.run_task and not state.run_task.done():
        return "⚠️ Already running. Use Status or Stop."
    if not state.numbers:
        return "❌ No numbers loaded. Use Add Numbers first."

    succeeded = state.succeeded_numbers()
    if skip_succeeded and succeeded and succeeded.issuperset(state.numbers):
        return "✅ All loaded numbers already succeeded. Nothing to resume."

    runner = RegistrationRunner(
        numbers=state.numbers,
        proxies=state.proxies,
        numbers_per_session=NUMBERS_PER_SESSION,
        concurrent_sessions=CONCURRENT_SESSIONS,
        loop_forever=LOOP_FOREVER,
        skip_succeeded=skip_succeeded,
        succeeded=succeeded,
        result_sink=state.record_result,
    )
    state.runner = runner

    streamer = EventStreamer(context.bot, chat_id)
    streamer.start()

    card = LiveCard(chat_id)

    async def _runner_wrapper():
        # Post the live card and start its animation loop.
        resp = await premium.raw_send(BOT_TOKEN, chat_id, card.render())
        try:
            card.message_id = resp["result"]["message_id"]
        except (KeyError, TypeError):
            card.message_id = None
        anim = asyncio.create_task(_animate_card(card))
        try:
            await runner.run(streamer.emit, on_step=card.on_event)
        finally:
            card.final_text = _final_card_text(runner)
            card.done = True
            try:
                await asyncio.wait_for(anim, timeout=ANIM_INTERVAL * 3)
            except Exception:
                anim.cancel()
            await streamer.stop()
            # Send completion notification
            try:
                s = runner.stats
                notif = (
                    f'{ce("🏁")} <b>RUN COMPLETE</b>\n{SEP}\n\n'
                    f'{ce("✅")} Success  →  <b>{s["successful"]}</b>\n'
                    f'{ce("❌")} Failed   →  <b>{s["failed"]}</b>\n'
                    f'{ce("📨")} Total    →  <b>{s["total_submissions"]}</b>\n'
                    f'{ce("⏱️")} Runtime  →  <b>{runner.runtime_str()}</b>'
                )
                await premium.raw_send(BOT_TOKEN, chat_id, notif)
            except Exception:
                pass
            # Fire on_complete callback (used by auto-run)
            if on_complete:
                try:
                    await on_complete(runner)
                except Exception:
                    pass

    state.run_task = asyncio.create_task(_runner_wrapper())
    return ""


def do_stop() -> str:
    if not state.runner or not state.runner.is_running:
        return "Nothing is running."
    state.runner.request_stop()
    return "🛑 Stop requested — finishing the current number…"


def do_clear() -> str:
    if state.runner and state.runner.is_running:
        return "⚠️ A run is in progress. Use Stop first."
    state.reset_collection()
    return "🧹 Cleared all numbers and proxies."


# ─────────────────────────────────────────────────────────────────
#  Document helper
# ─────────────────────────────────────────────────────────────────
async def _read_attached_text(update, context):
    """Return the text content of an attached/replied document, or None."""
    msg = update.message
    doc = msg.document if msg else None
    if not doc and msg and msg.reply_to_message:
        doc = msg.reply_to_message.document
    if not doc:
        return None
    if doc.file_size and doc.file_size > 2_000_000:
        return None
    f = await context.bot.get_file(doc.file_id)
    data = await f.download_as_bytearray()
    try:
        return bytes(data).decode("utf-8", errors="ignore")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
#  Command handlers
# ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = _uid(update)
    chat_id = update.effective_chat.id
    if has_access(uid):
        await premium.raw_send(BOT_TOKEN, chat_id, panel_text(), control_rows(uid))
    else:
        await premium.raw_send(BOT_TOKEN, chat_id, locked_welcome_text(), request_access_rows())


async def cmd_addnumbers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)

    # Inline numbers come from EITHER the command args (PTB already splits the
    # tokens after "/addnumbers") OR the raw message body (e.g. a bare file
    # upload routed here). Reading both double-counts every number, so pick a
    # single source.
    if context.args:
        text = "\n".join(context.args)
    else:
        text = update.message.text or ""

    file_text = await _read_attached_text(update, context)
    if file_text:
        text += "\n" + file_text

    if not text.strip():
        return await update.message.reply_text(
            "Send numbers after the command (one per line) or attach a .txt file.\n"
            "Example:\n/addnumbers\n9522999899\n9528309103"
        )

    valid, skipped = automation.parse_phone_numbers(text)
    seen = set(state.numbers)
    new = []
    for n in valid:
        if n not in seen:
            seen.add(n)
            new.append(n)
    state.numbers.extend(new)
    if new:
        state.save()

    await update.message.reply_text(
        f"✅ Added {len(new)} new numbers ({len(valid) - len(new)} duplicates skipped, "
        f"{len(skipped)} invalid).\nTotal loaded: {len(state.numbers)}."
    )


async def cmd_addproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)

    # Single-source rule as in cmd_addnumbers — combining args + body would
    # double-count every proxy.
    if context.args:
        text = "\n".join(context.args)
    else:
        text = update.message.text or ""

    file_text = await _read_attached_text(update, context)
    if file_text:
        text += "\n" + file_text

    if not text.strip():
        return await update.message.reply_text(
            "Send proxies after the command (one per line) or attach a file.\n"
            "Format: host:port or host:port:user:pass"
        )

    parsed = automation.parse_proxies(text)
    seen = set(state.proxies)
    new = []
    for p in parsed:
        if p not in seen:
            seen.add(p)
            new.append(p)
    state.proxies.extend(new)
    if new:
        state.save()
    await update.message.reply_text(
        f"✅ Added {len(new)} proxies.\nTotal proxies: {len(state.proxies)}."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    if state.runner and state.runner.is_running:
        return await update.message.reply_text("⚠️ A run is in progress — use Stop first.")
    chat_id = update.effective_chat.id
    clear_rows = [
        [
            with_icon({"text": "Numbers", "callback_data": "clear:numbers"}, "📱"),
            with_icon({"text": "Proxies", "callback_data": "clear:proxies"}, "🌐"),
        ],
        [
            with_icon({"text": "Results", "callback_data": "clear:results"}, "🗂"),
            with_icon({"text": "All", "callback_data": "clear:all"}, "🧹"),
        ],
        [with_icon({"text": "Cancel", "callback_data": "clear:cancel"}, "❌")],
    ]
    await premium.raw_send(
        BOT_TOKEN, chat_id,
        f'{ce("🗑️")} <b>CLEAR — choose what to delete:</b>\n{SEP}\n'
        f'├ {ce("📱")} Numbers   ({len(state.numbers)} loaded)\n'
        f'├ {ce("🌐")} Proxies   ({len(state.proxies)} loaded)\n'
        f'├ {ce("🗂")} Results   ({len(state.results)} recorded)\n'
        f'╰ {ce("🧹")} All of the above',
        clear_rows,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    await premium.raw_send(BOT_TOKEN, update.effective_chat.id, status_text_styled())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    await update.message.reply_text(do_stop())


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    # `/run skip` (or resume/new) only processes numbers that have not yet
    # succeeded, so reruns don't re-trigger registrations that already worked.
    skip = bool(context.args) and context.args[0].lower() in ("skip", "resume", "new", "remaining")
    msg = await start_run(context, update.effective_chat.id, skip_succeeded=skip)
    if msg:
        await update.message.reply_text(msg)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    await premium.raw_send(BOT_TOKEN, update.effective_chat.id, history_text_styled())


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_id(_uid(update)):
        return await deny(update)
    await premium.raw_send(
        BOT_TOKEN, update.effective_chat.id, members_text(), members_rows()
    )


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(_uid(update)):
        return await deny(update)
    chat_id = update.effective_chat.id
    if state.auto_run_enabled:
        state.auto_run_enabled = False
        state.auto_chat_id = None
        for job in context.job_queue.get_jobs_by_name("auto_run"):
            job.schedule_removal()
        await update.message.reply_text(f'{ce("🤖")} Auto-run <b>OFF</b>.', parse_mode="HTML")
    else:
        state.auto_run_enabled = True
        state.auto_chat_id = chat_id
        # Schedule repeating job for subsequent runs (first run is triggered directly below)
        context.job_queue.run_repeating(
            auto_run_job,
            interval=AUTO_RUN_INTERVAL_MIN * 60,
            first=AUTO_RUN_INTERVAL_MIN * 60,
            name="auto_run",
            chat_id=chat_id,
        )
        await update.message.reply_text(
            f'{ce("🤖")} Auto-run <b>ON</b> · starting now, then every {AUTO_RUN_INTERVAL_MIN} min. '
            f'Use /auto again to stop.',
            parse_mode="HTML",
        )
        # Directly trigger first run immediately (don't depend on scheduler)
        if state.numbers and (not state.run_task or state.run_task.done()):
            result = await start_run(context, chat_id, skip_succeeded=True)
            if result:
                await premium.raw_send(BOT_TOKEN, chat_id, result)


async def auto_run_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: fire a run if the bot is idle and numbers are loaded."""
    chat_id = state.auto_chat_id
    if not chat_id:
        return
    if not state.numbers:
        logger.info("Auto-run skipped: no numbers loaded.")
        return
    if state.run_task and not state.run_task.done():
        logger.info("Auto-run skipped: already running.")
        return

    # Check if all numbers already succeeded — skip silently
    succeeded = state.succeeded_numbers()
    if succeeded and succeeded.issuperset(state.numbers):
        logger.info("Auto-run skipped: all %d numbers already succeeded.", len(state.numbers))
        return

    remaining = len(state.numbers) - len(succeeded & set(state.numbers))
    logger.info("Auto-run triggered (interval %d min). %d numbers remaining.", AUTO_RUN_INTERVAL_MIN, remaining)
    await premium.raw_send(
        BOT_TOKEN, chat_id,
        f'{ce("🤖")} <b>AUTO-RUN TRIGGERED</b>\n'
        f'{ce("📱")} {remaining} numbers to process (skipping {len(succeeded & set(state.numbers))} succeeded)',
    )

    async def _auto_complete(runner):
        """Called when an auto-run finishes."""
        s = runner.stats
        if s["successful"] == 0 and s["failed"] == 0:
            return
        logger.info(
            "Auto-run finished: %d success, %d failed, runtime %s",
            s["successful"], s["failed"], runner.runtime_str(),
        )

    result = await start_run(context, chat_id, skip_succeeded=True, on_complete=_auto_complete)
    if result:
        await premium.raw_send(BOT_TOKEN, chat_id, result)


async def notify_admins_access_request(bot, uid: int, name: str, username: str):
    uname_part = f" (@{esc(username)})" if username else ""
    text = (
        f'{ce("🔔")} <b>ACCESS REQUEST</b>\n'
        f'{ce("👤")} <b>{esc(name)}</b>{uname_part}\n'
        f'<code>{uid}</code>\n\n'
        f'Tap to approve or decline:'
    )
    rows = [[
        with_icon({"text": "✅ Approve", "callback_data": f"approve:{uid}"}, "✅"),
        with_icon({"text": "❌ Decline", "callback_data": f"decline:{uid}"}, "❌"),
    ]]
    for admin_id in ADMIN_IDS:
        try:
            await premium.raw_send(BOT_TOKEN, admin_id, text, rows)
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", admin_id, e)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bare file upload (no command): route by caption."""
    if not has_access(_uid(update)):
        return await deny(update)
    caption = (update.message.caption or "").lower()
    if "proxy" in caption:
        await cmd_addproxy(update, context)
    else:
        await cmd_addnumbers(update, context)


# ─────────────────────────────────────────────────────────────────
#  Callback (inline button) handler
# ─────────────────────────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    uid = user.id
    data = query.data or ""
    msg = query.message
    chat_id = msg.chat.id if msg else uid
    msg_id = msg.message_id if msg else None

    # ── Access request ──
    if data == "req:access":
        if has_access(uid):
            return await query.answer("You already have access.", show_alert=True)
        await query.answer("✅ Request sent! An admin will review it.", show_alert=True)
        user = query.from_user
        name = user.full_name or str(uid)
        username = user.username or ""
        pending_users[uid] = {"name": name, "username": username}
        await notify_admins_access_request(context.bot, uid, name, username)
        return

    # ── Approve / decline ──
    if data.startswith("approve:") or data.startswith("decline:"):
        if not is_admin_id(uid):
            return await query.answer("Admins only.", show_alert=True)
        action_str, target_str = data.split(":", 1)
        try:
            target_id = int(target_str)
        except ValueError:
            return await query.answer("Invalid ID.", show_alert=True)
        if action_str == "approve":
            approved_users.add(target_id)
            pending_users.pop(target_id, None)
            await query.answer(f"✅ User {target_id} approved.")
            try:
                await premium.raw_send(
                    BOT_TOKEN, target_id,
                    f'{ce("✅")} <b>Access granted!</b> Send /start to open the panel.',
                )
            except Exception:
                pass
        else:
            pending_users.pop(target_id, None)
            approved_users.discard(target_id)
            await query.answer(f"❌ User {target_id} declined.")
            try:
                await premium.raw_send(
                    BOT_TOKEN, target_id,
                    f'{ce("❌")} <b>Request declined.</b> Contact the admin if you think this is a mistake.',
                )
            except Exception:
                pass
        if msg_id is not None:
            try:
                await premium.raw_edit(BOT_TOKEN, chat_id, msg_id, members_text(), members_rows())
            except Exception:
                pass
        return

    # ── Clear submenu ──
    if data.startswith("clear:"):
        if not has_access(uid):
            return await query.answer("Access denied.", show_alert=True)
        clear_action = data.split(":", 1)[1]
        if clear_action == "cancel":
            await query.answer("Cancelled.")
            if msg_id is not None:
                try:
                    await premium.raw_edit(BOT_TOKEN, chat_id, msg_id, "❌ Clear cancelled.")
                except Exception:
                    pass
            return
        if state.runner and state.runner.is_running:
            return await query.answer("⚠️ Stop the run first.", show_alert=True)
        if clear_action == "numbers":
            state.clear_numbers()
            await query.answer("📱 Numbers cleared.")
            label = f'{ce("✅")} Numbers cleared.'
        elif clear_action == "proxies":
            state.clear_proxies()
            await query.answer("🌐 Proxies cleared.")
            label = f'{ce("✅")} Proxies cleared.'
        elif clear_action == "results":
            state.clear_results()
            await query.answer("🗂 Results cleared.")
            label = f'{ce("✅")} Results history cleared.'
        elif clear_action == "all":
            state.reset_collection()
            await query.answer("🧹 All cleared.")
            label = f'{ce("✅")} All cleared — numbers, proxies and results.'
        else:
            return await query.answer()
        if msg_id is not None:
            try:
                await premium.raw_edit(BOT_TOKEN, chat_id, msg_id, label)
            except Exception:
                pass
        return

    # ── Control panel ──
    if data.startswith("panel:"):
        if not has_access(uid):
            return await query.answer("Access denied.", show_alert=True)
        action = data.split(":", 1)[1]

        if action == "run":
            await query.answer()
            result = await start_run(context, chat_id)
            if result:
                await premium.raw_send(BOT_TOKEN, chat_id, result)
        elif action == "resume":
            await query.answer()
            result = await start_run(context, chat_id, skip_succeeded=True)
            if result:
                await premium.raw_send(BOT_TOKEN, chat_id, result)
        elif action == "history":
            await query.answer()
            await premium.raw_send(
                BOT_TOKEN, chat_id, history_text_styled(), control_rows(uid)
            )
        elif action == "stop":
            await query.answer()
            await premium.raw_send(BOT_TOKEN, chat_id, do_stop())
        elif action == "status":
            await query.answer()
            await premium.raw_send(
                BOT_TOKEN, chat_id, status_text_styled(), control_rows(uid)
            )
        elif action == "clear":
            if state.runner and state.runner.is_running:
                return await query.answer("⚠️ Stop the run first.", show_alert=True)
            await query.answer()
            clear_rows = [
                [
                    with_icon({"text": "Numbers", "callback_data": "clear:numbers"}, "📱"),
                    with_icon({"text": "Proxies", "callback_data": "clear:proxies"}, "🌐"),
                ],
                [
                    with_icon({"text": "Results", "callback_data": "clear:results"}, "🗂"),
                    with_icon({"text": "All", "callback_data": "clear:all"}, "🧹"),
                ],
                [with_icon({"text": "Cancel", "callback_data": "clear:cancel"}, "❌")],
            ]
            await premium.raw_send(
                BOT_TOKEN, chat_id,
                f'{ce("🗑️")} <b>CLEAR — choose what to delete:</b>\n{SEP}\n'
                f'├ {ce("📱")} Numbers   ({len(state.numbers)} loaded)\n'
                f'├ {ce("🌐")} Proxies   ({len(state.proxies)} loaded)\n'
                f'├ {ce("🗂")} Results   ({len(state.results)} recorded)\n'
                f'╰ {ce("🧹")} All of the above',
                clear_rows,
            )
        elif action == "users":
            if not is_admin_id(uid):
                return await query.answer("Admins only.", show_alert=True)
            await query.answer()
            await premium.raw_send(BOT_TOKEN, chat_id, members_text(), members_rows())
        elif action == "auto":
            if not has_access(uid):
                return await query.answer("Access denied.", show_alert=True)
            await query.answer()
            if state.auto_run_enabled:
                state.auto_run_enabled = False
                state.auto_chat_id = None
                for job in context.job_queue.get_jobs_by_name("auto_run"):
                    job.schedule_removal()
                msg_auto = f'{ce("🤖")} Auto-run <b>disabled</b>.'
            else:
                state.auto_run_enabled = True
                state.auto_chat_id = chat_id
                # Schedule repeating job for subsequent runs (first run is triggered directly below)
                context.job_queue.run_repeating(
                    auto_run_job,
                    interval=AUTO_RUN_INTERVAL_MIN * 60,
                    first=AUTO_RUN_INTERVAL_MIN * 60,
                    name="auto_run",
                    chat_id=chat_id,
                )
                msg_auto = f'{ce("🤖")} Auto-run <b>enabled</b> · starting now, then every {AUTO_RUN_INTERVAL_MIN} min.'
            await premium.raw_send(BOT_TOKEN, chat_id, msg_auto)
            if msg_id is not None:
                try:
                    await premium.raw_edit(BOT_TOKEN, chat_id, msg_id, panel_text(), control_rows(uid))
                except Exception:
                    pass
            # If auto was just enabled, directly trigger first run immediately
            if state.auto_run_enabled and state.numbers and (not state.run_task or state.run_task.done()):
                result = await start_run(context, chat_id, skip_succeeded=True)
                if result:
                    await premium.raw_send(BOT_TOKEN, chat_id, result)
        elif action == "addnum":
            await query.answer(
                "Send /addnumbers then the list (one per line) or attach a .txt.",
                show_alert=True,
            )
        elif action == "addproxy":
            await query.answer(
                "Send /addproxy then the list, or attach a file captioned 'proxy'.",
                show_alert=True,
            )
        elif action == "refresh":
            await query.answer("Refreshed")
            if msg_id is not None:
                await premium.raw_edit(
                    BOT_TOKEN, chat_id, msg_id, panel_text(), control_rows(uid)
                )
        else:
            await query.answer()
        return

    await query.answer()


# ─────────────────────────────────────────────────────────────────
#  Entry
# ─────────────────────────────────────────────────────────────────
BOT_COMMANDS = [
    BotCommand("start", "Open the control panel"),
    BotCommand("run", "Start processing numbers"),
    BotCommand("stop", "Stop the current run"),
    BotCommand("status", "Live run statistics"),
    BotCommand("addnumbers", "Add phone numbers"),
    BotCommand("addproxy", "Add proxies"),
    BotCommand("clear", "Clear data (submenu)"),
    BotCommand("users", "Manage member access (admin)"),
    BotCommand("auto", "Toggle 30-min auto-run (admin)"),
    BotCommand("help", "Show help / panel"),
]


async def _post_init(app):
    try:
        await premium.load_custom_emoji_packs(app.bot)
    except Exception as e:
        logger.warning("custom emoji load failed: %s", e)
    # Register the command menu so typing "/" shows every command in Telegram.
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning("set_my_commands failed: %s", e)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Add it via environment secrets.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty — the bot will reject everyone until it is set.")

    state.load()
    approved_users.update(ADMIN_IDS)

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("addnumbers", cmd_addnumbers))
    app.add_handler(CommandHandler("addproxy", cmd_addproxy))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("results", cmd_history))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    logger.info("Bot starting (admins: %s)…", ", ".join(map(str, ADMIN_IDS)) or "NONE")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
