"""
One-time browser-cookie auth setup for the YouTube Music MCP server.
OAuth and Google Cloud credentials are intentionally not supported.

Usage:
    python3 setup_auth.py

Follow the printed instructions to copy a request header from your browser's
devtools. This writes browser.json next to this script. server.py reads that
file automatically.

These credentials can expire or be invalidated if you sign out, so re-run
this script if authenticated requests begin returning 401 errors.
"""

import json
import logging
import re
from pathlib import Path

from ytmusicapi import setup

from logging_config import configure_logging

LOGGER = logging.getLogger(__name__)
AUTH_FILE = Path(__file__).resolve().with_name("browser.json")


def normalize_headers(raw: str) -> str:
    """Convert Firefox's structured request-header JSON to header lines."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Some Firefox exports contain an unquoted Cookie value, making the
        # otherwise JSON-shaped clipboard text invalid JSON.
        converted = []
        pending_name = None
        for line in raw.splitlines():
            name_match = re.search(r'"name"\s*:\s*"([^"]+)"', line)
            if name_match:
                pending_name = name_match.group(1)
                continue
            if not pending_name:
                continue
            value_match = re.search(r'"value"\s*:\s*"(.*)"\s*,?$', line)
            if value_match:
                converted.append(f"{pending_name}: {value_match.group(1)}")
                pending_name = None
            elif pending_name.lower() == "cookie" and "=" in line:
                converted.append(f"{pending_name}: {line.strip().rstrip(chr(34))}")
                pending_name = None
        return "\n".join(converted) if converted else raw

    headers = data.get("requestHeaders", {}).get("headers", [])
    if not isinstance(headers, list):
        return raw

    converted = [
        f"{header['name']}: {header['value']}"
        for header in headers
        if isinstance(header, dict) and "name" in header and "value" in header
    ]
    return "\n".join(converted) if converted else raw

def read_pasted_headers() -> str:
    """Read pasted headers until the user submits an empty line."""
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    configure_logging()
    print(
        "\nSteps:\n"
        "  1. Open music.youtube.com and make sure you're logged in.\n"
        "  2. Open DevTools (F12) -> Network, then reload the page.\n"
        "  3. Select a music.youtube.com/youtubei/v1/browse request.\n"
        "  4. Copy all request headers and paste them below.\n"
        "  5. Press Enter once more on an empty line to finish.\n"
    )
    print("Paste request headers here (finish with an empty line):")
    raw = read_pasted_headers()
    if not raw:
        LOGGER.error("No headers entered; auth file was not created")
        return 1

    LOGGER.info("Validating authentication headers")
    try:
        setup(filepath=str(AUTH_FILE), headers_raw=normalize_headers(raw))
    except Exception:
        LOGGER.exception("Authentication setup failed (header values were not logged)")
        return 1

    LOGGER.info("Authentication file saved to %s", AUTH_FILE)
    print("\nDone! You can now run: python server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
