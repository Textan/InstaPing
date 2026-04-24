# ── IMPORTS ───────────────────────────────────────────────────────────────────
import asyncio
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from fastapi import FastAPI
import uvicorn
# ──────────────────────────────────────────────────────────────────────────────

# ── CONFIG ────────────────────────────────────────────────────────────────────
USERNAME          = os.getenv("IG_USERNAME")
PASSWORD          = os.getenv("IG_PASSWORD")
BARK_TOKEN        = os.getenv("BARK_TOKEN")
BARK_SERVER       = os.getenv("BARK_SERVER", "https://api.day.app")

ACCOUNTS_RAW      = os.getenv("ACCOUNTS_TO_WATCH", "")
ACCOUNTS_TO_WATCH = [acc.strip() for acc in ACCOUNTS_RAW.split(",") if acc.strip()]

CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "60"))
HEADLESS          = os.getenv("HEADLESS", "True").lower() == "true"
TIMEOUT_RELOAD    = int(os.getenv("TIMEOUT_RELOAD", "3600"))
SESSION_FILE      = Path(os.getenv("SESSION_PATH", "ig_session.json"))
STATE_FILE        = Path(os.getenv("STATE_PATH", "ig_state.json"))
LOG_FILE          = Path(os.getenv("LOG_PATH", "ig_monitor.log"))

if not USERNAME or not PASSWORD:
    print("ERROR: IG_USERNAME and IG_PASSWORD environment variables are required!")
    sys.exit(1)

if not ACCOUNTS_TO_WATCH:
    print("ERROR: ACCOUNTS_TO_WATCH environment variable is required (comma-separated)!")
    sys.exit(1)
# ──────────────────────────────────────────────────────────────────────────────

# ── LOGGING ───────────────────────────────────────────────────────────────────
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _file_handler,
    ],
)
log = logging.getLogger("ig_monitor")
# ─────────────────────────────────────────────────────────────────────────────

# ── MONITOR STATE ─────────────────────────────────────────────────────────────
_monitor_healthy    = True
_monitor_last_check: datetime | None = None
# ─────────────────────────────────────────────────────────────────────────────

# ── NOTIFICATIONS ────────────────────────────────────────────────────────────
def notify(title: str, message: str, sound: str = "alert", retries: int = 3):
    log.info(f"NOTIFY | {title} | {message}")

    if not BARK_TOKEN:
        log.warning("BARK_TOKEN not set, notification skipped")
        return

    title_safe = quote(title, safe="")
    msg_safe   = quote(message, safe="")
    url        = f"{BARK_SERVER}/{BARK_TOKEN}/{title_safe}/{msg_safe}?sound={sound}"

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code in (200, 201):
                return
            log.warning(f"Bark returned {resp.status_code}: {resp.text[:100]} (attempt {attempt}/{retries})")
        except requests.Timeout:
            log.warning(f"Bark notification timeout (attempt {attempt}/{retries})")
        except Exception as e:
            log.warning(f"Bark notify failed: {type(e).__name__}: {e} (attempt {attempt}/{retries})")
        if attempt < retries:
            time.sleep(2)
# ─────────────────────────────────────────────────────────────────────────────

# ── STATE PERSISTENCE ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            log.info(f"Loaded state: {len(data.get('posts', []))} posts, "
                     f"{len(data.get('stories', []))} stories, "
                     f"{len(data.get('dm_senders', []))} DM senders")
            return data
        except json.JSONDecodeError as e:
            log.warning(f"Corrupted state file: {e}, starting fresh")
    return {"posts": [], "stories": [], "dm_senders": []}

def save_state(state: dict):
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error(f"Failed to save state: {e}")
# ─────────────────────────────────────────────────────────────────────────────

# ── BROWSER HELPERS ───────────────────────────────────────────────────────────
_BLOCKED_PATTERNS = [
    "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
    "**googletagmanager**",
    "**google-analytics**",
    "**doubleclick**",
    "**facebook.com/tr*",
]

_POPUP_SELECTORS = [
    'button:has-text("Not Now")',
    'button:has-text("Later")',
    'button:has-text("Dismiss")',
    'button[aria-label*="Close"]',
]

async def dismiss_popups(page, max_attempts: int = 5):
    for _ in range(max_attempts):
        dismissed = False
        for selector in _POPUP_SELECTORS:
            try:
                btn = page.locator(selector)
                if await btn.count() > 0:
                    await btn.first.click(timeout=1500)
                    await asyncio.sleep(0.4)
                    dismissed = True
                    break
            except Exception as e:
                log.debug(f"Popup selector '{selector}' failed: {e}")
        if not dismissed:
            break

async def safe_goto(page, url: str, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            return True
        except PWTimeout:
            if attempt < retries:
                log.warning(f"Timeout loading {url}, retry {attempt + 1}/{retries}...")
                await asyncio.sleep(3)
            else:
                log.error(f"Failed to load {url} after {retries + 1} attempts")
                return False
        except Exception as e:
            if attempt < retries:
                log.warning(f"Error loading {url}: {e}, retrying...")
                await asyncio.sleep(3)
            else:
                log.error(f"Failed to load {url}: {e}")
                return False
    return False

async def is_session_valid(page) -> bool:
    try:
        is_login_page   = "login" in page.url.lower()
        has_login_input = await page.query_selector('input[name="username"]') is not None
        return not (is_login_page or has_login_input)
    except Exception as e:
        log.error(f"Session validity check failed: {e}")
        return False

async def is_logged_in(page) -> bool:
    try:
        if "instagram.com" not in page.url:
            await safe_goto(page, "https://www.instagram.com/")
            await asyncio.sleep(1)
        return await is_session_valid(page)
    except Exception as e:
        log.error(f"is_logged_in check failed: {e}")
        return False

async def login(page, context):
    log.info("Starting login flow...")

    if not await safe_goto(page, "https://www.instagram.com/accounts/login/"):
        raise RuntimeError("Could not reach login page")
    try:
        await page.wait_for_selector('input[name="username"]', timeout=15000)

        log.info("Filling credentials...")
        await page.fill('input[name="username"]', USERNAME)
        await asyncio.sleep(0.5)
        await page.fill('input[name="password"]', PASSWORD)

        await page.click('button[type="submit"]')

        log.info("Waiting for authentication...")
        await page.wait_for_url("https://www.instagram.com/**", timeout=30000)

        await asyncio.sleep(2)
        await dismiss_popups(page)

        storage = await context.storage_state()
        SESSION_FILE.write_text(json.dumps(storage, indent=2))
        log.info("✅ Login successful and session saved")

    except PWTimeout as e:
        raise RuntimeError(f"Login timeout - check credentials, 2FA, or CAPTCHA: {e}")
    except Exception as e:
        raise RuntimeError(f"Login failed: {type(e).__name__}: {e}")

async def ensure_logged_in(page, context) -> bool:
    if not await is_session_valid(page):
        log.warning("Session expired mid-run, re-logging in...")
        try:
            await login(page, context)
            return True
        except Exception as e:
            log.error(f"Re-login failed: {e}")
            return False
    return True
# ─────────────────────────────────────────────────────────────────────────────

# ── POST / REEL EXTRACTION ────────────────────────────────────────────────────
_POST_HREF_JS = """
() => {
    const hrefs = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        const h = a.getAttribute('href');
        if (h && (h.includes('/p/') || h.includes('/reel/'))) {
            const clean = h.split('?')[0].replace(/\\/$/, '');
            hrefs.add(clean);
        }
    });
    return Array.from(hrefs);
}
"""

_SHARED_DATA_JS = """
() => {
    try {
        const scripts = document.querySelectorAll('script[type="application/json"]');
        const hrefs = new Set();
        scripts.forEach(s => {
            const text = s.textContent || '';
            const matches = text.matchAll(/"shortcode":"([^"]+)"/g);
            for (const m of matches) hrefs.add('/p/' + m[1] + '/');
            const reelMatches = text.matchAll(/"code":"([^"]+)"/g);
            for (const m of reelMatches) {
                if (text.includes('"product_type":"clips"') || text.includes('"__typename":"XDTMediaDict"')) {
                    hrefs.add('/reel/' + m[1] + '/');
                }
            }
        });
        return Array.from(hrefs);
    } catch(e) { return []; }
}
"""

async def extract_post_hrefs(page, account: str) -> list[str]:
    hrefs: set[str] = set()

    dom_hrefs = await page.evaluate(_POST_HREF_JS)
    for h in dom_hrefs:
        if h:
            hrefs.add(h)
    log.info(f"[{account}] DOM strategy: {len(dom_hrefs)} links")

    try:
        sd_hrefs = await page.evaluate(_SHARED_DATA_JS)
        for h in sd_hrefs:
            if h:
                hrefs.add(h)
        if sd_hrefs:
            log.info(f"[{account}] SharedData strategy: {len(sd_hrefs)} additional links")
    except Exception as e:
        log.debug(f"[{account}] SharedData extraction failed: {e}")

    result = list(hrefs)
    log.info(f"[{account}] Total unique post links: {len(result)}")
    return result
# ─────────────────────────────────────────────────────────────────────────────

# ── CHECKERS ─────────────────────────────────────────────────────────────────
async def check_dms(page, context, seen: set) -> bool:
    try:
        if not await safe_goto(page, "https://www.instagram.com/direct/inbox/"):
            return False

        if not await ensure_logged_in(page, context):
            return False

        await asyncio.sleep(2)

        title = await page.title()
        log.debug(f"DM page title: {title}")
        unread_count = 0
        title_match = re.search(r'\((\d+)\)', title)
        if title_match:
            unread_count = int(title_match.group(1))
            log.info(f"Page title indicates {unread_count} unread message(s)")

        try:
            await page.wait_for_selector(
                'div[role="listitem"], div[role="row"]',
                timeout=8000, state="attached"
            )
        except PWTimeout:
            log.warning("DM inbox: timed out waiting for threads")
            if unread_count == 0:
                return True
            return False

        threads = await page.query_selector_all('div[role="listitem"], div[role="row"]')
        log.debug(f"Found {len(threads)} DM threads")

        for thread in threads[:20]:
            try:
                name_el = await thread.query_selector(
                    'span[dir="auto"], '
                    'span[class*="username"], '
                    'strong'
                )
                if not name_el:
                    continue

                name = (await name_el.inner_text()).strip()
                if not name:
                    continue

                preview_el = await thread.query_selector(
                    'span[dir="auto"]:last-child, '
                    'div[dir="auto"]:last-child'
                )
                preview = ""
                if preview_el:
                    try:
                        preview = (await preview_el.inner_text()).strip()
                    except Exception:
                        pass

                dm_key = f"{name}:{preview[:40]}" if preview else name

                is_unread = False
                unread_el = await thread.query_selector(
                    'span[style*="font-weight: 700"], '
                    'span[style*="font-weight:700"], '
                    '[aria-label*="unread"], '
                    'svg[aria-label*="unread"]'
                )
                if unread_el:
                    is_unread = True

                bold_spans = await thread.query_selector_all('span[style*="font-weight"]')
                for span in bold_spans:
                    try:
                        style = await span.get_attribute("style") or ""
                        if "700" in style or "bold" in style.lower():
                            is_unread = True
                            break
                    except Exception:
                        pass

                if unread_count > 0:
                    is_unread = True

                if is_unread and dm_key not in seen:
                    seen.add(dm_key)
                    log.info(f"New DM from: {name}")
                    notify("New DM 💬", f"Message from {name}", sound="alert")

            except Exception as e:
                log.debug(f"Error parsing DM thread: {e}")
                continue

        return True

    except Exception as e:
        log.error(f"DM check failed: {e}")
        return False

async def check_account(page, context, account: str, seen_posts: set, seen_stories: set) -> bool:
    try:
        if not await safe_goto(page, f"https://www.instagram.com/{account}/"):
            return False

        if not await ensure_logged_in(page, context):
            return False

        try:
            await page.wait_for_selector(
                'header, section, article, main',
                timeout=10000, state="attached"
            )
        except PWTimeout:
            log.warning(f"[{account}] Profile page structure did not load in time")

        await asyncio.sleep(1)

        story_key   = f"{account}:story"
        story_el    = await page.query_selector(
            f'a[href*="/stories/{account}/"], '
            'canvas[aria-label*="story"], '
            'div[role="button"] canvas, '
            'header a canvas'
        )
        if story_el and story_key not in seen_stories:
            seen_stories.add(story_key)
            log.info(f"New story from {account}")
            notify("New Story 📸", f"{account} posted a story")
        elif not story_el and story_key in seen_stories:
            seen_stories.discard(story_key)
            log.debug(f"[{account}] Story expired, key cleared")

        hrefs = await extract_post_hrefs(page, account)

        if not hrefs:
            log.warning(f"[{account}] No post links found, waiting 5s and retrying...")
            await asyncio.sleep(5)
            if not await safe_goto(page, f"https://www.instagram.com/{account}/"):
                return False
            try:
                await page.wait_for_selector(
                    'header, section, article, main',
                    timeout=10000, state="attached"
                )
            except PWTimeout:
                pass
            await asyncio.sleep(2)
            hrefs = await extract_post_hrefs(page, account)

        if not hrefs:
            log.warning(f"[{account}] Still no post links after retry — skipping cycle")
            return False

        new_hrefs = [h for h in hrefs if f"{account}:{h}" not in seen_posts]
        for href in new_hrefs:
            seen_posts.add(f"{account}:{href}")
            is_reel      = "/reel/" in href
            content_type = "Reel 🎬" if is_reel else "Post 🖼️"
            log.info(f"New {content_type} from {account}: {href}")
            notify(f"New {content_type}", f"{account} posted new content")

        if not new_hrefs:
            log.debug(f"[{account}] No new posts (checked {len(hrefs)} links)")

        return True

    except Exception as e:
        log.error(f"Account check failed for {account}: {e}")
        return False
# ─────────────────────────────────────────────────────────────────────────────

# ── BROWSER FACTORY ──────────────────────────────────────────────────────────
async def make_browser_and_page(pw, storage=None):
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ],
    )

    context = await browser.new_context(
        storage_state=storage,
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = await context.new_page()

    async def _block(route):
        await route.abort()

    for pattern in _BLOCKED_PATTERNS:
        await page.route(pattern, _block)

    page.set_default_timeout(15000)

    return browser, context, page
# ─────────────────────────────────────────────────────────────────────────────

# ── MAIN MONITORING LOOP ─────────────────────────────────────────────────────
async def run():
    global _monitor_healthy, _monitor_last_check

    state        = load_state()
    seen_posts   = set(state["posts"])
    seen_stories = set(state["stories"])
    seen_dms     = set(state["dm_senders"])

    log.info(f"🚀 Starting IG Monitor with {len(ACCOUNTS_TO_WATCH)} accounts")
    log.info(f"Accounts: {', '.join(ACCOUNTS_TO_WATCH)}")
    log.info(f"Check interval: {CHECK_INTERVAL}s")

    async with async_playwright() as pw:
        storage = None
        if SESSION_FILE.exists():
            try:
                storage = json.loads(SESSION_FILE.read_text())
                log.info("✅ Loaded saved session")
            except json.JSONDecodeError:
                log.warning("⚠️  Corrupted session file, will re-login")

        browser, context, page = await make_browser_and_page(pw, storage)
        last_reload        = datetime.now()
        consecutive_errors = 0

        try:
            if not await is_logged_in(page):
                await login(page, context)
                storage = json.loads(SESSION_FILE.read_text())
            else:
                log.info("✅ Already logged in")
                await dismiss_popups(page)

            log.info("📋 Seeding initial state...")
            for account in ACCOUNTS_TO_WATCH:
                await check_account(page, context, account, seen_posts, seen_stories)
            await check_dms(page, context, seen_dms)
            save_state({
                "posts":      list(seen_posts),
                "stories":    list(seen_stories),
                "dm_senders": list(seen_dms),
            })

            log.info("✅ Monitoring started!")
            notify("IG Monitor ✅", "Monitoring is now active")

            while True:
                await asyncio.sleep(CHECK_INTERVAL)

                if (datetime.now() - last_reload).total_seconds() > TIMEOUT_RELOAD:
                    log.info("♻️  Reloading browser (periodic refresh)...")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    browser, context, page = await make_browser_and_page(pw, storage)
                    if not await is_logged_in(page):
                        await login(page, context)
                        storage = json.loads(SESSION_FILE.read_text())
                    else:
                        await dismiss_popups(page)
                    last_reload = datetime.now()

                log.info(f"🔍 Checking [{datetime.now().strftime('%H:%M:%S')}]...")

                cycle_errors = 0
                for account in ACCOUNTS_TO_WATCH:
                    try:
                        if not await check_account(page, context, account, seen_posts, seen_stories):
                            cycle_errors += 1
                    except Exception as e:
                        cycle_errors += 1
                        log.error(f"[{account}] Unhandled error: {type(e).__name__}: {e}")

                try:
                    if not await check_dms(page, context, seen_dms):
                        cycle_errors += 1
                except Exception as e:
                    cycle_errors += 1
                    log.error(f"DM check unhandled error: {type(e).__name__}: {e}")

                save_state({
                    "posts":      list(seen_posts),
                    "stories":    list(seen_stories),
                    "dm_senders": list(seen_dms),
                })

                _monitor_last_check = datetime.now()

                if cycle_errors == 0:
                    consecutive_errors = 0
                    log.debug("✅ Cycle complete")
                else:
                    consecutive_errors += 1
                    log.warning(f"⚠️ Cycle finished with {cycle_errors} error(s) — consecutive: {consecutive_errors}")

                if consecutive_errors >= 5:
                    log.critical("🔴 5 consecutive error cycles — restarting browser")
                    notify("IG Monitor ⚠️", "Restarting due to repeated errors")
                    try:
                        await browser.close()
                    except Exception:
                        pass

                    browser, context, page = await make_browser_and_page(pw, storage)
                    if not await is_logged_in(page):
                        await login(page, context)
                        storage = json.loads(SESSION_FILE.read_text())
                    else:
                        await dismiss_popups(page)

                    consecutive_errors = 0
                    last_reload        = datetime.now()

        finally:
            _monitor_healthy = False
            try:
                await browser.close()
            except Exception:
                pass
            log.info("Browser closed")
# ─────────────────────────────────────────────────────────────────────────────

# ── FASTAPI SERVER ───────────────────────────────────────────────────────────
app = FastAPI(title="IG Monitor", version="1.0")

@app.get("/")
def read_root():
    return {
        "status":     "IG Monitor is Active",
        "time":       datetime.now().isoformat(),
        "accounts":   ACCOUNTS_TO_WATCH,
        "healthy":    _monitor_healthy,
        "last_check": _monitor_last_check.isoformat() if _monitor_last_check else None,
    }

@app.get("/status")
def get_status():
    try:
        state = load_state()
        return {
            "status":          "running" if _monitor_healthy else "crashed",
            "healthy":         _monitor_healthy,
            "last_check":      _monitor_last_check.isoformat() if _monitor_last_check else None,
            "posts_tracked":   len(state.get("posts", [])),
            "stories_tracked": len(state.get("stories", [])),
            "dms_tracked":     len(state.get("dm_senders", [])),
            "timestamp":       datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
# ─────────────────────────────────────────────────────────────────────────────

# ── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def run_monitor():
        global _monitor_healthy
        while True:
            _monitor_healthy = True
            try:
                asyncio.run(run())
            except KeyboardInterrupt:
                log.info("Monitor stopped by user")
                break
            except Exception as e:
                log.critical(f"Monitor crashed: {e}")
                notify("IG Monitor 🔴", "Monitor crashed — restarting in 30s")
                _monitor_healthy = False
                time.sleep(30)

    monitor_thread = threading.Thread(target=run_monitor, daemon=True)
    monitor_thread.start()

    log.info("Starting FastAPI server on 0.0.0.0:7860")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")
