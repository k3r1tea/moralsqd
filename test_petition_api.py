from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from petition_api import (
    PetitionServer,
    Settings,
    create_identity_token,
    initialize_database,
    validate_identity_token,
)


class PetitionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_path = Path(self.temporary_directory.name)
        self.settings = Settings(
            secret=b"test-secret-that-is-not-used-in-production",
            db_path=temporary_path / "petition.db",
            static_dir=Path(__file__).resolve().parent,
            identity_limit_per_day=5,
            post_limit_per_ten_minutes=10,
        )
        initialize_database(self.settings)
        self.server = PetitionServer(("127.0.0.1", 0), self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def opener(self) -> urllib.request.OpenerDirector:
        cookie_jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

    def get_state(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        user_agent: str = "petition-api-test",
    ) -> tuple[dict[str, object], urllib.response.addinfourl]:
        request = urllib.request.Request(
            f"{self.base_url}/api/petition",
            headers={"User-Agent": user_agent},
        )
        response = opener.open(request)
        return json.loads(response.read()), response

    def sign(
        self,
        opener: urllib.request.OpenerDirector,
        *,
        user_agent: str = "petition-api-test",
        origin: str | None = None,
    ) -> dict[str, object]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        }
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(
            f"{self.base_url}/api/petition",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        with opener.open(request) as response:
            return json.loads(response.read())

    def test_signature_is_idempotent_and_target_keeps_moving(self) -> None:
        first_browser = self.opener()
        initial, response = self.get_state(first_browser)
        self.assertEqual(initial, {"count": 0, "target": 500, "signed": False, "canSign": True})
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])

        first_signature = self.sign(first_browser)
        self.assertEqual(first_signature["count"], 1)
        self.assertEqual(first_signature["target"], 1000)
        self.assertTrue(first_signature["signed"])

        repeated_signature = self.sign(first_browser)
        self.assertEqual(repeated_signature["count"], 1)
        self.assertEqual(repeated_signature["target"], 1000)

        second_browser = self.opener()
        second_initial, _ = self.get_state(second_browser, user_agent="petition-api-test-2")
        self.assertEqual(second_initial["count"], 1)
        second_signature = self.sign(second_browser, user_agent="petition-api-test-2")
        self.assertEqual(second_signature["count"], 2)
        self.assertEqual(second_signature["target"], 1500)

    def test_identity_issuance_is_limited_after_cookie_clearing(self) -> None:
        limited_settings = Settings(
            secret=self.settings.secret,
            db_path=self.settings.db_path,
            static_dir=self.settings.static_dir,
            identity_limit_per_day=1,
            post_limit_per_ten_minutes=10,
        )
        self.server.settings = limited_settings

        first_browser = self.opener()
        first_state, _ = self.get_state(first_browser, user_agent="cookie-clear-test")
        self.assertTrue(first_state["canSign"])

        cleared_browser = self.opener()
        cleared_state, response = self.get_state(cleared_browser, user_agent="cookie-clear-test")
        self.assertFalse(cleared_state["canSign"])
        self.assertIsNone(response.headers.get("Set-Cookie"))

    def test_cross_origin_signature_is_rejected(self) -> None:
        browser = self.opener()
        self.get_state(browser)
        try:
            self.sign(browser, origin="https://example.com")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 403)
            error.close()
        else:
            self.fail("cross-origin request was not rejected")

    def test_signed_identity_token_rejects_tampering(self) -> None:
        token = create_identity_token(self.settings.secret)
        self.assertTrue(validate_identity_token(token, self.settings.secret))
        self.assertFalse(validate_identity_token(f"{token}x", self.settings.secret))
        self.assertFalse(validate_identity_token(token, b"different-secret"))


if __name__ == "__main__":
    unittest.main()
