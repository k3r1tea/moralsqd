from __future__ import annotations

import hashlib
import http.client
import io
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import video_suggestions_api as api


VIDEO_A = "dQw4w9WgXcQ"
VIDEO_B = "9bZkp7q19f0"
VIDEO_C = "M7lc1UVf-VE"
FIXED_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
ADMIN_PASSWORD = "correct-horse-battery-staple"
ADMIN_HASH = api.hash_password(ADMIN_PASSWORD, salt=b"0123456789abcdef")


class FakeProvider:
    def __init__(self, metadata: dict[str, api.VideoMetadata] | None = None):
        self.metadata = metadata or {}
        self.failures: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.delay = 0.0
        self.lock = threading.Lock()
        self.started_event: threading.Event | None = None
        self.release_event: threading.Event | None = None

    def fetch(self, youtube_id: str) -> api.VideoMetadata:
        with self.lock:
            self.calls.append(youtube_id)
        if self.started_event is not None:
            self.started_event.set()
        if self.release_event is not None:
            self.release_event.wait(timeout=5)
        if self.delay:
            time.sleep(self.delay)
        failure = self.failures.get(youtube_id)
        if failure:
            raise failure
        metadata = self.metadata.get(youtube_id)
        if metadata is None:
            metadata = api.VideoMetadata(
                youtube_id=youtube_id,
                title=f"Видео {youtube_id}",
                duration_seconds=185,
                published_at=api.utc_now() - timedelta(days=1),
            )
        return metadata


class SettingsMixin:
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        (root / "index.html").write_text("home", encoding="utf-8")
        (root / "dk-video-inbox").mkdir()
        (root / "dk-video-inbox" / "index.html").write_text(
            "inbox", encoding="utf-8"
        )
        self.settings = api.Settings(
            secret=b"test-secret-that-is-definitely-32-bytes-long",
            admin_password_hash=ADMIN_HASH,
            youtube_api_key="unused-test-key",
            db_path=root / "suggestions.db",
            static_dir=root,
            secure_cookie=False,
            allowed_origin=None,
        )
        self.provider = FakeProvider()


class YouTubeURLTests(unittest.TestCase):
    def test_supported_urls_canonicalize_to_one_video(self) -> None:
        urls = [
            f"https://www.youtube.com/watch?v={VIDEO_A}",
            f"https://youtube.com/watch?feature=share&v={VIDEO_A}&t=10",
            f"https://m.youtube.com/shorts/{VIDEO_A}?si=abc",
            f"https://www.youtube.com/live/{VIDEO_A}",
            f"https://www.youtube.com/embed/{VIDEO_A}",
            f"https://youtu.be/{VIDEO_A}?si=normal-share-token",
            f"https://www.youtube.com:443/watch?v={VIDEO_A}#fragment",
        ]
        for value in urls:
            with self.subTest(value=value):
                self.assertEqual(
                    api.canonicalize_youtube_url(value),
                    (VIDEO_A, f"https://www.youtube.com/watch?v={VIDEO_A}"),
                )

    def test_non_youtube_or_ambiguous_urls_are_rejected(self) -> None:
        urls = [
            f"http://youtube.com/watch?v={VIDEO_A}",
            f"https://youtube.com.evil.test/watch?v={VIDEO_A}",
            f"https://user@youtube.com/watch?v={VIDEO_A}",
            f"https://youtube.com:444/watch?v={VIDEO_A}",
            f"https://youtube.com./watch?v={VIDEO_A}",
            "https://youtube.com/playlist?list=abc",
            f"https://youtube.com/watch?v={VIDEO_A}&v={VIDEO_B}",
            "https://youtu.be/not-an-id",
            VIDEO_A,
            "",
            None,
        ]
        for value in urls:
            with self.subTest(value=value):
                with self.assertRaises(api.VideoSuggestionError) as context:
                    api.canonicalize_youtube_url(value)
                self.assertEqual(context.exception.code, "invalid_youtube_url")

    def test_duration_parser(self) -> None:
        cases = {"PT1S": 1, "PT2M3S": 123, "PT1H2M3S": 3723, "P1DT1S": 86401}
        for raw, expected in cases.items():
            self.assertEqual(api.parse_youtube_duration(raw), expected)
        for raw in ("PT0S", "PT", "", None, "one minute"):
            with self.assertRaises(ValueError):
                api.parse_youtube_duration(raw)


class FakeHTTPResponse:
    status = 200

    def __init__(self, payload: dict[str, object]):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.payload


class YouTubeProviderTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        snippet: dict[str, object] = {
            "title": "Public video",
            "publishedAt": "2026-08-10T10:00:00Z",
            "liveBroadcastContent": "none",
        }
        snippet.update(overrides.pop("snippet", {}))
        item: dict[str, object] = {
            "id": VIDEO_A,
            "snippet": snippet,
            "contentDetails": {"duration": "PT3M5S"},
            "status": {"privacyStatus": "public"},
        }
        item.update(overrides)
        return {"items": [item]}

    def test_api_key_is_header_only_and_response_is_parsed(self) -> None:
        provider = api.YouTubeDataAPIProvider("server-secret-key", timeout_seconds=3.5)
        with mock.patch.object(
            api, "urlopen", return_value=FakeHTTPResponse(self.payload())
        ) as opener:
            metadata = provider.fetch(VIDEO_A)
        request = opener.call_args.args[0]
        self.assertNotIn("server-secret-key", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "server-secret-key")
        self.assertEqual(opener.call_args.kwargs["timeout"], 3.5)
        self.assertEqual(metadata.duration_seconds, 185)
        self.assertEqual(metadata.title, "Public video")

    def test_live_private_missing_and_zero_duration_are_rejected(self) -> None:
        payloads = [
            self.payload(snippet={"liveBroadcastContent": "live"}),
            self.payload(status={"privacyStatus": "private"}),
            {"items": []},
            self.payload(contentDetails={"duration": "PT0S"}),
        ]
        provider = api.YouTubeDataAPIProvider("key")
        for payload in payloads:
            with self.subTest(payload=payload):
                with mock.patch.object(
                    api, "urlopen", return_value=FakeHTTPResponse(payload)
                ):
                    with self.assertRaises(api.VideoSuggestionError):
                        provider.fetch(VIDEO_A)


class DomainTests(SettingsMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        api.initialize_database(self.settings)

    @staticmethod
    def identity(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def submit(
        self,
        youtube_id: str,
        visitor: str,
        *,
        now: datetime = FIXED_NOW,
        provider: api.MetadataProvider | None = None,
    ) -> dict[str, object]:
        identity_hash = self.identity(visitor)
        with closing(api.connect_database(self.settings)) as connection:
            with api.transaction(connection, immediate=True):
                if not api.touch_visitor_identity(
                    connection, identity_hash, now=now
                ):
                    api.register_visitor_identity(
                        connection, identity_hash, now=now
                    )
        return api.submit_suggestion(
            self.settings,
            provider or self.provider,
            f"https://youtu.be/{youtube_id}?si=test",
            identity_hash,
            policy_accepted=True,
            now=now,
        )

    def test_one_visitor_counts_once_per_video_but_can_submit_another(self) -> None:
        first = self.submit(VIDEO_A, "visitor")
        duplicate = api.submit_suggestion(
            self.settings,
            self.provider,
            f"https://youtube.com/shorts/{VIDEO_A}",
            self.identity("visitor"),
            policy_accepted=True,
            now=FIXED_NOW,
        )
        other = self.submit(VIDEO_B, "visitor")
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["requestCount"], 1)
        self.assertEqual(other["requestCount"], 1)

    def test_different_visitors_aggregate_and_list_is_sorted(self) -> None:
        self.provider.metadata = {
            VIDEO_A: api.VideoMetadata(
                VIDEO_A, "Popular", 10, FIXED_NOW - timedelta(days=10)
            ),
            VIDEO_B: api.VideoMetadata(
                VIDEO_B, "Newer", 20, FIXED_NOW - timedelta(days=1)
            ),
        }
        self.submit(VIDEO_A, "one")
        self.submit(VIDEO_A, "two")
        self.submit(VIDEO_B, "three")
        videos = api.list_admin_videos(self.settings, now=FIXED_NOW)
        self.assertEqual([item["youtubeId"] for item in videos], [VIDEO_A, VIDEO_B])
        self.assertEqual(videos[0]["requestCount"], 2)
        self.assertEqual(videos[0]["freshness"], "moderate")
        self.assertEqual(videos[1]["freshness"], "fresh")

    def test_equal_counts_sort_by_published_date(self) -> None:
        self.provider.metadata = {
            VIDEO_A: api.VideoMetadata(
                VIDEO_A, "Old", 10, FIXED_NOW - timedelta(days=20)
            ),
            VIDEO_B: api.VideoMetadata(
                VIDEO_B, "New", 10, FIXED_NOW - timedelta(days=2)
            ),
        }
        self.submit(VIDEO_A, "one")
        self.submit(VIDEO_B, "two")
        videos = api.list_admin_videos(self.settings, now=FIXED_NOW)
        self.assertEqual([item["youtubeId"] for item in videos], [VIDEO_B, VIDEO_A])

    def test_freshness_boundaries_are_inclusive(self) -> None:
        cases = [
            (timedelta(hours=72), "fresh"),
            (timedelta(hours=72, seconds=1), "moderate"),
            (timedelta(days=14), "moderate"),
            (timedelta(days=14, seconds=1), "old"),
        ]
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(
                    api.freshness_for(FIXED_NOW - age, now=FIXED_NOW)[0], expected
                )

    def test_policy_must_be_accepted_before_provider_or_database_write(self) -> None:
        with self.assertRaises(api.VideoSuggestionError) as context:
            api.submit_suggestion(
                self.settings,
                self.provider,
                f"https://youtu.be/{VIDEO_A}",
                self.identity("visitor"),
                policy_accepted=False,
                now=FIXED_NOW,
            )
        self.assertEqual(context.exception.code, "policy_required")
        self.assertEqual(self.provider.calls, [])
        with closing(api.connect_database(self.settings)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 0)

    def test_stale_transient_failure_uses_cache_before_day_30(self) -> None:
        created = FIXED_NOW - timedelta(days=29, hours=1)
        self.provider.metadata[VIDEO_A] = api.VideoMetadata(
            VIDEO_A, "Cached", 30, created - timedelta(days=2)
        )
        self.submit(VIDEO_A, "one", now=created)
        self.provider.failures[VIDEO_A] = api.MetadataUnavailable()
        result = self.submit(VIDEO_A, "two", now=FIXED_NOW)
        self.assertEqual(result["requestCount"], 2)
        self.assertEqual(result["video"]["title"], "Cached")

    def test_day_30_failure_clears_derived_fields_and_hides_video(self) -> None:
        created = FIXED_NOW - timedelta(days=30)
        self.provider.metadata[VIDEO_A] = api.VideoMetadata(
            VIDEO_A, "Must expire", 30, created - timedelta(days=2)
        )
        self.submit(VIDEO_A, "one", now=created)
        self.provider.failures[VIDEO_A] = api.MetadataUnavailable()
        with self.assertRaises(api.MetadataUnavailable):
            self.submit(VIDEO_A, "two", now=FIXED_NOW)
        with closing(api.connect_database(self.settings)) as connection:
            row = connection.execute(
                "SELECT title, duration_seconds, published_at FROM videos WHERE youtube_id = ?",
                (VIDEO_A,),
            ).fetchone()
            self.assertEqual(tuple(row), (None, None, None))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0], 1
            )
        self.assertEqual(api.list_admin_videos(self.settings, now=FIXED_NOW), [])

    def test_terminal_refresh_failure_clears_metadata_immediately(self) -> None:
        created = FIXED_NOW - timedelta(days=29)
        self.provider.metadata[VIDEO_A] = api.VideoMetadata(
            VIDEO_A, "Deleted", 30, created - timedelta(days=2)
        )
        self.submit(VIDEO_A, "one", now=created)
        self.provider.failures[VIDEO_A] = api.VideoRejected(
            "video_not_available", "gone", 422
        )
        result = api.refresh_metadata(
            self.settings, self.provider, youtube_id=VIDEO_A, force=True, now=FIXED_NOW
        )
        self.assertEqual(result["failed"][0]["error"], "video_not_available")
        with closing(api.connect_database(self.settings)) as connection:
            row = connection.execute(
                "SELECT title, duration_seconds, published_at FROM videos"
            ).fetchone()
            self.assertEqual(tuple(row), (None, None, None))

    def test_maintenance_drains_multiple_due_batches_before_purge(self) -> None:
        created = FIXED_NOW - timedelta(days=29)
        for index, youtube_id in enumerate((VIDEO_A, VIDEO_B, VIDEO_C)):
            self.provider.metadata[youtube_id] = api.VideoMetadata(
                youtube_id,
                f"Initial {index}",
                30 + index,
                created - timedelta(days=1),
            )
            self.submit(youtube_id, f"visitor-{index}", now=created)
        self.provider.calls.clear()
        result = api.maintain_metadata(
            replace(self.settings, metadata_refresh_batch_size=2),
            self.provider,
            now=FIXED_NOW,
            refresh_limit=10,
        )
        self.assertEqual(set(result["refreshed"]), {VIDEO_A, VIDEO_B, VIDEO_C})
        self.assertEqual(result["expiredMetadataPurged"], 0)
        self.assertEqual(len(self.provider.calls), 3)

    def test_terminal_refresh_failures_do_not_starve_newer_due_video(self) -> None:
        created_times = {
            VIDEO_A: FIXED_NOW - timedelta(days=29),
            VIDEO_B: FIXED_NOW - timedelta(days=28, hours=12),
            VIDEO_C: FIXED_NOW - timedelta(days=28, hours=1),
        }
        for index, (youtube_id, created) in enumerate(created_times.items()):
            self.provider.metadata[youtube_id] = api.VideoMetadata(
                youtube_id,
                f"Initial {index}",
                60,
                created - timedelta(days=1),
            )
            self.submit(youtube_id, f"visitor-{index}", now=created)
        self.provider.calls.clear()
        self.provider.failures[VIDEO_A] = api.VideoRejected(
            "video_not_available", "gone", 422
        )
        self.provider.failures[VIDEO_B] = api.VideoRejected(
            "live_video_not_allowed", "live", 422
        )
        limited = replace(self.settings, metadata_refresh_batch_size=2)

        first = api.maintain_metadata(
            limited, self.provider, now=FIXED_NOW, refresh_limit=2
        )
        self.assertEqual(
            {item["youtubeId"] for item in first["failed"]}, {VIDEO_A, VIDEO_B}
        )
        self.provider.calls.clear()

        second = api.maintain_metadata(
            limited,
            self.provider,
            now=FIXED_NOW + timedelta(hours=12),
            refresh_limit=2,
        )
        self.assertEqual(second["refreshed"], [VIDEO_C])
        self.assertEqual(self.provider.calls, [VIDEO_C])

    def test_concurrent_new_video_uses_one_lookup_and_atomic_unique_votes(self) -> None:
        underlying = FakeProvider()
        underlying.delay = 0.08
        provider = api.SingleFlightMetadataProvider(underlying)

        def submit(index: int) -> dict[str, object]:
            return self.submit(VIDEO_A, f"visitor-{index}", provider=provider)

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(submit, range(12)))
        self.assertEqual(len(underlying.calls), 1)
        self.assertEqual(max(int(result["requestCount"]) for result in results), 12)
        with closing(api.connect_database(self.settings)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0], 12
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            duplicates = list(executor.map(lambda _: submit(0), range(8)))
        self.assertTrue(all(result["duplicate"] for result in duplicates))
        with closing(api.connect_database(self.settings)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0], 12
            )

    def test_suggestion_stores_consent_but_no_client_bucket(self) -> None:
        self.submit(VIDEO_A, "visitor")
        with closing(api.connect_database(self.settings)) as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(suggestions)")
            }
            row = connection.execute(
                "SELECT policy_accepted_at, visitor_hash FROM suggestions"
            ).fetchone()
        self.assertNotIn("client_bucket_hash", columns)
        self.assertTrue(row["policy_accepted_at"].endswith("Z"))
        self.assertEqual(len(row["visitor_hash"]), 64)


class SecurityPrimitiveTests(SettingsMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        api.initialize_database(self.settings)

    def test_password_and_session_csrf_lifecycle(self) -> None:
        self.assertTrue(api.verify_password(ADMIN_PASSWORD, ADMIN_HASH))
        self.assertFalse(api.verify_password("wrong-password", ADMIN_HASH))
        with closing(api.connect_database(self.settings)) as connection:
            with api.transaction(connection, immediate=True):
                token, csrf, _ = api.create_admin_session(
                    connection, self.settings, now=FIXED_NOW
                )
            session = api.get_admin_session(
                connection, self.settings, token, now=FIXED_NOW
            )
            self.assertIsNotNone(session)
            self.assertTrue(api.check_csrf(session, csrf))
            self.assertFalse(api.check_csrf(session, "wrong"))
            self.assertIsNone(
                api.get_admin_session(
                    connection,
                    self.settings,
                    token,
                    now=FIXED_NOW
                    + timedelta(seconds=self.settings.session_ttl_seconds),
                )
            )

    def test_fixed_window_rate_limit_is_atomic(self) -> None:
        def consume(_: int) -> bool:
            with closing(api.connect_database(self.settings)) as connection:
                with api.transaction(connection, immediate=True):
                    return api.consume_rate_limit(
                        connection, "bucket", "scope", 3, 60, now_epoch=120
                    )

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(consume, range(8)))
        self.assertEqual(sum(outcomes), 3)

    def test_secure_environment_is_fail_closed(self) -> None:
        names = {
            "VIDEO_SUGGESTIONS_SECRET": "short",
            "VIDEO_SUGGESTIONS_SECURE_COOKIE": "1",
            "VIDEO_SUGGESTIONS_ADMIN_PASSWORD_HASH": ADMIN_HASH,
            "VIDEO_SUGGESTIONS_ALLOWED_ORIGIN": "https://moralsqd.ru",
            "VIDEO_SUGGESTIONS_YOUTUBE_API_KEY": "key",
        }
        with mock.patch.dict(api.os.environ, names, clear=True):
            with self.assertRaises(ValueError):
                api.Settings.from_environment()

    def test_maintenance_margin_and_transient_exit_status(self) -> None:
        self.assertEqual(
            self.settings.metadata_refresh_seconds, 28 * 24 * 60 * 60
        )
        self.assertEqual(api.maintenance_exit_code({"failed": []}), 0)
        self.assertEqual(
            api.maintenance_exit_code(
                {"failed": [{"youtubeId": VIDEO_A, "error": "video_not_available"}]}
            ),
            0,
        )
        self.assertEqual(
            api.maintenance_exit_code(
                {"failed": [{"youtubeId": VIDEO_A, "error": "metadata_unavailable"}]}
            ),
            1,
        )

    def test_maintenance_cli_exits_nonzero_on_transient_refresh_failure(self) -> None:
        now = api.utc_now()
        metadata = api.VideoMetadata(
            VIDEO_A, "Needs refresh", 60, now - timedelta(days=40)
        )
        with closing(api.connect_database(self.settings)) as connection:
            with api.transaction(connection, immediate=True):
                api.store_metadata(
                    connection, metadata, fetched_at=now - timedelta(days=29)
                )
        environment = {
            "VIDEO_SUGGESTIONS_SECRET": self.settings.secret.decode("utf-8"),
            "VIDEO_SUGGESTIONS_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "VIDEO_SUGGESTIONS_YOUTUBE_API_KEY": "test-key",
            "VIDEO_SUGGESTIONS_DB_PATH": str(self.settings.db_path),
            "VIDEO_SUGGESTIONS_STATIC_DIR": str(self.settings.static_dir),
            "VIDEO_SUGGESTIONS_SECURE_COOKIE": "0",
        }
        output = io.StringIO()
        with (
            mock.patch.dict(api.os.environ, environment, clear=True),
            mock.patch.object(api.sys, "argv", ["video_suggestions_api.py", "--maintain-metadata"]),
            mock.patch.object(api, "urlopen", side_effect=api.URLError("offline")),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as context,
        ):
            api.main()
        self.assertEqual(context.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["failed"][0]["error"], "metadata_unavailable")

    def test_visitor_registry_cleanup_outlives_cookie(self) -> None:
        active_hash = hashlib.sha256(b"active").hexdigest()
        expired_hash = hashlib.sha256(b"expired").hexdigest()
        with closing(api.connect_database(self.settings)) as connection:
            with api.transaction(connection, immediate=True):
                api.register_visitor_identity(
                    connection,
                    active_hash,
                    now=FIXED_NOW - timedelta(days=400),
                )
                api.register_visitor_identity(
                    connection,
                    expired_hash,
                    now=FIXED_NOW - timedelta(days=402),
                )
        result = api.cleanup_database(self.settings, now=FIXED_NOW)
        self.assertEqual(result["visitorIdentitiesDeleted"], 1)
        with closing(api.connect_database(self.settings)) as connection:
            remaining = {
                row[0]
                for row in connection.execute(
                    "SELECT visitor_hash FROM visitor_identities"
                ).fetchall()
            }
        self.assertEqual(remaining, {active_hash})

    def test_visitor_registry_expiry_is_fixed_and_old_identity_cannot_submit(self) -> None:
        identity_hash = hashlib.sha256(b"fixed-expiry").hexdigest()
        with closing(api.connect_database(self.settings)) as connection:
            with api.transaction(connection, immediate=True):
                api.register_visitor_identity(
                    connection, identity_hash, now=FIXED_NOW
                )
                self.assertTrue(
                    api.touch_visitor_identity(
                        connection,
                        identity_hash,
                        now=FIXED_NOW + timedelta(days=399),
                    )
                )
            expires_at = connection.execute(
                "SELECT expires_at FROM visitor_identities WHERE visitor_hash = ?",
                (identity_hash,),
            ).fetchone()[0]
        self.assertEqual(
            expires_at,
            api.isoformat(
                FIXED_NOW
                + timedelta(seconds=api.VISITOR_REGISTRY_TTL_SECONDS)
            ),
        )

        provider = FakeProvider()
        with self.assertRaises(api.VideoSuggestionError) as context:
            api.submit_suggestion(
                self.settings,
                provider,
                f"https://youtu.be/{VIDEO_A}",
                identity_hash,
                policy_accepted=True,
                now=FIXED_NOW + timedelta(days=401),
            )
        self.assertEqual(context.exception.code, "visitor_identity_required")
        self.assertEqual(provider.calls, [])
        result = api.cleanup_database(
            self.settings, now=FIXED_NOW + timedelta(days=401)
        )
        self.assertEqual(result["visitorIdentitiesDeleted"], 1)


class HTTPIntegrationTests(SettingsMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = api.VideoSuggestionsServer(
            ("127.0.0.1", 0), self.settings, self.provider
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        cookie: str | None = None,
        csrf: str | None = None,
        origin: str | None = None,
        real_ip: str | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers: dict[str, str] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["Origin"] = self.origin if origin is None else origin
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if real_ip:
            headers["X-Real-IP"] = real_ip
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        content_type = response_headers.get("content-type", "")
        parsed = (
            json.loads(raw.decode("utf-8"))
            if raw and content_type.startswith("application/json")
            else {}
        )
        connection.close()
        return response.status, parsed, response_headers

    @staticmethod
    def cookie_from(headers: dict[str, str]) -> str:
        return headers["set-cookie"].split(";", 1)[0]

    def identity(self) -> str:
        status, payload, headers = self.request(
            "POST", f"{api.API_PREFIX}/identity", {"policyAccepted": True}
        )
        self.assertEqual(status, 201)
        self.assertTrue(payload["ready"])
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn("Max-Age=34560000", headers["set-cookie"])
        return self.cookie_from(headers)

    def submit(self, cookie: str, url: str | None = None) -> tuple[int, dict[str, object]]:
        status, payload, _ = self.request(
            "POST",
            api.API_PREFIX,
            {"url": url or f"https://youtu.be/{VIDEO_A}?si=x", "policyAccepted": True},
            cookie=cookie,
        )
        return status, payload

    def test_policy_rejection_has_no_cookie_rate_or_provider_side_effect(self) -> None:
        for value in (None, False):
            status, payload, headers = self.request(
                "POST",
                f"{api.API_PREFIX}/identity",
                {} if value is None else {"policyAccepted": value},
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "policy_required")
            self.assertNotIn("set-cookie", headers)
        self.assertEqual(self.provider.calls, [])
        with closing(api.connect_database(self.settings)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rate_limits").fetchone()[0], 0)

    def test_identity_is_required_then_duplicate_is_idempotent(self) -> None:
        status, payload = self.submit("")
        self.assertEqual(status, 428)
        self.assertEqual(payload["error"], "visitor_identity_required")
        cookie = self.identity()
        status, first = self.submit(cookie)
        self.assertEqual(status, 201)
        self.assertFalse(first["duplicate"])
        status, duplicate = self.submit(
            cookie, f"https://www.youtube.com/shorts/{VIDEO_A}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["requestCount"], 1)

    def test_second_identity_can_add_another_request(self) -> None:
        first_cookie = self.identity()
        second_cookie = self.identity()
        self.submit(first_cookie)
        _, result = self.submit(second_cookie)
        self.assertEqual(result["requestCount"], 2)

    def test_cross_origin_and_invalid_domain_are_rejected_before_provider(self) -> None:
        cookie = self.identity()
        status, payload, _ = self.request(
            "POST",
            api.API_PREFIX,
            {"url": f"https://youtu.be/{VIDEO_A}", "policyAccepted": True},
            cookie=cookie,
            origin="https://evil.test",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "origin_rejected")
        status, payload = self.submit(
            cookie, f"https://youtube.com.evil.test/watch?v={VIDEO_A}"
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "invalid_youtube_url")
        self.assertEqual(self.provider.calls, [])

    def test_global_rate_limit_happens_before_metadata_fetch(self) -> None:
        self.stop_server()
        self.server = api.VideoSuggestionsServer(
            ("127.0.0.1", 0),
            replace(self.settings, public_global_limit=1),
            self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"
        cookie = self.identity()
        self.assertEqual(self.submit(cookie)[0], 201)
        status, payload = self.submit(cookie, f"https://youtu.be/{VIDEO_B}")
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"], "rate_limited")
        self.assertEqual(self.provider.calls, [VIDEO_A])

    def test_exhausted_login_limit_rejects_before_scrypt(self) -> None:
        self.stop_server()
        self.server = api.VideoSuggestionsServer(
            ("127.0.0.1", 0),
            replace(self.settings, login_rate_limit=1),
            self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"

        status, _, _ = self.request(
            "POST",
            f"{api.API_PREFIX}/admin/login",
            {"username": "dk", "password": "wrong-password"},
        )
        self.assertEqual(status, 401)
        with mock.patch.object(api, "verify_password") as verifier:
            status, payload, _ = self.request(
                "POST",
                f"{api.API_PREFIX}/admin/login",
                {"username": "dk", "password": ADMIN_PASSWORD},
            )
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"], "rate_limited")
        verifier.assert_not_called()

    def test_exhausted_client_does_not_drain_global_login_limit(self) -> None:
        self.stop_server()
        self.server = api.VideoSuggestionsServer(
            ("127.0.0.1", 0),
            replace(self.settings, login_rate_limit=1),
            self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"

        login_path = f"{api.API_PREFIX}/admin/login"
        wrong_credentials = {"username": "dk", "password": "wrong-password"}
        self.assertEqual(self.request("POST", login_path, wrong_credentials)[0], 401)
        self.assertEqual(self.request("POST", login_path, wrong_credentials)[0], 429)
        self.assertEqual(self.request("POST", login_path, wrong_credentials)[0], 429)
        with closing(api.connect_database(self.settings)) as connection:
            global_count = connection.execute(
                """
                SELECT request_count FROM rate_limits
                WHERE scope = 'admin_login_global'
                """
            ).fetchone()[0]
        self.assertEqual(global_count, 1)

        with mock.patch.object(
            api, "verify_password", wraps=api.verify_password
        ) as verifier:
            status, _, _ = self.request(
                "POST",
                login_path,
                {"username": "dk", "password": ADMIN_PASSWORD},
                real_ip="8.8.8.8",
            )
        self.assertEqual(status, 200)
        verifier.assert_called_once()
        with closing(api.connect_database(self.settings)) as connection:
            global_count = connection.execute(
                """
                SELECT request_count FROM rate_limits
                WHERE scope = 'admin_login_global'
                """
            ).fetchone()[0]
        self.assertEqual(global_count, 2)

    def test_login_kdf_concurrency_is_bounded_and_recovers(self) -> None:
        self.stop_server()
        self.server = api.VideoSuggestionsServer(
            ("127.0.0.1", 0),
            replace(
                self.settings,
                login_rate_limit=20,
                login_kdf_slots=2,
            ),
            self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.origin = f"http://{self.host}:{self.port}"

        active = 0
        maximum_active = 0
        counter_lock = threading.Lock()
        slots_filled = threading.Event()
        release_kdf = threading.Event()

        def blocked_verify(password: str, _: str) -> bool:
            nonlocal active, maximum_active
            with counter_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    slots_filled.set()
            try:
                release_kdf.wait(timeout=5)
                return password == ADMIN_PASSWORD
            finally:
                with counter_lock:
                    active -= 1

        login_path = f"{api.API_PREFIX}/admin/login"

        def login(password: str) -> tuple[int, dict[str, object], dict[str, str]]:
            return self.request(
                "POST",
                login_path,
                {"username": "dk", "password": password},
            )

        with mock.patch.object(api, "verify_password", side_effect=blocked_verify) as verifier:
            with ThreadPoolExecutor(max_workers=6) as executor:
                blockers = [executor.submit(login, "wrong-password") for _ in range(2)]
                self.assertTrue(slots_filled.wait(timeout=2))

                started = time.monotonic()
                overflow = [executor.submit(login, "wrong-password") for _ in range(4)]
                overflow_results = [future.result(timeout=2) for future in overflow]
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertTrue(all(result[0] == 429 for result in overflow_results))
                self.assertEqual(verifier.call_count, 2)

                release_kdf.set()
                blocker_results = [future.result(timeout=2) for future in blockers]
                self.assertTrue(all(result[0] == 401 for result in blocker_results))

            status, _, _ = login(ADMIN_PASSWORD)

        self.assertEqual(status, 200)
        self.assertEqual(verifier.call_count, 3)
        self.assertEqual(maximum_active, 2)

    def test_admin_unicode_login_list_csrf_and_logout(self) -> None:
        visitor_cookie = self.identity()
        self.submit(visitor_cookie)
        status, payload, _ = self.request(
            "POST",
            f"{api.API_PREFIX}/admin/login",
            {"username": "дк", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "invalid_credentials")

        status, login, headers = self.request(
            "POST",
            f"{api.API_PREFIX}/admin/login",
            {"username": "dk", "password": ADMIN_PASSWORD},
        )
        self.assertEqual(status, 200)
        admin_cookie = self.cookie_from(headers)
        csrf = str(login["csrfToken"])
        status, session, _ = self.request(
            "GET", f"{api.API_PREFIX}/admin/session", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(session["authenticated"])
        status, inbox, _ = self.request(
            "GET", f"{api.API_PREFIX}/admin/videos", cookie=admin_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(inbox["videos"][0]["requestCount"], 1)
        self.assertEqual(
            set(inbox["videos"][0]),
            {
                "youtubeId",
                "url",
                "title",
                "durationSeconds",
                "publishedAt",
                "freshness",
                "freshnessLabel",
                "requestCount",
                "firstRequestedAt",
                "lastRequestedAt",
            },
        )
        status, payload, _ = self.request(
            "POST", f"{api.API_PREFIX}/admin/logout", {}, cookie=admin_cookie
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "csrf_rejected")
        status, payload, headers = self.request(
            "POST",
            f"{api.API_PREFIX}/admin/logout",
            {},
            cookie=admin_cookie,
            csrf=csrf,
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertIn("Max-Age=0", headers["set-cookie"])

    def test_delete_mine_removes_only_own_requests_without_youtube_call(self) -> None:
        first_cookie = self.identity()
        second_cookie = self.identity()
        self.assertEqual(self.submit(first_cookie)[0], 201)
        self.assertEqual(
            self.submit(first_cookie, f"https://youtu.be/{VIDEO_B}")[0], 201
        )
        self.assertEqual(self.submit(second_cookie)[0], 201)
        calls_before_delete = list(self.provider.calls)

        status, payload, _ = self.request(
            "POST",
            f"{api.API_PREFIX}/delete-mine",
            cookie=first_cookie,
            origin="https://evil.test",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "origin_rejected")

        status, payload, headers = self.request(
            "POST", f"{api.API_PREFIX}/delete-mine", cookie=first_cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "deletedCount": 2})
        self.assertIn("Max-Age=0", headers["set-cookie"])
        self.assertEqual(self.provider.calls, calls_before_delete)

        with closing(api.connect_database(self.settings)) as connection:
            remaining_suggestions = connection.execute(
                "SELECT youtube_id, visitor_hash FROM suggestions"
            ).fetchall()
            remaining_videos = connection.execute(
                "SELECT youtube_id FROM videos ORDER BY youtube_id"
            ).fetchall()
        self.assertEqual(len(remaining_suggestions), 1)
        self.assertEqual(remaining_suggestions[0]["youtube_id"], VIDEO_A)
        self.assertEqual([row["youtube_id"] for row in remaining_videos], [VIDEO_A])

        status, duplicate = self.submit(second_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["requestCount"], 1)

        status, ready, replacement_headers = self.request(
            "POST",
            f"{api.API_PREFIX}/identity",
            {"policyAccepted": True},
            cookie=first_cookie,
        )
        self.assertEqual(status, 201)
        self.assertTrue(ready["ready"])
        replacement_cookie = self.cookie_from(replacement_headers)
        self.assertNotEqual(replacement_cookie, first_cookie)
        old_token = first_cookie.split("=", 1)[1]
        old_hash = api.visitor_hash(self.settings.secret, old_token)
        with closing(api.connect_database(self.settings)) as connection:
            self.assertFalse(api.visitor_identity_exists(connection, old_hash))

    def test_delete_wins_against_submit_waiting_for_youtube(self) -> None:
        cookie = self.identity()
        self.provider.started_event = threading.Event()
        self.provider.release_event = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending_submit = executor.submit(self.submit, cookie)
            self.assertTrue(self.provider.started_event.wait(timeout=2))
            status, deletion, _ = self.request(
                "POST", f"{api.API_PREFIX}/delete-mine", cookie=cookie
            )
            self.assertEqual(status, 200)
            self.assertEqual(deletion, {"ok": True, "deletedCount": 0})
            self.provider.release_event.set()
            submit_status, submit_payload = pending_submit.result(timeout=3)

        self.assertEqual(submit_status, 428)
        self.assertEqual(submit_payload["error"], "visitor_identity_required")
        with closing(api.connect_database(self.settings)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM visitor_identities"
                ).fetchone()[0],
                0,
            )

    def test_static_server_does_not_expose_source_and_marks_inbox_noindex(self) -> None:
        status, _, _ = self.request("GET", "/video_suggestions_api.py")
        self.assertEqual(status, 404)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request("GET", "/dk-video-inbox/")
        response = connection.getresponse()
        response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(headers["x-robots-tag"], "noindex, nofollow, noarchive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
