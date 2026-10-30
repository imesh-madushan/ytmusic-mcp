import json

import pytest
from conftest import FAKE_HEADERS

from ytmusic_mcp.errors import AccountError
from ytmusic_mcp.headers import build_auth_headers, headers_from_text, normalize_headers

EXPECTED = [
    "cookie: __Secure-3PAPISID=abc123; SID=xyz",
    "x-goog-authuser: 0",
    "origin: https://music.youtube.com",
]
PAIRS = [{"name": k, "value": v} for k, v in FAKE_HEADERS.items()]
REQUIRED = {"cookie", "x-goog-authuser", "origin"}


def test_headers_from_existing_browser_json():
    assert headers_from_text(json.dumps(FAKE_HEADERS)) == FAKE_HEADERS


def test_headers_from_firefox_export():
    headers = headers_from_text(json.dumps({"requestHeaders": {"headers": PAIRS}}))
    assert headers["cookie"] == FAKE_HEADERS["cookie"]


def test_headers_from_garbage_is_a_clear_error():
    with pytest.raises(AccountError):
        headers_from_text("this is not headers")


@pytest.mark.parametrize(
    "broken, message",
    [
        ({"cookie": "a=b"}, "not browser-cookie"),
        ({"cookie": "a=b", "x-goog-authuser": "0"}, "Origin"),
        (
            {"cookie": "a=b", "x-goog-authuser": "0", "origin": "https://music.youtube.com"},
            "__Secure-3PAPISID",
        ),
    ],
)
def test_incomplete_headers_are_rejected(broken, message):
    with pytest.raises(AccountError, match=message):
        build_auth_headers(broken)


def test_build_auth_headers_adds_authorization():
    assert build_auth_headers(FAKE_HEADERS)["authorization"].startswith("SAPISIDHASH ")


@pytest.mark.parametrize(
    "raw",
    [
        "\n".join(EXPECTED),
        json.dumps(FAKE_HEADERS),
        json.dumps(PAIRS),
        json.dumps({"requestHeaders": {"headers": PAIRS}}),
        json.dumps({"request": {"headers": PAIRS}}),
        json.dumps({"log": {"entries": [{"request": {"headers": PAIRS}}]}}),
    ],
)
def test_normalize_headers_accepts_common_formats(raw):
    lines = normalize_headers(raw).splitlines()
    assert {line.split(": ", 1)[0].lower() for line in lines} >= REQUIRED


def test_normalize_headers_accepts_copied_curl():
    curl = (
        "curl 'https://music.youtube.com/youtubei/v1/next' "
        "-H 'x-goog-authuser: 0' -H \"origin: https://music.youtube.com\" "
        "-b '__Secure-3PAPISID=abc123; SID=xyz'"
    )
    assert normalize_headers(curl).splitlines() == [
        "x-goog-authuser: 0",
        "origin: https://music.youtube.com",
        "cookie: __Secure-3PAPISID=abc123; SID=xyz",
    ]


def test_normalize_headers_accepts_firefox_export_with_unquoted_cookie():
    loose = """{"requestHeaders": {"headers": [
  {
    "name": "x-goog-authuser",
    "value": "0"
  },
  {
    "name": "cookie",
    __Secure-3PAPISID=abc123; SID=xyz"""
    assert normalize_headers(loose).splitlines() == [
        "x-goog-authuser: 0",
        "cookie: __Secure-3PAPISID=abc123; SID=xyz",
    ]


def test_incomplete_curl_is_rejected_with_a_clear_error():
    curl = "curl 'https://music.youtube.com/youtubei/v1/next' -H 'x-goog-authuser: 0'"
    with pytest.raises(AccountError):
        headers_from_text(curl)
