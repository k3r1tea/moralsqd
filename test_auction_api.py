from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from auction_api import (
    COMMITMENT_DOMAIN,
    COOKIE_NAME,
    DRAW_DOMAIN,
    MAX_CONTRIBUTION_KOPECKS,
    AuctionError,
    AuctionHandler,
    AuctionServer,
    Settings,
    _valid_signed_token,
    add_contribution,
    add_option,
    cancel_auction,
    close_auction,
    connect_database,
    create_auction,
    fetch_auction,
    hash_password,
    initialize_database,
    isoformat,
    merge_option,
    option_totals,
    parse_timestamp,
    public_options,
    resolve_dispute,
    serialize_result,
    start_auction,
    update_option,
    utc_now,
    void_contribution,
)


ADMIN_PASSWORD = "test-password"
TEST_SECRET = b"auction-test-secret-with-at-least-32-bytes"
TEST_PASSWORD_HASH = ""
NO_BODY = object()
AUTO_ORIGIN = object()


def setUpModule() -> None:
    global TEST_PASSWORD_HASH
    TEST_PASSWORD_HASH = hash_password(
        ADMIN_PASSWORD,
        salt=bytes.fromhex("000102030405060708090a0b0c0d0e0f"),
    )


def make_settings(
    directory: Path,
    *,
    static_dir: Path | None = None,
    scheduler_interval_seconds: float = 0.03,
) -> Settings:
    return Settings(
        secret=TEST_SECRET,
        admin_password_hash=TEST_PASSWORD_HASH,
        db_path=directory / "auction.db",
        static_dir=static_dir or Path(__file__).resolve().parent,
        secure_cookie=False,
        login_rate_limit=50,
        admin_rate_limit=2_000,
        rate_window_seconds=60,
        scheduler_interval_seconds=scheduler_interval_seconds,
    )


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: object = NO_BODY,
        origin: object = AUTO_ORIGIN,
        csrf: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, Any, str]:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "auction-api-unittest",
            **(headers or {}),
        }
        if method in {"POST", "PATCH", "DELETE"}:
            supplied_origin = self.base_url if origin is AUTO_ORIGIN else origin
            if supplied_origin is not None:
                request_headers["Origin"] = str(supplied_origin)
        if csrf is not None:
            request_headers["X-CSRF-Token"] = csrf
        data: bytes | None = None
        if body is not NO_BODY:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            payload = (
                json.loads(raw.decode("utf-8"))
                if raw and "application/json" in content_type
                else raw
            )
            return response.status, payload, response.headers, response.geturl()

    def login(self, password: str = ADMIN_PASSWORD) -> tuple[int, dict[str, Any], Any]:
        status, payload, headers, _ = self.request(
            "/api/auc/admin/login",
            method="POST",
            body={"password": password},
        )
        return status, payload, headers


class AuctionHttpIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_log_message = AuctionHandler.log_message
        AuctionHandler.log_message = lambda *args, **kwargs: None

    @classmethod
    def tearDownClass(cls) -> None:
        AuctionHandler.log_message = cls.original_log_message

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_path = Path(self.temporary_directory.name)
        self.settings = make_settings(self.temporary_path)
        self._start_server()
        self.client = HttpClient(self.base_url)

    def tearDown(self) -> None:
        self._stop_server()
        self.temporary_directory.cleanup()

    def _start_server(self) -> None:
        self.server = AuctionServer(("127.0.0.1", 0), self.settings)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def _restart_server(self) -> None:
        self._stop_server()
        self._start_server()
        self.client.base_url = self.base_url

    def _login(self) -> str:
        status, payload, _ = self.client.login()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["authenticated"])
        self.assertIsInstance(payload["csrfToken"], str)
        return payload["csrfToken"]

    def _create_auction(
        self,
        csrf: str,
        *,
        title: str = "Тестовый аукцион",
        mode: str = "leader",
        duration: int = 600,
        description: str = "Правила теста",
    ) -> dict[str, Any]:
        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            csrf=csrf,
            body={
                "title": title,
                "description": description,
                "mode": mode,
                "durationSeconds": duration,
            },
        )
        self.assertEqual(status, 201, payload)
        return payload["auction"]

    def _add_option(self, csrf: str, auction_id: int, name: str) -> tuple[int, dict[str, Any]]:
        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/options",
            method="POST",
            csrf=csrf,
            body={"name": name},
        )
        return status, payload

    def test_exact_auc_route_serves_standalone_page(self) -> None:
        status, body, headers, final_url = self.client.request("/auc")
        self.assertEqual(status, 200)
        self.assertEqual(final_url, f"{self.base_url}/auc")
        self.assertIn(b"MORAL SQUAD", body)
        self.assertIn(b'/auc/auc.css', body)
        self.assertIn(b'/auc/auc.js', body)
        self.assertIn(b'aria-live="polite"', body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

        slash_status, slash_body, _, _ = self.client.request("/auc/")
        self.assertEqual(slash_status, 200)
        self.assertEqual(slash_body, body)

    def test_authentication_cookie_session_restore_csrf_origin_and_logout(self) -> None:
        status, payload, _, _ = self.client.request("/api/auc/admin/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])

        malformed_cookie = f'{COOKIE_NAME}="\\351.{"0" * 64}"'
        status, payload, _, _ = self.client.request(
            "/api/auc/admin/session",
            headers={"Cookie": malformed_cookie},
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["authenticated"])

        status, payload, _ = self.client.login("definitely-wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "invalid_credentials")

        status, login_payload, login_headers = self.client.login()
        self.assertEqual(status, 200, login_payload)
        csrf = login_payload["csrfToken"]
        raw_cookie = login_headers["Set-Cookie"]
        self.assertIn("HttpOnly", raw_cookie)
        self.assertIn("SameSite=Strict", raw_cookie)
        self.assertIn("Path=/", raw_cookie)
        self.assertNotIn("Secure", raw_cookie)
        parsed_cookie = SimpleCookie()
        parsed_cookie.load(raw_cookie)
        token = parsed_cookie[COOKIE_NAME].value
        self.assertTrue(_valid_signed_token(token, self.settings.secret))

        status, restored, _, _ = self.client.request("/api/auc/admin/session")
        self.assertEqual(status, 200)
        self.assertTrue(restored["authenticated"])
        restored_csrf = restored["csrfToken"]
        self.assertEqual(restored_csrf, csrf)

        create_body = {
            "title": "Auth check",
            "description": "",
            "mode": "leader",
            "durationSeconds": 60,
        }
        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions", method="POST", body=create_body
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")

        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            body=create_body,
            csrf=restored_csrf,
            origin=None,
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "origin_rejected")

        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            body=create_body,
            csrf=restored_csrf,
            origin="https://attacker.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "origin_rejected")

        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            body=create_body,
            csrf=restored_csrf,
        )
        self.assertEqual(status, 201, payload)

        status, logout_payload, logout_headers, _ = self.client.request(
            "/api/auc/admin/logout",
            method="POST",
            body={},
            csrf=restored_csrf,
        )
        self.assertEqual(status, 200)
        self.assertFalse(logout_payload["authenticated"])
        self.assertIn("Max-Age=0", logout_headers["Set-Cookie"])
        status, payload, _, _ = self.client.request("/api/auc/admin/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])

    def test_forwarded_https_marks_session_cookie_secure(self) -> None:
        https_origin = self.base_url.replace("http://", "https://", 1)
        status, payload, headers, _ = self.client.request(
            "/api/auc/admin/login",
            method="POST",
            body={"password": ADMIN_PASSWORD},
            origin=https_origin,
            headers={"X-Forwarded-Proto": "https", "User-Agent": "secure-cookie-test"},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_one_active_auction_then_cancel_allows_another(self) -> None:
        csrf = self._login()
        first = self._create_auction(csrf, title="Первый")
        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            csrf=csrf,
            body={
                "title": "Второй",
                "description": "",
                "mode": "leader",
                "durationSeconds": 60,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "active_auction_exists")

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{first['id']}/cancel",
            method="POST",
            csrf=csrf,
            body={"reason": "Тестовая отмена"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["auction"]["status"], "CANCELLED")
        second = self._create_auction(csrf, title="Второй")
        self.assertNotEqual(second["id"], first["id"])

    def test_draft_crud_merge_and_html_rejection(self) -> None:
        csrf = self._login()
        status, payload, _, _ = self.client.request(
            "/api/auc/admin/auctions",
            method="POST",
            csrf=csrf,
            body={
                "title": "<script>alert(1)</script>",
                "description": "",
                "mode": "leader",
                "durationSeconds": 60,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "invalid_input")

        auction = self._create_auction(
            csrf,
            description="Текст <b>не становится HTML</b>",
        )
        auction_id = auction["id"]
        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}",
            method="PATCH",
            csrf=csrf,
            body={"title": "Обновлённый", "durationSeconds": 120},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["auction"]["title"], "Обновлённый")
        self.assertEqual(payload["auction"]["durationSeconds"], 120)
        self.assertEqual(
            payload["auction"]["description"], "Текст <b>не становится HTML</b>"
        )

        status, payload = self._add_option(csrf, auction_id, "<img src=x>")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "invalid_input")

        option_ids: dict[str, int] = {}
        for name in ("А", "Б", "В"):
            status, payload = self._add_option(csrf, auction_id, name)
            self.assertEqual(status, 201, payload)
            option_ids[name] = payload["optionId"]

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/options/{option_ids['А']}",
            method="PATCH",
            csrf=csrf,
            body={"name": "Альфа"},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("Альфа", [item["name"] for item in payload["auction"]["options"]])

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/options/{option_ids['В']}/merge",
            method="POST",
            csrf=csrf,
            body={"targetOptionId": option_ids["Б"]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["auction"]["options"]), 2)

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/options/{option_ids['А']}",
            method="DELETE",
            csrf=csrf,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual([item["name"] for item in payload["auction"]["options"]], ["Б"])

    def test_option_names_are_unique_after_unicode_normalization(self) -> None:
        csrf = self._login()
        auction_id = self._create_auction(csrf)["id"]

        status, payload = self._add_option(csrf, auction_id, "Кот")
        self.assertEqual(status, 201, payload)
        status, payload = self._add_option(csrf, auction_id, "кот")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_option")

        status, payload = self._add_option(csrf, auction_id, "е\u0308ж")
        self.assertEqual(status, 201, payload)
        status, payload = self._add_option(csrf, auction_id, "ёж")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate_option")

    def test_admin_contribution_list_is_not_limited_to_public_window(self) -> None:
        csrf = self._login()
        auction_id = self._create_auction(csrf, mode="weighted-wheel")["id"]
        option_ids: list[int] = []
        for name in ("Первый", "Второй"):
            status, payload = self._add_option(csrf, auction_id, name)
            self.assertEqual(status, 201, payload)
            option_ids.append(payload["optionId"])

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/start",
            method="POST",
            csrf=csrf,
            body={},
        )
        self.assertEqual(status, 200, payload)

        for index in range(201):
            status, payload, _, _ = self.client.request(
                f"/api/auc/admin/auctions/{auction_id}/contributions",
                method="POST",
                csrf=csrf,
                body={
                    "optionId": option_ids[index % 2],
                    "amountKopecks": 100,
                    "requestId": f"admin-window-{index:03d}",
                },
            )
            self.assertEqual(status, 201, payload)

        status, public_payload, _, _ = self.client.request(
            f"/api/auc/{auction_id}/contributions"
        )
        self.assertEqual(status, 200, public_payload)
        self.assertEqual(len(public_payload["contributions"]), 30)

        status, admin_payload, _, _ = self.client.request(
            f"/api/auc/admin/contributions?auctionId={auction_id}&limit=200"
        )
        self.assertEqual(status, 200, admin_payload)
        self.assertEqual(len(admin_payload["contributions"]), 200)
        self.assertTrue(all("requestId" in item for item in admin_payload["contributions"]))

        before_id = admin_payload["contributions"][-1]["id"]
        status, next_payload, _, _ = self.client.request(
            f"/api/auc/admin/contributions?auctionId={auction_id}"
            f"&limit=200&beforeId={before_id}"
        )
        self.assertEqual(status, 200, next_payload)
        self.assertEqual(len(next_payload["contributions"]), 1)

    def test_start_requires_two_options_and_freezes_draft(self) -> None:
        csrf = self._login()
        auction = self._create_auction(csrf)
        auction_id = auction["id"]
        status, payload = self._add_option(csrf, auction_id, "Один")
        self.assertEqual(status, 201, payload)
        first_option_id = payload["optionId"]

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/start",
            method="POST",
            csrf=csrf,
            body={},
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "not_enough_options")

        status, _ = self._add_option(csrf, auction_id, "Два")
        self.assertEqual(status, 201)
        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/start",
            method="POST",
            csrf=csrf,
            body={},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["auction"]["status"], "OPEN")
        self.assertEqual(len(payload["auction"]["seedCommitment"]), 64)

        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/options/{first_option_id}",
            method="PATCH",
            csrf=csrf,
            body={"name": "Поздно"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "invalid_state")

    def test_finished_result_persists_across_server_restart(self) -> None:
        csrf = self._login()
        auction = self._create_auction(csrf, mode="weighted-wheel")
        auction_id = auction["id"]
        option_ids = []
        for name in ("А", "Б"):
            status, payload = self._add_option(csrf, auction_id, name)
            self.assertEqual(status, 201, payload)
            option_ids.append(payload["optionId"])
        status, _, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/start",
            method="POST",
            csrf=csrf,
            body={},
        )
        self.assertEqual(status, 200)
        status, payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/contributions",
            method="POST",
            csrf=csrf,
            body={
                "optionId": option_ids[0],
                "amountKopecks": 12_345,
                "requestId": "restart-request-001",
            },
        )
        self.assertEqual(status, 201, payload)
        status, close_payload, _, _ = self.client.request(
            f"/api/auc/admin/auctions/{auction_id}/close",
            method="POST",
            csrf=csrf,
            body={},
        )
        self.assertEqual(status, 200, close_payload)
        original_result = close_payload["auction"]["result"]

        self._restart_server()
        status, current, _, _ = self.client.request("/api/auc/current")
        self.assertEqual(status, 200, current)
        self.assertEqual(current["auction"]["status"], "FINISHED")
        self.assertEqual(current["auction"]["totalKopecks"], 12_345)
        status, result_payload, _, _ = self.client.request(
            f"/api/auc/{auction_id}/result"
        )
        self.assertEqual(status, 200, result_payload)
        self.assertEqual(result_payload["result"], original_result)


class AuctionDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_path = Path(self.temporary_directory.name)
        self.settings = make_settings(self.temporary_path)
        initialize_database(self.settings)
        self.connection = connect_database(self.settings)
        self.session_id = "domain-test-session"

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def create_started(
        self,
        *,
        mode: str = "leader",
        duration: int = 600,
        names: tuple[str, ...] = ("А", "Б"),
    ) -> tuple[int, list[int]]:
        auction_id = create_auction(
            self.connection,
            {
                "title": f"{mode} auction",
                "description": "",
                "mode": mode,
                "durationSeconds": duration,
            },
            self.session_id,
        )
        option_ids = [
            add_option(self.connection, auction_id, {"name": name}, self.session_id)
            for name in names
        ]
        start_auction(self.connection, auction_id, self.session_id)
        return auction_id, option_ids

    def contribute(
        self,
        auction_id: int,
        option_id: int,
        amount: int,
        request_id: str,
        **extra: Any,
    ) -> tuple[dict[str, Any], bool]:
        return add_contribution(
            self.connection,
            auction_id,
            {
                "optionId": option_id,
                "amountKopecks": amount,
                "requestId": request_id,
                **extra,
            },
            self.session_id,
        )

    def test_leader_tie_and_zero_total_complete_full_lifecycle(self) -> None:
        tie_id, tie_options = self.create_started(mode="leader")
        self.contribute(tie_id, tie_options[0], 10_000, "leader-tie-a-001")
        self.contribute(tie_id, tie_options[1], 10_000, "leader-tie-b-001")
        tie_result = close_auction(
            self.settings,
            tie_id,
            session_id=self.session_id,
            reason="test close",
        )
        self.assertIsNotNone(tie_result)
        self.assertIn(tie_result["winnerOptionId"], tie_options)
        self.assertEqual(tie_result["drawSpace"], 2)
        self.assertIn("Ничья", tie_result["reason"])
        self.assertEqual(fetch_auction(self.connection, tie_id)["state"], "FINISHED")

        zero_id, _ = self.create_started(mode="leader")
        zero_result = close_auction(
            self.settings,
            zero_id,
            session_id=self.session_id,
            reason="zero close",
        )
        self.assertIsNotNone(zero_result)
        self.assertIsNone(zero_result["winnerOptionId"])
        self.assertEqual(zero_result["drawSpace"], 0)
        self.assertIsNone(zero_result["hmacDigest"])
        self.assertIn("нулю", zero_result["reason"])

    def test_weighted_probabilities_and_independent_commit_reveal_verification(self) -> None:
        auction_id, option_ids = self.create_started(
            mode="weighted-wheel", names=("Фильм A", "Фильм B", "Фильм C")
        )
        amounts = (300_000, 150_000, 50_000)
        for index, (option_id, amount) in enumerate(zip(option_ids, amounts), start=1):
            self.contribute(
                auction_id,
                option_id,
                amount,
                f"weighted-request-{index:03d}",
            )

        options = public_options(self.connection, auction_id)
        self.assertEqual([item["shareBasisPoints"] for item in options], [6000, 3000, 1000])
        self.assertEqual([item["amountKopecks"] for item in options], list(amounts))

        result = close_auction(
            self.settings,
            auction_id,
            session_id=self.session_id,
            reason="weighted close",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["drawSpace"], sum(amounts))
        self.assertIn(result["winnerOptionId"], option_ids)

        seed = bytes.fromhex(result["seed"])
        independent_commitment = hashlib.sha256(COMMITMENT_DOMAIN + seed).hexdigest()
        self.assertEqual(independent_commitment, result["seedCommitment"])

        canonical = result["canonicalSnapshot"]
        parsed_snapshot = json.loads(canonical)
        self.assertEqual(
            canonical,
            json.dumps(
                parsed_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        independent_snapshot_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(independent_snapshot_hash, result["snapshotHash"])

        message = (
            DRAW_DOMAIN
            + bytes.fromhex(independent_snapshot_hash)
            + int(result["hmacCounter"]).to_bytes(8, "big")
        )
        digest = hmac.new(seed, message, hashlib.sha256).digest()
        self.assertEqual(digest.hex(), result["hmacDigest"])
        candidate = int.from_bytes(digest, "big")
        self.assertLess(candidate, int(result["rejectionLimit"]))
        self.assertEqual(candidate % result["drawSpace"], result["selectedOffset"])

        cursor = 0
        expected_winner = None
        for option in parsed_snapshot["options"]:
            cursor += option["amountKopecks"]
            if result["selectedOffset"] < cursor:
                expected_winner = option["id"]
                break
        self.assertEqual(expected_winner, result["winnerOptionId"])

    def test_request_and_provider_external_idempotency(self) -> None:
        auction_id, option_ids = self.create_started()
        payload = {
            "optionId": option_ids[0],
            "amountKopecks": 1234,
            "requestId": "idempotent-request-001",
            "provider": "mock-provider",
            "externalId": "event-001",
        }
        first, first_duplicate = add_contribution(
            self.connection, auction_id, payload, self.session_id
        )
        replay, replay_duplicate = add_contribution(
            self.connection, auction_id, payload, self.session_id
        )
        self.assertFalse(first_duplicate)
        self.assertTrue(replay_duplicate)
        self.assertEqual(replay["id"], first["id"])

        provider_replay, provider_duplicate = add_contribution(
            self.connection,
            auction_id,
            {**payload, "requestId": "idempotent-request-002"},
            self.session_id,
        )
        self.assertTrue(provider_duplicate)
        self.assertEqual(provider_replay["id"], first["id"])
        self.assertEqual(option_totals(self.connection, auction_id)[option_ids[0]], 1234)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM contributions WHERE kind = 'ENTRY'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        with self.assertRaises(AuctionError) as conflict:
            add_contribution(
                self.connection,
                auction_id,
                {**payload, "amountKopecks": 9999},
                self.session_id,
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(conflict.exception.status, 409)

    def test_invalid_bool_negative_and_too_large_amounts(self) -> None:
        auction_id, option_ids = self.create_started()
        invalid_amounts = (True, -1, MAX_CONTRIBUTION_KOPECKS + 1)
        for index, amount in enumerate(invalid_amounts):
            with self.subTest(amount=amount):
                with self.assertRaises(AuctionError) as error:
                    add_contribution(
                        self.connection,
                        auction_id,
                        {
                            "optionId": option_ids[0],
                            "amountKopecks": amount,
                            "requestId": f"invalid-amount-{index:03d}",
                        },
                        self.session_id,
                    )
                self.assertEqual(error.exception.code, "invalid_input")
                self.assertEqual(error.exception.status, 422)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM contributions").fetchone()[0],
            0,
        )

    def test_void_once_idempotently_and_ledger_rows_are_immutable(self) -> None:
        auction_id, option_ids = self.create_started()
        entry, _ = self.contribute(
            auction_id, option_ids[0], 5000, "void-source-request-001"
        )
        void_payload = {"reason": "ошибочная сумма", "requestId": "void-request-001"}
        voided, duplicate = void_contribution(
            self.connection, entry["id"], void_payload, self.session_id
        )
        self.assertFalse(duplicate)
        self.assertEqual(voided["kind"], "VOID")
        replay, duplicate = void_contribution(
            self.connection, entry["id"], void_payload, self.session_id
        )
        self.assertTrue(duplicate)
        self.assertEqual(replay["id"], voided["id"])
        self.assertEqual(option_totals(self.connection, auction_id)[option_ids[0]], 0)

        with self.assertRaises(AuctionError) as already_voided:
            void_contribution(
                self.connection,
                entry["id"],
                {"reason": "ещё раз", "requestId": "void-request-002"},
                self.session_id,
            )
        self.assertEqual(already_voided.exception.code, "already_voided")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "contributions are immutable"):
            self.connection.execute(
                "UPDATE contributions SET amount_kopecks = 1 WHERE id = ?", (entry["id"],)
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "contributions are immutable"):
            self.connection.execute("DELETE FROM contributions WHERE id = ?", (entry["id"],))

    def test_exact_deadline_rejection_and_scheduler_auto_finishes(self) -> None:
        auction_id, option_ids = self.create_started(duration=60)
        ends_at = parse_timestamp(fetch_auction(self.connection, auction_id)["ends_at"])
        with self.assertRaises(AuctionError) as rejected:
            add_contribution(
                self.connection,
                auction_id,
                {
                    "optionId": option_ids[0],
                    "amountKopecks": 100,
                    "requestId": "deadline-request-001",
                },
                self.session_id,
                now=ends_at,
            )
        self.assertEqual(rejected.exception.code, "betting_closed")

        scheduler_server = AuctionServer(("127.0.0.1", 0), self.settings)
        scheduler_thread = threading.Thread(
            target=scheduler_server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        scheduler_thread.start()
        try:
            self.connection.execute(
                "UPDATE auctions SET ends_at = ? WHERE id = ?",
                (isoformat(utc_now() - timedelta(seconds=1)), auction_id),
            )
            deadline = time.monotonic() + 2
            observed_state = "OPEN"
            while time.monotonic() < deadline:
                observed_state = fetch_auction(self.connection, auction_id)["state"]
                if observed_state == "FINISHED":
                    break
                time.sleep(0.02)
            self.assertEqual(observed_state, "FINISHED")
            self.assertIsNotNone(serialize_result(self.connection, auction_id))
        finally:
            scheduler_server.shutdown()
            scheduler_server.server_close()
            scheduler_thread.join(timeout=2)

    def test_close_contribution_race_preserves_snapshot_invariant(self) -> None:
        auction_id, option_ids = self.create_started(duration=600)
        barrier = threading.Barrier(2)

        def add_worker() -> tuple[str, str | None]:
            barrier.wait(timeout=2)
            with closing(connect_database(self.settings)) as connection:
                try:
                    add_contribution(
                        connection,
                        auction_id,
                        {
                            "optionId": option_ids[0],
                            "amountKopecks": 777,
                            "requestId": "race-request-001",
                        },
                        "race-session",
                    )
                except AuctionError as error:
                    return "rejected", error.code
                return "accepted", None

        def close_worker() -> str:
            barrier.wait(timeout=2)
            result = close_auction(
                self.settings,
                auction_id,
                session_id="race-session",
                reason="race close",
            )
            return "closed" if result is not None else "not-closed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            add_future = executor.submit(add_worker)
            close_future = executor.submit(close_worker)
            add_outcome = add_future.result(timeout=5)
            close_outcome = close_future.result(timeout=5)

        self.assertEqual(close_outcome, "closed")
        self.assertIn(add_outcome, (("accepted", None), ("rejected", "betting_closed")))
        snapshot = self.connection.execute(
            "SELECT total_kopecks FROM snapshots WHERE auction_id = ?", (auction_id,)
        ).fetchone()
        self.assertIsNotNone(snapshot)
        ledger_total = sum(option_totals(self.connection, auction_id).values())
        self.assertEqual(int(snapshot["total_kopecks"]), ledger_total)
        self.assertEqual(ledger_total, 777 if add_outcome[0] == "accepted" else 0)
        self.assertEqual(fetch_auction(self.connection, auction_id)["state"], "FINISHED")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM results WHERE auction_id = ?", (auction_id,)
            ).fetchone()[0],
            1,
        )

    def test_result_snapshot_and_audit_are_immutable(self) -> None:
        auction_id, option_ids = self.create_started()
        self.contribute(auction_id, option_ids[0], 1000, "immutable-request-001")
        close_auction(
            self.settings,
            auction_id,
            session_id=self.session_id,
            reason="immutable close",
        )
        checks = (
            ("UPDATE results SET reason = 'changed' WHERE auction_id = ?", "results"),
            ("DELETE FROM snapshots WHERE auction_id = ?", "snapshots"),
            ("UPDATE audit_events SET action = 'changed' WHERE auction_id = ?", "audit events"),
        )
        for statement, expected_message in checks:
            with self.subTest(table=expected_message):
                with self.assertRaisesRegex(sqlite3.IntegrityError, f"{expected_message} are immutable"):
                    self.connection.execute(statement, (auction_id,))

    def test_dispute_override_preserves_original_result_and_proof(self) -> None:
        auction_id, option_ids = self.create_started()
        self.contribute(auction_id, option_ids[0], 1000, "dispute-request-001")
        original = close_auction(
            self.settings,
            auction_id,
            session_id=self.session_id,
            reason="normal close",
        )
        self.assertEqual(original["winnerOptionId"], option_ids[0])
        proof_before = {
            key: original[key]
            for key in (
                "seed",
                "seedCommitment",
                "snapshotHash",
                "canonicalSnapshot",
                "hmacDigest",
                "selectedOffset",
            )
        }

        resolve_dispute(
            self.connection,
            auction_id,
            option_ids[1],
            "Модератор проверил спорную ситуацию",
            self.session_id,
        )
        effective = serialize_result(self.connection, auction_id)
        self.assertTrue(effective["forced"])
        self.assertEqual(effective["winnerOptionId"], option_ids[1])
        self.assertEqual(effective["originalWinnerOptionId"], option_ids[0])
        self.assertEqual(effective["moderatorResolution"]["reason"], "Модератор проверил спорную ситуацию")
        self.assertEqual(
            {key: effective[key] for key in proof_before},
            proof_before,
        )
        stored_winner = self.connection.execute(
            "SELECT winner_option_id FROM results WHERE auction_id = ?", (auction_id,)
        ).fetchone()[0]
        self.assertEqual(stored_winner, option_ids[0])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM dispute_resolutions WHERE auction_id = ?", (auction_id,)
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
