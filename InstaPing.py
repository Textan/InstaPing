import os
import sys
import time
import json
import random
import logging
import traceback
from pathlib import Path

import requests
from instagrapi import Client
from instagrapi.exceptions import (
    LoginRequired,
    ClientError,
    ClientConnectionError,
    ClientThrottledError,
    ReloginAttemptExceeded,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ig_monitor")

class Config:
    IG_USERNAME: str = os.environ["IG_USERNAME"]
    IG_PASSWORD: str = os.environ["IG_PASSWORD"]
    TARGET_USERNAME: str = os.environ["TARGET_USERNAME"]
    BARK_URL: str = os.environ["BARK_URL"].rstrip("/")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "30"))
    SESSION_FILE: Path = Path(os.getenv("SESSION_FILE", "/data/session.json"))
    STATE_FILE: Path = Path(os.getenv("STATE_FILE", "/data/state.json"))

    # Back-off
    MAX_BACKOFF: int = 300
    MIN_BACKOFF: int = 30
    JITTER: int = 5


cfg = Config()

def bark_push(title: str, body: str, sound: str = "minuet") -> bool:
    payload = {
        "title": title,
        "body": body,
        "group": "IGMonitor",
        "sound": sound,
        "icon": "https://www.instagram.com/favicon.ico",
        "isArchive": 1,
    }
    try:
        r = requests.post(cfg.BARK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            log.info("📱 Bark sent: %s — %s", title, body)
            return True
        log.warning("Bark HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.error("Bark request failed: %s", exc)
    return False

class IGClient:
    def __init__(self):
        self.cl = Client()
        self.cl.delay_range = [1, 3]
        self._login()

    def _login(self):
        cfg.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        if cfg.SESSION_FILE.exists():
            log.info("Loading Saved Session")
            try:
                self.cl.load_settings(str(cfg.SESSION_FILE))
                self.cl.login(cfg.IG_USERNAME, cfg.IG_PASSWORD)
                log.info("Session Restored")
                return
            except Exception as exc:
                log.warning("Session Restore Failed (%s) — Fresh Login", exc)
                self.cl = Client()
                self.cl.delay_range = [1, 3]

        log.info("Logging in as @%s …", cfg.IG_USERNAME)
        self.cl.login(cfg.IG_USERNAME, cfg.IG_PASSWORD)
        self.cl.dump_settings(str(cfg.SESSION_FILE))
        log.info("Login OK — Session Saved")

    def relogin(self):
        log.warning("Re Logging In")
        try:
            self.cl.relogin()
            self.cl.dump_settings(str(cfg.SESSION_FILE))
            log.info("Re Login OK")
        except ReloginAttemptExceeded:
            log.error("Re Login Attempts Exceeded — Fresh Login in 60S")
            time.sleep(60)
            self.cl = Client()
            self.cl.delay_range = [1, 3]
            self._login()

    def call(self, fn, *args, retries: int = 3, **kwargs):
        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except LoginRequired:
                log.warning("LoginRequired (attempt %d)", attempt)
                self.relogin()
            except ClientThrottledError:
                wait = 60 * attempt
                log.warning("Rate-limited — sleeping %d s …", wait)
                time.sleep(wait)
            except ClientConnectionError as exc:
                wait = 15 * attempt
                log.warning("Connection error: %s — sleeping %d s …", exc, wait)
                time.sleep(wait)
            except ClientError as exc:
                log.error("ClientError: %s (attempt %d/%d)", exc, attempt, retries)
                if attempt == retries:
                    raise
                time.sleep(10 * attempt)
        return None

def load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception as exc:
            log.warning("State load error (%s) — starting fresh.", exc)
    return {
        "seen_posts": [],       
        "seen_stories": [],     
        "seen_notes": [],       
        "my_stories": {},       
    }

def save_state(state: dict):
    cfg.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(cfg.STATE_FILE)

class Monitor:
    def __init__(self):
        self.ig = IGClient()
        self.state = load_state()
        self.target_id: int | None = None
        self.my_id: int | None = None
        self._resolve_ids()

    def _resolve_ids(self):
        info = self.ig.call(self.ig.cl.user_info_by_username, cfg.TARGET_USERNAME)
        if not info:
            log.error("Cannot resolve @%s — check TARGET_USERNAME.", cfg.TARGET_USERNAME)
            sys.exit(1)
        self.target_id = info.pk
        log.info("Target @%s → pk %s", cfg.TARGET_USERNAME, self.target_id)

        me = self.ig.call(self.ig.cl.user_info_by_username, cfg.IG_USERNAME)
        if me:
            self.my_id = me.pk
            log.info("My account pk: %s", self.my_id)

    def _check_posts(self):
        medias = self.ig.call(self.ig.cl.user_medias, self.target_id, amount=12)
        if not medias:
            return

        seen = set(self.state["seen_posts"])
        new_items = [m for m in medias if str(m.pk) not in seen]

        for m in reversed(new_items):  
            media_type = m.media_type
            product_type = getattr(m, "product_type", "") or ""

            if media_type == 2 and product_type == "clips":
                kind = "Reel"
            elif media_type == 8:
                kind = "Carousel Post"
            elif media_type == 2:
                kind = "Video Post"
            else:
                kind = "Photo Post"

            caption = (m.caption_text or "")[:120]
            bark_push(
                title=f"@{cfg.TARGET_USERNAME} posted a {kind}",
                body=caption or "(no caption)",
            )
            seen.add(str(m.pk))

        self.state["seen_posts"] = list(seen)[-200:]

    def _check_target_stories(self):
        stories = self.ig.call(self.ig.cl.user_stories, self.target_id)
        if stories is None:
            return

        seen = set(self.state["seen_stories"])
        new_stories = [s for s in stories if str(s.pk) not in seen]

        for s in reversed(new_stories):
            kind = "Video Story" if s.media_type == 2 else "Story 📷"
            bark_push(
                title=f"@{cfg.TARGET_USERNAME} posted a {kind}",
                body=f"Posted at {s.taken_at.strftime('%H:%M')}",
            )
            seen.add(str(s.pk))

        self.state["seen_stories"] = list(seen)[-500:]

    def _check_notes(self):
        try:
            notes = self.ig.call(self.ig.cl.get_notes)
        except AttributeError:
            return   
        if not notes:
            return

        seen = set(self.state["seen_notes"])

        for note in notes:
            uid = str(
                getattr(note, "user_id", None)
                or getattr(getattr(note, "user", None), "pk", None)
                or ""
            )
            if uid != str(self.target_id):
                continue

            note_id = str(note.id)
            if note_id in seen:
                continue

            text = (getattr(note, "text", "") or "")[:200]
            bark_push(
                title=f"@{cfg.TARGET_USERNAME} posted a Note 📝",
                body=text or "(empty note)",
            )
            seen.add(note_id)

        self.state["seen_notes"] = list(seen)[-200:]

    def _check_my_story_views(self):
        if not self.my_id:
            return

        my_stories = self.ig.call(self.ig.cl.user_stories, self.my_id)
        if not my_stories:
            self.state["my_stories"] = {}
            return

        story_state: dict = self.state.setdefault("my_stories", {})

        for story in my_stories:
            pk = str(story.pk)
            entry = story_state.setdefault(pk, {"target_viewed": False, "view_count": 0})

            viewers = self.ig.call(self.ig.cl.story_viewers, story.pk, amount=200)
            if viewers is None:
                continue

            entry["view_count"] = len(viewers)

            if not entry["target_viewed"]:
                for viewer in viewers:
                    if str(viewer.pk) == str(self.target_id):
                        entry["target_viewed"] = True
                        bark_push(
                            title=f"@{cfg.TARGET_USERNAME} viewed your story 👀",
                            body=(
                                f"Story posted at {story.taken_at.strftime('%H:%M')} · "
                                f"{entry['view_count']} total view(s)"
                            ),
                        )
                        break

        active = {str(s.pk) for s in my_stories}
        for pk in list(story_state.keys()):
            if pk not in active:
                del story_state[pk]

    def tick(self):
        self._check_posts()
        self._check_target_stories()
        self._check_notes()
        self._check_my_story_views()
        save_state(self.state)

    def run(self):
        log.info(
            "Monitor Started · target=@%s · interval=%ds",
            cfg.TARGET_USERNAME, cfg.POLL_INTERVAL,
        )
        bark_push(
            title="IGMonitor Started",
            body=f"Watching @{cfg.TARGET_USERNAME} every {cfg.POLL_INTERVAL}s",
        )
        consecutive_errors = 0
        backoff = 0

        while True:
            if backoff:
                log.info("Back-off: %d s", backoff)
                time.sleep(backoff)
                backoff = 0

            try:
                self.tick()
                consecutive_errors = 0

            except KeyboardInterrupt:
                log.info("Shutting down …")
                save_state(self.state)
                sys.exit(0)

            except Exception:
                consecutive_errors += 1
                log.error("Unhandled exception #%d:\n%s", consecutive_errors, traceback.format_exc())
                backoff = min(cfg.MIN_BACKOFF * (2 ** (consecutive_errors - 1)), cfg.MAX_BACKOFF)
                if consecutive_errors % 5 == 0:
                    bark_push(
                        title="IGMonitor ⚠️ Errors",
                        body=f"{consecutive_errors} consecutive failures — backing off {backoff}s",
                        sound="sosumi",
                    )
            jitter = random.randint(-cfg.JITTER, cfg.JITTER)
            time.sleep(max(10, cfg.POLL_INTERVAL + jitter))

if __name__ == "__main__":
    Monitor().run()
