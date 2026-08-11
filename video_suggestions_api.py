#!/usr/bin/env python3
"""MORAL SQD YouTube video-suggestion service.

The service deliberately depends only on the Python standard library.  It
keeps browser identities pseudonymous, stores no raw IP addresses, verifies
all metadata server-side with the YouTube Data API, and exposes a small
same-origin administrator inbox for DK.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen


API_PREFIX = "/api/video-suggestions"
VISITOR_COOKIE_NAME = "moralsqd_video_visitor"
ADMIN_COOKIE_NAME = "moralsqd_video_admin"
ADMIN_USERNAME = "dk"
SCHEMA_VERSION = 1

PASSWORD_SCHEME = "scrypt"
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 5

YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
YOUTUBE_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
ROOT_STATIC_FILES = frozenset(
    {
        "index.html",
        "favicon.png",
        "dk.jpg",
        "feanrir.png",
        "frametamer.png",
        "iscancore.png",
        "katya.jpg",
        "kostek.png",
        "kot.jpg",
        "support.js",
        "telegram.svg",
        "twitch.svg",
        "video-suggestions.js",
    }
)
NESTED_STATIC_DIRECTORIES = frozenset({"dk-video-inbox", "privacy"})
STATIC_EXTENSIONS = frozenset({".html", ".css", ".js", ".svg", ".png", ".jpg", ".webp"})

VISITOR_COOKIE_MAX_AGE = 400 * 24 * 60 * 60
VISITOR_REGISTRY_TTL_SECONDS = VISITOR_COOKIE_MAX_AGE + 24 * 60 * 60
FRESH_SECONDS = 72 * 60 * 60
MODERATE_SECONDS = 14 * 24 * 60 * 60


class VideoSuggestionError(Exception):
    """Expected domain error carrying a stable HTTP-compatible code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


class MetadataUnavailable(VideoSuggestionError):
    def __init__(self, message: str = "YouTube metadata is temporarily unavailable"):
        super().__init__("metadata_unavailable", message, 503)


class VideoRejected(VideoSuggestionError):
    pass


@dataclass(frozen=True)
class VideoMetadata:
    youtube_id: str
    title: str
    duration_seconds: int
    published_at: datetime


class MetadataProvider(Protocol):
    def fetch(self, youtube_id: str) -> VideoMetadata:
        """Return validated public metadata or raise a VideoSuggestionError."""


@dataclass(frozen=True)
class Settings:
    secret: bytes
    admin_password_hash: str
    youtube_api_key: str
    db_path: Path
    static_dir: Path
    admin_username: str = ADMIN_USERNAME
    host: str = "127.0.0.1"
    port: int = 8790
    secure_cookie: bool = True
    allowed_origin: str | None = None
    session_ttl_seconds: int = 8 * 60 * 60
    max_request_bytes: int = 4 * 1024
    visitor_issue_limit: int = 10
    visitor_global_issue_limit: int = 300
    public_bucket_limit: int = 30
    public_visitor_limit: int = 20
    public_global_limit: int = 60
    login_rate_limit: int = 5
    login_kdf_slots: int = 3
    admin_rate_limit: int = 120
    rate_window_seconds: int = 10 * 60
    metadata_refresh_seconds: int = 28 * 24 * 60 * 60
    metadata_max_age_seconds: int = 30 * 24 * 60 * 60
    metadata_retry_seconds: int = 6 * 60 * 60
    metadata_refresh_batch_size: int = 10
    youtube_timeout_seconds: float = 8.0

    @classmethod
    def from_environment(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parent
        raw_secret = os.environ.get(
            "VIDEO_SUGGESTIONS_SECRET", "local-development-only-video-secret"
        )
        secure_cookie = _environment_bool("VIDEO_SUGGESTIONS_SECURE_COOKIE", True)
        password_hash = os.environ.get(
            "VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH", ""
        ).strip()
        if not password_hash:
            raw_password = os.environ.get(
                "VIDEO_SUGGESTIONS_ADMIN_PASSWORD", "local-development-only"
            )
            password_hash = hash_password(
                raw_password,
                salt=hashlib.sha256(
                    b"moralsqd-video-dev-password-v1\x00" + raw_secret.encode("utf-8")
                ).digest()[:16],
            )

        settings = cls(
            secret=raw_secret.encode("utf-8"),
            admin_password_hash=password_hash,
            youtube_api_key=(
                os.environ.get("VIDEO_SUGGESTIONS_YOUTUBE_API_KEY", "").strip()
                or os.environ.get("YOUTUBE_API_KEY", "").strip()
            ),
            db_path=Path(
                os.environ.get(
                    "VIDEO_SUGGESTIONS_DB_PATH",
                    str(project_dir / "video_suggestions.db"),
                )
            ),
            static_dir=Path(
                os.environ.get("VIDEO_SUGGESTIONS_STATIC_DIR", str(project_dir))
            ),
            admin_username=os.environ.get(
                "VIDEO_SUGGESTIONS_ADMIN_USERNAME", ADMIN_USERNAME
            ).strip()
            or ADMIN_USERNAME,
            host=os.environ.get("VIDEO_SUGGESTIONS_HOST", "127.0.0.1"),
            port=_environment_int("VIDEO_SUGGESTIONS_PORT", 8790, 1, 65535),
            secure_cookie=secure_cookie,
            allowed_origin=(
                os.environ.get("VIDEO_SUGGESTIONS_ALLOWED_ORIGIN", "").strip()
                or None
            ),
            session_ttl_seconds=_environment_int(
                "VIDEO_SUGGESTIONS_SESSION_TTL_SECONDS", 8 * 60 * 60, 300
            ),
            login_kdf_slots=_environment_int(
                "VIDEO_SUGGESTIONS_LOGIN_KDF_SLOTS", 3, 1, 16
            ),
            max_request_bytes=_environment_int(
                "VIDEO_SUGGESTIONS_MAX_REQUEST_BYTES", 4 * 1024, 512
            ),
            metadata_refresh_seconds=_environment_int(
                "VIDEO_SUGGESTIONS_METADATA_REFRESH_SECONDS", 28 * 24 * 60 * 60, 60
            ),
            metadata_max_age_seconds=_environment_int(
                "VIDEO_SUGGESTIONS_METADATA_MAX_AGE_SECONDS", 30 * 24 * 60 * 60, 60
            ),
            youtube_timeout_seconds=_environment_float(
                "VIDEO_SUGGESTIONS_YOUTUBE_TIMEOUT_SECONDS", 8.0, 0.1
            ),
        )
        if len(settings.secret) < 32:
            raise ValueError("VIDEO_SUGGESTIONS_SECRET must contain at least 32 bytes")
        if settings.metadata_refresh_seconds >= settings.metadata_max_age_seconds:
            raise ValueError("metadata refresh age must be lower than maximum age")
        if secure_cookie:
            if raw_secret == "local-development-only-video-secret":
                raise ValueError(
                    "VIDEO_SUGGESTIONS_SECRET is required when secure cookies are enabled"
                )
            if not os.environ.get(
                "VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH", ""
            ).strip():
                raise ValueError(
                    "VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH is required when secure cookies are enabled"
                )
            if not settings.allowed_origin:
                raise ValueError(
                    "VIDEO_SUGGESTIONS_ALLOWED_ORIGIN is required when secure cookies are enabled"
                )
            if not settings.youtube_api_key:
                raise ValueError(
                    "VIDEO_SUGGESTIONS_YOUTUBE_API_KEY is required when secure cookies are enabled"
                )
            if not password_hash_is_supported(settings.admin_password_hash):
                raise ValueError("VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH is invalid")
        return settings


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _environment_int(
    name: str, default: int, minimum: int, maximum: int | None = None
) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside the supported range")
    return value


def _environment_float(name: str, default: float, minimum: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    actual = value or utc_now()
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    return actual.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonicalize_youtube_url(raw_url: Any) -> tuple[str, str]:
    """Validate a concrete HTTPS YouTube URL and return id plus canonical URL."""
    if not isinstance(raw_url, str):
        raise VideoSuggestionError(
            "invalid_youtube_url", "Укажите ссылку на видео YouTube", 422
        )
    value = raw_url.strip()
    if not value or len(value) > 2048 or any(ord(character) < 32 for character in value):
        raise VideoSuggestionError(
            "invalid_youtube_url", "Некорректная ссылка на YouTube", 422
        )
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise VideoSuggestionError(
            "invalid_youtube_url", "Некорректная ссылка на YouTube", 422
        ) from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise VideoSuggestionError(
            "invalid_youtube_url", "Разрешены только HTTPS-ссылки на YouTube", 422
        )

    video_id: str | None = None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if hostname in YOUTUBE_SHORT_HOSTS:
        if len(segments) == 1:
            video_id = segments[0]
    elif hostname in YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            query = parse_qs(parsed.query, keep_blank_values=True)
            values = query.get("v", [])
            if len(values) == 1:
                video_id = values[0]
        elif len(segments) == 2 and segments[0] in {"shorts", "live", "embed"}:
            video_id = segments[1]

    if not video_id or not YOUTUBE_ID_PATTERN.fullmatch(video_id):
        raise VideoSuggestionError(
            "invalid_youtube_url",
            "Нужна ссылка на конкретное видео YouTube",
            422,
        )
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def parse_youtube_duration(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("YouTube duration is missing")
    match = YOUTUBE_DURATION_PATTERN.fullmatch(value)
    if not match or not any(match.groupdict().values()):
        raise ValueError("invalid YouTube duration")
    seconds = (
        int(match.group("days") or 0) * 86400
        + int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + float(match.group("seconds") or 0)
    )
    result = int(math.ceil(seconds))
    if result <= 0:
        raise ValueError("YouTube duration must be positive")
    return result


class YouTubeDataAPIProvider:
    """Fetch public video metadata from Google's fixed videos.list endpoint."""

    API_URL = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, api_key: str, *, timeout_seconds: float = 8.0):
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY is required")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def fetch(self, youtube_id: str) -> VideoMetadata:
        if not YOUTUBE_ID_PATTERN.fullmatch(youtube_id):
            raise ValueError("invalid YouTube id")
        query = urlencode(
            {
                "part": "snippet,contentDetails,status",
                "id": youtube_id,
                "fields": (
                    "items(id,snippet(title,publishedAt,liveBroadcastContent),"
                    "contentDetails(duration),status(privacyStatus))"
                ),
            }
        )
        request = Request(
            f"{self.API_URL}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "MORALSQD/1.0",
                "X-Goog-Api-Key": self.api_key,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise MetadataUnavailable()
                raw_payload = response.read(1024 * 1024 + 1)
                if len(raw_payload) > 1024 * 1024:
                    raise MetadataUnavailable("YouTube returned an oversized response")
                payload = json.loads(raw_payload.decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {400, 403}:
                raise MetadataUnavailable("YouTube API rejected the server request") from exc
            raise MetadataUnavailable() from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MetadataUnavailable() from exc

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise MetadataUnavailable("YouTube returned malformed metadata")
        if len(items) != 1:
            raise VideoRejected(
                "video_not_available",
                "Видео не найдено, недоступно или является приватным",
                422,
            )
        item = items[0]
        if not isinstance(item, dict) or item.get("id") != youtube_id:
            raise MetadataUnavailable("YouTube returned malformed metadata")
        snippet = item.get("snippet")
        details = item.get("contentDetails")
        status = item.get("status")
        if (
            not isinstance(snippet, dict)
            or not isinstance(details, dict)
            or not isinstance(status, dict)
        ):
            raise MetadataUnavailable("YouTube returned incomplete metadata")
        privacy_status = status.get("privacyStatus")
        if privacy_status == "private":
            raise VideoRejected(
                "video_not_available", "Приватные видео предложить нельзя", 422
            )
        if privacy_status not in {"public", "unlisted"}:
            raise MetadataUnavailable("YouTube returned an unknown privacy status")
        broadcast = snippet.get("liveBroadcastContent")
        if broadcast in {"live", "upcoming"}:
            raise VideoRejected(
                "live_video_not_allowed",
                "Текущие и будущие трансляции предложить нельзя",
                422,
            )
        if broadcast != "none":
            raise MetadataUnavailable("YouTube returned an unknown broadcast status")
        try:
            duration_seconds = parse_youtube_duration(details.get("duration"))
            published_at = parse_timestamp(str(snippet["publishedAt"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise VideoRejected(
                "video_not_available",
                "У видео нет доступных даты публикации или длительности",
                422,
            ) from exc
        if published_at > utc_now() + timedelta(minutes=5):
            raise VideoRejected(
                "live_video_not_allowed", "Будущие видео предложить нельзя", 422
            )
        title = snippet.get("title")
        if not isinstance(title, str) or not title.strip():
            raise VideoRejected(
                "video_not_available", "У видео нет доступного названия", 422
            )
        return VideoMetadata(
            youtube_id=youtube_id,
            title=title.strip()[:500],
            duration_seconds=duration_seconds,
            published_at=published_at,
        )


class SingleFlightMetadataProvider:
    """Serialize concurrent fetches for the same id without blocking other ids."""

    def __init__(self, provider: MetadataProvider):
        self.provider = provider
        self._guard = threading.Lock()
        self._locks: dict[str, tuple[threading.Lock, int]] = {}
        self._recent: dict[
            str, tuple[float, VideoMetadata | VideoSuggestionError]
        ] = {}

    def _remember_locked(
        self, youtube_id: str, value: VideoMetadata | VideoSuggestionError
    ) -> None:
        now = time.monotonic()
        self._recent[youtube_id] = (now, value)
        if len(self._recent) > 2048:
            cutoff = now - 300.0
            self._recent = {
                key: item for key, item in self._recent.items() if item[0] >= cutoff
            }

    def fetch(self, youtube_id: str) -> VideoMetadata:
        with self._guard:
            lock, users = self._locks.get(youtube_id, (threading.Lock(), 0))
            self._locks[youtube_id] = (lock, users + 1)
        try:
            with lock:
                with self._guard:
                    recent = self._recent.get(youtube_id)
                    if recent:
                        value = recent[1]
                        ttl = 300.0 if isinstance(value, VideoSuggestionError) else 2.0
                        if time.monotonic() - recent[0] <= ttl:
                            if isinstance(value, VideoSuggestionError):
                                raise value
                            return value
                    if recent:
                        self._recent.pop(youtube_id, None)
                try:
                    value = self.provider.fetch(youtube_id)
                except VideoSuggestionError as exc:
                    with self._guard:
                        self._remember_locked(youtube_id, exc)
                    raise
                with self._guard:
                    self._remember_locked(youtube_id, value)
                return value
        finally:
            with self._guard:
                current_lock, current_users = self._locks[youtube_id]
                if current_users == 1:
                    del self._locks[youtube_id]
                else:
                    self._locks[youtube_id] = (current_lock, current_users - 1)


def connect_database(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection, *, immediate: bool = False
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize_database(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                youtube_id TEXT PRIMARY KEY CHECK (length(youtube_id) = 11),
                canonical_url TEXT NOT NULL,
                title TEXT,
                duration_seconds INTEGER CHECK (
                    duration_seconds IS NULL OR duration_seconds > 0
                ),
                published_at TEXT,
                metadata_fetched_at TEXT NOT NULL,
                refresh_attempted_at TEXT,
                refresh_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                youtube_id TEXT NOT NULL REFERENCES videos(youtube_id) ON DELETE CASCADE,
                visitor_hash TEXT NOT NULL CHECK (length(visitor_hash) = 64),
                created_at TEXT NOT NULL,
                policy_accepted_at TEXT NOT NULL,
                UNIQUE (youtube_id, visitor_hash)
            );

            CREATE TABLE IF NOT EXISTS visitor_identities (
                visitor_hash TEXT PRIMARY KEY CHECK (length(visitor_hash) = 64),
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS suggestions_video_created_idx
                ON suggestions(youtube_id, created_at);

            CREATE TABLE IF NOT EXISTS admin_sessions (
                id TEXT PRIMARY KEY CHECK (length(id) = 64),
                csrf_token_hash TEXT NOT NULL CHECK (length(csrf_token_hash) = 64),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                bucket_hash TEXT NOT NULL,
                scope TEXT NOT NULL,
                window_started INTEGER NOT NULL,
                request_count INTEGER NOT NULL CHECK (request_count > 0),
                PRIMARY KEY (bucket_hash, scope, window_started)
            );
            """
        )
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in {0, SCHEMA_VERSION}:
            raise RuntimeError(
                f"video suggestions schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("administrator password must contain at least 12 characters")
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=actual_salt,
        n=PASSWORD_SCRYPT_N,
        r=PASSWORD_SCRYPT_R,
        p=PASSWORD_SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_SCRYPT_N),
            str(PASSWORD_SCRYPT_R),
            str(PASSWORD_SCRYPT_P),
            _b64encode(actual_salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        if n < 2**14 or n > 2**20 or r < 1 or r > 32 or p < 1 or p > 16:
            return False
        expected = _b64decode(raw_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(raw_salt),
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_hash_is_supported(encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt = _b64decode(raw_salt)
        digest = _b64decode(raw_digest)
    except (ValueError, TypeError):
        return False
    return (
        scheme == PASSWORD_SCHEME
        and 2**14 <= n <= 2**20
        and (n & (n - 1)) == 0
        and 8 <= r <= 32
        and 1 <= p <= 16
        and n * p >= PASSWORD_SCRYPT_N * PASSWORD_SCRYPT_P
        and len(salt) >= 16
        and len(digest) == 32
    )


def _domain_hmac(secret: bytes, domain: bytes, value: str) -> str:
    return hmac.new(
        secret, domain + b"\x00" + value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _signed_token(secret: bytes, domain: bytes, size: int = 32) -> str:
    payload = _b64encode(secrets.token_bytes(size))
    signature = _domain_hmac(secret, domain, payload)
    return f"{payload}.{signature}"


def _valid_signed_token(token: str | None, secret: bytes, domain: bytes) -> bool:
    if not token or len(token) > 256 or "." not in token:
        return False
    payload, supplied = token.rsplit(".", 1)
    if not payload or len(supplied) != 64:
        return False
    try:
        payload.encode("ascii")
        supplied.encode("ascii")
    except UnicodeError:
        return False
    return hmac.compare_digest(supplied, _domain_hmac(secret, domain, payload))


def create_visitor_token(secret: bytes) -> str:
    return _signed_token(secret, b"video-visitor-v1", 24)


def validate_visitor_token(token: str | None, secret: bytes) -> bool:
    return _valid_signed_token(token, secret, b"video-visitor-v1")


def visitor_hash(secret: bytes, token: str) -> str:
    return _domain_hmac(secret, b"video-visitor-hash-v1", token)


def register_visitor_identity(
    connection: sqlite3.Connection,
    visitor_identity_hash: str,
    *,
    now: datetime | None = None,
) -> None:
    actual_now = now or utc_now()
    connection.execute(
        """
        INSERT INTO visitor_identities(
            visitor_hash, created_at, last_seen_at, expires_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            visitor_identity_hash,
            isoformat(actual_now),
            isoformat(actual_now),
            isoformat(
                actual_now + timedelta(seconds=VISITOR_REGISTRY_TTL_SECONDS)
            ),
        ),
    )


def touch_visitor_identity(
    connection: sqlite3.Connection,
    visitor_identity_hash: str,
    *,
    now: datetime | None = None,
) -> bool:
    actual_now = now or utc_now()
    result = connection.execute(
        """
        UPDATE visitor_identities
        SET last_seen_at = ?
        WHERE visitor_hash = ? AND expires_at > ?
        """,
        (
            isoformat(actual_now),
            visitor_identity_hash,
            isoformat(actual_now),
        ),
    )
    return result.rowcount == 1


def visitor_identity_exists(
    connection: sqlite3.Connection,
    visitor_identity_hash: str,
    *,
    now: datetime | None = None,
) -> bool:
    actual_now = now or utc_now()
    return (
        connection.execute(
            """
            SELECT 1 FROM visitor_identities
            WHERE visitor_hash = ? AND expires_at > ?
            """,
            (visitor_identity_hash, isoformat(actual_now)),
        ).fetchone()
        is not None
    )


def _session_id(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _csrf_token(secret: bytes, session_token: str) -> str:
    return _b64encode(
        hmac.new(
            secret,
            b"moralsqd-video-csrf-v1\x00" + session_token.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def _csrf_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_admin_session(
    connection: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> tuple[str, str, str]:
    actual_now = now or utc_now()
    token = _signed_token(settings.secret, b"video-admin-v1")
    csrf = _csrf_token(settings.secret, token)
    session_id = _session_id(token)
    connection.execute(
        """
        INSERT INTO admin_sessions
            (id, csrf_token_hash, created_at, expires_at, last_seen_at, revoked_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            session_id,
            _csrf_hash(csrf),
            isoformat(actual_now),
            isoformat(actual_now + timedelta(seconds=settings.session_ttl_seconds)),
            isoformat(actual_now),
        ),
    )
    return token, csrf, session_id


def get_admin_session(
    connection: sqlite3.Connection,
    settings: Settings,
    token: str | None,
    *,
    touch: bool = False,
    now: datetime | None = None,
) -> sqlite3.Row | None:
    if not _valid_signed_token(token, settings.secret, b"video-admin-v1"):
        return None
    session_id = _session_id(token)  # type: ignore[arg-type]
    row = connection.execute(
        "SELECT * FROM admin_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    actual_now = now or utc_now()
    if (
        row is None
        or row["revoked_at"] is not None
        or parse_timestamp(row["expires_at"]) <= actual_now
    ):
        return None
    if touch:
        connection.execute(
            "UPDATE admin_sessions SET last_seen_at = ? WHERE id = ?",
            (isoformat(actual_now), session_id),
        )
    return row


def check_csrf(session: sqlite3.Row, supplied_token: str | None) -> bool:
    if not supplied_token or len(supplied_token) > 256:
        return False
    try:
        supplied_hash = _csrf_hash(supplied_token)
    except UnicodeError:
        return False
    return hmac.compare_digest(session["csrf_token_hash"], supplied_hash)


def consume_rate_limit(
    connection: sqlite3.Connection,
    bucket_hash: str,
    scope: str,
    limit: int,
    window_seconds: int,
    *,
    now_epoch: float | None = None,
) -> bool:
    now_value = time.time() if now_epoch is None else now_epoch
    window = int(now_value) // window_seconds * window_seconds
    row = connection.execute(
        """
        SELECT request_count FROM rate_limits
        WHERE bucket_hash = ? AND scope = ? AND window_started = ?
        """,
        (bucket_hash, scope, window),
    ).fetchone()
    current = int(row[0]) if row else 0
    if current >= limit:
        return False
    connection.execute(
        """
        INSERT INTO rate_limits(bucket_hash, scope, window_started, request_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(bucket_hash, scope, window_started)
        DO UPDATE SET request_count = request_count + 1
        """,
        (bucket_hash, scope, window),
    )
    return True


def cleanup_database(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, int]:
    actual_now = now or utc_now()
    rate_cutoff = int(actual_now.timestamp()) - 7 * 24 * 60 * 60
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            rates = connection.execute(
                "DELETE FROM rate_limits WHERE window_started < ?", (rate_cutoff,)
            ).rowcount
            sessions = connection.execute(
                """
                DELETE FROM admin_sessions
                WHERE expires_at <= ?
                   OR (revoked_at IS NOT NULL AND revoked_at <= ?)
                """,
                (
                    isoformat(actual_now),
                    isoformat(actual_now - timedelta(days=1)),
                ),
            ).rowcount
            visitors = connection.execute(
                "DELETE FROM visitor_identities WHERE expires_at <= ?",
                (isoformat(actual_now),),
            ).rowcount
    return {
        "rateLimitsDeleted": rates,
        "sessionsDeleted": sessions,
        "visitorIdentitiesDeleted": visitors,
    }


def _validate_metadata(metadata: VideoMetadata, expected_id: str, now: datetime) -> None:
    if metadata.youtube_id != expected_id or not YOUTUBE_ID_PATTERN.fullmatch(
        metadata.youtube_id
    ):
        raise MetadataUnavailable("YouTube returned metadata for the wrong video")
    if not metadata.title.strip() or metadata.duration_seconds <= 0:
        raise VideoRejected(
            "video_not_available", "Видео не содержит доступных метаданных", 422
        )
    if metadata.published_at.tzinfo is None:
        raise MetadataUnavailable("YouTube returned a timestamp without a timezone")
    if metadata.published_at > now + timedelta(minutes=5):
        raise VideoRejected(
            "live_video_not_allowed", "Будущие видео предложить нельзя", 422
        )


def store_metadata(
    connection: sqlite3.Connection,
    metadata: VideoMetadata,
    *,
    fetched_at: datetime,
) -> None:
    youtube_id = metadata.youtube_id
    connection.execute(
        """
        INSERT INTO videos(
            youtube_id, canonical_url, title, duration_seconds, published_at,
            metadata_fetched_at, refresh_attempted_at, refresh_error,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(youtube_id) DO UPDATE SET
            canonical_url = excluded.canonical_url,
            title = excluded.title,
            duration_seconds = excluded.duration_seconds,
            published_at = excluded.published_at,
            metadata_fetched_at = excluded.metadata_fetched_at,
            refresh_attempted_at = excluded.refresh_attempted_at,
            refresh_error = NULL,
            updated_at = excluded.updated_at
        WHERE excluded.metadata_fetched_at >= videos.metadata_fetched_at
        """,
        (
            youtube_id,
            f"https://www.youtube.com/watch?v={youtube_id}",
            metadata.title.strip()[:500],
            metadata.duration_seconds,
            isoformat(metadata.published_at),
            isoformat(fetched_at),
            isoformat(fetched_at),
            isoformat(fetched_at),
            isoformat(fetched_at),
        ),
    )


def mark_refresh_failure(
    settings: Settings,
    youtube_id: str,
    error: VideoSuggestionError,
    *,
    attempted_at: datetime,
    clear_derived: bool = False,
) -> None:
    expiry_cutoff = isoformat(
        attempted_at - timedelta(seconds=settings.metadata_max_age_seconds)
    )
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            connection.execute(
                """
                UPDATE videos
                SET
                    title = CASE
                        WHEN metadata_fetched_at <= ? THEN NULL ELSE title
                    END,
                    duration_seconds = CASE
                        WHEN metadata_fetched_at <= ? THEN NULL ELSE duration_seconds
                    END,
                    published_at = CASE
                        WHEN metadata_fetched_at <= ? THEN NULL ELSE published_at
                    END,
                    refresh_attempted_at = ?,
                    refresh_error = ?,
                    updated_at = ?
                WHERE youtube_id = ?
                """,
                (
                    isoformat(attempted_at) if clear_derived else expiry_cutoff,
                    isoformat(attempted_at) if clear_derived else expiry_cutoff,
                    isoformat(attempted_at) if clear_derived else expiry_cutoff,
                    isoformat(attempted_at),
                    error.code[:100],
                    isoformat(attempted_at),
                    youtube_id,
                ),
            )


def purge_expired_metadata(
    settings: Settings, *, now: datetime | None = None
) -> int:
    """Remove API-derived fields once their maximum permitted age is reached."""
    actual_now = now or utc_now()
    cutoff = isoformat(
        actual_now - timedelta(seconds=settings.metadata_max_age_seconds)
    )
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            result = connection.execute(
                """
                UPDATE videos
                SET title = NULL,
                    duration_seconds = NULL,
                    published_at = NULL,
                    updated_at = ?
                WHERE metadata_fetched_at <= ?
                  AND (
                    title IS NOT NULL
                    OR duration_seconds IS NOT NULL
                    OR published_at IS NOT NULL
                  )
                """,
                (isoformat(actual_now), cutoff),
            )
            return result.rowcount


def submit_suggestion(
    settings: Settings,
    provider: MetadataProvider,
    raw_url: Any,
    visitor_identity_hash: str,
    *,
    policy_accepted: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if policy_accepted is not True:
        raise VideoSuggestionError(
            "policy_required",
            "Нужно согласиться с условиями использования YouTube API",
            400,
        )
    actual_now = now or utc_now()
    youtube_id, canonical_url = canonicalize_youtube_url(raw_url)
    metadata: VideoMetadata | None = None
    existing: sqlite3.Row | None
    with closing(connect_database(settings)) as connection:
        if not visitor_identity_exists(
            connection, visitor_identity_hash, now=actual_now
        ):
            raise VideoSuggestionError(
                "visitor_identity_required",
                "Сначала нужно получить идентификатор посетителя",
                428,
            )
        existing = connection.execute(
            "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()

    needs_refresh = existing is None
    if existing is not None:
        age = actual_now - parse_timestamp(existing["metadata_fetched_at"])
        needs_refresh = (
            existing["title"] is None
            or existing["duration_seconds"] is None
            or existing["published_at"] is None
            or age.total_seconds() >= settings.metadata_refresh_seconds
        )
    if needs_refresh:
        try:
            metadata = provider.fetch(youtube_id)
            _validate_metadata(metadata, youtube_id, actual_now)
        except VideoSuggestionError as exc:
            if existing is not None:
                mark_refresh_failure(
                    settings,
                    youtube_id,
                    exc,
                    attempted_at=actual_now,
                    clear_derived=isinstance(exc, VideoRejected),
                )
                if isinstance(exc, VideoRejected):
                    raise
                cache_age = actual_now - parse_timestamp(existing["metadata_fetched_at"])
                if cache_age.total_seconds() < settings.metadata_max_age_seconds:
                    metadata = None
                else:
                    raise MetadataUnavailable(
                        "Не удалось обновить данные видео. Попробуйте позже"
                    ) from exc
            else:
                raise
        except Exception as exc:
            unavailable = MetadataUnavailable()
            if existing is not None:
                mark_refresh_failure(
                    settings, youtube_id, unavailable, attempted_at=actual_now
                )
                cache_age = actual_now - parse_timestamp(existing["metadata_fetched_at"])
                if cache_age.total_seconds() < settings.metadata_max_age_seconds:
                    metadata = None
                else:
                    raise unavailable from exc
            else:
                raise unavailable from exc

    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            if not touch_visitor_identity(
                connection, visitor_identity_hash, now=actual_now
            ):
                raise VideoSuggestionError(
                    "visitor_identity_required",
                    "Идентификатор посетителя больше не действует",
                    428,
                )
            if metadata is not None:
                store_metadata(connection, metadata, fetched_at=actual_now)
            video = connection.execute(
                "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
            ).fetchone()
            if video is None:
                raise MetadataUnavailable()
            result = connection.execute(
                """
                INSERT OR IGNORE INTO suggestions(
                    youtube_id, visitor_hash, created_at, policy_accepted_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    youtube_id,
                    visitor_identity_hash,
                    isoformat(actual_now),
                    isoformat(actual_now),
                ),
            )
            duplicate = result.rowcount == 0
            request_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM suggestions WHERE youtube_id = ?",
                    (youtube_id,),
                ).fetchone()[0]
            )
            video = connection.execute(
                "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
            ).fetchone()

    return {
        "ok": True,
        "duplicate": duplicate,
        "video": {
            "youtubeId": youtube_id,
            "url": canonical_url,
            "title": video["title"],
            "durationSeconds": int(video["duration_seconds"]),
            "publishedAt": video["published_at"],
        },
        "requestCount": request_count,
    }


def delete_visitor_suggestions(
    settings: Settings, visitor_identity_hash: str
) -> int | None:
    """Delete one browser identity's requests and only its newly orphaned videos."""
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            if not visitor_identity_exists(connection, visitor_identity_hash):
                return None
            youtube_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT youtube_id FROM suggestions WHERE visitor_hash = ?",
                    (visitor_identity_hash,),
                ).fetchall()
            ]
            deleted_count = connection.execute(
                "DELETE FROM suggestions WHERE visitor_hash = ?",
                (visitor_identity_hash,),
            ).rowcount
            if youtube_ids:
                placeholders = ",".join("?" for _ in youtube_ids)
                connection.execute(
                    f"""
                    DELETE FROM videos
                    WHERE youtube_id IN ({placeholders})
                      AND NOT EXISTS (
                        SELECT 1 FROM suggestions
                        WHERE suggestions.youtube_id = videos.youtube_id
                      )
                    """,
                    youtube_ids,
                )
            connection.execute(
                "DELETE FROM visitor_identities WHERE visitor_hash = ?",
                (visitor_identity_hash,),
            )
    return deleted_count


def freshness_for(
    published_at: datetime, *, now: datetime | None = None
) -> tuple[str, str]:
    actual_now = now or utc_now()
    age_seconds = max(0.0, (actual_now - published_at).total_seconds())
    if age_seconds <= FRESH_SECONDS:
        return "fresh", "Свежее"
    if age_seconds <= MODERATE_SECONDS:
        return "moderate", "Не очень свежее"
    return "old", "Старенькое"


def _metadata_refresh_candidates(
    settings: Settings,
    *,
    now: datetime,
    youtube_id: str | None = None,
    force: bool = False,
) -> list[str]:
    with closing(connect_database(settings)) as connection:
        if youtube_id is not None:
            row = connection.execute(
                "SELECT youtube_id FROM videos WHERE youtube_id = ?", (youtube_id,)
            ).fetchone()
            if row is None:
                raise VideoSuggestionError("video_not_found", "Видео не найдено", 404)
            return [youtube_id]
        refresh_cutoff = isoformat(
            now - timedelta(seconds=settings.metadata_refresh_seconds)
        )
        retry_cutoff = isoformat(now - timedelta(seconds=settings.metadata_retry_seconds))
        query = """
            SELECT youtube_id FROM videos
            WHERE metadata_fetched_at <= ?
              AND (refresh_error IS NULL OR refresh_error = 'metadata_unavailable')
              AND (? OR refresh_attempted_at IS NULL OR refresh_attempted_at <= ?)
            ORDER BY COALESCE(refresh_attempted_at, metadata_fetched_at) ASC,
                     youtube_id ASC
            LIMIT ?
        """
        return [
            str(row[0])
            for row in connection.execute(
                query,
                (
                    refresh_cutoff,
                    1 if force else 0,
                    retry_cutoff,
                    settings.metadata_refresh_batch_size,
                ),
            ).fetchall()
        ]


def refresh_metadata(
    settings: Settings,
    provider: MetadataProvider,
    *,
    youtube_id: str | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    actual_now = now or utc_now()
    if youtube_id is not None and not YOUTUBE_ID_PATTERN.fullmatch(youtube_id):
        raise VideoSuggestionError("invalid_youtube_id", "Некорректный ID видео", 422)
    candidates = _metadata_refresh_candidates(
        settings,
        now=actual_now,
        youtube_id=youtube_id,
        force=force,
    )
    refreshed: list[str] = []
    failed: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            metadata = provider.fetch(candidate)
            _validate_metadata(metadata, candidate, actual_now)
            with closing(connect_database(settings)) as connection:
                with transaction(connection, immediate=True):
                    store_metadata(connection, metadata, fetched_at=actual_now)
            refreshed.append(candidate)
        except VideoSuggestionError as exc:
            mark_refresh_failure(
                settings,
                candidate,
                exc,
                attempted_at=actual_now,
                clear_derived=isinstance(exc, VideoRejected),
            )
            failed.append({"youtubeId": candidate, "error": exc.code})
        except Exception:
            unavailable = MetadataUnavailable()
            mark_refresh_failure(
                settings, candidate, unavailable, attempted_at=actual_now
            )
            failed.append({"youtubeId": candidate, "error": unavailable.code})
    return {"refreshed": refreshed, "failed": failed}


def maintain_metadata(
    settings: Settings,
    provider: MetadataProvider,
    *,
    now: datetime | None = None,
    refresh_limit: int = 100,
) -> dict[str, Any]:
    """Refresh bounded due batches, then remove every remaining expired value."""
    actual_now = now or utc_now()
    refreshed: list[str] = []
    failed: list[dict[str, str]] = []
    while len(refreshed) + len(failed) < refresh_limit:
        batch = refresh_metadata(settings, provider, now=actual_now)
        batch_size = len(batch["refreshed"]) + len(batch["failed"])
        if batch_size == 0:
            break
        refreshed.extend(batch["refreshed"])
        failed.extend(batch["failed"])
    purged = purge_expired_metadata(settings, now=actual_now)
    cleanup_result = cleanup_database(settings, now=actual_now)
    return {
        "serverTime": isoformat(actual_now),
        "refreshed": refreshed,
        "failed": failed,
        "expiredMetadataPurged": purged,
        **cleanup_result,
    }


def list_admin_videos(
    settings: Settings, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    actual_now = now or utc_now()
    purge_expired_metadata(settings, now=actual_now)
    cutoff = isoformat(actual_now - timedelta(seconds=settings.metadata_max_age_seconds))
    with closing(connect_database(settings)) as connection:
        rows = connection.execute(
            """
            SELECT
                v.youtube_id,
                v.canonical_url,
                v.title,
                v.duration_seconds,
                v.published_at,
                COUNT(s.id) AS request_count,
                MIN(s.created_at) AS first_requested_at,
                MAX(s.created_at) AS last_requested_at
            FROM videos AS v
            JOIN suggestions AS s ON s.youtube_id = v.youtube_id
            WHERE v.metadata_fetched_at >= ?
              AND v.title IS NOT NULL
              AND v.duration_seconds IS NOT NULL
              AND v.published_at IS NOT NULL
            GROUP BY v.youtube_id
            ORDER BY request_count DESC, v.published_at DESC, v.youtube_id ASC
            """,
            (cutoff,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        freshness, freshness_label = freshness_for(
            parse_timestamp(row["published_at"]), now=actual_now
        )
        result.append(
            {
                "youtubeId": row["youtube_id"],
                "url": row["canonical_url"],
                "title": row["title"],
                "durationSeconds": int(row["duration_seconds"]),
                "publishedAt": row["published_at"],
                "freshness": freshness,
                "freshnessLabel": freshness_label,
                "requestCount": int(row["request_count"]),
                "firstRequestedAt": row["first_requested_at"],
                "lastRequestedAt": row["last_requested_at"],
            }
        )
    return result


class VideoSuggestionsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        settings: Settings,
        metadata_provider: MetadataProvider | None = None,
    ):
        self.settings = settings
        actual_provider = metadata_provider or YouTubeDataAPIProvider(
            settings.youtube_api_key, timeout_seconds=settings.youtube_timeout_seconds
        )
        self.metadata_provider = SingleFlightMetadataProvider(actual_provider)
        self.login_kdf_semaphore = threading.BoundedSemaphore(
            settings.login_kdf_slots
        )
        self._last_metadata_purge = 0.0
        self._last_database_cleanup = 0.0
        initialize_database(settings)
        purge_expired_metadata(settings)
        cleanup_database(settings)
        handler = partial(VideoSuggestionsHandler, directory=str(settings.static_dir))
        super().__init__(address, handler)

    def service_actions(self) -> None:
        now = time.monotonic()
        if now - self._last_metadata_purge < 60:
            return
        self._last_metadata_purge = now
        try:
            purge_expired_metadata(self.settings)
        except Exception as exc:  # pragma: no cover - operational logging path
            print(f"video suggestions metadata purge error: {exc}", file=sys.stderr)
        if now - self._last_database_cleanup >= 60 * 60:
            self._last_database_cleanup = now
            try:
                cleanup_database(self.settings)
            except Exception as exc:  # pragma: no cover - operational logging path
                print(f"video suggestions cleanup error: {exc}", file=sys.stderr)


class VideoSuggestionsHandler(SimpleHTTPRequestHandler):
    server: VideoSuggestionsServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == f"{API_PREFIX}/admin/session":
            self._dispatch(self._handle_session)
            return
        if path == f"{API_PREFIX}/admin/videos":
            self._dispatch(self._handle_admin_videos)
            return
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
            self.send_json(
                {"error": "method_not_allowed", "message": "Метод не поддерживается"},
                status=405,
            )
            return
        self._serve_static()

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
            self.send_json(
                {"error": "method_not_allowed", "message": "Метод не поддерживается"},
                status=405,
            )
            return
        self._serve_static(head_only=True)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        handlers = {
            API_PREFIX: self._handle_submit,
            f"{API_PREFIX}/identity": self._handle_identity,
            f"{API_PREFIX}/delete-mine": self._handle_delete_mine,
            f"{API_PREFIX}/admin/login": self._handle_login,
            f"{API_PREFIX}/admin/logout": self._handle_logout,
            f"{API_PREFIX}/admin/refresh": self._handle_admin_refresh,
        }
        handler = handlers.get(path)
        if handler is None:
            self.send_json({"error": "not_found", "message": "Не найдено"}, status=404)
            return
        self._dispatch(handler)

    def do_OPTIONS(self) -> None:
        self.send_json(
            {
                "error": "method_not_allowed",
                "message": "Cross-origin запросы не поддерживаются",
            },
            status=405,
        )

    def _serve_static(self, *, head_only: bool = False) -> None:
        original_path = self.path
        try:
            requested_path = urlsplit(original_path).path
            if requested_path in {"/dk-video-inbox", "/dk-video-inbox/"}:
                self.path = "/dk-video-inbox/index.html"
            elif requested_path in {"/privacy", "/privacy/"}:
                self.path = "/privacy/index.html"
            elif requested_path == "/":
                self.path = "/index.html"
            if not self._static_path_allowed(urlsplit(self.path).path):
                self.send_error(404, "File not found")
                return
            if head_only:
                super().do_HEAD()
            else:
                super().do_GET()
        finally:
            self.path = original_path

    @staticmethod
    def _static_path_allowed(path: str) -> bool:
        try:
            decoded_path = unquote(path, errors="strict")
        except UnicodeDecodeError:
            return False
        relative = decoded_path.lstrip("/")
        candidate = PurePosixPath(relative)
        if not relative or any(part in {"", ".", ".."} for part in candidate.parts):
            return False
        if len(candidate.parts) == 1:
            return candidate.name in ROOT_STATIC_FILES
        return (
            candidate.parts[0] in NESTED_STATIC_DIRECTORIES
            and candidate.suffix.lower() in STATIC_EXTENSIONS
            and not any(part.startswith(".") for part in candidate.parts)
        )

    def _dispatch(self, handler: Any) -> None:
        try:
            handler()
        except VideoSuggestionError as exc:
            self.send_json(exc.as_dict(), status=exc.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(
                {"error": "invalid_json", "message": "Некорректный JSON"}, status=400
            )
        except sqlite3.IntegrityError as exc:
            print(f"video suggestions integrity error: {exc}", file=sys.stderr)
            self.send_json(
                {"error": "conflict", "message": "Конфликт данных"}, status=409
            )
        except Exception as exc:  # pragma: no cover - operational safety net
            print(
                f"video suggestions API error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            self.send_json(
                {"error": "internal_error", "message": "Внутренняя ошибка сервера"},
                status=500,
            )

    def _handle_identity(self) -> None:
        self._require_same_origin()
        payload = self._read_json(required=True)
        if payload.get("policyAccepted") is not True:
            raise VideoSuggestionError(
                "policy_required",
                "Нужно согласиться с условиями использования YouTube API",
                400,
            )
        settings = self.server.settings
        token = self._read_cookie(VISITOR_COOKIE_NAME)
        bucket = self._client_bucket_hash()
        existing_identity = False
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                if validate_visitor_token(token, settings.secret):
                    current_hash = visitor_hash(settings.secret, token)
                    if touch_visitor_identity(connection, current_hash):
                        existing_identity = True
                if not existing_identity:
                    if not consume_rate_limit(
                        connection,
                        bucket,
                        "visitor_issue",
                        settings.visitor_issue_limit,
                        24 * 60 * 60,
                    ):
                        raise VideoSuggestionError(
                            "rate_limited",
                            "Слишком много попыток. Попробуйте позже",
                            429,
                        )
                    identity_global_bucket = _domain_hmac(
                        settings.secret, b"rate-global-v1", "visitor-issue"
                    )
                    if not consume_rate_limit(
                        connection,
                        identity_global_bucket,
                        "visitor_issue_global",
                        settings.visitor_global_issue_limit,
                        24 * 60 * 60,
                    ):
                        raise VideoSuggestionError(
                            "rate_limited",
                            "Слишком много попыток. Попробуйте позже",
                            429,
                        )
                    token = create_visitor_token(settings.secret)
                    register_visitor_identity(
                        connection, visitor_hash(settings.secret, token)
                    )
        if existing_identity:
            self.send_json({"ready": True})
            return
        self.send_json(
            {"ready": True}, status=201, set_cookie=self._visitor_cookie(token)
        )

    def _handle_submit(self) -> None:
        self._require_same_origin()
        payload = self._read_json(required=True)
        if payload.get("policyAccepted") is not True:
            raise VideoSuggestionError(
                "policy_required",
                "Нужно согласиться с условиями использования YouTube API",
                400,
            )
        # Validate the URL before spending an identity issuance slot or API quota.
        canonicalize_youtube_url(payload.get("url"))
        settings = self.server.settings
        bucket = self._client_bucket_hash()
        token = self._read_cookie(VISITOR_COOKIE_NAME)
        if not validate_visitor_token(token, settings.secret):
            raise VideoSuggestionError(
                "visitor_identity_required",
                "Сначала нужно получить идентификатор посетителя",
                428,
            )
        identity_hash = visitor_hash(settings.secret, token)
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                if not visitor_identity_exists(connection, identity_hash):
                    raise VideoSuggestionError(
                        "visitor_identity_required",
                        "Идентификатор посетителя больше не действует",
                        428,
                    )
                if not consume_rate_limit(
                    connection,
                    bucket,
                    "public_bucket",
                    settings.public_bucket_limit,
                    settings.rate_window_seconds,
                ):
                    raise VideoSuggestionError(
                        "rate_limited", "Слишком много попыток. Попробуйте позже", 429
                    )
                global_bucket = _domain_hmac(
                    settings.secret, b"rate-global-v1", "public-submit"
                )
                if not consume_rate_limit(
                    connection,
                    global_bucket,
                    "public_global",
                    settings.public_global_limit,
                    settings.rate_window_seconds,
                ):
                    raise VideoSuggestionError(
                        "rate_limited", "Слишком много попыток. Попробуйте позже", 429
                    )
                if not consume_rate_limit(
                    connection,
                    identity_hash,
                    "public_visitor",
                    settings.public_visitor_limit,
                    settings.rate_window_seconds,
                ):
                    raise VideoSuggestionError(
                        "rate_limited", "Слишком много попыток. Попробуйте позже", 429
                    )
        result = submit_suggestion(
            settings,
            self.server.metadata_provider,
            payload.get("url"),
            identity_hash,
            policy_accepted=True,
        )
        self.send_json(
            result,
            status=200 if result["duplicate"] else 201,
        )

    def _handle_delete_mine(self) -> None:
        self._require_same_origin()
        settings = self.server.settings
        token = self._read_cookie(VISITOR_COOKIE_NAME)
        if not validate_visitor_token(token, settings.secret):
            raise VideoSuggestionError(
                "visitor_identity_required",
                "Не найден идентификатор посетителя",
                428,
            )
        deleted_count = delete_visitor_suggestions(
            settings, visitor_hash(settings.secret, token)
        )
        if deleted_count is None:
            self.send_json(
                {
                    "error": "visitor_identity_required",
                    "message": "Идентификатор посетителя больше не действует",
                },
                status=428,
                set_cookie=self._visitor_cookie("", clear=True),
            )
            return
        self.send_json(
            {"ok": True, "deletedCount": deleted_count},
            set_cookie=self._visitor_cookie("", clear=True),
        )

    def _handle_session(self) -> None:
        settings = self.server.settings
        token = self._read_cookie(ADMIN_COOKIE_NAME)
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                session = get_admin_session(connection, settings, token, touch=True)
        payload = (
            {"authenticated": False, "csrfToken": None}
            if session is None
            else {
                "authenticated": True,
                "csrfToken": _csrf_token(settings.secret, token),  # type: ignore[arg-type]
            }
        )
        self.send_json(payload)

    def _handle_login(self) -> None:
        self._require_same_origin()
        payload = self._read_json(required=True)
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise VideoSuggestionError(
                "invalid_credentials", "Неверный логин или пароль", 401
            )
        if len(username) > 100 or len(password) > 1000:
            raise VideoSuggestionError(
                "invalid_credentials", "Неверный логин или пароль", 401
            )
        settings = self.server.settings
        bucket = self._client_bucket_hash()
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                client_allowed = consume_rate_limit(
                    connection,
                    bucket,
                    "admin_login",
                    settings.login_rate_limit,
                    settings.rate_window_seconds,
                )
                global_allowed = (
                    consume_rate_limit(
                        connection,
                        _domain_hmac(settings.secret, b"rate-global-v1", "login"),
                        "admin_login_global",
                        max(20, settings.login_rate_limit * 10),
                        settings.rate_window_seconds,
                    )
                    if client_allowed
                    else True
                )
            if not client_allowed:
                raise VideoSuggestionError(
                    "rate_limited", "Слишком много попыток входа", 429
                )
            if not global_allowed:
                raise VideoSuggestionError(
                    "rate_limited", "Слишком много попыток входа", 429
                )
            if not self.server.login_kdf_semaphore.acquire(blocking=False):
                raise VideoSuggestionError(
                    "rate_limited", "Сервер занят проверкой входа. Попробуйте позже", 429
                )
            try:
                credentials_valid = verify_password(
                    password, settings.admin_password_hash
                )
            finally:
                self.server.login_kdf_semaphore.release()
            try:
                username_valid = hmac.compare_digest(
                    username.encode("utf-8"), settings.admin_username.encode("utf-8")
                )
            except UnicodeError:
                username_valid = False
            if not username_valid or not credentials_valid:
                raise VideoSuggestionError(
                    "invalid_credentials", "Неверный логин или пароль", 401
                )
            with transaction(connection, immediate=True):
                connection.execute(
                    "DELETE FROM admin_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                    (isoformat(),),
                )
                token, csrf, _ = create_admin_session(connection, settings)
        self.send_json(
            {"authenticated": True, "csrfToken": csrf},
            set_cookie=self._admin_cookie(token),
        )

    def _handle_logout(self) -> None:
        session_id = self._require_admin(mutating=True)
        settings = self.server.settings
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE admin_sessions SET revoked_at = ? WHERE id = ?",
                    (isoformat(), session_id),
                )
        self.send_json(
            {"authenticated": False, "csrfToken": None},
            set_cookie=self._admin_cookie("", clear=True),
        )

    def _handle_admin_videos(self) -> None:
        self._require_admin(mutating=False)
        settings = self.server.settings
        now = utc_now()
        self.send_json(
            {"serverTime": isoformat(now), "videos": list_admin_videos(settings, now=now)}
        )

    def _handle_admin_refresh(self) -> None:
        self._require_admin(mutating=True)
        payload = self._read_json(required=False)
        youtube_id = payload.get("youtubeId")
        if youtube_id is not None and not isinstance(youtube_id, str):
            raise VideoSuggestionError(
                "invalid_youtube_id", "Некорректный ID видео", 422
            )
        settings = self.server.settings
        result = refresh_metadata(
            settings,
            self.server.metadata_provider,
            youtube_id=youtube_id,
            force=youtube_id is not None,
        )
        now = utc_now()
        self.send_json(
            {
                **result,
                "serverTime": isoformat(now),
                "videos": list_admin_videos(settings, now=now),
            }
        )

    def _require_admin(self, *, mutating: bool) -> str:
        if mutating:
            self._require_same_origin()
        settings = self.server.settings
        token = self._read_cookie(ADMIN_COOKIE_NAME)
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                session = get_admin_session(connection, settings, token, touch=True)
                if session is None:
                    raise VideoSuggestionError(
                        "authentication_required", "Требуется авторизация", 401
                    )
                if mutating and not check_csrf(
                    session, self.headers.get("X-CSRF-Token")
                ):
                    raise VideoSuggestionError(
                        "csrf_rejected", "CSRF-токен отклонён", 403
                    )
                if not consume_rate_limit(
                    connection,
                    str(session["id"]),
                    "admin_mutation" if mutating else "admin_read",
                    settings.admin_rate_limit,
                    settings.rate_window_seconds,
                ):
                    raise VideoSuggestionError(
                        "rate_limited", "Слишком много запросов", 429
                    )
                return str(session["id"])

    def _read_json(self, *, required: bool) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise VideoSuggestionError(
                "invalid_request", "Некорректный Content-Length", 400
            ) from exc
        if length < 0 or length > self.server.settings.max_request_bytes:
            raise VideoSuggestionError(
                "request_too_large", "Запрос слишком большой", 413
            )
        if length == 0:
            if required:
                raise VideoSuggestionError(
                    "invalid_json", "Требуется JSON-объект", 400
                )
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type.strip() != "application/json":
            raise VideoSuggestionError(
                "unsupported_media_type", "Требуется application/json", 415
            )
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise VideoSuggestionError(
                "invalid_json", "JSON должен быть объектом", 400
            )
        return payload

    def _require_same_origin(self) -> None:
        settings = self.server.settings
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0]
        scheme = forwarded_proto.strip() or ("https" if settings.secure_cookie else "http")
        host = self.headers.get("Host", "")
        expected = (settings.allowed_origin or f"{scheme}://{host}").rstrip("/")
        origin = self.headers.get("Origin")
        if origin:
            supplied = origin.rstrip("/")
        else:
            referer = self.headers.get("Referer")
            try:
                parsed = urlsplit(referer) if referer else None
            except ValueError:
                parsed = None
            supplied = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else ""
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            raise VideoSuggestionError(
                "origin_rejected", "Источник запроса отклонён", 403
            )

    def _read_cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def _visitor_cookie(self, token: str, *, clear: bool = False) -> str:
        parts = [
            f"{VISITOR_COOKIE_NAME}={token}",
            "Path=/",
            f"Max-Age={0 if clear else VISITOR_COOKIE_MAX_AGE}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if clear:
            parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
        if self._request_is_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _admin_cookie(self, token: str, *, clear: bool = False) -> str:
        parts = [
            f"{ADMIN_COOKIE_NAME}={token}",
            f"Path={API_PREFIX}/admin",
            f"Max-Age={0 if clear else self.server.settings.session_ttl_seconds}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if clear:
            parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
        if self._request_is_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _request_is_secure(self) -> bool:
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0]
        return self.server.settings.secure_cookie or forwarded_proto.strip() == "https"

    def _client_bucket_hash(self) -> str:
        raw_ip = self.client_address[0]
        try:
            address = ipaddress.ip_address(raw_ip)
            if address.is_loopback:
                forwarded = self.headers.get("X-Real-IP", "").split(",", 1)[0].strip()
                if forwarded:
                    candidate = ipaddress.ip_address(forwarded)
                    if not candidate.is_private and not candidate.is_loopback:
                        address = candidate
            prefix_length = 24 if address.version == 4 else 56
            prefix = str(
                ipaddress.ip_network(
                    f"{address}/{prefix_length}", strict=False
                ).network_address
            )
        except ValueError:
            prefix = "unknown"
        return _domain_hmac(
            self.server.settings.secret,
            b"video-client-bucket-v1",
            prefix,
        )

    def send_json(
        self,
        payload: Mapping[str, Any],
        *,
        status: int = 200,
        set_cookie: str | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        path = urlsplit(self.path).path
        if path == "/dk-video-inbox" or path.startswith("/dk-video-inbox/"):
            self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
                "object-src 'none'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'",
            )
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """Log the request result without BaseHTTPRequestHandler's raw client IP."""
        status = str(args[1]) if len(args) > 1 else "-"
        safe_path = urlsplit(self.path).path[:300]
        print(
            f"video suggestions HTTP {self.command} {safe_path} {status}",
            file=sys.stderr,
        )


def generate_admin_credentials() -> tuple[str, str]:
    password = secrets.token_urlsafe(24)
    return password, hash_password(password)


def maintenance_exit_code(result: Mapping[str, Any]) -> int:
    failures = result.get("failed")
    if not isinstance(failures, Sequence):
        return 1
    return (
        1
        if any(
            isinstance(failure, Mapping)
            and failure.get("error") == "metadata_unavailable"
            for failure in failures
        )
        else 0
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve MORAL SQD video suggestions")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--generate-admin-credentials",
        action="store_true",
        help="print a generated password and its scrypt environment value",
    )
    parser.add_argument(
        "--maintain-metadata",
        action="store_true",
        help="refresh due metadata, purge expired API fields, then exit",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    if arguments.generate_admin_credentials:
        password, encoded = generate_admin_credentials()
        print(f"VIDEO_SUGGESTIONS_ADMIN_USERNAME={ADMIN_USERNAME}")
        print(f"VIDEO_SUGGESTIONS_ADMIN_PASSWORD={password}")
        print(f"VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH={encoded}")
        return
    settings = Settings.from_environment()
    if arguments.maintain_metadata:
        initialize_database(settings)
        provider = SingleFlightMetadataProvider(
            YouTubeDataAPIProvider(
                settings.youtube_api_key,
                timeout_seconds=settings.youtube_timeout_seconds,
            )
        )
        maintenance_result = maintain_metadata(settings, provider)
        print(
            json.dumps(
                maintenance_result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        raise SystemExit(maintenance_exit_code(maintenance_result))
    host = arguments.host or settings.host
    port = arguments.port or settings.port
    server = VideoSuggestionsServer((host, port), settings)
    print(f"video suggestions listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
