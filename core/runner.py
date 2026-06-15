"""
RegistrationRunner: drives the browser automation over a queue of numbers.

Replaces the original CLI `run_bot` loop. Instead of printing to a rich
console and reading global stats, it tracks state on the instance and emits
human-readable progress lines through a synchronous `emit(text)` callback so
the Telegram bot can stream updates to the admin.
"""

import asyncio
import html
import re
import time

from playwright.async_api import async_playwright

from . import automation
from .premium import ce

# Visual separator for streamed premium "cards".
_LINE = "━━━━━━━━━━━━━━━"


def _esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


# Maps the automation's human log lines to structured step events that the
# Telegram bot animates as a live "processing" card. Coupled to the icons used
# in automation.py by design — the runner is the seam that translates raw
# automation output into bot-facing events.
_SESS_RE = re.compile(r"\[S(\d+)\]")
_NUM_RE = re.compile(r"Number\s+(\d+)\s*/\s*(\d+)\s*:\s*\+7(\S+)")


def _classify_log(icon, msg):
    sess = None
    m = _SESS_RE.search(msg)
    if m:
        sess = int(m.group(1))

    if icon == "▶️":
        mm = _NUM_RE.search(msg)
        if mm:
            return {
                "kind": "number",
                "index": int(mm.group(1)),
                "total": int(mm.group(2)),
                "phone": mm.group(3),
                "session": sess,
            }
        return {"kind": "number", "session": sess}

    phase = {"🚀": "launch", "🌐": "open", "🍪": "cookies"}.get(icon)
    if phase:
        return {"kind": "phase", "phase": phase, "session": sess}

    step = None
    if icon in ("📧", "🔑"):
        step = "fill"
    elif icon == "📞":
        step = "phone"
    elif icon in ("☑️", "📱"):
        step = "consent"
    elif icon == "📨":
        step = "submit"
    elif icon in ("✓", "⏳", "⏱️", "ℹ️"):
        step = "waiting"
    if step:
        return {"kind": "step", "step": step, "session": sess}

    return {"kind": "noise", "icon": icon, "session": sess}


class RegistrationRunner:
    NUMBERS_PER_SESSION_DEFAULT = 10

    def __init__(self, numbers, proxies, numbers_per_session=None, loop_forever=False,
                 skip_succeeded=False, succeeded=None, result_sink=None,
                 concurrent_sessions=1):
        self.numbers = list(numbers)
        self.proxies = list(proxies)
        self.numbers_per_session = numbers_per_session or self.NUMBERS_PER_SESSION_DEFAULT
        self.concurrent_sessions = max(1, int(concurrent_sessions or 1))
        self.loop_forever = loop_forever

        # When skip_succeeded is on, numbers present in `succeeded` are not
        # reprocessed. `result_sink(phone, status, detail)` is an optional
        # callback used to persist each per-number outcome as it happens.
        self.skip_succeeded = skip_succeeded
        self.succeeded = set(succeeded or ())
        self.result_sink = result_sink

        # The actual list processed this run (may exclude already-succeeded
        # numbers). Populated in run(); falls back to the full list.
        self.run_numbers = list(self.numbers)
        self.skipped_count = 0

        self._stop_event = asyncio.Event()
        self.is_running = False

        self.stats = {
            "total_submissions": 0,
            "successful": 0,
            "failed": 0,
            "processed": 0,        # numbers consumed in the current round
            "current_session": 0,
            "total_sessions": 0,
            "round": 0,
            "start_time": None,
            "status": "IDLE",
            "last_error": "",
            "last_number": "",
        }

    def remaining(self):
        return max(0, len(self.run_numbers) - self.stats["processed"])

    # ─────────────────────────────────────────────────────────────
    def request_stop(self):
        self._stop_event.set()

    def should_stop(self):
        return self._stop_event.is_set()

    def runtime_str(self):
        if not self.stats["start_time"]:
            return "0s"
        secs = int(time.time() - self.stats["start_time"])
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def status_text(self):
        s = self.stats
        return (
            f"Status: {s['status']}\n"
            f"Round: {s['round']}\n"
            f"Session: {s['current_session']}/{s['total_sessions']}\n"
            f"Last number: {('+7' + s['last_number']) if s['last_number'] else '—'}\n"
            f"Submissions: {s['total_submissions']}\n"
            f"Success: {s['successful']}\n"
            f"Failed: {s['failed']}\n"
            f"Runtime: {self.runtime_str()}\n"
            f"Numbers loaded: {len(self.numbers)}\n"
            f"Numbers remaining (this round): {self.remaining()}\n"
            f"Proxies: {len(self.proxies) if self.proxies else 0}"
            + (f"\nLast error: {s['last_error']}" if s['last_error'] else "")
        )

    # ─────────────────────────────────────────────────────────────
    async def run(self, emit, on_step=None):
        """
        Execute the registration loop.

        `emit(text)` is a synchronous callable used to stream progress.
        `on_step(event)` is an optional synchronous callable that receives
        structured step events (see `_classify_log`) so the Telegram bot can
        animate a live "processing" card for the current number.
        Returns when all numbers are processed (one pass), the run is
        stopped, or a hard site error is detected.
        """
        if self.is_running:
            emit(f'{ce("⚠️")} <b>Already running.</b>')
            return

        if not self.numbers:
            emit(f'{ce("❌")} <b>No phone numbers loaded.</b> <i>Use Add Numbers first.</i>')
            return

        self.is_running = True
        self._stop_event.clear()
        self.stats["start_time"] = time.time()
        self.stats["status"] = "RUNNING"
        self.stats["successful"] = 0
        self.stats["failed"] = 0
        self.stats["total_submissions"] = 0
        self.stats["processed"] = 0
        self.stats["last_error"] = ""

        # Genuine problems are still pushed to the text log even when an
        # animated card is consuming the happy-path step events, so failures
        # are never hidden behind the animation.
        alert_icons = {"⚠️", "❌", "🔄", "🛑", "💥"}

        def log(icon, msg):
            # All emits happen on this event loop's thread, so a synchronous
            # enqueue is correct and ordered (no run_coroutine_threadsafe).
            if on_step is not None:
                ev = _classify_log(icon, msg)
                try:
                    on_step(ev)
                except Exception:
                    pass
                if ev.get("kind") == "noise" and icon in alert_icons:
                    try:
                        emit(f"{ce(icon)} {_esc(msg)}")
                    except Exception:
                        pass
                return
            try:
                emit(f"{ce(icon)} {_esc(msg)}")
            except Exception:
                pass

        # Optionally drop numbers that already succeeded on a previous run.
        if self.skip_succeeded and self.succeeded:
            self.run_numbers = [n for n in self.numbers if n not in self.succeeded]
            self.skipped_count = len(self.numbers) - len(self.run_numbers)
        else:
            self.run_numbers = list(self.numbers)
            self.skipped_count = 0

        if not self.run_numbers:
            self.is_running = False
            self.stats["status"] = "DONE"
            emit(
                f'{ce("✅")} <b>All {len(self.numbers)} numbers already '
                f'succeeded</b> <i>· nothing to do.</i>'
            )
            return

        # Split numbers into session-sized groups.
        groups = [
            self.run_numbers[i:i + self.numbers_per_session]
            for i in range(0, len(self.run_numbers), self.numbers_per_session)
        ]
        self.stats["total_sessions"] = len(groups)

        proxy_label = f"{len(self.proxies)} proxies" if self.proxies else "No proxy (direct)"
        skipped = f"  <i>({self.skipped_count} skipped)</i>" if self.skipped_count else ""
        emit(
            f'{ce("🚀")} <b>ENGINE STARTED</b>\n'
            f"{_LINE}\n"
            f'{ce("📱")} Numbers   <b>{len(self.run_numbers)}</b>{skipped}\n'
            f'{ce("🧩")} Sessions  <b>{len(groups)}</b> · {self.numbers_per_session}/session\n'
            f'{ce("🌐")} {proxy_label}\n'
            f'{ce("🔄")} Loop      <b>{"ON" if self.loop_forever else "OFF"}</b>'
        )

        # Numbers that failed due to proxy (not site error) — these will be retried.
        proxy_failed_numbers = []
        MAX_PROXY_RETRY_ROUNDS = 3  # max times to retry proxy-failed numbers

        consecutive_site_errors = 0

        async def on_result(phone, result):
            nonlocal consecutive_site_errors
            
            self.stats["total_submissions"] += 1
            self.stats["processed"] += 1
            self.stats["last_number"] = phone
            status = result.get("status")
            if status == "success":
                self.stats["successful"] += 1
                self.succeeded.add(phone)
                consecutive_site_errors = 0
                emit(f'{ce("✅")} <b>+7{phone}</b> <i>· OTP sent</i>')
            elif status == "error_stop":
                self.stats["failed"] += 1
                consecutive_site_errors += 1
                detail = result.get("detail", "Site error")
                self.stats["last_error"] = detail
                emit(
                    f'{ce("⛔️")} <b>+7{phone}</b> <i>· site error ({consecutive_site_errors}/3)</i>'
                    + (f'\n{ce("⚠️")} <i>{_esc(detail)}</i>' if detail else "")
                )
                if consecutive_site_errors >= 3:
                    self._stop_event.set()
            elif status == "proxy_fail":
                # Don't count as permanent failure — will be retried
                proxy_failed_numbers.append(phone)
                detail = result.get("detail", "")
                self.stats["last_error"] = detail
                emit(
                    f'{ce("🔄")} <b>+7{phone}</b> <i>· proxy failed (will retry)</i>'
                )
            else:
                self.stats["failed"] += 1
                detail = result.get("detail", "")
                self.stats["last_error"] = detail
                emit(
                    f'{ce("❌")} <b>+7{phone}</b> <i>· failed</i>'
                    + (f'\n{ce("⚠️")} <i>{_esc(detail)}</i>' if detail else "")
                )

            if on_step is not None:
                try:
                    on_step({
                        "kind": "result",
                        "phone": phone,
                        "status": status,
                        "detail": result.get("detail", ""),
                    })
                except Exception:
                    pass

            # Persist this per-number outcome (success/failed + timestamp).
            # Don't persist proxy_fail — it will be retried.
            if self.result_sink and status != "proxy_fail":
                try:
                    self.result_sink(phone, status, result.get("detail", ""))
                except Exception:
                    pass

        try:
            async with async_playwright() as pw:
                round_num = 0
                while not self._stop_event.is_set():
                    round_num += 1
                    self.stats["round"] = round_num
                    self.stats["processed"] = 0
                    proxy_failed_numbers.clear()

                    if on_step is not None:
                        try:
                            on_step({"kind": "round", "round": round_num})
                        except Exception:
                            pass
                    else:
                        emit(f'{ce("🔄")} <b>ROUND {round_num}</b>')

                    # Process sessions in parallel chunks of `concurrent_sessions`.
                    c = self.concurrent_sessions
                    for chunk_start in range(0, len(groups), c):
                        if self._stop_event.is_set():
                            break
                        chunk = groups[chunk_start:chunk_start + c]

                        tasks = []
                        for j, group in enumerate(chunk):
                            session_id = chunk_start + j + 1
                            self.stats["current_session"] = session_id
                            proxy_raw = None
                            if self.proxies:
                                proxy_raw = self.proxies[(chunk_start + j) % len(self.proxies)]

                            if on_step is not None:
                                try:
                                    on_step({
                                        "kind": "session",
                                        "session": session_id,
                                        "total_sessions": len(groups),
                                        "proxy": proxy_raw or "",
                                    })
                                except Exception:
                                    pass
                            else:
                                nums_label = ", ".join("+7" + n for n in group)
                                emit(
                                    f'{ce("🧩")} <b>SESSION {session_id}/{len(groups)}</b>\n'
                                    f'{ce("📱")} <i>{_esc(nums_label)}</i>'
                                    + (f'\n{ce("🌐")} <tg-spoiler><i>{_esc(proxy_raw)}</i></tg-spoiler>' if proxy_raw else "")
                                )
                            tasks.append(automation.process_session(
                                group, pw, session_id, proxy_raw,
                                on_result=on_result, log=log,
                                should_stop=self.should_stop,
                            ))

                        label = f"{chunk_start+1}" if len(chunk) == 1 else f"{chunk_start+1}–{chunk_start+len(chunk)}"
                        self.stats["status"] = f"SESSIONS {label} RUNNING"
                        await asyncio.gather(*tasks, return_exceptions=True)

                        if self._stop_event.is_set():
                            self.stats["status"] = "STOPPED (site error)"
                            emit(f'{ce("⛔️")} <b>HARD SITE ERROR</b> <i>· stopping run</i>')
                            break

                        is_last = (chunk_start + c >= len(groups))
                        if not is_last and not self._stop_event.is_set():
                            await asyncio.sleep(0.5)

                    # ── Retry proxy-failed numbers ──
                    if not self._stop_event.is_set() and proxy_failed_numbers:
                        for retry_round in range(1, MAX_PROXY_RETRY_ROUNDS + 1):
                            if self._stop_event.is_set() or not proxy_failed_numbers:
                                break
                            retry_nums = list(proxy_failed_numbers)
                            proxy_failed_numbers.clear()

                            emit(
                                f'{ce("🔄")} <b>PROXY RETRY {retry_round}/{MAX_PROXY_RETRY_ROUNDS}</b> · '
                                f'{len(retry_nums)} numbers to retry'
                            )
                            await asyncio.sleep(2)

                            retry_groups = [
                                retry_nums[i:i + self.numbers_per_session]
                                for i in range(0, len(retry_nums), self.numbers_per_session)
                            ]
                            for rg_start in range(0, len(retry_groups), c):
                                if self._stop_event.is_set():
                                    break
                                rg_chunk = retry_groups[rg_start:rg_start + c]
                                rtasks = []
                                for rj, rgroup in enumerate(rg_chunk):
                                    rsid = 900 + rg_start + rj + 1
                                    proxy_raw = None
                                    if self.proxies:
                                        proxy_raw = self.proxies[(rg_start + rj + retry_round) % len(self.proxies)]
                                    rtasks.append(automation.process_session(
                                        rgroup, pw, rsid, proxy_raw,
                                        on_result=on_result, log=log,
                                        should_stop=self.should_stop,
                                    ))
                                
                                await asyncio.gather(*rtasks, return_exceptions=True)

                            if self._stop_event.is_set():
                                self.stats["status"] = "STOPPED (site error)"
                                emit(f'{ce("⛔️")} <b>HARD SITE ERROR</b> <i>· stopping run</i>')
                                break

                        # Any remaining proxy_failed after all retries → mark as failed
                        if proxy_failed_numbers:
                            for phone in proxy_failed_numbers:
                                self.stats["failed"] += 1
                                if self.result_sink:
                                    try:
                                        self.result_sink(phone, "failed", "Proxy failed after all retries")
                                    except Exception:
                                        pass
                            emit(
                                f'{ce("❌")} <b>{len(proxy_failed_numbers)} numbers</b> '
                                f'<i>failed after {MAX_PROXY_RETRY_ROUNDS} proxy retries</i>'
                            )

                    if self._stop_event.is_set() or not self.loop_forever:
                        break

                    emit(
                        f'{ce("✅")} <b>Round {round_num} complete</b>\n'
                        f'{ce("✅")} {self.stats["successful"]}   '
                        f'{ce("❌")} {self.stats["failed"]}'
                    )
                    await asyncio.sleep(0.5)

        except Exception as e:
            self.stats["last_error"] = str(e)[:120]
            emit(
                f'{ce("💥")} <b>RUN CRASHED</b>\n'
                f'{ce("⚠️")} <i>{_esc(str(e)[:120])}</i>'
            )
        finally:
            if not self._stop_event.is_set():
                self.stats["status"] = "DONE"
            else:
                self.stats["status"] = "STOPPED"
            if on_step is None:
                emit(
                    f'{ce("🏁")} <b>RUN FINISHED</b>\n'
                    f"{_LINE}\n"
                    f'{ce("✅")} Success  <b>{self.stats["successful"]}</b>\n'
                    f'{ce("❌")} Failed   <b>{self.stats["failed"]}</b>\n'
                    f'{ce("📨")} Total    <b>{self.stats["total_submissions"]}</b>\n'
                    f'{ce("⏱️")} Runtime  <b>{self.runtime_str()}</b>'
                )

