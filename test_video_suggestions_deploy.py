from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

import video_suggestions_api as api


ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy"


def location_block(configuration: str, signature: str) -> str:
    start = configuration.index(signature)
    next_location = configuration.find("\nlocation ", start + len(signature))
    return configuration[start:] if next_location == -1 else configuration[start:next_location]


class VideoSuggestionsDeployTests(unittest.TestCase):
    def test_maintenance_timeout_covers_the_full_network_budget(self) -> None:
        service = (DEPLOY / "moralsqd-video-suggestions-maintenance.service").read_text(
            encoding="utf-8"
        )
        match = re.search(r"^TimeoutStartSec=(\d+)(s|min)$", service, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        timeout_seconds = int(match.group(1)) * (60 if match.group(2) == "min" else 1)
        refresh_limit = inspect.signature(api.maintain_metadata).parameters[
            "refresh_limit"
        ].default
        youtube_timeout = api.Settings.__dataclass_fields__[  # type: ignore[attr-defined]
            "youtube_timeout_seconds"
        ].default
        worst_case_network_seconds = int(refresh_limit * youtube_timeout)
        self.assertGreaterEqual(timeout_seconds, worst_case_network_seconds + 60)

    def test_timer_has_multiple_daily_refresh_opportunities(self) -> None:
        timer = (DEPLOY / "moralsqd-video-suggestions-maintenance.timer").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            len(re.findall(r"^OnCalendar=", timer, re.MULTILINE)), 2
        )
        self.assertIn("Persistent=true", timer)

    def test_nginx_keeps_private_and_data_routes_out_of_access_logs(self) -> None:
        configuration = (DEPLOY / "nginx-video-suggestions.conf").read_text(
            encoding="utf-8"
        )
        inbox_redirect = location_block(configuration, "location = /dk-video-inbox {")
        self.assertIn("access_log off;", inbox_redirect)
        self.assertIn('X-Robots-Tag "noindex, nofollow, noarchive"', inbox_redirect)

        privacy_redirect = location_block(configuration, "location = /privacy {")
        privacy_page = location_block(configuration, "location = /privacy/ {")
        privacy_assets = location_block(configuration, "location ^~ /privacy/ {")
        for block in (privacy_redirect, privacy_page, privacy_assets):
            self.assertIn("access_log off;", block)

        public_api = location_block(
            configuration,
            "location ~ ^/api/video-suggestions(?:/identity|/delete-mine)?$ {",
        )
        self.assertIn("limit_except POST", public_api)
        self.assertIn("access_log off;", public_api)


if __name__ == "__main__":
    unittest.main()
