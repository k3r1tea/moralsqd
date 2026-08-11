#!/usr/bin/env python3
"""MORAL SQD auction domain service.

The module deliberately uses only the Python 3.10 standard library.  The
domain functions are independent from HTTP so they can be tested directly and
used by a small ``ThreadingHTTPServer`` adapter.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Mapping, NoReturn, Sequence
from urllib.parse import parse_qs, urlsplit


API_PREFIX = "/api/auc"
COOKIE_NAME = "moralsqd_auction_admin"
SCHEMA_VERSION = 1

AUCTION_MODES = frozenset({"leader", "weighted-wheel"})
AUCTION_STATES = frozenset(
    {"DRAFT", "OPEN", "LOCKED", "RESOLVING", "FINISHED", "CANCELLED"}
)
ACTIVE_STATES = ("DRAFT", "OPEN", "LOCKED", "RESOLVING")

COMMITMENT_DOMAIN = b"moralsqd-auction-commitment-v1\x00"
DRAW_DOMAIN = b"moralsqd-auction-draw-v1\x00"
DRAW_ALGORITHM = "MORALSQD-AUCTION-HMAC-SHA256-REJECTION-V1"
PASSWORD_SCHEME = "scrypt"
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
MAX_OPTIONS = 64
MAX_CONTRIBUTION_KOPECKS = 1_000_000_000_00
MAX_TOTAL_KOPECKS = 9_000_000_000_000_000
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class AuctionError(Exception):
    """Expected domain error carrying an HTTP-compatible status and code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class Settings:
    secret: bytes
    admin_password_hash: str
    db_path: Path
    static_dir: Path
    host: str = "127.0.0.1"
    port: int = 8788
    secure_cookie: bool = True
    allowed_origin: str | None = None
    session_ttl_seconds: int = 8 * 60 * 60
    login_rate_limit: int = 5
    admin_rate_limit: int = 120
    rate_window_seconds: int = 60
    scheduler_interval_seconds: float = 0.5
    max_request_bytes: int = 64 * 1024

    @classmethod
    def from_environment(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parent
        raw_secret = os.environ.get("AUCTION_SECRET", "local-development-only")
        password_hash = os.environ.get("AUCTION_ADMIN_PASSWORD_HASH", "").strip()
        secure_cookie = _environment_bool("AUCTION_SECURE_COOKIE", True)
        if not password_hash:
            raw_password = os.environ.get(
                "AUCTION_ADMIN_PASSWORD", "local-development-only"
            )
            # A stable development salt keeps local sessions usable across
            # restarts. Production deployments should always provide the hash.
            password_hash = hash_password(
                raw_password,
                salt=hashlib.sha256(
                    b"moralsqd-auction-dev-password\x00" + raw_secret.encode("utf-8")
                ).digest()[:16],
            )

        allowed_origin = os.environ.get("AUCTION_ALLOWED_ORIGIN", "").strip() or None
        settings = cls(
            secret=raw_secret.encode("utf-8"),
            admin_password_hash=password_hash,
            db_path=Path(
                os.environ.get("AUCTION_DB_PATH", str(project_dir / "auction.db"))
            ),
            static_dir=Path(os.environ.get("AUCTION_STATIC_DIR", str(project_dir))),
            host=os.environ.get("AUCTION_HOST", "127.0.0.1"),
            port=_environment_int("AUCTION_PORT", 8788, minimum=1, maximum=65535),
            secure_cookie=secure_cookie,
            allowed_origin=allowed_origin,
            session_ttl_seconds=_environment_int(
                "AUCTION_SESSION_TTL_SECONDS", 8 * 60 * 60, minimum=300
            ),
            login_rate_limit=_environment_int(
                "AUCTION_LOGIN_RATE_LIMIT", 5, minimum=1
            ),
            admin_rate_limit=_environment_int(
                "AUCTION_ADMIN_RATE_LIMIT", 120, minimum=1
            ),
            rate_window_seconds=_environment_int(
                "AUCTION_RATE_WINDOW_SECONDS", 60, minimum=1
            ),
            scheduler_interval_seconds=_environment_float(
                "AUCTION_SCHEDULER_INTERVAL_SECONDS", 0.5, minimum=0.1
            ),
            max_request_bytes=_environment_int(
                "AUCTION_MAX_REQUEST_BYTES", 64 * 1024, minimum=1024
            ),
        )
        if len(settings.secret) < 32 and raw_secret != "local-development-only":
            raise ValueError("AUCTION_SECRET must contain at least 32 UTF-8 bytes")
        if settings.secure_cookie and raw_secret == "local-development-only":
            raise ValueError("AUCTION_SECRET must be set when secure cookies are enabled")
        if settings.secure_cookie and not os.environ.get("AUCTION_ADMIN_PASSWORD_HASH", "").strip():
            raise ValueError(
                "AUCTION_ADMIN_PASSWORD_HASH is required when secure cookies are enabled"
            )
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
    name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _environment_float(name: str, default: float, *, minimum: float) -> float:
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
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def connect_database(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(
        settings.db_path,
        timeout=10,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
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


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 10000),
    mode TEXT NOT NULL CHECK (mode IN ('leader', 'weighted-wheel')),
    state TEXT NOT NULL DEFAULT 'DRAFT'
        CHECK (state IN ('DRAFT','OPEN','LOCKED','RESOLVING','FINISHED','CANCELLED')),
    duration_seconds INTEGER NOT NULL CHECK (
        typeof(duration_seconds) = 'integer' AND duration_seconds BETWEEN 1 AND 2592000
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    ends_at TEXT,
    locked_at TEXT,
    finished_at TEXT,
    cancelled_at TEXT,
    cancel_reason TEXT CHECK (cancel_reason IS NULL OR length(cancel_reason) BETWEEN 1 AND 2000),
    seed BLOB,
    commitment TEXT,
    CHECK ((state = 'DRAFT' AND started_at IS NULL AND ends_at IS NULL)
        OR state <> 'DRAFT'),
    CHECK ((seed IS NULL AND commitment IS NULL) OR
        (typeof(seed) = 'blob' AND length(seed) = 32 AND length(commitment) = 64))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_auction
    ON auctions ((1))
    WHERE state IN ('DRAFT','OPEN','LOCKED','RESOLVING');
CREATE INDEX IF NOT EXISTS auctions_created_idx ON auctions(created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL REFERENCES auctions(id),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    name_key TEXT NOT NULL CHECK (length(name_key) BETWEEN 1 AND 400),
    sort_order INTEGER NOT NULL CHECK (typeof(sort_order) = 'integer'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (auction_id, name_key),
    UNIQUE (auction_id, sort_order)
);
CREATE INDEX IF NOT EXISTS options_auction_idx ON options(auction_id, sort_order, id);

CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL REFERENCES auctions(id),
    option_id INTEGER NOT NULL REFERENCES options(id),
    kind TEXT NOT NULL CHECK (kind IN ('ENTRY','VOID')),
    amount_kopecks INTEGER NOT NULL CHECK (
        typeof(amount_kopecks) = 'integer' AND amount_kopecks BETWEEN 1 AND 9000000000000000
    ),
    original_contribution_id INTEGER REFERENCES contributions(id),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) BETWEEN 1 AND 200),
    provider TEXT CHECK (provider IS NULL OR length(provider) BETWEEN 1 AND 100),
    external_id TEXT CHECK (external_id IS NULL OR length(external_id) BETWEEN 1 AND 300),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 2000),
    created_at TEXT NOT NULL,
    created_by_session_id TEXT,
    CHECK ((provider IS NULL) = (external_id IS NULL)),
    CHECK ((kind = 'ENTRY' AND original_contribution_id IS NULL)
        OR (kind = 'VOID' AND original_contribution_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS contributions_provider_event_unique
    ON contributions(provider, external_id)
    WHERE provider IS NOT NULL AND external_id IS NOT NULL AND kind = 'ENTRY';
CREATE UNIQUE INDEX IF NOT EXISTS contribution_void_once
    ON contributions(original_contribution_id)
    WHERE kind = 'VOID';
CREATE INDEX IF NOT EXISTS contributions_auction_idx
    ON contributions(auction_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS contributions_option_idx
    ON contributions(option_id, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL UNIQUE REFERENCES auctions(id),
    canonical_json TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL CHECK (length(snapshot_sha256) = 64),
    total_kopecks INTEGER NOT NULL CHECK (
        typeof(total_kopecks) = 'integer' AND total_kopecks >= 0
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL UNIQUE REFERENCES auctions(id),
    snapshot_id INTEGER NOT NULL UNIQUE REFERENCES snapshots(id),
    winner_option_id INTEGER REFERENCES options(id),
    seed_hex TEXT NOT NULL CHECK (length(seed_hex) = 64),
    commitment TEXT NOT NULL CHECK (length(commitment) = 64),
    algorithm TEXT NOT NULL,
    hmac_counter INTEGER,
    hmac_digest_hex TEXT,
    rejection_limit_decimal TEXT,
    selected_offset INTEGER,
    draw_space INTEGER NOT NULL CHECK (draw_space >= 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispute_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER NOT NULL REFERENCES auctions(id),
    forced_winner_option_id INTEGER REFERENCES options(id),
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL,
    created_by_session_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS disputes_auction_idx
    ON dispute_resolutions(auction_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id INTEGER REFERENCES auctions(id),
    action TEXT NOT NULL CHECK (length(action) BETWEEN 1 AND 100),
    reason TEXT CHECK (reason IS NULL OR length(reason) <= 2000),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS audit_auction_idx
    ON audit_events(auction_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,
    csrf_token_hash TEXT NOT NULL CHECK (length(csrf_token_hash) = 64),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS admin_sessions_expiry_idx ON admin_sessions(expires_at);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket_hash TEXT NOT NULL,
    scope TEXT NOT NULL,
    window_started INTEGER NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 1),
    PRIMARY KEY (bucket_hash, scope, window_started)
);

CREATE TRIGGER IF NOT EXISTS contributions_no_update
BEFORE UPDATE ON contributions BEGIN
    SELECT RAISE(ABORT, 'contributions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS contributions_no_delete
BEFORE DELETE ON contributions BEGIN
    SELECT RAISE(ABORT, 'contributions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_update
BEFORE UPDATE ON snapshots BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
BEFORE DELETE ON snapshots BEGIN
    SELECT RAISE(ABORT, 'snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS results_no_update
BEFORE UPDATE ON results BEGIN
    SELECT RAISE(ABORT, 'results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS results_no_delete
BEFORE DELETE ON results BEGIN
    SELECT RAISE(ABORT, 'results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS dispute_resolutions_no_update
BEFORE UPDATE ON dispute_resolutions BEGIN
    SELECT RAISE(ABORT, 'dispute resolutions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS dispute_resolutions_no_delete
BEFORE DELETE ON dispute_resolutions BEGIN
    SELECT RAISE(ABORT, 'dispute resolutions are immutable');
END;
"""


def initialize_database(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        with transaction(connection, immediate=True):
            connection.executescript(SCHEMA_SQL)
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current not in (0, SCHEMA_VERSION):
                raise RuntimeError(
                    f"auction database schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute(
                """
                INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create the portable scrypt verifier stored in the protected env file."""
    if not isinstance(password, str) or len(password) < 10:
        raise ValueError("administrator password must contain at least 10 characters")
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


def _signed_token(secret: bytes, size: int = 32) -> str:
    payload = _b64encode(secrets.token_bytes(size))
    signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _valid_signed_token(token: str | None, secret: bytes) -> bool:
    if not token or len(token) > 256 or "." not in token:
        return False
    payload, supplied = token.rsplit(".", 1)
    if not payload or len(supplied) != 64:
        return False
    try:
        payload_bytes = payload.encode("ascii")
        supplied.encode("ascii")
    except UnicodeError:
        return False
    expected = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _csrf_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _csrf_token_for_session(secret: bytes, session_token: str) -> str:
    return _b64encode(
        hmac.new(
            secret,
            b"moralsqd-auction-csrf-v1\x00" + session_token.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )


def create_admin_session(
    connection: sqlite3.Connection, settings: Settings
) -> tuple[str, str, str]:
    token = _signed_token(settings.secret)
    csrf_token = _csrf_token_for_session(settings.secret, token)
    session_id = _token_id(token)
    now = utc_now()
    connection.execute(
        """
        INSERT INTO admin_sessions
            (id, csrf_token_hash, created_at, expires_at, last_seen_at, revoked_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            session_id,
            _csrf_hash(csrf_token),
            isoformat(now),
            isoformat(now + timedelta(seconds=settings.session_ttl_seconds)),
            isoformat(now),
        ),
    )
    return token, csrf_token, session_id


def get_admin_session(
    connection: sqlite3.Connection,
    settings: Settings,
    token: str | None,
    *,
    touch: bool = False,
) -> sqlite3.Row | None:
    if not _valid_signed_token(token, settings.secret):
        return None
    session_id = _token_id(token)  # type: ignore[arg-type]
    row = connection.execute(
        "SELECT * FROM admin_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if parse_timestamp(row["expires_at"]) <= utc_now():
        return None
    if touch:
        connection.execute(
            "UPDATE admin_sessions SET last_seen_at = ? WHERE id = ?",
            (isoformat(), session_id),
        )
    return row


def check_csrf(session: sqlite3.Row, supplied_token: str | None) -> bool:
    if not supplied_token or len(supplied_token) > 256:
        return False
    return hmac.compare_digest(session["csrf_token_hash"], _csrf_hash(supplied_token))


def consume_rate_limit(
    connection: sqlite3.Connection,
    bucket_hash: str,
    scope: str,
    limit: int,
    window_seconds: int,
) -> bool:
    window = int(time.time()) // window_seconds * window_seconds
    row = connection.execute(
        """
        SELECT request_count FROM rate_limits
        WHERE bucket_hash = ? AND scope = ? AND window_started = ?
        """,
        (bucket_hash, scope, window),
    ).fetchone()
    count = int(row[0]) if row else 0
    if count >= limit:
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
    if secrets.randbelow(100) == 0:
        connection.execute(
            "DELETE FROM rate_limits WHERE window_started < ?", (window - 86400,)
        )
    return True


def validate_text(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int,
    forbid_html: bool = False,
) -> str:
    if not isinstance(value, str):
        raise AuctionError("invalid_input", f"{field} must be a string", 422)
    normalized = value.strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        raise AuctionError(
            "invalid_input", f"{field} length must be between {minimum} and {maximum}", 422
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise AuctionError("invalid_input", f"{field} contains control characters", 422)
    if forbid_html and ("<" in normalized or ">" in normalized):
        raise AuctionError("invalid_input", f"{field} must not contain HTML", 422)
    return normalized


def require_integer(
    value: Any, field: str, *, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuctionError("invalid_input", f"{field} must be an integer", 422)
    if value < minimum or value > maximum:
        raise AuctionError(
            "invalid_input", f"{field} must be between {minimum} and {maximum}", 422
        )
    return value


def require_id(value: Any, field: str = "id") -> int:
    return require_integer(value, field, minimum=1, maximum=2**63 - 1)


def validate_request_id(value: Any) -> str:
    request_id = validate_text(value, "requestId", minimum=8, maximum=200)
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AuctionError("invalid_input", "requestId has an invalid format", 422)
    return request_id


def validate_option_name(value: Any) -> tuple[str, str]:
    name = unicodedata.normalize(
        "NFC",
        validate_text(value, "name", minimum=1, maximum=200, forbid_html=True),
    )
    if any(unicodedata.category(character).startswith("C") for character in name):
        raise AuctionError("invalid_input", "name contains invisible control characters", 422)
    return name, name.casefold()


def record_audit(
    connection: sqlite3.Connection,
    action: str,
    *,
    auction_id: int | None = None,
    reason: str | None = None,
    details: Mapping[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events
            (auction_id, action, reason, details_json, created_at, session_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            auction_id,
            action,
            reason,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            isoformat(),
            session_id,
        ),
    )


def fetch_auction(connection: sqlite3.Connection, auction_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
    if row is None:
        raise AuctionError("auction_not_found", "auction not found", 404)
    return row


def fetch_option(connection: sqlite3.Connection, option_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM options WHERE id = ?", (option_id,)).fetchone()
    if row is None:
        raise AuctionError("option_not_found", "option not found", 404)
    return row


def option_totals(connection: sqlite3.Connection, auction_id: int) -> dict[int, int]:
    rows = connection.execute(
        """
        SELECT option_id,
               COALESCE(SUM(CASE kind WHEN 'ENTRY' THEN amount_kopecks
                                      ELSE -amount_kopecks END), 0) AS total
        FROM contributions
        WHERE auction_id = ?
        GROUP BY option_id
        """,
        (auction_id,),
    ).fetchall()
    totals = {int(row["option_id"]): int(row["total"]) for row in rows}
    if any(value < 0 for value in totals.values()):
        raise RuntimeError("contribution ledger produced a negative option total")
    return totals


def public_options(connection: sqlite3.Connection, auction_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM options WHERE auction_id = ? ORDER BY sort_order, id", (auction_id,)
    ).fetchall()
    totals = option_totals(connection, auction_id)
    total = sum(totals.values())
    amounts = [totals.get(int(row["id"]), 0) for row in rows]
    result: list[dict[str, Any]] = []
    for row, amount in zip(rows, amounts):
        result.append(
            {
                "id": int(row["id"]),
                "name": row["name"],
                "sortOrder": int(row["sort_order"]),
                "amountKopecks": amount,
                "shareBasisPoints": (amount * 10000 // total) if total else 0,
                "rank": 1 + sum(1 for candidate in amounts if candidate > amount),
            }
        )
    return result


def _validate_auction_payload(payload: Mapping[str, Any]) -> tuple[str, str, str, int]:
    title = validate_text(
        payload.get("title"), "title", minimum=1, maximum=200, forbid_html=True
    )
    description = validate_text(
        payload.get("description", ""), "description", maximum=10000
    )
    mode = payload.get("mode")
    if mode not in AUCTION_MODES:
        raise AuctionError("invalid_input", "mode must be leader or weighted-wheel", 422)
    duration = require_integer(
        payload.get("durationSeconds"),
        "durationSeconds",
        minimum=1,
        maximum=2_592_000,
    )
    return title, description, str(mode), duration


def create_auction(
    connection: sqlite3.Connection, payload: Mapping[str, Any], session_id: str
) -> int:
    title, description, mode, duration = _validate_auction_payload(payload)
    now = isoformat()
    try:
        with transaction(connection, immediate=True):
            cursor = connection.execute(
                """
                INSERT INTO auctions
                    (title, description, mode, state, duration_seconds, created_at, updated_at)
                VALUES (?, ?, ?, 'DRAFT', ?, ?, ?)
                """,
                (title, description, mode, duration, now, now),
            )
            auction_id = int(cursor.lastrowid)
            record_audit(
                connection,
                "auction_created",
                auction_id=auction_id,
                details={"title": title, "mode": mode, "durationSeconds": duration},
                session_id=session_id,
            )
    except sqlite3.IntegrityError as exc:
        if "one_active_auction" in str(exc) or "UNIQUE constraint failed: index" in str(exc):
            raise AuctionError(
                "active_auction_exists", "another active auction already exists", 409
            ) from exc
        raise
    return auction_id


def update_auction(
    connection: sqlite3.Connection,
    auction_id: int,
    payload: Mapping[str, Any],
    session_id: str,
) -> None:
    with transaction(connection, immediate=True):
        current = fetch_auction(connection, auction_id)
        if current["state"] != "DRAFT":
            raise AuctionError("invalid_state", "only a draft can be edited", 409)
        merged = {
            "title": payload.get("title", current["title"]),
            "description": payload.get("description", current["description"]),
            "mode": payload.get("mode", current["mode"]),
            "durationSeconds": payload.get("durationSeconds", current["duration_seconds"]),
        }
        title, description, mode, duration = _validate_auction_payload(merged)
        connection.execute(
            """
            UPDATE auctions SET title = ?, description = ?, mode = ?,
                duration_seconds = ?, updated_at = ? WHERE id = ?
            """,
            (title, description, mode, duration, isoformat(), auction_id),
        )
        record_audit(
            connection,
            "auction_updated",
            auction_id=auction_id,
            details={"title": title, "mode": mode, "durationSeconds": duration},
            session_id=session_id,
        )


def add_option(
    connection: sqlite3.Connection,
    auction_id: int,
    payload: Mapping[str, Any],
    session_id: str,
) -> int:
    name, name_key = validate_option_name(payload.get("name"))
    with transaction(connection, immediate=True):
        auction = fetch_auction(connection, auction_id)
        if auction["state"] != "DRAFT":
            raise AuctionError("invalid_state", "options are fixed after start", 409)
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM options WHERE auction_id = ?", (auction_id,)
            ).fetchone()[0]
        )
        if count >= MAX_OPTIONS:
            raise AuctionError("too_many_options", "option limit reached", 422)
        next_order = int(
            connection.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM options WHERE auction_id = ?",
                (auction_id,),
            ).fetchone()[0]
        )
        try:
            cursor = connection.execute(
                """
                INSERT INTO options
                    (auction_id, name, name_key, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (auction_id, name, name_key, next_order, isoformat(), isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise AuctionError("duplicate_option", "option name already exists", 409) from exc
        option_id = int(cursor.lastrowid)
        record_audit(
            connection,
            "option_added",
            auction_id=auction_id,
            details={"optionId": option_id, "name": name},
            session_id=session_id,
        )
    return option_id


def update_option(
    connection: sqlite3.Connection,
    option_id: int,
    payload: Mapping[str, Any],
    session_id: str,
) -> None:
    name, name_key = validate_option_name(payload.get("name"))
    with transaction(connection, immediate=True):
        option = fetch_option(connection, option_id)
        auction = fetch_auction(connection, int(option["auction_id"]))
        if auction["state"] != "DRAFT":
            raise AuctionError("invalid_state", "options are fixed after start", 409)
        try:
            connection.execute(
                "UPDATE options SET name = ?, name_key = ?, updated_at = ? WHERE id = ?",
                (name, name_key, isoformat(), option_id),
            )
        except sqlite3.IntegrityError as exc:
            raise AuctionError("duplicate_option", "option name already exists", 409) from exc
        record_audit(
            connection,
            "option_updated",
            auction_id=int(option["auction_id"]),
            details={"optionId": option_id, "name": name},
            session_id=session_id,
        )


def delete_option(
    connection: sqlite3.Connection, option_id: int, session_id: str
) -> None:
    with transaction(connection, immediate=True):
        option = fetch_option(connection, option_id)
        auction_id = int(option["auction_id"])
        if fetch_auction(connection, auction_id)["state"] != "DRAFT":
            raise AuctionError("invalid_state", "options are fixed after start", 409)
        connection.execute("DELETE FROM options WHERE id = ?", (option_id,))
        record_audit(
            connection,
            "option_deleted",
            auction_id=auction_id,
            details={"optionId": option_id, "name": option["name"]},
            session_id=session_id,
        )


def merge_option(
    connection: sqlite3.Connection,
    source_option_id: int,
    target_option_id: int,
    session_id: str,
) -> None:
    if source_option_id == target_option_id:
        raise AuctionError("invalid_input", "an option cannot be merged into itself", 422)
    with transaction(connection, immediate=True):
        source = fetch_option(connection, source_option_id)
        target = fetch_option(connection, target_option_id)
        auction_id = int(source["auction_id"])
        if int(target["auction_id"]) != auction_id:
            raise AuctionError("invalid_input", "options belong to different auctions", 422)
        if fetch_auction(connection, auction_id)["state"] != "DRAFT":
            raise AuctionError("invalid_state", "options are fixed after start", 409)
        connection.execute("DELETE FROM options WHERE id = ?", (source_option_id,))
        record_audit(
            connection,
            "options_merged",
            auction_id=auction_id,
            details={
                "sourceOptionId": source_option_id,
                "sourceName": source["name"],
                "targetOptionId": target_option_id,
                "targetName": target["name"],
            },
            session_id=session_id,
        )


def seed_commitment(seed: bytes) -> str:
    return hashlib.sha256(COMMITMENT_DOMAIN + seed).hexdigest()


def start_auction(
    connection: sqlite3.Connection, auction_id: int, session_id: str
) -> None:
    with transaction(connection, immediate=True):
        auction = fetch_auction(connection, auction_id)
        if auction["state"] != "DRAFT":
            raise AuctionError("invalid_state", "only a draft can be started", 409)
        option_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM options WHERE auction_id = ?", (auction_id,)
            ).fetchone()[0]
        )
        if option_count < 2:
            raise AuctionError("not_enough_options", "at least two options are required", 422)
        seed = secrets.token_bytes(32)
        now = utc_now()
        ends_at = now + timedelta(seconds=int(auction["duration_seconds"]))
        connection.execute(
            """
            UPDATE auctions SET state = 'OPEN', started_at = ?, ends_at = ?,
                seed = ?, commitment = ?, updated_at = ? WHERE id = ?
            """,
            (isoformat(now), isoformat(ends_at), seed, seed_commitment(seed), isoformat(now), auction_id),
        )
        record_audit(
            connection,
            "auction_started",
            auction_id=auction_id,
            details={"endsAt": isoformat(ends_at), "commitment": seed_commitment(seed)},
            session_id=session_id,
        )


def _serialize_contribution(
    connection: sqlite3.Connection, contribution_id: int, *, public: bool
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT c.*, o.name AS option_name FROM contributions c
        JOIN options o ON o.id = c.option_id WHERE c.id = ?
        """,
        (contribution_id,),
    ).fetchone()
    if row is None:
        raise AuctionError("contribution_not_found", "contribution not found", 404)
    payload: dict[str, Any] = {
        "id": int(row["id"]),
        "optionId": int(row["option_id"]),
        "optionName": row["option_name"],
        "amountKopecks": int(row["amount_kopecks"]),
        "kind": row["kind"],
        "createdAt": row["created_at"],
    }
    if row["kind"] == "ENTRY":
        payload["voided"] = (
            connection.execute(
                """
                SELECT 1 FROM contributions
                WHERE original_contribution_id = ? AND kind = 'VOID'
                """,
                (int(row["id"]),),
            ).fetchone()
            is not None
        )
    if not public:
        payload.update(
            {
                "originalContributionId": row["original_contribution_id"],
                "requestId": row["request_id"],
                "note": row["note"],
            }
        )
    return payload


def add_contribution(
    connection: sqlite3.Connection,
    auction_id: int,
    payload: Mapping[str, Any],
    session_id: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    option_id = require_id(payload.get("optionId"), "optionId")
    amount = require_integer(
        payload.get("amountKopecks"),
        "amountKopecks",
        minimum=1,
        maximum=MAX_CONTRIBUTION_KOPECKS,
    )
    request_id = validate_request_id(payload.get("requestId"))
    note = validate_text(payload.get("note", ""), "note", maximum=2000)
    provider_value = payload.get("provider")
    external_value = payload.get("externalId")
    if (provider_value is None) != (external_value is None):
        raise AuctionError(
            "invalid_input", "provider and externalId must be supplied together", 422
        )
    provider = (
        validate_text(provider_value, "provider", minimum=1, maximum=100)
        if provider_value is not None
        else None
    )
    external_id = (
        validate_text(external_value, "externalId", minimum=1, maximum=300)
        if external_value is not None
        else None
    )
    actual_now = now or utc_now()
    with transaction(connection, immediate=True):
        duplicate = connection.execute(
            "SELECT * FROM contributions WHERE request_id = ?", (request_id,)
        ).fetchone()
        if duplicate is None and provider is not None:
            duplicate = connection.execute(
                """
                SELECT * FROM contributions
                WHERE provider = ? AND external_id = ? AND kind = 'ENTRY'
                """,
                (provider, external_id),
            ).fetchone()
        if duplicate is not None:
            if (
                duplicate["kind"] != "ENTRY"
                or int(duplicate["auction_id"]) != auction_id
                or int(duplicate["option_id"]) != option_id
                or int(duplicate["amount_kopecks"]) != amount
                or duplicate["provider"] != provider
                or duplicate["external_id"] != external_id
            ):
                raise AuctionError(
                    "idempotency_conflict",
                    "requestId or provider event was already used with different data",
                    409,
                )
            return _serialize_contribution(connection, int(duplicate["id"]), public=False), True

        auction = fetch_auction(connection, auction_id)
        if auction["state"] != "OPEN":
            raise AuctionError("betting_closed", "auction is not accepting contributions", 409)
        if parse_timestamp(auction["ends_at"]) <= actual_now:
            raise AuctionError("betting_closed", "auction deadline has passed", 409)
        option = fetch_option(connection, option_id)
        if int(option["auction_id"]) != auction_id:
            raise AuctionError("invalid_option", "option does not belong to auction", 422)
        current_total = sum(option_totals(connection, auction_id).values())
        if current_total + amount > MAX_TOTAL_KOPECKS:
            raise AuctionError("amount_limit", "auction total would exceed the safe limit", 422)
        cursor = connection.execute(
            """
            INSERT INTO contributions
                (auction_id, option_id, kind, amount_kopecks, original_contribution_id,
                 request_id, provider, external_id, note, created_at, created_by_session_id)
            VALUES (?, ?, 'ENTRY', ?, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                auction_id,
                option_id,
                amount,
                request_id,
                provider,
                external_id,
                note,
                isoformat(actual_now),
                session_id,
            ),
        )
        contribution_id = int(cursor.lastrowid)
        record_audit(
            connection,
            "contribution_added",
            auction_id=auction_id,
            details={
                "contributionId": contribution_id,
                "optionId": option_id,
                "amountKopecks": amount,
            },
            session_id=session_id,
        )
        result = _serialize_contribution(connection, contribution_id, public=False)
    return result, False


def void_contribution(
    connection: sqlite3.Connection,
    contribution_id: int,
    payload: Mapping[str, Any],
    session_id: str,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    reason = validate_text(payload.get("reason"), "reason", minimum=1, maximum=2000)
    request_id = validate_request_id(payload.get("requestId"))
    actual_now = now or utc_now()
    with transaction(connection, immediate=True):
        duplicate_request = connection.execute(
            "SELECT * FROM contributions WHERE request_id = ?", (request_id,)
        ).fetchone()
        if duplicate_request is not None:
            if (
                duplicate_request["kind"] != "VOID"
                or int(duplicate_request["original_contribution_id"] or 0)
                != contribution_id
            ):
                raise AuctionError(
                    "idempotency_conflict",
                    "requestId was already used for another operation",
                    409,
                )
            return _serialize_contribution(
                connection, int(duplicate_request["id"]), public=False
            ), True
        original = connection.execute(
            "SELECT * FROM contributions WHERE id = ?", (contribution_id,)
        ).fetchone()
        if original is None:
            raise AuctionError("contribution_not_found", "contribution not found", 404)
        if original["kind"] != "ENTRY":
            raise AuctionError("invalid_contribution", "only an entry can be voided", 409)
        auction_id = int(original["auction_id"])
        auction = fetch_auction(connection, auction_id)
        if auction["state"] != "OPEN" or parse_timestamp(auction["ends_at"]) <= actual_now:
            raise AuctionError("betting_closed", "auction is not accepting corrections", 409)
        existing = connection.execute(
            "SELECT id FROM contributions WHERE original_contribution_id = ? AND kind = 'VOID'",
            (contribution_id,),
        ).fetchone()
        if existing is not None:
            raise AuctionError("already_voided", "contribution was already voided", 409)
        cursor = connection.execute(
            """
            INSERT INTO contributions
                (auction_id, option_id, kind, amount_kopecks, original_contribution_id,
                 request_id, provider, external_id, note, created_at, created_by_session_id)
            VALUES (?, ?, 'VOID', ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                auction_id,
                int(original["option_id"]),
                int(original["amount_kopecks"]),
                contribution_id,
                request_id,
                reason,
                isoformat(actual_now),
                session_id,
            ),
        )
        void_id = int(cursor.lastrowid)
        record_audit(
            connection,
            "contribution_voided",
            auction_id=auction_id,
            reason=reason,
            details={"contributionId": contribution_id, "voidContributionId": void_id},
            session_id=session_id,
        )
        result = _serialize_contribution(connection, void_id, public=False)
    return result, False


def canonical_snapshot(
    connection: sqlite3.Connection, auction: sqlite3.Row
) -> tuple[str, str, int, list[dict[str, Any]]]:
    totals = option_totals(connection, int(auction["id"]))
    rows = connection.execute(
        "SELECT id, name FROM options WHERE auction_id = ? ORDER BY id",
        (int(auction["id"]),),
    ).fetchall()
    options = [
        {
            "amountKopecks": totals.get(int(row["id"]), 0),
            "id": int(row["id"]),
            "name": row["name"],
        }
        for row in rows
    ]
    payload = {
        "algorithmVersion": DRAW_ALGORITHM,
        "auctionId": int(auction["id"]),
        "mode": auction["mode"],
        "options": options,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, snapshot_hash, sum(item["amountKopecks"] for item in options), options


def uniform_draw(seed: bytes, snapshot_hash: str, upper_bound: int) -> dict[str, Any]:
    """Map HMAC-SHA256 to [0, upper_bound) without modulo bias."""
    if upper_bound <= 0 or upper_bound > MAX_TOTAL_KOPECKS:
        raise ValueError("upper_bound is outside the supported draw range")
    domain_size = 1 << 256
    rejection_limit = domain_size - domain_size % upper_bound
    for counter in range(1_000_000):
        message = DRAW_DOMAIN + bytes.fromhex(snapshot_hash) + counter.to_bytes(8, "big")
        digest = hmac.new(seed, message, hashlib.sha256).digest()
        candidate = int.from_bytes(digest, "big")
        if candidate < rejection_limit:
            return {
                "counter": counter,
                "digestHex": digest.hex(),
                "rejectionLimit": str(rejection_limit),
                "selectedOffset": candidate % upper_bound,
            }
    raise RuntimeError("rejection sampling did not converge")


def lock_auction(
    settings: Settings,
    auction_id: int,
    *,
    session_id: str | None,
    reason: str | None,
    force: bool,
    now: datetime | None = None,
) -> str:
    actual_now = now or utc_now()
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            auction = fetch_auction(connection, auction_id)
            if auction["state"] in {"LOCKED", "RESOLVING", "FINISHED"}:
                return str(auction["state"])
            if auction["state"] != "OPEN":
                raise AuctionError("invalid_state", "auction cannot be closed", 409)
            if not force and parse_timestamp(auction["ends_at"]) > actual_now:
                return "OPEN"
            canonical, snapshot_hash, total, _ = canonical_snapshot(connection, auction)
            connection.execute(
                """
                INSERT INTO snapshots
                    (auction_id, canonical_json, snapshot_sha256, total_kopecks, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (auction_id, canonical, snapshot_hash, total, isoformat(actual_now)),
            )
            connection.execute(
                """
                UPDATE auctions SET state = 'LOCKED', locked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (isoformat(actual_now), isoformat(actual_now), auction_id),
            )
            record_audit(
                connection,
                "auction_locked",
                auction_id=auction_id,
                reason=reason,
                details={
                    "automatic": not force,
                    "snapshotHash": snapshot_hash,
                    "totalKopecks": total,
                },
                session_id=session_id,
            )
    return "LOCKED"


def _resolve_winner(
    auction: sqlite3.Row,
    snapshot_options: Sequence[Mapping[str, Any]],
    snapshot_hash: str,
) -> dict[str, Any]:
    seed = bytes(auction["seed"])
    if seed_commitment(seed) != auction["commitment"]:
        raise RuntimeError("auction seed does not match its published commitment")
    positive = [item for item in snapshot_options if int(item["amountKopecks"]) > 0]
    if not positive:
        return {
            "winnerOptionId": None,
            "reason": "Общая сумма равна нулю — победитель не определён.",
            "drawSpace": 0,
            "draw": None,
        }

    if auction["mode"] == "leader":
        maximum = max(int(item["amountKopecks"]) for item in positive)
        candidates = [
            item for item in positive if int(item["amountKopecks"]) == maximum
        ]
        draw = uniform_draw(seed, snapshot_hash, len(candidates))
        winner = candidates[int(draw["selectedOffset"])]
        reason = (
            "Единственный лидер победил по максимальной сумме."
            if len(candidates) == 1
            else f"Ничья между {len(candidates)} лидерами разрешена проверяемой жеребьёвкой."
        )
        return {
            "winnerOptionId": int(winner["id"]),
            "reason": reason,
            "drawSpace": len(candidates),
            "draw": draw,
        }

    total = sum(int(item["amountKopecks"]) for item in positive)
    draw = uniform_draw(seed, snapshot_hash, total)
    offset = int(draw["selectedOffset"])
    cursor = 0
    winner_id: int | None = None
    for item in positive:
        cursor += int(item["amountKopecks"])
        if offset < cursor:
            winner_id = int(item["id"])
            break
    if winner_id is None:
        raise RuntimeError("weighted draw did not map to an option")
    return {
        "winnerOptionId": winner_id,
        "reason": (
            "Единственный вариант с положительной суммой победил автоматически."
            if len(positive) == 1
            else "Победитель выбран пропорционально финальным суммам."
        ),
        "drawSpace": total,
        "draw": draw,
    }


def resolve_auction(
    settings: Settings, auction_id: int, *, session_id: str | None = None
) -> dict[str, Any]:
    with closing(connect_database(settings)) as connection:
        with transaction(connection, immediate=True):
            auction = fetch_auction(connection, auction_id)
            if auction["state"] == "FINISHED":
                result = serialize_result(connection, auction_id)
                if result is None:
                    raise RuntimeError("finished auction has no result")
                return result
            if auction["state"] not in {"LOCKED", "RESOLVING"}:
                raise AuctionError("invalid_state", "auction is not locked", 409)
            snapshot = connection.execute(
                "SELECT * FROM snapshots WHERE auction_id = ?", (auction_id,)
            ).fetchone()
            if snapshot is None:
                raise RuntimeError("locked auction has no snapshot")
            connection.execute(
                "UPDATE auctions SET state = 'RESOLVING', updated_at = ? WHERE id = ?",
                (isoformat(), auction_id),
            )
            snapshot_payload = json.loads(snapshot["canonical_json"])
            resolution = _resolve_winner(
                auction, snapshot_payload["options"], snapshot["snapshot_sha256"]
            )
            draw = resolution["draw"]
            connection.execute(
                """
                INSERT INTO results
                    (auction_id, snapshot_id, winner_option_id, seed_hex, commitment,
                     algorithm, hmac_counter, hmac_digest_hex, rejection_limit_decimal,
                     selected_offset, draw_space, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    auction_id,
                    int(snapshot["id"]),
                    resolution["winnerOptionId"],
                    bytes(auction["seed"]).hex(),
                    auction["commitment"],
                    DRAW_ALGORITHM,
                    draw["counter"] if draw else None,
                    draw["digestHex"] if draw else None,
                    draw["rejectionLimit"] if draw else None,
                    draw["selectedOffset"] if draw else None,
                    resolution["drawSpace"],
                    resolution["reason"],
                    isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE auctions SET state = 'FINISHED', finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (isoformat(), isoformat(), auction_id),
            )
            record_audit(
                connection,
                "auction_resolved",
                auction_id=auction_id,
                details={
                    "winnerOptionId": resolution["winnerOptionId"],
                    "algorithm": DRAW_ALGORITHM,
                },
                session_id=session_id,
            )
        result = serialize_result(connection, auction_id)
        if result is None:
            raise RuntimeError("result insert was not visible after commit")
        return result


def close_auction(
    settings: Settings,
    auction_id: int,
    *,
    session_id: str | None,
    reason: str | None = None,
    force: bool = True,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    state = lock_auction(
        settings,
        auction_id,
        session_id=session_id,
        reason=reason,
        force=force,
        now=now,
    )
    if state == "OPEN":
        return None
    return resolve_auction(settings, auction_id, session_id=session_id)


def cancel_auction(
    connection: sqlite3.Connection,
    auction_id: int,
    reason: str,
    session_id: str,
) -> None:
    normalized_reason = validate_text(reason, "reason", minimum=1, maximum=2000)
    with transaction(connection, immediate=True):
        auction = fetch_auction(connection, auction_id)
        if auction["state"] not in {"DRAFT", "OPEN"}:
            raise AuctionError("invalid_state", "auction can no longer be cancelled", 409)
        if auction["state"] == "OPEN" and parse_timestamp(auction["ends_at"]) <= utc_now():
            raise AuctionError(
                "deadline_reached",
                "auction deadline has passed and its result must be resolved",
                409,
            )
        connection.execute(
            """
            UPDATE auctions SET state = 'CANCELLED', cancel_reason = ?,
                cancelled_at = ?, updated_at = ? WHERE id = ?
            """,
            (normalized_reason, isoformat(), isoformat(), auction_id),
        )
        record_audit(
            connection,
            "auction_cancelled",
            auction_id=auction_id,
            reason=normalized_reason,
            session_id=session_id,
        )


def resolve_dispute(
    connection: sqlite3.Connection,
    auction_id: int,
    option_id: int,
    reason: str,
    session_id: str,
) -> None:
    normalized_reason = validate_text(reason, "reason", minimum=1, maximum=2000)
    with transaction(connection, immediate=True):
        auction = fetch_auction(connection, auction_id)
        if auction["state"] != "FINISHED":
            raise AuctionError(
                "invalid_state", "a dispute can be resolved only after finish", 409
            )
        option = fetch_option(connection, option_id)
        if int(option["auction_id"]) != auction_id:
            raise AuctionError("invalid_option", "option does not belong to auction", 422)
        cursor = connection.execute(
            """
            INSERT INTO dispute_resolutions
                (auction_id, forced_winner_option_id, reason, created_at, created_by_session_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (auction_id, option_id, normalized_reason, isoformat(), session_id),
        )
        record_audit(
            connection,
            "dispute_resolved",
            auction_id=auction_id,
            reason=normalized_reason,
            details={"resolutionId": int(cursor.lastrowid), "forcedWinnerOptionId": option_id},
            session_id=session_id,
        )


def advance_auctions(settings: Settings) -> None:
    """Close expired OPEN auctions and recover committed intermediate states."""
    with closing(connect_database(settings)) as connection:
        rows = connection.execute(
            "SELECT id, state, ends_at FROM auctions WHERE state IN ('OPEN','LOCKED','RESOLVING')"
        ).fetchall()
    now = utc_now()
    for row in rows:
        try:
            if row["state"] == "OPEN":
                if parse_timestamp(row["ends_at"]) <= now:
                    close_auction(
                        settings,
                        int(row["id"]),
                        session_id=None,
                        reason="server deadline reached",
                        force=False,
                        now=now,
                    )
            else:
                resolve_auction(settings, int(row["id"]), session_id=None)
        except (AuctionError, sqlite3.OperationalError):
            # Another request or scheduler tick may have completed it first.
            continue


def serialize_result(
    connection: sqlite3.Connection, auction_id: int
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT r.*, s.canonical_json, s.snapshot_sha256, s.total_kopecks,
               o.name AS original_winner_name
        FROM results r
        JOIN snapshots s ON s.id = r.snapshot_id
        LEFT JOIN options o ON o.id = r.winner_option_id
        WHERE r.auction_id = ?
        """,
        (auction_id,),
    ).fetchone()
    if row is None:
        return None
    dispute = connection.execute(
        """
        SELECT d.*, o.name AS forced_winner_name
        FROM dispute_resolutions d
        LEFT JOIN options o ON o.id = d.forced_winner_option_id
        WHERE d.auction_id = ? ORDER BY d.id DESC LIMIT 1
        """,
        (auction_id,),
    ).fetchone()
    effective_id = (
        int(dispute["forced_winner_option_id"])
        if dispute is not None and dispute["forced_winner_option_id"] is not None
        else (int(row["winner_option_id"]) if row["winner_option_id"] is not None else None)
    )
    effective_name = (
        dispute["forced_winner_name"] if dispute is not None else row["original_winner_name"]
    )
    snapshot = json.loads(row["canonical_json"])
    mode = snapshot["mode"]
    return {
        "id": int(row["id"]),
        "auctionId": auction_id,
        "winnerOptionId": effective_id,
        "winnerName": effective_name,
        "originalWinnerOptionId": (
            int(row["winner_option_id"]) if row["winner_option_id"] is not None else None
        ),
        "originalWinnerName": row["original_winner_name"],
        "originalReason": row["reason"],
        "reason": dispute["reason"] if dispute is not None else row["reason"],
        "forced": dispute is not None,
        "seed": row["seed_hex"],
        "seedReveal": row["seed_hex"],
        "seedCommitment": row["commitment"],
        "snapshotHash": row["snapshot_sha256"],
        "snapshot": snapshot,
        "canonicalSnapshot": row["canonical_json"],
        "algorithm": row["algorithm"],
        "selectionRule": (
            "weighted cumulative integer ranges in option-id order"
            if mode == "weighted-wheel"
            else "maximum total, then selectedOffset indexes tied leaders in option-id order"
        ),
        "hmacCounter": row["hmac_counter"],
        "hmacDigest": row["hmac_digest_hex"],
        "rejectionLimit": row["rejection_limit_decimal"],
        "drawValue": row["selected_offset"],
        "selectedOffset": row["selected_offset"],
        "drawSpace": int(row["draw_space"]),
        "totalWeight": int(row["draw_space"]),
        "snapshotTotalKopecks": int(row["total_kopecks"]),
        "moderatorResolution": (
            {
                "id": int(dispute["id"]),
                "winnerOptionId": effective_id,
                "winnerName": effective_name,
                "reason": dispute["reason"],
                "createdAt": dispute["created_at"],
            }
            if dispute is not None
            else None
        ),
    }


def serialize_auction(
    connection: sqlite3.Connection, auction: sqlite3.Row
) -> dict[str, Any]:
    options = public_options(connection, int(auction["id"]))
    payload: dict[str, Any] = {
        "id": int(auction["id"]),
        "title": auction["title"],
        "description": auction["description"],
        "mode": auction["mode"],
        "status": auction["state"],
        "durationSeconds": int(auction["duration_seconds"]),
        "createdAt": auction["created_at"],
        "updatedAt": auction["updated_at"],
        "startsAt": auction["started_at"],
        "endsAt": auction["ends_at"],
        "lockedAt": auction["locked_at"],
        "finishedAt": auction["finished_at"],
        "cancelledAt": auction["cancelled_at"],
        "cancelReason": auction["cancel_reason"],
        "seedCommitment": auction["commitment"],
        "totalKopecks": sum(item["amountKopecks"] for item in options),
        "options": options,
    }
    if auction["state"] == "FINISHED":
        payload["result"] = serialize_result(connection, int(auction["id"]))
    return payload


def current_auction(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM auctions
        ORDER BY CASE WHEN state IN ('DRAFT','OPEN','LOCKED','RESOLVING') THEN 0 ELSE 1 END,
                 id DESC LIMIT 1
        """
    ).fetchone()
    return serialize_auction(connection, row) if row is not None else None


def public_contributions(
    connection: sqlite3.Connection, auction_id: int, limit: int = 30
) -> list[dict[str, Any]]:
    fetch_auction(connection, auction_id)
    rows = connection.execute(
        """
        SELECT id FROM contributions WHERE auction_id = ?
        ORDER BY id DESC LIMIT ?
        """,
        (auction_id, limit),
    ).fetchall()
    return [
        _serialize_contribution(connection, int(row["id"]), public=True) for row in rows
    ]


def admin_contributions(
    connection: sqlite3.Connection,
    auction_id: int,
    *,
    limit: int = 200,
    before_id: int | None = None,
    contribution_id: int | None = None,
) -> list[dict[str, Any]]:
    fetch_auction(connection, auction_id)
    if contribution_id is not None:
        rows = connection.execute(
            "SELECT id FROM contributions WHERE auction_id = ? AND id = ?",
            (auction_id, contribution_id),
        ).fetchall()
    elif before_id is not None:
        rows = connection.execute(
            """
            SELECT id FROM contributions
            WHERE auction_id = ? AND id < ? ORDER BY id DESC LIMIT ?
            """,
            (auction_id, before_id, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id FROM contributions
            WHERE auction_id = ? ORDER BY id DESC LIMIT ?
            """,
            (auction_id, limit),
        ).fetchall()
    return [
        _serialize_contribution(connection, int(row["id"]), public=False) for row in rows
    ]


def audit_events(
    connection: sqlite3.Connection, auction_id: int | None, limit: int = 100
) -> list[dict[str, Any]]:
    if auction_id is None:
        rows = connection.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT * FROM audit_events WHERE auction_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (auction_id, limit),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "auctionId": row["auction_id"],
            "action": row["action"],
            "reason": row["reason"],
            "details": json.loads(row["details_json"]),
            "createdAt": row["created_at"],
            "actor": "admin" if row["session_id"] else "system",
        }
        for row in rows
    ]


class AuctionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], settings: Settings):
        self.settings = settings
        self._last_scheduler_run = 0.0
        initialize_database(settings)
        advance_auctions(settings)
        handler = partial(AuctionHandler, directory=str(settings.static_dir))
        super().__init__(address, handler)

    def service_actions(self) -> None:
        now = time.monotonic()
        if now - self._last_scheduler_run < self.settings.scheduler_interval_seconds:
            return
        self._last_scheduler_run = now
        try:
            advance_auctions(self.settings)
        except Exception as exc:  # pragma: no cover - operational logging path
            print(f"auction scheduler error: {exc}", file=sys.stderr)


class AuctionHandler(SimpleHTTPRequestHandler):
    server: AuctionServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith(f"{API_PREFIX}/"):
            self._dispatch_api("GET")
            return
        self._serve_static()

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith(f"{API_PREFIX}/"):
            self.send_json({"error": "method_not_allowed", "message": "method not allowed"}, status=405)
            return
        self._serve_static(head_only=True)

    def do_POST(self) -> None:
        self._dispatch_api("POST")

    def do_PATCH(self) -> None:
        self._dispatch_api("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch_api("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_json(
            {"error": "method_not_allowed", "message": "cross-origin requests are not supported"},
            status=405,
        )

    def _serve_static(self, *, head_only: bool = False) -> None:
        original = self.path
        try:
            if urlsplit(original).path in {"/auc", "/auc/"}:
                self.path = "/auc/index.html"
            if head_only:
                super().do_HEAD()
            else:
                super().do_GET()
        finally:
            self.path = original

    def _dispatch_api(self, method: str) -> None:
        path = urlsplit(self.path).path
        if not path.startswith(f"{API_PREFIX}/"):
            self.send_json({"error": "not_found", "message": "not found"}, status=404)
            return
        segments = [segment for segment in path[len(API_PREFIX) :].split("/") if segment]
        try:
            if method == "GET":
                self._handle_get(segments)
            elif method in {"POST", "PATCH", "DELETE"}:
                self._handle_mutation(method, segments)
            else:
                raise AuctionError("method_not_allowed", "method not allowed", 405)
        except AuctionError as exc:
            self.send_json(exc.as_dict(), status=exc.status)
        except sqlite3.IntegrityError as exc:
            print(f"auction integrity error: {exc}", file=sys.stderr)
            self.send_json(
                {"error": "conflict", "message": "the requested change conflicts with current data"},
                status=409,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "invalid_json", "message": "invalid JSON"}, status=400)
        except Exception as exc:  # pragma: no cover - unexpected operational path
            print(f"auction API error: {type(exc).__name__}: {exc}", file=sys.stderr)
            self.send_json(
                {"error": "internal_error", "message": "internal server error"}, status=500
            )

    def _handle_get(self, segments: list[str]) -> None:
        settings = self.server.settings
        if segments == ["current"]:
            advance_auctions(settings)
            with closing(connect_database(settings)) as connection:
                payload = current_auction(connection)
            self.send_json({"serverTime": isoformat(), "auction": payload})
            return

        if segments == ["admin", "session"]:
            token = self._read_session_cookie()
            with closing(connect_database(settings)) as connection:
                with transaction(connection, immediate=True):
                    session = get_admin_session(connection, settings, token, touch=True)
            self.send_json(
                {"authenticated": False, "csrfToken": None}
                if session is None
                else {
                    "authenticated": True,
                    "csrfToken": self._session_csrf_from_cookie(token),
                }
            )
            return

        if segments == ["admin", "audit"]:
            self._require_admin(mutating=False)
            query = parse_qs(urlsplit(self.path).query)
            raw_auction_id = query.get("auctionId", [None])[0]
            auction_id = self._path_id(raw_auction_id) if raw_auction_id else None
            limit = self._query_limit(query, default=100, maximum=200)
            with closing(connect_database(settings)) as connection:
                events = audit_events(connection, auction_id, limit)
            self.send_json({"serverTime": isoformat(), "events": events})
            return

        if segments == ["admin", "contributions"]:
            self._require_admin(mutating=False)
            query = parse_qs(urlsplit(self.path).query)
            raw_auction_id = query.get("auctionId", [None])[0]
            if raw_auction_id is None:
                raise AuctionError("invalid_input", "auctionId is required", 422)
            auction_id = self._path_id(raw_auction_id)
            before_id = (
                self._path_id(query["beforeId"][0]) if query.get("beforeId") else None
            )
            contribution_id = (
                self._path_id(query["contributionId"][0])
                if query.get("contributionId")
                else None
            )
            limit = self._query_limit(query, default=200, maximum=500)
            with closing(connect_database(settings)) as connection:
                contributions = admin_contributions(
                    connection,
                    auction_id,
                    limit=limit,
                    before_id=before_id,
                    contribution_id=contribution_id,
                )
            self.send_json({"serverTime": isoformat(), "contributions": contributions})
            return

        if len(segments) == 2 and segments[1] in {"result", "contributions"}:
            auction_id = self._path_id(segments[0])
            with closing(connect_database(settings)) as connection:
                fetch_auction(connection, auction_id)
                if segments[1] == "result":
                    result = serialize_result(connection, auction_id)
                    self.send_json({"serverTime": isoformat(), "result": result})
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    limit = self._query_limit(query, default=30, maximum=50)
                    contributions = public_contributions(connection, auction_id, limit)
                    self.send_json(
                        {"serverTime": isoformat(), "contributions": contributions}
                    )
            return

        raise AuctionError("not_found", "not found", 404)

    def _handle_mutation(self, method: str, segments: list[str]) -> None:
        if segments == ["admin", "login"] and method == "POST":
            self._handle_login()
            return

        session_id = self._require_admin(mutating=True)
        if segments == ["admin", "logout"] and method == "POST":
            self._handle_logout(session_id)
            return

        payload = self._read_json(required=method != "DELETE")
        settings = self.server.settings

        if segments == ["admin", "auctions"] and method == "POST":
            with closing(connect_database(settings)) as connection:
                auction_id = create_auction(connection, payload, session_id)
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"auction": auction}, status=201)
            return

        if len(segments) == 3 and segments[:2] == ["admin", "auctions"] and method == "PATCH":
            auction_id = self._path_id(segments[2])
            with closing(connect_database(settings)) as connection:
                update_auction(connection, auction_id, payload, session_id)
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"auction": auction})
            return

        if (
            len(segments) == 4
            and segments[:2] == ["admin", "auctions"]
            and segments[3] == "options"
            and method == "POST"
        ):
            auction_id = self._path_id(segments[2])
            with closing(connect_database(settings)) as connection:
                option_id = add_option(connection, auction_id, payload, session_id)
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"optionId": option_id, "auction": auction}, status=201)
            return

        if (
            len(segments) == 4
            and segments[:2] == ["admin", "auctions"]
            and segments[3] == "contributions"
            and method == "POST"
        ):
            auction_id = self._path_id(segments[2])
            with closing(connect_database(settings)) as connection:
                contribution, duplicate = add_contribution(
                    connection, auction_id, payload, session_id
                )
            self.send_json(
                {"contribution": contribution, "duplicate": duplicate},
                status=200 if duplicate else 201,
            )
            return

        if len(segments) == 4 and segments[:2] == ["admin", "auctions"] and method == "POST":
            auction_id = self._path_id(segments[2])
            action = segments[3]
            if action == "start":
                with closing(connect_database(settings)) as connection:
                    start_auction(connection, auction_id, session_id)
            elif action == "close":
                close_auction(
                    settings,
                    auction_id,
                    session_id=session_id,
                    reason="closed early by administrator",
                    force=True,
                )
            elif action == "cancel":
                with closing(connect_database(settings)) as connection:
                    cancel_auction(connection, auction_id, payload.get("reason"), session_id)
            elif action == "resolve-dispute":
                with closing(connect_database(settings)) as connection:
                    resolve_dispute(
                        connection,
                        auction_id,
                        require_id(payload.get("optionId"), "optionId"),
                        payload.get("reason"),
                        session_id,
                    )
            else:
                raise AuctionError("not_found", "not found", 404)
            with closing(connect_database(settings)) as connection:
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"auction": auction})
            return

        if len(segments) == 3 and segments[:2] == ["admin", "options"]:
            option_id = self._path_id(segments[2])
            with closing(connect_database(settings)) as connection:
                option = fetch_option(connection, option_id)
                auction_id = int(option["auction_id"])
                if method == "PATCH":
                    update_option(connection, option_id, payload, session_id)
                elif method == "DELETE":
                    delete_option(connection, option_id, session_id)
                else:
                    raise AuctionError("method_not_allowed", "method not allowed", 405)
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"auction": auction})
            return

        if (
            len(segments) == 4
            and segments[:2] == ["admin", "options"]
            and segments[3] == "merge"
            and method == "POST"
        ):
            option_id = self._path_id(segments[2])
            target_id = require_id(payload.get("targetOptionId"), "targetOptionId")
            with closing(connect_database(settings)) as connection:
                source = fetch_option(connection, option_id)
                auction_id = int(source["auction_id"])
                merge_option(connection, option_id, target_id, session_id)
                auction = serialize_auction(connection, fetch_auction(connection, auction_id))
            self.send_json({"auction": auction})
            return

        if (
            len(segments) == 4
            and segments[:2] == ["admin", "contributions"]
            and segments[3] == "void"
            and method == "POST"
        ):
            contribution_id = self._path_id(segments[2])
            with closing(connect_database(settings)) as connection:
                contribution, duplicate = void_contribution(
                    connection, contribution_id, payload, session_id
                )
            self.send_json(
                {"contribution": contribution, "duplicate": duplicate},
                status=200 if duplicate else 201,
            )
            return

        raise AuctionError("not_found", "not found", 404)

    def _handle_login(self) -> None:
        self._require_same_origin()
        payload = self._read_json(required=True)
        password = payload.get("password")
        if not isinstance(password, str) or len(password) > 1000:
            raise AuctionError("invalid_credentials", "invalid credentials", 401)
        settings = self.server.settings
        bucket = self._client_bucket_hash()
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                allowed_for_client = consume_rate_limit(
                    connection,
                    bucket,
                    "login",
                    settings.login_rate_limit,
                    settings.rate_window_seconds,
                )
                allowed_globally = consume_rate_limit(
                    connection,
                    "global",
                    "login",
                    max(20, settings.login_rate_limit * 10),
                    settings.rate_window_seconds,
                )
            if not allowed_for_client or not allowed_globally:
                raise AuctionError("rate_limited", "too many login attempts", 429)
            if not verify_password(password, settings.admin_password_hash):
                raise AuctionError("invalid_credentials", "invalid credentials", 401)
            with transaction(connection, immediate=True):
                token, csrf_token, session_id = create_admin_session(connection, settings)
                record_audit(connection, "admin_login", session_id=session_id)
        self.send_json(
            {"authenticated": True, "csrfToken": csrf_token},
            set_cookie=self._session_cookie(token),
        )

    def _handle_logout(self, session_id: str) -> None:
        settings = self.server.settings
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                connection.execute(
                    "UPDATE admin_sessions SET revoked_at = ? WHERE id = ?",
                    (isoformat(), session_id),
                )
                record_audit(connection, "admin_logout", session_id=session_id)
        self.send_json(
            {"authenticated": False}, set_cookie=self._session_cookie("", clear=True)
        )

    def _require_admin(self, *, mutating: bool) -> str:
        if mutating:
            self._require_same_origin()
        settings = self.server.settings
        token = self._read_session_cookie()
        with closing(connect_database(settings)) as connection:
            with transaction(connection, immediate=True):
                session = get_admin_session(connection, settings, token, touch=True)
                if session is None:
                    raise AuctionError("authentication_required", "authentication required", 401)
                if mutating and not check_csrf(session, self.headers.get("X-CSRF-Token")):
                    raise AuctionError("csrf_rejected", "CSRF token rejected", 403)
                if not consume_rate_limit(
                    connection,
                    session["id"],
                    "admin_mutation" if mutating else "admin_read",
                    settings.admin_rate_limit,
                    settings.rate_window_seconds,
                ):
                    raise AuctionError("rate_limited", "too many administrative requests", 429)
                return str(session["id"])

    def _session_csrf_from_cookie(self, token: str | None) -> str | None:
        if not token or not _valid_signed_token(token, self.server.settings.secret):
            return None
        # JavaScript cannot read the HttpOnly cookie. The same-origin session
        # endpoint can safely return this deterministic companion token, which
        # also keeps multiple administrator tabs from invalidating each other.
        return _csrf_token_for_session(self.server.settings.secret, token)

    def _read_json(self, *, required: bool) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise AuctionError("invalid_request", "invalid Content-Length", 400) from exc
        if length < 0 or length > self.server.settings.max_request_bytes:
            raise AuctionError("request_too_large", "request body is too large", 413)
        if length == 0:
            if required:
                return {}
            return {}
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AuctionError("unsupported_media_type", "application/json is required", 415)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise AuctionError("invalid_json", "JSON body must be an object", 400)
        return payload

    def _require_same_origin(self) -> None:
        settings = self.server.settings
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        scheme = forwarded_proto or ("https" if settings.secure_cookie else "http")
        host = self.headers.get("Host", "")
        expected = (settings.allowed_origin or f"{scheme}://{host}").rstrip("/")
        origin = self.headers.get("Origin")
        if origin:
            supplied = origin.rstrip("/")
        else:
            referer = self.headers.get("Referer")
            parsed = urlsplit(referer) if referer else None
            supplied = f"{parsed.scheme}://{parsed.netloc}" if parsed and parsed.netloc else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise AuctionError("origin_rejected", "request origin rejected", 403)

    def _read_session_cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session_cookie(self, token: str, *, clear: bool = False) -> str:
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        secure = self.server.settings.secure_cookie or forwarded_proto == "https"
        parts = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            f"Max-Age={0 if clear else self.server.settings.session_ttl_seconds}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if clear:
            parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _client_bucket_hash(self) -> str:
        raw_ip = self.client_address[0]
        try:
            address = ipaddress.ip_address(raw_ip)
            if address.is_loopback:
                forwarded = self.headers.get("X-Real-IP", "").split(",", 1)[0].strip()
                if forwarded:
                    address = ipaddress.ip_address(forwarded)
            prefix = address.compressed
        except ValueError:
            prefix = "unknown"
        return hmac.new(
            self.server.settings.secret,
            str(prefix).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _path_id(raw: Any) -> int:
        try:
            value = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise AuctionError("not_found", "not found", 404) from exc
        if value <= 0:
            raise AuctionError("not_found", "not found", 404)
        return value

    @staticmethod
    def _query_limit(
        query: Mapping[str, Sequence[str]], *, default: int, maximum: int
    ) -> int:
        raw = query.get("limit", [str(default)])[0]
        try:
            value = int(raw)
        except ValueError as exc:
            raise AuctionError("invalid_input", "limit must be an integer", 422) from exc
        return max(1, min(maximum, value))

    def send_json(
        self,
        payload: Mapping[str, Any],
        *,
        status: int = 200,
        set_cookie: str | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        if path == "/auc" or path.startswith("/auc/"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                "form-action 'self'; frame-ancestors 'self'; img-src 'self' data:; "
                "object-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'",
            )
        super().end_headers()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the MORAL SQUAD auction API")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="prompt for a password and print an AUCTION_ADMIN_PASSWORD_HASH value",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    if arguments.hash_password:
        password = getpass.getpass("Administrator password: ")
        confirmation = getpass.getpass("Repeat password: ")
        if password != confirmation:
            raise SystemExit("passwords do not match")
        print(hash_password(password))
        return

    settings = Settings.from_environment()
    host = arguments.host or settings.host
    port = arguments.port or settings.port
    if not settings.secure_cookie:
        print("warning: auction admin cookie is not Secure; local development only", file=sys.stderr)
    server = AuctionServer((host, port), settings)
    print(f"auction API listening on http://{host}:{server.server_port}")
    try:
        server.serve_forever(poll_interval=min(settings.scheduler_interval_seconds, 0.5))
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
