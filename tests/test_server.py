import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import server


class ServerHelperTests(unittest.TestCase):
    def tearDown(self):
        server._yt = None

    def test_track_summary_handles_missing_optional_fields(self):
        self.assertEqual(
            server._track_summary({"videoId": "abc", "title": "Song"}),
            {
                "videoId": "abc",
                "setVideoId": None,
                "title": "Song",
                "artists": "",
                "album": None,
                "duration": None,
            },
        )

    @patch.object(server, "get_client")
    def test_search_enforces_result_limit(self, get_client):
        get_client.return_value.search.return_value = [
            {"title": "One", "videoId": "1"},
            {"title": "Two", "videoId": "2"},
            {"title": "Three", "videoId": "3"},
        ]
        self.assertEqual(len(server.search_music("test", limit=2)), 2)

    @patch.object(server, "YTMusic")
    @patch.object(server, "load_browser_auth", return_value={"cookie": "fake"})
    def test_get_client_is_cached(self, load_auth, ytmusic):
        client = object()
        ytmusic.return_value = client
        self.assertIs(server.get_client(), client)
        self.assertIs(server.get_client(), client)
        load_auth.assert_called_once_with()
        ytmusic.assert_called_once_with({"cookie": "fake"})


class BrowserAuthValidationTests(unittest.TestCase):
    def test_valid_browser_headers_are_accepted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            path.write_text(json.dumps({
                "cookie": "__Secure-3PAPISID=fake",
                "x-goog-authuser": "0",
                "origin": "https://music.youtube.com",
            }))
            headers = server.load_browser_auth(path)
            self.assertTrue(headers["authorization"].startswith("SAPISIDHASH "))

    def test_oauth_style_file_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "oauth.json"
            path.write_text(json.dumps({"client_id": "fake", "client_secret": "fake"}))
            with self.assertRaisesRegex(RuntimeError, "OAuth files are not supported"):
                server.load_browser_auth(path)

    def test_missing_file_has_setup_instruction(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "browser.json"
            with self.assertRaisesRegex(RuntimeError, "python setup_auth.py"):
                server.load_browser_auth(path)


if __name__ == "__main__":
    unittest.main()
