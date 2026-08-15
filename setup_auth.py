"""Create browser-cookie credentials for the YouTube Music MCP server.

OAuth and Google Cloud credentials are intentionally not supported.
"""

import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from ytmusicapi import setup

load_dotenv(Path(__file__).resolve().with_name(".env"))

LOGGER = logging.getLogger(__name__)
DEFAULT_AUTH_FILE = Path(__file__).resolve().with_name("browser.json")
AUTH_FILE = Path(os.environ.get("YTMUSIC_AUTH_FILE", DEFAULT_AUTH_FILE)).resolve()


def normalize_headers(raw: str) -> str:
    """Normalize raw headers, browser JSON/HAR exports, and copied cURL."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Firefox can export an otherwise JSON-shaped block with an unquoted
        # Cookie value. Parse that variant without ever logging its contents.
        curl_headers = [next(value for value in match if value) for match in re.findall(r"(?:-H|--header)\s+(?:\x27([^\x27]*)\x27|\"([^\"]*)\")", raw)]
        cookie_matches = re.findall(r"(?:-b|--cookie)\s+(?:\x27([^\x27]*)\x27|\"([^\"]*)\")", raw)
        curl_headers.extend(f"cookie: {next(value for value in match if value)}" for match in cookie_matches)
        if curl_headers:
            return "\n".join(curl_headers)
        converted: list[str] = []
        pending_name: str | None = None
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
        return "\n".join(converted) if converted else raw
    headers: object = []
    if isinstance(data, list):
        headers = data
    elif isinstance(data, dict):
        request_headers = data.get("requestHeaders")
        if isinstance(request_headers, dict):
            headers = request_headers.get("headers", [])
        elif isinstance(data.get("headers"), list):
            headers = data["headers"]
        elif isinstance(data.get("request"), dict):
            headers = data["request"].get("headers", [])
        elif isinstance(data.get("log"), dict):
            entries = data["log"].get("entries", [])
            if entries and isinstance(entries[0], dict):
                headers = entries[0].get("request", {}).get("headers", [])
        elif all(isinstance(value, (str, int, float, bool)) for value in data.values()):
            return "\n".join(f"{name}: {value}" for name, value in data.items())
    if not isinstance(headers, list):
        return raw
    converted = [
        f"{header['name']}: {header['value']}"
        for header in headers
        if isinstance(header, dict) and "name" in header and "value" in header
    ]
    return "\n".join(converted) if converted else raw


def read_pasted_headers() -> str:
    """Read pasted headers until an empty line or terminal EOF."""
    lines: list[str] = []
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
    logging.basicConfig(
        level=os.environ.get("YTMUSIC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(
        "\nSteps:\n"
        "  1. Open music.youtube.com and make sure you are logged in.\n"
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

    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Validating browser-cookie authentication headers")
    try:
        setup(filepath=str(AUTH_FILE), headers_raw=normalize_headers(raw))
    except Exception:
        LOGGER.exception("Authentication setup failed (secret values were not logged)")
        return 1

    LOGGER.info("Browser-cookie credentials saved to %s", AUTH_FILE)
    print("Done. Copy browser.json to the deployment server when required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
