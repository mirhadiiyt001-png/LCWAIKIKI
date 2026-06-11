# LC Waikiki RU — Telegram Bot

A Telegram-controlled version of the original CLI registration automation.
The browser automation (anti-detect stealth, proxy handling, multi-number
sessions) is preserved from the original `bot.py`; the command-line interface
is replaced by a Telegram control panel.

## Architecture

```
bot_tg.py            Telegram control panel (commands, panel, access gate, streaming)
core/automation.py   Browser automation primitives (ported from bot.py)
core/runner.py       RegistrationRunner — drives sessions over the number queue
core/premium.py      Premium-emoji + styled-button helpers (raw API, graceful fallback)
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Find your Telegram user ID (message [@userinfobot](https://t.me/userinfobot)).
3. Set the environment variables (see `.env.example`):
   - `BOT_TOKEN` — required
   - `ADMIN_IDS` — required, comma-separated user IDs that own the bot
   - Optional: `EMOJI_PACKS`, `NUMBERS_PER_SESSION`, `LOOP_FOREVER`,
     `HEADLESS`, `BROWSER_CHANNEL`, `FIXED_PASSWORD`
4. Install Playwright's Chromium browser (done automatically during setup):
   `python -m playwright install chromium`

## Deploy to Railway

This folder is a self-contained, deployable app. A `Dockerfile` (based on the
official Playwright Python image, so Chromium and all its system libraries are
already present) and a `railway.json` are included.

1. Push this folder to a GitHub repo (the repo root must contain `bot_tg.py`,
   `requirements.txt`, `Dockerfile` and `railway.json`).
2. On [Railway](https://railway.app): **New Project → Deploy from GitHub repo**
   and pick the repo. Railway detects the `Dockerfile` automatically.
3. Add the service **Variables** (Railway dashboard → your service → Variables):
   - `BOT_TOKEN` — required
   - `ADMIN_IDS` — required, comma-separated owner user IDs
   - Optional: `EMOJI_PACKS`, `NUMBERS_PER_SESSION`, `LOOP_FOREVER`,
     `HEADLESS` (keep `true`), `BROWSER_CHANNEL` (leave empty), `FIXED_PASSWORD`
4. Deploy. This is a long-polling bot — it dials out to Telegram, so **no
   public domain or port is needed** (you can ignore Railway's "no exposed
   ports" notice).

> **State note:** the bot persists loaded numbers / proxies / results to
> `bot_state.json` on the local disk. Railway's container filesystem is
> ephemeral and resets on every redeploy. To keep state across redeploys,
> attach a Railway **Volume** and point the bot at it — mount the volume at
> `/data` and set `STATE_FILE=/data/bot_state.json` in the service variables.
> (Do **not** mount the volume at `/app`: that would hide the app code and the
> container won't start.)

## Usage

Message the bot in Telegram:

| Command | Description |
| --- | --- |
| `/start` | Show the control panel and current state |
| `/addnumbers` | Add phone numbers — inline (one per line) or attach a `.txt` file |
| `/addproxy` | Add proxies — inline or attach a file with caption `proxy` |
| `/run` | Start processing the loaded numbers |
| `/run skip` | Resume — process only numbers that have not yet succeeded |
| `/stop` | Stop gracefully after the current number |
| `/status` | Live run statistics |
| `/history` | Per-number outcomes recorded so far (alias `/results`) |
| `/clear` | Clear loaded numbers, proxies and result history |

`/start` opens a **premium-emoji control panel**: inline buttons (Run, Resume,
Stop, Status, Results, Clear, Add Numbers, Add Proxy, Refresh) with custom-emoji
icons and coloured styles. The panel buttons mirror the commands, so you can
drive the bot entirely by tapping.

## Result tracking & resuming

Every number's outcome (success / failed, the last-attempt time and an attempt
count) is persisted to `bot_state.json` as it happens. This means:

- **`/history`** (or the **Results** button) shows a summary — how many numbers
  succeeded, how many failed, and the most recently attempted numbers.
- **`/run skip`** (or the **Resume** button) reruns the batch but skips any
  number that already has a successful OTP, so reruns — including
  `LOOP_FOREVER` rounds — don't re-trigger registrations that already worked.
- A plain **`/run`** still reprocesses everything from scratch.

`/clear` wipes the result history along with the loaded numbers and proxies.

All commands are registered with Telegram (`setMyCommands`), so typing `/` in
the chat shows the full list with descriptions for quick autocomplete.

Phone numbers are 10-digit RU numbers (no `+7`); `+7`, spaces and dashes are
stripped automatically. Proxies use `host:port` or `host:port:user:pass`.

## Live processing animation

While a run is in progress the bot shows a single **live card** that is edited
in place to animate the number currently being processed. It shows the number,
its session position, a progress bar, and the per-step status — *filling
details → phone number → accepting terms → clicking register → waiting for
OTP* — with an animated spinner on the active step and a running ✅ / ❌ /
remaining tally. Each number ends on ✅ *OTP sent* or ❌ with the failure
reason, and the card finishes with a run summary. The card is styled to match
the premium-emoji panel and degrades gracefully like the rest of the UI.

## Access control (admin-only)

The bot is private and **admin-only**. Only the Telegram user IDs listed in
`ADMIN_IDS` can use it — every command and every panel button is gated on that
list. Anyone else who opens the bot sees a locked notice and is rejected; there
is no request-to-join or approval flow. With `ADMIN_IDS` unset the bot fails
closed and rejects everyone.

## Premium emoji

Styled buttons (`style`) and custom-emoji icons (`icon_custom_emoji_id`), plus
custom emoji in message text (`<tg-emoji>` entities), are sent via the raw
Telegram API. Where the client or bot cannot render them, the bot automatically
falls back to plain unicode emoji and standard buttons — so the panel always
works, and looks richer where premium emoji are available. Configure which
sticker packs are scanned with `EMOJI_PACKS`.

## Notes / deviations from the original

- The original launched real Chrome with a visible window (`channel="chrome"`,
  `headless=False`). On a headless server this is not possible, so the bot
  defaults to the **bundled Chromium in headless mode**. Both are overridable
  via `HEADLESS` and `BROWSER_CHANNEL`.
- File-based input (`number.txt` / `proxy.txt`) is replaced by the
  `/addnumbers` and `/addproxy` commands (which also accept file uploads).
- The rich console UI is replaced by streamed Telegram progress messages.
- The hard site error (`Ошибка. Попробуйте снова.`) stops the run, matching
  the original behaviour.
