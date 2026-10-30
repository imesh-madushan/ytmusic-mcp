"""Serves every tool call the YouTube Music client of the user it is running for."""

import json
import logging
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread
from mcp.server.auth.middleware.auth_context import get_access_token
from ytmusicapi import YTMusic
from ytmusicapi.exceptions import YTMusicServerError

from .errors import AccountError
from .headers import build_auth_headers
from .security import CookieVault
from .store import Store

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class AccountRegistry:
    """Hands out one cached YTMusic client per user.

    With a ``store`` (HTTP mode) the user comes from the OAuth access token and
    the cookies from the encrypted database. Without one (stdio mode) a single
    local user is served from ``local_auth_file``.

    ``reconnect_url`` turns a username into a one-time browser link where that
    user can paste fresh headers; it is used in error messages.
    """

    def __init__(
        self,
        local_auth_file: Path,
        store: Store | None = None,
        vault: CookieVault | None = None,
        reconnect_url: Callable[[str], str] | None = None,
    ):
        self.local_auth_file = local_auth_file
        self.store = store
        self.vault = vault
        self.reconnect_url = reconnect_url
        self._clients: dict[str | None, tuple[float, YTMusic]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def current_username() -> str | None:
        token = get_access_token()
        return token.subject if token else None

    def has_cookies(self, username: str) -> bool:
        user = self.store.get_user(username) if self.store else None
        return bool(user and user.cookies)

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

    def _fix_hint(self, username: str) -> str:
        if self.reconnect_url:
            return f"Open {self.reconnect_url(username)} to paste fresh request headers, then retry."
        return f"Run `python -m ytmusic_mcp.cli set-cookies {username}`."

    def _load_headers(self, username: str | None) -> tuple[dict[str, str], float]:
        if self.store is None:
            return self._load_local_headers()
        if username is None:
            raise AccountError("This request is not authenticated.")
        user = self.store.get_user(username)
        if user is None:
            raise AccountError(f"Unknown user {username!r}.")
        if user.cookies is None or self.vault is None:
            raise AccountError(f"YouTube Music is not connected for {username!r}. {self._fix_hint(username)}")
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
            if "HTTP 401" not in str(exc) and "HTTP 403" not in str(exc):
                raise
            self.evict(username)
            if self.store is not None and username:
                self.store.clear_cookies(username)  # useless now; the next login asks for fresh ones
                hint = self._fix_hint(username)
            else:
                hint = "Run `python -m ytmusic_mcp.cli local-cookies` with fresh headers."
            raise AccountError(
                f"YouTube rejected the stored cookies; they have probably expired. {hint}"
            ) from exc
