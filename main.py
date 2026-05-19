from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, TwoFactorRequired

LOGGER = logging.getLogger(__name__)
DROPBOX_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
DROPBOX_SIMPLE_UPLOAD_LIMIT = 8 * 1024 * 1024


def bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    instagram_username: str
    instagram_password: str
    target_username: str
    bark_url: str
    data_dir: Path
    poll_interval_seconds: int
    initial_backfill: bool
    encryption_key: str
    dropbox_app_key: str | None
    dropbox_app_secret: str | None
    dropbox_refresh_token: str | None
    dropbox_access_token: str | None
    dropbox_remote_dir: str
    max_feed_items: int
    max_reels: int
    max_stories: int
    max_own_feed_items: int
    max_own_reels: int
    max_story_viewers: int
    max_media_likers: int
    max_media_comments: int
    health_port: int
    dry_run: bool

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "instagram-session.json"

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def secure_dir(self) -> Path:
        return self.data_dir / "secure"


def load_settings() -> Settings:
    load_dotenv()
    required = ["INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TARGET_USERNAME", "BARK_URL", "ENCRYPTION_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    key = os.environ["ENCRYPTION_KEY"].strip()
    try:
        base64.urlsafe_b64decode(key)
    except Exception as exc:
        raise RuntimeError(
            "ENCRYPTION_KEY must be a Fernet key. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc

    return Settings(
        instagram_username=os.environ["INSTAGRAM_USERNAME"].strip(),
        instagram_password=os.environ["INSTAGRAM_PASSWORD"].strip(),
        target_username=os.environ["TARGET_USERNAME"].strip().lstrip("@"),
        bark_url=os.environ["BARK_URL"].strip().rstrip("/"),
        data_dir=Path(os.getenv("DATA_DIR", "/data")),
        poll_interval_seconds=int_env("POLL_INTERVAL_SECONDS", 180),
        initial_backfill=bool_env("INITIAL_BACKFILL", False),
        encryption_key=key,
        dropbox_app_key=os.getenv("DROPBOX_APP_KEY") or None,
        dropbox_app_secret=os.getenv("DROPBOX_APP_SECRET") or None,
        dropbox_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN") or None,
        dropbox_access_token=os.getenv("DROPBOX_ACCESS_TOKEN") or None,
        dropbox_remote_dir=(os.getenv("DROPBOX_REMOTE_DIR") or "InstaPing").strip("/"),
        max_feed_items=int_env("MAX_FEED_ITEMS", 12),
        max_reels=int_env("MAX_REELS", 12),
        max_stories=int_env("MAX_STORIES", 20),
        max_own_feed_items=int_env("MAX_OWN_FEED_ITEMS", 12),
        max_own_reels=int_env("MAX_OWN_REELS", 12),
        max_story_viewers=int_env("MAX_STORY_VIEWERS", 200),
        max_media_likers=int_env("MAX_MEDIA_LIKERS", 200),
        max_media_comments=int_env("MAX_MEDIA_COMMENTS", 50),
        health_port=int_env("PORT", 8080),
        dry_run=bool_env("DRY_RUN", False),
    )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "seen": {
                "post": [],
                "reel": [],
                "story": [],
                "note": [],
                "story_view": [],
                "story_rewatch_signal": [],
                "media_like": [],
                "media_comment": [],
            }
        }

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            self.data.update(loaded)
            self.data.setdefault("seen", {})
            for key in ("post", "reel", "story", "note", "story_view", "story_rewatch_signal", "media_like", "media_comment"):
                self.data["seen"].setdefault(key, [])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def has_seen(self, activity_type: str, activity_id: str) -> bool:
        return activity_id in self.data["seen"].setdefault(activity_type, [])

    def mark_seen(self, activity_type: str, activity_id: str) -> None:
        seen = self.data["seen"].setdefault(activity_type, [])
        if activity_id not in seen:
            seen.append(activity_id)
        if len(seen) > 1000:
            del seen[:-1000]


class HealthStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = False
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._last_poll_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None

    def mark_ready(self) -> None:
        with self._lock:
            self._ready = True

    def mark_poll_success(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._last_poll_at = now
            self._last_success_at = now
            self._last_error = None

    def mark_poll_failure(self, error: BaseException) -> None:
        with self._lock:
            self._last_poll_at = datetime.now(timezone.utc).isoformat()
            self._last_error = f"{type(error).__name__}: {error}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self._ready,
                "started_at": self._started_at,
                "last_poll_at": self._last_poll_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
            }


def start_health_server(port: int, status: HealthStatus) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/health", "/"}:
                self.send_response(404)
                self.end_headers()
                return
            payload = status.snapshot()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200 if payload["ready"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("healthcheck: " + format, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    LOGGER.info("Health server listening on port %s", port)
    return server


class BarkNotifier:
    def __init__(self, bark_url: str, dry_run: bool = False) -> None:
        self.bark_url = bark_url
        self.dry_run = dry_run

    def ping(self, title: str, body: str, url: str | None = None) -> None:
        if self.dry_run:
            LOGGER.info("DRY_RUN Bark ping: %s - %s", title, body)
            return
        endpoint = f"{self.bark_url}/{quote(title)}/{quote(body)}"
        payload = {"group": "InstaPing", "sound": "minuet"}
        if url:
            payload["url"] = url
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = requests.get(endpoint, params=payload, timeout=20)
                response.raise_for_status()
                return
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    break
                time.sleep(2 * attempt)
        assert last_error is not None
        raise last_error


class SecureStorage:
    def __init__(
        self,
        secure_dir: Path,
        encryption_key: str,
        dropbox_app_key: str | None = None,
        dropbox_app_secret: str | None = None,
        dropbox_refresh_token: str | None = None,
        dropbox_access_token: str | None = None,
        dropbox_remote_dir: str = "InstaPing",
        dry_run: bool = False,
    ) -> None:
        self.secure_dir = secure_dir
        self.fernet = Fernet(encryption_key.encode("utf-8"))
        self.dropbox_app_key = dropbox_app_key
        self.dropbox_app_secret = dropbox_app_secret
        self.dropbox_refresh_token = dropbox_refresh_token
        self.dropbox_access_token = dropbox_access_token
        self.dropbox_remote_dir = dropbox_remote_dir.strip("/")
        self.dry_run = dry_run
        self._dropbox_client = None

    def store_activity(self, activity_type: str, activity_id: str, metadata: dict, files: Iterable[Path]) -> Path:
        self.secure_dir.mkdir(parents=True, exist_ok=True)
        bundle_name = self._safe_bundle_name(f"{activity_type}-{activity_id}")
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as archive_root:
            temp_dir = Path(temp_root)
            (temp_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str), encoding="utf-8")
            artifact_dir = temp_dir / "media"
            artifact_dir.mkdir()
            for file_path in files:
                if file_path.exists() and file_path.is_file():
                    shutil.copy2(file_path, artifact_dir / file_path.name)
            archive_path = Path(shutil.make_archive(str(Path(archive_root) / bundle_name), "zip", temp_dir))
            encrypted_path = self.secure_dir / f"{bundle_name}.zip.fernet"
            encrypted_path.write_bytes(self.fernet.encrypt(archive_path.read_bytes()))
            encrypted_path.with_suffix(encrypted_path.suffix + ".sha256").write_text(
                f"{self._sha256(encrypted_path)}  {encrypted_path.name}\n",
                encoding="utf-8",
            )

        LOGGER.info("Stored encrypted activity bundle at %s", encrypted_path)
        if self.dropbox_refresh_token or self.dropbox_access_token:
            try:
                self._upload_to_dropbox(encrypted_path)
                self._upload_to_dropbox(encrypted_path.with_suffix(encrypted_path.suffix + ".sha256"))
            except Exception:
                LOGGER.exception("Dropbox upload failed; encrypted bundle remains on local storage")
        return encrypted_path

    def _upload_to_dropbox(self, path: Path) -> None:
        if self.dry_run:
            LOGGER.info("DRY_RUN Dropbox upload: %s", path)
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dropbox_path = "/" + "/".join(part for part in [self.dropbox_remote_dir, day, path.name] if part)
        self._upload_file_with_retries(path, dropbox_path)
        LOGGER.info("Uploaded file to Dropbox path %s", dropbox_path)

    def _upload_file_with_retries(self, local_path: Path, dropbox_path: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                self._upload_file(local_path, dropbox_path)
                return
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    break
                LOGGER.warning("Dropbox upload attempt %d failed for %s; retrying", attempt, dropbox_path)
                time.sleep(3 * attempt)
        assert last_error is not None
        raise last_error

    def _upload_file(self, local_path: Path, dropbox_path: str) -> None:
        from dropbox.files import CommitInfo, UploadSessionCursor, WriteMode

        client = self._get_dropbox_client()
        file_size = local_path.stat().st_size
        with local_path.open("rb") as handle:
            if file_size <= DROPBOX_SIMPLE_UPLOAD_LIMIT:
                client.files_upload(handle.read(), dropbox_path, mode=WriteMode.overwrite, mute=True, strict_conflict=False)
                return
            session = client.files_upload_session_start(handle.read(DROPBOX_UPLOAD_CHUNK_SIZE))
            cursor = UploadSessionCursor(session_id=session.session_id, offset=handle.tell())
            commit = CommitInfo(path=dropbox_path, mode=WriteMode.overwrite, mute=True, strict_conflict=False)
            while handle.tell() < file_size:
                remaining = file_size - handle.tell()
                if remaining <= DROPBOX_UPLOAD_CHUNK_SIZE:
                    client.files_upload_session_finish(handle.read(remaining), cursor, commit)
                else:
                    client.files_upload_session_append_v2(handle.read(DROPBOX_UPLOAD_CHUNK_SIZE), cursor)
                    cursor.offset = handle.tell()

    def _get_dropbox_client(self):
        if self._dropbox_client is not None:
            return self._dropbox_client
        import dropbox

        if self.dropbox_refresh_token:
            missing = [
                name
                for name, value in {"DROPBOX_APP_KEY": self.dropbox_app_key, "DROPBOX_APP_SECRET": self.dropbox_app_secret}.items()
                if not value
            ]
            if missing:
                raise RuntimeError(f"Missing Dropbox OAuth variable(s): {', '.join(missing)}")
            self._dropbox_client = dropbox.Dropbox(
                oauth2_refresh_token=self.dropbox_refresh_token,
                app_key=self.dropbox_app_key,
                app_secret=self.dropbox_app_secret,
                timeout=120,
            )
            return self._dropbox_client
        if self.dropbox_access_token:
            LOGGER.warning("Using DROPBOX_ACCESS_TOKEN fallback; refresh tokens are safer for long Railway uptime")
            self._dropbox_client = dropbox.Dropbox(self.dropbox_access_token, timeout=120)
            return self._dropbox_client
        raise RuntimeError("Set DROPBOX_REFRESH_TOKEN with app key/secret, or set DROPBOX_ACCESS_TOKEN for short-lived testing")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_bundle_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")[:180]


class InstagramSession:
    def __init__(self, username: str, password: str, session_path: Path) -> None:
        self.username = username
        self.password = password
        self.session_path = session_path
        self.client = Client()
        self.client.delay_range = [1, 3]

    def login(self) -> Client:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        if self.session_path.exists():
            LOGGER.info("Loading Instagram session from %s", self.session_path)
            self.client.load_settings(self.session_path)
        try:
            self.client.login(self.username, self.password)
            self.client.dump_settings(self.session_path)
            return self.client
        except TwoFactorRequired:
            raise RuntimeError("Instagram requested 2FA. Complete a first login locally so instagrapi can persist a session.") from None
        except LoginRequired:
            LOGGER.warning("Stored session was rejected; retrying with a fresh login")
            self.client = Client()
            self.client.delay_range = [1, 3]
            self.client.login(self.username, self.password)
            self.client.dump_settings(self.session_path)
            return self.client


def polite_pause(min_seconds: float = 1.5, max_seconds: float = 4.0) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


@dataclass
class Activity:
    kind: str
    activity_id: str
    title: str
    body: str
    url: str | None
    raw: Any


class ActivityMonitor:
    def __init__(self, client: Client, settings: Settings, state: StateStore, notifier: BarkNotifier, storage: SecureStorage) -> None:
        self.client = client
        self.settings = settings
        self.state = state
        self.notifier = notifier
        self.storage = storage
        self.target_user_id: str | None = None

    def run_once(self) -> int:
        self.target_user_id = self.target_user_id or self.client.user_id_from_username(self.settings.target_username)
        target_activities = [*self._fetch_feed_posts(), *self._fetch_reels(), *self._fetch_stories(), *self._fetch_notes()]
        inbound_activities = self._fetch_inbound_interactions()

        new_target = [activity for activity in target_activities if not self.state.has_seen(activity.kind, activity.activity_id)]
        if not self.state.data.get("initialized") and not self.settings.initial_backfill:
            for activity in new_target:
                self.state.mark_seen(activity.kind, activity.activity_id)
            self.state.data["initialized"] = datetime.now(timezone.utc).isoformat()
            self.state.save()
            LOGGER.info("Initialized target state with %d existing activities; no target alerts sent", len(new_target))
            new_target = []

        new_inbound = [activity for activity in inbound_activities if not self.state.has_seen(activity.kind, activity.activity_id)]
        if not self.state.data.get("inbound_initialized") and not self.settings.initial_backfill:
            for activity in new_inbound:
                self.state.mark_seen(activity.kind, activity.activity_id)
            self.state.data["inbound_initialized"] = datetime.now(timezone.utc).isoformat()
            self.state.save()
            LOGGER.info("Initialized inbound state with %d existing interactions; no inbound alerts sent", len(new_inbound))
            new_inbound = []

        processed = 0
        for activity in sorted([*new_target, *new_inbound], key=lambda item: item.activity_id):
            self._handle_activity(activity)
            self.state.mark_seen(activity.kind, activity.activity_id)
            self.state.save()
            processed += 1
            polite_pause()

        self.state.data["initialized"] = self.state.data.get("initialized") or datetime.now(timezone.utc).isoformat()
        self.state.data["inbound_initialized"] = self.state.data.get("inbound_initialized") or datetime.now(timezone.utc).isoformat()
        self.state.save()
        return processed

    def _fetch_feed_posts(self) -> list[Activity]:
        assert self.target_user_id is not None
        activities = []
        for media in self.client.user_medias(self.target_user_id, amount=self.settings.max_feed_items):
            if (getattr(media, "product_type", "") or "") == "clips":
                continue
            code = getattr(media, "code", "") or ""
            activities.append(Activity("post", str(media.pk), f"New Instagram post from @{self.settings.target_username}", self._caption_preview(getattr(media, "caption_text", "")), f"https://www.instagram.com/p/{code}/" if code else None, media))
        return activities

    def _fetch_reels(self) -> list[Activity]:
        assert self.target_user_id is not None
        activities = []
        for reel in self.client.user_clips(self.target_user_id, amount=self.settings.max_reels):
            code = getattr(reel, "code", "") or ""
            activities.append(Activity("reel", str(reel.pk), f"New Reel from @{self.settings.target_username}", self._caption_preview(getattr(reel, "caption_text", "")), f"https://www.instagram.com/reel/{code}/" if code else None, reel))
        return activities

    def _fetch_stories(self) -> list[Activity]:
        assert self.target_user_id is not None
        return [
            Activity("story", str(story.pk), f"New Story from @{self.settings.target_username}", "Story activity detected", f"https://www.instagram.com/stories/{self.settings.target_username}/{story.pk}/", story)
            for story in self.client.user_stories(self.target_user_id, amount=self.settings.max_stories)
        ]

    def _fetch_notes(self) -> list[Activity]:
        try:
            note = self.client.get_note_by_user(self.client.get_notes(), self.settings.target_username)
        except Exception:
            LOGGER.exception("Unable to fetch notes")
            return []
        if not note:
            return []
        text = getattr(note, "text", "") or ""
        note_id = str(getattr(note, "id", "")) or f"{self.settings.target_username}:{text}"
        return [Activity("note", note_id, f"New Note from @{self.settings.target_username}", text or "Note activity detected", f"https://www.instagram.com/{self.settings.target_username}/", note)]

    def _fetch_inbound_interactions(self) -> list[Activity]:
        return [*self._fetch_story_view_interactions(), *self._fetch_media_like_and_comment_interactions()]

    def _fetch_story_view_interactions(self) -> list[Activity]:
        assert self.target_user_id is not None
        activities: list[Activity] = []
        try:
            own_stories = self.client.user_stories(self.client.user_id, amount=self.settings.max_stories)
        except Exception:
            LOGGER.exception("Unable to fetch own stories")
            return activities
        positions = self.state.data.setdefault("story_view_positions", {})
        target_id = str(self.target_user_id)
        for story in own_stories:
            story_pk = str(story.pk)
            try:
                viewers = self.client.story_viewers(int(story.pk), amount=self.settings.max_story_viewers)
            except Exception:
                LOGGER.exception("Unable to fetch story viewers for %s", story_pk)
                continue
            for index, viewer in enumerate(viewers):
                if not self._is_target_user(viewer):
                    continue
                rank = index + 1
                previous_rank = positions.get(story_pk, {}).get("rank")
                story_url = f"https://www.instagram.com/stories/{self.settings.instagram_username}/{story_pk}/"
                activities.append(Activity("story_view", f"story_view:{story_pk}:{target_id}", f"@{self.settings.target_username} viewed your Story", f"Viewer rank #{rank}", story_url, {"story_pk": story_pk, "viewer": self._raw_payload(viewer), "rank": rank}))
                if isinstance(previous_rank, int) and rank < previous_rank:
                    activities.append(Activity("story_rewatch_signal", f"story_rewatch_signal:{story_pk}:{previous_rank}->{rank}", f"Possible repeat Story view by @{self.settings.target_username}", f"They moved from viewer rank #{previous_rank} to #{rank}. This is a heuristic, not a guaranteed rewatch count.", story_url, {"story_pk": story_pk, "previous_rank": previous_rank, "current_rank": rank}))
                positions[story_pk] = {"rank": rank, "updated_at": datetime.now(timezone.utc).isoformat()}
                break
        return activities

    def _fetch_media_like_and_comment_interactions(self) -> list[Activity]:
        assert self.target_user_id is not None
        activities: list[Activity] = []
        target_id = str(self.target_user_id)
        for media in self._fetch_own_recent_media():
            media_pk = str(media.pk)
            media_id = str(getattr(media, "id", "") or self.client.media_id(media_pk))
            code = getattr(media, "code", "") or ""
            url = self._media_url(media)
            try:
                if any(self._is_target_user(user) for user in self.client.media_likers(media_id)[: self.settings.max_media_likers]):
                    label = "Reel" if getattr(media, "product_type", "") == "clips" else "post"
                    activities.append(Activity("media_like", f"media_like:{media_pk}:{target_id}", f"@{self.settings.target_username} liked your activity", f"They liked your {label} {code}".strip(), url, {"media_pk": media_pk, "media_id": media_id, "code": code}))
            except Exception:
                LOGGER.exception("Unable to fetch likers for media %s", media_pk)
            try:
                for comment in self.client.media_comments(media_id, amount=self.settings.max_media_comments):
                    if not self._is_target_user(getattr(comment, "user", None)):
                        continue
                    comment_pk = str(getattr(comment, "pk", ""))
                    text = getattr(comment, "text", "") or "Comment activity detected"
                    activities.append(Activity("media_comment", f"media_comment:{media_pk}:{comment_pk}", f"@{self.settings.target_username} commented on your activity", self._caption_preview(text, fallback="Comment activity detected"), url, {"media_pk": media_pk, "media_id": media_id, "code": code, "comment": self._raw_payload(comment)}))
            except Exception:
                LOGGER.exception("Unable to fetch comments for media %s", media_pk)
            polite_pause(0.75, 2.0)
        return activities

    def _fetch_own_recent_media(self) -> list[Any]:
        medias: list[Any] = []
        try:
            medias.extend(self.client.user_medias(self.client.user_id, amount=self.settings.max_own_feed_items))
        except Exception:
            LOGGER.exception("Unable to fetch own feed media")
        try:
            medias.extend(self.client.user_clips(self.client.user_id, amount=self.settings.max_own_reels))
        except Exception:
            LOGGER.exception("Unable to fetch own reels")
        return list({str(media.pk): media for media in medias}.values())

    def _handle_activity(self, activity: Activity) -> None:
        LOGGER.info("Processing new %s: %s", activity.kind, activity.activity_id)
        with tempfile.TemporaryDirectory(dir=self.settings.download_dir) as temp_root:
            files = self._download_activity(activity, Path(temp_root))
            encrypted_path = self.storage.store_activity(activity.kind, activity.activity_id, self._metadata(activity, files), files)
            self.notifier.ping(activity.title, f"{activity.body} | saved: {encrypted_path.name}"[:900], activity.url)

    def _download_activity(self, activity: Activity, folder: Path) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if activity.kind == "story":
                return self._as_paths(self.client.story_download(int(activity.activity_id), folder=folder))
            if activity.kind == "reel":
                return self._as_paths(self.client.clip_download(int(activity.activity_id), folder=folder))
            if activity.kind == "post":
                media_type = getattr(activity.raw, "media_type", None)
                if media_type == 1:
                    return self._as_paths(self.client.photo_download(int(activity.activity_id), folder=folder))
                if media_type == 2:
                    return self._as_paths(self.client.video_download(int(activity.activity_id), folder=folder))
                if media_type == 8:
                    return self._as_paths(self.client.album_download(int(activity.activity_id), folder=folder))
            if activity.kind == "note":
                note_path = folder / "note.txt"
                note_path.write_text(activity.body, encoding="utf-8")
                return [note_path]
        except Exception:
            LOGGER.exception("Download failed for %s %s; storing metadata only", activity.kind, activity.activity_id)
        return []

    def _metadata(self, activity: Activity, files: Iterable[Path]) -> dict:
        return {
            "kind": activity.kind,
            "id": activity.activity_id,
            "target_username": self.settings.target_username,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "notification": {"title": activity.title, "body": activity.body, "url": activity.url},
            "files": [path.name for path in files],
            "raw": self._raw_payload(activity.raw),
        }

    @staticmethod
    def _as_paths(value: Any) -> list[Path]:
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            return [Path(value)]
        return [Path(item) for item in value]

    @staticmethod
    def _caption_preview(text: str, fallback: str = "Instagram activity detected") -> str:
        cleaned = " ".join((text or "").split())
        return cleaned[:240] if cleaned else fallback

    def _is_target_user(self, user: Any) -> bool:
        if user is None:
            return False
        username = (getattr(user, "username", "") or "").lower()
        pk = str(getattr(user, "pk", "") or getattr(user, "id", ""))
        return username == self.settings.target_username.lower() or pk == str(self.target_user_id)

    @staticmethod
    def _media_url(media: Any) -> str | None:
        code = getattr(media, "code", "") or ""
        if not code:
            return None
        return f"https://www.instagram.com/reel/{code}/" if getattr(media, "product_type", "") == "clips" else f"https://www.instagram.com/p/{code}/"

    @staticmethod
    def _raw_payload(raw: Any) -> Any:
        if hasattr(raw, "dict"):
            return raw.dict()
        if hasattr(raw, "model_dump"):
            return raw.model_dump()
        if isinstance(raw, (dict, list, str, int, float, bool)) or raw is None:
            return raw
        return repr(raw)


def prepare_data_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.download_dir.mkdir(parents=True, exist_ok=True)
    settings.secure_dir.mkdir(parents=True, exist_ok=True)
    for child in settings.download_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def run_worker() -> None:
    configure_logging()
    settings = load_settings()
    prepare_data_dirs(settings)
    stop_event = threading.Event()
    health_status = HealthStatus()
    health_server = start_health_server(settings.health_port, health_status)

    def request_shutdown(signum: int, _frame: object) -> None:
        logging.info("Received signal %s; shutting down", signum)
        stop_event.set()
        health_server.shutdown()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    state = StateStore(settings.state_path)
    state.load()
    client = InstagramSession(settings.instagram_username, settings.instagram_password, settings.session_path).login()
    monitor = ActivityMonitor(
        client=client,
        settings=settings,
        state=state,
        notifier=BarkNotifier(settings.bark_url, dry_run=settings.dry_run),
        storage=SecureStorage(
            secure_dir=settings.secure_dir,
            encryption_key=settings.encryption_key,
            dropbox_app_key=settings.dropbox_app_key,
            dropbox_app_secret=settings.dropbox_app_secret,
            dropbox_refresh_token=settings.dropbox_refresh_token,
            dropbox_access_token=settings.dropbox_access_token,
            dropbox_remote_dir=settings.dropbox_remote_dir,
            dry_run=settings.dry_run,
        ),
    )
    health_status.mark_ready()

    while not stop_event.is_set():
        try:
            processed = monitor.run_once()
            logging.info("Poll complete; processed %d new activities", processed)
            health_status.mark_poll_success()
        except Exception as exc:
            health_status.mark_poll_failure(exc)
            logging.exception("Poll failed")
        stop_event.wait(settings.poll_interval_seconds)


def dropbox_auth() -> None:
    from dropbox.oauth import DropboxOAuth2FlowNoRedirect

    app_key = input("Dropbox app key: ").strip()
    app_secret = getpass.getpass("Dropbox app secret: ").strip()
    flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        app_secret,
        token_access_type="offline",
        scope=["files.content.write", "files.metadata.write"],
    )
    print("\nOpen this URL, approve the app, then paste the code below:\n")
    print(flow.start())
    result = flow.finish(input("\nAuthorization code: ").strip())
    print("\nSet these Railway variables:\n")
    print(f"DROPBOX_APP_KEY={app_key}")
    print("DROPBOX_APP_SECRET=<the app secret you entered>")
    print(f"DROPBOX_REFRESH_TOKEN={result.refresh_token}")


def decrypt_bundle(bundle: Path, out_dir: Path, key: str | None) -> None:
    key = key or os.getenv("ENCRYPTION_KEY")
    if not key:
        raise SystemExit("Missing encryption key. Pass --key or set ENCRYPTION_KEY.")
    if not bundle.exists():
        raise SystemExit(f"Bundle does not exist: {bundle}")
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / bundle.name.removesuffix(".fernet")
    zip_path.write_bytes(Fernet(key.encode("utf-8")).decrypt(bundle.read_bytes()))
    extract_dir = out_dir / zip_path.stem
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)
    print(f"Decrypted zip: {zip_path}")
    print(f"Extracted files: {extract_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="InstaPing Railway worker")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="Run the monitor worker")
    subparsers.add_parser("dropbox-auth", help="Generate a Dropbox refresh token")
    decrypt_parser = subparsers.add_parser("decrypt", help="Decrypt a .zip.fernet bundle")
    decrypt_parser.add_argument("bundle", type=Path)
    decrypt_parser.add_argument("--out", type=Path, default=Path("decrypted"))
    decrypt_parser.add_argument("--key", default=None)

    args = parser.parse_args()
    if args.command in {None, "run"}:
        run_worker()
    elif args.command == "dropbox-auth":
        dropbox_auth()
    elif args.command == "decrypt":
        decrypt_bundle(args.bundle, args.out, args.key)


if __name__ == "__main__":
    main()
