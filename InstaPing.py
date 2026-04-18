# ── IMPORTS ───────────────────────────────────────────────────────────────────
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
# ──────────────────────────────────────────────────────────────────────────────

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Identity & Auth
USERNAME          = os.getenv("IG_USERNAME")
PASSWORD          = os.getenv("IG_PASSWORD")
BARK_TOKEN        = os.getenv("BARK_TOKEN")
BARK_SERVER       = os.getenv("BARK_SERVER", "https://api.day.app")

ACCOUNTS_RAW      = os.getenv("ACCOUNTS_TO_WATCH", "")
ACCOUNTS_TO_WATCH = [acc.strip() for acc in ACCOUNTS_RAW.split(",") if acc.strip()]

CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "240"))
HEADLESS          = os.getenv("HEADLESS", "True").lower() == "true"

SESSION_FILE      = Path(os.getenv("SESSION_PATH", "ig_session.json"))
STATE_FILE        = Path(os.getenv("STATE_PATH", "ig_state.json"))
LOG_FILE          = Path(os.getenv("LOG_PATH", "ig_monitor.log"))
# ──────────────────────────────────────────────────────────────────────────────

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("ig")
# ─────────────────────────────────────────────────────────────────────────────

# ── NOTIFICATIONS ────────────────────────────────────────────────────────────
def notify(title: str, message: str):
    log.info(f"NOTIFY  {title} | {message}")
    try:
        url = f"{BARK_SERVER}/{BARK_TOKEN}/{title}/{message}"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            log.warning(f"Bark returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.warning(f"Bark notify failed: {e}")
# ─────────────────────────────────────────────────────────────────────────────

# ── STATE PERSISTENCE ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"posts": [], "stories": [], "dm_senders": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))
# ─────────────────────────────────────────────────────────────────────────────

# ── BROWSER HELPERS ───────────────────────────────────────────────────────────
async def dismiss_popups(page):
    for _ in range(4):
        try:
            btn = page.locator('button:has-text("Not Now"), button:has-text("Later"), button:has-text("Dismiss")')
            if await btn.count():
                await btn.first.click(timeout=2000)
                await asyncio.sleep(0.8)
            else:
                break
        except Exception:
            break

async def safe_goto(page, url: str, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            return True
        except PWTimeout:
            if attempt < retries:
                log.warning(f"Timeout loading {url}, retrying...")
                await asyncio.sleep(3)
    log.error(f"Failed to load {url} after {retries+1} attempts")
    return False

async def is_logged_in(page) -> bool:
    await safe_goto(page, "https://www.instagram.com/")
    await asyncio.sleep(2)
    return "login" not in page.url and not await page.query_selector('input[name="username"]')

async def login(page, context):
    log.info("Logging in...")
    if not await safe_goto(page, "https://www.instagram.com/accounts/login/"):
        raise RuntimeError("Could not reach login page")
    await page.wait_for_selector('input[name="username"]', timeout=15000)
    await page.fill('input[name="username"]', USERNAME)
    await page.fill('input[name="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    try:
        await page.wait_for_url("https://www.instagram.com/**", timeout=25000)
    except PWTimeout:
        raise RuntimeError("Login timed out - check credentials or CAPTCHA")
    await asyncio.sleep(3)
    await dismiss_popups(page)
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage))
    log.info("Logged in and session saved.")
# ─────────────────────────────────────────────────────────────────────────────

# ── CHECKERS ─────────────────────────────────────────────────────────────────
async def check_dms(page, seen: set) -> set:
    if not await safe_goto(page, "https://www.instagram.com/direct/inbox/"):
        return seen
    await asyncio.sleep(2)

    threads = await page.query_selector_all('div[role="listitem"]')
    for thread in threads[:15]:
        try:
            unread = await thread.query_selector(
                'span[data-testid="unread-dot"], '
                'div[class*="unread"], '
                'span[style*="font-weight: 700"]'
            )
            if not unread:
                continue
            name_el = await thread.query_selector('span[dir="auto"]')
            if not name_el:
                continue
            name = (await name_el.inner_text()).strip()
            if name and name not in seen:
                seen.add(name)
                notify("New DM 💬", f"Message from {name}")
        except Exception:
            continue
    return seen

async def check_account(page, account: str, seen_posts: set, seen_stories: set):
    if not await safe_goto(page, f"https://www.instagram.com/{account}/"):
        return

    await asyncio.sleep(1.5)

    # Stories
    story_link = await page.query_selector('a[href*="/stories/"]')
    if story_link:
        href = await story_link.get_attribute("href") or ""
        key = f"{account}:{href}"
        if key not in seen_stories:
            seen_stories.add(key)
            notify("New Story 📸", f"{account} posted a story")

    # Posts and Reels
    post_links = await page.query_selector_all('a[href*="/p/"], a[href*="/reel/"]')
    if post_links:
        href = await post_links[0].get_attribute("href") or ""
        key = f"{account}:{href}"
        if key not in seen_posts:
            seen_posts.add(key)
            is_reel = "/reel/" in href
            notify("New Reel 🎬" if is_reel else "New Post 🖼️",
                   f"{account} posted {'a reel' if is_reel else 'something new'}")
# ─────────────────────────────────────────────────────────────────────────────

# ── BROWSER FACTORY ──────────────────────────────────────────────────────────
async def make_browser_and_page(pw, storage=None):
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
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
    # Block Heavy Assets
    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3}",
        lambda route: route.abort()
    )
    return browser, context, page
# ─────────────────────────────────────────────────────────────────────────────

# ── MAIN LOOP ────────────────────────────────────────────────────────────────
async def run():
    state        = load_state()
    seen_posts   = set(state["posts"])
    seen_stories = set(state["stories"])
    seen_dms     = set(state["dm_senders"])

    async with async_playwright() as pw:
        storage = None
        if SESSION_FILE.exists():
            try:
                storage = json.loads(SESSION_FILE.read_text())
                log.info("Loaded saved session.")
            except Exception:
                log.warning("Could Not Load Session File, Will Re-Login.")

        browser, context, page = await make_browser_and_page(pw, storage)

        if not await is_logged_in(page):
            await login(page, context)
        else:
            log.info("Already logged in.")
            await dismiss_popups(page)

        # Seed: Get current state to avoid false positives on first run
        log.info("Seeding initial state...")
        for account in ACCOUNTS_TO_WATCH:
            await check_account(page, account, seen_posts, seen_stories)
        await check_dms(page, seen_dms)
        save_state({"posts": list(seen_posts), "stories": list(seen_stories), "dm_senders": list(seen_dms)})

        log.info(f"Monitoring started. Interval: {CHECK_INTERVAL}s")
        notify("IG Monitor ✅", "Monitoring is Now Active")

        consecutive_errors = 0
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            log.info(f"Checking... [{datetime.now().strftime('%H:%M:%S')}]")
            try:
                for account in ACCOUNTS_TO_WATCH:
                    await check_account(page, account, seen_posts, seen_stories)
                await check_dms(page, seen_dms)
                save_state({
                    "posts":      list(seen_posts),
                    "stories":    list(seen_stories),
                    "dm_senders": list(seen_dms),
                })
                consecutive_errors = 0

            except Exception as e:
                consecutive_errors += 1
                log.error(f"Cycle Error #{consecutive_errors}: {e}")

                if consecutive_errors >= 5:
                    log.critical("5 Consecutive Errors - Restarting Browser")
                    notify("IG Monitor ⚠️", "Restarting due to Errors")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    storage = json.loads(SESSION_FILE.read_text()) if SESSION_FILE.exists() else None
                    browser, context, page = await make_browser_and_page(pw, storage)
                    if not await is_logged_in(page):
                        await login(page, context)
                    consecutive_errors = 0

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped by User")

# Hugging Face Dummy Server Fix
import http.server
import socketserver
import threading

def run_dummy_server():
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", 7860), handler) as httpd:
        httpd.serve_forever()
threading.Thread(target=run_dummy_server, daemon=True).start()
