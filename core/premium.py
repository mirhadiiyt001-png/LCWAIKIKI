"""
Premium emoji + styled button helpers.

Telegram "premium" custom emoji and coloured inline buttons are not part of the
standard Bot API surface exposed by python-telegram-bot, so this module talks to
the raw HTTP API and degrades gracefully:

  * Custom emoji are loaded from public sticker packs and rendered in message
    text via <tg-emoji emoji-id="..."> entities. Where a custom emoji is not
    available (or the client/bot cannot render it), the plain unicode emoji is
    used instead.
  * Inline buttons are sent with an `icon_custom_emoji_id` and a colour `style`.
    If the API rejects those richer fields, raw_send retries with progressively
    simpler payloads (no styles -> no icons -> no custom-emoji text) so the
    message is always delivered.

This mirrors the design of the 0x_HAWK_TG reference bot.
"""

import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger("lcw-bot.premium")

VS16 = "\uFE0F"
MAX_CALLBACK_DATA = 60
TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)

# Public sticker packs scanned for custom (premium) emoji. Overridable via the
# EMOJI_PACKS env var (comma-separated pack short-names).
_DEFAULT_PACKS = [
    "Taj_Mehyar",
    "GiftsGiftsGifts",
    "Icon_2023",
    "GameEmoji",
    "TONEmoji",
    "NewsEmoji",
    "RestrictedEmoji",
]


def _parse_packs() -> list[str]:
    raw = os.environ.get("EMOJI_PACKS", "").strip()
    if not raw:
        return list(_DEFAULT_PACKS)
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


EMOJI_PACKS = _parse_packs()

# unicode emoji -> custom_emoji_id
custom_emoji_map: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────
#  Loading
# ─────────────────────────────────────────────────────────────────
async def load_custom_emoji_packs(bot) -> None:
    """Populate custom_emoji_map from the configured sticker packs."""
    custom_emoji_map.clear()
    for pack_name in EMOJI_PACKS:
        try:
            sticker_set = await bot.get_sticker_set(pack_name)
        except Exception:
            continue  # Missing/renamed pack — skip it.
        for sticker in sticker_set.stickers:
            if (
                getattr(sticker, "type", None) == "custom_emoji"
                and sticker.custom_emoji_id
                and sticker.emoji
                and sticker.emoji not in custom_emoji_map
            ):
                custom_emoji_map[sticker.emoji] = sticker.custom_emoji_id
    logger.info(
        "Custom emoji loaded: %d packs, %d unique emoji",
        len(EMOJI_PACKS),
        len(custom_emoji_map),
    )


# ─────────────────────────────────────────────────────────────────
#  Resolution
# ─────────────────────────────────────────────────────────────────
def ce_id(emoji: str) -> Optional[str]:
    """Resolve the custom_emoji_id for a unicode emoji (variation tolerant)."""
    result = custom_emoji_map.get(emoji)
    if result:
        return result
    result = custom_emoji_map.get(emoji.replace(VS16, ""))
    if result:
        return result
    if not emoji.endswith(VS16):
        result = custom_emoji_map.get(emoji + VS16)
    return result


def ce(emoji: str) -> str:
    """Wrap an emoji in a <tg-emoji> entity when a custom version exists."""
    eid = ce_id(emoji)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'
    return emoji


def with_icon(btn: dict, emoji: str) -> dict:
    """Attach icon_custom_emoji_id to a button dict when available."""
    eid = ce_id(emoji)
    if eid:
        return {**btn, "icon_custom_emoji_id": eid}
    return btn


# ─────────────────────────────────────────────────────────────────
#  Callback-data safety
# ─────────────────────────────────────────────────────────────────
def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_callback_data(prefix: str, value: str) -> str:
    return prefix + _truncate_bytes(value, MAX_CALLBACK_DATA - len(prefix.encode("utf-8")))


# ─────────────────────────────────────────────────────────────────
#  Styled keyboard assembly
# ─────────────────────────────────────────────────────────────────
_STYLES = ["primary", "success", "danger"]
_style_idx = 0


def _next_style() -> str:
    global _style_idx
    s = _STYLES[_style_idx % len(_STYLES)]
    _style_idx += 1
    return s


def _colorize(rows: list[list[dict]]) -> dict:
    out = []
    for row in rows:
        colored = []
        for btn in row:
            b = dict(btn)
            interactive = (
                (b.get("callback_data") and b["callback_data"] != "noop")
                or b.get("url")
                or b.get("copy_text")
            )
            if interactive and not b.get("style"):
                b["style"] = _next_style()
            colored.append(b)
        out.append(colored)
    return {"inline_keyboard": out}


def _strip_styles(kb: dict) -> dict:
    return {
        "inline_keyboard": [
            [{k: v for k, v in btn.items() if k != "style"} for btn in row]
            for row in kb["inline_keyboard"]
        ]
    }


def _strip_icons(kb: dict) -> dict:
    return {
        "inline_keyboard": [
            [
                {k: v for k, v in btn.items() if k not in ("style", "icon_custom_emoji_id")}
                for btn in row
            ]
            for row in kb["inline_keyboard"]
        ]
    }


def _strip_tg_emoji(text: str) -> str:
    if "<tg-emoji" not in text:
        return text
    return TG_EMOJI_RE.sub(r"\1", text)


# ─────────────────────────────────────────────────────────────────
#  Raw API send / edit with graceful fallback
# ─────────────────────────────────────────────────────────────────
async def _tg_post(token: str, method: str, payload: dict) -> dict:
    body = dict(payload)
    if "reply_markup" in body and isinstance(body["reply_markup"], dict):
        body["reply_markup"] = json.dumps(body["reply_markup"])
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/{method}", json=body
            )
            return resp.json()
    except Exception as e:  # network errors etc.
        # Never let the token reach a logged/returned description.
        desc = str(e).replace(token, "<redacted>") if token else str(e)
        return {"ok": False, "description": desc}


async def _send_with_fallback(token: str, method: str, base: dict, rows) -> dict:
    """Send/edit via `method`, downgrading the payload until it is accepted."""
    if not rows:
        resp = await _tg_post(token, method, base)
        if not resp.get("ok") and "<tg-emoji" in base.get("text", ""):
            resp = await _tg_post(
                token, method, {**base, "text": _strip_tg_emoji(base["text"])}
            )
        return resp

    kb = _colorize(rows)

    # Tier 1: full styled keyboard.
    resp = await _tg_post(token, method, {**base, "reply_markup": kb})
    if resp.get("ok"):
        return resp

    # Tier 2: drop button colour styles.
    no_styles = _strip_styles(kb)
    resp = await _tg_post(token, method, {**base, "reply_markup": no_styles})
    if resp.get("ok"):
        return resp

    # Tier 3: drop premium icons too.
    no_icons = _strip_icons(kb)
    resp = await _tg_post(token, method, {**base, "reply_markup": no_icons})
    if resp.get("ok"):
        return resp

    # Tier 4: strip <tg-emoji> tags from the text.
    if "<tg-emoji" in base.get("text", ""):
        resp = await _tg_post(
            token,
            method,
            {**base, "text": _strip_tg_emoji(base["text"]), "reply_markup": no_icons},
        )
        if resp.get("ok"):
            return resp

    logger.warning("raw %s failed after all fallbacks: %s", method, resp.get("description"))
    return resp


async def raw_send(token, chat_id, text, rows=None, parse_mode="HTML") -> dict:
    base = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    return await _send_with_fallback(token, "sendMessage", base, rows)


async def raw_edit(token, chat_id, message_id, text, rows=None, parse_mode="HTML") -> dict:
    base = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    return await _send_with_fallback(token, "editMessageText", base, rows)
