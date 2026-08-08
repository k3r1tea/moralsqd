#!/usr/bin/env python3
"""Small same-origin API for the DK movie petition.

The service stores no names or raw IP addresses. A signed HttpOnly cookie keeps
one browser identity from signing twice. A hashed IP-prefix and User-Agent
bucket limits repeated identity issuance after cookie clearing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PETITION_ID = "dk-watches-after-v1"
COOKIE_NAME = "moralsqd_petition"


@dataclass(frozen=True)
class Settings:
    secret: bytes
    db_path: Path
    static_dir: Path
    identity_limit_per_day: int = 5
    post_limit_per_ten_minutes: int = 30
    secure_cookie: bool = False

    @classmethod
    def from_environment(cls) -> "Settings":
        project_dir = Path(__file__).resolve().parent
        raw_secret = os.environ.get("PETITION_SECRET", "local-development-only")
        return cls(
            secret=raw_secret.encode("utf-8"),
            db_path=Path(os.environ.get("PETITION_DB_PATH", project_dir / "petition.db")),
            static_dir=Path(os.environ.get("PETITION_STATIC_DIR", project_dir)),
            identity_limit_per_day=int(os.environ.get("PETITION_IDENTITY_LIMIT", "5")),
            post_limit_per_ten_minutes=int(os.environ.get("PETITION_POST_LIMIT", "30")),
            secure_cookie=os.environ.get("PETITION_SECURE_COOKIE", "0") == "1",
        )


class PetitionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings):
        self.settings = settings
        handler = partial(PetitionHandler, directory=str(settings.static_dir))
        super().__init__(address, handler)


def connect_database(settings: Settings) -> sqlite3.Connection:
    connection = sqlite3.connect(settings.db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database(settings: Settings) -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS signatures (
                petition_id TEXT NOT NULL,
                signer_hash TEXT NOT NULL,
                client_bucket_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (petition_id, signer_hash)
            );

            CREATE TABLE IF NOT EXISTS identity_issuance (
                client_bucket_hash TEXT NOT NULL,
                issued_on TEXT NOT NULL,
                issued_count INTEGER NOT NULL,
                PRIMARY KEY (client_bucket_hash, issued_on)
            );

            CREATE TABLE IF NOT EXISTS post_rate_limits (
                client_bucket_hash TEXT NOT NULL,
                window_key TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                PRIMARY KEY (client_bucket_hash, window_key)
            );
            """
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_identity_token(secret: bytes) -> str:
    payload = base64.urlsafe_b64encode(secrets.token_bytes(18)).decode("ascii").rstrip("=")
    signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_identity_token(token: str | None, secret: bytes) -> bool:
    if not token or len(token) > 128 or "." not in token:
        return False
    payload, supplied_signature = token.rsplit(".", 1)
    if not payload or len(supplied_signature) != 64:
        return False
    expected_signature = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def hash_identity(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def ip_prefix(raw_ip: str) -> str:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return "unknown"
    prefix_length = 24 if address.version == 4 else 56
    return str(ipaddress.ip_network(f"{address}/{prefix_length}", strict=False).network_address)


def signed_hash(secret: bytes, value: str) -> str:
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def petition_state(connection: sqlite3.Connection, signer_hash: str | None) -> dict[str, Any]:
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM signatures WHERE petition_id = ?", (PETITION_ID,)
        ).fetchone()[0]
    )
    signed = False
    if signer_hash:
        signed = (
            connection.execute(
                "SELECT 1 FROM signatures WHERE petition_id = ? AND signer_hash = ?",
                (PETITION_ID, signer_hash),
            ).fetchone()
            is not None
        )
    return {
        "count": count,
        "target": 500 * (count + 1),
        "signed": signed,
    }


def reserve_identity_slot(connection: sqlite3.Connection, bucket_hash: str, limit: int) -> bool:
    issued_on = utc_now().date().isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT issued_count FROM identity_issuance WHERE client_bucket_hash = ? AND issued_on = ?",
            (bucket_hash, issued_on),
        ).fetchone()
        current = int(row[0]) if row else 0
        if current >= limit:
            connection.rollback()
            return False
        connection.execute(
            """
            INSERT INTO identity_issuance (client_bucket_hash, issued_on, issued_count)
            VALUES (?, ?, 1)
            ON CONFLICT(client_bucket_hash, issued_on)
            DO UPDATE SET issued_count = issued_count + 1
            """,
            (bucket_hash, issued_on),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def allow_post(connection: sqlite3.Connection, bucket_hash: str, limit: int) -> bool:
    now = utc_now()
    window_key = f"{now:%Y-%m-%dT%H}:{now.minute // 10}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT request_count FROM post_rate_limits WHERE client_bucket_hash = ? AND window_key = ?",
            (bucket_hash, window_key),
        ).fetchone()
        current = int(row[0]) if row else 0
        if current >= limit:
            connection.rollback()
            return False
        connection.execute(
            """
            INSERT INTO post_rate_limits (client_bucket_hash, window_key, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(client_bucket_hash, window_key)
            DO UPDATE SET request_count = request_count + 1
            """,
            (bucket_hash, window_key),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


class PetitionHandler(SimpleHTTPRequestHandler):
    server: PetitionServer

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/api/petition":
            self.handle_petition_state()
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/petition":
            self.send_json({"error": "not found"}, status=404)
            return
        self.handle_petition_signature()

    def handle_petition_state(self) -> None:
        settings = self.server.settings
        bucket_hash = self.client_bucket_hash()
        token = self.read_identity_cookie()
        set_cookie: str | None = None
        can_sign = True

        with closing(connect_database(settings)) as connection:
            if not validate_identity_token(token, settings.secret):
                token = None
                can_sign = reserve_identity_slot(
                    connection, bucket_hash, settings.identity_limit_per_day
                )
                if can_sign:
                    token = create_identity_token(settings.secret)
                    set_cookie = self.identity_cookie_header(token)

            state = petition_state(connection, hash_identity(token) if token else None)

        state["canSign"] = can_sign or state["signed"]
        self.send_json(state, set_cookie=set_cookie)

    def handle_petition_signature(self) -> None:
        settings = self.server.settings
        if not self.is_same_origin_request():
            self.send_json({"error": "origin rejected"}, status=403)
            return

        content_length = self.headers.get("Content-Length", "0")
        try:
            body_size = int(content_length)
        except ValueError:
            self.send_json({"error": "invalid content length"}, status=400)
            return
        if body_size > 1024:
            self.send_json({"error": "request too large"}, status=413)
            return
        if body_size:
            try:
                json.loads(self.rfile.read(body_size))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json({"error": "invalid json"}, status=400)
                return

        token = self.read_identity_cookie()
        if not validate_identity_token(token, settings.secret):
            self.send_json({"error": "reload to receive a signing identity"}, status=428)
            return

        bucket_hash = self.client_bucket_hash()
        signer_hash = hash_identity(token)
        with closing(connect_database(settings)) as connection:
            if not allow_post(connection, bucket_hash, settings.post_limit_per_ten_minutes):
                self.send_json({"error": "too many signing attempts"}, status=429)
                return

            connection.execute(
                """
                INSERT OR IGNORE INTO signatures
                    (petition_id, signer_hash, client_bucket_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (PETITION_ID, signer_hash, bucket_hash, utc_now().isoformat()),
            )
            connection.commit()
            state = petition_state(connection, signer_hash)

        state["canSign"] = True
        self.send_json(state)

    def read_identity_cookie(self) -> str | None:
        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def identity_cookie_header(self, token: str) -> str:
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        secure = self.server.settings.secure_cookie or forwarded_proto == "https"
        parts = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            "Max-Age=34560000",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def client_bucket_hash(self) -> str:
        remote_ip = self.client_address[0]
        try:
            if ipaddress.ip_address(remote_ip).is_loopback:
                remote_ip = self.headers.get("X-Real-IP", remote_ip).split(",", 1)[0].strip()
        except ValueError:
            pass
        user_agent = self.headers.get("User-Agent", "unknown")[:300]
        bucket_value = f"{ip_prefix(remote_ip)}\n{user_agent}"
        return signed_hash(self.server.settings.secret, bucket_value)

    def is_same_origin_request(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlsplit(origin).netloc == host

    def send_json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        set_cookie: str | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(encoded)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the MORAL SQUAD petition API")
    parser.add_argument("--host", default=os.environ.get("PETITION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PETITION_PORT", "8787")))
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    settings = Settings.from_environment()
    if settings.secret == b"local-development-only":
        print(
            "warning: PETITION_SECRET is not set; use only for local development",
            file=sys.stderr,
        )
    initialize_database(settings)
    server = PetitionServer((arguments.host, arguments.port), settings)
    print(f"petition API listening on http://{arguments.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
