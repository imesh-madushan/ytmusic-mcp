import builtins
import json
import unittest
from unittest.mock import patch

import setup_auth


class NormalizeHeadersTests(unittest.TestCase):
    def test_plain_headers_are_unchanged(self):
        raw = "Cookie: SID=fake\nX-Goog-AuthUser: 0"
        self.assertEqual(setup_auth.normalize_headers(raw), raw)

    def test_firefox_json_is_converted(self):
        raw = json.dumps({
            "requestHeaders": {
                "headers": [
                    {"name": "Cookie", "value": "SID=fake"},
                    {"name": "X-Goog-AuthUser", "value": "0"},
                ]
            }
        })
        result = setup_auth.normalize_headers(raw)
        self.assertEqual(result, "Cookie: SID=fake\nX-Goog-AuthUser: 0")

    def test_malformed_firefox_cookie_export_is_converted(self):
        raw = '''
        {"name": "Cookie",
        SID=fake"
        {"name": "X-Goog-AuthUser",
        "value": "0"
        '''
        result = setup_auth.normalize_headers(raw)
        self.assertIn("Cookie: SID=fake", result)
        self.assertIn("X-Goog-AuthUser: 0", result)


class ReadHeadersTests(unittest.TestCase):
    def test_reading_stops_at_empty_line(self):
        values = iter(["Cookie: SID=fake", "X-Goog-AuthUser: 0", "", "ignored"])
        with patch.object(builtins, "input", side_effect=values):
            result = setup_auth.read_pasted_headers()
        self.assertEqual(result, "Cookie: SID=fake\nX-Goog-AuthUser: 0")

    def test_eof_finishes_input(self):
        with patch.object(builtins, "input", side_effect=["Cookie: SID=fake", EOFError]):
            self.assertEqual(setup_auth.read_pasted_headers(), "Cookie: SID=fake")


if __name__ == "__main__":
    unittest.main()
