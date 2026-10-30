"""The OAuth provider, driven through its public methods like the MCP SDK drives it."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from ytmusic_mcp import oauth
from ytmusic_mcp.oauth import SQLiteOAuthProvider
from ytmusic_mcp.security import hash_password, hash_token

pytestmark = pytest.mark.anyio

PASSWORD = "correct horse battery"


class FakeRequest:
    def __init__(self, **fields):
        self.fields = fields

    async def form(self):
        return self.fields


@pytest.fixture
def provider(store):
    store.create_user("alice", hash_password(PASSWORD))
    return SQLiteOAuthProvider(store, "https://mcp.example.com", "ytmusic")


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


async def test_deleting_a_user_revokes_their_tokens(provider, client, store):
    _, tokens = await full_login(provider, client)
    store.delete_user("alice")
    assert await provider.load_access_token(tokens.access_token) is None


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
