"""
Core browser automation for LC Waikiki RU registration.

Ported from the original CLI bot.py. The anti-detect logic (stealth JS,
fingerprint pools, proxy handling) is preserved verbatim. The CLI/rich
output and global stats have been replaced with a `log` callback and
structured return values so the logic can be driven by the Telegram bot.
"""

import asyncio
import os
import random
import shutil
import string
import tempfile

from playwright.async_api import TimeoutError as PlaywrightTimeout
from faker import Faker

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
TARGET_URL = "https://www.lcwaikiki.ru/registratsiya"
FORM_TIMEOUT = 15000       # ms
TYPE_DELAY_MIN = 50        # ms between keystrokes (human-like min)
TYPE_DELAY_MAX = 150       # ms between keystrokes (human-like max)
PAGE_LOAD_WAIT = 1         # seconds after page load (give time to settle)
POPUP_CONFIRM_WAIT = 4     # seconds to wait for popup/timer to appear
PROXY_MAX_RETRIES = 5      # total connection attempts before giving up on a proxy
PROXY_RETRY_DELAY = 2      # seconds to wait between proxy retry attempts

# Single speed knob for the whole run. It scales every "human-like" thinking
# pause (human_delay) and every keystroke gap (human_type). 1.0 = original
# cautious pace; lower = faster but less human-like (slightly higher chance the
# site flags the traffic). 0.5 ~halves the per-number time.
SPEED_FACTOR = 0.5
BLOCK_HEAVY_RESOURCES = True  # block images/fonts/media/analytics for speed

# Browser launch settings. On a headless server we must use the bundled
# Chromium in headless mode (the original used real Chrome with a display).
# Both are overridable via environment variables.
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "").strip() or None

FIXED_PASSWORD = os.environ.get("FIXED_PASSWORD", "Hadii@2024")

EMAIL_DOMAINS = [
    "hadiipro.pw",
    "mailinator.com",
    "guerrillamail.com",
    "tempmail.net",
    "yopmail.com",
    "sharklasers.com",
    "guerrillamailblock.com",
    "grr.la",
    "dispostable.com",
    "mailnesia.com",
    "trashmail.me",
    "tempail.com",
    "mohmal.com",
    "emailondeck.com",
    "throwaway.email",
]

# ═══════════════════════════════════════════════════════════════
#  ANTI-DETECT POOLS
# ═══════════════════════════════════════════════════════════════
USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

SCREEN_SIZES = [
  {"width": 1920, "height": 1080},
  {"width": 1366, "height": 768},
  {"width": 1536, "height": 864},
  {"width": 1440, "height": 900},
  {"width": 1600, "height": 900},
  {"width": 1680, "height": 1050},
]

TIMEZONES = [
  {"timezone_id": "Europe/Moscow", "locale": "ru-RU"},
  {"timezone_id": "Europe/Samara", "locale": "ru-RU"},
  {"timezone_id": "Asia/Yekaterinburg", "locale": "ru-RU"},
  {"timezone_id": "Asia/Novosibirsk", "locale": "ru-RU"},
]

# ═══════════════════════════════════════════════════════════════
#  STEALTH SCRIPT
# ═══════════════════════════════════════════════════════════════
STEALTH_JS = """
() => {
  // ═══ 1. WEBDRIVER FLAG ═══
  Object.defineProperty(navigator, 'webdriver', { get: () => false });
  try { delete navigator.__proto__.webdriver; } catch(e) {}
  
  // Hide the webdriver from all frames
  const origDesc = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
  if (origDesc) {
      Object.defineProperty(Navigator.prototype, 'webdriver', {
          get: () => false,
          configurable: true,
      });
  }

  // ═══ 2. CHROME RUNTIME ═══
  if (!window.chrome) window.chrome = {};
  window.chrome.runtime = {
      connect: function() { return { onMessage: { addListener: function(){} }, postMessage: function(){}, disconnect: function(){} }; },
      sendMessage: function() {},
      onMessage: { addListener: function(){}, removeListener: function(){} },
      onConnect: { addListener: function(){}, removeListener: function(){} },
      getManifest: function() { return {}; },
      id: undefined,
  };
  window.chrome.app = {
      isInstalled: false,
      InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
      RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
      getDetails: function() { return null; },
      getIsInstalled: function() { return false; },
      installState: function(cb) { if (cb) cb('not_installed'); },
  };
  window.chrome.csi = function() { return { onloadT: Date.now(), startE: Date.now(), pageT: Math.random() * 1000 }; };
  window.chrome.loadTimes = function() {
      return {
          commitLoadTime: Date.now() / 1000,
          connectionInfo: 'h2',
          finishDocumentLoadTime: Date.now() / 1000,
          finishLoadTime: Date.now() / 1000,
          firstPaintAfterLoadTime: 0,
          firstPaintTime: Date.now() / 1000,
          navigationType: 'Other',
          npnNegotiatedProtocol: 'h2',
          requestTime: Date.now() / 1000,
          startLoadTime: Date.now() / 1000,
          wasAlternateProtocolAvailable: false,
          wasFetchedViaSpdy: true,
          wasNpnNegotiated: true,
      };
  };

  // ═══ 3. PLUGINS ═══
  Object.defineProperty(navigator, 'plugins', {
      get: () => {
          const makePlugin = (name, filename, desc) => {
              const p = { name, filename, description: desc, length: 1 };
              p[0] = { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
              return p;
          };
          const plugins = [
              makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format'),
              makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
              makePlugin('Native Client', 'internal-nacl-plugin', ''),
          ];
          plugins.refresh = function(){};
          Object.defineProperty(plugins, 'length', { get: () => 3 });
          return plugins;
      },
  });

  Object.defineProperty(navigator, 'mimeTypes', {
      get: () => {
          const mimes = [
              { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: { name: 'Chrome PDF Plugin' } },
          ];
          mimes.refresh = function(){};
          Object.defineProperty(mimes, 'length', { get: () => 1 });
          return mimes;
      },
  });

  // ═══ 4. LANGUAGES & PLATFORM ═══
  Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
  Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => [4, 8, 12, 16][Math.floor(Math.random()*4)] });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => [4, 8, 16][Math.floor(Math.random()*3)] });
  Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

  // ═══ 5. PERMISSIONS ═══
  const origQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (origQuery) {
      const origBind = Function.prototype.bind;
      window.navigator.permissions.query = (p) =>
          p && p.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : origQuery.call(navigator.permissions, p);
  }
  
  // Fix Notification.permission
  try {
      Object.defineProperty(Notification, 'permission', {
          get: () => 'default',
          configurable: true,
      });
  } catch(e) {}

  // ═══ 6. CANVAS FINGERPRINT ═══
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
      try {
          const ctx = this.getContext('2d');
          if (ctx && this.width > 0 && this.height > 0) {
              const d = ctx.getImageData(0, 0, this.width, this.height);
              for (let i = 0; i < Math.min(d.data.length, 100); i += 4) {
                  d.data[i] = d.data[i] ^ (Math.random() > 0.5 ? 1 : 0);
              }
              ctx.putImageData(d, 0, 0);
          }
      } catch(e) {}
      return origToDataURL.apply(this, arguments);
  };

  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function() {
      const data = origGetImageData.apply(this, arguments);
      for (let i = 0; i < Math.min(data.data.length, 40); i += 4) {
          data.data[i] = data.data[i] ^ (Math.random() > 0.5 ? 1 : 0);
      }
      return data;
  };

  // ═══ 7. WEBGL FINGERPRINT ═══
  const getParamOrig = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Google Inc. (NVIDIA)';
      if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return getParamOrig.call(this, p);
  };
  
  try {
      const getParamOrig2 = WebGL2RenderingContext.prototype.getParameter;
      WebGL2RenderingContext.prototype.getParameter = function(p) {
          if (p === 37445) return 'Google Inc. (NVIDIA)';
          if (p === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)';
          return getParamOrig2.call(this, p);
      };
  } catch(e) {}

  // ═══ 8. CDP (Chrome DevTools Protocol) LEAK ═══
  // Prevent detection of CDP by hiding Runtime.evaluate artifacts
  try {
      const origCall = Function.prototype.call;
      const origToString = Function.prototype.toString;
      
      // Make toString look natural for patched functions
      const patchedFns = new Set();
      const origFnToString = Function.prototype.toString;
      Function.prototype.toString = function() {
          if (patchedFns.has(this)) {
              return 'function ' + (this.name || '') + '() { [native code] }';
          }
          return origFnToString.call(this);
      };
      patchedFns.add(Function.prototype.toString);
  } catch(e) {}

  // ═══ 9. IFRAME CONTENTWINDOW ═══
  try {
      const origContentWindow = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
      if (origContentWindow) {
          Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
              get: function() {
                  const win = origContentWindow.get.call(this);
                  if (win) {
                      try {
                          Object.defineProperty(win.navigator, 'webdriver', { get: () => false });
                      } catch(e) {}
                  }
                  return win;
              },
          });
      }
  } catch(e) {}

  // ═══ 10. AUTOMATION-SPECIFIC PROPERTIES ═══
  // Remove Playwright/Puppeteer specific markers
  delete window.__playwright;
  delete window.__pw_manual;
  delete window.__PW_inspect;
  delete window._phantom;
  delete window.callPhantom;
  delete window.__nightmare;
  delete window.domAutomation;
  delete window.domAutomationController;
  delete window._Selenium_IDE_Recorder;
  delete window._selenium;
  delete window.__webdriver_script_fn;
  delete window.__driver_evaluate;
  delete window.__webdriver_evaluate;
  delete window.__fxdriver_evaluate;
  delete window.__fxdriver_unwrap;
  
  // ═══ 11. CONNECTION TYPE ═══
  try {
      Object.defineProperty(navigator, 'connection', {
          get: () => ({
              effectiveType: '4g',
              rtt: 50,
              downlink: 10,
              saveData: false,
          }),
      });
  } catch(e) {}

  // ═══ 12. SCREEN PROPERTIES ═══
  try {
      Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
      Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
  } catch(e) {}
}
"""

fake = Faker()

_temp_dirs = []


def _noop_log(icon, msg):
    pass


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════
def generate_random_email():
    domain = random.choice(EMAIL_DOMAINS)
    user = "hadii" + "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(5, 9)))
    return f"{user}@{domain}"


def get_random_ua():      return random.choice(USER_AGENTS)
def get_random_screen():  return random.choice(SCREEN_SIZES)
def get_random_tz():      return random.choice(TIMEZONES)


def create_temp_profile_dir():
    d = tempfile.mkdtemp(prefix="lcw_profile_")
    _temp_dirs.append(d)
    return d


def cleanup_profile_dir(d):
    try:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            return True
    except Exception:
        pass
    return False


def cleanup_all_temp_dirs():
    for d in list(_temp_dirs):
        cleanup_profile_dir(d)
    _temp_dirs.clear()


async def human_delay(min_ms=300, max_ms=800):
    """Random delay to simulate human thinking/reaction time."""
    delay = random.randint(min_ms, max_ms) / 1000.0 * SPEED_FACTOR
    await asyncio.sleep(delay)


async def human_type(locator, text):
    """Type text character by character with random human-like delays."""
    for char in text:
        await locator.press(char)
        delay = random.randint(TYPE_DELAY_MIN, TYPE_DELAY_MAX) * SPEED_FACTOR
        await locator.page.wait_for_timeout(delay)


# Visible-text phrases the site shows when registration hard-fails. Detecting
# one means the whole run should stop (mirrors the original sys.exit).
_HARD_ERROR_JS = """
    () => {
        const errorPhrases = [
            'Ошибка. Попробуйте снова.',
            'Ошибка. Попробуйте снова',
            'Регистрация не удалась',
        ];
        const candidates = document.querySelectorAll('span, p, small, label, div, li');
        for (const el of candidates) {
            const text = el.textContent ? el.textContent.trim() : '';
            if (text.length > 100) continue;
            if (text.length < 3) continue;
            const parent = el.closest('[class*="modal"], [class*="popup"], [class*="otp"], [class*="dialog"], [class*="overlay"], [class*="countdown"], [class*="timer"]');
            if (parent) continue;
            for (const phrase of errorPhrases) {
                if (text.includes(phrase)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        return text;
                    }
                }
            }
        }
        return '';
    }
"""


async def detect_hard_error(page) -> str:
    """Return the visible hard-error text if the site rejected registration, else ''."""
    try:
        return await page.evaluate(_HARD_ERROR_JS)
    except Exception:
        return ""


async def random_mouse_move(page):
    """Simulate random mouse movement to appear human."""
    try:
        x = random.randint(100, 800)
        y = random.randint(100, 500)
        await page.mouse.move(x, y)
        await human_delay(100, 300)
    except Exception:
        pass


def parse_proxies(raw_text):
    """Parse raw proxy.txt content into a list of proxy strings."""
    proxies = []
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxies.append(line)
    return proxies


def parse_phone_numbers(raw_text):
    """
    Parse raw text into a list of valid 10-digit RU phone numbers.
    Returns (valid_numbers, skipped_lines).
    """
    numbers = []
    skipped = []
    for line in (raw_text or "").splitlines():
        n = line.strip()
        if not n or n.startswith("#"):
            continue
        n = n.replace("+7", "").replace("+", "").replace(" ", "").replace("-", "")
        if n.startswith("7") and len(n) == 11:
            n = n[1:]
        if len(n) != 10 or not n.isdigit():
            skipped.append(line.strip())
            continue
        numbers.append(n)
    return numbers, skipped


# ═══════════════════════════════════════════════════════════════
#  PROXY PARSING
# ═══════════════════════════════════════════════════════════════
def parse_proxy(proxy_str, force_protocol=None):
  """
  Parse proxy string into Playwright proxy dict.
  Returns: {"server": "http://host:port", "username": ..., "password": ...}
  """
  proxy_str = proxy_str.strip()

  # Remove protocol prefix for parsing
  raw = proxy_str
  protocol = force_protocol or "http"
  if not force_protocol:
      if raw.startswith("http://"):
          raw = raw[7:]
          protocol = "http"
      elif raw.startswith("https://"):
          raw = raw[8:]
          protocol = "https"
      elif raw.startswith("socks5://"):
          raw = raw[9:]
          protocol = "socks5"
  else:
      # Strip any existing protocol
      for prefix in ["http://", "https://", "socks5://"]:
          if raw.startswith(prefix):
              raw = raw[len(prefix):]
              break

  username = None
  password = None

  # Format: user:pass@host:port
  if "@" in raw:
      creds, hostport = raw.rsplit("@", 1)
      if ":" in creds:
          username, password = creds.split(":", 1)
      server = f"{protocol}://{hostport}"
  else:
      parts = raw.split(":")
      if len(parts) == 4:
          # host:port:user:pass
          server = f"{protocol}://{parts[0]}:{parts[1]}"
          username = parts[2]
          password = parts[3]
      elif len(parts) == 2:
          # host:port
          server = f"{protocol}://{parts[0]}:{parts[1]}"
      else:
          server = f"{protocol}://{raw}"

  result = {"server": server}
  if username:
      result["username"] = username
  if password:
      result["password"] = password
  return result


def get_proxy_variants(proxy_str):
    """
    Returns a list of proxy dicts to try. Defaults to HTTP unless SOCKS5 is specified.
    """
    variants = []
    if proxy_str.lower().startswith("socks5://"):
        variants.append(parse_proxy(proxy_str, force_protocol="socks5"))
    elif proxy_str.lower().startswith("http://") or proxy_str.lower().startswith("https://"):
        variants.append(parse_proxy(proxy_str, force_protocol="http"))
    else:
        # Default to HTTP. SOCKS5 with auth is not supported by Chromium anyway.
        variants.append(parse_proxy(proxy_str, force_protocol="http"))
    return variants


# ═══════════════════════════════════════════════════════════════
#  CORE: Dismiss popup by clicking X / close button
# ═══════════════════════════════════════════════════════════════
async def dismiss_popup(page, session_id, log=_noop_log):
    """
    After Send OTP, a popup/modal appears with a timer.
    This function finds and clicks the X (close) button to dismiss it.
    """
    log("🔍", f"[S{session_id}] Looking for popup close (X) button...")

    close_selectors = [
        "button.modal__close",
        "button.popup__close",
        "button.close-button",
        "button.dialog__close",
        ".modal-close",
        ".popup-close",
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "button[aria-label='Закрыть']",
        "button[aria-label='закрыть']",
        "button:has(svg.icon-close)",
        "button:has(svg.close-icon)",
        "button:has(.icon-close)",
        ".modal .close",
        ".popup .close",
        ".overlay .close",
        "[class*='modal'] button:has([class*='close'])",
        "[class*='popup'] button:has([class*='close'])",
        "[class*='dialog'] button:has([class*='close'])",
        ".lcw-modal button.lcw-modal__close",
        ".otp-modal button",
        "[class*='otp'] button[class*='close']",
        "[class*='verification'] button[class*='close']",
        "button:has-text('×')",
        "button:has-text('✕')",
        "button:has-text('✖')",
    ]

    for sel in close_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                log("✕", f"[S{session_id}] Popup closed via: {sel}")
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue

    try:
        closed = await page.evaluate("""
            () => {
                const containers = document.querySelectorAll(
                    '[class*="modal"], [class*="popup"], [class*="dialog"], [class*="overlay"], [class*="otp"], [class*="verification"]'
                );
                for (const container of containers) {
                    const buttons = container.querySelectorAll('button, [role="button"], .close, [class*="close"]');
                    for (const btn of buttons) {
                        const text = (btn.innerText || '').trim();
                        const cls = (btn.className || '').toLowerCase();
                        const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (text === '×' || text === '✕' || text === '✖' || text === 'X' || text === 'x'
                            || cls.includes('close') || ariaLabel.includes('close')
                            || ariaLabel.includes('закрыть')
                            || btn.querySelector('svg[class*="close"]')
                            || btn.querySelector('[class*="close"]')) {
                            btn.click();
                            return true;
                        }
                    }
                }
                return false;
            }
        """)
        if closed:
            log("✕", f"[S{session_id}] Popup closed via JS fallback")
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    try:
        await page.keyboard.press("Escape")
        log("✕", f"[S{session_id}] Tried Escape key to close popup")
        await page.wait_for_timeout(500)
        return True
    except Exception:
        pass

    log("⚠️", f"[S{session_id}] Could not find popup close button")
    return False


# ═══════════════════════════════════════════════════════════════
#  CORE: Fill form and submit for one number (reuses open page)
# ═══════════════════════════════════════════════════════════════
async def fill_and_submit_number(page, phone_number, session_id, is_first_number, log=_noop_log):
    """
    Fill the registration form with a new phone number and submit.

    Returns a dict:
      {"status": "success"|"failed"|"error_stop", "detail": str, "email": str}
    "error_stop" means the site returned the hard registration error and the
    whole run should stop (mirrors the original sys.exit behaviour).
    """
    email = generate_random_email()

    try:
        if is_first_number:
            await random_mouse_move(page)
            await human_delay(500, 1000)

            log("📧", f"[S{session_id}] Email: {email}")
            email_loc = page.locator("#form-input-email").first
            await email_loc.wait_for(state="visible", timeout=FORM_TIMEOUT)
            await email_loc.click()
            await human_delay(200, 500)
            await human_type(email_loc, email)

            await human_delay(300, 700)
            await random_mouse_move(page)

            log("🔑", f"[S{session_id}] Password set")
            pwd_loc = page.locator("#form-input-password").first
            await pwd_loc.click()
            await human_delay(200, 400)
            await human_type(pwd_loc, FIXED_PASSWORD)

            await human_delay(300, 700)
            await random_mouse_move(page)

            log("📞", f"[S{session_id}] Phone: +7{phone_number}")
            phone_loc = page.locator("input.phone-field__number-input").first
            await phone_loc.wait_for(state="visible", timeout=FORM_TIMEOUT)
            await phone_loc.click()
            await human_delay(200, 400)
            await phone_loc.press("Control+a")
            await human_delay(100, 200)
            await phone_loc.press("Backspace")
            await human_delay(200, 400)
            await human_type(phone_loc, phone_number)

            await human_delay(500, 1000)

            try:
                cb_buttons = page.locator("button.checkbox__button")
                n = await cb_buttons.count()
                if n >= 3:
                    terms_btn = cb_buttons.nth(n - 1)
                    is_checked = await terms_btn.evaluate(
                        "el => el.getAttribute('aria-checked') === 'true' || (el.parentElement && el.parentElement.className.includes('checked'))"
                    )
                    if not is_checked:
                        await random_mouse_move(page)
                        await human_delay(200, 500)
                        await terms_btn.click()
                    log("☑️", f"[S{session_id}] Terms checked")

                    sms_btn = cb_buttons.nth(1)
                    sms_checked = await sms_btn.evaluate(
                        "el => el.getAttribute('aria-checked') === 'true' || (el.parentElement && el.parentElement.className.includes('checked'))"
                    )
                    if not sms_checked:
                        await human_delay(200, 400)
                        await sms_btn.click()
                    log("📱", f"[S{session_id}] SMS checked")
                else:
                    log("⚠️", f"[S{session_id}] Only {n} checkbox buttons found")
            except Exception as e:
                log("⚠️", f"[S{session_id}] Checkbox issue: {str(e)[:50]}")

        else:
            await random_mouse_move(page)
            await human_delay(300, 600)

            log("📞", f"[S{session_id}] Changing phone to: +7{phone_number}")
            phone_loc = page.locator("input.phone-field__number-input").first
            await phone_loc.wait_for(state="visible", timeout=FORM_TIMEOUT)
            await phone_loc.click()
            await human_delay(200, 400)
            await phone_loc.press("Control+a")
            await human_delay(100, 200)
            await phone_loc.press("Backspace")
            await human_delay(200, 400)
            await human_type(phone_loc, phone_number)

            new_email = generate_random_email()
            email = new_email
            log("📧", f"[S{session_id}] New email: {new_email}")
            email_loc = page.locator("#form-input-email").first
            try:
                await human_delay(300, 600)
                await email_loc.click()
                await human_delay(200, 300)
                await email_loc.press("Control+a")
                await human_delay(100, 200)
                await email_loc.press("Backspace")
                await human_delay(200, 400)
                await human_type(email_loc, new_email)
            except Exception:
                try:
                    await email_loc.fill(new_email)
                except Exception:
                    log("⚠️", f"[S{session_id}] Could not update email field")

        await human_delay(500, 1000)
        await random_mouse_move(page)

        log("📨", f"[S{session_id}] Clicking Send OTP / Регистрация for +7{phone_number}")
        clicked = False
        for sel in [
            "button.access-panel-button:has-text('Регистрация')",
            "button.lcw-button--primary:has-text('Регистрация')",
            "button:has-text('Регистрация')",
            "button:has-text('Отправить код')",
            "button:has-text('Получить код')",
            "button:has-text('Send OTP')",
        ]:
            try:
                b = page.locator(sel).first
                if await b.is_visible(timeout=2000):
                    await b.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            await page.evaluate("""
                const btns = [...document.querySelectorAll('button')];
                const t = btns.find(b => (b.innerText||'').trim() === 'Регистрация');
                if (t) t.click();
            """)
        log("✓", f"[S{session_id}] Submitted for +7{phone_number}")

        await page.wait_for_timeout(1200)

        detected_error_text = await detect_hard_error(page)

        if detected_error_text:
            log("⚠️", f"[S{session_id}] ERROR DETECTED: '{detected_error_text}' for +7{phone_number}")
            return {"status": "error_stop", "detail": detected_error_text, "email": email}

        log("⏳", f"[S{session_id}] Waiting for timer popup...")
        try:
            popup_loc = page.locator(
                "text=/\\d{1,2}:\\d{2}/"
                ", input[autocomplete='one-time-code']"
                ", input[placeholder*='код' i]"
                ", [class*='otp' i]"
                ", [class*='timer' i]"
                ", [class*='countdown' i]"
            ).first
            await popup_loc.wait_for(state="visible", timeout=POPUP_CONFIRM_WAIT * 1000)
            log("⏱️", f"[S{session_id}] Timer/OTP popup detected for +7{phone_number}")
        except Exception:
            # Already waited the full POPUP_CONFIRM_WAIT above; just a short
            # grace pause so the SMS has a moment to fire, then move on.
            await asyncio.sleep(1.5)
            # The post-submit settle was trimmed for speed, so re-scan here in
            # case the hard error rendered late — otherwise a genuine failure
            # would be recorded as success and the run would not stop.
            late_error = await detect_hard_error(page)
            if late_error:
                log("⚠️", f"[S{session_id}] ERROR DETECTED (late): '{late_error}' for +7{phone_number}")
                return {"status": "error_stop", "detail": late_error, "email": email}
            log("ℹ️", f"[S{session_id}] No timer popup detected (submitted anyway)")

        log("✅", f"[S{session_id}] OTP sent for +7{phone_number}")
        return {"status": "success", "detail": "OTP sent", "email": email}

    except PlaywrightTimeout as e:
        log("❌", f"[S{session_id}] TIMEOUT +7{phone_number}: {str(e)[:60]}")
        return {"status": "failed", "detail": f"Timeout: {str(e)[:60]}", "email": email}
    except Exception as e:
        log("❌", f"[S{session_id}] ERROR +7{phone_number}: {str(e)[:60]}")
        return {"status": "failed", "detail": str(e)[:80], "email": email}


# ═══════════════════════════════════════════════════════════════
#  CORE: Process a batch of numbers in one browser session
# ═══════════════════════════════════════════════════════════════
async def process_session(numbers_batch, pw_instance, session_id, proxy_str,
                          on_result, log=_noop_log, should_stop=None):
    """
    Open ONE browser, process up to len(numbers_batch) numbers in it.

    For each number, calls `await on_result(phone, result_dict)`.
    Returns True if the run should stop entirely (hard error detected).
    `should_stop()` is an optional callable returning True to abort early.
    """
    def _stop():
        return bool(should_stop and should_stop())

    context = None
    profile_dir = None
    ua = get_random_ua()
    screen = get_random_screen()
    tz = get_random_tz()

    proxy_variants = get_proxy_variants(proxy_str) if proxy_str else []

    try:
        profile_dir = create_temp_profile_dir()
        log("🛡️", f"[S{session_id}] Profile dir: ...{profile_dir[-16:]}")

        base_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-popup-blocking",
            "--disable-sync",
            "--mute-audio",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            f"--window-size={screen['width']},{screen['height']}",
        ]

        base_opts = dict(
            user_data_dir=profile_dir,
            headless=HEADLESS,
            viewport=screen,
            user_agent=ua,
            locale=tz["locale"],
            timezone_id=tz["timezone_id"],
            java_script_enabled=True,
            ignore_https_errors=True,
            args=base_args,
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
                "Dnt": "1",
            },
        )
        if BROWSER_CHANNEL:
            base_opts["channel"] = BROWSER_CHANNEL

        async def _setup_blocking(ctx):
            if not BLOCK_HEAVY_RESOURCES:
                return
            blocked_types = {"image", "media", "font"}
            blocked_hosts = (
                "google-analytics.com", "googletagmanager.com", "doubleclick.net",
                "facebook.net", "facebook.com", "hotjar.com", "clarity.ms",
                "yandex.ru/metrika", "mc.yandex.ru", "criteo.com", "bing.com",
                "adsrvr.org", "adservice.google", "segment.io", "intercom.io",
            )

            async def _route(route):
                req = route.request
                if req.resource_type in blocked_types:
                    return await route.abort()
                if any(h in req.url for h in blocked_hosts):
                    return await route.abort()
                await route.continue_()

            await ctx.route("**/*", _route)

        page = None
        connected = False

        if proxy_variants:
            for attempt in range(PROXY_MAX_RETRIES):
                if _stop():
                    return False
                if attempt > 0:
                    log("🔄", f"[S{session_id}] Proxy retry {attempt}/{PROXY_MAX_RETRIES - 1} in {PROXY_RETRY_DELAY}s…")
                    await asyncio.sleep(PROXY_RETRY_DELAY)
                for proxy_dict in proxy_variants:
                    if _stop():
                        return False
                    proto_name = proxy_dict["server"].split("://")[0].upper()
                    log("🌐", f"[S{session_id}] Trying proxy ({proto_name}): {proxy_dict['server']}")

                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass
                        cleanup_profile_dir(profile_dir)
                        profile_dir = create_temp_profile_dir()
                        base_opts["user_data_dir"] = profile_dir

                    launch_opts = {**base_opts, "proxy": proxy_dict}
                    try:
                        log("🚀", f"[S{session_id}] Launching browser ({proto_name})...")
                        context = await pw_instance.chromium.launch_persistent_context(**launch_opts)
                        await context.add_init_script(STEALTH_JS)
                        page = context.pages[0] if context.pages else await context.new_page()
                        await _setup_blocking(context)

                        log("🌐", f"[S{session_id}] Opening {TARGET_URL} via {proto_name}...")
                        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(PAGE_LOAD_WAIT * 1000)

                        connected = True
                        log("✅", f"[S{session_id}] Connected via {proto_name} proxy!")
                        break
                    except Exception as e:
                        log("⚠️", f"[S{session_id}] {proto_name} proxy failed: {str(e)[:80]}")
                        if context:
                            try:
                                await context.close()
                                context = None
                            except Exception:
                                pass
                        continue
                if connected:
                    break

            if not connected:
                log("❌", f"[S{session_id}] Proxy failed after {PROXY_MAX_RETRIES} attempts!")
                for phone in numbers_batch:
                    await on_result(phone, {"status": "proxy_fail",
                                            "detail": f"Proxy connection failed after {PROXY_MAX_RETRIES} retries",
                                            "email": ""})
                return False
        else:
            log("🚀", f"[S{session_id}] Launching browser (NO PROXY)...")
            context = await pw_instance.chromium.launch_persistent_context(**base_opts)
            await context.add_init_script(STEALTH_JS)
            log("🛡️", f"[S{session_id}] Stealth injected")
            page = context.pages[0] if context.pages else await context.new_page()
            await _setup_blocking(context)

            log("🌐", f"[S{session_id}] Opening {TARGET_URL}")
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(PAGE_LOAD_WAIT * 1000)
            connected = True

        if not connected or not page:
            return False

        for sel in [
            "button:has-text('Anladım')",
            "button:has-text('Принять')",
            "button:has-text('Согласен')",
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept')",
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    log("🍪", f"[S{session_id}] Cookies accepted")
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                pass

        for i, phone_number in enumerate(numbers_batch):
            if _stop():
                return False

            is_first = (i == 0)
            num_label = f"{i + 1}/{len(numbers_batch)}"
            log("▶️", f"[S{session_id}] Number {num_label}: +7{phone_number}")

            if not is_first:
                log("✕", f"[S{session_id}] Dismissing popup from previous number...")
                await dismiss_popup(page, session_id, log)
                await page.wait_for_timeout(500)

            result = await fill_and_submit_number(page, phone_number, session_id, is_first, log)

            if result["status"] == "failed":
                log("🔄", f"[S{session_id}] Retrying with page reload...")
                try:
                    await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(PAGE_LOAD_WAIT * 1000)
                    for sel in [
                        "button:has-text('Anladım')",
                        "button:has-text('Принять')",
                        "#onetrust-accept-btn-handler",
                    ]:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=1000):
                                await btn.click()
                                break
                        except Exception:
                            pass
                    result = await fill_and_submit_number(page, phone_number, session_id, True, log)
                except Exception as retry_err:
                    log("❌", f"[S{session_id}] Retry also failed: {str(retry_err)[:50]}")

            await on_result(phone_number, result)

            if result["status"] == "error_stop":
                return False

        await page.wait_for_timeout(1000)
        return False

    except PlaywrightTimeout as e:
        log("❌", f"[S{session_id}] SESSION TIMEOUT: {str(e)[:60]}")
        return False
    except Exception as e:
        log("❌", f"[S{session_id}] SESSION ERROR: {str(e)[:60]}")
        return False
    finally:
        if context:
            try:
                await context.close()
                log("🔒", f"[S{session_id}] Browser closed")
            except Exception:
                pass
        if profile_dir:
            await asyncio.sleep(1)
            if cleanup_profile_dir(profile_dir):
                log("🗑️", f"[S{session_id}] Cache + data CLEARED")
