import io
import unittest
from unittest.mock import patch

import smoke_test


class SmokeTestTests(unittest.TestCase):
    @patch.object(smoke_test, "search_music", return_value=[])
    def test_search_only_never_adds(self, search):
        with patch.object(smoke_test, "add_tracks_to_playlist") as add:
            self.assertEqual(smoke_test.main(["test song"]), 0)
        search.assert_called_once_with("test song", filter="songs", limit=5)
        add.assert_not_called()

    @patch.object(
        smoke_test,
        "search_music",
        return_value=[{"title": "Test Song", "videoId": "video-1"}],
    )
    def test_confirmed_add_uses_first_result(self, _search):
        with patch.object(smoke_test, "add_tracks_to_playlist", return_value="Added") as add:
            self.assertEqual(smoke_test.main(["test", "--add-first-to", "PL123", "--yes"]), 0)
        add.assert_called_once_with("PL123", ["video-1"])

    @patch.object(
        smoke_test,
        "search_music",
        return_value=[{"title": "Test Song", "videoId": "video-1"}],
    )
    def test_rejected_confirmation_does_not_add(self, _search):
        with patch("builtins.input", return_value="n"):
            with patch.object(smoke_test, "add_tracks_to_playlist") as add:
                self.assertEqual(smoke_test.main(["test", "--add-first-to", "PL123"]), 0)
        add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
