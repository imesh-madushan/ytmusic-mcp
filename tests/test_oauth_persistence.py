"""Restart and security tests for the persistent OAuth provider."""

import asyncio
import hashlib
import sqlite3
import stat
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import AnyUrl

from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull
from oauth_provider import PasswordOAuthProvider, ResourceRefreshToken
from oauth_store import SQLiteOAuthStore

TEST_CREDENTIAL_KEY = Fernet.generate_key().decode("ascii")


def provider(database: Path) -> PasswordOAuthProvider:
    return PasswordOAuthProvider(
        "https://mcp.example.com", "ytmusic", database, TEST_CREDENTIAL_KEY
    )


def client(client_id: str = "dynamic-client") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="client-secret",
        redirect_uris=[AnyUrl("https://client.example.com/callback")],
        token_endpoint_auth_method="client_secret_basic",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="ytmusic",
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_dynamic_client_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    first = provider(database)
    run(first.register_client(client()))

    restored = run(provider(database).get_client("dynamic-client"))

    assert restored is not None
    assert restored.client_secret == "client-secret"
    assert str(restored.redirect_uris[0]) == "https://client.example.com/callback"


def test_static_client_is_persisted_and_upserted(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    first = provider(database)
    first.add_static_client("static-client", "old-secret", ["https://old.example/callback"])
    restarted = provider(database)
    assert run(restarted.get_client("static-client")).client_secret == "old-secret"

    restarted.add_static_client("static-client", "new-secret", ["https://new.example/callback"])
    updated = run(provider(database).get_client("static-client"))
    assert updated.client_secret == "new-secret"
    assert str(updated.redirect_uris[0]) == "https://new.example/callback"


def test_access_and_refresh_tokens_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    first = provider(database)
    oauth_client = client()
    run(first.register_client(oauth_client))
    issued = first._issue_tokens("dynamic-client", ["ytmusic"], "https://resource", "owner")

    restarted = provider(database)
    access = run(restarted.load_access_token(issued.access_token))
    refresh = run(restarted.load_refresh_token(oauth_client, issued.refresh_token))

    assert access is not None
    assert access.resource == "https://resource"
    assert refresh is not None
    assert refresh.subject == "owner"


def test_refresh_rotation_is_persistent_and_single_use(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    oauth_client = client()
    first = provider(database)
    run(first.register_client(oauth_client))
    issued = first._issue_tokens("dynamic-client", ["ytmusic"], None, "owner")
    original = run(first.load_refresh_token(oauth_client, issued.refresh_token))

    replacement = run(first.exchange_refresh_token(oauth_client, original, []))
    restarted = provider(database)

    assert run(restarted.load_refresh_token(oauth_client, issued.refresh_token)) is None
    assert run(restarted.load_access_token(replacement.access_token)) is not None
    assert run(restarted.load_refresh_token(oauth_client, replacement.refresh_token)) is not None
    with pytest.raises(ValueError, match="already been used or revoked"):
        run(restarted.exchange_refresh_token(oauth_client, original, []))


def test_revocation_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    first = provider(database)
    issued = first._issue_tokens("dynamic-client", ["ytmusic"], None, "owner")
    access = run(first.load_access_token(issued.access_token))
    refresh = run(first.load_refresh_token(client(), issued.refresh_token))

    run(first.revoke_token(access))
    run(first.revoke_token(refresh))
    restarted = provider(database)
    assert run(restarted.load_access_token(issued.access_token)) is None
    assert run(restarted.load_refresh_token(client(), issued.refresh_token)) is None


def test_expired_tokens_are_removed(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    store = SQLiteOAuthStore(database)
    access = AccessToken(
        token="expired-access", client_id="client", scopes=["ytmusic"], expires_at=1
    )
    refresh = ResourceRefreshToken(
        token="expired-refresh", client_id="client", scopes=["ytmusic"], expires_at=1
    )
    store.save_tokens(access, refresh)

    restarted = provider(database)
    assert run(restarted.load_access_token("expired-access")) is None
    assert run(restarted.load_refresh_token(client("client"), "expired-refresh")) is None


def test_refresh_token_cannot_cross_clients(tmp_path: Path) -> None:
    instance = provider(tmp_path / "oauth.sqlite3")
    issued = instance._issue_tokens("client-one", ["ytmusic"], None, "owner")

    assert run(instance.load_refresh_token(client("client-two"), issued.refresh_token)) is None


def test_database_hashes_bearer_tokens_and_is_private(tmp_path: Path) -> None:
    database = tmp_path / "oauth.sqlite3"
    instance = provider(database)
    issued = instance._issue_tokens("dynamic-client", ["ytmusic"], None, "owner")

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    with sqlite3.connect(database) as connection:
        access_hash, access_json = connection.execute(
            "SELECT token_hash, model_json FROM oauth_access_tokens"
        ).fetchone()
    assert access_hash == hashlib.sha256(issued.access_token.encode()).hexdigest()
    assert issued.access_token not in access_json
    assert issued.refresh_token not in database.read_bytes().decode("utf-8", errors="ignore")


