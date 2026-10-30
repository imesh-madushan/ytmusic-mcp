"""YouTube Music accounts: parsing browser cookie headers and serving one client per user.

Authentication uses the browser-cookie method of ytmusicapi: the user copies the
request headers of a logged-in music.youtube.com request. This module turns
that text into validated headers, and hands every tool call the YTMusic client
that belongs to the user it is running for.
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from ytmusicapi import YTMusic, setup
from ytmusicapi.exceptions import YTMusicServerError
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

from .security import CookieVault
from .store import Store

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class AccountError(Exception):
    """A user-facing problem with a YouTube account or its stored cookies."""


# -- cookie header parsing ----------------------------------------------------------


def normalize_headers(raw: str) -> str:
    """Convert Firefox's structured header export into plain ``Name: value`` lines."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _normalize_malformed_export(raw)

    headers = data.get("requestHeaders", {}).get("headers", []) if isinstance(data, dict) else []
    lines = [
        f"{header['name']}: {header['value']}"
        for header in headers
        if isinstance(header, dict) and "name" in header and "value" in header
    ]
    return "\n".join(lines) if lines else raw


def _normalize_malformed_export(raw: str) -> str:
    """Firefox can export a JSON-shaped block whose Cookie value is not quoted."""
    lines: list[str] = []
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
            lines.append(f"{pending_name}: {value_match.group(1)}")
            pending_name = None
        elif pending_name.lower() == "cookie" and "=" in line:
            lines.append(f"{pending_name}: {line.strip().rstrip(chr(34))}")
            pending_name = None
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
            "music.youtube.com /browse request."
        )
    origin = lowered.get("origin") or lowered.get("x-origin")
    if not origin:
        raise AccountError("The headers are missing Origin; capture a /browse request.")
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


# -- per-user clients ------------------------------------------------------------------


class AccountRegistry:
    """Serves the YTMusic client of whoever a tool call is running for.

    With a ``store`` (HTTP mode) the user comes from the OAuth access token and
    the cookies from the encrypted database. Without one (stdio mode) a single
    local user is served from ``local_auth_file``.
    """

    def __init__(self, local_auth_file: Path, store: Store | None = None, vault: CookieVault | None = None):
        self.local_auth_file = local_auth_file
        self.store = store
        self.vault = vault
        self._clients: dict[str | None, tuple[float, YTMusic]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def current_username() -> str | None:
        token = get_access_token()
        return token.subject if token else None

    def client_for(self, username: str | None) -> YTMusic:
        """Return a cached client, rebuilding it when the user's cookies changed."""
        headers, version = self._load_headers(username)
        with self._lock:
            cached = self._clients.get(username)
            if cached and cached[0] == version:
                return cached[1]
            LOGGER.info("Creating YouTube Music client for %s", username or "local user")
            client = YTMusic(headers)
            self._clients[username] = (version, client)
            return client

    def evict(self, username: str | None) -> None:
        with self._lock:
            self._clients.pop(username, None)

    def _load_headers(self, username: str | None) -> tuple[dict[str, str], float]:
        if self.store is None:
            return self._load_local_headers()
        if username is None:
            raise AccountError("This request is not authenticated.")
        user = self.store.get_user(username)
        if user is None:
            raise AccountError(f"Unknown user {username!r}.")
        if user.cookies is None or self.vault is None:
            raise AccountError(
                f"No YouTube cookies are stored for {username!r}. "
                f"Run `python -m ytmusic_mcp.cli set-cookies {username}`."
            )
        return build_auth_headers(json.loads(self.vault.decrypt(user.cookies))), user.updated_at

    def _load_local_headers(self) -> tuple[dict[str, str], float]:
        try:
            text = self.local_auth_file.read_text(encoding="utf-8")
            mtime = self.local_auth_file.stat().st_mtime
        except FileNotFoundError as exc:
            raise AccountError(
                f"No cookie file found at {self.local_auth_file}. "
                "Run `python -m ytmusic_mcp.cli local-cookies` to create one."
            ) from exc
        try:
            return build_auth_headers(json.loads(text)), mtime
        except json.JSONDecodeError as exc:
            raise AccountError(f"{self.local_auth_file} is not valid JSON.") from exc

    async def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``func(client, *args, **kwargs)`` in a worker thread for the current user."""
        username = self.current_username()
        return await anyio.to_thread.run_sync(partial(self._invoke, username, func, args, kwargs))

    def _invoke(self, username: str | None, func: Callable[..., T], args: tuple, kwargs: dict) -> T:
        client = self.client_for(username)
        try:
            return func(client, *args, **kwargs)
        except YTMusicServerError as exc:
            if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                self.evict(username)
                raise AccountError(
                    "YouTube rejected the stored cookies; they have probably expired. "
                    "Capture fresh headers and run `python -m ytmusic_mcp.cli set-cookies`."
                ) from exc
            raise
