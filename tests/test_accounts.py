import json

import pytest
from conftest import FAKE_HEADERS
from ytmusicapi.exceptions import YTMusicServerError

from ytmusic_mcp import accounts
from ytmusic_mcp.accounts import (
    AccountError,
    AccountRegistry,
    build_auth_headers,
    encrypt_headers,
    headers_from_text,
)
from ytmusic_mcp.security import hash_password


class FakeYTMusic:
    def __init__(self, headers):
        self.headers = headers


@pytest.fixture(autouse=True)
def fake_ytmusic(monkeypatch):
    monkeypatch.setattr(accounts, "YTMusic", FakeYTMusic)


def registry_for(username, store, vault, tmp_path):
    reg = AccountRegistry(tmp_path / "browser.json", store, vault)
    reg.current_username = lambda: username
    return reg


def test_headers_from_existing_browser_json():
    assert headers_from_text(json.dumps(FAKE_HEADERS)) == FAKE_HEADERS


def test_headers_from_firefox_export():
    export = {"requestHeaders": {"headers": [{"name": k, "value": v} for k, v in FAKE_HEADERS.items()]}}
    headers = headers_from_text(json.dumps(export))
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


def test_each_user_gets_their_own_client(store, vault, tmp_path):
    for name in ("alice", "bob"):
        store.create_user(name, hash_password("x"))
        store.set_cookies(
            name, encrypt_headers({**FAKE_HEADERS, "cookie": f"__Secure-3PAPISID={name}"}, vault)
        )
    registry = AccountRegistry(tmp_path / "unused.json", store, vault)

    alice, bob = registry.client_for("alice"), registry.client_for("bob")
    assert alice is not bob
    assert registry.client_for("alice") is alice  # cached
    assert alice.headers["cookie"] == "__Secure-3PAPISID=alice"


def test_changed_cookies_are_picked_up_without_a_restart(store, vault, tmp_path):
    store.create_user("alice", hash_password("x"))
    store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    registry = AccountRegistry(tmp_path / "unused.json", store, vault)
    before = registry.client_for("alice")

    store.set_cookies("alice", encrypt_headers({**FAKE_HEADERS, "cookie": "__Secure-3PAPISID=new"}, vault))
    assert registry.client_for("alice") is not before


def test_user_without_cookies_gets_actionable_error(store, vault, tmp_path):
    store.create_user("alice", hash_password("x"))
    registry = AccountRegistry(tmp_path / "unused.json", store, vault)
    with pytest.raises(AccountError, match="set-cookies alice"):
        registry.client_for("alice")


def test_stored_cookies_are_encrypted_at_rest(store, vault):
    store.create_user("alice", hash_password("x"))
    store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    assert b"abc123" not in store.get_user("alice").cookies


def test_local_mode_reads_the_cookie_file(tmp_path):
    path = tmp_path / "browser.json"
    path.write_text(json.dumps(FAKE_HEADERS))
    assert AccountRegistry(path).client_for(None).headers["cookie"] == FAKE_HEADERS["cookie"]
    with pytest.raises(AccountError, match="local-cookies"):
        AccountRegistry(tmp_path / "missing.json").client_for(None)


def test_hosted_mode_refuses_unauthenticated_calls(store, vault, tmp_path):
    with pytest.raises(AccountError, match="not authenticated"):
        AccountRegistry(tmp_path / "x.json", store, vault).client_for(None)


@pytest.mark.anyio
async def test_call_passes_the_callers_client(store, vault, tmp_path):
    store.create_user("alice", hash_password("x"))
    store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    registry = registry_for("alice", store, vault, tmp_path)
    assert (
        await registry.call(lambda yt, suffix: yt.headers["origin"] + suffix, "/")
        == "https://music.youtube.com/"
    )


@pytest.mark.anyio
async def test_rejected_cookies_are_evicted_and_explained(store, vault, tmp_path):
    store.create_user("alice", hash_password("x"))
    store.set_cookies("alice", encrypt_headers(FAKE_HEADERS, vault))
    registry = registry_for("alice", store, vault, tmp_path)

    def expired(yt):
        raise YTMusicServerError("Server returned HTTP 401: Unauthorized.")

    with pytest.raises(AccountError, match="expired"):
        await registry.call(expired)
    assert "alice" not in registry._clients
