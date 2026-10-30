"""Turning copied browser request headers into validated YouTube Music credentials.

Authentication uses the browser-cookie method of ytmusicapi: the user copies the
request headers of a logged-in music.youtube.com request. People copy them in
many shapes, so `normalize_headers` accepts all the common ones.
"""

import json
import re

from ytmusicapi import setup
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

from .errors import AccountError
from .security import CookieVault

CURL_HEADER = re.compile(r"""(?:-H|--header)\s+(?:'([^']*)'|"([^"]*)")""")
CURL_COOKIE = re.compile(r"""(?:-b|--cookie)\s+(?:'([^']*)'|"([^"]*)")""")
JSON_NAME = re.compile(r'"name"\s*:\s*"([^"]+)"')
JSON_VALUE = re.compile(r'"value"\s*:\s*"(.*)"\s*,?$')


def _first_group(match: tuple[str, ...]) -> str:
    return next(value for value in match if value)


def _lines_from_curl(raw: str) -> list[str]:
    lines = [_first_group(match) for match in CURL_HEADER.findall(raw)]
    lines.extend(f"cookie: {_first_group(match)}" for match in CURL_COOKIE.findall(raw))
    return lines


def _lines_from_loose_json(raw: str) -> list[str]:
    """Firefox can export a JSON-shaped block whose Cookie value is not quoted."""
    lines: list[str] = []
    pending_name: str | None = None
    for line in raw.splitlines():
        name_match = JSON_NAME.search(line)
        if name_match:
            pending_name = name_match.group(1)
            continue
        if not pending_name:
            continue
        value_match = JSON_VALUE.search(line)
        if value_match:
            lines.append(f"{pending_name}: {value_match.group(1)}")
            pending_name = None
        elif pending_name.lower() == "cookie" and "=" in line:
            lines.append(f"{pending_name}: {line.strip().rstrip(chr(34))}")
            pending_name = None
    return lines


def _header_list(data: object) -> object:
    """Find the list of ``{"name", "value"}`` headers inside the known JSON export shapes."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    request_headers = data.get("requestHeaders")
    if isinstance(request_headers, dict):
        return request_headers.get("headers", [])
    if isinstance(data.get("headers"), list):
        return data["headers"]
    if isinstance(data.get("request"), dict):
        return data["request"].get("headers", [])
    if isinstance(data.get("log"), dict):  # HAR
        entries = data["log"].get("entries", [])
        if entries and isinstance(entries[0], dict):
            return entries[0].get("request", {}).get("headers", [])
    return []


def normalize_headers(raw: str) -> str:
    """Convert pasted headers in any common format into plain ``Name: value`` lines.

    Accepts raw header lines, a JSON object or array of headers, Firefox/Chrome
    request JSON, HAR JSON, and copied cURL. Unrecognised text is returned as is.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        lines = _lines_from_curl(raw) or _lines_from_loose_json(raw)
        return "\n".join(lines) if lines else raw

    if isinstance(data, dict) and all(isinstance(value, (str, int, float, bool)) for value in data.values()):
        return "\n".join(f"{name}: {value}" for name, value in data.items())
    headers = _header_list(data)
    lines = (
        [
            f"{header['name']}: {header['value']}"
            for header in headers
            if isinstance(header, dict) and "name" in header and "value" in header
        ]
        if isinstance(headers, list)
        else []
    )
    return "\n".join(lines) if lines else raw


def headers_from_text(text: str) -> dict[str, str]:
    """Parse pasted request headers, or an existing ``browser.json``, into a headers dict."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and any(str(key).lower() == "cookie" for key in data):
        return {str(key): str(value) for key, value in data.items()}

    try:
        # ytmusicapi validates the headers and fills in the ones it needs.
        return json.loads(setup(headers_raw=normalize_headers(text)))
    except Exception as exc:
        raise AccountError(f"Could not read the request headers: {exc}") from exc


def build_auth_headers(headers: dict[str, str]) -> dict[str, str]:
    """Validate cookie headers and add the SAPISID hash ytmusicapi uses to detect browser auth."""
    lowered = {key.lower(): value for key, value in headers.items()}
    if "cookie" not in lowered or "x-goog-authuser" not in lowered:
        raise AccountError(
            "These are not browser-cookie credentials; capture the request headers of a "
            "music.youtube.com request."
        )
    origin = lowered.get("origin") or lowered.get("x-origin")
    if not origin:
        raise AccountError("The headers are missing Origin; capture a request made by music.youtube.com.")
    try:
        sapisid = sapisid_from_cookie(lowered["cookie"])
    except (KeyError, ValueError) as exc:
        raise AccountError(
            "The cookie is missing __Secure-3PAPISID; capture the headers while logged in."
        ) from exc
    return {**headers, "authorization": get_authorization(f"{sapisid} {origin}")}


def encrypt_headers(headers: dict[str, str], vault: CookieVault) -> bytes:
    build_auth_headers(headers)  # fail now rather than on the user's first tool call
    return vault.encrypt(json.dumps(headers))
