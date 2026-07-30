from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge.connection_profile import (
    CONNECTION_PROFILE_FILENAME,
    load_connection_profile,
    write_connection_profile,
)


class ConnectionProfileTests(unittest.TestCase):
    def test_explicit_profile_loads_non_secret_hub_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / CONNECTION_PROFILE_FILENAME
            write_connection_profile(path, "http://10.0.0.225:8765/", site_name="Studio Hub")
            with patch.dict(
                os.environ,
                {"STORYFORGE_CONNECTION_PROFILE": str(path)},
                clear=False,
            ):
                profile = load_connection_profile()
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.endpoint, "http://10.0.0.225:8765")
            self.assertEqual(profile.site_name, "Studio Hub")
            self.assertNotIn("token", path.read_text(encoding="utf-8").casefold())
            self.assertNotIn("password", path.read_text(encoding="utf-8").casefold())

    def test_environment_endpoint_is_supported_for_deployment_testing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STORYFORGE_HUB_ENDPOINT": "https://storyforge.example.test:9443/",
                "STORYFORGE_CONNECTION_PROFILE": "",
            },
            clear=False,
        ):
            profile = load_connection_profile()
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.endpoint, "https://storyforge.example.test:9443")
        self.assertEqual(profile.source, "environment")

    def test_credentials_or_url_paths_are_rejected(self) -> None:
        for endpoint in (
            "http://worker:secret@10.0.0.225:8765",
            "http://10.0.0.225:8765/api",
            "ftp://10.0.0.225:8765",
        ):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / CONNECTION_PROFILE_FILENAME
                path.write_text(
                    json.dumps(
                        {"schema_version": 1, "endpoint": endpoint}
                    ),
                    encoding="utf-8",
                )
                with patch.dict(
                    os.environ,
                    {
                        "STORYFORGE_CONNECTION_PROFILE": str(path),
                        "STORYFORGE_HUB_ENDPOINT": "",
                    },
                    clear=False,
                ):
                    with self.assertRaises(ValueError):
                        load_connection_profile()

    def test_missing_source_profile_does_not_change_standalone_runs(self) -> None:
        with patch.dict(
            os.environ,
            {
                "STORYFORGE_CONNECTION_PROFILE": "",
                "STORYFORGE_HUB_ENDPOINT": "",
            },
            clear=False,
        ):
            self.assertIsNone(load_connection_profile())

    def test_plain_http_rejects_public_hosts_but_accepts_private_lan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / CONNECTION_PROFILE_FILENAME
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                write_connection_profile(path, "http://8.8.8.8:8765")
            written = write_connection_profile(path, "http://10.0.0.225:8765")
            self.assertEqual(
                json.loads(written.read_text(encoding="utf-8"))["endpoint"],
                "http://10.0.0.225:8765",
            )


if __name__ == "__main__":
    unittest.main()
