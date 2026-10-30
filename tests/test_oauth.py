"""The OAuth provider, driven through its public methods like the MCP SDK drives it."""

import json
import sqlite3
import time
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import FAKE_HEADERS
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from ytmusic_mcp import oauth
from ytmusic_mcp.config import StaticClient
from ytmusic_mcp.headers import encrypt_headers
from ytmusic_mcp.oauth import SQLiteOAuthProvider
from ytmusic_mcp.security import hash_password, hash_token
from ytmusic_mcp.store import Store

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"


class FakeRequest:
    def __init__(self, **fields):
        self.fields = fields

    async def form(self):
        return self.fields


@pytest.fixture
def provider(store, vault):
    """A provider with alice (YouTube already connected) and bob (not connected yet)."""
    store.create_user("alice", hash_password(PASSWORD))
    store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    store.create_user("bob", hash_password(PASSWORD))
    return SQLiteOAuthProvider(store, "https://mcp.example.com", "ytmusic", vault)


@pytest.fixture
def client():
    return OAuthClientInformationFull(
        client_id="client-1",
        client_name="Claude",
        redirect_uris=[AnyUrl("https://claude.ai/callback")],
        token_endpoint_auth_method="none",
    )


def authorization_params():
    return AuthorizationParams(
        state="client-state",
        scopes=None,
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://claude.ai/callback"),
        redirect_uri_provided_explicitly=True,
    )


async def start_login(provider, client) -> str:
    await provider.register_client(client)
    url = await provider.authorize(client, authorization_params())
    assert url.startswith("https://mcp.example.com/login?state=")
    return parse_qs(urlparse(url).query)["state"][0]


async def log_in(provider, state, password=PASSWORD):
    return await provider.login_submit(FakeRequest(state=state, username="alice", password=password))


async def full_login(provider, client):
    """Run authorize -> login -> code exchange and return (code, tokens)."""
    state = await start_login(provider, client)
    redirect = await log_in(provider, state)
    assert redirect.status_code == 302
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["state"] == ["client-state"]
    code = await provider.load_authorization_code(client, query["code"][0])
    return code, await provider.exchange_authorization_code(client, code)


async def test_login_page_names_the_client_and_forbids_framing(provider, client):
    state = await start_login(provider, client)
    response = await provider.login_page(state)
    page = response.body.decode()
    assert response.status_code == 200
    assert "Claude" in page and "claude.ai" in page
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


async def test_login_page_rejects_unknown_state(provider):
    assert (await provider.login_page("nope")).status_code == 400


async def test_wrong_password_is_rejected_then_locked_out(provider, client):
    state = await start_login(provider, client)
    for _ in range(oauth.MAX_LOGIN_ATTEMPTS - 1):
        assert (await log_in(provider, state, "wrong")).status_code == 401
    assert (await log_in(provider, state, "wrong")).status_code == 429
    # The request is gone, so even the right password no longer works.
    assert (await log_in(provider, state)).status_code == 400


async def test_unknown_user_gets_the_same_error_as_a_wrong_password(provider, client):
    state = await start_login(provider, client)
    response = await provider.login_submit(FakeRequest(state=state, username="mallory", password="x"))
    assert response.status_code == 401
    assert "Invalid username or password" in response.body.decode()


async def test_tokens_identify_the_user(provider, client):
    code, tokens = await full_login(provider, client)
    access = await provider.load_access_token(tokens.access_token)
    assert access.subject == "alice"
    assert access.scopes == ["ytmusic"]
    assert code.subject == "alice"


async def test_authorization_code_is_single_use(provider, client):
    code, _ = await full_login(provider, client)
    assert await provider.load_authorization_code(client, code.code) is None


async def test_refresh_rotates_and_keeps_the_user(provider, client):
    _, tokens = await full_login(provider, client)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    renewed = await provider.exchange_refresh_token(client, refresh, [])
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    assert (await provider.load_access_token(renewed.access_token)).subject == "alice"


async def test_refresh_cannot_widen_scope(provider, client):
    _, tokens = await full_login(provider, client)
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, ["ytmusic", "admin"])


async def test_revoking_one_token_revokes_the_whole_session(provider, client):
    _, tokens = await full_login(provider, client)
    access = await provider.load_access_token(tokens.access_token)
    await provider.revoke_token(access)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_expired_access_token_is_not_accepted(provider, client, store):
    store.save_token(
        hash_token("old"), "access", "fam", "client-1", "alice", ["ytmusic"], None, time.time() - 1
    )
    assert await provider.load_access_token("old") is None


async def test_registration_is_capped(provider, client, monkeypatch):
    monkeypatch.setattr(oauth, "MAX_DYNAMIC_CLIENTS", 1)
    await provider.register_client(client)
    other = client.model_copy(update={"client_id": "client-2"})
    with pytest.raises(RegistrationError):
        await provider.register_client(other)


async def test_expired_login_requests_are_purged(provider, client, store):
    store.save_pending(hash_token("stale"), "client-1", "{}", time.time() - 1)
    assert store.get_pending(hash_token("stale")) is None
    store.purge_expired()
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pending_logins").fetchone()[0] == 0


# -- connecting YouTube during login ----------------------------------------------------------


async def log_in_as(provider, state, username):
    return await provider.login_submit(FakeRequest(state=state, username=username, password=PASSWORD))


async def test_user_without_cookies_is_asked_for_headers_before_the_grant(provider, client, store):
    state = await start_login(provider, client)
    page = await log_in_as(provider, state, "bob")
    assert page.status_code == 200
    assert "/youtube-auth/callback" in page.body.decode()
    assert store.get_pending(hash_token(state)) is not None  # no authorization code issued yet

    redirect = await provider.youtube_submit(
        FakeRequest(state=state, ticket="", headers=json.dumps(FAKE_HEADERS))
    )
    assert redirect.status_code == 302
    assert "code=" in redirect.headers["location"]
    assert store.get_user("bob").cookies is not None


async def test_bad_headers_are_rejected_and_nothing_is_stored(provider, client, store):
    state = await start_login(provider, client)
    await log_in_as(provider, state, "bob")
    response = await provider.youtube_submit(FakeRequest(state=state, ticket="", headers="not headers"))
    assert response.status_code == 400
    assert store.get_user("bob").cookies is None


async def test_headers_cannot_be_submitted_without_logging_in_first(provider, client, store):
    state = await start_login(provider, client)
    response = await provider.youtube_submit(
        FakeRequest(state=state, ticket="", headers=json.dumps(FAKE_HEADERS))
    )
    assert response.status_code == 403
    assert store.get_user("bob").cookies is None


async def test_reconnect_link_replaces_cookies_once(provider, store):
    ticket = provider.reconnect_url("alice").split("ticket=")[1]
    assert (await provider.youtube_page("", ticket)).status_code == 200

    new_headers = {**FAKE_HEADERS, "cookie": "__Secure-3PAPISID=fresh"}
    response = await provider.youtube_submit(
        FakeRequest(state="", ticket=ticket, headers=json.dumps(new_headers))
    )
    assert response.status_code == 200
    assert "connected" in response.body.decode().lower()

    reuse = await provider.youtube_submit(
        FakeRequest(state="", ticket=ticket, headers=json.dumps(new_headers))
    )
    assert reuse.status_code == 400  # single use


async def test_unknown_reconnect_ticket_is_refused(provider):
    assert (await provider.youtube_page("", "made-up")).status_code == 400


# -- persistence across restarts --------------------------------------------------------------


async def test_clients_and_tokens_survive_a_restart(tmp_path, vault, client):
    path = tmp_path / "persist.db"
    first_store = Store(path)
    first_store.create_user("alice", hash_password(PASSWORD))
    first_store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    first = SQLiteOAuthProvider(first_store, "https://mcp.example.com", "ytmusic", vault)
    _, tokens = await full_login(first, client)

    restarted = SQLiteOAuthProvider(Store(path), "https://mcp.example.com", "ytmusic", vault)
    assert (await restarted.get_client("client-1")).client_name == "Claude"
    assert (await restarted.load_access_token(tokens.access_token)).subject == "alice"
    assert await restarted.load_refresh_token(client, tokens.refresh_token) is not None


async def test_static_client_is_upserted(store, vault):
    provider = SQLiteOAuthProvider(store, "https://mcp.example.com", "ytmusic", vault)
    provider.add_static_client(StaticClient("static", "old-secret", ["https://old.example/cb"]))
    provider.add_static_client(StaticClient("static", "new-secret", ["https://new.example/cb"]))
    stored = await provider.get_client("static")
    assert stored.client_secret == "new-secret"
    assert str(stored.redirect_uris[0]) == "https://new.example/cb"


async def test_refresh_token_cannot_cross_clients(provider, client):
    _, tokens = await full_login(provider, client)
    other = client.model_copy(update={"client_id": "client-2"})
    assert await provider.load_refresh_token(other, tokens.refresh_token) is None


async def test_tokens_are_only_stored_hashed(provider, client, store):
    _, tokens = await full_login(provider, client)
    stored = store.path.read_bytes()
    assert tokens.access_token.encode() not in stored
    assert tokens.refresh_token.encode() not in stored


def test_database_from_the_older_layout_is_refused(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    with pytest.raises(RuntimeError, match="older version"):
        Store(path)
