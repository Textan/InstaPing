# ── IMPORTS ───────────────────────────────────────────────────────────────────
import asyncio
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
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

CHECK_INTERVAL    = int(os.getenv("CHECK_INTERVAL", "300"))  
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("ig_monitor")
# ─────────────────────────────────────────────────────────────────────────────

# ── NOTIFICATIONS ────────────────────────────────────────────────────────────
def notify(title: str, message: str, sound: str = "alert"):
    """Send notification via Bark (requires BARK_TOKEN and BARK_SERVER)"""
    log.info(f"NOTIFY | {title} | {message}")
    
    if not BARK_TOKEN:
        log.warning("BARK_TOKEN not set, notification skipped")
        return
    try:
        title_safe = title.replace(" ", "%20").replace("✅", "%E2%9C%85")
        msg_safe = message.replace(" ", "%20")
        url = f"{BARK_SERVER}/{BARK_TOKEN}/{title_safe}/{msg_safe}?sound={sound}"
        resp = requests.get(url, timeout=5)
        if resp.status_code not in (200, 201):
            log.warning(f"Bark returned {resp.status_code}: {resp.text[:100]}")
    except requests.Timeout:
        log.warning("Bark notification timeout")
    except Exception as e:
        log.warning(f"Bark notify failed: {type(e).__name__}: {e}")
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
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error(f"Failed to save state: {e}")
# ─────────────────────────────────────────────────────────────────────────────

# ── BROWSER HELPERS ───────────────────────────────────────────────────────────
async def dismiss_popups(page, max_attempts: int = 5):
    selectors = [
        'button:has-text("Not Now")',
        'button:has-text("Later")',
        'button:has-text("Dismiss")',
        'button[aria-label*="Close"]',
    ]
    
    for attempt in range(max_attempts):
        try:
            for selector in selectors:
                try:
                    btn = page.locator(selector)
                    if await btn.count() > 0:
                        await btn.first.click(timeout=1500)
                        await asyncio.sleep(0.5)
                        break
                except:
                    continue
            else:
                break
        except Exception as e:
            log.debug(f"Popup dismiss attempt {attempt+1} failed: {e}")
            break

async def safe_goto(page, url: str, retries: int = 2) -> bool:
    for attempt in range(retries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            return True
        except PWTimeout:
            if attempt < retries:
                log.warning(f"Timeout loading {url}, retry {attempt+1}/{retries}...")
                await asyncio.sleep(2)
            else:
                log.error(f"Failed to load {url} after {retries+1} attempts")
                return False
        except Exception as e:
            if attempt < retries:
                log.warning(f"Error loading {url}: {e}, retrying...")
                await asyncio.sleep(2)
            else:
                log.error(f"Failed to load {url}: {e}")
                return False
    return False

async def is_logged_in(page) -> bool:
    try:
        await safe_goto(page, "https://www.instagram.com/")
        await asyncio.sleep(1)
        is_login_page = "login" in page.url.lower()
        has_login_input = await page.query_selector('input[name="username"]') is not None
        return not (is_login_page or has_login_input)
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
# ─────────────────────────────────────────────────────────────────────────────

# ── CHECKERS ─────────────────────────────────────────────────────────────────
async def check_dms(page, seen: set) -> bool:
    try:
        if not await safe_goto(page, "https://www.instagram.com/direct/inbox/"):
            return False
        
        await asyncio.sleep(2)

        threads = await page.query_selector_all('div[role="listitem"]')
        log.debug(f"Found {len(threads)} DM threads")
        
        checked = 0
        for thread in threads[:20]:  
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
                    log.info(f"New DM from: {name}")
                    notify("New DM 💬", f"Message from {name}", sound="alert")
                
                checked += 1
            except Exception as e:
                log.debug(f"Error parsing DM thread: {e}")
                continue
        
        log.debug(f"Checked {checked} DM threads")
        return True
        
    except Exception as e:
        log.error(f"DM check failed: {e}")
        return False

async def check_account(page, account: str, seen_posts: set, seen_stories: set) -> bool:
    try:
        if not await safe_goto(page, f"https://www.instagram.com/{account}/"):
            return False

        await asyncio.sleep(1)

        story_links = await page.query_selector_all('a[href*="/stories/"]')
        if story_links:
            for story_link in story_links[:1]:  
                try:
                    href = await story_link.get_attribute("href") or ""
                    key = f"{account}:{href}"
                    if key not in seen_stories:
                        seen_stories.add(key)
                        log.info(f"New story from {account}")
                        notify("New Story 📸", f"{account} posted a story")
                        break
                except:
                    continue

        post_links = await page.query_selector_all('a[href*="/p/"], a[href*="/reel/"]')
        if post_links:
            for post_link in post_links[:1]:  
                try:
                    href = await post_link.get_attribute("href") or ""
                    key = f"{account}:{href}"
                    if key not in seen_posts:
                        seen_posts.add(key)
                        is_reel = "/reel/" in href
                        content_type = "Reel 🎬" if is_reel else "Post 🖼️"
                        log.info(f"New {content_type} from {account}")
                        notify(f"New {content_type}", f"{account} posted new content")
                        break
                except:
                    continue
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
    

    await page.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4,mp3,css}",
        lambda route: route.abort()
    )
    
    page.set_default_timeout(15000)
    
    return browser, context, page
# ─────────────────────────────────────────────────────────────────────────────

# ── MAIN MONITORING LOOP ─────────────────────────────────────────────────────
async def run():
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
        last_reload = datetime.now()
        consecutive_errors = 0

        try:
            if not await is_logged_in(page):
                await login(page, context)
            else:
                log.info("✅ Already logged in")
                await dismiss_popups(page)

            log.info("📋 Seeding initial state...")
            for account in ACCOUNTS_TO_WATCH:
                await check_account(page, account, seen_posts, seen_stories)
            await check_dms(page, seen_dms)
            save_state({
                "posts": list(seen_posts),
                "stories": list(seen_stories),
                "dm_senders": list(seen_dms)
            })

            log.info("✅ Monitoring started!")
            notify("IG Monitor ✅", "Monitoring is now active")

            while True:
                await asyncio.sleep(CHECK_INTERVAL)
                
                if (datetime.now() - last_reload).total_seconds() > TIMEOUT_RELOAD:
                    log.info("♻️  Reloading browser (periodic refresh)...")
                    await browser.close()
                    browser, context, page = await make_browser_and_page(pw, storage)
                    if not await is_logged_in(page):
                        await login(page, context)
                    last_reload = datetime.now()
                
                log.info(f"🔍 Checking [{datetime.now().strftime('%H:%M:%S')}]...")
                
                try:
                    success_count = 0
                    for account in ACCOUNTS_TO_WATCH:
                        if await check_account(page, account, seen_posts, seen_stories):
                            success_count += 1
                    
                    if await check_dms(page, seen_dms):
                        success_count += 1
                    
                    save_state({
                        "posts": list(seen_posts),
                        "stories": list(seen_stories),
                        "dm_senders": list(seen_dms),
                    })
                    
                    log.debug(f"✅ Check cycle complete ({success_count}/{len(ACCOUNTS_TO_WATCH)+1} successful)")
                    consecutive_errors = 0

                except Exception as e:
                    consecutive_errors += 1
                    log.error(f"❌ Cycle error #{consecutive_errors}: {type(e).__name__}: {e}")

                    if consecutive_errors >= 5:
                        log.critical("🔴 5 consecutive errors - restarting browser")
                        notify("IG Monitor ⚠️", "Restarting due to repeated errors")
                        try:
                            await browser.close()
                        except:
                            pass
                        
                        storage = None
                        if SESSION_FILE.exists():
                            try:
                                storage = json.loads(SESSION_FILE.read_text())
                            except:
                                pass
                        
                        browser, context, page = await make_browser_and_page(pw, storage)
                        if not await is_logged_in(page):
                            await login(page, context)
                        
                        consecutive_errors = 0
                        last_reload = datetime.now()
                        
        finally:
            await browser.close()
            log.info("Browser closed")
# ─────────────────────────────────────────────────────────────────────────────

# ── FASTAPI SERVER ───────────────────────────────────────────────────────────
app = FastAPI(title="IG Monitor", version="1.0")

@app.get("/")
def read_root():
    return {
        "status": "IG Monitor is Active",
        "time": datetime.now().isoformat(),
        "accounts": ACCOUNTS_TO_WATCH,
    }

@app.get("/status")

def get_status():
    try:
        state = load_state()
        return {
            "status": "running",
            "posts_tracked": len(state.get("posts", [])),
            "stories_tracked": len(state.get("stories", [])),
            "dms_tracked": len(state.get("dm_senders", [])),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
# ─────────────────────────────────────────────────────────────────────────────

# ── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def run_monitor():
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            log.info("Monitor stopped by user")
        except Exception as e:
            log.critical(f"Monitor crashed: {e}")
            notify("IG Monitor 🔴", "Monitor crashed unexpectedly")

    monitor_thread = threading.Thread(target=run_monitor, daemon=True)
    monitor_thread.start()
    
    log.info("Starting FastAPI server on 0.0.0.0:7860")
    uvicorn.run(app, host="0.0.0.0", port=7860, log_level="warning")
