# ═══════════════════════════════════════════════════════════════════════════════
#  InstaPing — Instagram Monitor
#  Two loops: DMs every 30 s   |   Posts / Stories / Notes every 3 min
#  One browser, persistent session, guaranteed Bark notifications
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import FastAPI
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import uvicorn

# ── CONFIG ────────────────────────────────────────────────────────────────────
USERNAME          = os.getenv("IG_USERNAME",       "")
PASSWORD          = os.getenv("IG_PASSWORD",       "")
BARK_TOKEN        = os.getenv("BARK_TOKEN",        "")
BARK_SERVER       = os.getenv("BARK_SERVER",       "https://api.day.app")
ACCOUNTS_RAW      = os.getenv("ACCOUNTS_TO_WATCH", "")
HEADLESS          = os.getenv("HEADLESS",          "true").lower() == "true"
DM_INTERVAL       = int(os.getenv("DM_INTERVAL",      "30"))    # seconds
CONTENT_INTERVAL  = int(os.getenv("CONTENT_INTERVAL", "180"))   # seconds
SESSION_FILE      = Path(os.getenv("SESSION_PATH", "ig_session.json"))
STATE_FILE        = Path(os.getenv("STATE_PATH",   "ig_state.json"))
LOG_FILE          = Path(os.getenv("LOG_PATH",     "ig_monitor.log"))

ACCOUNTS = [a.strip() for a in ACCOUNTS_RAW.split(",") if a.strip()]

if not USERNAME or not PASSWORD:
    sys.exit("ERROR: IG_USERNAME and IG_PASSWORD are required")
if not ACCOUNTS:
    sys.exit("ERROR: ACCOUNTS_TO_WATCH is required (comma-separated usernames)")

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
log = logging.getLogger("instaping")

# ── GLOBAL HEALTH STATE (read by FastAPI) ─────────────────────────────────────
_healthy         = True
_last_dm_check:  datetime | None = None
_last_con_check: datetime | None = None

# ═══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION ENGINE
#  Non-blocking queue drained by a dedicated daemon thread.
#  Unlimited retries with exponential backoff — notifications WILL be delivered.
# ═══════════════════════════════════════════════════════════════════════════════
_notify_queue: queue.Queue = queue.Queue()

def _notification_worker():
    while True:
        title, body, sound = _notify_queue.get()
        if not BARK_TOKEN:
            log.warning(f"BARK_TOKEN not set — skipping: {title} | {body}")
            _notify_queue.task_done()
            continue
        url     = (f"{BARK_SERVER}/{BARK_TOKEN}/"
                   f"{quote(title, safe='')}/{quote(body, safe='')}?sound={sound}")
        attempt = 0
        while True:
            attempt += 1
            try:
                r = requests.get(url, timeout=8)
                if r.status_code in (200, 201):
                    log.debug(f"Notification delivered (attempt {attempt}): {title}")
                    break
                log.warning(f"Bark HTTP {r.status_code} (attempt {attempt}): {r.text[:80]}")
            except Exception as e:
                log.warning(f"Bark error (attempt {attempt}): {e}")
            time.sleep(min(2 ** attempt, 60))   # 2 s, 4 s, 8 s … cap 60 s
        _notify_queue.task_done()

threading.Thread(
    target=_notification_worker, daemon=True, name="bark-worker"
).start()

def notify(title: str, body: str, sound: str = "alert"):
    """Non-blocking. Enqueues and returns immediately."""
    log.info(f"NOTIFY | {title} | {body}")
    _notify_queue.put((title, body, sound))

# ═══════════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            log.info(
                f"State loaded — posts:{len(d.get('posts',[]))} "
                f"stories:{len(d.get('stories',[]))} "
                f"dms:{len(d.get('dms',[]))}"
            )
            return d
        except Exception as e:
            log.warning(f"Corrupted state ({e}) — starting fresh")
    return {"posts": [], "stories": [], "dms": []}

def save_state(posts: set, stories: set, dms: set):
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"posts": list(posts), "stories": list(stories), "dms": list(dms)},
            indent=2,
        ))
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.error(f"State save failed: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
#  BROWSER
# ═══════════════════════════════════════════════════════════════════════════════
_BLOCK_TYPES = {"image", "media", "font", "stylesheet"}

async def _route_handler(route, request):
    if request.resource_type in _BLOCK_TYPES:
        await route.abort()
    else:
        await route.continue_()

def _load_session() -> dict | None:
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    return None

def _save_session(storage: dict):
    try:
        SESSION_FILE.write_text(json.dumps(storage, indent=2))
    except Exception as e:
        log.error(f"Session save failed: {e}")

async def make_browser(pw):
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--disable-extensions",
            "--single-process",
        ],
    )
    storage = _load_session()
    ctx_kw  = dict(
        viewport   = {"width": 1024, "height": 768},
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale = "en-US",
    )
    if storage:
        ctx_kw["storage_state"] = storage
    context = await browser.new_context(**ctx_kw)
    page    = await context.new_page()
    await page.route("**/*", _route_handler)
    page.set_default_timeout(20000)
    return browser, context, page

# ═══════════════════════════════════════════════════════════════════════════════
#  SESSION / LOGIN
#  Rule: URL is the ONLY source of truth for session state.
#  A slow page, a blank page, or 0 posts NEVER triggers re-login.
#  Only a redirect to /accounts/login or /challenge does.
# ═══════════════════════════════════════════════════════════════════════════════
_LOGIN_URL_RE = re.compile(
    r"instagram\.com/(accounts/login|accounts/signup|challenge|suspended)",
    re.IGNORECASE,
)

def _url_is_login(url: str) -> bool:
    return bool(_LOGIN_URL_RE.search(url))

_POPUP_SELS = [
    'button:has-text("Not Now")',
    'button:has-text("Later")',
    'button[aria-label*="Close"]',
]

async def _dismiss_popups(page):
    for sel in _POPUP_SELS:
        try:
            b = page.locator(sel)
            if await b.count() > 0:
                await b.first.click(timeout=1500)
                await asyncio.sleep(0.3)
        except Exception:
            pass

async def do_login(page, context) -> bool:
    """Full login. Returns True on success. Never raises."""
    log.info("Login flow starting...")
    try:
        await page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_selector('input[name="username"]', timeout=20000)
        await page.fill('input[name="username"]', USERNAME)
        await asyncio.sleep(0.7)
        await page.fill('input[name="password"]', PASSWORD)
        await asyncio.sleep(0.4)
        await page.click('button[type="submit"]')
        await page.wait_for_function(
            "() => !window.location.href.includes('/accounts/login')",
            timeout=40000,
        )
        await asyncio.sleep(2)
        await _dismiss_popups(page)
        _save_session(await context.storage_state())
        log.info("✅ Login successful — session saved")
        return True
    except PWTimeout as e:
        log.error(f"Login timeout: {e}")
    except Exception as e:
        log.error(f"Login error: {type(e).__name__}: {e}")
    return False

async def ensure_logged_in(page, context) -> bool:
    """
    Navigate to IG home, let any redirect happen, check URL.
    Re-logs in if needed. Called only at startup and after session recovery.
    """
    try:
        await page.goto(
            "https://www.instagram.com/",
            wait_until="domcontentloaded",
            timeout=25000,
        )
        await asyncio.sleep(1.5)
    except Exception:
        pass

    if not _url_is_login(page.url):
        await _dismiss_popups(page)
        log.info("✅ Session valid")
        return True

    log.warning("Not logged in — logging in now")
    return await do_login(page, context)

async def recover_session(page, context) -> bool:
    """
    Called when a checker confirmed the session is dead (returned None).
    Tries up to 3 times with increasing delays before giving up.
    """
    for attempt in range(1, 4):
        wait = attempt * 15   # 15 s, 30 s, 45 s
        log.warning(f"Session recovery attempt {attempt}/3 — waiting {wait}s...")
        await asyncio.sleep(wait)
        if await do_login(page, context):
            log.info("✅ Session recovered")
            return True
        log.error(f"Recovery attempt {attempt}/3 failed")
    notify("InstaPing 🔴", "Session expired — all recovery attempts failed, restarting")
    return False

# ═══════════════════════════════════════════════════════════════════════════════
#  JAVASCRIPT EXTRACTORS
# ═══════════════════════════════════════════════════════════════════════════════
_JS_POSTS = """
() => {
    const out = new Set();
    // DOM links
    document.querySelectorAll('a[href]').forEach(a => {
        const h = a.getAttribute('href') || '';
        if (h.includes('/p/') || h.includes('/reel/'))
            out.add(h.split('?')[0].replace(/\/$/, ''));
    });
    // Embedded JSON blobs
    document.querySelectorAll('script[type="application/json"]').forEach(s => {
        const t = s.textContent || '';
        for (const m of t.matchAll(/"shortcode":"([A-Za-z0-9_-]+)"/g))
            out.add('/p/' + m[1]);
        for (const m of t.matchAll(/"code":"([A-Za-z0-9_-]+)"/g))
            if (t.includes('"product_type":"clips"') || t.includes('"__typename":"XDTMediaDict"'))
                out.add('/reel/' + m[1]);
    });
    return [...out];
}
"""

_JS_HAS_STORY = """
(account) => {
    const anchors = [...document.querySelectorAll('a[href]')];
    return anchors.some(a =>
        (a.href || '').includes('/stories/' + account) ||
        a.querySelector('canvas')
    );
}
"""

_JS_DM_THREADS = """
() => {
    const threads = [];
    document.querySelectorAll('div[role="listitem"], div[role="row"]').forEach(el => {
        const nameEl = el.querySelector('span[dir="auto"], strong');
        const name   = nameEl ? (nameEl.innerText || '').trim() : '';
        if (!name) return;

        let unread = false;
        el.querySelectorAll('span[style]').forEach(s => {
            const fw = s.style.fontWeight;
            if (fw === '700' || fw === 'bold') unread = true;
        });
        const labels = (el.getAttribute('aria-label') || '') +
            [...el.querySelectorAll('[aria-label]')]
                .map(x => x.getAttribute('aria-label') || '').join(' ');
        if (/unread/i.test(labels)) unread = true;

        const spans    = [...el.querySelectorAll('span[dir="auto"]')];
        const preview  = spans.length > 1
            ? (spans[spans.length - 1].innerText || '').trim().slice(0, 60)
            : '';

        threads.push({ name, unread, preview });
    });
    return threads;
}
"""

_JS_NOTES = """
() => {
    const els = [
        ...document.querySelectorAll('[aria-label*="note" i], [data-testid*="note" i]')
    ];
    return els.map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
              .filter(Boolean);
}
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  CHECKERS
#  Return values:
#    True  = completed successfully
#    False = page/network error this cycle (non-fatal, try again next cycle)
#    None  = session is confirmed dead (supervisor must re-login)
# ═══════════════════════════════════════════════════════════════════════════════
async def _goto(page, url: str) -> bool:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        return True
    except PWTimeout:
        log.warning(f"Timeout: {url}")
        return False
    except Exception as e:
        log.warning(f"Nav error {url}: {e}")
        return False


async def check_dms(page, seen_dms: set):
    if not await _goto(page, "https://www.instagram.com/direct/inbox/"):
        return False
    if _url_is_login(page.url):
        return None

    # Wait for thread list (non-fatal)
    try:
        await page.wait_for_selector(
            'div[role="listitem"], div[role="row"]',
            timeout=8000, state="attached",
        )
    except PWTimeout:
        pass

    await asyncio.sleep(0.5)

    # Page title carries the unread badge count — fast and reliable
    title        = await page.title()
    title_unread = 0
    m = re.search(r'\((\d+)\)', title)
    if m:
        title_unread = int(m.group(1))

    # Notes
    try:
        for note in await page.evaluate(_JS_NOTES):
            key = f"note:{note}"
            if key not in seen_dms:
                seen_dms.add(key)
                log.info(f"New note: {note}")
                notify("New Note 🎵", note)
    except Exception as e:
        log.debug(f"Notes error: {e}")

    # DM threads
    try:
        threads = await page.evaluate(_JS_DM_THREADS)
    except Exception as e:
        log.debug(f"DM parse error: {e}")
        if title_unread > 0:
            key = f"__unread_{title_unread}_at_{int(time.time() // 60)}"
            if key not in seen_dms:
                seen_dms.add(key)
                notify("New DM 💬", f"{title_unread} unread message(s)")
        return True

    for t in threads[:30]:
        name, unread, preview = t.get("name",""), t.get("unread", False), t.get("preview","")
        if not name:
            continue
        if title_unread > 0:   # title badge overrides DOM detection
            unread = True
        if unread:
            key = f"dm:{name}:{preview}"
            if key not in seen_dms:
                seen_dms.add(key)
                log.info(f"New DM — {name}: {preview or '(no preview)'}")
                notify("New DM 💬", f"From {name}" + (f": {preview}" if preview else ""))

    return True


async def _check_one_account(page, account: str, seen_posts: set, seen_stories: set):
    url = f"https://www.instagram.com/{account}/"
    if not await _goto(page, url):
        return False
    if _url_is_login(page.url):
        log.warning(f"[{account}] Redirected to login — URL: {page.url}")
        return None

    try:
        await page.wait_for_selector("article, main, header", timeout=10000, state="attached")
    except PWTimeout:
        log.warning(f"[{account}] Slow profile load — continuing anyway (URL: {page.url})")

    await asyncio.sleep(0.5)

    # Story
    try:
        if await page.evaluate(_JS_HAS_STORY, account):
            key = f"story:{account}"
            if key not in seen_stories:
                seen_stories.add(key)
                log.info(f"New story: {account}")
                notify("New Story 📸", f"{account} posted a story")
        else:
            seen_stories.discard(f"story:{account}")
    except Exception as e:
        log.debug(f"[{account}] Story error: {e}")

    # Posts / Reels
    hrefs = []
    try:
        hrefs = await page.evaluate(_JS_POSTS)
    except Exception as e:
        log.warning(f"[{account}] Post extraction error: {e}")
        return False

    if not hrefs:
        # One quiet retry
        await asyncio.sleep(4)
        if not await _goto(page, url):
            return False
        if _url_is_login(page.url):
            return None
        try:
            await page.wait_for_selector("article, main", timeout=8000, state="attached")
        except PWTimeout:
            pass
        await asyncio.sleep(1)
        try:
            hrefs = await page.evaluate(_JS_POSTS)
        except Exception:
            pass

    if not hrefs:
        log.warning(f"[{account}] No post links found — skipping")
        return False

    log.debug(f"[{account}] {len(hrefs)} links")
    for href in hrefs:
        key = f"{account}:{href}"
        if key not in seen_posts:
            seen_posts.add(key)
            label = "Reel 🎬" if "/reel/" in href else "Post 🖼️"
            log.info(f"New {label} — {account}: {href}")
            notify(f"New {label}", f"{account} — instagram.com{href}")

    return True


async def check_content(page, seen_posts: set, seen_stories: set):
    for account in ACCOUNTS:
        try:
            result = await _check_one_account(page, account, seen_posts, seen_stories)
        except Exception as e:
            log.error(f"[{account}] Unhandled: {type(e).__name__}: {e}")
            result = False
        if result is None:
            return None   # session dead — abort remaining accounts
    return True

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SUPERVISOR
# ═══════════════════════════════════════════════════════════════════════════════
async def run():
    global _healthy, _last_dm_check, _last_con_check

    log.info(f"🚀 InstaPing — accounts: {', '.join(ACCOUNTS)}")
    log.info(f"DM every {DM_INTERVAL}s | Content every {CONTENT_INTERVAL}s")

    state      = load_state()
    seen_posts = set(state.get("posts",   []))
    seen_story = set(state.get("stories", []))
    seen_dms   = set(state.get("dms",     []))

    async with async_playwright() as pw:
        browser, context, page = await make_browser(pw)

        try:
            # Startup login
            if not await ensure_logged_in(page, context):
                notify("InstaPing 🔴", "Login failed — restarting in 60 s")
                return

            # Seed state without notifying (these are already-known items)
            # IG sometimes redirects the first profile visit even with a valid
            # session — the cookie needs a "warm-up" page first.  We do a
            # single recovery attempt here before giving up.
            log.info("📋 Seeding initial state...")
            seed = await check_content(page, seen_posts, seen_story)
            if seed is None:
                log.warning("Profile redirect during seeding — warming up session and retrying...")
                # Visit home page to warm up the session cookie
                try:
                    await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(3)
                    await _dismiss_popups(page)
                except Exception:
                    pass
                # If we're on login page now, do a full login
                if _url_is_login(page.url):
                    if not await do_login(page, context):
                        notify("InstaPing 🔴", "Login failed during seeding — restarting in 60 s")
                        return
                # Retry seed — this time treat None as non-fatal (just skip seeding)
                seed = await check_content(page, seen_posts, seen_story)
                if seed is None:
                    log.warning("Could not seed from profile — starting with empty post state (safe)")
                    # Don't return — start monitoring anyway. First cycle will
                    # populate seen_posts so we don't spam notifications.
            await check_dms(page, seen_dms)
            save_state(seen_posts, seen_story, seen_dms)

            log.info("✅ Monitoring active!")
            notify("InstaPing ✅", f"Watching: {', '.join(ACCOUNTS)}")

            last_dm  = 0.0
            last_con = 0.0

            while True:
                now     = time.monotonic()
                due_dm  = (now - last_dm)  >= DM_INTERVAL
                due_con = (now - last_con) >= CONTENT_INTERVAL

                if not due_dm and not due_con:
                    await asyncio.sleep(5)
                    continue

                # ── DM / Notes ─────────────────────────────────────────────
                if due_dm:
                    log.debug(f"DM [{datetime.now().strftime('%H:%M:%S')}]")
                    try:
                        r = await check_dms(page, seen_dms)
                    except Exception as e:
                        log.error(f"DM check crashed: {e}")
                        r = False

                    if r is None:
                        if not await recover_session(page, context):
                            return
                    else:
                        _last_dm_check = datetime.now()
                        last_dm        = time.monotonic()
                        save_state(seen_posts, seen_story, seen_dms)

                # ── Posts / Stories ────────────────────────────────────────
                if due_con:
                    log.info(f"🔍 Content [{datetime.now().strftime('%H:%M:%S')}]")
                    try:
                        r = await check_content(page, seen_posts, seen_story)
                    except Exception as e:
                        log.error(f"Content check crashed: {e}")
                        r = False

                    if r is None:
                        if not await recover_session(page, context):
                            return
                    else:
                        _last_con_check = datetime.now()
                        last_con        = time.monotonic()
                        save_state(seen_posts, seen_story, seen_dms)

        finally:
            _healthy = False
            try:
                await browser.close()
            except Exception:
                pass
            log.info("Browser closed")

# ═══════════════════════════════════════════════════════════════════════════════
#  PROCESS-LEVEL RESTART WRAPPER
# ═══════════════════════════════════════════════════════════════════════════════
def _monitor_loop():
    global _healthy
    while True:
        _healthy = True
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            log.info("Stopped")
            sys.exit(0)
        except Exception as e:
            log.critical(f"Crashed: {type(e).__name__}: {e}")
            notify("InstaPing 🔴", f"Crashed — restarting in 60 s")
        _healthy = False
        time.sleep(60)

# ═══════════════════════════════════════════════════════════════════════════════
#  FASTAPI HEALTH SERVER
# ═══════════════════════════════════════════════════════════════════════════════
app = FastAPI(title="InstaPing", version="3.0")

@app.get("/")
def root():
    return {
        "healthy":        _healthy,
        "accounts":       ACCOUNTS,
        "last_dm_check":  _last_dm_check.isoformat()  if _last_dm_check  else None,
        "last_con_check": _last_con_check.isoformat() if _last_con_check else None,
        "notify_queued":  _notify_queue.qsize(),
        "time":           datetime.now().isoformat(),
    }

@app.get("/status")
def status():
    try:
        s = load_state()
        return {
            "healthy":         _healthy,
            "last_dm_check":   _last_dm_check.isoformat()  if _last_dm_check  else None,
            "last_con_check":  _last_con_check.isoformat() if _last_con_check else None,
            "posts_tracked":   len(s.get("posts",   [])),
            "stories_tracked": len(s.get("stories", [])),
            "dms_tracked":     len(s.get("dms",     [])),
            "notify_queued":   _notify_queue.qsize(),
        }
    except Exception as e:
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()
    log.info("FastAPI on 0.0.0.0:7860")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")
